#!/usr/bin/env python3
"""worktree.py: one `git worktree` per subject off the domain repo, made CHARTER-FREE.

The worker's cwd must carry no charter (CLAUDE.md / AGENTS.md / *-AGENT.md),
so a stripped-charter worker (`--system-prompt` REPLACE + `--setting-sources ''`) doesn't
silently re-inherit one from the working directory. This is belt and suspenders with the flags.

Value-free: git plumbing only, no secrets.
"""
import os, re, shutil, subprocess, logging, time

log = logging.getLogger("eph-broker.worktree")

# mr_diff retry backoff (seconds between attempts) for an empty MR diff right after open.
# mr_diff is a LOCAL `git diff` (never GitLab's MR-diff API), so GitLab's async diff generation is not
# what it waits on; an earlier empty diff was a wrong CUT (main's sha), not latency.
# The backoff only rides out a just-pushed branch/target not yet visible to fetch: about 5 min total, kept.
MR_DIFF_BACKOFF_S = (5, 10, 20, 30, 45, 60, 60, 70)
# make(expect_sha=...) refetch and recut backoff when the cut SHA is not the expected MR head.
CUT_SHA_RETRY_S = (2, 5, 10)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")      # a valid expect_sha (full or >=7-hex abbreviated)


class CutShaMismatch(RuntimeError):
    """The worktree could not be cut at the expected MR head SHA (after refetch retries)."""


def _fetch_branch_sha(repo, remote, ref, *pre, env=None):
    """Fetch branch `ref` with an EXPLICIT refspec (+refs/heads/X:refs/remotes/<remote>/X) and return
    the SHA that tracking ref now holds. Never goes through FETCH_HEAD: it is per-worktree, and these fetches
    run in the SHARED main checkout of the base repo, so a concurrent fetch of another ref there (e.g. the
    target `main`, from another worker or a human) could overwrite it between our fetch and the cut.
    That once cut a worktree at main's sha instead of the MR branch head."""
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    tracking = f"refs/remotes/{remote}/{branch}"
    _git(repo, *pre, "fetch", remote, f"+refs/heads/{branch}:{tracking}", env=env)
    return _git(repo, "rev-parse", "--verify", f"{tracking}^{{commit}}", env=env)

_CHARTER_NAMES = ("CLAUDE.md", "AGENTS.md")
_CHARTER_SUFFIX = "-AGENT.md"          # e.g. CODER-AGENT.md


def _safe(key):
    return re.sub(r"[^A-Za-z0-9._-]", "_", key)


def _ci_skip_opts():
    """With REPO_INFER_ENABLED on, worker branches can land in any catalog repo,
    and a worker can edit .gitlab-ci.yml, so its push carries `-o ci.skip`. Flag off: plain push, CI runs as
    before (the allowlisted repos keep their MR test results)."""
    import routing
    return ["-o", "ci.skip"] if routing.repo_infer_enabled() else []


def _push_cred_args(base_repo):
    """`-c credential.helper` args for the broad push token when REPO_PUSH_BROAD_CRED_ENABLED is on and
    `base_repo` is a catalog repo with no per-repo credential; [] otherwise (flag off: push exactly as before).
    Value-free: the args name the token env var, never its value."""
    import repo_catalog
    return repo_catalog.broad_cred_git_args_for_path(base_repo)


from site_env import push_hosts   # the ONLY hosts a worker branch may ever be pushed to (fail closed)


class PushTargetRefused(RuntimeError):
    """The resolved push URL is not https://<BROKER_PUSH_HOSTS>/..., so fail closed: nothing is pushed."""


def _gitlab_push_url(path, remote):
    """Fail closed: resolve `remote`'s EXPLICIT push URL in the worktree and return it only if it is
    https://<a BROKER_PUSH_HOSTS host>/<...>; otherwise raise PushTargetRefused BEFORE any push. The worker push used to
    go to whatever `origin` was, with ambient creds that include a GitHub token (one push went to a public
    upstream on github.com, which GitHub rejected with a 403). The remote's NAME proves nothing, so callers
    push to the returned URL, never to the remote name. ssh/file/other-host URLs, and URLs with an embedded
    password (it would end up in argv), are refused too."""
    import urllib.parse
    try:
        url = _git(path, "remote", "get-url", "--push", remote, env=_wt_env(path))
    except subprocess.CalledProcessError:
        url = ""
    hosts = push_hosts()
    allowed = ", ".join(sorted(hosts)) or "nowhere (BROKER_PUSH_HOSTS unset)"
    try:
        u = urllib.parse.urlsplit(url)
        # u.password: a token embedded in the URL would land in the push argv -> refuse (creds come from helpers)
        ok = (u.scheme == "https" and u.hostname in hosts and u.port is None and len(u.path) > 1
              and not u.password and all(0x20 < ord(c) < 0x7f for c in url))   # urlsplit drops tab/CR/LF
    except ValueError:
        ok = False
    if ok and _push_url_rewritten(path, url):            # `git push <url>` would rewrite it again
        raise PushTargetRefused(f"refusing to push: remote {remote!r} push URL is rewritten again on push "
                                f"(repo url.*.insteadOf/pushInsteadOf or a remote section names it); worker "
                                f"branches are pushed to {allowed} only")
    if not ok:
        shown = re.sub(r"//[^/@]*@", "//", url)          # never echo URL userinfo (could carry a token)
        raise PushTargetRefused(f"refusing to push: remote {remote!r} push URL {shown!r} is not "
                                f"an allowed https host (worker branches are pushed to {allowed} only)")
    return url


