#!/usr/bin/env python3
"""repo_catalog.py: the set of repos a code worker may be pointed at when repo inference is ON.

A code ticket without a `repo:` line used to fail closed. With
REPO_INFER_ENABLED=1 the broker instead infers the repo (repo_infer.py) from THIS catalog:

  * listing   - GitLab `/projects?membership=true&simple=true&archived=false`, paginated, via the broker's
                GITLAB_MR_TOKEN (read from the env at RUNTIME by NAME; never logged, never stored). Filtered to
                `<REPO_CATALOG_NS>/*` (from site.env; unset means the catalog is unavailable). Cached as JSON with
                a TTL; a failed refresh falls back to the stale cache, and no cache at all raises
                CatalogUnavailable (the caller asks, never guesses).
  * cards     - one line per repo for the matchers: GitLab description, else the first prose line of a local
                README.md / AGENTS.md, plus `config/repo-aliases.json` (aliases + card overrides), plus
                generated aliases for REPO_SUBPROJECT_REPOS subprojects (`_scan_subprojects`).
  * tiers     - DENY (never reachable, never shown): REPO_DENY globs (site.env; e.g. PII repos, mirrors/*) and
                anything archived. TIER B (control plane): REPO_TIER_B globs + any local checkout
                carrying a `*-AGENT.md` charter: reachable ONLY by an explicit `repo:` line or an exact-name
                match (enforced in repo_infer, never by a model pick).
  * clones    - an existing local checkout is preferred (scan of REPO_LOCAL_ROOTS children + REPO_LOCAL_DIRS,
                overridable by REPO_LOCAL_MAP); otherwise clone on demand into ~/.cache/eph-repo-cache/<ns>/<name>
                under a per-repo lock, fetching before every cut.

Value-free: no secret bytes here. `has_push_cred` only asks "does the credential store hold an entry for
this repo path?" It reads the file line-by-line in memory and returns a bool; nothing is logged or kept.
Stdlib only (py3.9, like the rest of the broker).
"""
import fcntl, fnmatch, json, logging, os, re, shutil, subprocess, threading, time
import urllib.parse, urllib.request
import site_env
from dataclasses import dataclass, field

log = logging.getLogger("eph-broker.repo_catalog")

HERE = os.path.dirname(os.path.abspath(__file__))


def _env_list(name, default):
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _required_list(name):
    """A protective glob list (REPO_DENY / REPO_TIER_B) from site.env. Unset raises CatalogUnavailable (the
    caller asks), never an empty list that would silently drop the protection; an explicit empty value is allowed."""
    if name not in os.environ:
        raise CatalogUnavailable(f"{name} unset (site.env); set it, empty allowed")
    out = _env_list(name, "")
    if not out and name == "REPO_DENY" and name not in _WARNED_EMPTY:   # legal, but auditable
        _WARNED_EMPTY.add(name)
        log.warning("REPO_DENY explicitly empty: no repo is denied")
    return out


_WARNED_EMPTY = set()


GITLAB_API = site_env.gitlab_api_url()   # GITLAB_API / GITLAB_WEB_URL (site.env); "" means catalog unavailable
GITLAB_TOKEN_ENV = os.getenv("GITLAB_MR_TOKEN_ENV", "GITLAB_MR_TOKEN")   # NAME only (same var gitlab_mr uses)
GITLAB_HOST = urllib.parse.urlparse(GITLAB_API).netloc
CATALOG_NS = os.getenv("REPO_CATALOG_NS", "")   # from site.env; "" means catalog unavailable
CACHE_FILE = os.path.expanduser(os.getenv("REPO_CATALOG_CACHE", "~/.cache/eph-broker/repo-catalog.json"))
CACHE_TTL_S = float(os.getenv("REPO_CATALOG_TTL_S", "3600"))
HTTP_TIMEOUT_S = float(os.getenv("REPO_CATALOG_HTTP_TIMEOUT", "15"))
CLONE_ROOT = os.path.expanduser(os.getenv("REPO_CLONE_ROOT", "~/.cache/eph-repo-cache"))
ALIASES_FILE = os.getenv("REPO_ALIASES_FILE", os.path.join(HERE, "config", "repo-aliases.json"))
CRED_FILE = os.path.expanduser(os.getenv("REPO_CRED_FILE", "~/.git-credentials"))
# NAME of the env var holding the namespace-wide push token (used only with REPO_PUSH_BROAD_CRED_ENABLED).
PUSH_TOKEN_ENV = os.getenv("GITLAB_PUSH_TOKEN_ENV", "GITLAB_PUSH_TOKEN")
LOCAL_ROOTS = [os.path.expanduser(p) for p in os.getenv("REPO_LOCAL_ROOTS", "").split(":") if p]
LOCAL_DIRS = [os.path.expanduser(p) for p in os.getenv("REPO_LOCAL_DIRS", "").split(":") if p]

