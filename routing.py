#!/usr/bin/env python3
"""routing.py: subject_key to Route(domain, cwd/worktree base, env, prompt_class, model, read_only).

Phase-1 ships ONE row: `vikunja:task` assigned-to / labelled `ansible` routes to the ansible domain,
first worker READ/REPORT-ONLY (proving subject = ansible). model is resolved via `fleet-model.sh
ansible`, NOT hard-coded, so a fleet-mode change moves the worker's model too.

Value-free: no secrets. ZBUS_ENV / repo path are references, not credentials.
"""
import os, re, html, json, subprocess, logging
from dataclasses import dataclass, field

import provenance
import site_env  # noqa: F401  load the site overlay before the env reads below

log = logging.getLogger("eph-broker.routing")

FLEET_MODEL_SH = os.getenv("FLEET_MODEL_SH", "")   # from site.env; unset means resolve_model uses the fallback model


def _zbus_env(name):
    """A row's bus identity is the bot-env PATH in env var `name` (site.env). Unset means no ZBUS_ENV at all,
    so the worker has no bus identity (fail closed), never an empty one."""
    v = os.getenv(name, "")
    return {"ZBUS_ENV": v} if v else {}

# Built-in row triggers. An EMPTY trigger leaves its row out of PROFILES (nothing routes there). The rows the
# public tree ships a signed prompt for (code, general, mr-review, issues) default to eph-named triggers; every
# other built-in row needs site plumbing (its repo, accounts, launcher, prompt) and defaults to off, so a site
# turns it on by naming its trigger in site.env / broker.yaml `settings`.
# The label/assignee that DEFINES the ansible row. A config knob
# so the allowlist doesn't hard-code fleet policy. Override via env (or the hot-reload config surface).
ANSIBLE_ROW_LABEL = os.getenv("ANSIBLE_ROW_LABEL", "")

# The label that DEFINES the wiki-docs write row (same env-override pattern as the
# ansible row). A ticket carrying this label routes to the FIRST write-capable profile.
WIKI_ROW_LABEL = os.getenv("WIKI_ROW_LABEL", "")

# The event SOURCE that DEFINES the inbox/triage row. Unlike ansible/wiki (which match
# a Vikunja label/assignee), mail-tap events carry NO label; they are per-account new-mail sweeps, so
# the inbox row matches on the event's `source` namespace instead (see _source_signal_predicates). A
# config knob so the source string isn't hard-coded (same spirit as the *_ROW_LABEL knobs).
INBOX_ROW_SOURCE = os.getenv("INBOX_ROW_SOURCE", "")

# Base dir where per-account inbox MCP config files live (`<dir>/<account>.json`).
# EXPOSED so per-account google-workspace (and Hindsight) OAuth configs can be filled in out of band; this code
# NEVER authors those (secret bytes). Wired onto _INBOX_ROW.mcp_config_dir below; cc.build_argv reads it.
INBOX_MCP_CONFIG_DIR = os.getenv("INBOX_MCP_CONFIG_DIR", os.path.expanduser("~/.config/eph-broker/inbox-mcp"))

# The label that DEFINES the code change-proposer write row (same env-override pattern
# as the ansible/wiki rows). A Vikunja ticket carrying this label routes to the code-capable profile; its
# absence stays read-only (capability is label-DECLARED, never inferred). See _CODE_ROW below.
CODE_ROW_LABEL = os.getenv("CODE_ROW_LABEL", "eph:code-change")

# Assignee tier: the ASSIGNEE username that routes a Vikunja task to the code change-proposer
# row WITHOUT requiring the eph:code-change LABEL. Unlike the *_ROW_LABEL knobs (matched against the
# assignee+label UNION, substring), this is matched against the event's ASSIGNEES ONLY, by EXACT membership
# (see _assignee_signal_predicates). Env-overridable, same spirit as the label knobs. Routed as a FALLBACK
# tier AFTER the narrow-label rows (an explicit label still wins first-match) and BEFORE the general row.
DEV_ASSIGNEE = os.getenv("DEV_ASSIGNEE", "eph-code")

# The ASSIGNEE username that routes a Vikunja task to the INFRA stack-proposer
# row (MRs against the infra repo). Same assignee-ONLY, EXACT-membership matcher as DEV_ASSIGNEE (an "infra-eph" LABEL does not
# admit; the standing "infra" assignee never matches). Env-overridable.
INFRA_ASSIGNEE = os.getenv("INFRA_ASSIGNEE", "")
# Assignee INFRA_ASSIGNEE PLUS this exact LABEL gives the GENERALIST infra row (infra/general); the same
# assignee WITHOUT it stays on the stack-proposer row. Env-overridable.
INFRA_GENERAL_LABEL = os.getenv("INFRA_GENERAL_LABEL", "infra:general")

# Assignee tier (general mirror of DEV_ASSIGNEE): the ASSIGNEE username that routes a Vikunja
# task to the GENERAL worker row WITHOUT requiring the eph:general LABEL. Matched assignee-ONLY, by EXACT
# membership (_assignee_signal_predicates), exactly like DEV_ASSIGNEE; an "eph" LABEL does NOT match here.
# Env-overridable. Routed as a FALLBACK tier AFTER the narrow-label + @dev rows and BEFORE the general LABEL
# row (both point at the identical _GENERAL_ROW, so order between them is stamp-insensitive).
# The default assignee is "eph", which matches the general worker's own bus identity, so you assign @eph.
FLEET_ASSIGNEE = os.getenv("FLEET_ASSIGNEE", "eph")
# A SECOND fleet assignee, but NOT a general-worker alias: @fleet routes to the interactive TFA row
# (_TFA_ROW, domain "tfa"; app.ingest launches spawn.sh, a long-lived tmux agent), NOT the general
# worker. (It first aliased @eph; it was later split out as the TFA trigger. See the PROFILES
# entry below.) Same assignee-ONLY, EXACT-membership predicate as FLEET_ASSIGNEE; env-overridable.
FLEET_ASSIGNEE_ALT = os.getenv("FLEET_ASSIGNEE_ALT", "")

# General-purpose worker: the label that DEFINES the general row. Routed
# LAST in PROFILES so the narrow labels (ansible/wiki/code) win first; a ticket carries this ONLY when a human
# deliberately hands it to a broad-tool worker. capability is label-DECLARED, never inferred.
GENERAL_ROW_LABEL = os.getenv("GENERAL_ROW_LABEL", "eph:general")

# The worktree-target repos a general worker may cut from: an explicit ALLOWLIST so the worktree target is
# BOUNDED even though the toolset isn't. A ticket's declared target repo (or the row
# default) must be in this set or the spawn fails closed (general_repo_allowed()). Env-overridable
# (colon-separated) so an operator widens it without a code change. Set in site.env; unset means empty (admits nothing).
GENERAL_REPO_ALLOWLIST = set(filter(None, os.getenv("GENERAL_REPO_ALLOWLIST", "").split(":")))


def general_repo_allowed(repo_path):
    """True iff repo_path is in the general worker's worktree-target allowlist (fail-closed: unknown gives False)."""
    return repo_path in GENERAL_REPO_ALLOWLIST


# The worktree-target repos the CODE worker may cut from. Sibling of GENERAL_REPO_ALLOWLIST. The code
# path previously had NO override at all: every Vikunja ticket cut the hardcoded
# `CODE_REPO_PATH` default (one fixed repo), so a ticket about any other repo produced a worker that
# correctly refused the mismatch and never reached an MR (four occurrences). The
# declared-repo mechanism already existed but was wired ONLY for the general row; this mirrors it onto the
# code row so a `repo:<abs-path>` token works there too. Env-overridable, fail-closed on anything unlisted.
CODE_REPO_ALLOWLIST = set(filter(None, os.getenv("CODE_REPO_ALLOWLIST", "").split(":")))


def code_repo_allowed(repo_path):
    """True iff repo_path is in the code worker's worktree-target allowlist (fail-closed: unknown gives False).
    With REPO_INFER_ENABLED on, the allowlist ALSO admits any non-denied repo_catalog repo's local
    checkout / cache clone (catalog unavailable means legacy list only, fail-closed)."""
    if repo_path in CODE_REPO_ALLOWLIST:
        return True
    if repo_infer_enabled():
        import repo_catalog
        return repo_catalog.path_allowed(repo_path)
    return False


# Repo auto-infer. DEFAULT OFF: with both flags unset the code row behaves byte-identically to before
# (no `repo:` line gives a fail-closed reject). SHADOW=1 (flag still off) logs what the ladder WOULD pick while
# still requiring the token. Read at call time so a test (or a unit restart) sees the current env.
# `default` lets a flag default ON (e.g. comment_respawn_enabled below) while every existing caller
# (bare `_flag(name)`) keeps its OFF-by-default behaviour unchanged.
def _flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def repo_infer_enabled():
    return _flag("REPO_INFER_ENABLED")


def repo_infer_shadow():
    return _flag("REPO_INFER_SHADOW")


# A HUMAN comment on a ticket the ladder parked re-runs the ladder with the comment text.
# DEFAULT OFF; only acts with REPO_INFER_ENABLED also on (the rerun spawn goes through the same ladder).
def repo_infer_comment_rerun_enabled():
    return _flag("REPO_INFER_COMMENT_RERUN_ENABLED")


# Push with ONE namespace-wide token for any catalog repo that has no per-repo
# credential line. DEFAULT OFF: when off, repo_catalog.has_push_cred / ensure_clone / publish_mr are unchanged.
def repo_push_broad_cred_enabled():
    return _flag("REPO_PUSH_BROAD_CRED_ENABLED")


def repo_infer_rerun_users():
    """Vikunja usernames (besides the ticket's creator) whose comment may re-run a parked ladder (the
    operator). REPO_INFER_RERUN_USERS, comma-separated; "" means the ticket creator only."""
    return {u.strip().lower() for u in os.getenv("REPO_INFER_RERUN_USERS", "").split(",") if u.strip()}


# Generalizes the repo-parked-only comment rerun: a HUMAN comment on
# ANY OPEN ticket that routes by ASSIGNEE or an "eph:"-prefixed LABEL (see comment_respawn_domains), with
# NO live worker for its key, re-spawns the same row (app.Broker._comment_respawn). DEFAULT ON, behind
# its own env flag for rollback: set COMMENT_RESPAWN_ENABLED=0 to
# disable. Read at call time, same spirit as every other flag here.
def comment_respawn_enabled():
    return _flag("COMMENT_RESPAWN_ENABLED", "1")


def comment_respawn_domains():
    """The row DOMAINS eligible for comment-respawn: every row whose ticket routes by
    ASSIGNEE (DEV_ASSIGNEE maps to code, FLEET_ASSIGNEE to general) or by an "eph:"-prefixed LABEL
    (CODE_ROW_LABEL=eph:code-change maps to code, GENERAL_ROW_LABEL=eph:general to general). FLEET_ASSIGNEE_ALT
    ("fleet" maps to tfa) is EXCLUDED on purpose: a TFA is a long-lived interactive tmux agent launched OUTSIDE
    the coalescer (app.Broker._launch_tfa): it does not "spin down" the way a one-shot ephemeral worker
    does, there is no coalescer liveness to check for it, and comment-respawn has no meaning there. Derived
    from PROFILES so the env knobs (DEV_ASSIGNEE, FLEET_ASSIGNEE, CODE_ROW_LABEL, GENERAL_ROW_LABEL, ...)
    stay the single source of truth; a future "eph:"-prefixed label row is automatically included."""
    domains = set()
    for entry in PROFILES:
        signal, tmpl = entry[1], entry[2]
        factory = entry[3] if len(entry) > 3 else _label_signal_predicates
        if tmpl.domain == "tfa":
            continue
        if factory in (_assignee_signal_predicates, _assignee_label_signal_predicates,
                       _exact_label_signal_predicates) or (
                factory is _label_signal_predicates and str(signal).strip().lower().startswith("eph:")):
            domains.add(tmpl.row_key)
    return domains


def comment_respawn_domain(assignees, labels, rows=None):
    """Which comment-respawn-eligible domain (see comment_respawn_domains) a ticket with these FRESH
    assignees/labels routes to, or None if it matches none of them (including "no row at all"). `assignees`
    /`labels` are read off a ticket fetch taken AT RESPAWN time; the comment event itself carries neither
    (see app.Broker._comment_respawn). Reuses each row's own STRICT classify_pred against a synthetic evt,
    so the exact routing rule (assignee exact-membership / label substring) is defined ONCE in PROFILES,
    never duplicated here. `rows` is threadable for tests; default = the live PROFILES table."""
    eligible = comment_respawn_domains()
    evt = {"assignees": list(assignees or []), "labels": list(labels or [])}
    for _ns, _route_pred, classify_pred, tmpl in (rows if rows is not None else _profiles_to_rows(PROFILES)):
        if tmpl.row_key in eligible and classify_pred(evt):
            return tmpl.row_key
    return None


# The code worker's test runner is picked PER REPO, not per route. A route-level
# CODE_TEST_RUNNER=pytest ran system py3.9 pytest against every repo, so a Python repo with deps died on
# ModuleNotFoundError and shell/JS repos got the wrong runner. Value = a KEY into cc's reviewed runner menu
# (pytest/npm/go/venv/bash-n/ansible-syntax/none), never a raw command. An unmapped repo uses the route default (unchanged).
# `venv` = the repo's OWN pre-provisioned `<repo>/.venv` (provisioned out of band; the broker fails the spawn
# closed with "venv missing at <path>" if absent; it never falls back to system python).
# The site's seed map is CODE_REPO_RUNNER_SEEDS (site.env, same `<abs-path>=<key>,...` shape); env
# CODE_REPO_RUNNERS overrides/extends it per path. With both unset, every repo uses the route default.


def parse_repo_runner_map(raw):
    """Parse CODE_REPO_RUNNERS (`<abs-path>=<runner-key>,...`) into a dict. Same shape/rules as
    parse_issue_repo_map: blank/malformed entries skipped (and logged), only an ABSOLUTE path key
    honoured."""
    m = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        path, sep, key = entry.partition("=")
        path, key = path.strip(), key.strip()
        if sep and key and path.startswith("/"):
            m[path] = key
        else:
            log.warning("CODE_REPO_RUNNERS: skipping malformed entry %r", entry)
    return m


_CODE_REPO_RUNNER_SEEDS = parse_repo_runner_map(os.getenv("CODE_REPO_RUNNER_SEEDS", ""))
CODE_REPO_RUNNERS = {**_CODE_REPO_RUNNER_SEEDS, **parse_repo_runner_map(os.getenv("CODE_REPO_RUNNERS", ""))}


# Repo markers that make an UNMAPPED repo pick its runner by itself. A Python marker wins (so a Python
# repo that also ships a package.json keeps today's resolution, unchanged); a JS/TS repo with only
# package.json gets the reviewed `npm` runner (npm ci + npm test). Detection yields a menu KEY, never a command.
_PYTHON_MARKERS = ("pyproject.toml", "pytest.ini", "setup.py", "setup.cfg", "tox.ini")


def detect_test_runner(repo_path):
    """Runner KEY implied by repo_path's top-level marker files, or None (no opinion: existing logic)."""
    if not repo_path:
        return None
    if any(os.path.isfile(os.path.join(repo_path, m)) for m in _PYTHON_MARKERS):
        return None
    if os.path.isfile(os.path.join(repo_path, "package.json")):
        return "npm"
    return None