def _push_url_rewritten(path, url):
    """Would `git push <url>` push somewhere OTHER than `url`? `git push <url>` re-applies the repo's
    url.<base>.insteadOf / pushInsteadOf (a plain prefix match on the URL) and honours a `remote "<url>"`
    section, so a CHAINED redirect (insteadOf to a gitlab URL, then pushInsteadOf on that gitlab URL to
    elsewhere) passed get-url's single rewrite + the host check and then pushed elsewhere. True (refuse)
    when any insteadOf/pushInsteadOf value is a prefix of `url` (an EMPTY value is a prefix of every URL, as
    git treats it), when a `remote "<url>"` section exists, or when the config cannot be read (fail closed).
    Read under _wt_env, i.e. exactly the config the push loads."""
    r = subprocess.run(_wt_argv(path, "config", "-z", "--get-regexp", r"^(url\..*\.(push)?insteadof|remote\..*)$"),
                       capture_output=True, text=True, env=_wt_env(path))
    if r.returncode == 1:                                # no url.*/remote.* config at all
        return False
    if r.returncode != 0:
        return True
    for entry in (r.stdout or "").split("\0"):
        key, _, val = entry.partition("\n")
        low = key.lower()
        if low.startswith("url.") and (low.endswith(".insteadof") or low.endswith(".pushinsteadof")):
            if url.startswith(val):                      # empty val matches every URL -> fail closed
                return True
        elif key.startswith(f"remote.{url}.") and "." not in key[len(f"remote.{url}."):]:
            return True
    return False


# A code worker can Edit/Write anything in its cwd, including the worktree's `.git` FILE (the gitdir
# pointer). Repointed at a worker-made dir, the BROKER's own git calls there (publish_mr's credential-bearing
# push, save_wip's `add -A`, save_patch) would load worker-chosen config + hooks and run them as the broker
# user. Layers: (1) every broker git call carries these `-c` overrides (command-line config beats any repo
# config), so no hook (pre-push/pre-commit/post-rewrite...) and no fsmonitor command ever runs; (2) broker
# git calls IN A WORKER WORKTREE also run with _wt_env (no global/system config, ceiling at the worktree's
# parent), --no-ext-diff/--no-textconv on diffs and commit.gpgSign=false on the WIP commit; (3)
# WorktreeManager._checked() refuses to touch a worktree whose `.git` pointer no longer matches the one make()
# recorded (so the rest of a planted gitdir config never loads either). RESIDUAL (repo-local config the worker
# can still plant with exec, e.g. config.worktree filter.*.clean): docs/security-residual-same-uid.md.
_NO_REPO_CODE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
_NO_EXT_DIFF = ("--no-ext-diff", "--no-textconv")        # a planted diff.external / textconv never runs


class WorktreeTampered(RuntimeError):
    """The worktree's `.git` gitdir pointer changed since make() (or was never recorded), so the broker
    runs NO git there."""


def _gitdir_pointer(path):
    """The stripped contents of <path>/.git when it is a regular, non-symlink FILE (a linked worktree's
    `gitdir: ...` pointer); None for a missing/dir/symlink `.git`."""
    p = os.path.join(path, ".git")
    if os.path.islink(p) or not os.path.isfile(p):
        return None
    with open(p) as f:
        return f.read().strip()


def _wt_env(path):
    """Env for a broker git call inside a worker worktree. GIT_CONFIG_GLOBAL=/dev/null +
    GIT_CONFIG_NOSYSTEM=1: a worker-planted ~/.gitconfig (or system config) is not loaded; every setting the
    call needs (safe.directory, credentials, identity) is passed explicitly with -c instead.
    GIT_CEILING_DIRECTORIES=<worktree parent>: repo discovery never walks up into a parent repo."""
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_CEILING_DIRECTORIES=os.path.dirname(os.path.abspath(path)))
    return env