# REPO_TIER_B / REPO_DENY: REQUIRED in site.env (the tier list and the PII deny list). Globs match the
# short name OR the full namespace path.
# Second-level subproject dirs (`<checkout>/<box>/<sub>/` with a compose file or a handoff) of these repos
# become aliases of the repo at catalog build, so a ticket naming `calendar-app` surfaces owner/docker. Common
# words and names that collide with a catalog repo name are never aliased.
SUBPROJECT_REPOS = _env_list("REPO_SUBPROJECT_REPOS", "")   # from site.env
SUBPROJECT_STOPWORDS = set(x.lower() for x in _env_list("REPO_SUBPROJECT_STOPWORDS", (
    "scripts,caddy,jenkins,docker,build,wiki,ops,fleet,agents,config,configs,data,backup,backups,test,tests,"
    "docs,web,www,app,api,lib,tmp,old,archive,shared,common,nginx,traefik,postgres,redis,mysql,monitoring,"
    "logs,src,tools,cron,gitlab,proxy,dns,mail")))
_COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")


class CatalogUnavailable(RuntimeError):
    """No listing and no cache: the caller must ASK (never guess)."""


@dataclass
class Repo:
    path: str                         # "owner/eph-broker" (full namespace path)
    name: str                         # "eph-broker" (path minus the catalog namespace)
    desc: str = ""
    default_branch: str = "main"
    http_url: str = ""
    archived: bool = False
    local: str = ""                   # existing local checkout (preferred), else "" -> on-demand clone
    remote: str = ""                  # the local checkout's origin url (for the push-cred check)
    charter: bool = False             # local checkout carries a *-AGENT.md charter -> tier B
    readme: str = ""                  # first prose line of a local README/AGENTS.md
    aliases: tuple = ()
    card_override: str = ""
    subprojects: dict = field(default_factory=dict)   # generated alias -> "box/sub" inside the checkout

    @property
    def card(self):
        return (self.card_override or self.desc or self.readme or "").strip()[:80]


def _glob_hit(repo, globs):
    return any(fnmatch.fnmatch(repo.name, g) or fnmatch.fnmatch(repo.path, g) for g in globs)


class Catalog:
    def __init__(self, repos, tier_b=None, deny=None):
        self.tier_b_globs = list(tier_b if tier_b is not None else _required_list("REPO_TIER_B"))
        self.deny_globs = list(deny if deny is not None else _required_list("REPO_DENY"))
        self.all = list(repos)
        # DENY: filtered out entirely; never reachable, never offered to a model, never shown as a candidate.
        self.repos = [r for r in self.all if not self.is_denied(r)]
        self.by_path = {r.path: r for r in self.repos}
        self.by_name = {r.name: r for r in self.repos}

    def is_denied(self, repo):
        return bool(repo.archived) or _glob_hit(repo, self.deny_globs)

    def is_tier_b(self, repo):
        return bool(repo.charter) or _glob_hit(repo, self.tier_b_globs)

    def clone_path(self, repo):
        return os.path.join(CLONE_ROOT, *repo.path.split("/"))

    def local_path(self, repo):
        return repo.local or self.clone_path(repo)

    def resolve_ref(self, ref):
        """An explicit `repo:` value -> Repo (or None). Accepts an absolute local path (a mapped checkout or
        a cache clone), a full `ns/name` path, or a bare name. A denied repo is never returned."""
        ref = (ref or "").strip().rstrip("/")
        if not ref:
            return None
        if ref.startswith("/"):
            return self.by_local_path(ref)
        if ref.endswith(".git"):
            ref = ref[:-4]
        return self.by_path.get(ref) or self.by_name.get(ref) or self.by_path.get(f"{CATALOG_NS}/{ref}")

    def names_listed(self, ref):
        """True iff an explicit `repo:` value names ANY listed repo, denied/archived ones included
        (resolve_ref returns None for those; the caller must park, not infer around an explicit intent)."""
        ref = (ref or "").strip().rstrip("/")
        ref = ref[:-4] if ref.endswith(".git") else ref
        return bool(ref) and any(ref in (r.name, r.path) for r in self.all)

    def by_local_path(self, path):
        path = os.path.normpath(path)
        for r in self.repos:
            if (r.local and os.path.normpath(r.local) == path) or os.path.normpath(self.clone_path(r)) == path:
                return r
        return None

    def path_allowed(self, path):
        """routing.code_repo_allowed backing (flag ON): a local path is allowed iff it IS a non-denied
        catalog repo's checkout/clone path. Tier B is allowed at this layer (explicit token reaches it);
        the no-model-pick-into-tier-B rule is enforced by repo_infer.infer."""
        return bool(path) and self.by_local_path(path) is not None