def resolve_test_runner(repo_path, default, runner_map=None):
    """The runner KEY for a code worker cutting from repo_path: the per-repo map entry, else `none` for
    a repo admitted only by repo inference, else a marker-detected runner for a CODE_REPO_ALLOWLIST
    repo (package.json-only gives npm), else the route `default` (the original behaviour). runner_map threadable
    for tests."""
    runner_map = CODE_REPO_RUNNERS if runner_map is None else runner_map
    if repo_path in runner_map:
        return runner_map[repo_path]
    # A catalog repo nobody has mapped gets NO test Bash (`none`), never the route default; the default
    # (pytest) was tuned for the legacy allowlist, not for an arbitrary cloned repo. This floor runs BEFORE
    # marker detection, so an inferred-only repo with a package.json still gets `none`, never npm.
    if repo_infer_enabled() and repo_path not in CODE_REPO_ALLOWLIST:
        return "none"
    # Auto-detection (npm ci runs registry installs + install scripts) is for allowlisted repos ONLY.
    if repo_path in CODE_REPO_ALLOWLIST:
        detected = detect_test_runner(repo_path)
        if detected:
            return detected
    return default


def code_venv_python(repo_path):
    """The interpreter the `venv` runner uses: <repo>/.venv/bin/python in the BASE repo, not the worktree
    (the venv is gitignored, so a fresh worktree has none). `python -m pytest` run from the worktree cwd puts
    the worktree first on sys.path, so the worker's edits are what gets tested."""
    return os.path.join(repo_path, ".venv", "bin", "python")


# Per-ticket target-repo resolution. A ticket may declare which repo its worktree is cut
# from via a `repo:<absolute-path>` token line in its brief/description, instead of always using the route
# DEFAULT repo. Kept deliberately minimal: a one-line CONVENTION, not a config system.
def parse_repo_token(text):
    """Pure parser: pull the first `repo:<absolute-path>` declaration out of a ticket's rendered text.
    Returns the declared ABSOLUTE path (str) or None if no well-formed token is present. Only an absolute
    path (leading `/`) is honoured; a relative/garbage value is treated as "nothing declared" (None), so a
    bad token can never widen the target; the allowlist gate in resolve_target_repo is the fail-closed line.

    Tolerant of HTML-rendered ticket text: Vikunja's `ticket_fetch` returns the description verbatim as HTML,
    so the token arrives wrapped (`<p>repo:/abs/path</p>`) and possibly with logical lines joined by `<br>`.
    Sibling block elements are concatenated with NO newline between them (`</p><p>` on one physical line,
    as seen on a real ticket), so we split each physical line on `<br...>` AND on OPENING+closing block tags
    (`<p>`/`</p>`/`<div>`/`</div>`/`<li>`/`</li>`, with or without attrs) before stripping tags. In practice,
    the real `ticket_fetch` blob puts the token AFTER the field label on
    the SAME physical line: `**description:** <p>repo:/abs/path</p><p>next…</p>`, so we MUST also break on
    the OPENING `<p>` to separate the `**description:** ` label from the token; splitting on closing tags alone
    left the first fragment as `**description:** repo:/…` (label prefix defeats the anchor, so no match). We then
    strip tags, unescape entities, and normalise non-breaking spaces. Still strictly BARE-LINE anchored (cleaned
    fragment must START with `repo:`); a mid-sentence "…the repo: foo…" mention never routes a worker (an
    opening `<p>the repo:/x</p>` yields the fragment `the repo:/x`, which does NOT start with `repo:`). FIRST
    match wins."""
    for val in _repo_token_values(text):
        if val.startswith("/"):
            return val
    return None


def parse_repo_ref(text):
    """Like parse_repo_token (same bare-line anchoring + HTML tolerance) but ALSO accepts a catalog
    reference (`repo: owner/eph-broker` or `repo: eph-broker`) for the inference ladder's explicit step.
    An absolute path is returned exactly as parse_repo_token would; anything else is its first word with
    wrapping backticks/quotes/trailing punctuation stripped. FIRST well-formed token wins. Used ONLY when
    REPO_INFER_ENABLED is on."""
    for val in _repo_token_values(text):
        if val.startswith("/"):
            return val
        word = re.sub(r"^[`'\"(]+|[`'\".,;:)]+$", "", (val.split() or [""])[0])
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", word):
            return word
    return None


_DESCRIPTION_LABEL_RE = re.compile(r'(?i)^\*\*description:\*\*\s*')


def _repo_token_values(text):
    """Yield the value of every bare-line `repo:` token in ticket text (see parse_repo_token), in order."""
    if not text:
        return
    for raw in str(text).splitlines():
        # A physical line can hold multiple logical lines AND a leading field label: joined by <br> (with/without
        # attrs), concatenated sibling blocks with no newline (</p><p>, </div>, </li>), or a `**label:** <p>token`
        # prefix. Split on OPENING and closing block tags + <br> so the label and each block separate cleanly.
        for frag in re.split(r'(?i)<br[^>]*>|</?p[^>]*>|</?div[^>]*>|</?li[^>]*>', raw):
            # Strip tags, unescape entities, normalise NBSP to space, then anchor exactly as the plain-text path.
            clean = html.unescape(re.sub(r'<[^>]+>', '', frag)).replace('\xa0', ' ').strip()
            # A PLAIN-TEXT description (API-created ticket, no <p>) renders as `**description:** repo:/abs`
            # on one line with no tag to split on, so strip that one leading field label, then anchor as usual
            # (still strictly start-anchored: `**description:** the repo:/x` does NOT route).
            clean = _DESCRIPTION_LABEL_RE.sub('', clean, count=1)
            if clean.lower().startswith("repo:"):
                yield clean[len("repo:"):].strip()


def resolve_infra_repo(ticket_text, default, allowed_fn):
    """The worktree-target repo for an INFRA row ticket. Unlike the code row, a missing `repo:` token is
    NOT a reject: the infra row has one natural target (its default, the infra repo), so nothing declared gives `default`.
    A declared token still wins IFF allowlisted. FAIL-CLOSED either way: the resolved repo (declared OR default)
    must pass `allowed_fn` or this RAISES ValueError (the caller cuts NO worktree). No inference ladder."""
    target = resolve_target_repo(parse_repo_token(ticket_text), default, allowed_fn)
    if not allowed_fn(target):
        raise ValueError(f"infra target repo {target!r} is not in the allowlist (fail-closed)")
    return target


def resolve_target_repo(declared, default, allowed_fn):
    """Resolve a worker's worktree-target repo. `declared` = a repo path a ticket declared via parse_repo_token
    (or None/empty if none). FAIL-CLOSED: a DECLARED repo MUST pass `allowed_fn` or this RAISES ValueError
    (the caller then spawns NO worker). With nothing declared, returns `default` (the row default; the caller
    still allowlist-checks that default itself, preserving the pre-existing fail-closed behaviour)."""
    if declared:
        if allowed_fn(declared):
            return declared
        raise ValueError(f"declared target repo {declared!r} is not in the allowlist (fail-closed)")
    return default


# GitHub/GitLab issue to code change-proposer: an external issue names its repo by an EXTERNAL
# identifier (github:owner/name or gitlab:group/project); the code worker needs a LOCAL clone path. This is
# the per-issue repo ALLOWLIST and resolver (repo allowlist, fail-closed on unknown).
# The map is env-overridable and DEFAULTS EMPTY, so EVERY issue fails closed until an operator populates it.
def parse_issue_repo_map(raw):
    """Parse the comma-separated ISSUE_REPO_MAP env into a dict. Each entry is
    `<source>:<repo_ident>=<local-abs-path>` (e.g. `github:owner/name=/srv/git/name`) gives
    {"github:owner/name": "/srv/git/name"}. Blank/malformed entries (no '=' or an empty side) are skipped;
    only an ABSOLUTE local path (leading '/') is honoured, so a bad/relative value can never widen the
    target; an unmapped or ill-formed repo simply fails closed in resolve_issue_repo. Pure function."""
    m = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        ident, path = entry.split("=", 1)
        ident, path = ident.strip(), path.strip()
        if ident and path.startswith("/"):
            m[ident] = path
    return m


# Parsed once at module load (env-overridable). EMPTY by default, so every issue fails closed until an operator
# populates ISSUE_REPO_MAP; the deployer must set it. Format is comma-separated
# `github:owner/name=/srv/git/name,gitlab:group/project=/srv/git/project` entries.
ISSUE_REPO_MAP = parse_issue_repo_map(os.getenv("ISSUE_REPO_MAP", ""))

# Who may start work by opening a GitHub/GitLab issue: comma-separated usernames. EMPTY (the default) admits
# NOBODY, so issue intake stays off until an operator lists the trusted authors. On a public project anyone can
# open an issue, and its body becomes the worker's task, so an open default is not acceptable. Checked in
# Router.classify for every forge source, which covers /webhooks/gitlab and a forge-shaped body on /event alike.
# GitHub and GitLab usernames are case-insensitive, so the list is compared lowercased.
ISSUE_AUTHOR_ALLOWLIST = set(filter(None, (x.strip().lower()
                                           for x in os.getenv("ISSUE_AUTHOR_ALLOWLIST", "").split(","))))


def issue_author_allowed(username, allowlist=None):
    """True iff `username` is in the allowlist (default: ISSUE_AUTHOR_ALLOWLIST), compared lowercased.
    FAIL-CLOSED: an empty allowlist, a missing username or a non-string one gives False. Pure function
    (allowlist threadable for tests)."""
    allow = ISSUE_AUTHOR_ALLOWLIST if allowlist is None else allowlist
    if not isinstance(username, str) or not username:
        return False
    return username.lower() in {str(a).lower() for a in allow}


def resolve_issue_repo(source, repo_ident, repo_map=None):
    """Resolve an external issue's (source, repo identifier) to a LOCAL clone path. Builds the map KEY as
    `<source>:<repo_ident>` (e.g. "github:owner/name") and looks it up in `repo_map` (default: the module
    ISSUE_REPO_MAP parsed from env). FAIL-CLOSED: an unmapped repo RAISES ValueError and the caller cuts NO
    worker and alerts the operator (same fail-closed shape as resolve_target_repo's declared-repo gate and the inbox
    unknown-account reject). An empty map (the default) fails everything closed until an operator populates
    ISSUE_REPO_MAP. Pure function of its inputs (repo_map is threadable for tests)."""
    repo_map = ISSUE_REPO_MAP if repo_map is None else repo_map
    ident = f"{str(source).lower()}:{repo_ident}"
    path = repo_map.get(ident)
    if not path:
        raise ValueError(f"issue repo {ident!r} is not in ISSUE_REPO_MAP (fail-closed; ops must map it)")
    return path

# Echo-guard: event names that must NEVER trigger ingest admission, regardless of what
# allowlist signal (assignee/label) they happen to carry. Checked FIRST in Router.classify(), ahead of
# the per-row allowlist below.
#
# `task.comment.created` is INTENTIONALLY non-triggering. Phase-1 read-report workers are ONE-SHOT: they
# spawn on the assignee/label signal (`task.created`/`task.updated`), then read, report and exit. They never
# need a comment to TRIGGER a spawn, nor to feed a mid-run --resume forward. Comments are the domain of the
# separate @mention consumer, not this broker.
#
# The incident this closes was a self-loop: the broker posts its worker's report as a Vikunja
# comment via `mcp.sh vikunja-bridge` (poster.py), authored as doer `svc-agent`. That post emits a
# `task.comment.created` webhook which RE-ENTERED this broker's ingest, was admitted (the ticket still
# carried the `ansible` label), and got forwarded into the still-live worker, which posted again,
# in an unbounded ~15s loop. Two independent gaps let it through: (1) `is_fleet_bot()` (echo_guard.py) only
# matches `-bot`/`@<bus-domain>` doers, so it does not recognize the shared `svc-agent` service-account doer;
# (2) the raw `task.comment.created` ingest payload carries NO comment body, run_id marker, or comment_id
# (confirmed live from events.jsonl), so a run_id-marker ingest gate has nothing to match against even if
# one existed. Dropping the whole event class at ingest kills the loop at the root without needing either.
#
# HARD PREREQ: before any future phase re-admits `task.comment.created` as a
# comment-driven-forward trigger, (a) Path-A n8n tap-enrichment must add the comment BODY + a run_id marker
# + comment_id to the ingest payload, AND (b) ingest must gain a run_id-marker gate that uses that added data.
#
# That prerequisite is SATISFIED; comments are now FORWARD-ONLY, never triggering. (a) the tap now sends task_id
# (DB lookup, may be absent), comment (HTML body), comment_id, run_id (parsed from `<!-- broker-run:UUID -->`)
# and doer; (b) comment_forward_admissible() below is that gate. The event class is STILL non-triggering: a
# comment NEVER spawns a worker (not here, not via coalescer DEDUP-CLEAR, not via a close-time re-arm). It is
# admitted ONLY as a forward into a worker that is LIVE on that subject right now, and only when ALL hold:
#   1. task_id present (key is not `vikunja:task:?`);
#   2. provenance.classify(doer) == "user" (Phase-1 humans only: svc-agent/agent bots/empty doer drop;
#      closes gap (1) above; echo_guard also drops agent doers first);
#   3. no run_id stamp and no `broker-run:` marker in the body (the broker's own posts ALWAYS carry one;
#      closes gap (2); echo_guard separately drops an ACTIVE run_id);
#   4. the subject has a live worker (coalescer open_idle/open_running) AND a route stamp.
# Anything else drops exactly as before. Belt-and-braces downstream: Coalescer.on_event drops a comment if
# the worker vanished between classify and on_event (race), and rearm_pending never re-arms a comment left
# in fwd_buffer at close (it stays on the ticket). Only the event NAME set is shared; the gate is below.
NON_TRIGGERING_EVENTS = {"task.comment.created"}
_BROKER_RUN_MARKER_RE = re.compile(r"broker-run:", re.IGNORECASE)


def comment_forward_admissible(key, evt, is_live):
    """May this `task.comment.created` be FORWARDED into the live worker on `key`? Fail-closed
    (see the block above). `is_live` = key -> bool (Coalescer.has_live_worker); None means never admit."""
    if is_live is None or evt.get("task_id") is None or key.endswith(":?"):
        return False
    if provenance.classify(evt.get("doer")) != "user":
        return False
    if evt.get("run_id") or _BROKER_RUN_MARKER_RE.search(str(evt.get("comment") or "")):
        return False
    return bool(is_live(key))


# The native model a worker gets when FLEET_MODEL_SH is unset or fails, and the `--model` a backend-named answer
# falls back to. Must be a full claude-* id (with_model reads any other answer as a backend name).
_NATIVE_FALLBACK_MODEL = os.getenv("NATIVE_FALLBACK_MODEL", "claude-sonnet-5")
if not _NATIVE_FALLBACK_MODEL.startswith("claude-"):
    raise ValueError(f"NATIVE_FALLBACK_MODEL={_NATIVE_FALLBACK_MODEL!r} must be a claude-* model id")


def resolve_model(agent):
    """Ask fleet-model.sh for the model this agent should launch with (mode-aware). Never hard-code."""
    if not FLEET_MODEL_SH:
        return _NATIVE_FALLBACK_MODEL
    try:
        out = subprocess.run(["bash", FLEET_MODEL_SH, agent], capture_output=True, text=True, timeout=15)
        model = (out.stdout or "").strip()
        if model:
            return model
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("fleet-model.sh %s failed (%s) -> falling back to %s", agent, e, _NATIVE_FALLBACK_MODEL)
    return _NATIVE_FALLBACK_MODEL   # fail-safe cheap, same spirit as the launcher's default