def _wt_cred_args(repo):
    """Credential `-c` args for a credentialed push run under _wt_env: the broad-token helper when
    it applies (unchanged), else _store_cred_args()."""
    return list(_push_cred_args(repo)) or _store_cred_args()


def _store_cred_args():
    """Credential `-c` args for a network git call run under _wt_env (which drops ~/.gitconfig, where
    the credential-store helper used to come from): the path-scoped ~/.git-credentials store (repo_catalog.CRED_FILE) that has_push_cred checks, with
    useHttpPath=true (the fleet store invariant) so only the per-repo entry matches. GET-ONLY: store/erase are
    no-ops, so a rejected push can never erase or rewrite a shared store line (a credential clobber).
    Value-free: names a helper + file path, never a secret."""
    import shlex
    import repo_catalog
    store = f"git credential-store --file={shlex.quote(repo_catalog.CRED_FILE)} get"
    helper = f'!f() {{ test "$1" = get || return 0; {store}; }}; f'
    return ["-c", "credential.helper=", "-c", f"credential.helper={helper}", "-c", "credential.useHttpPath=true"]


def _wt_identity_args():
    """`-c user.name/-c user.email` for the broker's WIP commit under _wt_env. Read from the broker's
    GLOBAL config only (`git config --global`, no repo, so no worker repo config is loaded); fixed fallback."""
    out = {}
    for k, dflt in (("user.name", "eph-broker"), ("user.email", "eph-broker@localhost")):
        r = subprocess.run(["git", "config", "--global", "--get", k], capture_output=True, text=True,
                           cwd=os.path.expanduser("~"))
        out[k] = (getattr(r, "stdout", "") or "").strip() or dflt
    return ["-c", f"user.name={out['user.name']}", "-c", f"user.email={out['user.email']}"]


def _wt_argv(path, *args):
    """Argv for a direct subprocess git call in a worker worktree (run it with env=_wt_env(path)):
    scoped safe.directory (the global list is not loaded under _wt_env) + _NO_REPO_CODE."""
    return ["git", "-c", f"safe.directory={path}", *_NO_REPO_CODE, "-C", path, *args]


def _git(repo, *args, check=True, env=None):
    """Run `git -C repo <args>`, return stripped stdout. Value-free plumbing (no secrets in argv).

    `-c safe.directory=<repo>`, SCOPED to the exact path this call operates on, never a wildcard.

    Why it's needed: a worktree cut from a repo whose .git is owned by a DIFFERENT uid trips git's
    dubious-ownership guard. The infra repo is the case that matters: its .git is root-owned while workers
    run as the invoking user, so `git -C <worktree>` (which resolves through a .git FILE pointing at
    <repo>/.git/worktrees/...) refuses: 'detected dubious ownership' with exit 128. That killed every
    infra-targeted worker at the post-cut config step. The global safe.directory list
    covers the repo itself but not the per-task worktree path under ~/.cache, and pre-seeding a mutable
    global list per task is worse than scoping the exception to the one path in hand. This grants the
    exception to a path the broker itself just created or resolved, no broader than the call it wraps.

    Also carries _NO_REPO_CODE: the broker never runs repo hooks / an fsmonitor command. Callers in a
    worker worktree pass env=_wt_env(path)."""
    r = subprocess.run(["git", "-c", f"safe.directory={repo}", *_NO_REPO_CODE, "-C", repo, *args],
                       check=check, capture_output=True, text=True, env=env)
    return r.stdout.strip()


def strip_charter(path):
    """Remove any charter files from the worktree root so cwd is charter-free."""
    removed = []
    try:
        for name in os.listdir(path):
            if name in _CHARTER_NAMES or name.endswith(_CHARTER_SUFFIX):
                fp = os.path.join(path, name)
                try:
                    os.remove(fp)
                    removed.append(name)
                except OSError as e:
                    log.warning("could not strip charter %s: %s", fp, e)
    except OSError as e:
        log.warning("charter strip listdir failed for %s: %s", path, e)
    if removed:
        log.info("stripped charter files from %s: %s", path, removed)
    return removed