# ---------------------------------------------------------------- listing + cache
def _gitlab_list(token, ns=CATALOG_NS, max_pages=20):
    out = []
    for page in range(1, max_pages + 1):
        q = urllib.parse.urlencode({"membership": "true", "simple": "true", "archived": "false",
                                    "per_page": 100, "page": page, "order_by": "path", "sort": "asc"})
        req = urllib.request.Request(f"{GITLAB_API}/projects?{q}", headers={"PRIVATE-TOKEN": token})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            batch = json.loads(r.read() or "[]")
            nxt = r.headers.get("X-Next-Page", "")
        for p in batch:
            path = p.get("path_with_namespace") or ""
            if not path.startswith(ns + "/") or p.get("archived"):
                continue
            out.append({"path": path, "desc": (p.get("description") or "").strip(),
                        "default_branch": p.get("default_branch") or "main",
                        "http_url": p.get("http_url_to_repo") or f"https://{GITLAB_HOST}/{path}.git",
                        "archived": bool(p.get("archived"))})
        if len(batch) < 100 or not nxt:
            break
    return out


def _origin_path(d):
    """`ns/name` from d's origin remote if it points at our GitLab host, else None (plus the raw url)."""
    try:
        url = subprocess.run(["git", "-c", f"safe.directory={d}", "-C", d, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, ""
    if not GITLAB_HOST:              # an empty host would match every origin
        return None, re.sub(r"//[^@/]*@", "//", url)
    m = re.search(re.escape(GITLAB_HOST) + r"[:/](.+?)(?:\.git)?/?$", url)
    return (m.group(1) if m else None), re.sub(r"//[^@/]*@", "//", url)   # never keep userinfo


def _first_prose_line(d):
    for f in ("README.md", "AGENTS.md"):
        fp = os.path.join(d, f)
        if not os.path.isfile(fp):
            continue
        try:
            with open(fp, errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i > 60:
                        break
                    t = re.sub(r"[#>*`_\[\]]", "", line).strip()
                    if len(t) > 15 and not t.lower().startswith(("claude", "@", "<!--")):
                        return t[:120]
        except OSError:
            pass
    return ""


def _scan_local():
    """ns/name -> {local, remote, charter, readme} for existing checkouts. Worktrees (.git is a FILE) are
    skipped so a stray `-wt` checkout never shadows the real one; the first dir found wins."""
    found = {}
    cands = list(LOCAL_DIRS)
    for root in LOCAL_ROOTS:
        try:
            cands += [os.path.join(root, x) for x in sorted(os.listdir(root))]
        except OSError:
            pass
    for d in cands:
        if not os.path.isdir(os.path.join(d, ".git")):
            continue
        path, remote = _origin_path(d)
        if not path or path in found:
            continue
        try:
            charter = any(x.endswith("-AGENT.md") for x in os.listdir(d))
        except OSError:
            charter = False
        found[path] = {"local": d, "remote": remote, "charter": charter, "readme": _first_prose_line(d)}
    for entry in _env_list("REPO_LOCAL_MAP", ""):          # explicit `ns/name=/abs` overrides win
        p, sep, d = entry.partition("=")
        if sep and d.strip().startswith("/"):
            found.setdefault(p.strip(), {}).update({"local": d.strip()})
    return found


def _load_aliases(path=None):
    try:
        with open(path or ALIASES_FILE) as f:
            raw = json.load(f)
        return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}
    except (OSError, ValueError):
        return {}


def _scan_subprojects(root):
    """{sub: "box/sub"} for `root/<box>/<sub>/` dirs carrying a compose file or a handoff (a `*handoff*`
    file inside, or `root/<box>/_handoff/<sub>.md`). Raw: stop words / name collisions are dropped in build()."""
    out = {}
    try:
        boxes = sorted(os.listdir(root))
    except OSError:
        return out
    for box in boxes:
        bd = os.path.join(root, box)
        if box.startswith((".", "_")) or not os.path.isdir(bd):
            continue
        try:
            subs = sorted(os.listdir(bd))
        except OSError:
            continue
        for sub in subs:
            sd = os.path.join(bd, sub)
            if sub.startswith((".", "_")) or not os.path.isdir(sd):
                continue
            try:
                files = os.listdir(sd)
            except OSError:
                files = []
            if (any(f in _COMPOSE_FILES or "handoff" in f.lower() for f in files)
                    or os.path.isfile(os.path.join(bd, "_handoff", sub + ".md"))):
                out.setdefault(sub.lower(), f"{box}/{sub}")
    return out


def build(entries, aliases=None, tier_b=None, deny=None):
    """Pure: raw cache entries (+ aliases dict) -> Catalog. Threadable for tests."""
    aliases = _load_aliases() if aliases is None else aliases
    # A generated subproject alias never shadows a catalog repo name (denied/archived ones included)
    taken = set()
    for e in entries:
        taken |= {e["path"].lower(), e["path"].rsplit("/", 1)[-1].lower()}
    repos = []
    for e in entries:
        path = e["path"]
        name = path[len(CATALOG_NS) + 1:] if path.startswith(CATALOG_NS + "/") else path
        a = aliases.get(path) or aliases.get(name) or {}
        curated = [x.lower() for x in a.get("aliases", [])]
        subs = {s.lower(): w for s, w in (e.get("subprojects") or {}).items()
                if s.lower() not in SUBPROJECT_STOPWORDS and s.lower() not in taken and len(s) > 3}
        repos.append(Repo(path=path, name=name, desc=e.get("desc", ""),
                          default_branch=e.get("default_branch") or "main", http_url=e.get("http_url", ""),
                          archived=bool(e.get("archived")), local=e.get("local", ""), remote=e.get("remote", ""),
                          charter=bool(e.get("charter")), readme=e.get("readme", ""),
                          aliases=tuple(curated + [s for s in sorted(subs) if s not in curated]),
                          card_override=a.get("card", ""), subprojects=subs))
    return Catalog(repos, tier_b=tier_b, deny=deny)


def _read_cache(max_age=None):
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    # The cache is namespace-keyed. No namespace, or a cache written for another one (a config change,
    # an OSS install over an old cache file, an old file with no "ns"), is no cache: never serve its repos.
    if not CATALOG_NS or data.get("ns") != CATALOG_NS:
        return None
    if max_age is not None and time.time() - float(data.get("fetched_at", 0)) > max_age:
        return None
    return data.get("entries")


def _write_cache(entries):
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"fetched_at": time.time(), "ns": CATALOG_NS, "entries": entries}, f, indent=1)
    os.replace(tmp, CACHE_FILE)