# The worker harnesses a Route.adapter may name (adapters/cc.py, adapters/hermes.py). Default "cc".
ADAPTERS = ("cc", "hermes")


def _is_registry_backend(name):
    """True iff `name` is a spawn-registry backend NAME (adapters/cc._backend_entry, the same lookup
    load_backend uses) despite starting with "claude-" (e.g. "claude-api"), so with_model() treats a
    fleet-model.sh answer like that as a backend, not a `--model` id. The "claude" sentinel is native, never a
    backend here. Lazy import: adapters.cc imports this module at load. Unreadable registry / no cc gives False
    (the answer stays a model id, the existing behaviour)."""
    if not name or name == "claude":
        return False
    try:
        from adapters import cc
        return cc._backend_entry(name) is not None
    except Exception:                      # noqa: BLE001; fail-safe: never break routing on a registry problem
        return False


@dataclass
class Route:
    domain: str
    repo: str                       # base repo the per-subject worktree forks off (charter-free cwd)
    prompt_class: str               # prompts/<domain>/<prompt_class>.md, sha256-gated
    agent: str                      # fleet-model.sh agent name, used to pick the model
    read_only: bool = True          # Phase-1 ansible proving worker = read/report-only FIRST
    env: dict = field(default_factory=dict)   # e.g. {"ZBUS_ENV": "..."}; a reference, not a secret
    model: str = ""                 # filled by resolve_model at route() time
    # capability is DECLARED on the row, never inferred from the prompt/domain.
    # adapters/cc.py::build_argv DERIVES the worker's tool grants from this field (--permission-mode
    # plus allow/deny tools). "read" or unknown means read-only (fail-closed); "write" means scoped
    # file-edit + git-commit (see cc.capability_tool_grant). Default "read" keeps every
    # existing row read-only and byte-identical in behaviour.
    capability: str = "read"
    # publish: seam config for the wiki write worker. This file and cc.build_argv enforce WHAT a
    # write worker may do; the git plumbing (cutting the worktree, suppressing the
    # wiki post-commit hook, and doing the ONLY push, cred-side) lives elsewhere. These are the config FIELDS this file
    # EXPOSES for that plumbing to FILL (NOT hardcoded paths). Empty on a read row. See _WIKI_DOCS_ROW for
    # the wiki values and the seam note.
    publish: dict = field(default_factory=dict)
    # Base dir where per-account MCP config files live (`<dir>/<account>.json`). ONLY
    # the inbox row uses it (see _INBOX_ROW + the inbox seam note): cc.build_argv turns it into a
    # `--mcp-config <dir>/<account>.json --strict-mcp-config` at spawn so an inbox worker loads ONLY that
    # ONE account's Gmail(+Hindsight) MCP and nothing from the broker host's ambient config. The per-account files are authored
    # out of band (they carry secret OAuth); this code only EXPOSES the base dir and fails closed
    # if the file is absent. Empty on every non-inbox row (default INBOX_MCP_CONFIG_DIR is only wired onto
    # _INBOX_ROW), so ansible/wiki rows are unaffected.
    mcp_config_dir: str = ""
    # A code route's per-repo test runner: a KEY into cc._CODE_TEST_RUNNERS (e.g.
    # "pytest"), NEVER a raw command. cc.build_argv passes it to capability_tool_grant, which adds ONLY that
    # runner's code-reviewed test-Bash to a capability=="code" grant. Empty/unknown means no test Bash
    # (fail-closed). Empty on every non-code row.
    test_runner: str = ""
    # General worker: the --setting-sources value for this row. Default ""
    # keeps every existing worker CHARTER-BLIND (no project/user CLAUDE.md). The general row sets "user"
    # to DELIBERATELY load ~/.claude/CLAUDE.md (the user-level rules) and ~/.claude/settings.json (the autoMode
    # classifier config). cc.build_argv reads it. Empty on every non-general row.
    setting_sources: str = ""
    # General worker: a SINGLE fixed non-secret Hindsight-ONLY --mcp-config path (not per-account like
    # mcp_config_dir). cc.build_argv fails closed if it is absent, so a general worker (which has NO
    # --allowedTools) can never fall back to the ambient account-connector tool surface (security
    # review). The file is placed at deploy time; the repo only points at it and gates on it.
    # Empty on every non-general row.
    mcp_config_path: str = ""
    # Run THIS row's worker on a NON-default inference backend (e.g. "deepseek"
    # / "glm" via a LiteLLM proxy to OpenRouter, 1M ctx) so the ephemeral fleet grinds on non-Anthropic budget.
    # Empty = default Anthropic cloud (every existing row unchanged). The backend's full launch recipe
    # (model + base_url + ctx + small-model routes + effort + auth-env file) is resolved AT SPAWN from the
    # shared registry /etc/site/spawn-models.json (adapters/cc.load_backend), one source of truth with
    # spawn.sh. The secret auth token is sourced by the broker (a local process) at spawn, by path.
    backend: str = ""
    # Let FLEET_MODE pick this row's backend. When True AND `backend` is empty, a
    # fleet-model.sh answer that is NOT a `claude-*` model id (e.g. "deepseek") is taken as a BACKEND NAME
    # and resolved at spawn through the same load_backend/spawn-models path an explicit backend uses (same
    # fail-closed-and-alert on an unresolvable name). Mode-name-agnostic: the broker never knows WHICH
    # mode produced the answer, only whether it is a claude model. An explicit `backend` (the row's own
    # `<ROW>_BACKEND` pin, e.g. GENERAL_BACKEND) always wins. EVERY worker row
    # sets this True; resolve_model(self.agent) is already asked on every spawn, so this just lets a
    # non-claude fleet-model.sh answer select a backend fleet-wide, not only on the general row. The TFA row
    # (_TFA_ROW) is the ONE exception: its model comes from spawn.sh's own registry (agent=""), not
    # resolve_model, so it stays False.
    backend_from_mode: bool = False
    # The HARNESS this row's worker runs on: "cc" (Claude Code, every existing row) or
    # "hermes" (adapters/hermes.py: hermes-agent one-shot, same envelope/caps/sinks). Validated at LOAD
    # (__post_init__): an unknown value raises, so a typo'd row never silently runs on the wrong harness.
    adapter: str = "cc"
    # True when `backend` came from the row's explicit `<ROW>_BACKEND` pin (set by with_model), not from
    # FLEET_MODE. A per-ticket `model:<name>` label never overrides a pin (apply_model_label).
    backend_pinned: bool = False
    # The row's ROUTING identity (the ingest stamp, classify()'s return, the dispatch/respawn sets) when
    # two rows share a `domain` (prompts/<domain>/ ... ), such as infra/stack-proposer vs infra/general. Empty means
    # the identity is `domain` (every other row, byte-unchanged). Read it via `row_key`, never directly.
    row_id: str = ""
    # A FIXED native model for this row in EVERY fleet mode (e.g. "opus"). When set, with_model() skips
    # resolve_model/fleet-model.sh and FLEET_MODE's backend pick, and a `model:` label never overrides it (the
    # route reports backend_pinned). An explicit `backend` still rides through. Empty (every other row) = unchanged.
    model_pin: str = ""
    # routes.json: True on a row loaded from the site's config/routes.json (load_config_routes), False on
    # every built-in row. cc._run_worker binds a config row to its own `repo`: a "read" row's worktree is cut
    # from it (not the broker-wide ANSIBLE_REPO), and a code/general row skips the ticket `repo:` / repo-infer
    # resolution (a differing `repo:` line is refused).
    from_config: bool = False
    # A site capability (broker.yaml `capabilities:`, see load_site_capabilities) this row runs. `capability` is
    # then the site capability's built-in base, so every branch keyed on capability behaves as for the base;
    # cc.build_argv adds the site capability's extra allow/deny rules on top of the base grant. Empty = none.
    site_capability: str = ""

    @property
    def row_key(self):
        return self.row_id or self.domain

    def __post_init__(self):
        if self.adapter not in ADAPTERS:
            raise ValueError(f"Route {self.domain!r}: unknown adapter {self.adapter!r} "
                             f"(known: {', '.join(ADAPTERS)}) - refusing (fail-closed)")

    def with_model(self):
        if self.model_pin:                   # pinned model, no mode lookup
            model, backend = self.model_pin, self.backend
        else:
            model, backend = resolve_model(self.agent), self.backend
        if self.backend_from_mode and not self.model_pin and (
                not model.startswith("claude-") or _is_registry_backend(model)):
            if not backend:
                backend = model              # mode-driven backend name, resolved at spawn
            # never hand a backend NAME to `--model`: a resolved backend supplies its own registry model
            # (cc._one_turn), and an explicit native override (GENERAL_BACKEND=claude, or the fallback's
            # native rung) needs a real claude id, so use the same fail-safe cheap default resolve_model uses.
            model = _NATIVE_FALLBACK_MODEL
        return Route(self.domain, self.repo, self.prompt_class, self.agent,
                     self.read_only, dict(self.env), model, self.capability,
                     dict(self.publish), self.mcp_config_dir, self.test_runner, self.setting_sources,
                     self.mcp_config_path, backend, self.backend_from_mode, self.adapter,
                     bool(self.backend) or bool(self.model_pin), self.row_id, self.model_pin,
                     self.from_config, self.site_capability)


# Per-ticket model selection via a `model:<name>` label. The name is either a native
# Claude alias (passed to `--model`, native Anthropic backend) or a spawn-registry backend name resolved through
# the SAME cc.load_backend / spawn-models.json path a <ROW>_BACKEND pin uses. Decisions:
#   * precedence: an explicit <ROW>_BACKEND pin WINS (operator policy beats a ticket); the label beats FLEET_MODE.
#   * unknown name / two different model: labels raise ValueError (fail-closed: the spawn is refused and the operator alerted).
#   * a NON-Anthropic name is honoured only when the ticket's creator is in MODEL_LABEL_TRUSTED_CREATORS
#     (the same-uid residual, docs/security-residual-same-uid.md): a guest-created ticket cannot pick a
#     bypass-classifier backend. Native aliases are allowed for any creator. The code row's own
#     CODE_NONCLAUDE_OK gate (cc._one_turn) still applies on top.
MODEL_LABEL_PREFIX = "model:"
NATIVE_MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku"})


def model_label_trusted_creators():
    """Ticket creators (Vikunja usernames) whose `model:` label may pick a NON-Anthropic backend.
    MODEL_LABEL_TRUSTED_CREATORS, comma-separated (site.env); unset means nobody (fail closed). Read at call time."""
    return {u.strip().lower() for u in os.getenv("MODEL_LABEL_TRUSTED_CREATORS", "").split(",")
            if u.strip()}


# Friendly aliases for a model label, and BARE labels accepted as model names (Vikunja
# quick-add `*haiku` == `*model:haiku`). A bare label is matched whole-label, case-insensitively, against this
# fixed allowlist only; any other label is not a model label. An alias is resolved in apply_model_label, BEFORE
# the native/backend branches, so it still goes through the backend_resolvable + trusted-creator checks.
MODEL_LABEL_ALIASES = {"local": "vLLM-Main"}
BARE_MODEL_LABELS = frozenset({"opus", "sonnet", "haiku", "deepseek", "local"})


def _model_key(name):
    """Dedupe key: native names and aliases compare case-insensitively (model:OPUS == model:opus; local ==
    vLLM-Main). Any other name is a case-sensitive registry backend, so it keeps its exact spelling."""
    low = name.lower()
    if low in NATIVE_MODEL_ALIASES:
        return low
    return MODEL_LABEL_ALIASES.get(low, name)


def _model_label_hits(labels, trusted=True):
    """[(name, bare)] for the model labels on a ticket: `model:<name>` (case-insensitive prefix) or a bare
    BARE_MODEL_LABELS label. Duplicates collapse (see _model_key); the first spelling wins. A bare NON-Anthropic
    label on an UNTRUSTED ticket is IGNORED (log.info), dropped before the conflict check, so an ordinary label
    never refuses a spawn. Two DIFFERENT models raise ValueError (ambiguous, fail-closed). Pure."""
    if isinstance(labels, str):
        labels = [labels]
    hits = []
    for lab in labels or []:
        s = str(lab).strip()
        if s.lower().startswith(MODEL_LABEL_PREFIX):
            name, bare = s[len(MODEL_LABEL_PREFIX):].strip(), False
            if not name:
                raise ValueError(f"empty model label {s!r} (fail-closed)")
        elif s.lower() in BARE_MODEL_LABELS:
            name, bare = s, True
            if not trusted and name.lower() not in NATIVE_MODEL_ALIASES:
                log.info("bare model label %r ignored: ticket creator is not trusted for a non-Anthropic model", s)
                continue
        else:
            continue
        if _model_key(name) not in [_model_key(n) for n, _ in hits]:
            hits.append((name, bare))
    if len(hits) > 1:
        raise ValueError(f"conflicting model labels {[n for n, _ in hits]!r} (fail-closed)")
    return hits


def parse_model_label(labels):
    """The model name from a ticket's `model:<name>` label or bare model label (see _model_label_hits), or None
    if there is none. Case-insensitive prefix; the name keeps its case (registry names like `vLLM-Main` are
    case-sensitive). Two DIFFERENT model labels raise ValueError (ambiguous, fail-closed). Pure."""
    hits = _model_label_hits(labels)
    return hits[0][0] if hits else None


def apply_model_label(route, labels, creator, backend_resolvable):
    """Return `route` with a ticket's model label applied (see the model-label blocks above). `route` is a
    with_model() result; `backend_resolvable(name) -> bool` checks the spawn registry (cc.load_backend).
    No label, or a pinned row, returns `route` unchanged. Raises ValueError on an unknown name or a non-Anthropic
    name on a ticket whose creator is not trusted; a bare non-Anthropic label on an untrusted ticket is ignored."""
    trusted = str(creator or "").strip().lower() in model_label_trusted_creators()
    hits = _model_label_hits(labels, trusted)
    if not hits:
        return route
    name = hits[0][0]
    if route.backend_pinned:
        log.info("model label %r ignored: row %s backend is pinned to %r", name, route.domain, route.backend)
        return route
    if name.lower() in MODEL_LABEL_ALIASES:      # `local` maps to vLLM-Main, before any branch below
        name = MODEL_LABEL_ALIASES[name.lower()]
    low = name.lower()
    if low in NATIVE_MODEL_ALIASES:
        backend, model = "claude", low
    elif low == "claude":
        backend, model = "claude", route.model
    else:
        if not backend_resolvable(name):
            raise ValueError(f"model label {name!r} is not a known model or spawn-registry backend (fail-closed)")
        if not trusted:
            raise ValueError(f"model label {name!r} picks a non-Anthropic backend, but ticket creator "
                             f"{creator!r} is not in MODEL_LABEL_TRUSTED_CREATORS (fail-closed)")
        backend, model = name, _NATIVE_FALLBACK_MODEL
    out = Route(**{**route.__dict__, "env": dict(route.env), "publish": dict(route.publish)})
    out.backend, out.model = backend, model
    return out