class WorktreeManager:
    def __init__(self, base_repo, worktrees_root):
        self.base_repo = base_repo
        self.worktrees_root = worktrees_root
        # key -> (repo, path, cut_sha): the repo the worktree forks off, its path, and the sha it was cut
        # at. Tracked so remove()/publish() are correct even for a wiki route whose repo/root
        # differ from the defaults. cut_sha lets publish() tell "worker committed" from "no-op".
        self._made = {}
        # key -> the worktree's `.git` gitdir pointer as make() created it (see _checked()).
        self._gitdirs = {}

    def path_for(self, key, worktrees_root=None):
        return os.path.join(worktrees_root or self.worktrees_root, _safe(key))

    def make(self, key, *, base_repo=None, worktrees_root=None, publish_ref=None, suppress_hooks=False,
             expect_sha=None, sleep=None):
        """Create a worktree for `key` (see _make_once), return the path.

        expect_sha (a full SHA or a >=7-hex prefix, e.g. the MR head): the cut SHA MUST match it. On a
        mismatch the worktree is removed, the branch refetched and the cut retried (CUT_SHA_RETRY_S); if it is still
        mismatched, CutShaMismatch is raised (worktree already removed), so a caller never works on the wrong tree.

        An expect_sha that is not 7-40 hex chars can never be verified, so CutShaMismatch is raised at once (fail
        closed, nothing cut). A git error during a cut attempt (e.g. the fetch) is retried like a mismatch, and if
        it keeps failing, CutShaMismatch is raised, so the caller takes its neutral skip path, not a generic failure."""
        want = (expect_sha or "").strip().lower()
        if not want:
            return self._make_once(key, base_repo=base_repo, worktrees_root=worktrees_root,
                                   publish_ref=publish_ref, suppress_hooks=suppress_hooks)
        if not _SHA_RE.match(want):
            raise CutShaMismatch(f"worktree {key}: expected head {want!r} is not a 7-40 hex sha")
        sleep = sleep or time.sleep
        got = None
        for attempt, delay in enumerate((0, *CUT_SHA_RETRY_S), 1):
            if delay:
                sleep(delay)
            try:
                path = self._make_once(key, base_repo=base_repo, worktrees_root=worktrees_root,
                                       publish_ref=publish_ref, suppress_hooks=suppress_hooks)
            except subprocess.CalledProcessError as e:
                got = f"<git error: {(e.stderr or '').strip() or e}>"
                log.warning("worktree make key=%s: cut failed (attempt %d/%d): %s -> retry",
                            key, attempt, len(CUT_SHA_RETRY_S) + 1, got)
                self.remove(key)
                continue
            got = self.cut_sha(key) or ""
            if got.lower().startswith(want):
                return path
            log.warning("worktree make key=%s: cut sha %s != expected head %s (attempt %d/%d) -> refetch",
                        key, got, want, attempt, len(CUT_SHA_RETRY_S) + 1)
            self.remove(key)
        raise CutShaMismatch(f"worktree {key}: cut at {got}, expected head {want} after "
                             f"{len(CUT_SHA_RETRY_S) + 1} attempts")

    def _make_once(self, key, *, base_repo=None, worktrees_root=None, publish_ref=None, suppress_hooks=False):
        """Create a worktree for `key`, strip its charter, return the path.

        Default (read domain): a detached worktree off self.base_repo @ current HEAD, in
        self.worktrees_root.

        Route-aware for the wiki write worker: base_repo=wiki_repo_path,
        worktrees_root=worktree_base, publish_ref set -> the worktree is cut off the FRESH remote tip
        (explicit-refspec fetch of <publish_ref>, then detach at the SHA it stored), so every write starts from the
        true upstream and self-serialised cuts never need a rebase. suppress_hooks=True sets
        core.hooksPath=/dev/null in the worktree so the standing wiki post-commit auto-push CANNOT fire
        from inside the worker (the broker does the ONLY push, in publish())."""
        repo = base_repo or self.base_repo
        if not repo:   # `git -C ""` would act on the broker's own cwd; an unset repo is a refusal
            raise ValueError(f"worktree for {key}: no base repo configured (ANSIBLE_REPO / row repo unset)")
        root = worktrees_root or self.worktrees_root
        os.makedirs(root, exist_ok=True)
        path = self.path_for(key, root)
        # Make setup IDEMPOTENT. A
        # KILLED prior run can leave a stale worktree DIR and/or a stale `.git/worktrees` REGISTRATION for
        # this path; the next `git worktree add` then dies exit-128 ("already exists" / "missing but
        # registered"). Prune dead registrations on the base repo, then force-remove THIS specific worktree
        # (registered -> `git worktree remove --force`), then rm any leftover dir. SCOPED to the one per-key
        # path in hand, never another worktree, never the base checkout. check=False: nothing-to-prune /
        # not-registered are normal (idempotent no-ops). Uses `repo` (the resolved base), so it is correct for
        # a code/wiki route whose repo differs from self.base_repo.
        subprocess.run(["git", "-c", f"safe.directory={repo}", "-C", repo, "worktree", "prune"],
                       check=False, capture_output=True, text=True)
        subprocess.run(["git", "-c", f"safe.directory={repo}", "-C", repo,
                        "worktree", "remove", "--force", path],
                       check=False, capture_output=True, text=True)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        self._made.pop(key, None)                # drop any stale in-process tracking for this key
        self._gitdirs.pop(key, None)
        if publish_ref:
            # freshen the upstream tip before the cut; cut from the SHA THIS fetch stored (explicit
            # refspec), never the shared, raceable FETCH_HEAD
            tip = _fetch_branch_sha(repo, "origin", publish_ref)
            _git(repo, "worktree", "add", "--detach", path, tip)
            _cut_desc = f"origin/{publish_ref}"
        else:
            # This used to cut straight off `repo`'s current checked-out HEAD with no fetch, so a
            # base repo nobody had pulled in a while handed the read-worker a STALE tree. Freshen
            # from origin/<the repo's current branch> first, mirroring the publish_ref lane above. A fetch
            # failure (offline, no `origin` remote, detached HEAD) is NOT fatal: fall back to the old
            # current-HEAD behaviour with a log.warning rather than hard-failing the spawn.
            base_ref = None
            try:
                branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
                if branch and branch != "HEAD":            # HEAD == detached; nothing sane to fetch by name
                    # cut from the SHA this fetch stored (explicit refspec), never the shared FETCH_HEAD
                    base_ref = _fetch_branch_sha(repo, "origin", branch)
                    _cut_desc = f"origin/{branch}"
            except subprocess.CalledProcessError as e:
                log.warning("worktree make key=%s: git fetch origin failed (%s); falling back to local HEAD",
                            key, (e.stderr or "").strip() or e)
            if base_ref:
                _git(repo, "worktree", "add", "--detach", path, base_ref)
            else:
                _git(repo, "worktree", "add", "--detach", path)   # fallback: current HEAD (fetch skipped/failed)
                _cut_desc = "local HEAD"
        # Mark the fresh worktree safe for the WORKER's own git calls. `_git` above passes
        # -c safe.directory per call, but the worker runs plain `git` with no such flag, so against a
        # repo whose .git is owned by another uid (/srv/infra: root-owned, workers run as the invoking user)
        # EVERY in-worktree git command died with 'detected dubious ownership': the worker could open the
        # worktree, then could not run git status/add/commit in it at all, and burned its whole wall-clock
        # producing nothing (the broker's cut succeeded and the worker still made no
        # commit). Registering the path once, here, is what makes the worktree usable end to end.
        # SCOPED to this exact path, never a wildcard, never another repo.
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", path],
                       check=False, capture_output=True, text=True)
        if suppress_hooks:
            # WORKTREE-LOCAL only: `git config core.hooksPath` without --worktree writes the SHARED repo
            # config, which would disable hooks for the base repo AND every standing bridge worktree (the
            # real wiki post-commit auto-push). extensions.worktreeConfig (idempotent, on the shared repo)
            # scopes --worktree writes to THIS ephemeral worktree, leaving the bridges' hooks intact.
            _git(repo, "config", "extensions.worktreeConfig", "true")
            _git(path, "config", "--worktree", "core.hooksPath", "/dev/null")
        strip_charter(path)                # charter-free cwd
        cut_sha = _git(path, "rev-parse", "HEAD")
        self._gitdirs[key] = _gitdir_pointer(path)   # recorded BEFORE the worker ever runs in `path`
        self._made[key] = (repo, path, cut_sha)
        log.info("worktree cut key=%s base=%s sha=%s path=%s", key, _cut_desc, cut_sha, path)
        return path

    def remove(self, key):
        repo, path, _ = self._made.pop(key, (self.base_repo, self.path_for(key), None))
        self._gitdirs.pop(key, None)
        # `--force` because we stripped tracked charter files (dirty worktree).
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", path],
                       capture_output=True, text=True)
        if os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)
        # make() registered `git config --global --add safe.directory <path>` for THIS
        # worktree (so the worker's plain `git` didn't trip dubious-ownership). Worktree paths are ephemeral
        # (one per subject, removed here), so without an unset these entries accumulate in the global gitconfig
        # forever. Unset EXACTLY the entry this worktree added: `--fixed-value` matches the value literally
        # (paths carry regex-special chars) and, unlike a bare `--unset`, handles the multi-valued key without
        # erroring. Best-effort + idempotent: check=False so a not-present entry / an ancient git lacking
        # --fixed-value is a harmless no-op that never raises into the remove path. SCOPED to this one path,
        # never a wildcard, never --unset-all of unrelated entries.
        subprocess.run(["git", "config", "--global", "--unset", "--fixed-value", "safe.directory", path],
                       check=False, capture_output=True, text=True)

    def _checked(self, key):
        """(repo, path, cut_sha) for `key`, but only after verifying the worktree's `.git` pointer is
        still exactly what make() recorded. Raises WorktreeTampered (fail closed, no git run) if make() never
        recorded a pointer for `key` (unknown key / no regular `.git` file at the cut), or if the pointer
        changed, was removed, or became a dir/symlink."""
        repo, path, cut_sha = self._made.get(key, (self.base_repo, self.path_for(key), None))
        want = self._gitdirs.get(key)
        if want is None:
            raise WorktreeTampered(f"refusing git in {path}: no recorded gitdir pointer for key={key}")
        if _gitdir_pointer(path) != want:
            raise WorktreeTampered(f"refusing git in {path}: .git gitdir pointer changed since the cut "
                                   f"(key={key})")
        return repo, path, cut_sha

    def publish(self, key, *, publish_ref, remote="origin", retries=3):
        """Push the worker's committed worktree HEAD to <remote>/<publish_ref>.

        The worker is cred-free and its worktree has hooks suppressed, so THIS is the only push. If the
        worker made no commit (HEAD == the cut sha) it's a clean no-op and returns False. On a non-ff
        reject (a concurrent standing wiki-fs/wiki-git bridge push landed first; those don't take our
        per-repo lock) we fetch + rebase the single worker commit onto the new tip and retry, so the
        publish stays fast-forward-only. Raises on a genuine rebase conflict or exhausted retries, and
        PushTargetRefused (nothing pushed) unless `remote`'s push URL is https://<BROKER_PUSH_HOSTS>/."""
        repo, path, cut_sha = self._checked(key)        # fail closed on a repointed .git
        env = _wt_env(path)                              # no global/system config in the worker worktree
        if _git(path, "rev-parse", "HEAD", env=env) == cut_sha:
            log.info("publish key=%s: worker made no commit -> no-op", key)
            return False
        # Same fail-closed guard as publish_mr/save_wip: resolve once, BEFORE any push attempt, and
        # push the resolved allowlisted https URL, never the bare remote name.
        push_url = _gitlab_push_url(path, remote)
        cred = _store_cred_args()                        # ~/.gitconfig's store helper, made explicit
        for attempt in range(1, retries + 1):
            r = subprocess.run(_wt_argv(path, *cred, "push", push_url, f"HEAD:{publish_ref}"),
                               capture_output=True, text=True, env=env)
            if r.returncode == 0:
                log.info("publish key=%s: pushed HEAD -> %s/%s (attempt %d)", key, remote, publish_ref, attempt)
                return True
            log.warning("publish key=%s: push rejected (attempt %d): %s", key, attempt, r.stderr.strip())
            _git(path, *cred, "fetch", remote, publish_ref, env=env)
            rb = subprocess.run(_wt_argv(path, *_wt_identity_args(), "-c", "commit.gpgSign=false",
                                         "rebase", f"{remote}/{publish_ref}"),
                                capture_output=True, text=True, env=env)
            if rb.returncode != 0:
                subprocess.run(_wt_argv(path, "rebase", "--abort"), capture_output=True, text=True, env=env)
                raise RuntimeError(f"wiki publish rebase conflict key={key}: {rb.stderr.strip()}")
        raise RuntimeError(f"wiki publish key={key}: push still rejected after {retries} rebase-retries")

    def publish_mr(self, key, *, branch_prefix="eph/code-change", mr_target_ref="main", remote="origin"):
        """Publish a code change-proposer's committed HEAD as a FEATURE BRANCH for an
        MR, NEVER a push to `mr_target_ref` (main). This is the human-gated code contract: the worker's diff
        lands on a fresh eph branch on the remote; opening the actual MR (GitLab API, token side) is
        a separate step. Returns the pushed branch name, or None on a no-op (worker made no commit).

        SECURITY INVARIANT: the ONLY ref this method ever pushes to is a fresh
        `<branch_prefix>/...` feature branch. It must NEVER push to mr_target_ref, since that would auto-deploy
        unreviewed code. The assert below makes that invariant executable."""
        repo, path, cut_sha = self._checked(key)        # fail closed on a repointed .git
        env = _wt_env(path)                              # no global/system config in the worker worktree
        if _git(path, "rev-parse", "HEAD", env=env) == cut_sha:
            log.info("publish_mr key=%s: worker made no commit -> no-op", key)
            return None
        head = _git(path, "rev-parse", "HEAD", env=env)
        branch = f"{branch_prefix}/{_safe(key)}-{head[:8]}"
        assert branch != mr_target_ref and not mr_target_ref.endswith(f"/{branch}"), \
            f"refusing to publish onto the MR target ref {mr_target_ref!r} (code MRs push a feature branch only)"
        push_url = _gitlab_push_url(path, remote)        # fail closed unless https://<BROKER_PUSH_HOSTS>/
        r = subprocess.run(_wt_argv(path, *_wt_cred_args(repo), "push", *_ci_skip_opts(), push_url,
                                    f"HEAD:refs/heads/{branch}"), capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"code publish_mr key={key}: feature-branch push failed: {r.stderr.strip()}")
        # Branch is up on the remote = the reviewable deliverable. Opening the MR (branch -> mr_target_ref) is
        # a separate step (it needs the repo's API token). Surfaced here so an operator can open it (or wire the API).
        log.info("publish_mr key=%s: pushed feature branch %s/%s (target %s) -> MR-open is lane-b",
                 key, remote, branch, mr_target_ref)
        return branch

    def save_wip(self, key, remote="origin"):
        """A turn-capped code worker's work-in-progress must survive teardown. Commit any
        uncommitted edits as a WIP commit, then push HEAD (incl. any unpushed worker commits) to a fresh
        `eph/<key>-wip-<sha8>` branch. Branch ONLY; the caller opens NO MR (incomplete work is never put up
        for review/merge). The stripped charter files (strip_charter) are excluded so the WIP commit never
        records their deletion. Returns the pushed branch, or None when there is nothing to save (clean tree
        AND HEAD == the cut sha). Raises on a git/push failure; the caller logs it and continues teardown."""
        repo, path, cut_sha = self._checked(key)        # fail closed on a repointed .git
        env = _wt_env(path)                              # no global/system config in the worker worktree
        _git(path, "add", "-A", "--", ".", ":(top,exclude)CLAUDE.md", ":(top,exclude)AGENTS.md",
             f":(top,exclude,glob)*{_CHARTER_SUFFIX}", env=env)
        staged = subprocess.run(_wt_argv(path, "diff", "--cached", "--quiet", *_NO_EXT_DIFF),
                                capture_output=True, text=True, env=env)
        if staged.returncode == 1:                       # 1 = staged changes present (0 = none)
            # Identity passed explicitly (_wt_env drops ~/.gitconfig); never run a planted gpg.program
            _git(path, *_wt_identity_args(), "-c", "commit.gpgSign=false",
                 "commit", "-q", "--no-verify", "-m", f"WIP: turn-cap save ({key})", env=env)
        head = _git(path, "rev-parse", "HEAD", env=env)
        if head == cut_sha:
            log.info("save_wip key=%s: nothing to save (clean, no commits)", key)
            return None
        branch = f"eph/{_safe(key)}-wip-{head[:8]}"
        push_url = _gitlab_push_url(path, remote)        # fail closed unless https://<BROKER_PUSH_HOSTS>/
        r = subprocess.run(_wt_argv(path, *_wt_cred_args(repo), "push", *_ci_skip_opts(), push_url,
                                    f"HEAD:refs/heads/{branch}"), capture_output=True, text=True, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"save_wip key={key}: WIP branch push failed: {r.stderr.strip()}")
        log.info("save_wip key=%s: pushed WIP branch %s/%s", key, remote, branch)
        return branch

    def save_patch(self, key):
        """A worker that edited files but made NO commit (e.g. its commit was refused) would lose that
        work when the worktree is removed. Write its uncommitted changes (incl. new files; not the charter
        strip) as a `git diff` patch to <worktrees_root>/_patches/<key>-<cut8>.patch, OUTSIDE the worktree so
        it survives removal. Returns the patch path, or None when there is nothing uncommitted. Local only:
        no push, no commit. Raises on a git/IO failure (the caller logs it)."""
        repo, path, cut_sha = self._checked(key)        # fail closed on a repointed .git
        env = _wt_env(path)                              # no global/system config in the worker worktree
        _git(path, "add", "-A", "--", ".", ":(top,exclude)CLAUDE.md", ":(top,exclude)AGENTS.md",
             f":(top,exclude,glob)*{_CHARTER_SUFFIX}", env=env)
        diff = subprocess.run(_wt_argv(path, "diff", "--cached", "--binary", *_NO_EXT_DIFF),
                              check=True, capture_output=True, env=env).stdout   # bytes (non-UTF-8 files)
        if not diff.strip():
            return None
        out_dir = os.path.join(os.path.dirname(path), "_patches")
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, f"{_safe(key)}-{(cut_sha or 'nohead')[:8]}.patch")
        with open(out, "wb") as f:
            f.write(diff)
        log.info("save_patch key=%s: uncommitted worker edits saved to %s", key, out)
        return out

    def cut_sha(self, key):
        """The HEAD SHA `key`'s worktree was cut at (None if unknown); CD pins its merge to it."""
        return (self._made.get(key) or (None, None, None))[2]

    def head_sha(self, key):
        """The worktree's current HEAD SHA (what publish_mr pushed), None on any error. Never raises."""
        try:
            _, path, _ = self._checked(key)
            return _git(path, "rev-parse", "HEAD", env=_wt_env(path)) or None
        except Exception as e:
            log.warning("head_sha key=%s failed: %s", key, e)
            return None

    def mr_diff(self, key, target_ref, remote="origin", backoff_s=None, sleep=None):
        """MR reviewer: broker-side MR diff. Fetch the target ref into the review worktree, return
        the THREE-DOT diff (changes on the MR branch since it diverged from target). The review worktree is
        already checked out at the MR SOURCE branch (make(publish_ref=source_branch)); this adds the target
        as FETCH_HEAD and diffs FETCH_HEAD...HEAD. Read-only plumbing; NEVER raises into the turn (returns
        '' on failure so the review still runs on the files the worker can Read).

        An empty result right after MR open once made the reviewer BLOCK a full change ("not implemented").
        So an empty/failed attempt (a) falls back to the BASE repo's fetch of the target (the same credentials
        make() just cut the source branch with) and a local merge-base diff vs the worktree HEAD, and (b) is
        retried with backoff (MR_DIFF_BACKOFF_S, ~5 min total). '' only when every attempt is
        empty; the caller then SKIPS the review rather than reviewing the base tree.

        If the worktree HEAD is already contained in the target (cut SHA == merge-base, e.g. a cut at the
        target's sha instead of the MR head) no amount of waiting yields a diff, so it returns '' at once, no backoff."""
        try:
            repo, path, _ = self._checked(key)          # a tampered worktree is never retried
        except Exception as e:
            log.warning("mr_diff key=%s failed: %s", key, e)
            return ""
        sleep = sleep or time.sleep
        delays = MR_DIFF_BACKOFF_S if backoff_s is None else backoff_s
        for attempt, delay in enumerate((0, *delays), 1):
            if delay:
                sleep(delay)
            diff = self._mr_diff_once(key, repo, path, target_ref, remote)
            if diff.strip():
                if attempt > 1:
                    log.info("mr_diff key=%s: non-empty diff on attempt %d", key, attempt)
                return diff
            log.warning("mr_diff key=%s: empty diff on attempt %d/%d", key, attempt, len(delays) + 1)
            if attempt == 1 and self._head_is_merge_base(key, repo, path, target_ref, remote):
                log.warning("mr_diff key=%s: worktree HEAD is the merge-base with %s (cut at the target, not "
                            "the MR head) -> diff unavailable, no retries", key, target_ref)
                return ""
        return ""

    def _head_is_merge_base(self, key, repo, path, target_ref, remote):
        """True iff the worktree HEAD is an ancestor of (or equal to) the target tip, i.e. HEAD ==
        merge-base(HEAD, target): the three-dot diff is empty by construction. Never raises; False on error."""
        try:
            base = _fetch_branch_sha(repo, remote, target_ref)
            r = subprocess.run(_wt_argv(path, "merge-base", "--is-ancestor", "HEAD", base),
                               capture_output=True, text=True, env=_wt_env(path))
            return r.returncode == 0
        except Exception as e:
            log.warning("mr_diff key=%s merge-base check failed: %s", key, e)
            return False

    def _mr_diff_once(self, key, repo, path, target_ref, remote):
        """One mr_diff attempt: the worktree fetch + three-dot diff, then (if that failed or came back
        empty) the base-repo fetch + `<target sha>...HEAD` diff. Never raises; '' when both are empty/fail."""
        env = _wt_env(path)
        try:
            _git(path, *_store_cred_args(), "fetch", remote, target_ref, env=env)
            diff = _git(path, "diff", *_NO_EXT_DIFF, "FETCH_HEAD...HEAD", env=env)
            if diff.strip():
                return diff
        except Exception as e:
            log.warning("mr_diff key=%s worktree fetch/diff failed: %s", key, e)
        try:                                             # local fallback: base repo shares the object db
            base = _fetch_branch_sha(repo, remote, target_ref)   # not the shared FETCH_HEAD
            return _git(path, "diff", *_NO_EXT_DIFF, f"{base}...HEAD", env=env)
        except Exception as e:
            log.warning("mr_diff key=%s base-repo fallback failed: %s", key, e)
            return ""