def refresh():
    token = os.getenv(GITLAB_TOKEN_ENV)
    if not GITLAB_API:
        raise CatalogUnavailable("GITLAB_API / GITLAB_WEB_URL unset")
    if not CATALOG_NS:
        raise CatalogUnavailable("REPO_CATALOG_NS unset (site.env)")
    if not token:
        raise CatalogUnavailable(f"{GITLAB_TOKEN_ENV} unset")
    entries = _gitlab_list(token)
    local = _scan_local()
    for e in entries:
        e.update(local.get(e["path"], {}))
        if e["path"] in SUBPROJECT_REPOS and e.get("local"):
            e["subprojects"] = _scan_subprojects(e["local"])
    _write_cache(entries)
    log.info("repo catalog refreshed: %d repos under %s/*", len(entries), CATALOG_NS)
    return entries


_LOCK = threading.Lock()
_MEM = {"cat": None, "at": 0.0}
_OVERRIDE = None   # tests: set_catalog(Catalog) pins the catalog (no network, no cache file)


def set_catalog(cat):
    global _OVERRIDE
    _OVERRIDE = cat


def get_catalog():
    """The live catalog (memo -> fresh cache -> GitLab refresh -> stale cache). Raises CatalogUnavailable."""
    if _OVERRIDE is not None:
        return _OVERRIDE
    with _LOCK:
        if _MEM["cat"] is not None and time.time() - _MEM["at"] < CACHE_TTL_S:
            return _MEM["cat"]
        entries = _read_cache(CACHE_TTL_S)
        if entries is None:
            try:
                entries = refresh()
            except Exception as e:                            # never raise the token/url; message only
                log.warning("repo catalog refresh failed (%s); falling back to stale cache", type(e).__name__)
                entries = _read_cache()
        if not entries:
            raise CatalogUnavailable("no GitLab listing and no cached catalog")
        _MEM["cat"], _MEM["at"] = build(entries), time.time()
        return _MEM["cat"]


def path_allowed(path):
    try:
        return get_catalog().path_allowed(path)
    except Exception as e:
        log.warning("repo catalog unavailable for allowlist check (%s) -> fail-closed", e)
        return False