# A bare `hermes` ticket label flips ONE ticket's worker onto the hermes harness
# (adapters/hermes.py). Same trust model as the model label: honoured only for MODEL_LABEL_TRUSTED_CREATORS
# (ignored + logged otherwise), never on a pinned row. The label may only flip a capability listed in
# HERMES_LABEL_CAPABILITIES; anything else is a fail-closed spawn REJECT (never a silent cc fallback).
# ACTIVATION: "general" only, and only because the general row's deny-first gate
# (eph-gate floors, then unmapped DENY, then the policy judge, then deny-and-report) is merged
# AND verified live. Never widen without the same: a code lane needs a reviewed CODE_NONCLAUDE_OK
# posture (separate proposal). A code constant, not env, so widening it is always a reviewed diff.
HERMES_LABEL = "hermes"
HERMES_LABEL_CAPABILITIES = frozenset({"general"})
# Hermes workers never get an Anthropic route (no API-credit spend). A native/empty or
# claude-* backend is a fail-closed REJECT (it used to map to the retired claude-api registry entries); the
# ticket must choose a non-Anthropic registry backend with a model label (e.g. `model:deepseek`).


def apply_adapter_label(route, labels, creator, capabilities=None):
    """Return `route` with a ticket's `hermes` label applied (see the block above). Run AFTER
    apply_model_label (its backend choice is kept when it is a registry backend). No label, untrusted creator,
    pinned row, or a row already on hermes, returns `route` unchanged. Raises ValueError (fail-closed) when the row's
    capability is not in `capabilities` (default HERMES_LABEL_CAPABILITIES). Pure; never mutates `route`."""
    if isinstance(labels, str):
        labels = [labels]
    if not any(str(lab).strip().lower() == HERMES_LABEL for lab in labels or []):
        return route
    if str(creator or "").strip().lower() not in model_label_trusted_creators():
        log.info("hermes label ignored: ticket creator %r is not in MODEL_LABEL_TRUSTED_CREATORS", creator)
        return route
    if route.adapter == "hermes":
        return route
    if route.backend_pinned:
        log.info("hermes label ignored: row %s backend is pinned to %r", route.domain, route.backend)
        return route
    allowed = HERMES_LABEL_CAPABILITIES if capabilities is None else capabilities
    cap = getattr(route, "capability", "read")
    if getattr(route, "site_capability", ""):   # hermes has no allow/deny lists to add the site rules to
        raise ValueError(f"hermes label: site capability {route.site_capability!r} runs on the cc harness only "
                         "(fail-closed)")
    if cap not in allowed:
        raise ValueError(f"hermes label: capability {cap!r} is not enabled for the hermes harness "
                         f"(enabled: {sorted(allowed) or 'none'}) (fail-closed)")
    backend = route.backend
    if not backend or backend == "claude" or backend.startswith("claude-"):
        raise ValueError(f"hermes label: backend {backend or 'native'!r} is Anthropic - hermes workers never run on "
                         f"Anthropic API credits; add a non-Anthropic model label, e.g. model:deepseek (fail-closed)")
    out = Route(**{**route.__dict__, "env": dict(route.env), "publish": dict(route.publish)})
    out.adapter, out.backend = "hermes", backend
    return out


# --- Declarative N-profile routing table --------------------------------------------------
# A row = (namespace, route_pred, classify_pred, Route-template). First match wins. Phase-1 hand-wrote
# ONE row with two bespoke predicates (`_assigned_or_labelled_ansible` / `_has_ansible_signal`); this table
# generalizes that into PROFILES, a plain declarative list of (namespace, trigger_label, Route), plus a
# single matcher FACTORY (`_label_signal_predicates`, below) that derives the (lenient, strict) predicate
# pair for ANY label. Dropping in a second profile is therefore exactly one more PROFILES entry, with no new
# hand-rolled predicate function required. `Router.__init__` builds its default `rows` from PROFILES via
# `_profiles_to_rows`.
_ANSIBLE_ROW = Route(
    domain="ansible",
    repo=os.getenv("ANSIBLE_REPO", ""),
    prompt_class="read-report",     # read/report-only proving class; CONTENT is operator-gated
    agent="ansible",                # resolved via fleet-model.sh ansible
    read_only=True,
    # zbus reads $ZBUS_ENV as a LITERAL path to the per-handle bot-env file (it does NOT resolve a bare
    # slug); a bare slug silently fails, so the worker can't escalate. Default must be the absolute path.
    env=_zbus_env("ANSIBLE_ZBUS_ENV"),
    # Per-row inference backend override, SAME pattern/resolution path as GENERAL_BACKEND (cc.load_backend
    # / spawn-models registry, cc._one_turn). Empty (default) = native Claude, UNCHANGED.
    backend=os.getenv("ANSIBLE_BACKEND", ""),
    backend_from_mode=True,             # every row follows FLEET_MODE; a pin wins
)

# The FIRST write-capable profile: wiki-docs. Routes a `wiki`-labelled ticket to the
# wiki-docs worker class (domain=wiki, prompt_class=docs, which maps to
# prompts/wiki/docs.md, worker manifest workers/wiki/docs.json). capability="write" is what gives
# build_argv teeth: this worker is granted acceptEdits (auto-approve edits within its cwd) + git add/commit
# (see cc.capability_tool_grant); NO git push (the worker is cred-free; the BROKER does the ONLY
# push, cred-side), no arbitrary Bash, no ref/remote/history mutation, no other repo.
#
# ┌─ SEAM: the git plumbing is owned elsewhere ──────────────────────────────────────────────────────────────────┐
# │ This file and cc.build_argv enforce WHAT the worker may do and WHICH worker a wiki ticket routes to.         │
# │ The git plumbing belongs to the publishing side: cut the worktree off origin/main, SUPPRESS the wiki         │
# │ post-commit hook (the broker sets core.hooksPath=/dev/null in the worktree's git config at creation),        │
# │ and do the ONLY push (push <branch>:main, fast-forward local main, teardown), single-flighted under a        │
# │ per-repo lock. This file does NOT implement any of that; it EXPOSES the config fields below to FILL.         │
# │                                                                                                              │
# │ `publish` uses these field names:                                                                            │
# │   - wiki_repo_path : the live wiki repo the broker cuts the worktree from (set by the deployment)            │
# │   - publish_ref    : the ref the broker publishes to / cuts from (main)                                      │
# │   - worktree_base  : box-local scratch root for the cut worktree, NOT /tmp-on-root. The broker already       │
# │       has a worktree-root convention: app.py `WORKTREES_ROOT` (env, default $TMPDIR/eph-broker-worktrees).   │
# │       The deployment can reuse WORKTREES_ROOT or set this to a dedicated home dir (candidate                 │
# │       ~/.cache/eph-wiki-worktrees/); left empty here.                                                        │
# │   - content_subdir : SECURITY REQUIREMENT. capability=write runs under `--permission-mode acceptEdits`,      │
# │       which confines edits to the process CWD but does NOT honour allow-list path patterns (verified         │
# │       live). So "edit ONLY the content tree" holds ONLY IF the worker cwd is <worktree>/<content_subdir>     │
# │       (git add/commit still operate on the whole repo from a subdir, so publish is unaffected). If cwd       │
# │       were the worktree ROOT, the worker could edit quartz.config.ts / .gitlab-ci.yml too. Default           │
# │       "content"; the publisher sets the cwd.                                                                 │
# │ COUPLED SURFACE: `agent` selects the fleet-model.sh model tier via the explicit `wiki-agent` arm (it was     │
# │   a placeholder "coder-agent" before). Change the tier in fleet-model.sh and the fleet model table together. │
# └──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
_WIKI_DOCS_ROW = Route(
    domain="wiki",
    repo=os.getenv("WIKI_REPO", ""),      # base repo reference (the publisher cuts the worktree)
    prompt_class="docs",            # prompts/wiki/docs.md; CONTENT is operator-gated (sha256 sign-off)
    agent=os.getenv("WIKI_AGENT", "wiki-agent"),        # used by fleet-model.sh; the tier is an operator call
    read_only=False,
    capability="write",             # ENFORCED by cc.build_argv: acceptEdits + git-commit, NO push
    # zbus reads $ZBUS_ENV as a LITERAL path (no slug resolution); the wiki worker's bus identity is its own
    # bot-env file. Default must be that absolute path, not the bare slug.
    env=_zbus_env("WIKI_ZBUS_ENV"),
    publish={                       # field names the publisher expects, build-time defaults, env-overridable
        "wiki_repo_path": os.getenv("WIKI_REPO_PATH", ""),   # the shared wiki checkout on the broker host
        "publish_ref": os.getenv("WIKI_PUBLISH_REF", "main"),            # push to / cut off origin/main
        # box-local scratch off the broker host's home, NOT the WORKTREES_ROOT $TMPDIR/=/tmp default that fills root.
        "worktree_base": os.getenv("WIKI_WORKTREE_BASE", os.path.expanduser("~/.cache/eph-wiki-worktrees")),
        "content_subdir": os.getenv("WIKI_CONTENT_SUBDIR", "content"),  # containment cwd subtree
    },
    # Per-row inference backend override (same pattern/resolution path as GENERAL_BACKEND). Empty
    # (default) = native Claude, UNCHANGED.
    backend=os.getenv("WIKI_DOCS_BACKEND", ""),
    backend_from_mode=True,             # every row follows FLEET_MODE; a pin wins
)

# Matcher FACTORY for SOURCE/namespace-signalled rows. Defined here (ahead of PROFILES,
# which references it at module-load time); the label factory `_label_signal_predicates` below stays the
# default for label rows; this one is the explicit override the inbox row carries. A mail-tap event carries
# NO label/assignee (a per-account new-mail sweep, not a labelled ticket), so `_label_signal_predicates`
# would REJECT it at classify (no fields gives False). This admits when the event's `source` field
# equals `source`: a namespace match, no label needed. Unlike the label pair there is NO lenient/strict
# split: a mail-tap event either IS from this source or is not, and an ingest with the right source is a
# legitimate spawn trigger, so route_pred and classify_pred are the SAME source test. Kept SEPARATE (not a
# widening of `_label_signal_predicates`) so the label rows' strict/lenient semantics stay byte-unchanged.
def _source_signal_predicates(source):
    src = str(source).strip().lower()
    if not src:   # trigger knob off: match nothing (an empty source would admit every event that has none)
        return (lambda evt: False), (lambda evt: False)

    def source_pred(evt):
        return str(evt.get("source", "")).lower() == src

    return source_pred, source_pred


# Assignee tier: matcher FACTORY for an ASSIGNEE-ONLY row. Defined here (ahead of PROFILES,
# which references it at module-load time). Mirrors `_label_signal_predicates`'s lenient/strict split
# EXACTLY (route_pred: no signal gives the namespace fallback True; classify_pred: no signal gives a reject, False),
# but differs in the field set + match rule: it matches ONLY against the event's ASSIGNEES (never labels)
# and by EXACT membership (a lowercased-username set test), NOT the label factory's substring join. Kept
# SEPARATE (not a widening of `_label_signal_predicates`) so (a) a LABEL that happens to equal the username
# never routes here, and (b) the label rows' substring-over-union semantics stay byte-unchanged.
def _assignee_fields(evt):
    """The ASSIGNEE signal on this event/brief, coerced to a list (labels are NOT included; parallel to
    _routing_fields but assignee-ONLY, for the assignee-tier matcher)."""
    assignees = evt.get("assignees") or []
    if isinstance(assignees, str):
        assignees = [assignees]
    return list(assignees)


def _assignee_signal_predicates(username):
    """Matcher FACTORY: given a trigger ASSIGNEE username, return the (lenient route_pred, strict
    classify_pred) pair with the SAME lenient/strict semantics as `_label_signal_predicates`, but matching
    ONLY the event's ASSIGNEES by EXACT (lowercased) membership rather than a substring over assignees+labels.

    NOTE: "No signal" here means NO routing signal AT ALL, i.e. the ASSIGNEES+LABELS union is empty (the same
    notion `_label_signal_predicates` uses, via `_routing_fields`), NOT merely "no assignee". This matters
    for first-match ordering: a LABELLED-but-unassigned event (e.g. an `eph:general`-labelled ticket with no
    assignee) is NOT "no signal"; it must fall THROUGH this assignee row to a later label row, not be
    swallowed by a namespace fallback. Using assignee-emptiness as the fallback trigger would hijack every
    later label row for any unassigned ticket.

    route_pred: LENIENT (route() when unstamped): an empty assignees+labels union gives True (namespace fallback,
    dead in practice, since the ansible row owns that fallback first); else `username` is an EXACT member of the
    (lowercased) ASSIGNEES gives True (labels are NOT consulted for the match).
    classify_pred: STRICT (classify() at ingest): an empty union gives False (reject); else exact assignee member."""
    uname = str(username).strip().lower()
    if not uname:   # trigger knob off: match nothing (else a no-signal event would hit the namespace fallback)
        return (lambda evt: False), (lambda evt: False)

    def _members(evt):
        return [str(x).lower() for x in _assignee_fields(evt)]   # MATCH is assignee-ONLY, exact

    def route_pred(evt):
        if not _routing_fields(evt):          # no signal AT ALL (empty assignee+label union): namespace fallback
            return True                       # (dead behind the ansible row, kept for structural parity)
        return uname in _members(evt)         # else EXACT assignee membership (labels never match here)

    def classify_pred(evt):
        if not _routing_fields(evt):          # no signal at ingest: do NOT admit
            return False
        return uname in _members(evt)         # else EXACT assignee membership (a 'dev' LABEL does NOT admit)

    route_pred.namespace_fallback = True   # no signal = True; Router.route only lets that pick a read row
    return route_pred, classify_pred


def _assignee_label_signal_predicates(signal):
    """Matcher FACTORY: `signal` = (username, label). A row that needs BOTH an exact ASSIGNEE and an
    exact LABEL (the infra/general row: assignee infra-eph + label infra:general). Same lenient/strict split
    as `_assignee_signal_predicates` (route_pred: an empty assignee+label union gives the namespace fallback True;
    classify_pred: an empty union gives False). Both matches are EXACT lowercased membership (a label superstring
    like "infra:general-x" does NOT admit), so dropping either half falls through to the next row (the
    plain infra-eph assignee row, stack-proposer) deterministically."""
    uname, label = (str(s).strip().lower() for s in signal)
    if not (uname and label):   # either half off: match nothing
        return (lambda evt: False), (lambda evt: False)

    def _match(evt):
        labels = evt.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        return (uname in [str(x).lower() for x in _assignee_fields(evt)]
                and label in [str(x).strip().lower() for x in labels])

    def route_pred(evt):
        if not _routing_fields(evt):
            return True
        return _match(evt)

    def classify_pred(evt):
        if not _routing_fields(evt):
            return False
        return _match(evt)

    route_pred.namespace_fallback = True   # no signal = True; Router.route only lets that pick a read row
    return route_pred, classify_pred


