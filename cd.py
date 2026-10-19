#!/usr/bin/env python3
"""cd.py: CD, broker half: reviewer-PASS auto-merge and post-publish deploy hand-off.

DEFAULT OFF. Every entry point returns immediately unless env CD_ENABLED is truthy ("1"/"true"/"yes"/"on",
read at CALL time), so an unset flag is byte-identical to a broker without this module.

1. AUTO-MERGE (on_review_verdict, called by the CC adapter after the mr-reviewer verdict is posted):
   reviewer PASS on a worker-authored MR (source branch under CD_WORKER_BRANCH_PREFIX, default "eph/") in a
   repo on CD_REPO_ALLOWLIST (opt-in; empty = no repo): GitLab's own view of the MR is re-verified (head ==
   the SHA the reviewer saw, same project, default target, opened, not draft, bot-authored), then tier =
   max(reviewer-stated tier, path classifier). Any doubt (empty/truncated diff, lookup failure) = Full.
   Full: NO merge; DM the operator and add a ticket comment.
   Skip/Light with the MR pipeline green: merge via the GitLab API PINNED to the reviewed SHA (pipeline still
   running: set merge_when_pipeline_succeeds). Pipeline failed/absent, or ANY API failure: DM the operator and
   add a ticket comment with the error. Never silent.
2. HAND-OFF (handle_pipeline, called from /webhooks/gitlab on a GitLab Pipeline Hook): a SUCCESSFUL pipeline
   on the project's default branch for a repo in config/deploy-paths.json (status/ref/sha re-read from the
   GitLab API, the payload is not trusted):
     kind ansible-*                 -> zbus DM to the CD_ANSIBLE_HANDLE bot, EXACTLY
                                       "DEPLOY-HANDOFF repo=<ns/name> sha=<sha> ver=<ver> ticket=<id>"
                                       and a ticket comment;
     kind nightly / registry-image* -> ticket comment only, with the expected live time;
     kind none / unlisted repo      -> nothing.
   The broker NEVER closes the ticket (the deploy side closes it on live-verify). POLICY: this hands off EVERY
   green default-branch pipeline of a listed repo, hand-pushed and operator-merged Full ones included. Merging
   to the default branch IS the deploy sign-off once CD_ENABLED (it replaces a separate manual sign-off step).

Secrets: the merge token is read at runtime from the env var NAMED by GITLAB_MERGE_TOKEN_ENV (default
GITLAB_MERGE_TOKEN), falling back to the MR-open token (gitlab_mr.GITLAB_MR_TOKEN_ENV). Its value is never
logged, echoed into a comment, or put in a DM.
"""
import base64, fnmatch, html, logging, os, re, threading, time, urllib.error, urllib.parse, uuid

import gitlab_mr
from site_env import ticket_url, ticket_id_regex, gitlab_web_url   # site overlay, neutral defaults

log = logging.getLogger("eph-broker.cd")

GITLAB_MERGE_TOKEN_ENV = os.getenv("GITLAB_MERGE_TOKEN_ENV", "GITLAB_MERGE_TOKEN")   # NAME only
ANSIBLE_HANDLE = os.getenv("CD_ANSIBLE_HANDLE", "")   # site.env; "" = hand-off DM fails, operator told
WORKER_BRANCH_PREFIX = os.getenv("CD_WORKER_BRANCH_PREFIX", "eph/")
# CD_REPO_ALLOWLIST ("ns/name" list, comma/colon/space separated, case-insensitive) and CD_MR_AUTHOR (GitLab
# username that opens worker MRs; REQUIRED; if unset, nothing auto-merges and the operator is DMed once) are
# read at CALL time.

TIERS = ("skip", "light", "full")
_PIPE_PENDING = {"created", "waiting_for_resource", "preparing", "pending", "running", "scheduled"}

# Path classifier: ANY changed path (old or new) matching a pattern => Full. fnmatch, case-insensitive,
# tried against the full path AND the basename. config/cd-tier.json can only ADD globs ("full_patterns" /
# "extra_patterns" / "repo_patterns" are all unioned onto these built-ins), never remove one, so a worker MR
# that edits the config cannot lower its own tier (and config/** is itself Full).
DEFAULT_FULL_PATTERNS = {
    "dockerfile": ["Dockerfile*", "*.dockerfile", "Containerfile*", ".dockerignore"],
    "ci": [".gitlab-ci.yml", ".gitlab-ci/*", ".gitlab/*", ".github/workflows/*", "Jenkinsfile"],
    "compose": ["docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml"],
    "deploy": ["*.service", "*.timer", "*.socket", "deploy/*", "*/deploy/*", "packaging/*", "*.spec",
               "ansible/*", "*/ansible/*", "playbooks/*", "systemd/*", "*/systemd/*"],
    "dependencies": ["requirements*.txt", "requirements/*", "constraints*.txt", "pyproject.toml",
                     "setup.py", "setup.cfg", "Pipfile", "Pipfile.lock", "poetry.lock", "uv.lock",
                     "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "go.mod",
                     "go.sum", "Cargo.toml", "Cargo.lock", "Gemfile", "Gemfile.lock"],
    "migrations": ["*migrations/*", "*migration*", "alembic/*", "*.sql"],
    "auth": ["*auth*", "*gate*", "hooks/*", "*/hooks/*", "*hook*", "*permission*", "*allowlist*",
             "*acl*", "*policy*", "*approved-prompts*", "*guard*"],
    "secret": ["*.env", ".env*", "*-env", "*secret*", "*credential*", "*token*", "*.pem", "*.key",
               "*vault*", "*password*"],
    "cd-config": ["config/*", "cd-tier.json", "deploy-paths.json"],
}