# ---------------------------------------------------------------- push creds + clones
def has_push_cred(repo, cred_file=None):
    """Would the broker's publish_mr push succeed credential-wise? An existing checkout with a non-https
    (ssh) origin carries its own auth -> True. Otherwise the path-scoped credential store must hold an entry
    for exactly this repo path. Reads in memory, returns a bool; never logs or returns a line.
    With REPO_PUSH_BROAD_CRED_ENABLED on, the broad token env var (PUSH_TOKEN_ENV, checked by NAME
    for presence only) also counts; flag on + token unset gives the per-repo answer, so the ticket parks."""
    return _per_repo_cred(repo, cred_file) or _broad_token_available()


def _broad_token_available():
    import routing
    return (routing.repo_push_broad_cred_enabled() and bool(os.getenv(PUSH_TOKEN_ENV))
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", PUSH_TOKEN_ENV) is not None)


def broad_cred_git_args(repo, cred_file=None):
    """`git -c ...` args that authenticate an HTTPS clone/fetch/push with the broad token, for a repo
    that has NO per-repo credential. [] when the flag is off, the token is unset, or the repo has its own
    credential (behavior then exactly as before). The token VALUE is never in argv, a URL, a log line, or a
    repo config: the inline helper expands the env var by NAME inside git's helper shell, answers `get` only
    (so a store/erase never persists it), and the leading empty `credential.helper=` keeps other helpers
    (the ~/.git-credentials store) from saving it on success.
    The helper answers ONLY for protocol=https + host=GITLAB_HOST (exact, so no other port/host); for
    any other host (e.g. a push redirected by a planted insteadOf/pushInsteadOf) it prints nothing."""
    import shlex
    if repo is None or not _broad_token_available() or _per_repo_cred(repo, cred_file):
        return []
    helper = ('!f() { test "$1" = get || return 0; h=; p=; '
              'while IFS== read -r k v; do case "$k" in host) h=$v;; protocol) p=$v;; esac; done; '
              f'test "$p" = https && test "$h" = {shlex.quote(GITLAB_HOST)} || return 0; '
              'echo username=oauth2; '
              f'echo "password=${{{PUSH_TOKEN_ENV}}}"; }}; f')
    return ["-c", "credential.helper=", "-c", f"credential.helper={helper}"]


def broad_cred_git_args_for_path(local_path):
    """publish_mr/save_wip backing: broad_cred_git_args for the non-denied catalog repo whose checkout/clone
    is `local_path`. Flag off -> [] without touching the catalog; unknown path / catalog unavailable -> []."""
    import routing
    if not routing.repo_push_broad_cred_enabled():
        return []
    try:
        return broad_cred_git_args(get_catalog().by_local_path(local_path))
    except Exception as e:
        log.warning("broad push cred lookup failed (%s) -> plain push", type(e).__name__)
        return []


def _per_repo_cred(repo, cred_file=None):
    url = repo.remote if repo.local else repo.http_url
    if repo.local and url and not url.startswith("https://"):
        return True
    pat = re.compile(r"@" + re.escape(GITLAB_HOST) + "/" + re.escape(repo.path) + r"(?:\.git)?/?\s*$")
    try:
        with open(cred_file or CRED_FILE) as f:
            return any(pat.search(line) for line in f)
    except OSError:
        return False


_CLONE_LOCKS = {}
_CLONE_LOCKS_GUARD = threading.Lock()


def ensure_clone(cat, repo, timeout=300):
    """Local path to cut from. Existing checkout -> itself (worktree.make fetches the target ref before the
    cut). Else clone into CLONE_ROOT/<ns>/<name> (first use) or `fetch --prune` it (every later use) under a
    per-repo lock (thread lock + flock, so two broker threads/processes never race a clone)."""
    if repo.local:
        return repo.local
    dest = cat.clone_path(repo)
    with _CLONE_LOCKS_GUARD:
        tlock = _CLONE_LOCKS.setdefault(dest, threading.Lock())
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    with tlock, open(dest + ".lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        cred = broad_cred_git_args(repo)                         # [] unless flag on + no per-repo cred
        if os.path.isdir(os.path.join(dest, ".git")):
            verb, argv = "fetch", ["git", *cred, "-C", dest, "fetch", "--prune", "--quiet", "origin"]
        else:
            if os.path.exists(dest):
                shutil.rmtree(dest, ignore_errors=True)          # a half-finished clone from a killed run
            verb, argv = "clone", ["git", *cred, "clone", "--quiet",
                                   repo.http_url or f"https://{GITLAB_HOST}/{repo.path}.git", dest]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            raise RuntimeError(f"{verb} {repo.path} failed: "
                               f"{re.sub(r'//[^@/]*@', '//', (r.stderr or '').strip())[:300]}")
    return dest