# The FIRST inbox/triage profile: a Gmail-mutation-capable worker. Routes a mail-tap
# event (`source=gmail`, per-account `gmail:<account>` key) to the account-agnostic signed inbox/triage
# prompt (domain=inbox, class=triage). capability="inbox" is what gives build_argv teeth (see
# cc.capability_tool_grant): the worker is granted Gmail READ/search/label/archive/draft + Hindsight
# recall/retain + zbus + git add/commit (ledger + rules.md grooming), and HARD-DENIED Gmail send +
# manage_gmail_filter (creation) + git push/remote/config + destructive shell + web fetch.
#
# ┌─ SEAM: capability=inbox is SECURITY-CRITICAL (spawns a Gmail-mutation worker) ───────────────────────────────┐
# │ This file, cc.build_argv and cc._run_worker enforce WHAT the worker may do and WHICH worker a mail-tap       │
# │ event routes to, plus the SPAWN-TIME fail-closed (an unknown account cuts no worker).                        │
# │ The DENY-FLOOR is the core: the only send tool (send_gmail_message) is hard-denied (and excluded at          │
# │ OAuth scope), so a triage worker can archive (remove INBOX) and draft but can NEVER send. TRASH is           │
# │ blocked at the SIGNED-PROMPT layer, not the grant: there is no trash tool; trash shares                      │
# │ modify_gmail_message_labels (add TRASH) with archive (remove INBOX), so it can't be grant-separated          │
# │ (see the cc.py TRASH note). Cred-free: no git push/remote/config, no network.                                │
# │                                                                                                              │
# │ `mcp_config_dir` is EXPOSED to be FILLED out of band. Per account, <mcp_config_dir>/<account>.json           │
# │ carries ONLY that ONE account's google-workspace (Gmail-scoped) + Hindsight OAuth (NOT Calendar; zbus        │
# │ rides ZBUS_ENV/CLI, not MCP), which gives hard per-account credential isolation. This code never             │
# │ writes those files (secret bytes). cc.build_argv turns the path into `--mcp-config                           │
# │ <dir>/<account>.json --strict-mcp-config` (load ONLY that config, ignore the ambient .mcp.json) and          │
# │ FAILS CLOSED (refuses to spawn) if the file is absent. SERVER-NAME CONTRACT: those per-account configs       │
# │ MUST name their `mcpServers` keys `google-workspace` and `hindsight` (the names the live                     │
# │ google-workspace-mcp uses) so the `mcp__<server>__<tool>` allow/deny tokens in                               │
# │ cc.capability_tool_grant match. NOT the claude.ai account-connector names.                                   │
# │ COUPLED SURFACE: `agent="mail-agent"` selects the fleet-model.sh model tier. mail-agent is already in        │
# │   fleet-model.sh's table (sonnet in normal); keep the two in sync if it is retiered.                         │
# └──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
_INBOX_ROW = Route(
    domain="inbox",
    repo=os.getenv("INBOX_REPO", ""),   # cwd for the worker (ledger + rules must persist)
    prompt_class="triage",          # prompts/inbox/triage.md: account-agnostic, sha256-signed
    agent=os.getenv("INBOX_AGENT", "mail-agent"),           # fleet-model.sh mail-agent (sonnet in normal)
    read_only=False,
    capability="inbox",             # ENFORCED by cc: Gmail read/label/archive/draft + HS + zbus + git-commit
    # zbus reads $ZBUS_ENV as a LITERAL path to the per-handle bot-env file (it does NOT resolve a bare
    # slug); a bare slug silently fails, so the worker can't escalate. Default must be the absolute path.
    env=_zbus_env("INBOX_ZBUS_ENV"),
    mcp_config_dir=INBOX_MCP_CONFIG_DIR,   # <dir>/<account>.json is filled out of band; build_argv fails closed if absent
    # Per-row inference backend override (same pattern/resolution path as GENERAL_BACKEND). Empty
    # (default) = native Claude, UNCHANGED.
    backend=os.getenv("INBOX_TRIAGE_BACKEND", ""),
    backend_from_mode=True,             # every row follows FLEET_MODE; a pin wins
)

# Zulip-wake: a 1:1 DM from an allowlisted human to a wake-mapped bot (config/zulip-wake.json, first:
# mail-bot) gives an ephemeral inbox CHAT worker. Source-signalled like the gmail row (zulip_wake.py
# ingests {source:"zulip"} only after its guards pass). Same inbox domain + per-account MCP config as triage,
# but its OWN capability "inbox-chat" (cc.capability_tool_grant): Gmail read/label/archive/draft + read-only
# Hindsight, NO zbus / git / edits, and NO ZBUS_ENV at all, so the worker has no bus identity to DM anyone
# with. The BROKER posts the envelope summary back to the original sender as the DMed bot. The account is
# NOT in the subject key (zulip:<bot>:<sender_id>): cc takes it from the bot map, fail-closed if unmapped.
ZULIP_ROW_SOURCE = os.getenv("ZULIP_ROW_SOURCE", "")

_ZULIP_INBOX_ROW = Route(
    domain="inbox",
    repo=os.getenv("INBOX_REPO", ""),   # cwd (read-only here: rules.md / docs for context)
    prompt_class="chat",            # prompts/inbox/chat.md; UNSIGNED until the operator signs its sha256
    agent=os.getenv("INBOX_AGENT", "mail-agent"),           # fleet-model.sh mail-agent (same tier as triage)
    read_only=False,                # Gmail label/archive/draft (no file edits; see capability)
    capability="inbox-chat",        # ENFORCED by cc: Gmail read/label/archive/draft + HS read; NO zbus/git/Edit
    env={},                         # deliberately NO ZBUS_ENV: the broker, not the worker, replies on Zulip
    mcp_config_dir=INBOX_MCP_CONFIG_DIR,   # same per-account <dir>/<account>.json as triage; fail-closed if absent
    # Per-row inference backend override (same pattern/resolution path as GENERAL_BACKEND). Empty
    # (default) = native Claude, UNCHANGED.
    backend=os.getenv("INBOX_CHAT_BACKEND", ""),
    backend_from_mode=True,             # every row follows FLEET_MODE; a pin wins
)

# PROFILES entry = (namespace_prefix, trigger_label, Route-template). `trigger_label` is the
# assignee/label string that DEFINES the row, with the same env-override pattern as ANSIBLE_ROW_LABEL
# (a config knob rather than hard-coded fleet policy; see the ansible row label above). The
# ansible entry is first, UNCHANGED behavior (same label, same Route, same predicate semantics); the wiki
# entry is second, the first write-capable row. First match wins, so read/ansible is unaffected.
# A PROFILES entry is (namespace_prefix, signal, Route-template[, predicate_factory]). The optional 4th
# element is the matcher FACTORY; it DEFAULTS to `_label_signal_predicates` so the two existing (ansible,
# wiki) rows are byte-identical in behaviour; they stay 3-tuples and keep their strict/lenient label
# semantics UNCHANGED. The inbox row carries an explicit factory (`_source_signal_predicates`) because
# mail-tap events have no label; `signal` there is the event SOURCE, not a label. First-match-wins order
# is preserved: ansible, then wiki, then inbox.
# The code change-proposer write row. Routes an `eph:code-change`-labelled ticket to
# the SIGNED change-proposer prompt (domain=code, class=change-proposer). capability="code" gives
# cc.build_argv teeth: acceptEdits + git add/commit + a NARROW per-repo test-Bash (test_runner KEY into
# cc._CODE_TEST_RUNNERS), with the FULL _WRITE_DENIED floor (no push/remote/config/history-rewrite, no
# destructive shell, no network). Cred-free: the worker commits in a throwaway worktree; the BROKER pushes a
# FEATURE BRANCH and opens an MR, NEVER main. Nothing auto-merges/deploys (human-gated MR).
#
# COUPLED SURFACE: agent="code-worker" selects the fleet-model.sh model tier. ADD a
#   `code-worker) claude-opus-4-8` entry to fleet-model.sh AND the fleet per-agent model table
#   (opus in normal/high, sonnet in lean/min), or it silently falls through the catch-all default.
# The row is generic (repo and runner are env params), with per-ticket repo/runner resolution on top.
#   NO capable run until prompts/code/change-
#   proposer.md is security-reviewed and the operator hash-signs its sha256 in approved-prompts.json.
_CODE_ROW = Route(
    domain="code",
    repo=os.getenv("CODE_REPO", ""),   # base repo (the publisher cuts the worktree; the checkout is provisioned out of band)
    prompt_class="change-proposer",     # prompts/code/change-proposer.md; SIGNED (hash-gated; unsigned until the operator signs)
    agent=os.getenv("CODE_AGENT", "code-worker"),   # fleet-model.sh code-worker (opus; coupled surface, see note above)
    read_only=False,
    capability="code",                  # ENFORCED by cc.build_argv: acceptEdits + git-commit + scoped test-Bash, NO push
    test_runner=os.getenv("CODE_TEST_RUNNER", "pytest"),   # KEY into cc._CODE_TEST_RUNNERS; FALLBACK only, CODE_REPO_RUNNERS per-repo wins
    env=_zbus_env("CODE_ZBUS_ENV"),   # LITERAL path (no slug resolution)
    publish={                           # filled by the publisher; code MRs push a FEATURE BRANCH and open an MR, NEVER push to main
        "code_repo_path": os.getenv("CODE_REPO_PATH", ""),   # local checkout the publisher cuts from and pushes
        "mr_target_ref": os.getenv("CODE_MR_TARGET_REF", "main"),   # MR target (worktree cut FROM this; NEVER pushed to directly)
        "branch_prefix": os.getenv("CODE_BRANCH_PREFIX", "eph/code-change"),        # feature-branch namespace for the MR
        "worktree_base": os.getenv("CODE_WORKTREE_BASE", os.path.expanduser("~/.cache/eph-code-worktrees")),
    },
    # Per-row inference backend override (same pattern/resolution path as GENERAL_BACKEND). Empty
    # (default) = native Claude, UNCHANGED.
    backend=os.getenv("CODE_CHANGE_BACKEND", ""),
    backend_from_mode=True,             # every row follows FLEET_MODE; a pin wins
)

# GitHub/GitLab issue to code change-proposer row. Unlike _CODE_ROW (a Vikunja LABEL row), an issue
# arrives from an external forge and carries NO vikunja label, so it is SOURCE-signalled like inbox/mr
# (matches the event `source` "github"/"gitlab" via _source_signal_predicates). It REUSES the code lane
# VERBATIM: domain="code" + prompt_class="change-proposer", the SAME signed prompts/code/change-proposer.md
# (NO new prompt, NO hash change), capability="code" (acceptEdits + git-commit + Hindsight + fail-closed
# test-Bash), agent=code-worker (SAME as _CODE_ROW, so no new fleet-model.sh coupled surface). The publish is
# a FEATURE-branch MR (branch_prefix "eph/issue", NEVER a default branch; the publish_mr assert holds). The
# `repo`/publish["code_repo_path"] DEFAULT is irrelevant: cc._run_worker resolves the LOCAL repo per-issue
# (resolve_issue_repo, fail-closed on an unmapped repo) and overrides code_repo_path with the resolved path.
_ISSUE_CODE_ROW = Route(
    domain="code",                      # REUSE the code lane's signed prompt (prompts/code/change-proposer.md)
    repo=os.getenv("ISSUE_CODE_REPO", ""),   # DEFAULT only; per-issue resolution overrides
    prompt_class="change-proposer",     # SAME signed prompt as _CODE_ROW (hash-gated; do NOT create a new one)
    agent=os.getenv("CODE_AGENT", "code-worker"),   # SAME agent as _CODE_ROW (fleet-model.sh code-worker, opus)
    read_only=False,
    capability="code",                  # ENFORCED by cc.build_argv: acceptEdits + git-commit + test-Bash, NO push
    test_runner=os.getenv("ISSUE_CODE_TEST_RUNNER", ""),   # per-issue repos vary; empty means no test Bash (fail-closed)
    env=_zbus_env("ISSUE_CODE_ZBUS_ENV"),   # LITERAL path
    publish={                           # feature-branch MR; code_repo_path is a DEFAULT overridden per-issue in cc
        "code_repo_path": os.getenv("ISSUE_CODE_REPO_PATH", ""),   # irrelevant default
        "mr_target_ref": os.getenv("ISSUE_CODE_MR_TARGET_REF", "main"),   # MR target (worktree cut FROM it)
        "branch_prefix": os.getenv("ISSUE_CODE_BRANCH_PREFIX", "eph/issue"),   # feature-branch namespace; NEVER main
        "worktree_base": os.getenv("ISSUE_CODE_WORKTREE_BASE", os.path.expanduser("~/.cache/eph-issue-worktrees")),
    },
    # Per-row inference backend override (same pattern/resolution path as GENERAL_BACKEND). Empty
    # (default) = native Claude, UNCHANGED.
    backend=os.getenv("ISSUE_CODE_BACKEND", ""),
    backend_from_mode=True,             # every row follows FLEET_MODE; a pin wins
)

# The INFRA stack-proposer row, triggered by assignee INFRA_ASSIGNEE. It REUSES the code lane's capability ("code":
# acceptEdits + git-commit + Hindsight + the _CODE_DENIED floor incl. the .*-env read-deny, NO push/bus) and the
# code publish path (broker pushes a FEATURE branch + opens the MR; mr-review reviews; never merges/deploys), with
# its own SIGNED prompt (prompts/infra/stack-proposer.md: /srv/infra gotcha list and the investigate-only no-commit path).
# Differences from _CODE_ROW, all on purpose:
#   * target repo defaults to /srv/infra WITHOUT a `repo:` token (resolve_infra_repo), still CODE_REPO_ALLOWLIST-gated
#     (an operator adds /srv/infra to the live allowlist); no repo-inference ladder.
#   * branch eph/infra/<ticket>; worktree off ~/.cache/eph-infra-worktrees (never the live NFS /srv/infra checkout).
#   * test_runner "none" by default (no /srv/infra validator is granted; validation happens before deploy).
#   * publish["mr_notify_handle"]: the broker DMs the configured handle the MR link on open. The worker
#     itself has no bus (code grant), so the DM is broker-side.
# COUPLED SURFACE: agent="infra-worker" needs a fleet-model.sh entry and a fleet model table row.
_INFRA_ROW = Route(
    domain="infra",
    repo=os.getenv("INFRA_REPO", ""),
    prompt_class="stack-proposer",      # prompts/infra/stack-proposer.md; SIGNED (hash-gated; unsigned until the operator signs)
    agent=os.getenv("INFRA_AGENT", "infra-worker"),
    read_only=False,
    capability="code",                  # ENFORCED by cc.build_argv: acceptEdits + git-commit + Hindsight, NO push
    test_runner=os.getenv("INFRA_TEST_RUNNER", "none"),
    env=_zbus_env("INFRA_ZBUS_ENV"),   # LITERAL path
    publish={
        "code_repo_path": os.getenv("INFRA_REPO_PATH", ""),
        "mr_target_ref": os.getenv("INFRA_MR_TARGET_REF", "master"),   # resolved per-repo at spawn anyway
        "branch_prefix": os.getenv("INFRA_BRANCH_PREFIX", "eph/infra"),
        "worktree_base": os.getenv("INFRA_WORKTREE_BASE", os.path.expanduser("~/.cache/eph-infra-worktrees")),
        "mr_notify_handle": os.getenv("INFRA_MR_NOTIFY_HANDLE", ""),   # from site.env; "" means no DM
    },
    backend=os.getenv("INFRA_BACKEND", ""),
    backend_from_mode=True,
)