# Per-repo Full categories (fleet policy: shared engine/gate/launcher code = Full). config/cd-tier.json
# {"repo_patterns": {...}} is unioned onto this map (add-only).
# Keyed `*/eph-broker` (any namespace; keys are fnmatch globs, see _for_repo) so no site namespace is baked in.
DEFAULT_REPO_PATTERNS = {
    "*/eph-broker": {
        "engine": ["adapters/*", "app.py", "routing.py", "coalescer.py", "echo_guard.py", "envelope.py",
                   "provenance.py", "cd.py", "worktree.py", "gitlab_mr.py", "forge.py", "stream.py",
                   "poster.py", "notify.py", "hooks/*", "prompts/*", "prompts.py", "approved-prompts.json",
                   "packaging/*", "workers/*"],
    },
}

# Engine repos: an UNLISTED path is Full (a new module is engine code until proven otherwise); only paths
# matching these globs (full path, case-insensitive) can be Light. config/cd-tier.json {"default_full_repos":
# {"<ns/name>": [light globs]}} may ADD repos, but can't change the Light list of a repo listed here.
DEFAULT_FULL_REPOS = {
    "*/eph-broker": ["docs/*", "tests/*", "README*"],
}

_lock = threading.Lock()
_BY_MR = {}        # (project, mr_iid) -> task_id  (MRs the broker merged / armed)
_BY_SHA = {}       # merge_commit_sha -> task_id
_DM_SENT = set()     # (project, sha) whose ansible hand-off DM went out (a retry re-posts only the comment)
_HANDED_OFF = set()  # (project, sha) whose hand-off DM + comment SUCCEEDED (a redelivery is then a no-op)
_IN_FLIGHT = set()   # (project, sha) being handed off right now (guards concurrent redeliveries)
_NO_AUTHOR_WARNED = []  # non-empty once the operator was DMed that CD_MR_AUTHOR is unset (DM once per process)


def enabled():
    return (os.getenv("CD_ENABLED") or "0").strip().lower() in ("1", "true", "yes", "on")


def hermes_accepted():
    """Opt-in (default OFF): a hermes-harness reviewer PASS may auto-merge like a cc one. The tier is
    still decided from changed paths (a reviewer can only RAISE it), so this never widens what merges."""
    return (os.getenv("CD_ACCEPT_HERMES") or "0").strip().lower() in ("1", "true", "yes", "on")


def repo_allowed(project):
    allow = {r.lower() for r in re.split(r"[,:\s]+", os.getenv("CD_REPO_ALLOWLIST") or "") if r}
    return str(project or "").lower() in allow


def _token():
    return os.getenv(GITLAB_MERGE_TOKEN_ENV) or os.getenv(gitlab_mr.GITLAB_MR_TOKEN_ENV)


def _pid(project):
    return urllib.parse.quote(str(project), safe="")


def _err(e):
    """Short, token-free description of an API failure (HTTP code + first 200 chars of the body)."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = e.read().decode(errors="replace")[:200]
        except Exception:
            detail = ""
        return f"HTTP {e.code} {detail}".strip()
    return f"{type(e).__name__}: {str(e)[:200]}"


# ---------------- tier ----------------
def _for_repo(mapping, repo):
    """mapping[repo], matched case-insensitively; a key may be an fnmatch glob (`*/eph-broker`). Exact key first,
    then the first glob hit. None if absent. Globs only ever ADD Full coverage (these maps never lower a tier)."""
    repo = str(repo or "").lower()
    items = list((mapping or {}).items())
    return next((v for k, v in items if str(k).lower() == repo),
                next((v for k, v in items if repo and fnmatch.fnmatchcase(repo, str(k).lower())), None))


def _patterns(tier_cfg, repo=None):
    """Built-in Full globs UNION the config's (add-only: config never removes or replaces a built-in)."""
    cfg = tier_cfg or {}
    pats = {k: list(v) for k, v in DEFAULT_FULL_PATTERNS.items()}
    for src in (cfg.get("full_patterns"), cfg.get("extra_patterns"), _for_repo(DEFAULT_REPO_PATTERNS, repo),
                _for_repo(cfg.get("repo_patterns"), repo)):
        for k, v in (src or {}).items():
            pats.setdefault(k, []).extend(v)
    return pats


def _light_only(tier_cfg, repo):
    """The Light-glob list if `repo` is default-Full (built-in entry wins over config), else None."""
    light = _for_repo(DEFAULT_FULL_REPOS, repo)
    if light is None:
        light = _for_repo((tier_cfg or {}).get("default_full_repos"), repo)
    return None if light is None else list(light)


def classify_paths(paths, tier_cfg=None, repo=None):
    """Pure: returns ("full", [(category, path), ...]) if any path hits a Full pattern (global categories and the
    per-repo ones for `repo`), or is unlisted in a default-Full repo, else ("light", []). No paths at all
    gives Full (fail closed: an empty diff is an unknown diff)."""
    hits = []
    pats_by_cat = _patterns(tier_cfg, repo)
    light_only = _light_only(tier_cfg, repo)
    if not any(paths):
        return "full", [("empty-diff", "(no changed paths)")]
    for path in paths:
        if not path:
            continue
        low = path.lower()
        base = low.rsplit("/", 1)[-1]
        for cat, pats in pats_by_cat.items():
            if any(fnmatch.fnmatchcase(low, p.lower()) or fnmatch.fnmatchcase(base, p.lower()) for p in pats):
                hits.append((cat, path))
                break
        else:
            if light_only is not None and not any(fnmatch.fnmatchcase(low, p.lower()) for p in light_only):
                hits.append(("unlisted", path))
    return ("full", hits) if hits else ("light", [])