# The GENERALIST infra row, infra/general, triggered by assignee INFRA_ASSIGNEE + label INFRA_GENERAL_LABEL (the
# plain assignee keeps the signed stack-proposer, unchanged). Same grant + publish path as _INFRA_ROW
# (capability "code": acceptEdits + git-commit + mcp-ro.sh, NO push/bus; broker pushes eph/infra/<ticket>,
# opens the MR, DMs the configured handle), with its own SIGNED prompt (prompts/infra/general.md) that adds:
#   * live READ-ONLY diagnosis over the mcp-ro.sh read arms (grafana/loki, proxmox, unifi). The
#     wrapper denies every write tool itself; the raw mcp.sh stays hard-denied by the code floor.
#   * more target repos, one per ticket via `repo:` (default /srv/infra): /srv/ansible (playbooks/yaml only;
#     per-repo runner "ansible-syntax" = `ansible-playbook --syntax-check -i /dev/null`) and /srv/git/wiki
#     runbooks. Every target is still CODE_REPO_ALLOWLIST-gated (resolve_infra_repo, fail-closed): an operator adds
#     /srv/ansible and /srv/git/wiki to the live allowlist to enable them.
# row_id "infra/general" keeps its ingest stamp distinct from _INFRA_ROW's (both are domain "infra").
# COUPLED SURFACE: agent defaults to "infra-worker" (same fleet-model.sh entry and model table row).
_INFRA_GENERAL_ROW = Route(
    domain="infra",
    repo=_INFRA_ROW.repo,
    prompt_class="general",             # prompts/infra/general.md; SIGNED (hash-gated; unsigned until the operator signs)
    agent=os.getenv("INFRA_GENERAL_AGENT", _INFRA_ROW.agent),
    read_only=False,
    capability="code",                  # ENFORCED by cc.build_argv: acceptEdits + git-commit + mcp-ro.sh, NO push
    test_runner=_INFRA_ROW.test_runner, # per-repo seed wins (resolve_test_runner): /srv/ansible uses ansible-syntax
    env=dict(_INFRA_ROW.env),
    publish=dict(_INFRA_ROW.publish),
    backend=os.getenv("INFRA_GENERAL_BACKEND", _INFRA_ROW.backend),
    backend_from_mode=True,
    row_id="infra/general",
)

# The event SOURCE strings that DEFINE the two issue rows (env-overridable, same spirit as MR_REVIEW_ROW_SOURCE).
GITHUB_ISSUE_SOURCE = os.getenv("GITHUB_ISSUE_SOURCE", "github")
GITLAB_ISSUE_SOURCE = os.getenv("GITLAB_ISSUE_SOURCE", "gitlab")

# Forge-event allowlist (defense-in-depth). The issue rows above are
# _source_signal_predicates (bare source match), so absent this gate, ANY github/gitlab event
# (issue_comment / issues.edited / .closed / .labeled, PR events) that carries a repo+issue would
# classify to the code lane and spawn a WRITE worker. Today only the external tap's issue-OPEN filter
# stands between a stray forge event and a bogus spawn. This allowlist makes the broker NOT depend on
# the tap alone: only an issue-OPEN event_name admits a forge event; every other forge event is rejected
# at classify. Env-overridable so an operator can match whatever event_name their tap emits; default covers the
# common github `issues`+action `opened` and the gitlab `issue`/open spellings.
ISSUE_TRIGGERING_EVENTS = set(filter(None, os.getenv(
    "ISSUE_TRIGGERING_EVENTS", "issues.opened,issue.opened,issue.open").split(",")))
# The forge SOURCES the gate is SCOPED to (reusing the row source constants, not hard-coded). Only an
# event whose `source` is one of these is subject to ISSUE_TRIGGERING_EVENTS; vikunja/gmail/mr events are
# never in this set, so the gate never fires for them.
_FORGE_SOURCES = {GITHUB_ISSUE_SOURCE.lower(), GITLAB_ISSUE_SOURCE.lower()}

# MR-reviewer ephemeral capability: an adversarial, PURE-READ reviewer of an MR the
# broker itself opened (code change-proposer / general-worker). Unlike ansible/wiki/code (Vikunja-label
# rows) it is SOURCE-signalled like inbox (MR events carry no vikunja label), so it matches on the event
# `source` ("mr") via _source_signal_predicates. A config knob keeps the source string out of hard-code.
MR_REVIEW_ROW_SOURCE = os.getenv("MR_REVIEW_ROW_SOURCE", "mr")
# Box-local scratch root for the throwaway review worktree (checked out at the MR SOURCE branch); off
# the broker host's home, NOT the $TMPDIR/=/tmp default that fills root. cc._run_worker reads the same env default.
MR_REVIEW_WORKTREE_BASE = os.getenv("MR_REVIEW_WORKTREE_BASE", os.path.expanduser("~/.cache/eph-mr-worktrees"))

# The MR-review row. capability="mr-review" gives cc.build_argv teeth: a PURE READ grant (Read/Grep/Glob;
# Edit/Write/Bash/git/network DENIED); the reviewer reads the checked-out MR branch + the broker-injected
# diff and emits a {status,summary} verdict; the BROKER posts that verdict as an MR note (the reviewer posts
# NOTHING itself and opens no MR, so there is no merge power and no side-effect loop). read_only=True,
# publish={} (no push/merge), env={} (cred-free/pure-compute). The per-event repo_path from the mr brief
# wins over `repo` at spawn; `repo` here is only the default when a brief lacks it.
#
# COUPLED SURFACE: agent="mr-reviewer" selects the fleet-model.sh model tier. ADD a
#   `mr-reviewer) claude-opus-4-8` entry to fleet-model.sh AND the fleet per-agent model table
#   (opus in normal/high, sonnet in lean/min, since it's a review agent), or it silently falls through the
#   catch-all default. That change lives outside this repo.
_MR_REVIEW_ROW = Route(
    domain="mr-review",
    repo=os.getenv("MR_REVIEW_REPO", ""),  # default; the per-event repo_path wins at spawn
    prompt_class="agent",           # prompts/mr-review/agent.md, sha256-gated
    agent=os.getenv("MR_REVIEW_AGENT", "mr-reviewer"),  # fleet-model.sh (opus; coupled surface, add it)
    read_only=True,
    capability="mr-review",
    env={},                         # cred-free/pure-compute: reviewer posts nothing; broker posts the note
    publish={},                     # no push, no merge
    # Per-row inference backend override (same pattern/resolution path as GENERAL_BACKEND). Empty
    # (default) = native Claude, UNCHANGED.
    backend=os.getenv("MR_REVIEWER_BACKEND", ""),
    backend_from_mode=True,             # every row follows FLEET_MODE; a pin wins
    # Worker harness for this row. Default "cc" = inert (unchanged native Claude Code review). "hermes"
    # ALSO requires MR_REVIEWER_BACKEND set to a non-native backend; hermes_provider refuses "" / "claude"
    # (adapters/hermes.py, no Claude Code OAuth path in hermes). Route.__post_init__ fail-closes any value
    # outside ADAPTERS (typo'd row never silently runs on the wrong harness).
    adapter=os.getenv("MR_REVIEW_ADAPTER", "cc"),
    # Claude as a supported reviewer backend, flipped by config alone. Empty
    # (default) = UNCHANGED: FLEET_MODE / MR_REVIEWER_BACKEND decide (deepseek today). Set to a Claude model
    # (e.g. "opus") uses the model_pin path: native Claude Code on that model in EVERY
    # fleet mode, FLEET_MODE's backend pick skipped, `model:` labels ignored. Needs MR_REVIEW_ADAPTER=cc and
    # MR_REVIEWER_BACKEND unset (or "claude"); an explicit non-native backend pin still rides through.
    model_pin=os.getenv("MR_REVIEWER_MODEL", ""),
)

# General-purpose worker: broad tools adjudicated by the auto-classifier
# (capability="general": cc runs --permission-mode auto with a deny-floor, NO allow-list), loading the user-level rules and
# the classifier config via setting_sources="user". Cred-free like code: commits in a throwaway worktree and
# the broker pushes a FEATURE BRANCH and opens the MR (publish_mr/open_mr, feature-branch-only); a no-commit
# ticket is a clean no-op whose only output is the envelope-derived comment. Worktree target is BOUNDED by
# GENERAL_REPO_ALLOWLIST even though the toolset isn't. Signed prompt is THIN+STABLE (ephemeral-oneshot
# framing only; no user-level rules embedded, so a mode flip never breaks the sha; the user-level rules ride setting-sources).
# COUPLED SURFACE: agent="general-worker" needs a fleet-model.sh entry and a model table row (opus normal/high).
# Bus identity = its own bot (own env file); routed LAST so every narrow label wins first.
_GENERAL_ROW = Route(
    domain="general",
    repo=os.getenv("GENERAL_REPO", ""),   # default worktree target (must be in GENERAL_REPO_ALLOWLIST)
    prompt_class="agent",               # prompts/general/agent.md; SIGNED (thin+stable; unsigned until the operator signs)
    agent=os.getenv("GENERAL_AGENT", "general-worker"),   # fleet-model.sh general-worker (opus; coupled surface)
    read_only=False,
    capability="general",               # ENFORCED by cc.build_argv: --permission-mode auto + deny-floor, NO allow-list
    setting_sources=os.getenv("GENERAL_SETTING_SOURCES", "user"),   # load the user-level rules + autoMode classifier config
    mcp_config_path=os.getenv("GENERAL_MCP_CONFIG",     # strict-mcp-config, Hindsight-ONLY (fail-closed if absent)
                              os.path.expanduser("~/.config/eph-broker/general-mcp.json")),
    env=_zbus_env("GENERAL_ZBUS_ENV"),   # scoped bus identity
              # The worker may DM the operator with a blocker/status via the zbus CLI. It
              # authenticates WITH the file (never Reads it; the .*-env secret-read floor still blocks that).
    publish={                           # cred-free feature-branch MR (reuses the code publish_mr/open_mr), NEVER main
        "code_repo_path": os.getenv("GENERAL_REPO_PATH", ""),
        "mr_target_ref": os.getenv("GENERAL_MR_TARGET_REF", "main"),
        "branch_prefix": os.getenv("GENERAL_BRANCH_PREFIX", "eph/general"),
        "worktree_base": os.getenv("GENERAL_WORKTREE_BASE", os.path.expanduser("~/.cache/eph-general-worktrees")),
    },
    # @eph + @fleet general workers run on this backend (resolved from
    # /etc/site/spawn-models.json at spawn). "" (unset) = MODE-DRIVEN default (via
    # backend_from_mode below): fleet-model.sh general-worker is asked at EVERY spawn; a `claude-*` answer
    # (high/normal opus, lean sonnet) = native Anthropic with that model, any other answer (e.g. "deepseek"
    # in the cheapest mode) = that backend name, resolved and fail-closed exactly like GENERAL_BACKEND=<name>.
    # A mode flip therefore takes effect on the NEXT spawn, no broker restart. Setting GENERAL_BACKEND (in
    # ~/.config/eph-broker/eph-broker.env) PINS the backend regardless of mode ("claude" pins native).
    # Reversible: unset + restart.
    # TIER-2 (cheap-cloud: OpenRouter DeepSeek/Qwen-MoE via a litellm proxy) = THIS knob (explicit pin)
    # OR the mode-driven default above (fleet-model.sh answering a non-claude backend name).
    #   enable:  GENERAL_BACKEND=deepseek   (or any `kind:"cloud"` registry name: qwen-moe, glm, ...)
    #   effect:  --model <registry model> + --effort, base_url/ctx/small-model routes + the backend's auth
    #            key (allowlisted, sourced BY PATH from the registry's env file, with no new secret sourcing),
    #            and bypass_classifier=True (non-Anthropic backends can't run the auto classifier; the
    #            _GENERAL_DENIED floor still applies). Operator-gated, as with any backend enable.
    #   misconfig: a value that won't resolve (typo / missing registry entry) FAILS CLOSED: the turn is
    #            refused and the operator alerted, never silently re-run on native Anthropic. "claude" = native.
    backend=os.getenv("GENERAL_BACKEND", ""),
    backend_from_mode=True,             # unset GENERAL_BACKEND means FLEET_MODE decides (see with_model)
)

# @fleet: a FULL INTERACTIVE temp fleet agent, NOT an ephemeral `claude -p` worker. The
# distinction: @eph = headless ephemeral worker (above, _GENERAL_ROW); @fleet = a real TFA a person can
# INTERACT WITH IN-PANE. Handled OUTSIDE the coalescer/CCAdapter (app.Broker._launch_tfa): the broker shells
# out to /opt/site/bin/spawn.sh --<backend> which leases a POOL bot and launches a DETACHED TMUX session (own bus
# identity, attachable via `tmux attach`, lives until spun down), seeded with the ticket. domain="tfa" is the
# marker app.ingest branches on BEFORE the coalescer. This row carries only what the launch needs (backend +
# cwd); the CCAdapter fields (prompt_class/agent/capability/mcp) are unused for a tfa row.
_TFA_ROW = Route(
    domain="tfa",
    repo=os.getenv("TFA_CWD_BASE", os.path.expanduser("~/.cache/eph-tfa")),  # BASE; _launch_tfa appends task-<id> (spawn.sh auto-mkdirs). A full TFA navigates out from here.
    prompt_class="n/a",                                   # unused (no prompt render for a tfa row)
    agent="",                                             # unused (model/effort come from spawn.sh's registry)
    backend=os.getenv("TFA_BACKEND", ""),                 # spawn.sh --<backend>; unset = no flag (spawn.sh default)
)

# Every row now carries a per-row inference backend override, resolved through the SAME path as
# GENERAL_BACKEND (cc.load_backend / spawn-models registry, cc._one_turn); empty (default) = native Claude,
# unchanged. Env knobs: ANSIBLE_BACKEND, WIKI_DOCS_BACKEND, INBOX_TRIAGE_BACKEND, INBOX_CHAT_BACKEND,
# CODE_CHANGE_BACKEND, ISSUE_CODE_BACKEND, MR_REVIEWER_BACKEND, GENERAL_BACKEND (mode-driven), TFA_BACKEND.
# MR_REVIEWER_MODEL pins the reviewer to a native Claude model (see _MR_REVIEW_ROW).

def _build_profiles():
    """The PROFILES table."""
    return list(_BASE_PROFILES)


_BASE_PROFILES = [
    ("vikunja:task", ANSIBLE_ROW_LABEL, _ANSIBLE_ROW),
    ("vikunja:task", WIKI_ROW_LABEL, _WIKI_DOCS_ROW),
    ("gmail", INBOX_ROW_SOURCE, _INBOX_ROW, _source_signal_predicates),
    ("vikunja:task", CODE_ROW_LABEL, _CODE_ROW),
    # @dev ASSIGNEE (no label) routes to the SAME code change-proposer row. Assignee-ONLY, exact
    # membership (_assignee_signal_predicates). Placed AFTER the narrow-label rows so an explicit label
    # still wins first-match, and BEFORE the general row so assignee is the FALLBACK tier, not the catch-all.
    ("vikunja:task", DEV_ASSIGNEE, _CODE_ROW, _assignee_signal_predicates),
    # @infra-eph ASSIGNEE plus infra:general LABEL gives the generalist infra row. MUST sit BEFORE the plain
    # @infra-eph row (first match wins; without the label the ticket falls through to stack-proposer).
    ("vikunja:task", (INFRA_ASSIGNEE, INFRA_GENERAL_LABEL), _INFRA_GENERAL_ROW, _assignee_label_signal_predicates),
    # @infra-eph ASSIGNEE gives the infra stack-proposer row (/srv/infra MRs). Same tier/ordering as @dev.
    ("vikunja:task", INFRA_ASSIGNEE, _INFRA_ROW, _assignee_signal_predicates),
    # FLEET_ASSIGNEE (@eph, no label) gives the general worker row. Assignee-ONLY, exact membership
    # (_assignee_signal_predicates), the general mirror of the @dev row above. Placed AFTER the @dev assignee
    # row and BEFORE the eph:general LABEL row (both give the identical _GENERAL_ROW, so order between them is
    # stamp-insensitive); AFTER all narrow-label + @dev rows so an explicit narrow label still wins first.
    ("vikunja:task", FLEET_ASSIGNEE, _GENERAL_ROW, _assignee_signal_predicates),
    # @fleet ASSIGNEE (no label) gives a FULL INTERACTIVE TFA, NOT the headless general worker.
    # domain="tfa" makes app.ingest launch spawn.sh (real tmux TFA) instead of enqueuing a coalescer worker.
    ("vikunja:task", FLEET_ASSIGNEE_ALT, _TFA_ROW, _assignee_signal_predicates),
    # appended so first-match order for the existing vikunja rows is unchanged; source-signalled like inbox.
    ("mr", MR_REVIEW_ROW_SOURCE, _MR_REVIEW_ROW, _source_signal_predicates),
    # GitHub/GitLab issue gives a code change-proposer. Source-signalled (github/gitlab namespaces); both give
    # the SAME _ISSUE_CODE_ROW (code lane reused). Distinct namespaces from every vikunja row, so first-match
    # order for the vikunja rows is untouched.
    ("github", GITHUB_ISSUE_SOURCE, _ISSUE_CODE_ROW, _source_signal_predicates),
    ("gitlab", GITLAB_ISSUE_SOURCE, _ISSUE_CODE_ROW, _source_signal_predicates),
    # Own namespace, so its position changes no existing first-match; kept above the general row (LAST).
    ("zulip", ZULIP_ROW_SOURCE, _ZULIP_INBOX_ROW, _source_signal_predicates),
    ("vikunja:task", GENERAL_ROW_LABEL, _GENERAL_ROW),   # routed LAST: narrow labels win first
]


def _trigger_on(entry):
    """False when the row's trigger is empty (an unset knob): an assignee+label row needs both halves."""
    sig = entry[1]
    return all(str(s).strip() for s in sig) if isinstance(sig, tuple) else bool(str(sig).strip())


_BASE_PROFILES = [e for e in _BASE_PROFILES if _trigger_on(e)]
# routes.json: site-defined agent types. A deployment adds its own rows in CONFIG_DIR/routes.json without
# touching this file: each row names a trigger (an exact label and/or assignee on a tracker ticket), a signed
# prompt and one of the capabilities below (or a site capability built on one, see load_site_capabilities). Tool
# grants stay in code (cc.capability_tool_grant); a row can only pick from this menu, and each entry's plumbing (publish target, MCP config, setting sources, bus env) is copied
# from the built-in row that already runs that capability. "read" = the read-only grant in a throwaway worktree
# of the row's repo (agent and backend pin from the ansible row). Other capabilities need per-row code (inbox
# accounts, wiki publish, MR events) and are refused.
CONFIG_ROUTE_CAPABILITIES = {"read": _ANSIBLE_ROW, "code": _CODE_ROW, "general": _GENERAL_ROW}
# prompt domains cc.py/app.py branch on by name (tfa launch, inbox account gate, infra grants): a config row
# reusing one would inherit that special handling, so they are refused.
CONFIG_ROUTE_RESERVED_DOMAINS = frozenset({"tfa", "inbox", "infra"})
_CONFIG_ROUTE_KEYS = {"id", "match", "prompt", "capability", "repo", "agent", "model"}

# Every capability name cc.capability_tool_grant knows. A site capability may not reuse one.
BUILTIN_CAPABILITIES = frozenset({"read", "write", "code", "general", "inbox", "inbox-chat", "mr-review",
                                  })
# One permission rule as Claude Code writes it: `Tool` or `Tool(spec)`, no nested parens, no commas, one line.
_TOOL_RULE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(\([^(),\x00-\x1f]+\))?")
# A Bash allow rule that admits every command (bare, `*`-led, or `:*`): it would make the grant shell access.
_BROAD_BASH_RE = re.compile(r"Bash(\(\s*(\*.*|:\*)\s*\))?")
# ...and so does allowing a shell, an interpreter, a command runner or a wrapper, whatever follows it
# (`Bash(bash:*)`). The head is the first word once quotes, VAR=value prefixes and `command`/`builtin` (and their
# flags) are stripped, so `Bash("bash" -c:*)`, `Bash(X=1 sh:*)` and `Bash(command -p bash:*)` are caught too; a head
# with a glob, `$`, `{` or `~` in it is refused outright (`Bash($SHELL:*)`, `Bash(/bin/ba?h:*)`). A listed head
# with a version suffix is refused too (`tclsh8.6`, `ksh93`, `python3.11`). So is a script runner (`make`,
# `npm run`, `tox`): it runs files the worker can edit. Best effort: find, sed and git stay allowed (they can run
# commands too; docs/routes.md says so), broker.yaml is the operator's.
_SHELL_HEADS = frozenset({
    "sh", "bash", "rbash", "dash", "zsh", "ksh", "mksh", "pdksh", "oksh", "yash", "ash", "fish", "csh", "tcsh",
    "rc", "es", "elvish", "nu", "xonsh", "pwsh", "busybox", "toybox",
    "awk", "gawk", "mawk", "nawk", "tclsh", "wish", "expect", "osascript",
    "python", "pypy", "perl", "ruby", "php", "lua", "luajit",
    "node", "nodejs", "deno", "bun", "bunx", "npx", "pnpx", "uvx",
    "make", "tox", "nox",
    "env", "xargs", "exec", "eval", "source", ".", "sudo", "su", "doas", "pkexec", "runuser", "setpriv", "nohup",
    "timeout", "nice", "ionice", "chrt", "taskset", "stdbuf", "time", "setsid", "flock", "unshare", "nsenter",
    "chroot", "script", "watch", "parallel", "strace", "ltrace", "gdb", "systemd-run", "bwrap", "firejail"})
# One explicit name per listed head, plus an optional version suffix: `(sh|bash|...)[0-9.]*`.
_SHELL_HEAD_RE = re.compile("(" + "|".join(re.escape(h) for h in sorted(_SHELL_HEADS)) + r")[0-9.]*")
_SHELL_HEAD_GLOB = re.compile(r"[$*?\[{~]")
_SHELL_RUNNERS = frozenset({("uv", "run"), ("npm", "exec"), ("npm", "x"), ("npm", "run"), ("pnpm", "exec"),
                            ("pnpm", "dlx"), ("pnpm", "run"), ("yarn", "dlx"), ("yarn", "run"), ("bun", "run"),
                            ("poetry", "run"), ("pipenv", "run"), ("pdm", "run"), ("cargo", "run"), ("go", "run")})


def _allows_a_shell(rule):
    """True when a Bash allow rule's head is a shell, an interpreter or a command runner."""
    m = re.fullmatch(r"Bash\((.*)\)", rule)
    if not m:
        return False
    words = [w for w in (re.sub(r"[\"'`\\]", "", w) for w in re.split(r"[\s:]+", m.group(1))) if w]
    after_command = False
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or words[0] in ("command", "builtin")
                     or (after_command and words[0].startswith("-"))):
        after_command = after_command or words[0] in ("command", "builtin")
        words.pop(0)
    if not words or _SHELL_HEAD_GLOB.search(words[0]):
        return True
    head = os.path.basename(words[0]).lower()
    return (bool(_SHELL_HEAD_RE.fullmatch(head))
            or tuple(w.lower() for w in words[:2]) in _SHELL_RUNNERS)


class CapabilitiesConfigError(ValueError):
    """broker.yaml `capabilities:` is invalid. Raised at import, so the broker refuses to start (fail-closed)."""


def load_site_capabilities(doc=None):
    """broker.yaml `capabilities:` -> {name: {"base", "allow", "deny"}} (tuples). A site capability is one of the
    CONFIG_ROUTE_CAPABILITIES bases plus extra rules: `allow` entries are added to the base's --allowedTools and
    `deny` entries to its --disallowedTools. The base's deny floor stays as is and deny always wins, so a site
    capability can never lift a base deny. `general` takes no `allow` (an allow entry skips the auto-mode
    classifier). No section -> {} (nothing changes)."""
    caps = site_env.section("capabilities") if doc is None else doc
    if caps is None:
        return {}
    where = f"{site_env.BROKER_CONFIG}: capabilities"
    if not isinstance(caps, dict):
        raise CapabilitiesConfigError(f"{where}: expected a mapping of name: {{base, allow, deny}}")
    out = {}
    for name, spec in caps.items():
        at = f"{where}.{name}"
        if not isinstance(name, str) or not _CONFIG_ROUTE_ID_RE.fullmatch(name):
            raise CapabilitiesConfigError(f"{at}: name must be lowercase letters, digits and dashes (max 40)")
        if name in BUILTIN_CAPABILITIES:
            raise CapabilitiesConfigError(f"{at}: clashes with a built-in capability")
        if not isinstance(spec, dict):
            raise CapabilitiesConfigError(f"{at}: expected {{base, allow, deny}}")
        extra = {k for k in spec if not str(k).startswith("_")} - {"base", "allow", "deny", "description"}
        if extra:
            raise CapabilitiesConfigError(f"{at}: unknown key(s) {sorted(extra)}")
        base = spec.get("base")
        if base not in CONFIG_ROUTE_CAPABILITIES:
            raise CapabilitiesConfigError(f"{at}: base must be one of {', '.join(sorted(CONFIG_ROUTE_CAPABILITIES))}")
        rules = {}
        for k in ("allow", "deny"):
            v = spec.get(k, [])
            if not isinstance(v, list) or not all(isinstance(x, str) and _TOOL_RULE_RE.fullmatch(x) for x in v):
                raise CapabilitiesConfigError(f'{at}: {k} must be a list of rules like "Read" or "Bash(wc -w:*)"')
            rules[k] = tuple(dict.fromkeys(v))
        if base == "general" and rules["allow"]:
            raise CapabilitiesConfigError(f"{at}: base general takes no allow rules (they would skip the "
                                          "auto-mode classifier); use deny only")
        broad = [r for r in rules["allow"] if _BROAD_BASH_RE.fullmatch(r) or _allows_a_shell(r)]
        if broad:
            raise CapabilitiesConfigError(f"{at}: allow {broad} admits every shell command; name the commands")
        out[name] = {"base": base, "allow": rules["allow"], "deny": rules["deny"]}
    return out
_CONFIG_ROUTE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")             # always .fullmatch (a "$" admits "x\n")
_CONFIG_ROUTE_PROMPT_RE = re.compile(r"([a-z0-9][a-z0-9_-]*)/([a-z0-9][a-z0-9_-]*)")
_CONFIG_ROUTE_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,80}")
_HERE = os.path.dirname(os.path.abspath(__file__))
ROUTES_PATH = os.getenv("ROUTES_PATH", os.path.join(os.getenv("CONFIG_DIR", os.path.join(_HERE, "config")),
                                                    "routes.json"))


class RoutesConfigError(ValueError):
    """routes.json is present but invalid. Raised at import, so the broker refuses to start (fail-closed)."""


def _exact_label_signal_predicates(label):
    """routes.json matcher FACTORY for a LABEL-only config row. Same lenient/strict split as
    `_label_signal_predicates` (route_pred: no signal at all -> namespace fallback True; classify_pred: no
    signal -> False), but the match is EXACT lowercased membership in the event's LABELS only: a label
    superstring or an assignee with the same name never admits (unlike the built-in substring label rows)."""
    want = str(label).strip().lower()

    def _match(evt):
        labels = evt.get("labels") or []
        if isinstance(labels, str):
            labels = [labels]
        return want in [str(x).strip().lower() for x in labels]

    def route_pred(evt):
        if not _routing_fields(evt):
            return True
        return _match(evt)

    def classify_pred(evt):
        if not _routing_fields(evt):
            return False
        return _match(evt)

    route_pred.namespace_fallback = True   # no signal = True; Router.route only lets that pick a read row
    return route_pred, classify_pred


def _builtin_row_keys():
    keys = {e[2].row_key for e in _build_profiles()}
    return keys | {_TFA_ROW.row_key}