_TIER_RE = re.compile(r"\btier\b(?:</?\w+>|[*\s])*[:=]?(?:</?\w+>|[*\s])*(skip|light|full)\b", re.I)


def reviewer_tier(env):
    """The reviewer-stated tier, if any: env["tier"], else a 'tier: X' mention in the summary. None if absent."""
    if not isinstance(env, dict):
        return None
    t = str(env.get("tier") or "").strip().lower()
    if t in TIERS:
        return t
    m = _TIER_RE.search(str(env.get("summary") or ""))
    return m.group(1).lower() if m else None


def max_tier(*tiers):
    return max((t for t in tiers if t in TIERS), key=TIERS.index, default="light")


def _mr_paths(api, token, project, iid):
    """All changed paths of the MR. Raises (classify-failed) on a non-list page, an empty diff (GitLab
    computes MR diffs async), or a diff past the 20x100-file page cap (truncated = unclassifiable)."""
    paths = []
    for page in range(1, 21):
        batch = api("GET", f"/projects/{_pid(project)}/merge_requests/{iid}/diffs?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise ValueError(f"unexpected diffs response on page {page}")
        for d in batch:
            paths += [d.get("old_path"), d.get("new_path")]
        if len(batch) < 100:
            paths = sorted({p for p in paths if p})
            if not paths:
                raise ValueError("empty diff (GitLab may still be computing it)")
            return paths
    raise ValueError("diff exceeds 2000 files (truncated) - cannot classify")


def _sha_pipelines(api, token, project, iid, sha):
    """The pipelines of the MR at `sha` that decide the merge: only the NEWEST (highest id) per
    `source` (push / merge_request_event / ...), so a failed pipeline later retried green at the same sha no
    longer blocks forever. Reads every page (per_page=100, capped at 20 pages = 2000 pipelines; past that
    raises, so it fails closed). A pipeline without an integer id cannot be ordered, so it is always kept; one
    without a `source` is its own group, so a sourceless newer green never masks an older failure."""
    rows = []
    for page in range(1, 21):
        batch = api("GET", f"/projects/{_pid(project)}/merge_requests/{iid}/pipelines"
                           f"?per_page=100&page={page}", token) or []
        if not isinstance(batch, list):
            raise ValueError(f"unexpected pipelines response on page {page}")
        rows += batch
        if len(batch) < 100:
            break
    else:
        raise ValueError("MR has more than 2000 pipelines - cannot check them all")
    newest, unordered = {}, []
    for p in rows:
        if not isinstance(p, dict) or p.get("sha") != sha:
            continue
        if not isinstance(p.get("id"), int):
            unordered.append(p)
            continue
        key = p.get("source") or "?%s" % p.get("id")
        if key not in newest or p["id"] > newest[key]["id"]:
            newest[key] = p
    return list(newest.values()) + unordered


def _expected_author():
    """GitLab username that opens worker MRs: CD_MR_AUTHOR (required; no GET /user fallback, since the PAT owner
    may also open MRs by hand, and those must not count as the bot)."""
    return (os.getenv("CD_MR_AUTHOR") or "").strip() or None


def _identity_problem(api, token, project, mr, info, reviewed):
    """Re-verify the MR from GitLab's side (the event payload is not trusted). Returns None if it is exactly the
    reviewed, bot-authored, same-project MR into the default branch; else a short reason (no merge)."""
    sha = str(info.get("sha") or "")
    if sha != reviewed:
        return f"MR head {sha[:8] or '?'} != reviewed {reviewed[:8]} (pushed after review?)"
    if (info.get("diff_refs") or {}).get("head_sha") != reviewed:
        return "GitLab's MR diff is not yet computed at the reviewed SHA"
    if info.get("state") != "opened":
        return f"MR state is {info.get('state')!r}, not opened"
    if info.get("draft") or info.get("work_in_progress"):
        return "MR is a draft"
    sb = info.get("source_branch") or ""
    if sb != mr.get("source_branch") or not sb.startswith(WORKER_BRANCH_PREFIX):
        return f"MR source branch {sb!r} != the reviewed worker branch"
    pid = info.get("project_id")
    if pid is None or info.get("source_project_id") != pid or info.get("target_project_id", pid) != pid:
        return "cross-project (fork) MR"
    proj = api("GET", f"/projects/{_pid(project)}", token) or {}
    if proj.get("id") != pid or str(proj.get("path_with_namespace") or "").lower() != str(project).lower():
        return "MR project does not match"
    if not proj.get("default_branch") or info.get("target_branch") != proj.get("default_branch"):
        return f"MR targets {info.get('target_branch')!r}, not the default branch"
    author, want = (info.get("author") or {}).get("username"), _expected_author()
    if not want:
        return "cannot determine the broker bot identity (set CD_MR_AUTHOR)"
    if str(author or "").lower() != want.lower():
        return f"MR author {author!r} is not the broker bot"
    return None


# ---------------- 1. auto-merge ----------------
def on_review_verdict(mr, env, run_id=None, *, comment, dm_ops, api=None, tier_cfg=None):
    """Called after the mr-reviewer verdict is posted. Returns an outcome string (for tests/logs) or None
    when CD is off / not applicable. NEVER raises."""
    if not enabled():
        return None
    try:
        return _on_review_verdict(mr or {}, env, run_id, comment, dm_ops, api or gitlab_mr._api, tier_cfg)
    except Exception as e:   # belt-and-suspenders: never wedge the reviewer worker; never silent, never merges
        log.exception("cd: on_review_verdict crashed: %s", e)
        m = mr if isinstance(mr, dict) else {}
        try:
            dm_ops(f"[CD] auto-merge check crashed for {m.get('project')}!{m.get('mr_iid')} "
                   f"({type(e).__name__}) - NOT merged, ops please review", subject="cd:error")
        except Exception as e2:
            log.warning("cd: ops DM failed: %s", e2)
        return "error"


def _on_review_verdict(mr, env, run_id, comment, dm_ops, api, tier_cfg):
    if not isinstance(env, dict) or env.get("status") != "pass":
        return None
    project, iid, tid = mr.get("project"), mr.get("mr_iid"), mr.get("task_id")
    if not project or not iid or not str(mr.get("source_branch") or "").startswith(WORKER_BRANCH_PREFIX):
        log.info("cd: %s!%s not a worker-authored MR -> no auto-merge", project, iid)
        return None
    if not repo_allowed(project):
        log.info("cd: %s not on CD_REPO_ALLOWLIST -> no auto-merge", project)
        return None
    url = mr.get("web_url") or f"{project}!{iid}"
    ref = f"MR {url}" + (f" ticket {ticket_url(tid)}" if tid is not None else "")
    rid = run_id or uuid.uuid4().hex

    def note(html_body):
        if tid is not None:
            try:
                comment(tid, html_body, rid)
            except Exception as e:
                log.warning("cd: ticket comment failed task_id=%s: %s", tid, e)

    def say(msg, html_body):
        try:
            dm_ops(f"[CD] {msg}", subject=f"cd:{project}!{iid}")
        except Exception as e:
            log.warning("cd: ops DM failed: %s", e)
        note(html_body)

    link = f'<a href="{html.escape(url)}">!{iid}</a>'
    # CD safety: only a verdict produced on the native "cc" harness auto-merges. envelope.EnvelopeParser
    # stamps env["harness"] from the reviewer route's Route.adapter at parse time; hermes (or any other/
    # unknown value, incl. a older envelope with no "harness" key at all - fail-open would be wrong there
    # too) never auto-merges. Cheap and first: no GitLab API call spent on a verdict we're not going to act on.
    # CD_ACCEPT_HERMES (default off) adds "hermes" to the trusted set; the tier gate below is unchanged.
    harness = str((env or {}).get("harness") or "")
    trusted = ("cc", "hermes") if hermes_accepted() else ("cc",)
    if harness not in trusted:
        names = ", ".join(repr(h) for h in trusted)
        say(f"not auto-merging {ref}: reviewer verdict came from harness {harness or 'unknown'!r} "
            f"(only {names} auto-merges), ops please review",
            f"<p>⚠️ <b>CD:</b> reviewer PASS on {link}, but the verdict came from the "
            f"<code>{html.escape(harness or 'unknown')}</code> harness (auto-merge only trusts "
            f"{', '.join(f'<code>{h}</code>' for h in trusted)}); not merging, ops notified.</p>")
        return "non-cc-harness"
    via = " (hermes verdict)" if harness == "hermes" else ""
    if not _expected_author():     # fail closed: no bot identity means no merge; DM the operator once, not per MR
        with _lock:
            first = not _NO_AUTHOR_WARNED
            if first:
                _NO_AUTHOR_WARNED.append(True)
        if first:
            say(f"CD_MR_AUTHOR is unset - auto-merge is OFF until it is set (first hit: {ref})",
                f"<p>⏳ <b>CD:</b> reviewer PASS on {link}, but the broker's bot identity "
                f"(<code>CD_MR_AUTHOR</code>) is not configured - not merging; ops notified.</p>")
        else:
            note(f"<p>⏳ <b>CD:</b> reviewer PASS on {link}, but <code>CD_MR_AUTHOR</code> is not configured "
                 f"- not merging; waiting on ops.</p>")
        return "no-author"
    token = _token()
    if not token:
        say(f"cannot auto-merge {ref}: no merge token ({GITLAB_MERGE_TOKEN_ENV} / fallback unset) - ops, "
            f"please merge by hand",
            f"<p>⏳ <b>CD:</b> reviewer PASS on {link}, but the broker has no merge token - waiting on ops "
            f"to merge.</p>")
        return "no-token"
    reviewed = str(mr.get("reviewed_sha") or "")
    if not reviewed:
        say(f"not auto-merging {ref}: no reviewed SHA recorded (the reviewer saw no diff) - ops please review",
            f"<p>⚠️ <b>CD:</b> reviewer PASS on {link}, but no reviewed commit was recorded (empty review "
            f"diff) - not merging; ops notified.</p>")
        return "unpinned"
    try:
        info = api("GET", f"/projects/{_pid(project)}/merge_requests/{iid}", token) or {}
        problem = _identity_problem(api, token, project, mr, info, reviewed)
    except Exception as e:
        problem = f"MR lookup failed: {_err(e)}"
    if problem:
        say(f"not auto-merging {ref}: {problem} - ops please review",
            f"<p>⚠️ <b>CD:</b> reviewer PASS on {link}, but GitLab's view of the MR does not match what was "
            f"reviewed (<code>{html.escape(problem)}</code>) - not merging; ops notified.</p>")
        return "identity-mismatch"
    try:
        paths = _mr_paths(api, token, project, iid)
    except Exception as e:
        err = _err(e)
        say(f"cannot classify {ref} (diff fetch failed: {err}) - not merging, ops please review",
            f"<p>⚠️ <b>CD:</b> could not fetch the diff of {link} to classify its tier "
            f"(<code>{html.escape(err)}</code>) - not merging; ops notified.</p>")
        return "classify-failed"
    path_tier, hits = classify_paths(paths, tier_cfg, project)
    tier = max_tier(reviewer_tier(env), path_tier)
    if tier == "full":
        why = ", ".join(f"{c}:{p}" for c, p in hits[:6]) or "reviewer-stated Full"
        say(f"Full-tier, reviewer PASS, WAITING ON OPS to merge: {ref} (why: {why})",
            f"<p>⏳ <b>CD: waiting on ops</b>: {link} passed review but is <b>Full</b>-tier "
            f"({html.escape(why)}), so the broker will not auto-merge it. Ops has been DMed.</p>")
        return "full"
    try:
        pl = info.get("head_pipeline") or info.get("pipeline") or {}
        pipe = (pl.get("status") or "").lower()
        if pl.get("sha") and pl.get("sha") != reviewed:
            pipe = f"{pipe or 'unknown'} (for {str(pl.get('sha'))[:8]}, not the reviewed SHA)"
        if pipe == "success" or pipe in _PIPE_PENDING:
            # head_pipeline is ONE pipeline. A sha can carry both a push (branch) pipeline and an MR
            # pipeline (e.g. the MR pipeline FAILED while the push pipeline PASSED), so EVERY pipeline of
            # this MR at the reviewed SHA must be green; one failed/canceled/still-running one refuses (MWPS
            # only waits on the head pipeline, so a running sibling would be merged past).
            # Only the newest pipeline per (sha, source) counts, so a retried-green failure no longer blocks.
            # A lookup failure raises and becomes merge-failed (fail closed, operator DMed).
            bad = [p for p in _sha_pipelines(api, token, project, iid, reviewed)
                   # the pending head itself is MWPS's job; excluded only while the list agrees it's pending
                   if not (pl.get("id") and p.get("id") == pl.get("id") and pipe in _PIPE_PENDING
                            and (p.get("status") or "").lower() in _PIPE_PENDING)
                   and (p.get("status") or "").lower() != "success"]
            if bad:
                pipe = f"{bad[0].get('status') or 'unknown'} (pipeline {bad[0].get('id')} at the reviewed SHA)"
        body = {"sha": reviewed, "should_remove_source_branch": True}   # GitLab refuses if head moved
        if pipe == "success":
            pass
        elif pipe in _PIPE_PENDING:
            body["merge_when_pipeline_succeeds"] = True
        else:
            state = pipe or "no pipeline"
            say(f"not auto-merging {ref}: MR pipeline is '{state}' (needs green) - ops please look",
                f"<p>⚠️ <b>CD:</b> {link} passed review ({tier}-tier) but its pipeline is "
                f"<code>{html.escape(state)}</code>, so the broker did not merge it. Ops notified.</p>")
            return "pipeline-not-green"
        res = api("PUT", f"/projects/{_pid(project)}/merge_requests/{iid}/merge", token, body) or {}
    except Exception as e:
        err = _err(e)
        say(f"auto-merge FAILED for {ref}: {err} - ops please merge by hand",
            f"<p>⚠️ <b>CD: auto-merge failed</b> for {link}: <code>{html.escape(err)}</code>. "
            f"Ops notified.</p>")
        return "merge-failed"
    with _lock:
        _BY_MR[(project, iid)] = tid
        if res.get("merge_commit_sha"):
            _BY_SHA[res["merge_commit_sha"]] = tid
    merged = res.get("state") == "merged"
    if merged:
        body_html = (f"<p>✅ <b>CD: merged</b> {link} ({tier}-tier, reviewer PASS{via}, pipeline green). "
                     f"Deploy hand-off follows the main publish pipeline; this ticket stays open until the "
                     f"deploy is live-verified.</p>")
    else:
        body_html = (f"<p>🔁 <b>CD: auto-merge armed</b> on {link} ({tier}-tier, reviewer PASS{via}) - it "
                     f"merges when its pipeline succeeds. Ticket stays open until live-verify.</p>")
    if via:     # a hermes-driven merge is also DMed to the operator, so it never lands silently
        say(f"{'merged' if merged else 'auto-merge armed on'} {ref} ({tier}-tier{via})", body_html)
    else:
        note(body_html)
    return "merged" if merged else "armed"


# ---------------- 1b. per-repo auto-merge on MR open ----------------
# AUTO_MERGE_REPOS ("ns/name" list, same separators as CD_REPO_ALLOWLIST; empty = OFF, read at CALL time): a worker
# MR opened in a listed repo is merged by the broker once its pipeline passes, with no reviewer verdict and no tier
# gate (meant for content-only repos, e.g. owner/docs-wiki; remove the repo to go back to manual). Independent
# of CD_ENABLED. Still fail-closed: GitLab's own view of the MR must be exactly the branch the broker just pushed
# (opened, not draft, same project, default target, author CD_MR_AUTHOR, head == the pushed commit), EVERY
# pipeline at that head must be green, and the merge is pinned to that SHA. Any doubt means no merge,
# with a DM to the operator and a ticket comment. The pipeline is polled (it is created a few seconds after the MR)
# AUTO_MERGE_POLLS x AUTO_MERGE_POLL_S; still running after that: merge_when_pipeline_succeeds; none at all: no merge.
def auto_merge_repo(project):
    allow = {r.lower() for r in re.split(r"[,:\s]+", os.getenv("AUTO_MERGE_REPOS") or "") if r}
    return str(project or "").lower() in allow


def on_mr_opened(opened, branch, task_id=None, run_id=None, *, comment, dm_ops, api=None, sleep=None,
                 background=True):
    """Called by the CC adapter right after a worker MR opens. Returns None when the repo is not in
    AUTO_MERGE_REPOS; "started" when the merge check was handed to a daemon thread; else (background=False)
    the outcome string. NEVER raises."""
    opened = opened if isinstance(opened, dict) else {}
    if not auto_merge_repo(opened.get("project")):
        return None
    args = (opened, branch, task_id, run_id, comment, dm_ops, api or gitlab_mr._api, sleep or time.sleep)
    if background:
        threading.Thread(target=_auto_merge_safe, args=args, name="cd-auto-merge", daemon=True).start()
        return "started"
    return _auto_merge_safe(*args)


def _auto_merge_safe(*args):
    try:
        return _auto_merge(*args)
    except Exception as e:   # never silent, never merges
        log.exception("cd: auto-merge crashed: %s", e)
        try:
            args[5](f"[CD] auto-merge check crashed for {args[0].get('project')}!{args[0].get('iid')} "
                    f"({type(e).__name__}) - NOT merged, ops please review", subject="cd:error")
        except Exception as e2:
            log.warning("cd: ops DM failed: %s", e2)
        return "error"


def _auto_merge(opened, branch, tid, run_id, comment, dm_ops, api, sleep):
    project, iid = opened.get("project"), opened.get("iid")
    url = opened.get("web_url") or f"{project}!{iid}"
    ref = f"MR {url}" + (f" ticket {ticket_url(tid)}" if tid is not None else "")
    link = f'<a href="{html.escape(url)}">!{iid}</a>'
    rid = run_id or uuid.uuid4().hex

    def say(msg, html_body):
        try:
            dm_ops(f"[CD] {msg}", subject=f"cd:{project}!{iid}")
        except Exception as e:
            log.warning("cd: ops DM failed: %s", e)
        if tid is not None:
            try:
                comment(tid, html_body, rid)
            except Exception as e:
                log.warning("cd: ticket comment failed task_id=%s: %s", tid, e)

    def refuse(outcome, why):
        say(f"not auto-merging {ref}: {why} - ops please review",
            f"<p>⚠️ <b>Auto-merge:</b> {link} was not merged (<code>{html.escape(why)}</code>); ops notified.</p>")
        return outcome

    if not project or iid is None or not str(branch or "").startswith(WORKER_BRANCH_PREFIX):
        return refuse("not-worker-mr", f"branch {branch!r} is not a worker branch")
    if not _expected_author():
        return refuse("no-author", "CD_MR_AUTHOR is unset (the broker's bot identity)")
    token = _token()
    if not token:
        return refuse("no-token", f"no merge token ({GITLAB_MERGE_TOKEN_ENV} / fallback unset)")
    head8 = str(branch).rsplit("-", 1)[-1].lower()       # worktree.publish_mr names it <prefix>/<key>-<head[:8]>
    polls = max(1, int(_num_env("AUTO_MERGE_POLLS", 12)))
    for attempt in range(polls):
        try:
            info = api("GET", f"/projects/{_pid(project)}/merge_requests/{iid}", token) or {}
            sha = str(info.get("sha") or "")
            # diff_refs is the reviewer's-diff check; nobody reviews here and it lags a fresh MR, so it's not required.
            problem = (_identity_problem(api, token, project, {"source_branch": branch},
                                         {**info, "diff_refs": {"head_sha": sha}}, sha)
                       if len(head8) == 8 and sha.lower().startswith(head8)
                       else f"MR head {sha[:8] or '?'} is not the pushed commit {head8}")
        except Exception as e:
            problem = f"MR lookup failed: {_err(e)}"
        if problem:
            return refuse("identity-mismatch", problem)
        pl = info.get("head_pipeline") or info.get("pipeline") or {}
        pipe = (pl.get("status") or "").lower() if pl.get("sha") in (None, sha) else ""
        if pipe:
            break
        if attempt + 1 < polls:
            sleep(_num_env("AUTO_MERGE_POLL_S", 10))
    else:
        return refuse("no-pipeline", "no pipeline at the MR head (needs a green pipeline)")
    try:
        if pipe == "success" or pipe in _PIPE_PENDING:
            bad = [p for p in _sha_pipelines(api, token, project, iid, sha)
                   if not (pl.get("id") and p.get("id") == pl.get("id") and pipe in _PIPE_PENDING
                           and (p.get("status") or "").lower() in _PIPE_PENDING)
                   and (p.get("status") or "").lower() != "success"]
            if bad:
                pipe = f"{bad[0].get('status') or 'unknown'} (pipeline {bad[0].get('id')} at the MR head)"
        if pipe != "success" and pipe not in _PIPE_PENDING:
            return refuse("pipeline-not-green", f"MR pipeline is '{pipe}'")
        body = {"sha": sha, "should_remove_source_branch": True}   # GitLab refuses if the head moved
        if pipe != "success":
            body["merge_when_pipeline_succeeds"] = True
        res = api("PUT", f"/projects/{_pid(project)}/merge_requests/{iid}/merge", token, body) or {}
    except Exception as e:
        return refuse("merge-failed", f"merge API failed: {_err(e)}")
    with _lock:
        _BY_MR[(project, iid)] = tid
        if res.get("merge_commit_sha"):
            _BY_SHA[res["merge_commit_sha"]] = tid
    merged = res.get("state") == "merged"
    if tid is not None:
        try:
            comment(tid, (f"<p>✅ <b>Auto-merge:</b> merged {link} (pipeline green, AUTO_MERGE_REPOS).</p>" if merged
                          else f"<p>🔁 <b>Auto-merge armed</b> on {link}: it merges when its pipeline "
                               f"succeeds (AUTO_MERGE_REPOS).</p>"), rid)
        except Exception as e:
            log.warning("cd: ticket comment failed task_id=%s: %s", tid, e)
    return "merged" if merged else "armed"


def _num_env(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return float(default)


# ---------------- 2. publish and deploy hand-off ----------------
_TID_PATTERNS = tuple(rx for rx in (re.compile(r"vikunja[:_]task[:_](\d+)"), ticket_id_regex()) if rx)


def _tid_in(*texts):
    for t in texts:
        for rx in _TID_PATTERNS:
            m = rx.search(str(t or ""))
            if m:
                return int(m.group(1))
    return None


def find_ticket(api, token, project, sha, commit_msg=""):
    """Trace a default-branch commit back to its Vikunja ticket: (1) the broker's own merge records,
    (2) the MR(s) that contain the commit (source branch eph/code-change/vikunja_task_<id>-*, or the MR
    description 'key=vikunja:task:<id>' / a TICKET_URL_TEMPLATE link), (3) the commit message."""
    with _lock:
        if sha in _BY_SHA:
            return _BY_SHA[sha]
    mrs = []
    if token:
        try:
            mrs = api("GET", f"/projects/{_pid(project)}/repository/commits/{sha}/merge_requests", token) or []
        except Exception as e:
            log.warning("cd: commit->MR lookup failed %s@%s: %s", project, sha[:8], _err(e))
    for m in mrs:
        with _lock:
            if (project, m.get("iid")) in _BY_MR and _BY_MR[(project, m.get("iid"))] is not None:
                return _BY_MR[(project, m.get("iid"))]
        tid = _tid_in(m.get("source_branch"), m.get("description"), m.get("title"))
        if tid is not None:
            return tid
    return _tid_in(commit_msg)


def resolve_ver(api, token, project, sha, entry, pipeline_iid):
    """entry["ver_template"] with {VERSION} (the VERSION file at sha), {pipeline_iid}, {sha}, {short_sha}.
    Any unresolvable placeholder falls back to the sha."""
    tmpl = entry.get("ver_template")
    if not tmpl:
        return sha
    vals = {"sha": sha, "short_sha": sha[:8], "pipeline_iid": pipeline_iid}
    if "{VERSION}" in tmpl:
        try:
            f = api("GET", f"/projects/{_pid(project)}/repository/files/VERSION?ref={sha}", token)
            vals["VERSION"] = base64.b64decode(f["content"]).decode().strip()
        except Exception as e:
            log.warning("cd: VERSION fetch failed %s@%s: %s -> ver=sha", project, sha[:8], _err(e))
            return sha
    try:
        if any(vals.get(k) in (None, "") for k in re.findall(r"\{(\w+)\}", tmpl)):
            return sha
        return tmpl.format(**vals)
    except (KeyError, IndexError, ValueError):
        return sha


_EXPECTED_LIVE = {"nightly": "after the next nightly infra update run",
                  "registry-image+compose": "within a few minutes (the new image is pulled on its next "
                                            "compose/watchtower cycle)"}


def handle_pipeline(payload, deploy_paths, *, comment, dm, dm_ops, api=None):
    """Handle a GitLab Pipeline Hook (deploy hand-off). Returns an outcome string, or None if CD is off / not a pipeline
    event. NEVER raises."""
    if not enabled() or not isinstance(payload, dict) or payload.get("object_kind") != "pipeline":
        return None
    try:
        return _handle_pipeline(payload, deploy_paths or {}, comment, dm, dm_ops, api or gitlab_mr._api)
    except Exception as e:
        log.exception("cd: handle_pipeline crashed: %s", e)
        return "error"


def _handle_pipeline(payload, deploy_paths, comment, dm, dm_ops, api):
    attrs = payload.get("object_attributes") or {}
    proj = payload.get("project") or {}
    repo, sha = proj.get("path_with_namespace"), attrs.get("sha")
    if attrs.get("status") != "success":
        return "ignored:status"
    default = proj.get("default_branch") or "main"
    if attrs.get("tag") or attrs.get("ref") != default:
        return "ignored:ref"
    entry = deploy_paths.get(repo) if repo else None
    kind = (entry or {}).get("kind") or "none"
    if not sha or kind == "none":
        return "ignored:no-deploy-path"
    key = (repo, sha)
    with _lock:
        if key in _HANDED_OFF or key in _IN_FLIGHT:
            return "ignored:duplicate"
        _IN_FLIGHT.add(key)
    try:
        out = _hand_off(payload, repo, sha, entry, kind, attrs, comment, dm, dm_ops, api)
    finally:
        with _lock:
            _IN_FLIGHT.discard(key)
    if out in ("handoff-sent", "commented"):   # dedup ONLY after DM and comment succeeded, so a redelivery
        with _lock:                             # of a failed hand-off retries it
            _HANDED_OFF.add(key)
    return out


def _verify_pipeline(api, token, repo, sha, attrs):
    """Re-read the pipeline + project from GitLab (the hook payload is not trusted). Returns None if it really is a
    successful default-branch, non-tag pipeline at `sha`; else a short reason."""
    if not token:
        return "no GitLab token to verify the pipeline"
    p = api("GET", f"/projects/{_pid(repo)}/pipelines/{attrs.get('id')}", token) or {}
    proj = api("GET", f"/projects/{_pid(repo)}", token) or {}
    if p.get("status") != "success" or p.get("sha") != sha or p.get("tag") \
            or not proj.get("default_branch") or p.get("ref") != proj.get("default_branch"):
        return (f"GitLab says pipeline {attrs.get('id')} is status={p.get('status')!r} ref={p.get('ref')!r} "
                f"sha={str(p.get('sha'))[:8]} (default {proj.get('default_branch')!r})")
    branch = urllib.parse.quote(proj["default_branch"], safe="")
    head = ((api("GET", f"/projects/{_pid(repo)}/repository/branches/{branch}", token) or {})
            .get("commit") or {}).get("id")
    if head != sha:   # a stale replay (e.g. manual redelivery after a restart) must not roll prod back
        return (f"stale: {sha[:8]} is no longer the {proj['default_branch']!r} head "
                f"(head {str(head)[:8]}) - not rolling back")
    return None


def _hand_off(payload, repo, sha, entry, kind, attrs, comment, dm, dm_ops, api):
    token = _token()
    try:
        problem = _verify_pipeline(api, token, repo, sha, attrs)
    except Exception as e:
        problem = f"pipeline lookup failed: {_err(e)}"
    if problem:
        try:
            dm_ops(f"[CD] deploy hand-off NOT sent for {repo}@{sha[:8]}: {problem}",
                   subject=f"cd-handoff:{repo}")
        except Exception as e:
            log.warning("cd: ops DM failed: %s", e)
        return "verify-failed"
    tid = find_ticket(api, token, repo, sha, (payload.get("commit") or {}).get("message", ""))
    ver = resolve_ver(api, token, repo, sha, entry, attrs.get("iid"))
    pipe_url = f"{gitlab_web_url()}/{repo}/-/pipelines/{attrs.get('id')}" if gitlab_web_url() else f"pipeline {attrs.get('id')}"
    rid = uuid.uuid4().hex

    def note(html_body):
        """Return True if posted (or there is no ticket to post to), False if the comment failed."""
        if tid is None:
            return True
        try:
            return bool(comment(tid, html_body, rid))
        except Exception as e:
            log.warning("cd: ticket comment failed task_id=%s: %s", tid, e)
            return False

    if kind.startswith("ansible"):
        line = f"DEPLOY-HANDOFF repo={repo} sha={sha} ver={ver} ticket={tid if tid is not None else 'none'}"
        with _lock:
            sent = (repo, sha) in _DM_SENT        # a retry after comment-failed must not DM ansible twice
        if not sent:
            try:
                sent = dm(ANSIBLE_HANDLE, line)
            except Exception as e:
                log.warning("cd: ansible hand-off DM raised: %s", e)
            if sent:
                with _lock:
                    _DM_SENT.add((repo, sha))
        if not sent:
            try:
                dm_ops(f"[CD] deploy hand-off DM to {ANSIBLE_HANDLE} FAILED - send by hand: {line}",
                       subject=f"cd-handoff:{repo}")
            except Exception as e:
                log.warning("cd: ops DM failed: %s", e)
            note(f"<p>⚠️ <b>CD:</b> <a href=\"{html.escape(pipe_url)}\">publish pipeline</a> succeeded "
                 f"(<code>{html.escape(ver)}</code>) but the deploy hand-off DM to ansible failed - ops "
                 f"notified.</p>")
            return "handoff-failed"
        posted = note(f"<p>🚚 <b>CD: published</b> <code>{html.escape(ver)}</code> "
             f"(<a href=\"{html.escape(pipe_url)}\">pipeline</a>, <code>{sha[:8]}</code>); deploy hand-off "
             f"sent to ansible (play <code>{html.escape(str(entry.get('play', '?')))}</code>, pin "
             f"<code>{html.escape(str(entry.get('pin_var', '?')))}</code>). Ansible closes this ticket on "
             f"live-verify.</p>")
        return "handoff-sent" if posted else "comment-failed"
    expected = entry.get("expected_live") or _EXPECTED_LIVE.get(kind, "on the repo's normal deploy path")
    posted = note(f"<p>🚚 <b>CD: published</b> <code>{html.escape(ver)}</code> "
                  f"(<a href=\"{html.escape(pipe_url)}\">pipeline</a>, <code>{sha[:8]}</code>); deploy path "
                  f"<code>{html.escape(kind)}</code> - expected live {html.escape(expected)}.</p>")
    return "commented" if posted else "comment-failed"