def load_config_routes(path=None, prompts_dir=None, approved_path=None, site_caps=None):
    """Read routes.json (or the broker.yaml `routes:` list) and return its rows as PROFILES entries, in file order.
    No file -> [] (the built-in table, unchanged). Any problem -> RoutesConfigError naming the row and field; nothing is half-loaded.
    Each row's prompt must already be signed: it is loaded through PromptLoader (the same sha256 gate a spawn
    uses), so an unsigned or edited prompt fails here at load and again at every spawn."""
    from prompts import PromptLoader, PromptNotApproved
    site_caps = SITE_CAPABILITIES if site_caps is None else site_caps
    rows = None if path or os.getenv("ROUTES_PATH") else site_env.section("routes")   # an explicit path wins
    if rows is not None:
        if os.path.exists(ROUTES_PATH):
            log.warning("routes: broker.yaml sets the routes, %s is ignored", ROUTES_PATH)
        path = site_env.BROKER_CONFIG
        if not isinstance(rows, list):
            raise RoutesConfigError(f"{path}: routes must be a list of rows")
        doc = {"routes": rows}
    else:
        path = path or ROUTES_PATH
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as e:
            raise RoutesConfigError(f"{path}: unreadable ({e})")
        try:
            doc = json.loads(raw)
        except ValueError as e:
            raise RoutesConfigError(f"{path}: not valid JSON ({e})")
    if not isinstance(doc, dict) or not isinstance(doc.get("routes"), list):
        raise RoutesConfigError(f'{path}: expected {{"routes": [...]}}')
    extra = {k for k in doc if not k.startswith("_")} - {"routes"}
    if extra:
        raise RoutesConfigError(f"{path}: unknown top-level key(s) {sorted(extra)}")
    loader = PromptLoader(prompts_dir or os.getenv("PROMPTS_DIR", os.path.join(_HERE, "prompts")),
                          approved_path or os.getenv("APPROVED_PROMPTS",
                                                     os.path.join(_HERE, "approved-prompts.json")))
    reserved_ids, seen, entries = _builtin_row_keys(), set(), []
    # every built-in tracker row, wherever it sits: a config row whose own signal one of them also matches would
    # be pre-empted (narrow substring rows ahead of the splice, possibly with MORE capability) or would take over a
    # built-in fallback trigger (@eph, @fleet, eph:general) after it.
    builtin_rows = [r for r in _profiles_to_rows(_build_profiles()) if r[0] == "vikunja:task"]
    for n, row in enumerate(doc["routes"]):
        where = f"{path}: routes[{n}]"
        if not isinstance(row, dict):
            raise RoutesConfigError(f"{where}: expected an object")
        rid = row.get("id")
        if isinstance(rid, str):
            where = f"{path}: route {rid!r}"
        extra = {k for k in row if not k.startswith("_")} - _CONFIG_ROUTE_KEYS
        if extra:
            raise RoutesConfigError(f"{where}: unknown key(s) {sorted(extra)}")
        missing = [k for k in ("id", "match", "prompt", "capability", "repo") if k not in row]
        if missing:
            raise RoutesConfigError(f"{where}: missing {missing}")
        if not isinstance(rid, str) or not _CONFIG_ROUTE_ID_RE.fullmatch(rid):
            raise RoutesConfigError(f"{where}: id must be lowercase letters, digits and dashes (max 40)")
        if rid in reserved_ids:
            raise RoutesConfigError(f"{where}: id clashes with a built-in row")
        if rid in seen:
            raise RoutesConfigError(f"{where}: duplicate id")
        seen.add(rid)
        match = row["match"]
        if not isinstance(match, dict) or not match or set(match) - {"label", "assignee"} or not all(
                isinstance(v, str) and v.strip() for v in match.values()):
            raise RoutesConfigError(f'{where}: match must be {{"label": "..."}}, {{"assignee": "..."}} or both')
        cap, site_cap = row["capability"], ""
        if isinstance(cap, str) and cap in site_caps:
            cap, site_cap = site_caps[cap]["base"], cap
        if not isinstance(cap, str) or cap not in CONFIG_ROUTE_CAPABILITIES:
            raise RoutesConfigError(f"{where}: unknown capability {cap!r} "
                                    f"(allowed: {', '.join(sorted(CONFIG_ROUTE_CAPABILITIES) + sorted(site_caps))})")
        m = _CONFIG_ROUTE_PROMPT_RE.fullmatch(row["prompt"]) if isinstance(row["prompt"], str) else None
        if not m:
            raise RoutesConfigError(f'{where}: prompt must be "<domain>/<class>" (prompts/<domain>/<class>.md)')
        domain, prompt_class = m.groups()
        if domain in CONFIG_ROUTE_RESERVED_DOMAINS:
            raise RoutesConfigError(f"{where}: prompt domain {domain!r} is reserved for a built-in row")
        try:
            loader.load(prompt_class, domain)
        except PromptNotApproved as e:
            raise RoutesConfigError(f"{where}: prompt not signed: {e}")
        except (OSError, UnicodeDecodeError) as e:
            raise RoutesConfigError(f"{where}: prompt file missing or unreadable ({e})")
        repo = row["repo"]
        if not isinstance(repo, str) or not os.path.isabs(repo):
            raise RoutesConfigError(f"{where}: repo must be an absolute path")
        if cap == "code" and not code_repo_allowed(repo):
            raise RoutesConfigError(f"{where}: repo {repo!r} is not in CODE_REPO_ALLOWLIST")
        if cap == "general" and not general_repo_allowed(repo):
            raise RoutesConfigError(f"{where}: repo {repo!r} is not in GENERAL_REPO_ALLOWLIST")
        if cap == "read" and not (code_repo_allowed(repo) or general_repo_allowed(repo)):
            raise RoutesConfigError(f"{where}: repo {repo!r} is in neither CODE_REPO_ALLOWLIST nor "
                                    "GENERAL_REPO_ALLOWLIST")
        for k in ("agent", "model"):
            if k in row and not (isinstance(row[k], str) and _CONFIG_ROUTE_NAME_RE.fullmatch(row[k])):
                raise RoutesConfigError(f"{where}: {k} must be a plain name")
        label, assignee = match.get("label", "").strip(), match.get("assignee", "").strip()
        probe = {"labels": [label] if label else [], "assignees": [assignee] if assignee else []}
        for _ns, _rp, classify_pred, other in builtin_rows:
            if classify_pred(probe):
                raise RoutesConfigError(f"{where}: match {match} is also matched by built-in row "
                                        f"{other.row_key!r}; pick a label/assignee no built-in row matches")
        for _ns, signal, other, factory in entries:
            if factory(signal)[1](probe):
                raise RoutesConfigError(f"{where}: match {match} is already taken by route {other.row_key!r} "
                                        "(earlier in the file; this row would never run)")
        tmpl = CONFIG_ROUTE_CAPABILITIES[cap]
        # agent + backend pin come from the template row, so fleet-model.sh and an operator's <ROW>_BACKEND pin
        # apply as they do to the built-in worker of that capability
        fields = dict(domain=domain, repo=repo, prompt_class=prompt_class, agent=row.get("agent", tmpl.agent),
                      capability=cap, row_id=rid, model_pin=row.get("model", ""), backend=tmpl.backend,
                      backend_from_mode=True, from_config=True, site_capability=site_cap)
        if cap != "read":
            fields.update(read_only=False, env=dict(tmpl.env), test_runner=tmpl.test_runner,
                          setting_sources=tmpl.setting_sources, mcp_config_path=tmpl.mcp_config_path,
                          publish={**tmpl.publish, "code_repo_path": repo, "branch_prefix": f"eph/{rid}"})
        route = Route(**fields)
        if label and assignee:
            entries.append(("vikunja:task", (assignee, label), route, _assignee_label_signal_predicates))
        elif assignee:
            entries.append(("vikunja:task", assignee, route, _assignee_signal_predicates))
        else:
            entries.append(("vikunja:task", label, route, _exact_label_signal_predicates))
    return entries


def _with_config_routes(rows, extra):
    """Splice config rows in AFTER every built-in narrow row and BEFORE the general fallbacks (the first
    _GENERAL_ROW entry, i.e. the @eph assignee row, then @fleet and the eph:general label). A built-in label or
    assignee still wins first-match; a config row wins over the catch-alls. No config rows -> `rows` itself."""
    if not extra:
        return rows
    i = next((n for n, e in enumerate(rows) if e[2] is _GENERAL_ROW), len(rows))
    return rows[:i] + list(extra) + rows[i:]


def assignee_dispatch_domains(evt):
    """The row domains the event's ASSIGNEES name via the assignee-tier PROFILES rows (@dev gives code,
    @eph gives general, @fleet gives tfa). echo_guard admits an agent handoff only if classify() lands in this set,
    i.e. the event routes to the row its assignee actually dispatches to (a narrow label row winning
    first-match still drops). Derived from PROFILES, so the env knobs stay the single source of truth."""
    members = {str(a).strip().lower() for a in _assignee_fields(evt)}
    out = set()
    for e in PROFILES:
        factory = e[3] if len(e) > 3 else None
        if factory is _assignee_signal_predicates and str(e[1]).strip().lower() in members:
            out.add(e[2].row_key)
        elif factory is _assignee_label_signal_predicates and str(e[1][0]).strip().lower() in members:
            out.add(e[2].row_key)          # the assignee half names it; classify() still needs the label
    return out


def _routing_fields(evt):
    """The assignee/label signal available on this event/brief (may be empty for collapsed briefs)."""
    assignees = evt.get("assignees") or []
    if isinstance(assignees, str):
        assignees = [assignees]
    labels = evt.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    return list(assignees) + list(labels)


def _label_signal_predicates(label):
    """Matcher FACTORY: given a trigger LABEL, return the (lenient route_pred, strict classify_pred) pair
    with EXACTLY the original ansible-row semantics, generalized so a second (third, ...) profile needs no
    new hand-written predicate function.

    route_pred: LENIENT, used ONLY by Router.route() when unstamped: no signal on `evt` gives True
    (namespace fallback: an unstamped test caller, or a legitimately-collapsed post-ingest brief that
    already lost its signal after ingest correctly classified it); else label present in
    assignees+labels gives True.

    classify_pred: STRICT, used ONLY by Router.classify() at INGEST admission (the allowlist):
    no signal on `evt` gives False (reject, e.g. a bare pre-assign `task.created` webhook,
    fired before the label/assignee is attached, must NOT be admitted); else label present gives True.

    Do not conflate the two: using the lenient predicate for ingest admission would wrongly admit a
    no-signal event; using the strict predicate for route() would break the unstamped fallback
    for unstamped/collapsed callers.

    An empty label (a row whose trigger knob is off) matches nothing: substring "" would match every event."""
    if not str(label).strip():
        return (lambda evt: False), (lambda evt: False)

    def route_pred(evt):
        fields = _routing_fields(evt)
        if not fields:
            return True                       # no stamp, no signal: route by namespace (fallback)
        return label.lower() in " ".join(str(x).lower() for x in fields)

    def classify_pred(evt):
        fields = _routing_fields(evt)
        if not fields:
            return False                      # no signal at ingest: do NOT admit
        return label.lower() in " ".join(str(x).lower() for x in fields)

    route_pred.namespace_fallback = True   # no signal = True; Router.route only lets that pick a read row
    return route_pred, classify_pred


def _profiles_to_rows(profiles):
    """Expand a PROFILES-shaped list into Router's `rows` shape (namespace, route_pred, classify_pred,
    Route), generating each row's matcher pair via its predicate FACTORY. An entry is
    (namespace, signal, Route), where factory defaults to `_label_signal_predicates` (so the ansible/wiki rows
    are byte-unchanged), OR (namespace, signal, Route, factory) to override it (the inbox row uses
    `_source_signal_predicates`). This is the seam that lets a new profile drop in as one declarative
    PROFILES entry, label-signalled or source-signalled."""
    rows = []
    for entry in profiles:
        ns, signal, tmpl = entry[0], entry[1], entry[2]
        factory = entry[3] if len(entry) > 3 else _label_signal_predicates
        rows.append((ns, *factory(signal), tmpl))
    return rows


# Back-compat: keep these two names importable at module level, bound to the ansible profile's
# generated predicates, so existing callers/tests (`from routing import _assigned_or_labelled_ansible`)
# keep working unchanged.
_assigned_or_labelled_ansible, _has_ansible_signal = _label_signal_predicates(ANSIBLE_ROW_LABEL)

# routes.json: built here, below every matcher factory, because load_config_routes() runs them at import.
SITE_CAPABILITIES = load_site_capabilities()
PROFILES = _with_config_routes(_build_profiles(), load_config_routes())


class Router:
    def __init__(self, rows=None, stamps=None, live=None):
        # rows: list of (namespace_prefix, route_predicate, classify_predicate, Route). Default = built
        # from PROFILES via _profiles_to_rows(). route_predicate is LENIENT
        # (no signal gives the namespace fallback, used by route() only when unstamped); classify_predicate is
        # STRICT (no signal gives a reject, used by classify() at ingest).
        self.rows = rows if rows is not None else _profiles_to_rows(PROFILES)
        # Shared subject_key to row-domain stamp map, written at ingest (raw event), read by route().
        # None (default) means no stamping (predicate fallback), which keeps direct-on_event callers working.
        self.stamps = stamps
        # key to bool: "is a worker live on this subject right now" (Coalescer.has_live_worker).
        # None (default) means comments are never forwarded (they are dropped).
        self.live = live

    def classify(self, key, evt):
        """Decide the route ROW on the RAW event at INGEST (where the assignee/label signal
        lives), returning the row's domain, or None when nothing matches. app.ingest uses None as the
        allowlist REJECT: only KNOWN rows are admitted; everything else is dropped at ingest.

        Uses each row's STRICT `classify_pred` (from `_label_signal_predicates`, not the lenient
        `route_pred`): a no-signal event (e.g. a bare pre-assign `task.created` webhook) must NOT be
        admitted here, or it spawns a bogus worker ahead of the real assign/label event.

        `task.comment.created` is handled FIRST, before the per-row allowlist even runs, so
        a comment that happens to carry the ansible label/assignee never re-triggers via row signal.
        It is admitted ONLY through comment_forward_admissible() (human doer, no broker-run marker,
        live worker) and then only via the subject's EXISTING stamp; everything else is rejected as before.
        See NON_TRIGGERING_EVENTS above for the incident writeup + gate.

        A subject that is CURRENTLY STAMPED (admitted at least once and not yet
        closed; see app.Broker.ingest / Coalescer's on_close pop) is a KNOWN row by definition. It is
        the SAME notion of "known" that route() already treats as authoritative. Before this fix,
        classify() re-derived the row from the RAW event's OWN signal on every single call, so a legit
        follow-up event for an already-admitted, still-open subject (e.g. an unassign, or any update whose
        delta payload happens to omit assignees/labels) was dropped as "not a known row" even though the
        subject demonstrably already routed (seen live: INGEST REJECT fired after the
        same key had already ROUTED and SPAWNED). Consulting the stamp here, AFTER the NON_TRIGGERING_EVENTS
        and forge gates above, so those gates are untouched, admits exactly that known-subject case
        without widening admission for any event that was never stamped in the first place (a
        bare pre-assign `task.created` still correctly rejects, since no prior admission means no stamp)."""
        # Forge-event allowlist (defense-in-depth). SCOPED to forge sources (github/gitlab):
        # a forge event only triggers on an issue-OPEN event_name by a listed author; any other forge event
        # (comment/edited/closed/labeled, PR events, an unlisted or missing author) is REJECTED first, before
        # the comment-forward branch below and before the source-signalled issue rows can match, so a
        # forge-shaped comment can't forward into a live worker and the broker does not depend on the external
        # tap alone. vikunja/gmail/mr events are NOT in _FORGE_SOURCES and are unaffected.
        if str(evt.get("source", "")).lower() in _FORGE_SOURCES \
                and (evt.get("event_name") not in ISSUE_TRIGGERING_EVENTS
                     or not issue_author_allowed(evt.get("doer"))):
            return None
        if evt.get("event_name") in NON_TRIGGERING_EVENTS:
            # comment.created never triggers; it is forward-only into a live worker, via its stamp.
            if not comment_forward_admissible(key, evt, self.live):
                return None
            return self.stamps.get(key) if self.stamps is not None else None
        # A subject already stamped (still open/pending; the stamp is popped at close, see
        # Coalescer's on_close) is admitted via its EXISTING stamp, regardless of whether THIS particular
        # raw event carries fresh assignee/label signal. Scoped tightly: only a key that was ALREADY
        # admitted once (and hasn't closed since) benefits; a brand-new/never-stamped key still needs
        # real signal from the per-row checks below.
        if self.stamps is not None:
            stamp = self.stamps.get(key)
            if stamp is not None:
                return stamp
        for ns, _route_pred, classify_pred, tmpl in self.rows:
            if key.startswith(ns) and classify_pred(evt):
                return tmpl.row_key          # equals domain except for rows that share one (infra/general)
        return None

    def route(self, key, evt):
        # If the row was stamped at ingest, it is authoritative (the raw-event signal was captured
        # there, before collapse() dropped it). Otherwise fall back to the per-row LENIENT predicate on
        # `evt` (no signal gives the namespace fallback); this keeps unstamped/direct-on_event test callers and
        # legitimately-collapsed post-ingest briefs routing as before; only classify()/ingest is strict.
        stamp = self.stamps.get(key) if self.stamps is not None else None
        for ns, route_pred, _classify_pred, tmpl in self.rows:
            if not key.startswith(ns):
                continue
            # An unstamped event with no label or assignee at all may only take a label/assignee row's namespace
            # fallback onto a read row: with the site's label rows off (neutral defaults) the first vikunja:task row
            # is the code row, and a blind fallback must not hand out write tools. The TFA launcher row is "read" by
            # default but starts a full agent, so it doesn't count. No read row means NoRoute
            # (dead in practice: ingest always stamps). Source rows match on their source, not a fallback.
            if (stamp is not None and tmpl.row_key == stamp) or (
                    stamp is None and route_pred(evt) and (not getattr(route_pred, "namespace_fallback", False)
                                                           or _routing_fields(evt)
                                                           or (tmpl.capability == "read" and tmpl.domain != "tfa"))):
                return tmpl.with_model()     # resolve model fresh (mode-aware) at route time
        raise NoRoute(f"no route for key={key} (no PROFILES row matches this namespace/signal)")


class NoRoute(Exception):
    pass
