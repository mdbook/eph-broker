#!/usr/bin/env python3
"""CCAdapter: spawns a real ephemeral `claude` (Claude Code) worker per subject.

Implements the 4 Adapter primitives and the 7 constraints below with their default values
(all hot-reload-tunable via config/). VALUE-FREE mechanism: no secrets, no prompt CONTENT
(prompts are loaded and sha256-gated by prompts.py; the CONTENT is owner-approved).

The 7 constraints:
  1. never `--bare`                    -> argv builder asserts `--bare` absent; always a system prompt.
  2. pin the CLI                       -> REMOVED on 2026-09-19; the pin is not enforced at all.
                                          The broker no longer checks or alerts on the
                                          installed `claude --version`; a routine CLI bump is a non-event.
  3. explicit --model                  -> from routing (fleet-model.sh), never a default/implicit model.
  4. charter-strip                     -> `--system-prompt` REPLACE + `--setting-sources ''` (no project/
                                          user CLAUDE.md) AND a charter-free worktree cwd (constraint 5).
  5. charter-free worktree cwd         -> worktree.py strips CLAUDE.md/AGENTS.md; cwd is that worktree.
  6. hard-kill                         -> max-turns 15 (flag; per-capability override, code=40),
                                          wall-clock 600s (Popen timeout->kill; per-capability override,
                                          code=1200),
                                          max-forwards/resumes 10 (counter).
  7. session rotation                  -> after 20 turns OR ~120k tokens: drop --resume, start fresh session.
"""
import html, inspect, json, os, queue, re, shlex, shutil, subprocess, threading, time, uuid, logging
from urllib.parse import urlsplit
from .base import Adapter
from envelope import parse_cli_json   # session-resume fix: recover the CLI wrapper's session_id on a parse-miss
from envelope import wrap_untrusted   # fence worker-derived blocked reasons in the alert DM
from envelope import reprompt_status_example   # capability-aware warm re-prompt example status
import stream                         # BROKER_STREAM_ENABLED gate for --session-id
from gitlab_mr import open_mr   # open the MR for a code proposer's pushed feature branch
import gitlab_mr                      # break_refs for GitLab-bound worker text
import events                         # outbound mr.opened (EVENTS_WEBHOOK_URL; no-op when unset)
from worktree import PushTargetRefused   # fail-closed push target (BROKER_PUSH_HOSTS only)
import tickets                        # ticket ref grammar (ref_from_key, vikunja_ref, vikunja_id)
from trackers import Registry         # ticket reads and write-back by ref (docs/trackers.md)
# The done predicate lives in the Vikunja tracker now; re-exported here, where the tests import it from
from trackers.gitlab import GitLabTracker   # GitLab issue notes (write-back only; intake is forge.py)
from trackers.vikunja import VikunjaTracker, _TICKET_DONE_RE, _ticket_text_is_done   # noqa: F401
from worktree import CutShaMismatch      # Review worktree could not be cut at the MR head
import inbox_accounts   # spawn-time fail-closed account gate (inbox_fact_block / UnknownInboxAccount)
import routing   # general worker: spawn-time repo-allowlist gate (general_repo_allowed)
import repo_catalog  # Repo catalog (GitLab listing + clones) behind REPO_INFER_ENABLED
import repo_infer    # Repo inference ladder (name match, then Laya, then 4B, then ask)
import write_grant   # Deterministic infra live-write grants (mcp-rw.sh) + their journal audit lines

log = logging.getLogger("eph-broker.cc")


# MR-reviewer: box-local scratch root for the throwaway review worktree (checked out at the MR
# SOURCE branch). Off the broker host's home, NOT the $TMPDIR/=/tmp default that fills root. Same env knob as
# routing.MR_REVIEW_WORKTREE_BASE so the two agree; read here because _run_worker cuts the worktree.
MR_REVIEW_WORKTREE_BASE = os.getenv("MR_REVIEW_WORKTREE_BASE", os.path.expanduser("~/.cache/eph-mr-worktrees"))
# Zulip-wake chat reply: Zulip caps a message at 10000 chars; the fallback covers a turn with no envelope.
CHAT_REPLY_MAX_CHARS = 9000
CHAT_FALLBACK_REPLY = ("Sorry, I couldn't finish that one (the worker produced no usable reply). "
                       "Ops has been notified; try again in a bit.")
# A chat worker that hits max-forwards or dies mid-conversation tells the sender (via the same
# guarded broker reply path) instead of going silent.
CHAT_LIMIT_REPLY = ("I've hit my limit for this conversation, so I've stopped here. "
                    "DM me again to start fresh.")

# Postmortem capture of a FAILED turn (non-zero exit or parse miss). The raw stdout/stderr of that turn
# is written, size-capped, under the broker state dir. Explicit TURN_CAPTURE_DIR wins; else it sits beside an
# EXPLICITLY-configured EVENTS_LOG (same rule as coalescer.COMPLETED_PATH); else "" == disabled (tests / shadow
# selftest never touch disk). ONLY the turn's own stdout/stderr + non-secret metadata are written; never the
# worker env, argv, or any secret value; secret-shaped text in the streams is redacted.
TURN_CAPTURE_DIR = os.getenv("TURN_CAPTURE_DIR") or (
    os.path.join(os.path.dirname(os.environ["EVENTS_LOG"]), "turn-captures") if os.getenv("EVENTS_LOG") else "")
_TURN_CAPTURE_DEFAULT_CHARS = 65536


def _int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


TURN_CAPTURE_MAX_CHARS = _int_env("TURN_CAPTURE_MAX_CHARS", _TURN_CAPTURE_DEFAULT_CHARS)   # per stream
# The capture dir is pruned after every write: files older than TURN_CAPTURE_MAX_AGE_S are dropped,
# then only the newest TURN_CAPTURE_KEEP remain. A flapping worker can never fill the state disk.
TURN_CAPTURE_KEEP = _int_env("TURN_CAPTURE_KEEP", 200)
TURN_CAPTURE_MAX_AGE_S = _int_env("TURN_CAPTURE_MAX_AGE_S", 7 * 24 * 3600)

# secret-shaped text redacted from a capture (a hermes tool result, a traceback echoing an env var...).
# Belt-and-braces: the streams should hold no secret, but a postmortem file must never be where one lands.
# ReDoS: py3.9 `re` holds the GIL, so a super-linear pattern on a 64K adversarial stream stalls the WHOLE
# broker. Every pattern that must backtrack starts only at a run boundary (lookbehind) and has bounded runs.
_SECRETISH_RES = (
    re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----.*?(?:-----END [A-Z ]{0,40}PRIVATE KEY-----|\Z)", re.S),
    re.compile(r"\b(?:glpat|gldt|glrt|ghp|gho|ghs|ghu|github_pat|xox[abprs]|sk|hf)[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)\b(bearer|basic)\s{1,8}[A-Za-z0-9._~+/=\-]{12,}"),
)
# KEY=value / "key": "value" / key: value where the key NAME looks secret -> the value is redacted, name kept.
# Limitation: key names are capped at 64 chars either side of the keyword; a longer name goes unredacted.
# token-COUNT metric keys (input_tokens, cache_*_tokens, ...) are an explicit allowlist and never match:
# redacting their ints made every captured stdout JSON unparseable and hid the counts.
_SECRET_KV_RE = re.compile(
    r"""(?ix)(?<![A-Za-z0-9_.\-"'])
          (?!["']?(?:input|output|max|total|cache_(?:read|creation)(?:_input)?)_tokens["']?\s{0,8}[:=])
          (["']?[A-Za-z0-9_.\-]{0,64}(?:token|secret|passw(?:or)?d|passwd|api[_-]?key|apikey|credential|private[_-]?key|auth)
          [A-Za-z0-9_.\-]{0,64}["']?\s{0,8}[:=]\s{0,8})(["']?)([^\s"',}]{4,})""")


def _redact_secretish(text):
    """Replace secret-shaped substrings with a marker. Pure; never raises on str input."""
    s = "" if text is None else str(text)
    for rx in _SECRETISH_RES:
        s = rx.sub("[redacted]", s)
    return _SECRET_KV_RE.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", s)


def _cap_tail(text, cap):
    """Keep the LAST `cap` chars of `text` (the error / final envelope sits at the end), marking a cut.
    Cap <= 0 (e.g. TURN_CAPTURE_MAX_CHARS=0) means the DEFAULT cap, never "uncapped" (s[-0:] == s)."""
    cap = cap if isinstance(cap, int) and cap > 0 else _TURN_CAPTURE_DEFAULT_CHARS
    s = "" if text is None else str(text)
    return s if len(s) <= cap else f"[truncated {len(s) - cap} chars]\n" + s[-cap:]


# only files capture_failed_turn itself names (<utc-ts>-<run_id>-t<turn>.txt): an explicit TURN_CAPTURE_DIR
# pointed at a shared dir must never lose someone else's *.txt
_CAPTURE_NAME_RE = re.compile(r"^\d{8}T\d{6}Z-[A-Za-z0-9._-]{1,80}-t\d+\.txt$")


def _prune_captures(d, keep=None, max_age_s=None, now=None):
    """Drop capture files older than `max_age_s`, then keep only the newest `keep`. Best-effort."""
    keep = TURN_CAPTURE_KEEP if keep is None else keep
    max_age_s = TURN_CAPTURE_MAX_AGE_S if max_age_s is None else max_age_s
    keep = keep if keep > 0 else 200                       # <= 0 is the default, never "rotation off"
    max_age_s = max_age_s if max_age_s > 0 else 7 * 24 * 3600
    now = time.time() if now is None else now
    files = []
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if _CAPTURE_NAME_RE.match(name) and os.path.isfile(p) and not os.path.islink(p):
                files.append((os.stat(p).st_mtime, p))
        except OSError:
            continue
    files.sort(reverse=True)                              # newest first
    for i, (mtime, p) in enumerate(files):
        if now - mtime > max_age_s or i >= keep:
            try:
                os.unlink(p)
            except OSError:
                pass


def capture_failed_turn(worker, route, reason, rc, out, err, capture_dir=None, max_chars=None):
    """Write a failed turn's raw stdout/stderr (each tail-capped at `max_chars`, secret-shaped text
    redacted) to <capture_dir>/<utc-ts>-<run_id>-t<turn>.txt (mode 0600) for postmortem, then prune the dir.
    Returns the path, or None when capture is disabled or the write fails; a capture must NEVER break or
    wedge the turn."""
    d = TURN_CAPTURE_DIR if capture_dir is None else capture_dir
    cap = TURN_CAPTURE_MAX_CHARS if max_chars is None else max_chars
    if not d:
        return None
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(getattr(worker, "run_id", "") or "run"))[:80]
        name = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{safe}-t{getattr(worker, 'turns', 0)}.txt"
        path = os.path.join(d, name)
        body = (f"key: {getattr(worker, 'key', '?')}\nrun_id: {getattr(worker, 'run_id', '?')}\n"
                f"adapter: {getattr(route, 'adapter', 'cc')}\ncapability: {getattr(route, 'capability', '?')}\n"
                f"reason: {reason}\nreturncode: {rc}\n"
                f"--- stdout ---\n{_redact_secretish(_cap_tail(out, cap))}\n"
                f"--- stderr ---\n{_redact_secretish(_cap_tail(err, cap))}\n")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(body)
        log.info("failed turn captured key=%s -> %s", getattr(worker, "key", "?"), path)
        try:
            _prune_captures(d)
        except Exception as e:                           # noqa: BLE001 - pruning is best-effort
            log.warning("failed-turn capture prune %s failed: %s", d, e)
        return path
    except Exception as e:                               # noqa: BLE001 - postmortem is best-effort
        log.warning("failed-turn capture key=%s failed: %s", getattr(worker, "key", "?"), e)
        return None


# ============================================================================================
# Run a route's worker on a NON-default inference backend.
# ============================================================================================
# A Route may declare backend="deepseek"/"glm" so the ephemeral fleet grinds on NON-Anthropic budget
# (deepseek/glm via a LiteLLM proxy to OpenRouter, 1M ctx). The full launch recipe for a backend lives in the
# SHARED spawn registry /etc/site/spawn-models.json, the SAME file the spawn scripts read, so there is
# ONE source of truth. load_backend() turns a backend name into (model, env-overrides, effort) mirroring
# the spawn script's cloud recipe (base_url + 1M ctx + all-tier small-model routes + attribution) PLUS the
# secret auth token, which the BROKER (a local process) sources from the backend's env file at spawn,
# referenced BY PATH, never hard-coded, never in cloud context. Unknown/empty backend -> (None,{},None) ->
# the caller keeps the fleet-model.sh model + default Anthropic env (fail-SAFE, never a silent break);
# EXCEPT a ROW that explicitly selected a non-Anthropic backend: that fails CLOSED at dispatch (see _one_turn).
SPAWN_MODELS_JSON = os.getenv("SPAWN_MODELS_JSON", "")   # site.env; unset -> no non-default backends

# A `kind:"local"` backend (vLLM-Main, vLLM-Fast) is measurably SLOWER
# than hosted Claude on the same prompt, so the cloud-tuned per-capability wall_clock_s hard-kills a
# local turn that is still making progress. LOCAL_WALL_CLOCK_MULT widens the effective wall-clock for a
# turn whose RESOLVED backend is local (never a hosted turn; see _is_local_backend / _one_turn).
LOCAL_WALL_CLOCK_MULT = _float_env("LOCAL_WALL_CLOCK_MULT", 4.0)

# After a wall-clock hard-kill, the warm re-prompt (_turn_with_
# envelope) used to spend a SECOND full (now possibly LOCAL_WALL_CLOCK_MULT-scaled) allotment asking the
# worker for a clean envelope; on a wedged local backend that sat 10+ minutes holding a concurrency slot
# and blocked an ansible broker deploy. HARDKILL_REPROMPT_CAP_S bounds just that ONE re-prompt turn (never
# the worker's normal turns) so a killed worker releases its slot promptly either way.
HARDKILL_REPROMPT_CAP_S = _float_env("HARDKILL_REPROMPT_CAP_S", 60.0)


# ============================================================================================
# Usage-cap fallback chain. DEFAULT OFF; enable with USAGE_CAP_FALLBACK_ENABLED=1.
# ============================================================================================
# On 2026-09-19 every inbox-triage worker died byte-identically on Anthropic's "You've hit your weekly
# limit" (0-in/0-out tokens, model <synthetic>). This wraps a worker's SEED dispatch in a bounded backend
# fallback chain: native Anthropic (the row default) -> USAGE_CAP_FALLBACK_CHAIN (the site's rungs, in order) ->
# LOUD FAILURE. Each backend is tried AT MOST ONCE, in order.
#
# ⚠️ RELEASE-GATE: this ships DEFAULT-OFF / explicit opt-in. The deepseek + vLLM rungs run on
# NON-Anthropic budget ($$), gated by the live "GENERAL_BACKEND stays UNSET" release gate. When
# USAGE_CAP_FALLBACK_ENABLED is off (the default) behavior is EXACTLY today's: a single Anthropic dispatch,
# no proactive usage-file read, no re-dispatch; the capability is dormant until someone opts in. Enabling it
# COSTS non-Anthropic budget whenever it fires. This wrapper does NOT change any row's default backend= and
# does NOT set/require GENERAL_BACKEND.
def _env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

USAGE_CAP_FALLBACK_ENABLED = _env_flag("USAGE_CAP_FALLBACK_ENABLED", "0")   # master switch, DEFAULT OFF
# ordered comma-separated fallback backend NAMES, consulted ONLY when the master flag is on. No default: the
# rungs are registry names, so the site lists them (site.env); even set, nothing happens unless USAGE_CAP_FALLBACK_ENABLED.
USAGE_CAP_FALLBACK_CHAIN = os.getenv("USAGE_CAP_FALLBACK_CHAIN", "")   # registry backend names, comma list; unset = no rungs
# proactive signal: the fleet usage snapshot the heartbeat writes. Shape:
# {"five_hour_pct":N,"seven_day_pct":N,"five_hour_resets_at":ts,"seven_day_resets_at":ts,"wrote_at":ts}.
FLEET_USAGE_FILE = os.getenv("FLEET_USAGE_FILE", "")   # unset = no proactive usage read (reactive cap detection only)
# at/above these percentages the Anthropic weekly/5h budget is treated as exhausted -> skip the doomed
# Anthropic attempt and dispatch straight on the first fallback. Defaults: 100 (fully capped) for both.
USAGE_CAP_SEVEN_DAY_THRESHOLD = float(os.getenv("USAGE_CAP_SEVEN_DAY_THRESHOLD", "100"))
USAGE_CAP_FIVE_HOUR_THRESHOLD = float(os.getenv("USAGE_CAP_FIVE_HOUR_THRESHOLD", "100"))
# local vLLM CC recipe env-file (base_url + auth token + ctx, same keys spawn writes). base_url + the auth
# token always come from THIS file, referenced BY PATH, never echoed. The MODEL, though, comes from the
# registry entry's own `model` field when set (each of vLLM-Main/vLLM-Fast pins its own route id); the file's
# ANTHROPIC_DEFAULT_OPUS_MODEL is only a FALLBACK for a registry entry that omits `model`. Both `vLLM-Main`
# and `vLLM-Fast` read the SAME env file (they share one litellm virtual key), so without a per-entry `model`
# they'd silently resolve to whichever model the file happens to pin (a bug seen live:
# vLLM-Main ran vLLM-Fast's model). Override the file path with VLLM_CC_ENV. If a local registry entry ever
# grows its own `env` pointer, that wins over this default. Default is the BROKER's OWN litellm virtual key
# (per-consumer sibling of the other `.vllm-cc-env-*` files; minted separately), NOT the generic
# `/etc/site/.vllm-cc-env`, which doesn't exist. Set in site.env; when unset the local rungs are unresolvable and skipped.
VLLM_CC_ENV = os.getenv("VLLM_CC_ENV", "")

# Anthropic usage-cap phrases in a capped worker's --output-format json result text (lowercased match).
_USAGE_CAP_PHRASES = ("hit your weekly limit", "hit your usage limit", "weekly limit will reset")

# sentinel so _one_turn(backend_override=...) distinguishes "no override, use route.backend" (today's
# behavior, byte-identical) from an EXPLICIT override to native Anthropic ("" / "claude").
_NO_OVERRIDE = object()


def _detect_usage_cap(raw):
    """True IFF a worker's raw `--output-format json` output shows the Anthropic usage cap was hit: the
    weekly/usage-limit STRING, OR the 0-input/0-output-tokens + <synthetic>-model SHAPE the CLI emits on a
    capped run. Pure + fail-safe: a falsy/garbage input -> False (never a false cap that needlessly burns
    non-Anthropic budget)."""
    if not raw:
        return False
    s = str(raw)
    if any(p in s.lower() for p in _USAGE_CAP_PHRASES):
        return True
    if "<synthetic>" in s:                                 # the CLI's capped-run sentinel model id
        try:
            _, _, tokens = parse_cli_json(s)
        except Exception:
            tokens = None
        if tokens == 0:
            return True
    return False


def _detect_turn_cap(raw):
    """True IFF a worker's raw `--output-format json` output shows the `--max-turns` cap was hit.
    The CLI's result wrapper carries `"subtype": "error_max_turns"` (+ `"terminal_reason": "max_turns"`, no
    `result` text); verified live against the claude CLI 2026-09-26. Pure and fail-safe: garbage gives False."""
    try:
        w = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return False
    return isinstance(w, dict) and (w.get("subtype") == "error_max_turns" or w.get("terminal_reason") == "max_turns")


# A transient Anthropic API failure (429 rate-limit / 529 overloaded) makes a turn fail with 0 tokens and no
# envelope. That is NOT a parse miss: back off + retry the SAME turn instead of spending the warm re-prompt, and on
# exhaustion leave the subject UN-stamped (re-armable). Delays (seconds, comma-separated) are env-tunable.
_API_OVERLOAD_STATUSES = (429, 529)
API_OVERLOAD_RETRY_DELAYS_S = tuple(
    float(x) for x in os.getenv("API_OVERLOAD_RETRY_DELAYS_S", "120,300,600").split(",") if x.strip())


def _detect_api_overload(raw):
    """True IFF a worker's raw `--output-format json` wrapper is a transient API failure: `is_error` (or
    `terminal_reason == "api_error"`) with `api_error_status` in {429, 529}. Other statuses (401/403/400) and
    garbage -> False, so they keep the parse-miss -> warm re-prompt -> escalation path."""
    try:
        w = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return False
    if not isinstance(w, dict):
        return False
    if not (w.get("is_error") is True or w.get("terminal_reason") == "api_error"):
        return False
    try:
        return int(w.get("api_error_status")) in _API_OVERLOAD_STATUSES
    except (TypeError, ValueError):
        return False


def _gate_denials_html(denials, cap=20):
    """The hermes eph-gate denials of a turn as an HTML block for the result comment. Every field is
    worker-influenced, so all of it is html-escaped; the sha is shown in full (byte-exact, from gate-hook.py) because
    it is the allow-once token filename the gate mints. kind tells a reader WHY: floor | unmapped-tool | judge."""
    rows = []
    for d in list(denials)[:cap]:
        sha = d.get("sha")
        rows.append("<li><code>%s</code> [%s] %s%s</li>" % (
            html.escape(str(d.get("tool") or "")), html.escape(str(d.get("kind") or "")),
            html.escape(str(d.get("reason") or "")[:300]),
            " - allow-once sha <code>%s</code> (ops mints)" % html.escape(sha) if sha else ""))
    more = " (+%d more)" % (len(denials) - cap) if len(denials) > cap else ""
    return "<p><b>eph-gate denied %d call(s)%s</b> (deny-and-report):</p><ul>%s</ul>" % (
        len(denials), more, "".join(rows))


def _gitlab_issue_ref(worker):
    """The `gitlab:group/app#7` ref when this worker's subject is a GitLab issue, else None. GitLab issue briefs
    carry no task_id, so their ticket is the subject key itself."""
    key = getattr(worker, "key", None)
    return key if tickets.tracker_of(key) == "gitlab" else None


def _ops_ref(key):
    """The Vikunja ref for a key, else None. Push-refused, WIP, patch and runner notices name host paths and
    settings, so they go on a Vikunja ticket (the operator's) and never on a GitLab issue, which may be public."""
    return tickets.vikunja_ref(tickets.vikunja_id(key))


# A worker summary quoted in an MR description must not close, link or ping anything: gitlab_mr.break_refs
# breaks its references (GitLab reads closing patterns in a description) and gitlab_mr._api defuses quick
# actions and @mentions.
MR_SUMMARY_MAX_CHARS = 4000


def _mr_description(key, branch, target, summary):
    """The MR description for a GitLab-issue subject: open_mr's default text, `Closes <project>#<iid>`, and the
    worker's latest summary as a quote. None for any other subject, so open_mr keeps its default text."""
    if tickets.tracker_of(key) != "gitlab":
        return None
    issue = key.split(":", 1)[1]                  # group/app#7, a full-path reference
    text = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"(?i)<br\s*/?>|</p>|</li>", "\n", str(summary or ""))))
    text = gitlab_mr.break_refs(text).strip()[:MR_SUMMARY_MAX_CHARS]
    quoted = "\n".join(f"> {line}" for line in text.splitlines()) or "> (no summary)"
    return (f"Automated code-change proposal from the ephemeral code worker (key={key}).\n\n"
            f"Branch `{branch}` -> `{target}`. Review the diff and merge if good; "
            f"this MR was opened by the broker but **nothing merges without a human**.\n\n"
            f"Closes {issue}\n\nWorker summary (written by the worker, not checked by the broker):\n\n{quoted}")


def _plain_reason(summary, cap=300):
    """Worker summary HTML to a short plain-text reason for the one-line alert DM (tags stripped,
    whitespace collapsed, capped). Still worker-derived, so the caller fences it with wrap_untrusted."""
    s = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", str(summary or "")))).strip()
    return s[:cap] + ("…" if len(s) > cap else "")


def _usage_over_threshold():
    """Proactive pre-check: read FLEET_USAGE_FILE; True if seven_day_pct OR five_hour_pct is at/above its
    threshold (Anthropic budget effectively exhausted -> skip the doomed Anthropic attempt). FAIL-OPEN: a
    missing/unreadable/garbage file gives False ('no signal, proceed normally'); never crash dispatch because
    the file is absent (the whole point of the reactive detector is to cover a stale/absent file)."""
    if not FLEET_USAGE_FILE:
        return False
    try:
        with open(FLEET_USAGE_FILE) as f:
            u = json.load(f)
    except (OSError, ValueError):
        return False
    try:
        seven = float(u.get("seven_day_pct", 0) or 0)
        five = float(u.get("five_hour_pct", 0) or 0)
    except (TypeError, ValueError):
        return False
    return seven >= USAGE_CAP_SEVEN_DAY_THRESHOLD or five >= USAGE_CAP_FIVE_HOUR_THRESHOLD


def resolve_default_branch(repo_path, fallback="main"):
    """The repo's own default branch, so the MR target ref can't be wrong for a repo that doesn't use `main`.

    The publish config's `mr_target_ref` defaults to the literal "main", but fleet repos differ: the infra
    repo (owner/docker) is on `master`. A hardcoded `main` made every worker targeting such a repo die at the
    worktree cut with `git fetch origin main` failing with exit 128 (2026-09-17: the repo routing worked and
    delivered the worker to the infra repo, which then failed on the ref). A single global env var can't serve both,
    so resolve it from the repo itself. Order: origin/HEAD (authoritative remote default) -> the checked-out
    branch -> `fallback`. Never raises: a repo we can't introspect gets the old behaviour."""
    try:
        out = subprocess.run(["git", "-C", repo_path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().split("/", 1)[-1]        # "origin/master" -> "master"
    except Exception:
        pass
    try:
        out = subprocess.run(["git", "-C", repo_path, "symbolic-ref", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return fallback


def _parse_env_file(path):
    """{KEY: VALUE} for simple KEY=VALUE lines in an env file. Limitation: no shell, no export/quote eval;
    the backend auth-env files are token-only KEY=VALUE. Missing/unreadable gives {} (caller fails safe)."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


# The ONLY keys load_backend forwards out of a backend's secret auth-env file. A CC worker
# authenticates to a non-default backend's base_url with an Anthropic-style auth token; nothing else in that
# file is a var a worker needs. Keeping this tight stops an unrelated secret in the env file from re-widening
# the scrubbed worker env (see load_backend / scrub_worker_env).
_BACKEND_ENV_KEEP = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")

# Keys a LOCAL (vLLM) backend's CC recipe env-file legitimately contributes to a worker. Unlike a
# cloud backend (base_url/model live in spawn-models.json), a local backend's base_url + model come FROM
# this env-file, so the keep-list is wider than _BACKEND_ENV_KEEP, but STILL a fixed allowlist, never the
# whole file (same scrub discipline: an unrelated secret ever added to the file cannot re-widen the worker
# env). base_url + at least the opus model route are REQUIRED for the local backend to resolve.
_LOCAL_ENV_KEEP = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                   "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                   "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                   "CLAUDE_CODE_ATTRIBUTION_HEADER")


def _load_local_backend(b):
    """Resolve a `kind:"local"` (vLLM) registry entry into (model, env_overrides, effort)
    by sourcing the local CC recipe env-file (VLLM_CC_ENV, or the entry's own `env` if it ever grows one)
    for base_url + auth token. The MODEL is the registry entry's own `model` when set, NOT the file's
    ANTHROPIC_DEFAULT_OPUS_MODEL, which is only a fallback for an entry that omits `model`. This matters
    because vLLM-Main and vLLM-Fast share ONE env file: without a per-entry `model` they'd both resolve to
    whichever model the file happens to pin, silently ignoring which backend was actually asked for
    (live-verified bug: vLLM-Main ran vLLM-Fast). When the registry supplies the model, also PIN it as
    ANTHROPIC_DEFAULT_OPUS_MODEL/ANTHROPIC_DEFAULT_SONNET_MODEL in the returned env: CC's own tier routing
    (auto-mode "opus"/"sonnet" turns) otherwise falls back to those file-set env vars for the ACTUAL model it
    dispatches to, which would ignore the explicit --model this function's return value drives (build_argv
    always passes an explicit `--model`, but CC re-derives the model per-turn from these env defaults for its
    own internal tier classification, so leaving them at the file's value re-introduces the same cross-talk
    for any turn CC re-routes internally). ANTHROPIC_DEFAULT_HAIKU_MODEL is left as the file sets it; HAIKU
    is a small-model/title-call tier shared across both local backends by design, not a decision routed by
    which backend the turn's main model resolves to. FAIL-SAFE: an unreadable file / missing base_url or
    model gives (None, {}, None), so the fallback chain SKIPS an unresolvable vLLM rung rather than silently
    running on capped Anthropic. Referenced BY PATH; no secret byte is echoed. Limitation: depends on
    VLLM_CC_ENV carrying exactly the spawn recipe keys (verified by KEY NAME only, 2026-09-20); ceiling: if
    that file's shape drifts, the rung fails safe (skipped), never crashes."""
    env_path = b.get("env") or VLLM_CC_ENV
    kept = {k: v for k, v in _parse_env_file(env_path).items() if k in _LOCAL_ENV_KEEP}
    base = kept.get("ANTHROPIC_BASE_URL")
    model = b.get("model") or kept.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
    if not base or not model:
        return None, {}, None
    if b.get("model"):
        # the registry entry pinned its OWN model route: make sure CC's tier routing can't fall back to the
        # (shared-file) opus/sonnet defaults and cross-talk onto the OTHER local backend's model.
        kept["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
        kept["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
    if b.get("context_tokens") and "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in kept:
        kept["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(b["context_tokens"])
    return model, kept, b.get("effort")


def _backend_entry(name):
    """Raw dict entry for `name` in SPAWN_MODELS_JSON, or None if empty/unknown/unreadable. The ONE place
    that opens the registry file; shared by load_backend and _is_local_backend so they can never
    disagree about what a given backend name resolves to."""
    if not name:
        return None
    try:
        with open(SPAWN_MODELS_JSON) as f:
            return json.load(f).get("backends", {}).get(name)
    except (OSError, ValueError):
        return None


def _is_local_backend(name):
    """True iff `name` resolves to a `kind:"local"` registry entry
    (today vLLM-Main, vLLM-Fast): the slow-model rung _one_turn/hermes.one_turn widen the wall-clock for.
    FAIL-SAFE: an empty/unknown/unresolvable name is never local, so a typo or an unreadable registry can
    only ever leave a turn at its UNSCALED (shorter) wall-clock, never silently stretch one that isn't local."""
    b = _backend_entry(name)
    return bool(b) and b.get("kind") == "local"


def load_backend(name):
    """Resolve a spawn-registry backend name -> (model, env_overrides, effort), or (None, {}, None) if the
    name is empty/unknown/not a usable cloud backend. env_overrides carry base_url + 1M ctx + all-tier
    small-model routes + attribution + the auth token sourced from the backend's env file."""
    b = _backend_entry(name)
    if b and b.get("kind") == "local":                     # vLLM rung: recipe from the env-file
        return _load_local_backend(b)
    if not b or b.get("kind") != "cloud" or not b.get("model") or not b.get("base_url"):
        return None, {}, None
    model = b["model"]
    env = {
        "ANTHROPIC_BASE_URL": b["base_url"],
        # map EVERY tier to the one pinned route so CC's small-model (title/haiku) calls hit a litellm-known id
        "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
    }
    if b.get("context_tokens"):
        env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(b["context_tokens"])
    if "attribution_header" in b:
        env["CLAUDE_CODE_ATTRIBUTION_HEADER"] = str(b["attribution_header"])
    if b.get("env"):                       # source the backend's secret auth-env (token-only): path, not value
        # Forward ONLY the auth key(s) a worker needs, NOT the whole env file. This lands
        # in the worker env as `backend_env` on TOP of scrub_worker_env's allowlist (cc.scrub_worker_env
        # `env.update(backend_env)`), so slurping the whole file would silently RE-WIDEN the very env the
        # scrub just narrowed: any unrelated secret that ever gets added to the backend env file would leak
        # straight into the worker. Filtering here keeps load_backend from defeating the scrub.
        parsed = _parse_env_file(b["env"])
        env.update({k: v for k, v in parsed.items() if k in _BACKEND_ENV_KEEP})
    return model, env, b.get("effort")


# ============================================================================================
# SCRUB the worker env to an allowlist.
# ============================================================================================
# The worker previously got `os.environ.copy()`, the BROKER's FULL env, which carries GITLAB_MR_TOKEN and
# BROKER_HMAC_SECRET. Under a bypass / off-Anthropic worker fed UNTRUSTED ticket text that is an exfil risk.
# The worker needs NEITHER: the broker does publish_mr/open_mr (gitlab_mr.py reads GITLAB_MR_TOKEN broker-side)
# and the HMAC check (app.py) ITSELF; verified: no worker-path code reads GITLAB_MR_TOKEN or the HMAC secret.
# So build the worker env from an ALLOWLIST of what a worker legitimately needs and DROP everything else. Any
# *TOKEN*/*SECRET*/*KEY* the broker holds is dropped by construction (none is on the keep-list). The auth a
# worker DOES need rides route.env (ZBUS_ENV) + backend_env (a non-default backend's base_url/token/model-
# routes); default-Anthropic auth is file-based (~/.claude via HOME), not an env token.
_WORKER_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TZ", "TMPDIR", "PYTHONPATH")
_WORKER_ENV_KEEP_PREFIX = ("LC_", "XDG_")


# The mcp.sh wrapper dir (+ the absolute-path rule forms below) comes from site.env BROKER_MCP_SH, the zbus
# binary from NOTIFY_ZBUS. Unset or relative: no exact absolute form and no PATH prepend; the config-independent
# `Bash(*/<name> *)` wildcard denies stay regardless, so a missing site.env never drops the absolute-path floor.
# site_env is loaded by the `from site_env import` above.
_MCP_SH = os.getenv("BROKER_MCP_SH", "")
_MCP_DIR = os.path.dirname(_MCP_SH) if os.path.isabs(_MCP_SH) else ""
_ZBUS_BIN = os.getenv("NOTIFY_ZBUS", "")


def _mcp_abs(*names):
    """DENY forms for an mcp wrapper called by path: the exact site dir (if known) + `*/<name> *` for any dir."""
    return ([f"Bash({_MCP_DIR}/{n}:*)" for n in names] if _MCP_DIR else []) + [f"Bash(*/{n} *)" for n in names]


def scrub_worker_env(base, route_env=None, backend_env=None, mcp_dir=None):
    """Build a worker's env from the allowlist above (+ LC_*/XDG_* by prefix), then layer route.env and
    backend_env (what a worker legitimately needs) ON TOP, and prepend the mcp.sh dir (default _MCP_DIR; "" =
    none) to PATH so it wins.
    Pure + unit-testable: `base` is a dict (os.environ at the call site). Broker secrets (GITLAB_MR_TOKEN /
    BROKER_HMAC_SECRET / any *TOKEN*/*SECRET*/*KEY*) are absent because they are not on the keep-list."""
    env = {k: base[k] for k in _WORKER_ENV_KEEP if k in base}
    env.update({k: v for k, v in base.items()
                if any(k.startswith(p) for p in _WORKER_ENV_KEEP_PREFIX)})
    env.update(route_env or {})
    env.update(backend_env or {})                    # non-default backend base_url/token/ctx/model-routes WIN
    mcp_dir = _MCP_DIR if mcp_dir is None else mcp_dir
    if mcp_dir:
        env["PATH"] = f"{mcp_dir}:" + env.get("PATH", base.get("PATH", ""))   # mcp.sh dir wins
    # Never load the claude.ai account connectors in a worker. Each `claude -p` turn is a
    # fresh process, and with connectors on, every turn opened a new MCP session on all ~20 agentgateway
    # routes (Anthropic egress, 0 tool calls, never closed). That was the initialize/422 churn.
    # No worker uses them: MCP reaches workers via mcp.sh over Bash or a strict --mcp-config. Set last, so
    # a route/backend env can't turn it back on. The fleet's interactive sessions already do the same.
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    # an empty or relative PYTHONPATH component resolves against the worker cwd (the worktree): a worker-written
    # module there would be imported by every python the worker env starts . Keep absolute ones only.
    if "PYTHONPATH" in env:
        parts = [p for p in str(env["PYTHONPATH"]).split(os.pathsep) if p and os.path.isabs(p)]
        if parts:
            env["PYTHONPATH"] = os.pathsep.join(parts)
        else:
            del env["PYTHONPATH"]
    # Same class as PYTHONPATH above: an empty or relative PATH component resolves a bare-name command
    # (a hermes terminal `git`, a CC Bash bare-name call) against the worker cwd, so a worker-written ./git
    # would win. Keep absolute parts only (mcp_dir is always absolute, so it survives). Hardening only: the
    # live broker PATH is clean today.
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if p and os.path.isabs(p))
    return env


# ============================================================================================
# Capability to ENFORCED tool grants.
# ============================================================================================
# Before this, Route.capability was DECLARATIVE ONLY: build_argv emitted no --permission-mode and no
# allow/deny tool lists, so "read-only" rested entirely on prompt text and a throwaway worktree. Prompt text alone is
# NOT tolerable as the sole line for a write worker. This makes the capability
# actually GATE the harness grants.
#
# The mapping below was VERIFIED EMPIRICALLY against the CLI (claude 2.1.270, headless `-p`) on 2026-09-14. Its
# command-substitution/allow-list behavior was re-confirmed on 2.1.272 (2026-09-14).
# Permission semantics are subtle and security-load-bearing, so each grant was proven, not
# assumed:
#   * --allowedTools / --disallowedTools accept a space-separated list, and the CLI parses tool patterns
#     that themselves contain spaces (e.g. `Bash(git commit:*)`) correctly within that list.
#   * READ posture: NO --permission-mode + `--disallowedTools "... Edit Write MultiEdit ... Bash ..."`
#     DISABLES those tools outright ("tool is disabled for this session"); a benign Write request is
#     refused and no file is created. This is the enforced replacement for the old prompt-only read-only.
#   * WRITE posture: a write worker cannot function without a permission-mode: with none, even an
#     allow-listed, in-cwd Write is DENIED headless. `--permission-mode acceptEdits` auto-approves
#     Edit/Write/MultiEdit, but ONLY within the process CWD, and it IGNORES allow-list path patterns
#     (a `Write(<cwd>/sub/**)` allow does NOT stop an in-cwd write to a sibling dir). Proven: with
#     cwd=<dir>, an out-of-cwd write is denied, but an in-cwd/out-of-allow-pattern write SUCCEEDS.
#     => The "edit only the content subtree" containment is therefore a CWD choice owned by the worktree setup,
#        NOT expressible here as an allow-list pattern. See the seam note on the wiki row
#        in routing.py: the worktree setup MUST set the worker cwd to the content subtree (<worktree>/content) so
#        acceptEdits confines edits to content-only. This code guarantees the rest (below).
#   * acceptEdits does NOT auto-approve Bash: `git add`/`git commit` must be explicitly allow-listed
#     (proven: allow-listed `git commit` SUCCEEDS); `git push` and `rm` on the deny-floor are DENIED,
#     and a chained `git add && git commit && git push` is denied WHOLE (a denied component fails the
#     whole compound command; no smuggling push inside a chain). Any non-allow-listed Bash is denied
#     headless.
#
# FAIL-CLOSED by construction: any capability that is not EXACTLY "write" (unknown / absent / "read" /
# mixed-case) yields the READ grant, never write. acceptEdits is used ONLY for capability=="write".
#
# adapter-agnostic seam: Route carries only the ABSTRACT capability; THIS function is the
# Claude-Code-specific translation. A future hermes/opencode adapter writes its own translation of the
# same abstract capability.

# Read-only built-in tools every worker may use (read capability = exactly these; write ADDS git-commit).
_READ_ALLOWED = ["Read", "Grep", "Glob"]

# Root-anchored (//) secret-file Read denies, hoisted here
# so every capability's deny-floor can include them. This is the shared floor for any capability that could ever run
# with bypass_classifier=True (a non-Anthropic backend, via a per-row <ROW>_BACKEND pin or backend_from_mode):
# the auto-classifier is what would otherwise catch a stray secret-file read, and bypass skips it. A bare **/
# glob is cwd-relative and misses env files outside the worker cwd (proven with claude -p
# canaries: **/.*-env outside cwd = ALLOWED), so every glob here is // (root-anchored). Read() path rules
# ALSO govern Grep and Glob (Claude Code permissions doc § Read and Edit: Read rules are applied best-effort
# to every built-in file-read tool); a Grep(path)/Glob(path) rule is NOT a valid specifier (accepted, never
# consulted, startup warning), so the Read() form is the one that actually denies all three; we do NOT add a
# separate Grep()/Glob() entry per glob (would be a silent no-op that fakes a stronger floor).
# Further additions: *.env (dotenv-style, distinct from the *-env family), vault password files, a generic
# credentials.json, the broker's OWN config dir (~/.config/eph-broker/**, which carries the general/inbox
# --mcp-config JSON), SSH keys, git's plaintext credential-helper store, and .netrc (HTTP basic-auth
# credentials curl/wget/etc. read automatically). ⚠️ Denying Read on ~/.config/eph-broker/** does NOT break
# the inbox/general --mcp-config load: build_argv passes that path as a `--mcp-config <path>` CLI ARGV flag
# to the `claude` binary itself (see build_argv below); the harness's OWN MCP-server bootstrap reads that
# file at process startup, outside the running turn entirely; it is never fetched via the model's Read tool,
# so this deny-floor (which only gates the model's OWN tool_use calls during the turn) cannot see or block it.
# More additions: Docker's config.json (registry auth tokens), the gh CLI's config dir (its
# hosts.yml carries a GitHub token), and .npmrc (an npm registry auth token).
_SECRET_FILE_GLOBS = ("//**/.*-env", "//**/.*-*-env", "//**/*-env", "//**/*.env",
                      "//**/.pii-store", "//**/.pii-store/**",
                      "//**/.vault-password*", "//**/.credentials.json", "//**/.config/eph-broker/**",
                      "//**/.ssh/**", "//**/.git-credentials", "//**/.netrc",
                      "//**/.docker/config.json", "//**/.config/gh/**", "//**/.npmrc")
_SECRET_READ_DENIED = [f"Read({g})" for g in _SECRET_FILE_GLOBS]

# The code lane's "bash-n" test runner (_CODE_TEST_RUNNERS below) grants
# `Bash(bash -n:*)`, and the old comment there claimed `bash -n` "parses without executing". That claim is
# FALSE: `bash -n` only sets the shell's noexec option; several of bash's OWN flags flip execution back on
# or otherwise escape "parse only", all while still matching the literal `bash -n` prefix the allow-list
# checks:
#   * `+n` (or any other `+`-prefixed option) TOGGLES noexec back OFF, so `bash -n +n script.sh` runs the
#     script for real (empirically verified: printed EXECUTED).
#   * `-c <command>` runs an arbitrary command string directly, ignoring the script-file argument entirely.
#   * `-o <option>` / `-O <shopt>` can themselves clear noexec (or change other execution semantics) the same
#     way `+n` does.
#   * `--rcfile <file>` / `--init-file <file>` source an arbitrary startup file (bash accepts both spellings
#     for the same flag).
#   * `-i` forces interactive mode, which also sources startup files.
# The Claude Code Bash-pattern matcher is a literal prefix/wildcard STRING match on the whole command text
# (verified elsewhere in this file, e.g. the `Bash(git * --no-index*)` notes above); it is not
# argument-aware, so it cannot express "exactly one bare file argument, no flags" and the exact-safe-form fix
# isn't expressible. We therefore keep the `bash -n` prefix allow (below) and close each exec-reenabling form
# with an explicit deny here instead: deny wins over allow (same model the git denies above rely on), and
# each pattern requires a SPACE before the flag text (mirrors "Bash(git commit * -F*)" above) so a same-
# lettered filename (e.g. `my-cool-script.sh`, which contains "-c" but never " -c") doesn't false-positive.
_BASH_N_DENIED = [
    "Bash(bash -n* +*)",           # any `+`-prefixed option anywhere after `bash -n` (e.g. `+n`)
    "Bash(bash -n* -c*)",          # `-c <command>` runs an arbitrary command string
    "Bash(bash -n* -o*)",          # `-o <option>` can itself disable noexec
    "Bash(bash -n* -O*)",          # `-O <shopt>` ditto at the shopt level
    "Bash(bash -n* --rcfile*)",    # sources an arbitrary startup file
    "Bash(bash -n* --init-file*)", # same as --rcfile (bash accepts both spellings)
    "Bash(bash -n* -i*)",          # forces interactive mode, which also sources startup files
]

# The "ansible-syntax" runner (infra/general on /srv/ansible) grants ONE exact form, `ansible-playbook
# --syntax-check -i /dev/null <playbook.yml>...`, with an empty inventory, so the real inventory/host_vars dump is
# never loaded. Like `git commit -m` (narrowed on the ALLOW side, see _WRITE_ALLOWED) the safe form is "no
# further option at all": this ONE deny blocks ANY ` -`-prefixed arg after the prefix (a second `-i`/
# `--inventory`, `-e @file`/`--extra-vars`, `--vault-password-file`/`--vault-id`/`-J`, `-M`, combined short
# flags...), so only bare playbook paths remain. Same residual as pytest: ansible.cfg in the worktree is
# honoured (the cred-free worktree and human MR gate bound it). A no-op for every other runner.
_ANSIBLE_SYNTAX_PREFIX = "ansible-playbook --syntax-check -i /dev/null"
_ANSIBLE_SYNTAX_DENIED = [f"Bash({_ANSIBLE_SYNTAX_PREFIX}* -*)"]

# The live-WRITE bridge wrapper. `Bash(mcp-rw.sh:*)` is allow-listed ONLY for an infra
# worker whose spawn wrote a write-grant file (write_grant.py: trusted provenance, no `write:none`, rollback flag
# on); the wrapper itself checks that grant file's bundles + expiry. With no grant the bare token sits on every
# deny-floor (deny wins), so a classifier-judged general worker can't reach it either. The ABSOLUTE-path forms
# of mcp-rw.sh and the raw mcp.sh are ALWAYS denied: the Bash matcher is a literal prefix, so the bare-name
# `Bash(mcp.sh:*)` deny never matched `<mcp dir>/mcp.sh …` (the dir comes from site.env, see _mcp_abs).
_MCP_RW = ["Bash(mcp-rw.sh:*)"]
_MCP_ABS_DENIED = _mcp_abs("mcp.sh", "mcp-rw.sh")

# Tools a READ worker must never get; they're explicitly DISABLED (verified: disallow disables the tool outright).
# Also denies the secret-file globs: this capability (ansible's default and mr-review) has NO Bash at
# all (so ansible-inventory/--vault-password-file/git push are already unreachable), but Read/Grep/Glob stay
# allowed, so a stray *-env file in the cwd would otherwise be plainly readable.
_READ_DENIED = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "WebFetch", "WebSearch"] + _SECRET_READ_DENIED

# The git verbs a WRITE worker may run as Bash: STAGE + COMMIT only (the broker does the ONLY
# push, cred-side; the worker is cred-free and NEVER pushes). status/diff/log are read-only orientation.
# NOTHING here mutates the remote, rewrites history, or moves refs. (Edit/Write/MultiEdit are NOT listed
# since acceptEdits auto-approves those within cwd; only Bash needs an explicit allow.)
# The broad "Bash(git diff:*)"/"Bash(git commit:*)"
# prefix-allows were NARROWED because the first round of deny-floor additions alone wasn't enough:
#   - `git diff /dev/null <path>` (or any two args where at least one names a path OUTSIDE the work tree)
#     AUTO-ENABLES no-index WITHOUT the literal `--no-index` flag ever appearing on the command line;
#     live-canaried: `Bash(git * --no-index*)` never matches because the flag text isn't there, so the deny
#     is a no-op against this exact form. Only a NO-PATH-ARGS allow-list closes it.
#   - `git commit -aF <path>` (combined short flags) and `git commit --fil <path>` (long-option prefix
#     abbreviation, which git accepts) both evade the literal `-F*`/`--file*` denies the same way. Narrowing
#     the allow to the exact `-m` form closes this at the ALLOW side instead of trying to enumerate every
#     abbreviation/combination on the deny side (git accepts unambiguous prefixes of any long option, so a
#     deny-side enumeration can never be exhaustive). `-m` and `-F` are also mutually exclusive to git itself
#     ("options '-m' and '-F' cannot be used together"), so this is a real, not merely nominal, exclusion.
# The -F/--file/--no-index/--output denies from the first round STAY as belt-and-suspenders (e.g. they still
# catch a future capability that ever grants a broader git verb) but are no longer the primary control.
# `git commit -q -m ...` is ALSO allowed (a code worker once lost its built work when all three of
# its `-q -m` commits were refused). `-q` only silences output; `-F`/`--file` after it still hit the
# `git commit * -F*` / `* --file*` denies (deny wins), same residual as the plain `-m` form.
_WRITE_ALLOWED = _READ_ALLOWED + ["Bash(git add:*)", "Bash(git commit -m:*)", "Bash(git commit -q -m:*)",
                                  "Bash(git status:*)",
                                  "Bash(git diff)", "Bash(git diff --cached)", "Bash(git diff --stat)",
                                  "Bash(git diff --cached --stat)", "Bash(git diff HEAD)",
                                  "Bash(git log:*)"]

# Deny-floor a WRITE worker keeps regardless of allow-list (disallow WINS): no push/remote/history-rewrite/
# ref-move/worktree/branch mutation, no destructive shell, no network fetch. Arbitrary Bash is ALSO
# fail-closed simply by not being allow-listed (denied headless); these are belt-and-suspenders for the
# verbs we most want to guarantee, and the CHAIN test proves a denied component fails the whole command.
# Raw `mcp.sh` (the BROAD, write-capable-per-bridge gateway) is hard-denied here too; any
# class that needs fleet-bridge/memory access gets it via the read-only `mcp-ro.sh` wrapper instead (see
# _MCP_RO below), never the raw script. Denying a DIFFERENT command token than the mcp-ro.sh grant means
# this entry cannot shadow that allow-list (Claude Code's Bash tool-pattern match is on the literal command
# prefix: "Bash(mcp.sh:*)" matches commands starting with `mcp.sh`, NOT `mcp-ro.sh`).
# No file-tool edits to a `.git` FILE (a linked worktree's gitdir pointer) or anything under a `.git/`
# dir. Repointing the gitdir at worker-made config/hooks would otherwise run worker code inside the BROKER's
# own git calls (publish_mr's credentialed push, save_wip). worktree.py hardens those calls too (-c overrides,
# GIT_CONFIG_GLOBAL/NOSYSTEM env, gitdir check). Shared by _WRITE_DENIED (write/code/inbox) and _GENERAL_DENIED.
_GITDIR_EDIT_DENIED = ["Edit(//**/.git)", "Edit(//**/.git/**)", "Write(//**/.git)", "Write(//**/.git/**)"]
_WRITE_DENIED = ["Bash(git push:*)", "Bash(git remote:*)", "Bash(git config:*)", "Bash(git reset:*)",
                 "Bash(git rebase:*)", "Bash(git checkout:*)", "Bash(git branch:*)", "Bash(git worktree:*)",
                 "Bash(rm:*)", "Bash(sudo:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(ssh:*)",
                 "Bash(mcp.sh:*)", "WebFetch", "WebSearch",
                 # Close two bypasses the plain "Bash(git diff:*)"/"Bash(git commit:*)"
                 # prefix-allows leave open (verified against the Claude Code permissions doc: Bash rules
                 # match the WHOLE command text with `*` standing in for arbitrary text at ANY position, so
                 # a mid-string `*` here is a supported, real match, not a no-op). (1) `git diff --no-index
                 # <a> <b>` (any git subcommand, really, `git * --no-index*`) compares two ARBITRARY paths
                 # outside any git repo entirely, turning the "read-only git diff" allow into an unscoped
                 # file-read; ditto `--output=<file>` (any git subcommand), which WRITES the command's
                 # output to an arbitrary path instead of stdout. (2) `git commit -F <file>` / `--file
                 # <file>` embeds an ARBITRARY file's contents as the commit message, which `git log`/
                 # `git show` (already allowed) then plays right back: an end-run around the secret-file
                 # Read-tool deny above. Deny wins over allow regardless of which allow-list granted the verb
                 # (_WRITE_ALLOWED / _INBOX_GIT / _CODE_TEST_RUNNERS never add these forms either).
                 "Bash(git diff --no-index*)", "Bash(git * --no-index*)", "Bash(git * --output*)",
                 "Bash(git commit -F*)", "Bash(git commit * -F*)",
                 "Bash(git commit --file*)", "Bash(git commit * --file*)",
                 # `--pathspec-from-file=<path>`
                 # (any git subcommand: `git add --pathspec-from-file=<abs>`, `git commit -m x
                 # --pathspec-from-file=../outside`) reads pathspecs from an ARBITRARY file one per line; a
                 # non-matching line is echoed VERBATIM in `error: pathspec '<line>' did not match ...`,
                 # printing another file's contents through the (denied) command's own error output. git also
                 # accepts an unambiguous prefix abbreviation of any long option (confirmed: `--pathspec-fr=`
                 # works), so enumerating abbreviations on the deny side is unbounded; `--paths*` is the
                 # shortest PREFIX every accepted spelling shares ("--pathspec-from-file" and "--pathspec-fr"
                 # both start with "--paths"), and `--paths`/`--pathspec-f` alone are ambiguous/rejected by
                 # git, so this one deny covers every form without over-matching a real option.
                 "Bash(git * --paths*)",
                 ] + _GITDIR_EDIT_DENIED + _MCP_RW + _MCP_ABS_DENIED + _SECRET_READ_DENIED + _BASH_N_DENIED + _ANSIBLE_SYNTAX_DENIED   # See above. See the _SECRET_READ_DENIED note above; covers write/code/inbox (_CODE_DENIED/_INBOX_DENIED reuse this list). _BASH_N_DENIED: see comment above its definition; only the code lane's bash-n runner ever grants `Bash(bash -n:*)`, so these are a no-op everywhere else.

# ============================================================================================
# The "code" capability: a code change-proposer worker (SECURITY-CRITICAL).
# ============================================================================================
# A code worker edits a target repo inside a disposable worktree (acceptEdits confines edits to cwd = the
# worktree root, set by the worktree setup), commits (git add/commit only; NEVER pushes. The broker pushes a
# FEATURE BRANCH and opens an MR, never main), and, as the new grants over `write`, RUNS TESTS to self-verify
# its diff (test-runner menu below) and reaches Hindsight MEMORY via the read-only `mcp-ro.sh` wrapper (see
# _CODE_MEMORY / _MCP_RO below). Everything else is `write` verbatim: the _WRITE_DENIED floor applies as-is
# (no push/remote/config/history-rewrite/ref-move, no rm/sudo/curl/wget/ssh, no raw mcp.sh, no web fetch).
#
# THE TEST-Bash GRANT IS THE SINGLE SECURITY-REVIEW FOCUS. A blanket
# `Bash` grant is a shell-escape hole, so we do NOT grant it. Instead a ticket/row declares a runner KEY
# (e.g. "pytest") that indexes this CODE-REVIEWED MENU; the ticket NEVER supplies a raw command, so no
# arbitrary string ever reaches the allow-list. Each menu value is the MINIMUM runner invocation as a
# Claude-Code Bash prefix pattern (`Bash(<runner>:*)` = "commands starting with <runner>"). Safety rests on
# the SAME model `write` already relies on and the CHAIN test proves: the deny-floor WINS, and a command
# with ANY non-allow-listed component (a `;`/`&&`-chained `curl`/`rm`/`echo`/etc.) fails the WHOLE command
# (fail-closed), so a prefix like `Bash(pytest:*)` cannot smuggle `pytest; rm -rf` past the floor. Adding a
# repo's runner = one code-reviewed entry here (not a config/ticket string). FAIL-CLOSED: an unknown/absent
# runner key grants NO test Bash at all (the worker stays write-capable but simply can't run tests).
#
# COMMAND-SUBSTITUTION is ALSO blocked, verified EMPIRICALLY against the pinned CLI (2.1.272, 2026-09-14):
# a Bash tool-call like `python -m pytest $(touch /tmp/x)` (or backticks) is REJECTED pre-execution by the
# allow-list with reason "Contains command_substitution"; it never reaches the shell, independent of the
# inner command (even `touch`, which is NOT on the deny-floor). So `Bash(pytest:*)` cannot smuggle a
# `$(...)`/backtick payload either. Three independent layers: (1) this harness allow-list blocks
# substitution + chaining, (2) the signed change-proposer prompt forbids chaining/substitution/network,
# (3) even a bypass is cred-free, feature-branch-only and human-MR-gated (no deploy/main path).
_CODE_TEST_RUNNERS = {
    "pytest": ["Bash(python -m pytest:*)", "Bash(pytest:*)"],
    # A JS/TS repo (package.json; see routing.detect_test_runner, which only fires for a
    # CODE_REPO_ALLOWLIST repo) needs its deps before `npm test`, so `npm ci` is granted as the EXACT bare
    # command (no `:*`, so no `--prefix <elsewhere>`/`--cache`/`--userconfig` or any other flag/arg). Never bare
    # `npm`/`npx`/`npm run <anything>`/`npm exec`. This is NOT posture-neutral: `npm ci` FETCHES packages from
    # the npm registry over the network, HONOURS any `.npmrc` in the repo/worktree (registry URL, scripts
    # settings, etc.), and RUNS dependency install scripts (pre/post-install) inside the worktree sandbox; `npm
    # test` then runs the repo's own "test" script. That is arbitrary third-party code with network access,
    # beyond what pytest grants. It is accepted ONLY for allowlisted repos (an inferred-only catalog repo
    # resolves to `none`, never `npm`), on the basis that the worker is cred-free and its sole output is a
    # human-reviewed feature-branch MR.
    "npm":    ["Bash(npm ci)", "Bash(npm test:*)", "Bash(npm run test:*)"],
    "go":     ["Bash(go test:*)"],
    # Syntax-check ONLY for script repos: `bash -n <file>` with NO other flags parses without
    # executing. The allow-list itself is still a `bash -n` prefix grant (the CC Bash-pattern matcher can't
    # express "exactly one bare file arg, no flags"; see _BASH_N_DENIED above), so this is paired with
    # explicit denies for every flag that re-enables execution or otherwise escapes parse-only (`+n`/any
    # `+*` option, `-c`, `-o`, `-O`, `--rcfile`, `--init-file`, `-i`); deny wins over allow. Like pytest
    # (which runs any test file the worker writes) it is only as strong as the cred-free worktree + deny-
    # floor around it.
    "bash-n": ["Bash(bash -n:*)"],
    # Ansible playbook syntax-check against an EMPTY inventory only (see _ANSIBLE_SYNTAX_DENIED).
    "ansible-syntax": [f"Bash({_ANSIBLE_SYNTAX_PREFIX}:*)"],
    "none":   [],   # docs-only repo: an explicit "no test Bash" (same grant as an unknown key)
    # "venv" is per-repo (the pattern embeds the repo's own .venv path) -> built in _code_test_bash below.
}

# What the worker is told to run (the signed prompt says "your runtime brief names it"). Seed brief only.
_CODE_TEST_HINTS = {
    "pytest": "python -m pytest",
    "npm":    "npm ci  then  npm test  (two separate calls; no chaining)",
    "go":     "go test ./...",
    "bash-n": "bash -n <file.sh>  (one tracked *.sh per call; no chaining)",
    "ansible-syntax": f"{_ANSIBLE_SYNTAX_PREFIX} <playbook.yml>  (no other flags; no chaining)",
    "venv":   "{python} -m pytest",
}


def _code_test_bash(test_runner, repo=None):
    """The reviewed test-Bash allow-list for a runner KEY. `venv` is the one per-repo entry: the repo's own
    <repo>/.venv/bin/python (repo is broker-resolved + CODE_REPO_ALLOWLIST-gated, never ticket-raw). A repo
    path with whitespace/parens can't be a safe Bash(...) prefix -> no grant (fail-closed)."""
    if test_runner == "venv":
        if not repo or any(c in repo for c in " \t\n()"):
            return []
        return [f"Bash({routing.code_venv_python(repo)} -m pytest:*)"]
    return _CODE_TEST_RUNNERS.get(test_runner, []) if test_runner else []


def code_test_hint(test_runner, repo=None):
    """One-line 'how to run the tests' for the code worker's brief; None when no runner is granted."""
    hint = _CODE_TEST_HINTS.get(test_runner or "")
    if hint and test_runner == "venv":
        hint = hint.format(python=routing.code_venv_python(repo)) if repo else None
    return hint

# Memory + read-bridge grant, SHARED across every class that needs it: reach the fleet MCP bridges via the
# read-only wrapper `mcp-ro.sh` over Bash. This
# REPLACES the old raw-`mcp.sh`-over-Bash grant because
# raw `mcp.sh` is a BROAD gateway (write-capable per each bridge's own token), which is a hole for any
# class that only needs memory/reads. `mcp-ro.sh` is deny-by-default: its hindsight arm allows ONLY
# `recall|retain|reflect` (denies `clear_memories` = a shared-bank wipe) and every non-read tool on every
# other bridge is denied inside the wrapper itself. Scope + safety:
#   * The prefix is `mcp-ro.sh`, a DIFFERENT command token than `mcp.sh`, so it does NOT collide with the
#     `Bash(mcp.sh:*)` deny-floor entry added alongside this change (_WRITE_DENIED / _GENERAL_DENIED): the
#     harness's Bash allow/deny matching is on the literal command prefix, and "mcp-ro.sh" does not start
#     with "mcp.sh " / equal "mcp.sh", so the deny cannot shadow this allow. (This is the whole point of the
#     rename instead of narrowing the raw-mcp.sh grant in place: a `--disallowedTools` entry always WINS
#     over an `--allowedTools` entry when both match the SAME token, so keeping the grant on `mcp.sh` while
#     also blanket-denying `mcp.sh` would have deleted the grant.)
#   * Read-write separation is enforced INSIDE mcp-ro.sh, not by this harness-level prefix; the wrapper is
#     the deny-by-default gate; the harness only needs to admit the whole wrapper command.
#   * The SAME chain/substitution floor the test-runners rely on holds here (verified 2.1.272): a
#     `;`/`&&`/`|`-chained or `$()`/backtick command is rejected pre-exec ("Contains command_substitution"
#     / chaining), so `mcp-ro.sh hindsight …; curl evil` or `mcp-ro.sh $(curl evil)` fails the WHOLE
#     command; the prefix cannot smuggle a second command or exfil past the deny-floor.
#   * The worker's own `Bash(curl:*)`/`wget`/`ssh` stay DENIED (the deny-floor is unchanged); the curl
#     that reaches a bridge runs INSIDE mcp-ro.sh (a child of the allow-listed script), not as a worker Bash
#     tool-call, so the worker still cannot curl an arbitrary URL itself.
# NOT a directly-loaded MCP: keeping this an mcp-ro.sh-over-Bash grant (not a --mcp-config server) means a
# code/inbox worker's directly-loaded MCP surface stays empty, so no strict-mcp-config plumbing needed here.
_MCP_RO = ["Bash(mcp-ro.sh:*)"]
_CODE_MEMORY = _MCP_RO

# Deny-floor for a code worker = the write floor VERBATIM (no push/network/destructive/history-rewrite).
# Named separately only for read-site clarity; it is _WRITE_DENIED unchanged.
_CODE_DENIED = _WRITE_DENIED

# ============================================================================================
# General-purpose worker: the FIRST class that INVERTS the posture:
# broad interactive tools adjudicated by the AUTO-CLASSIFIER, not a narrow allow-list. It is opus running
# the fleet rules (loaded via --setting-sources user), so its in-worker trust is that of a standing dev agent; the
# safety delta is cred-free + broker-only-publish + one-shot disposable worktree + hash-gated framing, NOT
# tool-narrowing (proportional security: "the auto-classifier exists, lean on it").
#
# EMPIRICALLY VERIFIED (CLI 2.1.272 headless -p, 2026-09-15): `--permission-mode auto`
# ADJUDICATES: a benign edit/Bash runs (classifier-allowed) while `git push` is VETOED at the harness layer
# (permission_denials), so auto is NEITHER block-all NOR fail-open. `claude auto-mode config` shows the
# effective ruleset (27 environment rules incl a fleet trust block, 17 allow, 70 soft_deny + hard_deny).
# We therefore grant NO allow-list: an allow-list entry AUTO-APPROVES (bypasses the classifier) under auto, so
# a broad allow would DEFEAT "classifier is the guardrail". Instead the classifier judges the whole open
# surface and the deny-floor is the hard override (disallow WINS over any classifier verdict).
#
# THE FLOOR (a floor, not a cage): the git-destructive/history-rewrite/ref-move set + rm/sudo (never-do's
# under any mode) + SECRET-READ denies (.*-env / .pii-store; the one thing proportional-security keeps hard,
# handled separately) and raw `mcp.sh`. ⚠️ The MCP surface is NOT contained by this Bash-only
# floor; it is contained separately by --strict-mcp-config + a minimal Hindsight-ONLY --mcp-config in
# build_argv (see below): a 2026-09-15 security review proved a -p worker inherits the full
# account-connector tool surface (hundreds of tools, many of them write-capable)
# regardless of --setting-sources, so a broad-tool worker MUST have strict-mcp-config or it holds standing
# write/deploy creds gated only by the classifier. Network is NOT floored; it is classifier-judged like
# every other command (deliberately loosened: network is how the worker reaches the fleet bridges).
# ⚠️ Raw `mcp.sh` (a BROAD, write-capable-per-bridge gateway) is HARD-DENIED; the general
# worker has NO allow-list, so this deny is what actually closes its raw-mcp.sh hole (a classifier "allow"
# verdict cannot override an explicit deny). The general worker still reaches memory + read-only bridges via
# `mcp-ro.sh` (see _MCP_RO above): that is a DIFFERENT command token this deny does not match, and it stays
# classifier-judged like any other Bash call, deliberately NOT allow-listed here, because an allow-list
# entry would auto-approve it and bypass the classifier (the general-worker invariant this whole class rests
# on). The account-connector surface stays contained by --strict-mcp-config + Hindsight-only --mcp-config in
# build_argv (no direct Gmail-send / credentialed connector tools). git-escape vectors (-C / --git-dir reach
# another repo, bypassing the "git push:*" prefix) + rm-substitutes (dd/shred/truncate) are floored too.
_GENERAL_DENIED = ["Bash(git push:*)", "Bash(git remote:*)", "Bash(git config:*)", "Bash(git reset:*)",
                   "Bash(git rebase:*)", "Bash(git checkout:*)", "Bash(git branch:*)", "Bash(git worktree:*)",
                   "Bash(git -C:*)", "Bash(git --git-dir:*)",   # git-escape: reach ANOTHER repo (evades git push:*)
                   "Bash(rm:*)", "Bash(sudo:*)",
                   "Bash(dd:*)", "Bash(shred:*)", "Bash(truncate:*)",   # rm-substitutes (destructive)
                   "Bash(mcp.sh:*)",   # Raw broad gateway denied; mcp-ro.sh stays classifier-judged
                   ] + _GITDIR_EDIT_DENIED + _MCP_RW + _MCP_ABS_DENIED + [   # live-write wrapper + absolute-path gateway forms
                   # secret-read floor (belt under the user-level rules' refusal + classifier credential rules,
                   # both verified live to block a secret read): deny the Read tool on env/secret files.
                   # Reuse the ONE shared list (_SECRET_READ_DENIED, defined near _READ_DENIED
                   # above) instead of an inline copy, which was drifting (this list lacked the later additions
                   # the other capabilities got); now every capability shares one source of truth.
                   ] + _SECRET_READ_DENIED

# ============================================================================================
# The "inbox" capability: a Gmail-MUTATION-capable triage worker (SECURITY-CRITICAL).
# ============================================================================================
# The inbox worker triages one account's mail per wake: it READS/searches mail, applies/removes labels
# (archive = remove the INBOX label, which is reversible), drafts (NEVER sends), retains receipts to Hindsight,
# reports over zbus, and grooms its own seen-ledger + rules.md in the inbox repo (git add/commit only).
# It runs under `--permission-mode acceptEdits` (it edits the ledger + rules.md within its cwd = the inbox
# repo). Its MCP surface is loaded ONLY from a per-account `--mcp-config` (build_argv, + --strict-mcp-config).
#
# ⚠️ SERVER-NAME CONTRACT (authoritative, taken from the LIVE google-workspace-mcp, NOT guessed): the
# per-account config MUST name its Gmail server `google-workspace`, so tool ids are
# `mcp__google-workspace__<tool>`, the tokens below. (Do NOT use the claude.ai account-connector names:
# the old inbox agent reached Gmail via the OAuth connector, so its .mcp.json does NOT list these tools; the names
# here came from the running MCP.) zbus rides ZBUS_ENV/CLI (Bash(zbus:*)), not MCP.
#
# ⚠️ HINDSIGHT IS *NOT* LOADED VIA THE PER-ACCOUNT --mcp-config (fixed): the live per-account
# config files (`~/.config/eph-broker/inbox-mcp/<account>.json`) name ONLY `google-workspace`; there is no
# `hindsight` mcpServer entry, so `mcp__hindsight__recall`/`mcp__hindsight__retain` never existed in this
# worker's strict-mcp-config surface (a silently-inert allow-list entry: the worker had no path to
# Hindsight). Reached instead the SAME way the code worker does: the on-network read-only
# `mcp-ro.sh` wrapper over Bash (see `_CODE_MEMORY` / `_MCP_RO` above); no MCP-config
# plumbing needed, so it also can't drift out of sync with the (secret, hand-authored) per-account
# config files.
#
# THE DENY-FLOOR IS THE SECURITY CORE (disallow WINS over allow): the only dedicated send tool
# (`send_gmail_message`) is HARD-DENIED (also excluded at the OAuth scope), and `manage_gmail_filter`
# (filter CREATION; propose-gated per the signed prompt) is denied while read-only `list_gmail_filters`
# stays allowed. Cred-free: the full _WRITE_DENIED floor (no git push/remote/config/history-rewrite, no
# rm/sudo/curl/wget/ssh, no web fetch) is reused verbatim.
#
# ⚠️ TRASH is NOT grant-separable from ARCHIVE (verified live): there is NO `trash_gmail_message`
# tool; trashing is `modify_gmail_message_labels` with add_label_ids=TRASH, the SAME tool archive needs
# (remove_label_ids=INBOX). Denying a nonexistent `trash_*` tool would be a SILENT NO-OP (a FALSE deny-floor),
# so we do NOT. modify/batch_modify ARE allowed (archive needs them); TRASH-blocking is enforced at the
# SIGNED-PROMPT layer (the prompt hard-blocks trashing). Permanent delete is impossible (no delete tool
# exposed + not in the modify/drafts OAuth scope).
# Limitation: TRASH stays a prompt-layer block, not a grant-layer one, because it shares the modify-labels
# tool with archive. A PreToolUse hook (hooks/inbox_trash_guard.py)
# rejects modify/batch_modify_gmail_message_labels calls whose add_label_ids includes TRASH, the only
# place trash can be split from archive mechanically. Wired below via `--settings` (independent of
# --setting-sources, which stays "" / charter-blind for this route; see build_argv). ⚠️ FIELD NAME
# (live `tools/list`-verified): the real google-workspace-mcp param is
# `add_label_ids`/`remove_label_ids`, NOT the ticket's `labelsToAdd`/`labelsToRemove` shorthand; see the
# hook script's own docstring for the schema note; it checks both, `add_label_ids` primary.
# Commits carry no `Co-Authored-By:`/`Claude-Session:` trailers (by policy):
# a worker's `git commit` was carrying the CLI's default attribution trailer.
# `attribution` (current setting name; deprecates `includeCoAuthoredBy`) loads via `--settings`
# independently of `--setting-sources`, so it applies even though every worker route stays
# charter-blind (`--setting-sources ""`). `False` hides ALL commit/PR/session-url attribution for
# EVERY capability; cheap and correct even for a capability that never commits.
_ATTRIBUTION_OFF_SETTINGS = {"attribution": False}

_INBOX_TRASH_GUARD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "hooks", "inbox_trash_guard.py")
_INBOX_HOOK_SETTINGS = {
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "mcp__google-workspace__(modify_gmail_message_labels|"
                           "batch_modify_gmail_message_labels)",
                "hooks": [{"type": "command", "command": _INBOX_TRASH_GUARD}],
            }
        ]
    }
}

# Gmail READ tools (search/fetch/labels/filters-read) an inbox worker may use.
_GMAIL_READ = ["mcp__google-workspace__search_gmail_messages",
               "mcp__google-workspace__get_gmail_message_content",
               "mcp__google-workspace__get_gmail_messages_content_batch",
               "mcp__google-workspace__get_gmail_thread_content",
               "mcp__google-workspace__get_gmail_threads_content_batch",
               "mcp__google-workspace__get_gmail_attachment_content",
               "mcp__google-workspace__list_gmail_labels",
               "mcp__google-workspace__list_gmail_filters"]   # read-only; CREATION (manage_gmail_filter) DENIED
# Gmail REVERSIBLE-WRITE tools: label mgmt + archive (= remove INBOX label) + draft. NEVER send. (modify/
# batch_modify ALSO carry trash; see the TRASH note above. Trash is blocked at the prompt layer, not here.)
_GMAIL_WRITE_SAFE = ["mcp__google-workspace__manage_gmail_label",
                     "mcp__google-workspace__modify_gmail_message_labels",
                     "mcp__google-workspace__batch_modify_gmail_message_labels",
                     "mcp__google-workspace__draft_gmail_message"]
# Hindsight: reached via the read-only mcp-ro.sh wrapper (mirrors _CODE_MEMORY / _MCP_RO),
# NOT the per-account --mcp-config (that config only names `google-workspace`; see the HINDSIGHT note
# above). recall-before-retain (receipts) per modules/capabilities/inbox-triage.md.
_HINDSIGHT = _MCP_RO
# zbus escalation/report/digest: CLI over ZBUS_ENV, not an MCP server.
_ZBUS = ["Bash(zbus:*)"] + ([f"Bash({_ZBUS_BIN}:*)"] if os.path.isabs(_ZBUS_BIN) else [])   # NOTIFY_ZBUS
# DENY-only: zbus by any path (config-independent). Never on an allow list (would admit any `*/zbus`).
_ZBUS_ANY_DENIED = ["Bash(*/zbus *)"]
# git STAGE + COMMIT only (ledger + rules.md grooming in the inbox repo); cred-free, NEVER pushes.
# Same narrowing as _WRITE_ALLOWED (see its comment): `git diff:*`/`git commit:*` prefix
# allows are replaced with exact no-path-args diff forms + the exact `-m` commit form, closing the
# no-index-without-the-flag and -aF/--fil abbreviation bypasses the deny-floor alone couldn't catch.
_INBOX_GIT = ["Bash(git add:*)", "Bash(git commit -m:*)",
              "Bash(git status:*)",
              "Bash(git diff)", "Bash(git diff --cached)", "Bash(git diff --stat)",
              "Bash(git diff --cached --stat)", "Bash(git diff HEAD)",
              "Bash(git log:*)"]

_INBOX_ALLOWED = _READ_ALLOWED + _INBOX_GIT + _GMAIL_READ + _GMAIL_WRITE_SAFE + _HINDSIGHT + _ZBUS

# DENY-FLOOR: reuse the whole write deny-floor (push/remote/config/ref-rewrite + rm/sudo/curl/wget/ssh +
# web) and ADD the Gmail denies that map to REAL tools: send (the only send tool) + manage_gmail_filter
# (propose-gated creation). The `mcp__*__send_gmail_message` server-agnostic wildcard is kept ALONGSIDE the
# server-qualified deny as belt-and-suspenders: sending mail AS THE ACCOUNT OWNER is the single most dangerous,
# impersonation-prone, irreversible-ish op, so its deny must NOT hinge entirely on the per-account config
# naming the server exactly `google-workspace`; the wildcard bites even if that server is ever renamed. This
# is the ONE wildcard worth keeping, precisely because send_gmail_message is a REAL tool it matches; we do
# NOT add trash/delete wildcards; those match NO tool (a silent no-op that fakes a stronger floor). Trash
# stays a signed-prompt-layer block (see the TRASH note above); permanent delete is impossible (no tool
# exposed + not in scope).
_GMAIL_DENIED = ["mcp__google-workspace__send_gmail_message",
                 "mcp__*__send_gmail_message",   # server-agnostic belt-and-suspenders (send is the worst op)
                 "mcp__google-workspace__manage_gmail_filter"]
_INBOX_DENIED = _WRITE_DENIED + _GMAIL_DENIED

# The "inbox-chat" capability: an ephemeral worker answering a Zulip DM from the operator (zulip_wake.py
# routes to routing._ZULIP_INBOX_ROW). Same Gmail surface as triage (read + label/archive/draft, send/filter-create
# denied, TRASH hook) + read-only Hindsight, but NO bus and NO file edits: the BROKER posts the envelope
# summary back to the original sender, so the worker gets no way to DM anyone. zbus is not allow-listed AND is
# hard-denied (belt-and-suspenders; the row also carries no ZBUS_ENV), no git, no --permission-mode (Edit/
# Write/MultiEdit/NotebookEdit denied outright).
# Memory is Hindsight RECALL ONLY, through a small recall wrapper that fixes the tool name to
# "recall"; mcp-ro.sh itself is DENIED to this worker (its hindsight arm also passes retain/reflect, and its
# other arms reach git hosting, metrics, SSO, network and ticketing tools). Env/secret files are read-denied (below).
_HS_RECALL = None    # No recall wrapper ships publicly: the chat worker gets no memory tool (deny-everything).
# _SECRET_FILE_GLOBS / _SECRET_READ_DENIED are now defined once, near _READ_DENIED above;
# reused here unchanged (this row's own explicit +_SECRET_READ_DENIED below is belt-and-suspenders: it also
# rides in via _INBOX_DENIED -> _WRITE_DENIED now, but this capability keeps its own explicit tail so it
# reads correctly standalone).
_INBOX_CHAT_ALLOWED = _READ_ALLOWED + _GMAIL_READ + _GMAIL_WRITE_SAFE
_INBOX_CHAT_DENIED = (_INBOX_DENIED + ["Edit", "Write", "MultiEdit", "NotebookEdit"] + _ZBUS + _ZBUS_ANY_DENIED
                      + ["Bash(git:*)", "Bash(mcp-ro.sh:*)"] + _mcp_abs("mcp-ro.sh")
                      + _SECRET_READ_DENIED)
# (No zulip MCP deny: --strict-mcp-config + the per-account config expose ONLY google-workspace, so there is no
# zulip tool to deny; a wildcard here would match nothing and fake a stronger floor, see _GMAIL_DENIED.)



# Wiki skills for cc workers. Every route runs `--setting-sources ""` (charter-blind),
# so ~/.claude/skills is NEVER loaded; `--plugin-dir <dir>` still loads regardless of that flag (skills show as
# `wiki:<name>`). The dir is populated OUT-OF-REPO by `/opt/site/bin/skills-sync --plugin <dir>` on the
# broker's refresh cron. DEFAULT OFF: EPH_WIKI_SKILLS_ENABLED=1 opts in; EPH_WIKI_SKILLS_DIR
# overrides the default dir. Read at CALL time (not import) so a restart-free env flip / tests see it. A missing
# dir with the flag on is skipped with a warning (never spawn with a dangling --plugin-dir). hermes workers: out of scope.
_WIKI_SKILLS_DEFAULT_DIR = os.path.expanduser("~/.eph-broker/wiki-skills")


def _wiki_skills_plugin_dir():
    """The --plugin-dir to pass, or None (flag off, or flag on but the synced dir is absent)."""
    if not _env_flag("EPH_WIKI_SKILLS_ENABLED", "0"):
        return None
    d = os.path.expanduser(os.getenv("EPH_WIKI_SKILLS_DIR", "").strip() or _WIKI_SKILLS_DEFAULT_DIR)
    if not os.path.isdir(d):
        log.warning("EPH_WIKI_SKILLS_ENABLED but plugin dir %r is missing; spawning without wiki skills", d)
        return None
    return d


def capability_tool_grant(capability, test_runner=None, bypass_classifier=False, test_repo=None,
                          write_grant=False):
    """Translate an abstract Route.capability into the Claude-Code harness tool-grant flags.

    Returns a list of argv tokens. FAIL-CLOSED: any capability that is not EXACTLY "write", "inbox", or
    "code" is read-only. Containment of a write/code worker to its subtree is a CWD choice owned by the worktree setup
    (see module header); acceptEdits confines edits to cwd but ignores allow-list path patterns.

    `test_runner` applies ONLY to capability=="code": it is a KEY into the code-reviewed _CODE_TEST_RUNNERS
    menu (never a raw command). An unknown/None key grants NO test Bash (fail-closed); the worker stays
    write-capable but cannot run tests. `test_repo` is the base repo the `venv` runner's interpreter
    lives in; ignored by every other key.

    `write_grant` applies ONLY to capability=="code": True iff the broker wrote a write-grant file for
    this worker (write_grant.py). It adds `Bash(mcp-rw.sh:*)` and lifts that ONE bare token off the deny-floor
    (deny wins, so it must leave the floor); the absolute-path forms stay denied. Any other capability ignores it.
    """
    if capability == "write":
        return ["--permission-mode", "acceptEdits",       # auto-approve Edit/Write WITHIN cwd (the worktree setup sets cwd)
                "--allowedTools", " ".join(_WRITE_ALLOWED),
                "--disallowedTools", " ".join(_WRITE_DENIED)]
    if capability == "code":
        # code change-proposer: write grants + a NARROW, code-reviewed test-runner allow-list (keyed by
        # test_runner, NOT a raw command from the ticket). Deny-floor is _WRITE_DENIED verbatim. An unknown/
        # absent runner key -> no test Bash (fail-closed). NEVER a blanket Bash grant.
        test_bash = _code_test_bash(test_runner, test_repo)
        rw = _MCP_RW if write_grant is True else []
        return ["--permission-mode", "acceptEdits",
                "--allowedTools", " ".join(_WRITE_ALLOWED + _CODE_MEMORY + test_bash + rw),
                "--disallowedTools", " ".join(d for d in _CODE_DENIED if d not in rw)]
    if capability == "general":
        # general-purpose worker: broad tools adjudicated by the AUTO-CLASSIFIER, NOT an allow-list. NO
        # --allowedTools (an allow entry auto-approves and bypasses the classifier under auto; verified), so
        # the classifier judges the whole surface; the deny-floor is the hard override (disallow WINS). See
        # the _GENERAL_DENIED note above. The worker loads the user-level rules via route.setting_sources="user"
        # (threaded in build_argv) so it self-enforces proportional-security / no-secret / propose-don't-deploy.
        #
        # bypass_classifier (set by build_argv when a NON-Anthropic cloud backend is routed): the stage-2
        # auto-mode classifier's small-model calls get remapped by load_backend() onto that same backend
        # endpoint, which stalls, so the worker self-blocks on every tool call (root cause of the general
        # 100% failure). On such a backend we drop --permission-mode auto and run skip-permissions instead,
        # KEEPING the _GENERAL_DENIED floor (deny rules stay hard-enforced in every mode). Mirrors the
        # site spawner's non-Anthropic backend recipe. Native Anthropic
        # keeps auto (the classifier works there). Deny-floor is identical in both arms.
        if bypass_classifier:
            return ["--dangerously-skip-permissions",
                    "--disallowedTools", " ".join(_GENERAL_DENIED)]
        return ["--permission-mode", "auto",
                "--disallowedTools", " ".join(_GENERAL_DENIED)]
    if capability == "inbox":
        # Gmail-mutation triage worker. acceptEdits so it can edit the seen-ledger + rules.md within its
        # cwd (the inbox repo, set by _run_worker). Deny-floor WINS: send + manage_gmail_filter hard-denied
        # (trash is a prompt-layer block; it shares the modify-labels tool with archive; see notes above).
        return ["--permission-mode", "acceptEdits",
                "--allowedTools", " ".join(_INBOX_ALLOWED),
                "--disallowedTools", " ".join(_INBOX_DENIED)]
    if capability == "inbox-chat":
        # Gmail + Hindsight recall-only, NO permission-mode (no file edits), NO bus (see _INBOX_CHAT_*).
        return ["--allowedTools", " ".join(_INBOX_CHAT_ALLOWED),
                "--disallowedTools", " ".join(_INBOX_CHAT_DENIED)]
    if capability == "mr-review":
        # PURE READ: identical grant to read, distinct string for routing clarity + telemetry.
        # No --permission-mode, no Edit/Write/Bash/git/network. Reviews the checked-out MR branch
        # read-only (cwd = the MR source branch worktree); the broker-injected diff rides the brief.
        # the builder is never the acceptor, and no-merge-power holds BY CONSTRUCTION: a reviewer literally cannot
        # write, push, or merge, so it can never both build and accept.
        return ["--allowedTools", " ".join(_READ_ALLOWED),
                "--disallowedTools", " ".join(_READ_DENIED)]
    # read + fail-closed default (unknown/absent capability lands here -> read-only, never write).
    # No --permission-mode: with the deny-list disabling Edit/Write/Bash, edits are refused headless.
    return ["--allowedTools", " ".join(_READ_ALLOWED),
            "--disallowedTools", " ".join(_READ_DENIED)]


def _with_site_rules(grant, capability, name):
    """`grant` with site capability `name`'s extra allow rules appended to --allowedTools and deny rules to
    --disallowedTools (routing.load_site_capabilities validated them at startup). The base's own deny floor is
    untouched and deny wins, so nothing here can lift it. Unknown name, a name whose base isn't `capability`, or
    allow rules on a grant without an allow-list -> ValueError (the spawn is refused)."""
    site = routing.SITE_CAPABILITIES.get(name)
    if site is None or site["base"] != capability:
        raise ValueError(f"refusing to spawn: site capability {name!r} is unknown or not based on "
                         f"{capability!r} (fail-closed)")
    out = list(grant)
    for flag, rules in (("--allowedTools", site["allow"]), ("--disallowedTools", site["deny"])):
        if not rules:
            continue
        if flag not in out:
            raise ValueError(f"refusing to spawn: site capability {name!r} adds {flag} rules to a "
                             f"{capability!r} grant that has none (fail-closed)")
        i = out.index(flag) + 1
        out[i] = " ".join([out[i], *rules])
    return out


class Worker:
    """State for one live ephemeral worker."""
    def __init__(self, key, run_id, worktree):
        self.key = key
        self.run_id = run_id
        self.worktree = worktree
        self.session_id = None      # claude session id, once known (for --resume)
        self.stream_session = None  # Uuid4 we passed as --session-id (stream ON only) -> transcript file
        self.last_output = None      # Most recent raw worker output (wrapped UNTRUSTED on escalate)
        self.last_summary = None     # The latest envelope summary (a GitLab-issue MR description quotes it)
        self.turn_capped = False     # Sticky: some turn hit --max-turns, so a WIP branch at teardown and no MR
        self.api_overloaded = False  # Sticky: every 429/529 back-off retry failed, so exit un-stamped
        self.turn_wall_clock = None  # The CURRENT turn's effective wall-clock (per-capability), used for the liveness check
        self.hard_killed = False     # Set True iff the turn JUST ENDED was the wall-clock hard-kill path;
                                      # consumed (and reset) only by the warm-re-prompt cap in _turn_with_envelope
        self.repo = None             # code worker's resolved BASE repo (venv runner's interpreter lives there)
        self.test_runner = None      # code worker's per-repo runner KEY (routing.resolve_test_runner)
        self.inbox_account = None    # Inbox account resolved at spawn (zulip keys carry no account)
        self.write_grant = None      # Infra live-write grant dict {ticket, bundles, nonce, expires}, if any
        self.write_grant_path = None # Its grant file (revoked at teardown); None = no mcp-rw.sh
        self.audit_since = None      # Epoch the next closing comment's mcp-rw audit lines start from
        self.proc = None            # current in-flight subprocess.Popen, if any
        self.turns = 0              # cumulative turns this session (for rotation)
        self.tokens = 0             # cumulative tokens this session, if envelope exposes usage
        self.forwards = 0           # cumulative --resume forwards (for max-forwards)
        self.in_turn = False        # turn-completion signal source (liveness consumer)
        self.done = False
        # A warm worker now lives across MULTIPLE turns (seed + --resume forwards) until
        # it is closed, so it runs a command loop in its own thread. cmdq carries ("forward", brief) /
        # ("close",) from the LivenessDriver; turn_started stamps the current turn for force-close timing.
        self.cmdq = queue.Queue()   # command loop input (forward / close)
        self.turn_started = None    # ts the current turn began (None between turns), used by turn_age
        self.thread = None          # the worker's own loop thread (worker_alive check)


class CCAdapter(Adapter):
    def __init__(self, *, router, worktree_mgr, prompt_loader, envelope_parser,
                 run_registry=None, semaphore=None,
                 max_turns=15, wall_clock_s=600, max_forwards=10, max_turns_by_capability=None,
                 wall_clock_by_capability=None,
                 rotate_turns=20, rotate_tokens=120_000,
                 setting_sources="", claude_bin="claude",
                 cmd_poll_s=1.0, teardown_join_s=30.0, alert_ops=None,
                 comment_poster=None, ticket_fetcher=None,
                 emit_event=None, mr_note_poster=None, flush_ops=None,
                 label_poster=None, on_mr_verdict=None,
                 ticket_fetch_retry_delay_s=2.0, ticket_fetch_sleep=None, repo_parked_path=None,
                 zulip_account=None, chat_reply=None, hermes=None, agent_dm=None,
                 api_overload_delays_s=None, api_overload_sleep=None,
                 zulip_row_admits=None, on_mr_opened=None, zulip_history=None):
        self.router = router                  # routing.Router: key -> Route(cwd/model/prompt_class/zbus_env/read_only)
        self.worktree_mgr = worktree_mgr      # worktree.WorktreeManager
        self.prompt_loader = prompt_loader    # prompts.PromptLoader (sha256 hash-gate)
        self.envelope_parser = envelope_parser  # envelope.parse / EnvelopeParser
        self.run_registry = run_registry      # echo_guard.RunRegistry (run_id stamping)
        # concurrency cap (§ app owns the value; adapter enforces it). None -> unbounded (tests).
        self._sem = semaphore
        # Hard-kill constraint values (hot-reload-tunable):
        self.max_turns = max_turns
        # Per-capability --max-turns override (constraints.json max_turns_by_capability, e.g.
        # {"code": 40}); a capability absent from it keeps the global max_turns.
        self.max_turns_by_capability = dict(max_turns_by_capability or {})
        self.wall_clock_s = wall_clock_s
        # Per-capability wall-clock override (constraints.json wall_clock_by_capability, e.g.
        # {"code": 1200}); a 40-turn code turn needs more than 600s. An absent capability keeps the global wall_clock_s.
        self.wall_clock_by_capability = dict(wall_clock_by_capability or {})
        self.max_forwards = max_forwards
        self.rotate_turns = rotate_turns
        self.rotate_tokens = rotate_tokens
        self.setting_sources = setting_sources
        self.claude_bin = claude_bin
        self.cmd_poll_s = cmd_poll_s          # warm-worker command-loop poll granularity
        self.teardown_join_s = teardown_join_s  # bounded join of the worker thread in teardown()
        # Normalize the injected sink to a (msg, subject=None) contract. The abort/reject
        # alerts now pass a per-KEY `subject=` so each ticket lands in its OWN notify rate-limit bucket:
        # notify._subject_key otherwise DIGIT-COLLAPSES the ticket id and every ticket-abort shares one
        # bucket, going silent after the cap (which silently blackholed every labelled ticket).
        # notify.wake_ops takes `subject`; a subject-unaware sink (a test's list.append / a 1-arg lambda)
        # is tolerated. Resolve the sink's arity ONCE here via inspect.signature, not with a
        # try/except TypeError around the call: a sink that itself raises TypeError internally used to
        # get called TWICE (once with subject, once without) with the exception still escaping the retry.
        _raw_alert = alert_ops or (lambda msg, subject=None: log.warning("ALERT-OPS: %s", msg))
        try:
            _alert_params = inspect.signature(_raw_alert).parameters.values()
            _alert_takes_subject = ("subject" in inspect.signature(_raw_alert).parameters or
                                     any(p.kind == inspect.Parameter.VAR_KEYWORD for p in _alert_params))
        except (TypeError, ValueError):
            _alert_takes_subject = True   # unintrospectable (e.g. some builtins) -> assume modern contract
        if _alert_takes_subject:
            def _alert_ops(msg, subject=None):
                return _raw_alert(msg, subject=subject)
        else:
            def _alert_ops(msg, subject=None):
                return _raw_alert(msg)
        self._alert_ops = _alert_ops
        # Flush a subject's suppressed-escalation TAIL at worker close. Injected callable
        # flush_ops(subject) -> the real sink is notify.flush_subject (emits the ONE 'N suppressed' summary DM
        # now + clears it, so a flapping-then-quiet subject still reports its final count). Default no-op.
        self._flush_ops = flush_ops or (lambda subject=None: None)
        # The missing envelope to ticket-comment sink. Injected callable
        # comment_poster(task_id, html, run_id) -> bool; default is log-only (mirrors alert_ops).
        # VALUE-FREE here: the real poster (poster.post_comment) shells out to mcp.sh, which owns auth.
        self._comment_poster = comment_poster or (
            lambda task_id, html, run_id: log.info(
                "COMMENT-POST (no-op): task_id=%s run_id=%s len=%d", task_id, run_id, len(html or "")))
        # Input leg: the missing ticket-body to brief sink. Injected callable ticket_fetcher(task_id)
        # -> str|None; default is a log-only no-op (mirrors comment_poster's default). VALUE-FREE here: the
        # real fetcher (ticket_fetch.fetch_ticket) shells out to mcp.sh, which owns auth. The worker stays
        # pure-compute/cred-free; only the broker ever talks to Vikunja.
        self._ticket_fetcher = ticket_fetcher or (lambda task_id: None)
        self._audit_reader = None   # mcp-rw journal reader (since_epoch) -> str|None; None = journalctl
        # A ticket-fetch miss gets exactly ONE retry (short pause, no backoff policy, no config
        # knob) before fail-closing; see the retry in _run_worker. `ticket_fetch_sleep` is injectable so
        # tests can assert the retry fires without actually sleeping.
        self._ticket_fetch_retry_delay_s = ticket_fetch_retry_delay_s
        self._ticket_fetch_sleep = ticket_fetch_sleep or time.sleep
        # 429/529 back-off schedule (one retry per delay) + an injectable sleep so tests don't wait.
        self._api_overload_delays_s = tuple(API_OVERLOAD_RETRY_DELAYS_S if api_overload_delays_s is None
                                            else api_overload_delays_s)
        self._api_overload_sleep = api_overload_sleep or time.sleep
        # MR-reviewer: the broker RE-INGESTS a synthetic mr.opened event in-process after a code
        # worker's MR opens (instead of an HTTP self-POST). Broker.ingest already runs echo_guard +
        # classify + normalize, so an in-process call gives identical filtering with no self-HMAC/network
        # hop. Injected callable emit_event(evt) -> ignored; default is a log-only no-op.
        self._emit_event = emit_event or (
            lambda evt: log.info("EMIT-EVENT (no-op): %s", evt.get("event_name")))
        # MR-reviewer: the verdict sink. The reviewer is cred-free + posts nothing itself; the
        # BROKER posts the {status,summary} verdict as an MR note via this injected callable
        # mr_note_poster(project, iid, body, run_id, harness=None) -> id|None. Default is log-only (mirrors
        # comment_poster). The harness (Route.adapter) is stamped into the hidden broker-run tag.
        self._mr_note_poster = mr_note_poster or (
            lambda project, iid, body, run_id, harness=None: log.info(
                "MR-NOTE (no-op): %s!%s len=%d", project, iid, len(body or "")))
        # The "waiting on operator" label sink. Injected callable
        # label_poster(task_id) -> bool; default is log-only (mirrors comment_poster/mr_note_poster).
        # VALUE-FREE here: the real sink (poster.apply_label) shells out to mcp.sh, which owns auth.
        self._label_poster = label_poster or (
            lambda task_id: log.info("LABEL-POST (no-op): task_id=%s", task_id))
        # Ticket reads and write-back go through a tracker by ref (trackers/, docs/trackers.md). The Vikunja
        # tracker wraps the three sinks above LATE-BOUND, so they stay its int-task-id internals and a test
        # that swaps one after construction still reaches it. Assign has no sink here: poster.assign_user.
        self._trackers = Registry([VikunjaTracker(
            post_comment=lambda tid, body, run_id: self._comment_poster(tid, body, run_id),
            fetch_text=lambda tid: self._ticket_fetcher(tid),
            apply_label=lambda tid: self._label_poster(tid)), GitLabTracker()])
        # Fleet CD: post-verdict hook on_mr_verdict(mr, env, run_id) (the real one is cd.on_review_verdict,
        # itself a no-op unless CD_ENABLED). None means nothing runs.
        self._on_mr_verdict = on_mr_verdict
        # Zulip-wake: zulip_account(bot) returns the inbox account (None = unmapped, fail closed at spawn), and
        # chat_reply(bot, sender_id, text, run_id) is the BROKER's DM back to the original sender as that bot
        # (zulip_wake.ZulipWake.reply; zbus via the bot-env PATH, value-free). Defaults: unmapped / log-only.
        self._zulip_account = zulip_account or (lambda bot, sender_id=None: None)
        self._chat_reply = chat_reply or (
            lambda bot, sender_id, text, run_id: log.info(
                "CHAT-REPLY (no-op): bot=%s sender=%s run_id=%s len=%d", bot, sender_id, run_id, len(text or "")))
        # zulip_row_admits(bot, sender_id, row) -> bool, the spawn gate for an account-less chat row
        # (a chat row with no inbox account): the wake conditions hold AND the bot map names THAT row (zulip_wake.row_admits). Default
        # False = fail closed. on_mr_opened(opened, branch, task_id, run_id): fired after a worker MR opens (the
        # real one is cd.on_mr_opened, a no-op unless the repo is in AUTO_MERGE_REPOS). None -> nothing runs.
        self._zulip_row_admits = zulip_row_admits or (lambda bot, sender_id, row: False)
        self._on_mr_opened = on_mr_opened
        # zulip_history(bot, sender_id, before_msg_id, skip) -> prior DM rows (zulip_wake.ZulipWake.history;
        # [] unless the bot map row opts in with "history"). Called for the SEED turn only. Default: none.
        self._zulip_history = zulip_history or (lambda bot, sender_id, before_msg_id, skip=1: [])
        # The hermes harness for Route.adapter == "hermes" rows (adapters/hermes.HermesHarness); built lazily
        # on first use when not injected (tests inject one with a fake hermes binary).
        self.hermes = hermes
        # agent_dm(handle, text) is the broker's verbatim bus DM to a STANDING agent (real sink: notify.dm),
        # used when a row's publish names an "mr_notify_handle" (infra row: sysop-bot gets the MR link on open).
        self._agent_dm = agent_dm or (lambda handle, text: log.info("AGENT-DM (no-op): %s len=%d", handle, len(text)))
        self._workers = {}                    # key -> Worker
        self._lock = threading.Lock()
        self._threads = {}                    # key -> Thread (for join in tests/teardown)
        # One lock per publish-target repo so our own wiki write workers single-flight
        # the whole cut->edit->publish->teardown; the publish() rebase-retry backstops the un-locked
        # standing wiki-fs/wiki-git bridges. Created lazily under self._lock in _repo_lock().
        self._repo_locks = {}
        # Back-ref set by Coalescer.__init__ so the worker thread can advertise its turn
        # transitions (open_running <-> open_idle) through the coalescer's lock. `state` stays sole sync token.
        self.coalescer = None
        # For REPO_INFER_COMMENT_RERUN_ENABLED: subject_key -> row domain for tickets the repo
        # ladder parked, persisted like coalescer's completed.json (None == in-memory only), plus the queued
        # human comment text a rerun spawn feeds into the ladder.
        self._repo_parked_path = repo_parked_path or None
        self._parked_lock = threading.Lock()
        self._repo_parked = {}
        self._repo_rerun = {}
        if self._repo_parked_path:
            try:
                with open(self._repo_parked_path) as f:
                    self._repo_parked = {str(k): str(v) for k, v in json.load(f).items()}
            except FileNotFoundError:
                pass
            except Exception as e:
                log.warning("repo-parked load failed (%s): %s - starting empty", self._repo_parked_path, e)
        # Maps subject_key to queued human comment(s) (in-memory only; see queue_comment_respawn
        # / _pop_comment_respawn above). Generalizes the `_repo_rerun` queue beyond the repo ladder.
        self._comment_respawn_lock = threading.Lock()
        self._comment_respawn_queue = {}

    def set_coalescer(self, coalescer):
        self.coalescer = coalescer

    def _repo_lock(self, repo):
        """Get-or-create the single-flight lock for a publish-target repo."""
        with self._lock:
            lk = self._repo_locks.get(repo)
            if lk is None:
                lk = self._repo_locks[repo] = threading.Lock()
            return lk

    def _advertise_running(self, key):
        if self.coalescer is not None:
            self.coalescer.advertise_running(key)

    def _advertise_idle(self, key):
        # Turn-completion boundary: advertise open_idle so the LivenessDriver can
        # deliver buffered forwards or close. This is the ONLY driver of the open-to-idle transition; never pid.
        if self.coalescer is not None:
            self.coalescer.mark_idle(key)

    def _settle_self(self, key, mark_completed=True):
        # A worker exiting under its OWN power (finished / max-forwards / raised) settles its own
        # coalescer entry so the LivenessDriver never force-closes an already-dead thread (the spurious
        # FORCE-CLOSE / zombie-respawn class). No-op when the liveness close path already owns it
        # (state closing/closed). Returns the popped record (leftover fwd_buffer re-armed by the caller)
        # or None. Called from _run_worker's finally; the coalescer helper does the check+pop under lock.
        # mark_completed=False for a spawn-time fail-closed REJECT that never actually ran a worker
        # (see `attempted` in _run_worker); a rejected spawn must stay eligible to re-spawn once fixed.
        if self.coalescer is not None:
            return self.coalescer.settle_self(key, mark_completed=mark_completed)
        return None

    # ---- tickets by ref: one tracker registry for every read and write-back (trackers/) ----
    def tracker(self, ref):
        """The Tracker registered for a ticket ref, or None (no ref, or its tracker isn't registered)."""
        return self._trackers.get(ref)

    def _tracker_comment(self, ref, body, run_id):
        """Comment on the ticket by ref; no tracker means no call. A raising sink still reaches the caller's
        own try/except (the tracker doesn't swallow it), so every call site keeps its log line."""
        t = self._trackers.get(ref)
        return t.comment(ref, body, run_id) if t is not None else False

    def _tracker_label(self, ref, name):
        t = self._trackers.get(ref)
        return t.label(ref, name) if t is not None else False

    def _tracker_fetch(self, ref):
        """The fetched Ticket, or None (no tracker, or the fetch failed)."""
        t = self._trackers.get(ref)
        return t.fetch(ref) if t is not None else None

    def _ticket_url(self, ref):
        t = self._trackers.get(ref)
        return t.url(ref) if t is not None else ""

    def _dash_url(self, ref):
        """` - <url>` for an abort alert, or "" when the ref has no tracker (no dangling dash)."""
        url = self._ticket_url(ref)
        return f" - {url}" if url else ""

    def _ticket_link(self, ref):
        """` ticket <url>` for an alert line, or "" when the ref has no tracker."""
        url = self._ticket_url(ref)
        return f" ticket {url}" if url else ""

    # ---- post a successful turn's envelope summary as a ticket comment ----
    def _post_envelope(self, brief, run_id, env, worker=None):
        """On a non-None envelope, post `summary` (HTML) to the originating ticket via the injected
        comment_poster. task_id comes from the (collapsed) brief that seeded/forwarded this turn.
        Judgement: posts on EVERY successful turn's envelope (seed + each forward), not just a
        terminal status=="done": each turn is its own result and the ticket should reflect it as it
        happens; a caller wanting terminal-only can filter on env['status'] in its own comment_poster.
        A poster failure must NEVER wedge the worker: caught + logged here on top of poster.py's own
        internal catch-all, so even a badly-behaved injected poster can't break a turn boundary."""
        zc = brief.get("zulip") if isinstance(brief, dict) else None
        if zc:
            # A Zulip chat turn answers the person who DMed; the broker (never the worker) sends it, to
            # the sender id carried by the broker-built brief, as the bot they DMed. A parse-miss still gets a
            # short fixed notice (the alert recipient is notified separately) so the sender isn't left hanging.
            text = (env.get("summary") or "").strip() if env is not None else ""
            if not text:
                text = CHAT_FALLBACK_REPLY
            try:
                self._chat_reply(zc.get("bot"), zc.get("sender_id"), text[:CHAT_REPLY_MAX_CHARS], run_id)
            except Exception as e:                         # never let a reply failure wedge the worker
                log.warning("chat_reply failed bot=%s run_id=%s: %s", zc.get("bot"), run_id, e)
        if env is None:
            return                                        # parse-miss: nothing to post (escalation path
                                                            # already covers this in _turn_with_envelope)
        task_id = brief.get("task_id") if isinstance(brief, dict) else None
        ref = tickets.vikunja_ref(task_id) or _gitlab_issue_ref(worker)
        if worker is not None and env.get("summary"):
            worker.last_summary = env["summary"]           # the MR description quotes the latest one
        if ref is None:
            if not zc:
                log.info("no task_id on brief -> skipping comment post run_id=%s", run_id)
        else:
            summary = env.get("summary", "")
            if getattr(worker, "write_grant_path", None):   # Append this ticket's mcp-rw audit lines
                summary = (summary or "") + self._write_audit_html(worker, task_id)
            if env.get("gate_denials"):                     # hermes eph-gate deny-and-report
                summary = (summary or "") + _gate_denials_html(env["gate_denials"])
            try:
                self._tracker_comment(ref, summary, run_id)
            except Exception as e:                         # never let a poster failure wedge the worker
                log.warning("comment_poster failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        mintable = [d for d in env.get("gate_denials") or () if d.get("sha")]
        if mintable:
            # An allow-once is minted by hand only. One DM per turn listing each sha byte-exact plus the exact
            # subject (fenced UNTRUSTED: worker-chosen text). The re-run is COMMENT_RESPAWN after the mint.
            ticket_link = self._ticket_link(ref)
            lines = "\n".join(f"- {d.get('tool')}: allow-once sha {d['sha']}" for d in mintable[:10])
            subj = "\n\n".join(str(d.get("subject") or d.get("path") or "") for d in mintable[:10])
            try:
                self._alert_ops(f"hermes general worker: the policy judge denied {len(mintable)} call(s){ticket_link} - "
                                f"mint in the judge's eph-general/allow-once/ dir if warranted, then comment to "
                                f"respawn:\n{lines}" + wrap_untrusted(subj, cap=4000),
                                subject=f"gate-deny:{getattr(worker, 'key', None) or task_id}")
            except Exception as e:                         # never let an alert failure wedge the worker
                log.warning("gate-deny ops DM failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        if env.get("status") != "blocked":
            return
        # A blocked result ALWAYS sends an alert DM (ticket link and reason); the reason is worker-derived,
        # so it rides fenced as UNTRUSTED. A turn-cap exit says so up front.
        turn_capped = bool(getattr(worker, "turn_capped", False))
        ticket_link = self._ticket_link(ref)
        cap_note = " - hit the --max-turns cap (any WIP is pushed to a branch at teardown)" if turn_capped else ""
        try:
            self._alert_ops(f"worker blocked{ticket_link}{cap_note}"
                            + wrap_untrusted(_plain_reason(env.get("summary"))),
                            subject=f"blocked:{getattr(worker, 'key', None) or task_id}")
        except Exception as e:                             # never let an alert failure wedge the worker
            log.warning("blocked ops DM failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        # Apply the "waiting on operator" label ONLY when the envelope explicitly says it
        # waits on the operator (needs_operator). A turn-cap exit is NEVER an operator wait (the alert DM covers it).
        # label_poster (poster.apply_label) is idempotent server-side, so no extra dedupe is needed here.
        if task_id is not None and env.get("needs_operator") is True and not turn_capped:
            try:
                self._tracker_label(ref, "parked")
            except Exception as e:                         # never let a poster failure wedge the worker
                log.warning("label_poster failed task_id=%s run_id=%s: %s", task_id, run_id, e)

    def _write_audit_html(self, worker, task_id):
        """The HTML block of this ticket's `mcp-rw` journal audit lines since the last comment (broker-read,
        HTML-escaped). Advances worker.audit_since so a forward's comment only carries new lines. Never raises."""
        try:
            since = worker.audit_since or time.time()
            lines = write_grant.audit_lines(task_id, (worker.write_grant or {}).get("nonce"), since,
                                            reader=self._audit_reader)
            worker.audit_since = time.time()
            bundles = ", ".join((worker.write_grant or {}).get("bundles") or [])
            head = f"<p><b>mcp-rw audit</b> (write grant: {html.escape(bundles)}):</p>"
            if lines is None:
                return head + "<p>audit lines unavailable (journal read failed).</p>"
            if not lines:
                return head + "<p>no mcp-rw calls logged.</p>"
            return head + "<pre>" + html.escape("\n".join(lines)) + "</pre>"
        except Exception as e:                             # an audit failure must never block the comment
            log.warning("mcp-rw audit block failed task_id=%s: %s", task_id, e)
            return ""

    def _chat_notice(self, zc, text, run_id):
        """A canned Zulip DM to a chat worker's sender (max-forwards / worker failure) through the SAME
        guarded chat_reply sink as a normal reply. No-op for non-chat workers; never raises."""
        if not zc:
            return
        try:
            self._chat_reply(zc.get("bot"), zc.get("sender_id"), text, run_id)
        except Exception as e:
            log.warning("chat notice failed bot=%s run_id=%s: %s", zc.get("bot"), run_id, e)

    def _with_dm_history(self, key, brief, zc, run_id):
        """A COLD chat start -> the brief with the thread's prior DMs (zulip_history; [] unless the bot map
        row opts in) injected into the first DM's UNTRUSTED block, excluding the triggering DM(s) and anything
        newer. Fail-soft: any error -> the brief unchanged + one log line. Never raises."""
        import coalescer                                 # the DM block renderer lives beside _render_dm
        try:
            ids = [int(d.get("msg_id")) for d in (brief.get("dms") or []) if str(d.get("msg_id") or "").isdigit()]
            if not ids:
                return brief
            rows = self._zulip_history(zc.get("bot"), zc.get("sender_id"), min(ids), skip=len(ids)) or []
            out = coalescer.inject_dm_history(brief, rows)
            if out is not brief:
                log.info("chat history: key=%s run_id=%s -> %d prior DM(s) injected", key, run_id, len(rows))
            return out
        except Exception as e:
            log.warning("chat history: key=%s run_id=%s failed (%s) -> no history", key, run_id, e)
            return brief

    def _nag_push_refused(self, key, run_id, err):
        """The broker refused to push the worker's branch because the repo's push URL is not
        on the BROKER_PUSH_HOSTS allowlist (fail closed). Say so on the source ticket. Never raises."""
        ref = _ops_ref(key)
        if ref is None:
            return
        try:
            self._tracker_comment(ref, (
                f"<p><b>Push refused</b>: the worker's commit was NOT pushed: {html.escape(str(err))}. "
                f"Point the repo's <code>origin</code> at an allowed push host and re-run.</p>"), run_id)
        except Exception as e:
            log.warning("push-refused ticket comment failed task_id=%s run_id=%s: %s",
                        tickets.vikunja_id(key), run_id, e)

    # ---- turn-cap WIP save: commit and push an eph/*-wip branch (NO MR) before teardown ----
    def _save_wip(self, key, run_id):
        """Push a turn-capped worker's uncommitted/unpushed work to an eph/*-wip branch (worktree.save_wip),
        then name the branch on the ticket and in one alert DM. FAIL-SAFE: a save/push failure is logged + DM'd
        and teardown continues; never raises (a lost WIP must never wedge the worktree removal)."""
        task_id = tickets.vikunja_id(key)
        ref = _ops_ref(key)
        ticket_link = self._ticket_link(ref)
        try:
            branch = self.worktree_mgr.save_wip(key)
        except Exception as e:
            log.warning("save_wip key=%s run_id=%s failed (continuing teardown): %s", key, run_id, e)
            try:
                self._alert_ops(f"turn-cap WIP save FAILED{ticket_link} ({key}): {e}", subject=f"worker:{key}")
            except Exception as e2:
                log.warning("wip-fail ops DM failed key=%s: %s", key, e2)
            if isinstance(e, PushTargetRefused):
                self._nag_push_refused(key, run_id, e)
            return None
        if not branch:
            return None                                   # nothing edited/committed -> nothing to save
        if ref is not None:
            try:
                self._tracker_comment(ref, (
                    f"<p><b>Turn cap hit</b>: the worker ran out of turns before finishing. Its work in "
                    f"progress is saved on branch <code>{html.escape(branch)}</code> (no MR opened).</p>"), run_id)
            except Exception as e:                         # never let a poster failure wedge teardown
                log.warning("wip ticket comment failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        try:
            self._alert_ops(f"turn-cap WIP saved{ticket_link} -> branch {branch} (no MR)", subject=f"worker:{key}")
        except Exception as e:
            log.warning("wip ops DM failed key=%s: %s", key, e)
        return branch

    def _save_patch(self, key, run_id):
        """A code worker that made NO commit (e.g. its `git commit` was refused) may still have edited
        files; save them as a patch file outside the worktree (worktree.save_patch) and name its path in an alert
        DM and on the ticket, so removing the worktree doesn't lose the work. FAIL-SAFE: never raises."""
        task_id = tickets.vikunja_id(key)
        ref = _ops_ref(key)
        ticket_link = self._ticket_link(ref)
        if not hasattr(self.worktree_mgr, "save_patch"):
            return None
        try:
            patch = self.worktree_mgr.save_patch(key)
        except Exception as e:
            log.warning("save_patch key=%s run_id=%s failed: %s", key, run_id, e)
            try:
                self._alert_ops(f"no-commit patch save FAILED{ticket_link} ({key}): {e}", subject=f"worker:{key}")
            except Exception as e2:
                log.warning("patch-fail ops DM failed key=%s: %s", key, e2)
            return None
        if not patch:
            return None                                   # nothing uncommitted -> nothing to save
        if ref is not None:
            try:
                self._tracker_comment(ref, (
                    f"<p><b>No commit made</b>: the worker left uncommitted edits. They are saved as a patch "
                    f"at <code>{html.escape(patch)}</code> (no MR opened).</p>"), run_id)
            except Exception as e:
                log.warning("patch ticket comment failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        try:
            self._alert_ops(f"worker made no commit{ticket_link} -> uncommitted edits saved to {patch} (no MR)",
                            subject=f"worker:{key}")
        except Exception as e:
            log.warning("patch ops DM failed key=%s: %s", key, e)
        return patch

    # ---- MR-opened / MR-verdict notify: ticket comment and alert DM ----
    def _notify_mr_opened(self, task_id, opened, branch, run_id, notify_handle=None):
        """On a successful MR-open (open_mr returned a dict): post a Vikunja comment naming the MR + the
        source branch on the originating ticket, and send one short alert DM. Best-effort, mirrors every
        other sink here: caught + logged so a failure here can NEVER wedge the publish path (each
        underlying sink already swallows its own failures independently; this is belt-and-suspenders for
        a misbehaving injected sink). `task_id` is None for an issue-code (GitHub/GitLab) MR with no
        Vikunja ticket; the ticket-comment step degrades to a no-op, the alert DM still fires."""
        web_url = opened.get("web_url")
        ref = tickets.vikunja_ref(task_id)
        if task_id is not None:
            try:
                self._tracker_comment(ref, (
                    f"<p><b>MR opened:</b> "
                    f"<a href=\"{html.escape(web_url or '')}\">!{opened.get('iid')}</a> "
                    f"(branch <code>{html.escape(branch)}</code>)</p>"), run_id)
            except Exception as e:                         # never let a poster failure wedge the worker
                log.warning("mr-open ticket comment failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        ticket_link = self._ticket_link(ref)
        try:
            self._alert_ops(f"MR opened{ticket_link} -> {web_url}",
                            subject=f"mr-open:{task_id if task_id is not None else branch}")
        except Exception as e:                             # never let an alert failure wedge the worker
            log.warning("mr-open ops DM failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        if notify_handle:                                  # e.g. infra row -> the standing deployer
            try:
                self._agent_dm(notify_handle, f"eph MR opened{ticket_link} -> {web_url} (branch {branch}); "
                                              f"review, merge and deploy are yours")
            except Exception as e:                         # never let a DM failure wedge the worker
                log.warning("mr-open agent DM %s failed task_id=%s run_id=%s: %s", notify_handle, task_id, run_id, e)

    def _notify_mr_verdict(self, mr, run_id, env, note_id):
        """On the reviewer's verdict (after the MR note itself is posted): post a PASS/FAIL comment and
        link to the reviewer's note on the originating ticket, and send one short alert DM. `mr.get(
        "task_id")` is the SOURCE Vikunja ticket that opened the reviewed MR (threaded through
        collapse()'s "mr" block); None for an issue-code MR with no Vikunja ticket, same
        degrade-to-no-op-comment/still-DM contract as _notify_mr_opened. Best-effort: caught + logged so a
        failure here can never wedge the worker."""
        task_id = mr.get("task_id")
        verdict = "PASS" if env.get("status") == "pass" else "FAIL"
        web_url = mr.get("web_url")
        note_url = f"{web_url}#note_{note_id}" if web_url and note_id is not None else web_url
        ref = tickets.vikunja_ref(task_id)
        if task_id is not None:
            try:
                self._tracker_comment(ref, (
                    f"<p><b>MR review verdict: {verdict}</b> ("
                    f"<a href=\"{html.escape(note_url or '')}\">reviewer note</a>)</p>"), run_id)
            except Exception as e:                         # never let a poster failure wedge the worker
                log.warning("mr-verdict ticket comment failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        ticket_link = self._ticket_link(ref)
        try:
            self._alert_ops(f"MR review {verdict}{ticket_link} -> {note_url}",
                            subject=f"mr-verdict:{task_id if task_id is not None else mr.get('mr_iid')}")
        except Exception as e:                             # never let an alert failure wedge the worker
            log.warning("mr-verdict ops DM failed task_id=%s run_id=%s: %s", task_id, run_id, e)

    MR_REVIEW_SKIPPED_NOTE = (
        "ℹ️ **Review skipped: diff unavailable.** The broker could not compute this MR's diff ({reason}), so "
        "no reviewer was run. This is NOT a PASS or a BLOCK. The review was left re-armable (not marked "
        "completed), so a fresh `mr.opened` event for this MR re-runs it; ops has been notified.")

    def _skip_mr_review_no_diff(self, mr, key, run_id, harness="cc", reason="empty after every retry"):
        """No trustworthy MR diff (empty after all mr_diff retries, the cut is the merge-base, or the
        worktree could not be cut at the MR head). Never review the base tree: post a neutral (not PASS/BLOCK)
        MR note, a ticket comment when there is a source ticket, and escalate by alert DM. No _notify_mr_verdict /
        on_mr_verdict (no verdict exists, so CD never acts). Never raises."""
        log.warning("mr-review key=%s run_id=%s: diff unavailable (%s) -> review skipped", key, run_id, reason)
        if mr.get("mr_iid"):
            try:
                self._mr_note_poster(mr.get("project"), mr["mr_iid"],
                                     self.MR_REVIEW_SKIPPED_NOTE.format(reason=reason), run_id, harness=harness)
            except Exception as e:                         # never let a poster failure wedge the worker
                log.warning("mr skipped-note post failed project=%s iid=%s run_id=%s: %s",
                            mr.get("project"), mr.get("mr_iid"), run_id, e)
        task_id = mr.get("task_id")
        web_url = mr.get("web_url")
        ref = tickets.vikunja_ref(task_id)
        if task_id is not None:
            try:
                self._tracker_comment(ref, (
                    "<p><b>MR review skipped: diff unavailable</b> ("
                    f"<a href=\"{html.escape(web_url or '')}\">MR</a>, no verdict; ops notified)</p>"), run_id)
            except Exception as e:                         # never let a poster failure wedge the worker
                log.warning("mr-skipped ticket comment failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        ticket_link = self._ticket_link(ref)
        try:
            self._alert_ops(f"MR review SKIPPED (diff unavailable: {reason}; no verdict, re-trigger needed)"
                            f"{ticket_link} -> {web_url or mr.get('project')}!{mr.get('mr_iid')}",
                            subject=f"mr-verdict:{task_id if task_id is not None else mr.get('mr_iid')}")
        except Exception as e:                             # never let an alert failure wedge the worker
            log.warning("mr-skipped ops DM failed task_id=%s run_id=%s: %s", task_id, run_id, e)

    # ---- MR-reviewer: post the review verdict as an MR note (the reviewer posts nothing itself) ----
    def _post_mr_verdict(self, mr, run_id, env, harness="cc"):
        """Post the seed turn's {status,summary} verdict as a note on the reviewed MR via the injected
        mr_note_poster. The reviewer is a cred-free PURE-READ worker; the BROKER is the sink (mirrors
        _post_envelope, MR-side). A None envelope (parse-miss) or a missing mr_iid is a no-op. On
        status=="block" the note leads with a clear ⚠️ marker so a human sees it. A poster failure must
        NEVER wedge the worker: caught + logged here on top of gitlab_mr.post_mr_note's own catch-all.
        Also notifies the source ticket and sends an alert DM of the PASS/FAIL verdict (_notify_mr_verdict); fires
        regardless of whether the MR-note post itself succeeded (note_id is None on a skipped/failed
        post; the ticket and alert notify still carries the verdict, just without a working note link).
        `harness` (the route's Route.adapter, NOT model output) is passed to the poster so the hidden
        broker-run tag carries `harness=<name>` for hermes-vs-cc verdict audits. No CD-gating change."""
        if env is None or not mr.get("mr_iid"):
            return
        body = env.get("summary", "")
        if env.get("status") == "block":
            body = f"⚠️ **BLOCK**: {body}"
        note_id = None
        try:
            note_id = self._mr_note_poster(mr.get("project"), mr["mr_iid"], body, run_id, harness=harness)
        except Exception as e:                             # never let a poster failure wedge the worker
            log.warning("mr_note_poster failed project=%s iid=%s run_id=%s: %s",
                        mr.get("project"), mr.get("mr_iid"), run_id, e)
        self._notify_mr_verdict(mr, run_id, env, note_id)
        if self._on_mr_verdict is not None:                # CD auto-merge (never wedges the worker)
            try:
                self._on_mr_verdict(mr, env, run_id)
            except Exception as e:
                log.warning("on_mr_verdict failed project=%s iid=%s run_id=%s: %s",
                            mr.get("project"), mr.get("mr_iid"), run_id, e)

    # ---- constraints 1, 3, 4 and capability enforcement: argv builder ----
    def build_argv(self, prompt, model, system_prompt, resume_session=None, capability="read",
                   mcp_config_path=None, test_runner=None, setting_sources=None, effort=None,
                   bypass_classifier=False, session_uuid=None, test_repo=None, write_grant=False,
                   site_capability=""):
        if not system_prompt:
            raise ValueError("refusing to spawn without a system prompt (constraint #1: never --bare)")
        # Constraint 4, charter-strip: setting-sources defaults to self.setting_sources ("") so every existing worker
        # stays charter-blind (no project/user CLAUDE.md). A route may OVERRIDE per-row: the general worker
        # passes "user" to DELIBERATELY load ~/.claude/CLAUDE.md (-> the user-level rules) + ~/.claude/settings.json
        # (-> the autoMode classifier config). None = keep the charter-blind default. The charter-free
        # worktree (strip_charter) still prevents a *repo* charter from leaking in via cwd regardless.
        eff_setting_sources = self.setting_sources if setting_sources is None else setting_sources
        argv = [self.claude_bin, "-p", prompt,
                "--model", model,                         # constraint 3: explicit model, never implicit
                "--system-prompt", system_prompt,         # constraint 4: charter REPLACE
                "--max-turns", str(self.max_turns_by_capability.get(capability, self.max_turns)),  # constraint 6: turn cap
                "--output-format", "json",                # envelope.py parses this
                "--setting-sources", eff_setting_sources]  # per-route (default "" = charter-blind)
        if effort:                                    # non-default backend (deepseek/glm) sets its thinking level
            argv += ["--effort", effort]
        # DERIVE the worker's tool grants from its declared capability. capability
        # defaults to "read" so any caller that omits it (or an unknown value) gets read-only:
        # FAIL-CLOSED. See capability_tool_grant() for the empirically-verified security model.
        grant = capability_tool_grant(capability, test_runner=test_runner,
                                      bypass_classifier=bypass_classifier, test_repo=test_repo,
                                      write_grant=write_grant)   # mcp-rw.sh only with a grant file
        if site_capability:   # a broker.yaml capability: its base grant (above) plus its own extra rules
            grant = _with_site_rules(grant, capability, site_capability)
        # Wiki skills (EPH_WIKI_SKILLS_ENABLED, default OFF). An allow-listed capability does NOT include
        # the Skill tool, so a headless -p worker would be refused it -> append `Skill` to THAT capability's
        # --allowedTools only when the plugin dir is actually passed. The deny-floor is untouched (deny wins);
        # `general` has no allow-list (classifier-judged) so it just gets the --plugin-dir.
        plugin_dir = _wiki_skills_plugin_dir()
        if plugin_dir:
            if "--allowedTools" in grant:
                i = grant.index("--allowedTools") + 1
                grant[i] = grant[i] + " Skill"
            grant += ["--plugin-dir", plugin_dir]
        argv += grant
        # An inbox worker is Gmail-mutation-capable, so it MUST load ONLY its per-account
        # MCP config (that ONE account's Gmail + Hindsight) and NOTHING from the broker host's ambient .mcp.json.
        # --strict-mcp-config (verified present on the pinned CLI 2.1.272) loads ONLY --mcp-config's servers,
        # ignoring every other MCP configuration = the containment that keeps one account's worker off every
        # other account (and off the broker host's full connector set). FAIL-CLOSED: refuse to spawn if the per-account
        # config path is absent/missing; never spawn a Gmail-capable worker with no/ambient MCP. The path
        # is per-account, passed IN by the caller (_one_turn), never hard-coded here.
        # inbox = per-account (Gmail+Hindsight); general = a minimal Hindsight-ONLY config. BOTH need
        # --strict-mcp-config for the SAME reason: without it a -p worker inherits the full
        # account-connector surface (security REJECT 2026-09-15). The general worker has NO --allowedTools
        # (its Bash is classifier-judged), so strict-mcp-config is the ONLY thing keeping credentialed MCP
        # tools (the write-capable connectors) out of its callable surface, hence fail-closed here, no ambient fallback.
        # The general config file is non-secret (unauthenticated Hindsight) and is authored/placed by
        # hand at deploy (same as the inbox config, which is hand-authored); the repo only points and gates.
        if capability in ("inbox", "inbox-chat", "general"):   # inbox-chat = per-account
            if not mcp_config_path or not os.path.exists(mcp_config_path):
                raise ValueError(
                    f"refusing to spawn {capability} worker without its MCP config (fail-closed): "
                    f"{mcp_config_path!r} is missing; the operator places these config files "
                    f"(inbox = secret per-account; general = non-secret Hindsight-only)")
            argv += ["--mcp-config", mcp_config_path, "--strict-mcp-config"]
        # The inbox worker's mechanical TRASH floor. `--settings` loads independently of
        # --setting-sources (even under --bare it still applies, per the CLI's own help text); required
        # here because this route's --setting-sources stays "" (charter-blind), so a project/user
        # .claude/settings.json would never be read for its hooks.
        # Every capability ALSO gets the attribution-off settings (see
        # _ATTRIBUTION_OFF_SETTINGS) merged in; inbox's own PreToolUse hook settings win no fields, so a
        # plain dict-merge is safe (disjoint top-level keys: "attribution" vs "hooks").
        settings = {**_ATTRIBUTION_OFF_SETTINGS,
                    **(_INBOX_HOOK_SETTINGS if capability in ("inbox", "inbox-chat") else {})}
        argv += ["--settings", json.dumps(settings)]
        if resume_session:
            argv += ["--resume", resume_session]          # warm turn (session rotation drops this)
        elif session_uuid:
            argv += ["--session-id", session_uuid]        # Fresh session at a known id (stream ON only)
        assert "--bare" not in argv, "constraint #1 violated: --bare must never appear"
        return argv

    # ---- primitive: spawn (async: dispatch and return immediately) ----
    # ---- repo inference ladder (REPO_INFER_ENABLED); replaces the no-repo: fail-closed reject ----
    def _post_ticket_note(self, key, body, run_id, parked=False):
        """Best-effort ticket comment (plus an optional waiting-on-operator label). Never raises into the spawn path."""
        ref = _ops_ref(key)
        if ref is None:
            return
        try:
            self._tracker_comment(ref, body, run_id)
        except Exception as e:
            log.warning("repo-infer comment post failed key=%s: %s", key, e)
        if parked:
            try:
                self._tracker_label(ref, "parked")
            except Exception as e:
                log.warning("repo-infer parked label failed key=%s: %s", key, e)

    def _code_repo_park(self, key, run_id, headline, detail_html, alert, quiet=False):
        """No worker: tell the author on the ticket (WAITING ON YOU + the operator-wait label) and send one alert DM. `quiet`
        (a comment-rerun re-park): the first park already sent its DM, so this one is journal-only."""
        self._post_ticket_note(key, (
            f"<p><b>⏳ WAITING ON YOU:</b> {headline}{' (re-checked with your comment)' if quiet else ''} - "
            f"no code worker was spawned.</p>{detail_html}"
            f"<p>Add a bare line <code>repo:{html.escape(os.getenv('REPO_CATALOG_NS', ''), quote=False) or '&lt;namespace&gt;'}/&lt;name&gt;</code> (or <code>repo:&lt;absolute-path&gt;</code>) "
            "to the ticket description, then re-trigger"
            + (" - or just reply here with a comment naming the repo" if routing.repo_infer_comment_rerun_enabled()
               else "") + ".</p>"), run_id, parked=True)
        if quiet:
            log.info("repo-infer key=%s re-parked after comment rerun: %s", key, alert)
        else:
            self._alert_ops(f"code worker {key} parked: {alert}", subject=f"reject:{key}")
        return None, None

    # ---- ladder-parked marker and comment-rerun queue (REPO_INFER_COMMENT_RERUN_ENABLED) ----
    def _save_repo_parked(self):
        """Atomic rewrite (tmp + os.replace), caller holds _parked_lock. A write failure only loses durability."""
        if not self._repo_parked_path:
            return
        try:
            tmp = self._repo_parked_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._repo_parked, f)
            os.replace(tmp, self._repo_parked_path)
        except Exception as e:
            log.warning("repo-parked persist failed (%s): %s", self._repo_parked_path, e)

    def repo_parked(self, key):
        """Row domain of a ladder-parked subject, else None."""
        with self._parked_lock:
            return self._repo_parked.get(key)

    def _set_repo_parked(self, key, domain):
        with self._parked_lock:
            if domain is None:
                if self._repo_parked.pop(key, None) is None:
                    return
            else:
                self._repo_parked[key] = domain
            self._save_repo_parked()

    def queue_repo_rerun(self, key, comment_html):
        """A human comment on a parked subject: its text feeds the next ladder run on `key` (appended)."""
        with self._parked_lock:
            prev = self._repo_rerun.get(key)
            self._repo_rerun[key] = f"{prev}\n{comment_html}" if prev else comment_html

    # ---- generalized comment-respawn queue (COMMENT_RESPAWN_ENABLED) ----
    def queue_comment_respawn(self, key, comment_html, doer=None):
        """app._comment_respawn calls this right before re-arming a spun-down subject: the triggering human
        comment is queued here (oldest first; MULTIPLE comments queued before the next spawn consumes them
        all ACCUMULATE, same append spirit as queue_repo_rerun above). _run_worker pops + merges the whole
        queued thread into the fetched ticket text (same injection point as every other ticket enrichment,
        see _run_worker just after the ticket fetch) so the worker's FIRST turn sees why it was woken."""
        entry = (f"<p><b>{html.escape(str(doer or 'someone'))} commented:</b></p>{comment_html}" if comment_html
                 else f"<p><b>{html.escape(str(doer or 'someone'))} commented.</b></p>")
        with self._comment_respawn_lock:
            self._comment_respawn_queue.setdefault(key, []).append(entry)

    def _pop_comment_respawn(self, key):
        """Pop + render the queued comment thread for `key` as one HTML block (oldest first, NEWEST
        comment marked), or None if nothing is queued. This is the broker's own record of every
        comment-respawn-eligible human comment that arrived since the worker last ran; not a full
        historical Vikunja fetch."""
        with self._comment_respawn_lock:
            entries = self._comment_respawn_queue.pop(key, None)
        if not entries:
            return None
        body = "\n".join(entries[:-1] + [f'<div><b>⭐ NEWEST COMMENT:</b></div>{entries[-1]}'])
        return (f'<hr/><p><b>New comment(s) arrived after the worker closed - this run was re-spawned '
                f'because of them:</b></p>{body}')

    def _code_repo_ladder(self, key, ticket_text, run_id, comment=None):
        """Repo inference ladder for a Vikunja code ticket. Returns (local_repo_path, note_html|None) to cut from, or
        (None, None) after it has already parked the ticket (comment, waiting-on-operator label and alert DM). Steps:
        explicit repo: line, strong name match, Laya, 4B-agrees-with-Laya, then ask (top-3). A picked repo
        without a stored push credential is parked too (the broker's feature-branch push would fail).
        `comment`: human comment text from a comment rerun; its `repo:` line wins over the
        ticket's, and its text is appended to the inference text; a re-park is quiet (no second alert DM)."""
        park = self._code_repo_park
        if comment:
            park = lambda *a: self._code_repo_park(*a, quiet=True)
        ref = (routing.parse_repo_ref(comment) if comment else None) or routing.parse_repo_ref(ticket_text)
        if ref and ref.startswith("/") and ref in routing.CODE_REPO_ALLOWLIST:
            return ref, None                                  # legacy explicit path: exactly as before
        try:
            cat = repo_catalog.get_catalog()
        except Exception as e:
            log.warning("repo-infer key=%s: catalog unavailable: %s", key, e)
            return park(key, run_id, "the repo catalog is unavailable, so the target repo "
                        "couldn't be resolved", "", f"repo catalog unavailable ({e})")
        decision = None
        if ref:
            repo = cat.resolve_ref(ref)
            if repo is not None:
                decision = repo_infer.Decision("pick", repo, "token", "explicit repo: line")
            elif "/" in ref or cat.names_listed(ref):         # a path, ns/name, or a denied/archived
                return park(                                  # repo is explicit intent -> park, never infer around it
                    key, run_id, f"the declared repo <code>{html.escape(ref)}</code> isn't an allowed repo", "",
                    f"declared repo {ref!r} not in the catalog/allowlist (or denied/archived)")
            else:                                             # `repo: <prose>` is not a reference; infer instead
                log.info("repo-infer key=%s: repo: value %r matches no catalog repo -> inferring", key, ref)
        if decision is None:
            text = repo_infer.inference_text(ticket_text)
            if comment:
                text += "\n" + repo_infer.inference_text(comment)
            decision = repo_infer.infer(text, cat)
        log.info("repo-infer key=%s: %s", key, json.dumps(decision.summary()))
        if decision.action != "pick":
            cands = "".join(f"<li><code>{html.escape(c)}</code></li>" for c in decision.candidates)
            return park(
                key, run_id, "I couldn't confidently tell which repo this ticket is for",
                (f"<p>Best candidates:</p><ul>{cands}</ul>" if cands else "")
                + "<p>Why: " + re.sub(r"`([^`]+)`", r"<code>\1</code>", html.escape(decision.reason)) + "</p>",
                f"repo inference unsure ({decision.reason}); candidates {decision.candidates}")
        repo = decision.repo
        if not repo_catalog.has_push_cred(repo):
            return park(
                key, run_id, f"<code>{html.escape(repo.path)}</code> has no stored push credential for the "
                "broker", f"<p>Picked via {html.escape(decision.step)}: {html.escape(decision.reason)}. "
                "An operator needs to add a path-scoped credential for this repo (or widen the broker's "
                "token).</p>", f"no push cred for {repo.path} (picked via {decision.step})")
        try:
            path = repo_catalog.ensure_clone(cat, repo)
        except Exception as e:
            log.warning("repo-infer key=%s: clone/fetch of %s failed: %s", key, repo.path, e)
            return park(key, run_id, f"couldn't clone <code>{html.escape(repo.path)}</code>",
                        "", f"clone/fetch of {repo.path} failed: {e}")
        note = None
        if decision.step != "token":
            note = (f"<p><b>Inferred repo</b> <code>{html.escape(repo.path)}</code> "
                    f"({html.escape(decision.reason)}). Wrong one? Add a <code>repo:</code> line and "
                    "re-trigger.</p>")
        return path, note

    def spawn(self, key, brief):
        run_id = uuid.uuid4().hex
        if self.run_registry is not None:
            self.run_registry.open(run_id, key)          # stamp: worker side-effects tagged with this run_id
        t = threading.Thread(target=self._run_worker, args=(key, brief, run_id),
                             name=f"cc-worker-{key}", daemon=True)
        with self._lock:
            self._threads[key] = t
        t.start()                                        # returns immediately -> tick() is never blocked

    def _run_worker(self, key, brief, run_id):
        # A warm worker runs the seed turn, then loops serving --resume forwards / a close
        # command until torn down. The concurrency cap is acquired PER TURN (see _turn), not held across
        # the idle warm gap, so the cap bounds simultaneous active CC turns, not dormant sessions.
        worktree = None
        infer_note = None                                # "inferred repo X (reason)" note, posted at worker start
        worker = None
        route = None                                     # set below; pre-init so the flush in `finally` is safe
        repo_lock = None                                 # per-repo single-flight, if a write route
        publish = None                                   # route.publish dict for a write route, else None
        publish_ran = False                              # True once the clean-completion publish block runs
        inbox_posture = None                             # this account's runtime posture text, if an inbox route
        inbox_account = None                             # The account the inbox gate resolved (zulip: from the bot map)
        chat_zc = brief.get("zulip") if isinstance(brief, dict) else None   # Zulip reply target, if a chat
        mr_review = None                                 # MR-reviewer: the mr brief block, if an mr-review route
        review_skipped = False                           # diff-unavailable skip: settle un-stamped (re-armable)
        code_repo = code_runner = None                   # code route's resolved repo + per-repo runner key
        model_override = None                            # (backend, model) a `model:` label chose, if any
        adapter_override = None                          # "hermes" when a `hermes` label flipped the harness
        infra_grant = None                               # Deterministic live-write grant for an infra row
        attempted = False                                 # Flips True once EITHER (a) every spawn-time
                                                           # fail-closed gate (unknown account / no repo: token /
                                                           # not-allowlisted / venv missing) has passed and a worktree
                                                           # cut (or the inbox cred-free cwd) is actually about to
                                                           # happen, OR (b) the ticket-done drop fires (terminal, not
                                                           # fixable; see that return site), see the finally block
                                                           # below, which uses it to decide whether this subject may be
                                                           # marked `completed`. Stays False for a FIXABLE reject (the
                                                           # ticket can be edited and re-tried) and for the transient
                                                           # ticket-fetch-failed abort (a retry may succeed next time).
        try:
            # The CLI-version pin is REMOVED: no drift check, no alert. A routine
            # `claude` bump is a non-event; the broker no longer inspects the installed version.
            route = self.router.route(key, brief)        # model, cwd and prompt_class from routing (fleet-model.sh)
            pub = getattr(route, "publish", None) or {}
            # Fetch and inject the ticket's rendered text EARLY (before any worktree cut) so BOTH
            # the seed brief (enrichment) AND the per-ticket target-repo resolution (code/general,
            # below) can read it. The worker stays pure-compute/cred-free; only the BROKER talks to
            # Vikunja. A fetch failure/None must NEVER wedge the worker: caught + logged, and the seed turn
            # proceeds with the un-enriched brief (byte-identical to the old behaviour).
            _task_id = brief.get("task_id") if isinstance(brief, dict) else None
            _ref = tickets.vikunja_ref(_task_id)
            ticket = None                                # the fetched Ticket; its fields drive the label checks
            try:
                ticket = self._tracker_fetch(_ref)
            except Exception as e:
                log.warning("ticket_fetcher failed key=%s run_id=%s: %s", key, run_id, e)
            ticket_text = ticket.text if ticket is not None else None
            # A ticket-fetch miss gets exactly ONE retry after a short pause before we fail-close:
            # a bare None is as likely to be a transient blip (mcp.sh hiccup, brief network stall) as a real
            # problem, and the fail-close below drops the spawn entirely. HARD SCOPE: one retry, no backoff,
            # no config knob. `_ticket_fetch_sleep` is injectable so tests never actually wait.
            if _task_id is not None and ticket_text is None:
                log.warning("ticket_fetcher miss key=%s task_id=%s run_id=%s -> retrying once after %.1fs",
                            key, _task_id, run_id, self._ticket_fetch_retry_delay_s)
                self._ticket_fetch_sleep(self._ticket_fetch_retry_delay_s)
                try:
                    ticket = self._tracker_fetch(_ref)
                except Exception as e:
                    ticket = None
                    log.warning("ticket_fetcher retry failed key=%s run_id=%s: %s", key, run_id, e)
                ticket_text = ticket.text if ticket is not None else None
                if ticket_text is not None:
                    log.info("ticket_fetcher retry succeeded key=%s task_id=%s run_id=%s", key, _task_id, run_id)
                else:
                    log.warning("ticket_fetcher retry still None key=%s task_id=%s run_id=%s -> fail-closing",
                                key, _task_id, run_id)
            # FAIL-CLOSED on a genuine ticket-fetch
            # failure. If this is a Vikunja subject (task_id present) and the fetch returned None, the worker
            # would otherwise spawn with NO ticket context; that is exactly how a worker once ran contextless and
            # produced an unparseable envelope. Abort cleanly BEFORE any worktree cut / worker / MCP and emit ONE
            # alert (mirrors the inbox unknown-account fail-closed abort). SCOPED to task_id-present:
            # a non-Vikunja subject (issue-code / ansible-read: task_id None, so ticket_text is legitimately None)
            # still spawns normally, and a successfully-fetched SHORT ticket (non-None) spawns normally too;
            # only a real FETCH FAILURE (None on a ticket we were supposed to fetch) aborts. This runs BEFORE
            # the done-check below, so a None fetch aborts rather than being read as "not done".
            if _task_id is not None and ticket_text is None:
                log.warning("spawn ABORT (ticket fetch failed): key=%s task_id=%s run_id=%s -> not spawned",
                            key, _task_id, run_id)
                # Pass a per-KEY subject so each ticket's fail-closed abort gets its OWN
                # rate-limit bucket. Without it, wake_ops falls back to notify._subject_key(text), which
                # DIGIT-COLLAPSES the ticket id, so every ticket-abort alert shares ONE bucket and goes silent
                # after _PER_SUBJECT_CAP, which silently blackholed every labelled ticket.
                self._alert_ops(f"eph worker {key} aborted: ticket #{_task_id} fetch returned no body "
                                f"(fail-closed, no contextless spawn){self._dash_url(_ref)}", subject=f"abort:{key}")
                return
            # A comment-respawn (app._comment_respawn) queues the human comment(s) that woke
            # this spawn; merge the whole queued thread onto the fetched ticket text so the FIRST turn sees
            # it (same injection point as every other ticket enrichment here). Pop unconditionally; a
            # fail-safe no-op when nothing is queued (COMMENT_RESPAWN_ENABLED off, or an ordinary spawn).
            comment_thread = self._pop_comment_respawn(key)
            if comment_thread:
                ticket_text = (ticket_text or "") + comment_thread
            if ticket_text is not None:
                brief = {**brief, "ticket": ticket_text}
            if ticket_text is not None and _task_id is not None:
                # The rendered ticket carries the PROJECT-relative `identifier: #N`, which a worker
                # cited instead of the global id. Lead the brief with the GLOBAL id and link.
                brief = {"ticket_ref": (f"GLOBAL ticket id {_task_id} - {self._ticket_url(_ref)} "
                                        f"(cite THIS id/link; the ticket's `identifier: #N` is project-relative)"),
                         **brief}
            # DROP a spawn whose ticket is ALREADY DONE. A `task.updated` webhook fires
            # when an agent CLOSES/labels a ticket; the router still admits+routes it, so the broker spawns a
            # wasted worker (and often a spurious escalation) on an already-done ticket (live: closing
            # eph-assigned tickets each SPAWNed a worker, and since agents close tickets too, EVERY close does
            # this). The inbound event does NOT carry `done` (events.jsonl is flat: source/event_name/task_id/
            # doer/assignees/labels), so the EARLIEST the broker can KNOW done is the ticket fetch just above.
            # Abort here, cleanly, before any worktree cut / worker spawn / MCP, mirroring the inbox
            # unknown-account fail-closed abort, but WITHOUT an alert: a done-ticket drop is the expected,
            # benign case, and alerting would re-introduce exactly the spurious escalation this fix removes.
            # FAIL-SAFE: only a positively-observed `**done:** true` drops; a None/unfetched/false ticket
            # proceeds byte-identically to before, so a fetch miss never blackholes real work. Read off the
            # MERGED text (not Ticket.done) so a queued comment thread counts exactly as it did before refs.
            if _ticket_text_is_done(ticket_text):
                log.info("spawn DROP (ticket done): key=%s run_id=%s -> not spawned", key, run_id)
                # Unlike a fixable fail-closed REJECT (no repo: token, unknown
                # account, ...), an already-done ticket is a TERMINAL outcome, not a transient/fixable one;
                # the ticket won't become "not done" again on its own. Leaving this unstamped would let
                # every later webhook on the same closed ticket re-spawn -> re-fetch -> drop here, forever
                # (the exact re-fetch storm the done-check was added to kill). So this DOES
                # mark completed, same as a real run; it just never needed the worktree cut to know that.
                attempted = True
                return
            # A `model:<name>` ticket label picks this worker's model/backend (routing.apply_model_label:
            # pin wins, unknown name / untrusted creator picking a non-Anthropic backend: fail-closed). Read
            # off the FETCHED ticket (labels/created_by), so it holds even when the coalesced brief lost them.
            # Via the Ticket's creator/labels (ticket_meta underneath), which fail closed (creator None) on a
            # truncated / list-fallback / lookalike blob: then NO label is honored (default model), never a
            # description-spoofed label/creator.
            trusted = ticket is not None and ticket.creator is not None
            if _task_id is not None and ticket is not None and not trusted:
                log.info("model label: key=%s run_id=%s -> ticket labels/creator untrusted (truncated/fallback/"
                         "duplicate fields) -> no model label applied", key, run_id)
            if _task_id is not None and trusted:
                try:
                    labelled = routing.apply_model_label(
                        route, ticket.labels, ticket.creator,
                        lambda name: load_backend(name)[0] is not None)
                    if labelled is not route:
                        route, model_override = labelled, (labelled.backend, labelled.model)
                        log.info("model label: key=%s run_id=%s -> backend=%r model=%r", key, run_id,
                                 labelled.backend, labelled.model)
                    # A `hermes` label flips the harness (after the model label, so its backend rides through)
                    flipped = routing.apply_adapter_label(route, ticket.labels, ticket.creator)
                    if flipped is not route:
                        route, model_override = flipped, (flipped.backend, flipped.model)
                        adapter_override = flipped.adapter
                        log.info("hermes label: key=%s run_id=%s -> adapter=hermes backend=%r", key, run_id,
                                 flipped.backend)
                except ValueError as e:
                    log.warning("spawn REJECT (%s): key=%s run_id=%s: %s",
                                "hermes label" if str(e).startswith("hermes label:") else "model label",
                                key, run_id, e)
                    self._alert_ops(f"eph worker {key} not spawned: {e}{self._dash_url(_ref)}", subject=f"abort:{key}")
                    return
            # dev-spindown GH/GL issue-code: an external issue arrives WITH its title+body in the event (no
            # Vikunja fetch: task_id is None, so the fetcher above no-ops), and collapse() carried them into
            # brief["issue"]. Inject that text into the brief's "ticket" slot (the SAME slot the ticket
            # enrichment uses) so the SOURCE-AGNOSTIC signed change-proposer prompt reads the change request
            # the same way it reads a Vikunja ticket. Guarded on an absent "ticket" so it never clobbers a
            # real fetched ticket; only issue events (github/gitlab) ever carry brief["issue"].
            issue_blk = brief.get("issue") if isinstance(brief, dict) else None
            if issue_blk and brief.get("ticket") is None:
                ident = f"{issue_blk.get('source')}:{issue_blk.get('repo_ident')}#{issue_blk.get('issue_n')}"
                title = issue_blk.get("title") or ""
                body = issue_blk.get("body") or ""
                brief = {**brief, "ticket": f"[{ident}] {title}\n\n{body}".strip()}
            if getattr(route, "domain", "") == "inbox" or getattr(route, "capability", "read") == "inbox":
                # SPAWN-TIME fail-closed on an unknown account. Derive the account from
                # the per-account subject key (coalescer.normalize: "gmail:<account>"); inbox_fact_block
                # RAISES UnknownInboxAccount for anything not in the registry (incl. "gmail:unknown"). On an
                # unknown account we ABORT cleanly: NO worktree, NO worker, NO MCP spawn, rather than run a
                # Gmail-mutation-capable worker with no posture / no per-account credential isolation.
                account = key.split(":")[1] if ":" in key else key
                if key.startswith("zulip:"):
                    # The zulip key names the DMed BOT (zulip:<bot>:<sender_id>), not an account. Take the
                    # account from the live bot map (config/zulip-wake.json via the injected zulip_account); an
                    # unmapped bot gives None and fails closed below exactly like an unknown gmail account.
                    try:   # (bot, sender): the sink re-checks flag + map + retired + sender allowlist
                        _zk = key.split(":")
                        account = self._zulip_account(_zk[1], _zk[2] if len(_zk) > 2 else None) or ""
                    except Exception as e:
                        log.warning("zulip_account lookup failed key=%s: %s", key, e)
                        account = ""
                try:
                    inbox_posture = inbox_accounts.inbox_fact_block(account)
                except inbox_accounts.UnknownInboxAccount:
                    log.warning("inbox spawn REJECTED key=%s: unknown account %r (fail-closed, no worker cut)",
                                key, account)
                    self._alert_ops(f"inbox worker {key} rejected: unknown account {account!r} (fail-closed)",
                                    subject=f"reject:{key}")   # per-key bucket, not digit-collapsed
                    return
                # The inbox worker grooms its seen-ledger + rules.md in the LIVE inbox repo (they MUST persist
                # across spawns; the carryover-kill mechanism), so its cwd is route.repo, NOT a throwaway
                # worktree. acceptEdits confines edits to that cwd. worktree stays None (nothing to remove).
                attempted = True                          # Past the unknown-account gate -> really dispatching
                inbox_account = account                   # Carried onto the Worker for the MCP config path
                worker_cwd = route.repo
            elif getattr(route, "capability", "read") in ("code", "general"):
                # Capability alone picks this lane. An unset row repo (site.env) used to skip it
                # and fall into the ansible-default cut below with NO allowlist check; now every sub-path still
                # allowlist-gates its resolved repo, and an empty one is refused just before the cut.
                # Route-aware worktree for a code change-proposer OR a general worker.
                # Serialise the whole cut/edit/(test)/publish/teardown cycle per target repo (coarse per-repo
                # lock, like wiki). cwd = the worktree ROOT (a change may span the whole repo). The deny-floor
                # + broker-only publish keep it cred-free. publish is an MR on a FEATURE BRANCH, NEVER main
                # (the code publish path pushes a fresh eph branch + hands the MR open to the publisher; see the
                # publish block below + worktree.publish_mr).
                if getattr(route, "from_config", False):
                    # routes.json row: the worktree target IS the row's repo. No repo-infer ladder, no row
                    # default fallback; a ticket `repo:` line naming any other repo is refused (fail-closed), and
                    # the row repo is re-checked against its capability's allowlist at spawn.
                    declared = routing.parse_repo_token(ticket_text)
                    allowed = (routing.code_repo_allowed if route.capability == "code"
                               else routing.general_repo_allowed)
                    reason = (f"ticket repo: {declared!r} differs from the row repo {route.repo!r}"
                              if declared and declared != route.repo else
                              f"row repo {route.repo!r} not in the allowlist" if not allowed(route.repo) else None)
                    if reason:
                        log.warning("config row %s spawn REJECTED key=%s: %s (fail-closed, no worker cut)",
                                    route.row_key, key, reason)
                        self._alert_ops(f"{route.row_key} worker {key} rejected: {reason} (fail-closed)",
                                        subject=f"reject:{key}")
                        return
                    target_repo = route.repo
                elif getattr(route, "capability", "") == "general":
                    # General worker: the worktree TARGET is bounded by an allowlist even though the toolset
                    # isn't. A ticket may DECLARE its target via a `repo:<abs-path>`
                    # token in the ticket text (parsed above into ticket_text); resolve_target_repo returns
                    # the declared repo IFF allowlisted (else ValueError -> reject), or the row default when
                    # nothing is declared. We then allowlist-check the RESOLVED repo (catches a non-allowlisted
                    # DEFAULT too, preserving the fail-closed on the row default). FAIL-CLOSED both ways:
                    # non-allowlisted declared OR default means no worktree and no worker (mirrors inbox unknown-acct).
                    declared = routing.parse_repo_token(ticket_text)
                    repo_allowed = routing.general_repo_allowed
                    try:
                        target_repo = routing.resolve_target_repo(
                            declared, pub["code_repo_path"], repo_allowed)
                    except ValueError as e:
                        log.warning("general spawn REJECTED key=%s: %s (fail-closed, no worker cut)", key, e)
                        self._alert_ops(f"general worker {key} rejected: {e}", subject=f"reject:{key}")
                        return
                    if not repo_allowed(target_repo):
                        log.warning("general spawn REJECTED key=%s: repo %r not in GENERAL_REPO_ALLOWLIST "
                                    "(fail-closed, no worker cut)", key, target_repo)
                        self._alert_ops(f"general worker {key} rejected: repo {target_repo!r} "
                                        f"not in the allowlist (fail-closed)", subject=f"reject:{key}")
                        return
                else:
                    # code worker. dev-spindown GH/GL issue-code: an external ISSUE resolves its LOCAL repo
                    # per-issue from ISSUE_REPO_MAP, FAIL-CLOSED: an unmapped repo raises ValueError, so NO
                    # worker is cut and an alert is sent (mirrors the general declared-repo gate + inbox unknown-account
                    # reject). A vikunja-labelled/assigned code worker (no brief["issue"]) is UNCHANGED: it
                    # cuts from the row default.
                    issue = brief.get("issue") if isinstance(brief, dict) else None
                    if getattr(route, "domain", "") == "infra":
                        # Infra row: defaults to its own repo (/srv/infra) with no `repo:` token, still
                        # CODE_REPO_ALLOWLIST-gated (fail-closed); no inference ladder, no no-token reject.
                        try:
                            target_repo = routing.resolve_infra_repo(
                                ticket_text, pub["code_repo_path"], routing.code_repo_allowed)
                        except ValueError as e:
                            log.warning("infra spawn REJECTED key=%s: %s (fail-closed, no worker cut)", key, e)
                            self._alert_ops(f"infra worker {key} rejected: {e}", subject=f"reject:{key}")
                            return
                        # ZERO-LLM grant decision off broker-side facts only: the FETCHED ticket's labels +
                        # created_by and the event doer. Untrusted / opted-out / flag off: None (no mcp-rw.sh).
                        if _task_id is not None and ticket is not None:
                            infra_grant = write_grant.make_grant(
                                _task_id, ticket.labels, ticket.creator,
                                brief.get("last_doer") if isinstance(brief, dict) else None)
                    elif issue and issue.get("source") in ("github", "gitlab"):
                        try:
                            target_repo = routing.resolve_issue_repo(
                                issue.get("source"), issue.get("repo_ident"))
                        except ValueError as e:
                            log.warning("issue-code spawn REJECTED key=%s: %s (fail-closed, no worker cut)",
                                        key, e)
                            self._alert_ops(f"issue-code worker {key} rejected: {e}", subject=f"reject:{key}")
                            try:                         # say so on the issue (GitLab only; no-op elsewhere)
                                self._tracker_comment(tickets.ref_from_key(key), (
                                    "<p><b>Not started</b>: this project has no local repo in the broker's "
                                    "<code>ISSUE_REPO_MAP</code>, so no worker ran. The operator has been "
                                    "alerted.</p>"), run_id)
                            except Exception as ce:
                                log.warning("issue-code reject comment failed key=%s: %s", key, ce)
                            return
                    else:
                        # Vikunja-labelled/assigned code worker. Until 2026-09-17 this was a bare
                        # `target_repo = pub["code_repo_path"]`, the hardcoded default, so a ticket about
                        # any other repo cut the wrong worktree and the worker refused it.
                        # Now: honour a `repo:<abs-path>` token in the ticket text, gated by the code
                        # allowlist, exactly like the general row above. Nothing declared: the row default
                        # (still allowlist-checked below, preserving the pre-existing fail-closed behaviour).
                        declared = routing.parse_repo_token(ticket_text)
                        if routing.repo_infer_enabled():     # The inference ladder replaces the reject
                            with self._parked_lock:          # a queued human comment (rerun)
                                rerun_comment = self._repo_rerun.pop(key, None)
                            try:
                                declared, infer_note = self._code_repo_ladder(key, ticket_text, run_id,
                                                                              comment=rerun_comment)
                            except Exception as e:   # an unexpected error still parks visibly
                                log.warning("repo-infer key=%s: ladder raised: %s", key, e)
                                declared, infer_note = self._code_repo_park(
                                    key, run_id, "repo inference hit an internal error", "",
                                    f"repo inference raised ({type(e).__name__}: {str(e)[:160]})",
                                    quiet=bool(rerun_comment))   # a rerun re-park: no 2nd alert DM
                            # Remember the park so a human comment can re-run the ladder; a
                            # resolved ladder clears it.
                            if not declared:
                                if routing.repo_infer_comment_rerun_enabled():
                                    self._set_repo_parked(key, getattr(route, "row_key", None)
                                                           or getattr(route, "domain", None) or "code")
                                return
                            self._set_repo_parked(key, None)
                        elif routing.repo_infer_shadow():    # shadow mode: log the would-be pick, change nothing
                            repo_infer.shadow_async(key, ticket_text, declared, repo_catalog.get_catalog)
                        # A CODE ticket with NO `repo:` token
                        # must FAIL CLOSED, never silently default to the row repo (a ticket with no
                        # repo: line once ran in the wrong repo).
                        # Abort (no worktree, no worker), tell the author how to fix it on the ticket, send an alert.
                        # Same fail-closed shape as the inbox unknown-account abort. (CODE ROW ONLY; the general
                        # row keeps its default-repo behaviour; a declared-but-not-allowlisted repo still rejects
                        # below. This adds the ABSENT case.)
                        if not declared:
                            allow = ", ".join(sorted(routing.CODE_REPO_ALLOWLIST)) or "(none configured)"
                            log.warning("code spawn REJECTED key=%s: no repo: token in ticket "
                                        "(fail-closed, no default)", key)
                            _tref = _ops_ref(key)
                            if _tref is not None:
                                try:
                                    self._tracker_comment(_tref, (
                                        "<p><b>⏳ WAITING ON YOU:</b> this code ticket has no target repo, so "
                                        "no worker was spawned (fail-closed).</p><p>Add a bare line "
                                        "<code>repo:&lt;absolute-path&gt;</code> to the ticket description, then "
                                        "re-trigger.</p><p>Allowlisted repos: "
                                        f"<code>{html.escape(allow)}</code></p>"), run_id)
                                except Exception as e:                 # a lost comment must never wedge the abort
                                    log.warning("code no-repo comment post failed key=%s: %s", key, e)
                            self._alert_ops(f"code worker {key} rejected: ticket has no repo: token "
                                            f"(fail-closed, no default; allowlist: {allow})",
                                            subject=f"reject:{key}")
                            return
                        try:
                            target_repo = routing.resolve_target_repo(
                                declared, pub["code_repo_path"], routing.code_repo_allowed)
                        except ValueError as e:
                            log.warning("code spawn REJECTED key=%s: %s (fail-closed, no worker cut)", key, e)
                            self._alert_ops(f"code worker {key} rejected: {e}", subject=f"reject:{key}")
                            return
                        if not routing.code_repo_allowed(target_repo):
                            log.warning("code spawn REJECTED key=%s: repo %r not in CODE_REPO_ALLOWLIST "
                                        "(fail-closed, no worker cut)", key, target_repo)
                            self._alert_ops(f"code worker {key} rejected: repo {target_repo!r} "
                                            f"not in the allowlist (fail-closed)", subject=f"reject:{key}")
                            return
                # Thread the RESOLVED repo through the whole cut/push/MR chain: worktree.make/publish_mr/
                # mr_diff read the repo from _made[key] (so they follow it automatically), but open_mr derives
                # the GitLab project from publish["code_repo_path"], so override that key to the resolved repo.
                # ALSO resolve the MR target ref from the repo's OWN default branch: the config default is the
                # literal "main", which is wrong for a `master` repo (/srv/infra) and kills the worker at the
                # worktree cut (2026-09-17). Per-repo, so it can't be a single global env var.
                # Pick the test runner PER REPO (map, then route default). A `venv` repo whose venv infra hasn't
                # provisioned FAILS CLOSED here (no worktree, no worker) with an explicit reason, never a
                # silent fallback to system python (that is the ModuleNotFoundError this check exists to kill).
                code_repo = target_repo
                code_runner = routing.resolve_test_runner(target_repo, getattr(route, "test_runner", "") or None)
                if code_runner == "venv" and not os.access(routing.code_venv_python(target_repo), os.X_OK):
                    reason = (f"venv missing at {routing.code_venv_python(target_repo)} (repo {target_repo} is "
                              f"mapped to the venv test runner; infra must provision <repo>/.venv)")
                    log.warning("code spawn BLOCKED key=%s: %s (fail-closed, no worker cut)", key, reason)
                    _tref = _ops_ref(key)
                    if _tref is not None:
                        try:
                            self._tracker_comment(_tref, (
                                "<p><b>Blocked:</b> no code worker was spawned (fail-closed).</p>"
                                f"<p>{html.escape(reason)}.</p>"), run_id)
                        except Exception as e:                 # a lost comment must never wedge the abort
                            log.warning("code venv-missing comment post failed key=%s: %s", key, e)
                    self._alert_ops(f"code worker {key} blocked: {reason}", subject=f"reject:{key}")
                    return
                if not target_repo:                       # belt-and-braces, never cut from ""
                    log.warning("%s spawn REJECTED key=%s: row repo unset (site.env; fail-closed, no worker cut)",
                                getattr(route, "capability", ""), key)
                    self._alert_ops(f"{getattr(route, 'capability', '')} worker {key} rejected: row repo unset "
                                    "(site.env)", subject=f"reject:{key}")
                    return
                attempted = True                          # Past every code/general fail-closed gate (incl. venv)
                publish = {**pub, "code_repo_path": target_repo,
                           "mr_target_ref": resolve_default_branch(target_repo, pub["mr_target_ref"])}
                repo_lock = self._repo_lock(target_repo)
                repo_lock.acquire()
                worktree = self.worktree_mgr.make(
                    key, base_repo=target_repo, worktrees_root=pub["worktree_base"],
                    publish_ref=publish["mr_target_ref"], suppress_hooks=True)   # cut FROM the resolved MR target
                worker_cwd = worktree                    # whole-repo cwd (code edits span files); acceptEdits confines to it
                if infer_note:                           # Say which repo was inferred, and why
                    self._post_ticket_note(key, infer_note, run_id)
            elif getattr(route, "capability", "read") == "write":
                # Route-aware worktree for the wiki write worker. Serialise the whole
                # cut/edit/publish/teardown cycle per target repo (rebase-retry backstops the un-locked
                # standing bridges). Limitation: coarse per-repo lock; relax to publish-only if wiki write
                # volume ever makes the serialised edit phase a bottleneck.
                if not pub.get("wiki_repo_path"):         # unset WIKI_REPO_PATH: refuse, never cut from ""
                    log.warning("write spawn REJECTED key=%s: row repo unset (site.env; fail-closed, no worker cut)",
                                key)
                    self._alert_ops(f"write worker {key} rejected: row repo unset (site.env)", subject=f"reject:{key}")
                    return
                attempted = True                          # Wiki row has no spawn-time reject gate of its own
                publish = pub
                repo_lock = self._repo_lock(pub["wiki_repo_path"])
                repo_lock.acquire()
                worktree = self.worktree_mgr.make(
                    key, base_repo=pub["wiki_repo_path"], worktrees_root=pub["worktree_base"],
                    publish_ref=pub["publish_ref"], suppress_hooks=True)
                # SECURITY REQUIREMENT (see the routing._WIKI_DOCS_ROW note): acceptEdits confines
                # edits to the process CWD but IGNORES allow-list path patterns, so content-only scoping is
                # a CWD choice made here. cwd = <worktree>/<content_subdir> keeps the worker off
                # quartz.config.ts / .gitlab-ci.yml (git add/commit still run repo-wide from the subdir, so
                # publish is unaffected). NEVER the worktree root.
                worker_cwd = os.path.join(worktree, pub.get("content_subdir", "content"))
            elif getattr(route, "capability", "read") == "mr-review":
                # MR-reviewer: PURE-READ adversarial review of an MR the broker opened. Cut a
                # THROWAWAY worktree checked out at the MR SOURCE branch (read-only cwd); the diff is
                # computed BROKER-SIDE and injected into the brief (the READ grant forbids `git diff`). builder
                # is never the acceptor, and no-merge-power holds BY CONSTRUCTION: a fresh claude -p spawn with a READ-only
                # grant (no Edit/Write/Bash/git/push/merge) literally cannot write or accept. publish stays
                # None (a review publishes nothing; the broker posts the verdict note after the seed turn).
                assert route.capability == "mr-review" and route.read_only, \
                    "mr-review route must be read_only + capability=mr-review (no-merge / builder!=acceptor)"
                mr_review = brief.get("mr") or {}
                repo_path = mr_review.get("repo_path") or route.repo
                source_branch = mr_review.get("source_branch")
                target_ref = mr_review.get("target_ref") or "main"
                attempted = True                          # mr-review row has no spawn-time reject gate of its own
                _harness = getattr(route, "adapter", "cc")
                # An earlier review worktree was cut at main's sha (merge-base), not the MR head, so the reviewer read
                # the BASE tree. Pin the cut to the MR head: the event's head_sha, else the 8-hex sha suffix
                # every broker-pushed eph/ branch carries (publish_mr names it <key>-<head[:8]>).
                expect_sha = mr_review.get("head_sha")
                if not expect_sha:
                    _m = re.match(r"^eph/.*-([0-9a-f]{8})$", source_branch or "")
                    expect_sha = _m.group(1) if _m else None
                try:
                    worktree = self.worktree_mgr.make(
                        key, base_repo=repo_path, worktrees_root=MR_REVIEW_WORKTREE_BASE,
                        publish_ref=source_branch, suppress_hooks=True,   # checkout the MR SOURCE branch (read-only)
                        expect_sha=expect_sha)
                except CutShaMismatch as e:              # make() already removed the wrong-tree worktree
                    review_skipped = True                # No review ran -> re-armable
                    self._skip_mr_review_no_diff(mr_review, key, run_id, harness=_harness,
                                                 reason=f"could not cut the MR head: {e}")
                    return
                worker_cwd = worktree
                # enrich the brief with the broker-side three-dot MR diff (mirror the ticket_text
                # injection): the reviewer reads it from its brief since the READ grant has no `git diff`.
                diff = self.worktree_mgr.mr_diff(key, target_ref)   # Retries + local fallback inside
                if not (diff or "").strip():
                    # Still empty after every retry (or the cut is the merge-base): do NOT run the
                    # reviewer (it would review the BASE tree and post a wrong-tree BLOCK/PASS). Neutral note,
                    # alert escalation, no verdict, no CD hook. The finally below tears the worktree down.
                    review_skipped = True                # No review ran -> re-armable
                    self._skip_mr_review_no_diff(mr_review, key, run_id, harness=_harness)
                    return
                brief = {**brief, "diff": diff}
                # Pin CD to exactly the commit reviewed (this worktree's cut SHA). (An empty diff
                # never reaches here; the review is skipped above.)
                mr_review = {**mr_review, "reviewed_sha": self.worktree_mgr.cut_sha(key)}
            else:
                attempted = True                          # ansible-default row has no spawn-time reject gate
                if getattr(route, "from_config", False):   # routes.json "read" row: its own repo
                    worktree = self.worktree_mgr.make(key, base_repo=route.repo)
                else:
                    worktree = self.worktree_mgr.make(key)   # charter-free worktree cwd (ansible default)
                worker_cwd = worktree
            worker = Worker(key, run_id, worker_cwd)
            worker.repo, worker.test_runner = code_repo, code_runner   # None on every non-code route
            worker.inbox_account = inbox_account          # None on every non-inbox route
            if infra_grant is not None:                   # Write the grant file the mcp-rw.sh wrapper checks
                try:
                    worker.write_grant_path = write_grant.write_grant_file(infra_grant)
                    worker.write_grant, worker.audit_since = infra_grant, time.time()
                    log.info("write grant key=%s run_id=%s bundles=%s expires=%s", key, run_id,
                             ",".join(infra_grant["bundles"]), infra_grant["expires"])
                except Exception as e:                    # fail-closed: no file -> no mcp-rw.sh grant
                    log.warning("write grant file failed key=%s: %s (worker runs without mcp-rw.sh)", key, e)
            worker.thread = threading.current_thread()
            with self._lock:
                self._workers[key] = worker
            # The prompt is loaded and sha256-gated (value-free; content is owner-approved).
            system_prompt = self.prompt_loader.load(route.prompt_class, route.domain)
            # (The ticket-text fetch and inject happens EARLIER, just after route resolution above, so the
            # per-ticket repo resolution can read it before the worktree cut. brief is already enriched.)
            # Append THIS account's runtime posture to the brief tail (mirror the ticket enrichment
            # pattern), so the account-agnostic signed prompt gets its per-account facts (email/ledger/
            # posture from inbox_accounts.py) at RUNTIME without a re-sign. Appended at the SEED turn here
            # AND at every FORWARD turn below (both build their brief independently).
            if inbox_posture is not None:
                brief = {**brief, "posture": inbox_posture}
            if worker.write_grant_path:                   # The signed infra prompt reads this key
                brief = {**brief, "write_grant": {"bundles": list(worker.write_grant["bundles"]),
                                                  "expires": worker.write_grant["expires"]}}
            if code_repo is not None:                    # name the granted runner (the prompt promises it)
                brief = {**brief, "test_runner": {"key": code_runner or "none",
                                                  "command": code_test_hint(code_runner, code_repo)}}
            if chat_zc:                                  # COLD chat start only (forwards never get here)
                brief = self._with_dm_history(key, brief, chat_zc, run_id)
            first_prompt = json.dumps(brief)             # value-free: the collapsed (+ enriched) brief seeds turn 1
            # coalescer already advertised state=open_running for the seed turn at _would_spawn().
            env = self._turn(worker, route, system_prompt, first_prompt, resume=False, run_id=run_id)
            self._post_envelope(brief, run_id, env, worker)   # post the seed turn's summary
            if mr_review is not None:                     # MR-reviewer: broker posts the verdict as an
                self._post_mr_verdict(mr_review, run_id, env,   # MR note (reviewer is cred-free; seed only)
                                      harness=getattr(route, "adapter", "cc"))   # From the route
            if not worker.api_overloaded:                # Never idle (it would force a stamping close) on give-up
                self._advertise_idle(key)                # seed turn-boundary: mark open_idle
            # Warm-worker command loop (one turn at a time; a single CC session can't run two turns).
            # An API-overload give-up exits straight away (un-stamped) instead of idling to a stamping close.
            while not worker.done and not worker.api_overloaded:
                try:
                    cmd = worker.cmdq.get(timeout=self.cmd_poll_s)
                except queue.Empty:
                    continue                             # lets the LivenessDriver drive close on window-quiet
                if cmd[0] == "close":                    # teardown() signalled a hard-stop
                    break
                if cmd[0] == "forward":                  # deliver ONE collapsed --resume turn
                    brief2 = cmd[1]
                    if inbox_posture is not None:         # same per-account posture on the forward brief
                        brief2 = {**brief2, "posture": inbox_posture}
                    if worker.forwards >= self.max_forwards:   # max-forwards/resumes
                        log.warning("forward key=%s: max-forwards %d hit -> closing", key, self.max_forwards)
                        self._alert_ops(f"worker {key} hit max-forwards {self.max_forwards}",
                                        subject=f"worker:{key}")   # per-key bucket
                        self._chat_notice(chat_zc, CHAT_LIMIT_REPLY, run_id)   # Tell the Zulip sender
                        break
                    worker.forwards += 1
                    self._advertise_running(key)         # resume turn begins (idempotent w/ take_forwards)
                    route2 = self.router.route(key, brief2)
                    if model_override is not None:       # A resumed turn stays on the label's backend
                        route2.backend, route2.model = model_override
                    if adapter_override is not None:     # ...and on the label's harness
                        route2.adapter = adapter_override
                    sp2 = self.prompt_loader.load(route2.prompt_class, route2.domain)
                    env2 = self._turn(worker, route2, sp2, json.dumps(brief2), resume=True, run_id=run_id)
                    self._post_envelope(brief2, run_id, env2, worker)   # post this forward's summary
                    if worker.api_overloaded:            # Give up on this subject, un-stamped
                        break
                    self._advertise_idle(key)            # turn-boundary -> open_idle
            # The worker has committed its content edits into the (hook-suppressed,
            # cred-free) worktree; the BROKER does the ONLY push. Runs on clean completion inside the try
            # (NOT in finally) so a crashed/hard-killed turn never publishes a partial edit. A no-op
            # (worker made no commit) returns cleanly. A rebase conflict/exhausted-retry raises, and is logged and
            # alerted below, worktree still torn down in finally.
            if publish is not None:
                publish_ran = True                       # The finally's crash-path patch save keys off this
                try:
                    if publish.get("code_repo_path") and worker.turn_capped:
                        # A turn-capped worker's work is INCOMPLETE, so no MR and no reviewer; the
                        # finally below pushes it to an eph/*-wip branch (branch only) before teardown instead.
                        log.info("publish key=%s: turn-capped -> WIP branch at teardown, no MR", key)
                    elif publish.get("code_repo_path"):
                        # A code change-proposer NEVER auto-publishes to main. The broker pushes
                        # the worker's committed HEAD to a FRESH eph FEATURE BRANCH (never mr_target_ref) and
                        # hands MR creation to the publisher. This is the human-gated contract: nothing merges/deploys
                        # without a human MR review. (The branch is pushed before teardown so it survives on
                        # origin as the deliverable; the MR-open API call is made by the publisher, see worktree.publish_mr.)
                        branch = self.worktree_mgr.publish_mr(
                            key, branch_prefix=publish.get("branch_prefix", "eph/code-change"),
                            mr_target_ref=publish["mr_target_ref"])
                        if not branch:   # No commit -> keep any uncommitted edits as a patch file
                            self._save_patch(key, run_id)
                        if branch:  # None = worker made no commit; nothing to open
                            # Open the branch-to-target MR for human review. open_mr NEVER raises into
                            # the turn (branch is the durable deliverable) and skips gracefully if the token env
                            # is unset, so a first ticket is never blocked.
                            opened = open_mr(publish["code_repo_path"], branch,
                                             publish["mr_target_ref"], key=key,
                                             description=_mr_description(key, branch, publish["mr_target_ref"],
                                                                         worker.last_summary))
                            if opened:  # dict {web_url, iid, project}; None when token unset / no MR
                                # The SOURCE ticket's numeric id, when this
                                # code worker was spawned off a Vikunja ticket (None for an issue-code
                                # GitHub/GitLab MR). Notify the ticket and send the alert DM now, and stamp it onto the
                                # synthetic mr.opened event below so the reviewer's own brief can post its
                                # verdict back onto the SAME ticket (collapse() threads "mr".task_id through).
                                _mr_task_id = tickets.vikunja_id(key)
                                events.emit("mr.opened", key, run_id=run_id, web_url=opened.get("web_url"),
                                            iid=opened.get("iid"), project=opened.get("project"), branch=branch,
                                            target=publish["mr_target_ref"])
                                self._notify_mr_opened(_mr_task_id, opened, branch, run_id,
                                                       notify_handle=publish.get("mr_notify_handle"))
                                if self._on_mr_opened is not None:   # AUTO_MERGE_REPOS (never wedges)
                                    try:
                                        self._on_mr_opened(opened, branch, _mr_task_id, run_id)
                                    except Exception as e:
                                        log.warning("on_mr_opened failed key=%s run_id=%s: %s", key, run_id, e)
                                # MR-reviewer: re-ingest a synthetic mr.opened event in-process so the
                                # broker spawns a PURE-READ adversarial reviewer of the MR we just opened. NO
                                # doer and NO run_id, so it passes echo_guard (not an agent-bot echo, no active run_id);
                                # the reviewer opens no MR and posts nothing itself, so there is no side-effect loop.
                                # Stamping task_id here (already done above)
                                # ALSO fixes the reviewer's own brief being task_id=null/issue=null (seen end to end:
                                # the reviewer could only infer scope from the branch name):
                                # collapse() (coalescer.py) promotes a top-level "task_id" on ANY event into
                                # the worker brief, and the existing generic pre-route ticket-fetch (above,
                                # keyed on brief["task_id"]) fetches it into brief["ticket"] BEFORE the
                                # mr-review capability branch runs; no further plumbing needed.
                                self._emit_event({
                                    "source": "mr", "event_name": "mr.opened",
                                    "project": opened["project"], "mr_iid": opened["iid"],
                                    "source_branch": branch, "target_ref": publish["mr_target_ref"],
                                    "web_url": opened["web_url"], "repo_path": publish["code_repo_path"],
                                    "task_id": _mr_task_id,
                                    # The pushed head, so the reviewer's cut is asserted against it
                                    "head_sha": getattr(self.worktree_mgr, "head_sha", lambda k: None)(key),
                                })
                    else:
                        self.worktree_mgr.publish(key, publish_ref=publish["publish_ref"])
                except Exception as e:
                    log.exception("publish key=%s run_id=%s failed: %s", key, run_id, e)
                    self._alert_ops(f"publish {key} run_id={run_id} failed: {e}", subject=f"worker:{key}")
                    if isinstance(e, PushTargetRefused):
                        self._nag_push_refused(key, run_id, e)   # fail-closed push -> say so on the ticket
        except Exception as e:                           # a worker failure must never wedge the broker
            log.exception("worker key=%s run_id=%s failed: %s", key, run_id, e)
            self._alert_ops(f"worker {key} run_id={run_id} failed: {e}", subject=f"worker:{key}")
            self._chat_notice(chat_zc, CHAT_LIMIT_REPLY, run_id)   # A dead chat worker tells its sender
        finally:
            # A turn-capped code/general worker's edits would die with the worktree below; save
            # them to an eph/*-wip branch FIRST (inside the run, before run_registry.close). Never raises.
            if worktree and worker is not None and worker.turn_capped and (publish or {}).get("code_repo_path"):
                self._save_wip(key, run_id)
            # A code worker whose run RAISED before the publish block (e.g. _turn blew up) would lose its
            # uncommitted edits with the worktree below; save them as a LOCAL patch (no push, so this never
            # publishes a partial edit). Clean completion already handled this in the publish block. Never raises.
            elif (worktree and worker is not None and not publish_ran
                  and (publish or {}).get("code_repo_path")):
                self._save_patch(key, run_id)
            if worker is not None and worker.write_grant_path:   # Revoke the live-write grant with the worker
                write_grant.remove_grant_file(worker.write_grant_path)
            if self.run_registry is not None:
                self.run_registry.close(run_id)
            # Flush THIS worker's suppressed-escalation tail at exit. If its escalation
            # subject flapped over-cap then went quiet, the 'N suppressed' summary would otherwise only ride
            # the NEXT admitted DM for that subject, which never comes once the worker is gone. Subject mirrors
            # envelope.escalate's key (task_id, else the route domain). Never raises (a lost flush is a non-event).
            try:
                _flush_tid = tickets.vikunja_id(key)
                _flush_subj = (f"escalate:{_flush_tid}" if _flush_tid is not None
                               else f"escalate:{getattr(route, 'domain', '?')}")
                self._flush_ops(_flush_subj)
            except Exception as e:
                log.warning("flush_ops key=%s failed: %s", key, e)
            # The abort:/reject:/worker: subject buckets (all keyed by `key`, per the _alert_ops call
            # sites above) had NO flush path (only escalate: was flushed above), so a suppressed over-cap
            # count in any of those buckets died silently once the worker exited. Mirror the escalate flush
            # for each; flush_subject is a cheap no-op when nothing is pending for that subject.
            for _flush_prefix in ("abort", "reject", "worker", "blocked"):   # plus blocked:<key>
                try:
                    self._flush_ops(f"{_flush_prefix}:{key}")
                except Exception as e:
                    log.warning("flush_ops key=%s prefix=%s failed: %s", key, _flush_prefix, e)
            # the worker OWNS its worktree: remove it here (idempotent w/ teardown's signal path).
            if worktree:
                try:
                    self.worktree_mgr.remove(key)
                except Exception as e:
                    log.warning("worktree remove key=%s failed: %s", key, e)
            if getattr(worker, "hermes_home", None):     # A hermes worker's private HERMES_HOME goes with it
                shutil.rmtree(worker.hermes_home, ignore_errors=True)   # (weakref.finalize in hermes.py = backstop)
            if repo_lock is not None:                    # release AFTER teardown so the
                repo_lock.release()                      # whole cut/publish/teardown cycle is single-flight
            # Settle THIS worker's coalescer entry if it exited under its OWN power (turn finished,
            # max-forwards, or RAISED) and the LivenessDriver has NOT already begun the close. Without this
            # a self-exited worker left live[key] in open_running with a dead thread, so the next liveness
            # sweep force-closed it (spurious FORCE-CLOSE alert) and force_pop left the subject eligible, so
            # it re-spawned on later ticket noise (the ZOMBIE class). settle_self is a NO-OP when
            # state is already closing/closed: the clean liveness close owns that teardown, and teardown
            # JOINS this thread BEFORE finish_close, so this runs while state==closing and is a no-op; finish_close
            # wins (and marks completed). Runs AFTER worktree removal so "old worktree gone" happens-before
            # any respawn the pop could unblock (mirrors teardown's join-before-finish_close discipline).
            # `attempted` is False iff every exit above was a FIXABLE spawn-time fail-closed
            # REJECT (no worktree cut, or the inbox cred-free cwd, was ever reached) or the transient
            # ticket-fetch-failed abort. Such a subject must NOT be stamped `completed`: doing so made the
            # reject PERMANENT (e.g. "no repo: token" -> even adding the repo: line could never re-dispatch
            # the ticket short of a broker restart). By contrast, a real run (success, failure, or a raise
            # AFTER the cut was attempted) still stamps, unchanged, and so does the ticket-ALREADY-DONE
            # drop (also sets attempted=True at its own return site): that one IS terminal, not fixable, so
            # leaving it unstamped would re-fetch and re-drop on every future webhook for the same ticket.
            try:
                # An API-overload give-up (every 429/529 retry failed) is transient, not terminal, so it is un-stamped.
                # So is a skipped MR review (diff unavailable / cut not at the MR head): no verdict exists,
                # so a re-emitted mr.opened for the same MR must be able to re-run the review.
                _settled = self._settle_self(
                    key, mark_completed=attempted and not review_skipped
                    and not getattr(worker, "api_overloaded", False))
                if _settled and _settled.get("fwd_buffer"):
                    # Leftover buffered events should be empty (a live worker took the forward branch), but
                    # never silently drop them; re-arm as a fresh spawn, mirroring _do_close's guard. (If the
                    # subject was just marked completed, tick dedup-drops the re-arm: a self-EXITED worker is
                    # done with the subject and retry-on-failure is not a feature; the events are logged here.)
                    # rearm_pending drops (logs) forward-only comments; n counts only re-armed.
                    if self.coalescer is not None:
                        n = self.coalescer.rearm_pending(key, _settled["fwd_buffer"])
                        if n:
                            log.warning("settle-self key=%s: %d leftover buffered event(s) re-armed as a fresh spawn",
                                        key, n)
            except Exception as e:                       # a settle failure must never wedge worker teardown
                log.warning("settle-self key=%s failed: %s", key, e)
            with self._lock:
                self._workers.pop(key, None)
            if worker:
                worker.done = True

    def _turn(self, worker, route, system_prompt, prompt_text, resume, run_id):
        """Run one turn under the concurrency cap (acquired PER TURN, off the coalescer lock), with the
        envelope re-prompt. Returns the parsed envelope or None."""
        if self._sem is not None:
            self._sem.acquire()
        try:
            return self._turn_with_envelope(worker, route, system_prompt, prompt_text, resume, run_id)
        finally:
            if self._sem is not None:
                # Limitation: this release is NOT reached if a SIGKILLed child never reaps and the second
                # communicate() in _one_turn blocks (see the comment there) -> that one cap slot leaks.
                # SIGKILL makes it ~unreachable, and the liveness force-close still frees the coalescer side regardless.
                self._sem.release()

    def _turn_with_envelope(self, worker, route, system_prompt, prompt_text, resume, run_id):
        # A turn is "complete" when it yields a PARSED result-envelope.
        # If none parses at the turn boundary, the retry is a REAL warm-worker RE-PROMPT (a --resume turn
        # asking for a clean {status,summary,wake} envelope), NOT a re-parse of identical bytes. Only if the
        # re-prompt ALSO fails do we escalate (ticket comment and wake), NEVER to the operator directly.
        #
        # When the usage-cap fallback is ENABLED (default OFF), the FIRST dispatch runs the bounded
        # backend chain (native Anthropic, then deepseek, then vLLM-Main). On total exhaustion it has ALREADY
        # loud-failed, so short-circuit: do NOT re-prompt a capped backend or double-escalate. When
        # disabled, this is byte-identical to the plain single-dispatch path.
        if USAGE_CAP_FALLBACK_ENABLED:
            result, exhausted = self._dispatch_with_fallback(
                worker, route, system_prompt, prompt_text, resume, run_id)
            if result is None and exhausted:
                return None
        else:
            result = self._one_turn(worker, route, system_prompt, prompt_text, resume)
        if result is not None:
            return result
        # A turn killed by teardown() is a parse miss, but NEVER re-prompt it: the re-prompt
        # would launch a FRESH worker process after teardown's kill (unkilled until the wall clock, outliving
        # teardown's join / worktree removal, and for mr-review able to post a verdict). No escalation either:
        # the close was deliberate, and a force-close already alerts from the LivenessDriver.
        if getattr(worker, "done", False):             # getattr: minimal Worker stand-ins (tests) lack it
            log.info("worker %s torn down mid-turn -> no warm re-prompt, no escalation", worker.key)
            return None
        # Resume guard: a retry/re-prompt --resumes the session the fallback just produced, so pin it to THAT
        # backend (sticky), not route.backend; else it re-runs on the capped native head and escalates.
        sticky = ({"backend_override": worker.last_backend}
                  if USAGE_CAP_FALLBACK_ENABLED and hasattr(worker, "last_backend") else {})
        # A 429/529 API failure is transient, not a parse miss: back off and re-run the SAME turn; never
        # spend the warm re-prompt on it. If every retry is still overloaded, give up ONCE (comment and alert) and
        # flag the worker so _run_worker exits WITHOUT stamping the subject completed (re-armable).
        if _detect_api_overload(getattr(worker, "last_output", None)):
            for attempt, delay in enumerate(self._api_overload_delays_s, 1):
                log.warning("API overload (429/529) key=%s run_id=%s -> retry %d/%d in %ss", worker.key, run_id,
                            attempt, len(self._api_overload_delays_s), delay)
                remaining = float(delay)
                while remaining > 0 and not getattr(worker, "done", False):   # teardown interrupts the wait
                    step = min(remaining, 5.0)
                    self._api_overload_sleep(step)
                    remaining -= step
                if getattr(worker, "done", False):
                    log.info("worker %s torn down during API-overload back-off -> no retry", worker.key)
                    return None
                result = self._one_turn(worker, route, system_prompt, prompt_text, resume, **sticky)
                if result is not None:
                    return result
                if not _detect_api_overload(getattr(worker, "last_output", None)):
                    break                                # a real parse miss now -> the warm re-prompt below
            else:
                self._api_overload_giveup(worker, run_id)
                return None
            if getattr(worker, "done", False):
                return None
        # Deepseek (and other models) read a bare `{status, summary, wake}` as an unfilled
        # TEMPLATE PLACEHOLDER and reply "No response requested.", so give a CONCRETE example object it can
        # copy verbatim. Single-quoted Python string so the inner double quotes stay literal; json.dumps then
        # escapes them, so the worker receives a literal example object, never the placeholder braces.
        # The example status comes from THIS capability's allowed set (mr-review: pass|block, example
        # the fail-safe `block`), never a hard-coded `done`; a `done` from an mr-review worker mislabelled a
        # verdict. try_parse rejects any out-of-set status, so the set is also stated explicitly.
        ex_status, allowed = reprompt_status_example(getattr(route, "capability", "read"))
        reprompt = json.dumps({"_broker_reprompt":
                               'your last turn did not emit a parseable result envelope; reply with ONLY '
                               'a JSON object exactly like this, no other text: '
                               '{"status":"' + ex_status + '","summary":"<a short HTML summary of what you did>",'
                               '"wake":false} - "status" MUST be one of: ' + "|".join(allowed) +
                               ' (use the value that is actually true for your work, not just the example)'})
        # The turn that led here was wall-clock hard-killed, so bound THIS
        # one re-prompt turn to a short cap (never a second full, possibly LOCAL_WALL_CLOCK_MULT-scaled,
        # allotment) so a dead-ended worker releases its concurrency slot promptly instead of sitting on it.
        # Only passed when actually hard-killed (else omitted) so a test/harness that stubs _one_turn without
        # this kwarg keeps working unchanged in the (overwhelmingly common) non-hard-kill re-prompt case.
        reprompt_kwargs = dict(sticky)
        if getattr(worker, "hard_killed", False):
            reprompt_kwargs["wall_clock_override"] = HARDKILL_REPROMPT_CAP_S
        result = self._one_turn(worker, route, system_prompt, reprompt, resume=True, **reprompt_kwargs)
        if result is None:
            escalate = getattr(self.envelope_parser, "escalate", None)
            if escalate is not None:
                # Name the ticket (parsed from the subject key) so the escalation DM + ticket
                # comment identify their ticket. Also hand the raw worker output so escalate wraps it
                # UNTRUSTED. Both degrade cleanly (task_id None / worker_text None) for a non-vikunja key.
                # A turn-capped worker ALSO fails to parse (the cap emits a `error_max_turns` wrapper
                # with no inner envelope); distinguish WHY it failed so the alert isn't a bare "parse miss"
                # for a worker that simply ran out of turns (inbox rows have no code-row WIP-branch signal).
                _why = ("ran out of turns (--max-turns cap) before emitting an envelope"
                        if getattr(worker, "turn_capped", False) else "no parseable result envelope after a warm re-prompt")
                escalate(run_id, route, _why,
                         task_id=tickets.vikunja_id(worker.key),
                         worker_text=getattr(worker, "last_output", None))
            else:
                self._alert_ops(f"worker {worker.key} run_id={run_id}: no envelope after warm re-prompt",
                                subject=f"worker:{worker.key}")
        return result

    def _api_overload_giveup(self, worker, run_id):
        """Every 429/529 back-off retry failed. Flag the worker (so _run_worker exits WITHOUT stamping the
        subject completed), post ONE ticket comment and send the alert ONCE. Never raises into the turn."""
        worker.api_overloaded = True
        n = len(self._api_overload_delays_s)
        log.error("API overload: %d back-off retr%s exhausted key=%s run_id=%s -> exiting un-stamped",
                  n, "y" if n == 1 else "ies", worker.key, run_id)
        self._post_ticket_note(worker.key, (
            "<p><b>⚠️ Anthropic API overloaded:</b> the worker could not run - every turn failed with a "
            f"transient API error (HTTP 429/529) across {n} back-off retr{'y' if n == 1 else 'ies'}. "
            "No work was done. The ticket was left <b>re-armable</b>: re-trigger it (e.g. update the ticket) "
            "once the API recovers and a fresh worker will spawn.</p>"), run_id)
        link = self._ticket_link(tickets.ref_from_key(worker.key))
        try:
            self._alert_ops(f"worker {worker.key} run_id={run_id}{link}: Anthropic API overloaded (429/529) after "
                            f"{n} back-off retries - left un-stamped (re-armable)", subject=f"worker:{worker.key}")
        except Exception as e:                           # never let an alert failure wedge the worker
            log.warning("api-overload ops alert failed key=%s: %s", worker.key, e)

    # ---- usage-cap fallback chain (default OFF; USAGE_CAP_FALLBACK_ENABLED=1 to enable) ----
    def _backend_native(self, be):
        """A native-Anthropic candidate: '' (row default) or the sentinel registry name 'claude' (which
        load_backend resolves to None -> keeps the default Anthropic env). Always runnable."""
        return (not be) or be == "claude"

    def _backend_first_party(self, be):
        """True for a native candidate (see _backend_native) OR a registry `cloud` backend whose base_url
        is the first-party Anthropic API (exactly https://api.anthropic.com[:443], parsed, not substring-matched;
        no userinfo). Such a backend keeps native-Anthropic trust: the real auto-mode classifier works on it
        (bypass_classifier stays False) and the code capability may run on it. Deliberately NOT used by the
        subscription-usage / fallback-chain checks (those keep _backend_native: a first-party API-key lane is
        the overflow for a capped subscription, so it must not be dropped as 'native')."""
        if self._backend_native(be):
            return True
        b = _backend_entry(be)
        if not b or b.get("kind") != "cloud":
            return False
        try:
            u = urlsplit(str(b.get("base_url") or ""))
            return (u.scheme == "https" and u.hostname == "api.anthropic.com"
                    and u.port in (None, 443) and not u.username and not u.password)
        except ValueError:
            return False

    def _usage_cap_chain(self, route):
        """Ordered backend NAMES to try this turn ('' = native Anthropic). The row's own backend first, then
        USAGE_CAP_FALLBACK_CHAIN, deduped in order. PROACTIVE: if the head is native Anthropic and the usage
        file says we're over the cap, DROP the doomed Anthropic head so we start on the first fallback (the
        reactive detector still covers a stale/absent file)."""
        head = getattr(route, "backend", "") or ""
        tail = [b.strip() for b in (USAGE_CAP_FALLBACK_CHAIN or "").split(",") if b.strip()]
        chain = [head] + tail
        if self._backend_native(head) and _usage_over_threshold():
            log.info("usage-cap PROACTIVE: %s over threshold -> skipping native Anthropic head",
                     FLEET_USAGE_FILE)
            chain = tail or [head]                         # no fallbacks configured -> keep head (reactive still applies)
        seen, out = set(), []
        for b in chain:
            k = "" if self._backend_native(b) else b       # collapse native forms
            if k in seen:
                continue
            seen.add(k); out.append(b)
        return out

    def _dispatch_with_fallback(self, worker, route, system_prompt, prompt_text, resume, run_id):
        """Run the turn on the row backend, and on a usage-cap signal re-dispatch the SAME turn on each
        USAGE_CAP_FALLBACK_CHAIN backend in order, AT MOST ONCE each. Returns (result, exhausted): result is
        the parsed envelope (or None on a parse-miss from a NON-capped backend; the caller re-prompts);
        exhausted=True means every candidate was capped/unresolvable AND we have ALREADY loud-failed
        (caller must NOT re-prompt/re-escalate). detection-before-correction: the log shows the cap was
        DETECTED, not just that a retry happened."""
        chain = self._usage_cap_chain(route)
        tried, last, ran = [], None, 0
        for be in chain:
            if not self._backend_native(be) and load_backend(be)[0] is None:
                log.warning("usage-cap fallback: backend %r unresolvable in %s (or %s) -> skipping",
                            be, SPAWN_MODELS_JSON, VLLM_CC_ENV)
                continue
            label = be or "claude"
            use_resume = resume if ran == 0 else False     # only the first ACTUAL attempt may --resume (a
            ran += 1                                        # fallback backend can't resume the prior session)
            tried.append(label)
            last = self._one_turn(worker, route, system_prompt, prompt_text, use_resume,
                                  backend_override=be)
            if not _detect_usage_cap(getattr(worker, "last_output", None)):
                worker.last_backend = be                   # the backend that owns the session (re-prompt sticks to it)
                return last, False                         # NOT usage-capped -> this backend's result stands
            log.warning("usage-cap DETECTED on backend=%s key=%s run_id=%s -> falling through to next",
                        label, worker.key, run_id)
        self._loud_fail_all_backends(worker, route, run_id, tried)
        return last, True

    def _loud_fail_all_backends(self, worker, route, run_id, tried):
        """LOUD failure when the whole chain is exhausted: escalate via the wired alert sinks (envelope_parser.
        escalate = alert DM and ticket comment; else alert_ops = alert DM) with the job identity (ticket/route) and
        which backends were tried. A fully-failed job must NOT silently vanish (a past defect)."""
        triedstr = ", ".join(tried) if tried else "(none runnable)"
        reason = (f"usage-cap fallback EXHAUSTED: every backend was usage-capped or unresolvable "
                  f"[tried: {triedstr}; chain: {USAGE_CAP_FALLBACK_CHAIN!r}] - job not run")
        task_id = tickets.vikunja_id(worker.key)
        escalate = getattr(self.envelope_parser, "escalate", None)
        if escalate is not None:
            escalate(run_id, route, reason, task_id=task_id,
                     worker_text=getattr(worker, "last_output", None))
        else:
            self._alert_ops(f"worker {worker.key} run_id={run_id}: {reason}",
                            subject=f"worker:{worker.key}")
        log.error("usage-cap fallback exhausted key=%s run_id=%s tried=%s", worker.key, run_id, tried)

    def _one_turn(self, worker, route, system_prompt, prompt_text, resume, backend_override=_NO_OVERRIDE,
                  wall_clock_override=None):
        # Rotate the session when it gets big: drop --resume, start fresh.
        rotate = worker.turns >= self.rotate_turns or worker.tokens >= self.rotate_tokens
        resume_session = None if (rotate or not resume) else worker.session_id
        if rotate:
            log.info("session rotation key=%s (turns=%d tokens=%d) -> fresh session",
                     worker.key, worker.turns, worker.tokens)
            worker.turns = 0; worker.tokens = 0; worker.session_id = None
        # per-row harness selector (Route.adapter, default "cc" = the claude path below, unchanged). "hermes"
        # hands the harness step to adapters/hermes.py (same Worker fields, caps, envelope parser, sinks); any
        # other value raises, and _run_worker's except sends an alert (fail-closed; Route already rejects it at load).
        harness = getattr(route, "adapter", "cc")
        if harness != "cc":
            if harness != "hermes":
                raise ValueError(f"unknown worker adapter {harness!r} - refusing (fail-closed)")
            if self.hermes is None:
                from .hermes import HermesHarness     # lazy: a cc-only broker never loads the hermes harness
                self.hermes = HermesHarness()
            return self.hermes.one_turn(self, worker, route, system_prompt, prompt_text, resume_session,
                                        backend_override=backend_override,
                                        wall_clock_override=wall_clock_override)
        # Pass the route's declared capability so the harness grants are gated by it
        # (write gets acceptEdits and git-commit, no push; read/unknown get read-only, fail-closed).
        capability = getattr(route, "capability", "read")
        # For an inbox route, compute the PER-ACCOUNT MCP config path
        # (<mcp_config_dir>/<account>.json) from the subject key + route; build_argv fails closed if it is
        # absent (never a hard-coded path). Only inbox passes a path; every other capability passes None.
        mcp_config_path = None
        if capability in ("inbox", "inbox-chat"):
            # A zulip chat key (zulip:<bot>:<sender>) has no account in it, so use the one the spawn gate
            # resolved from the bot map (worker.inbox_account); gmail:<account> keys keep the key-derived path.
            account = getattr(worker, "inbox_account", None) or (
                worker.key.split(":")[1] if ":" in worker.key else worker.key)
            mcp_config_path = os.path.join(getattr(route, "mcp_config_dir", "") or "", f"{account}.json")
        elif capability == "general":   # reuse the fixed config
            # general worker: a SINGLE fixed non-secret Hindsight-only config (not per-account). build_argv
            # fails closed if it is absent, so the worker can NEVER fall back to the ambient MCP surface.
            mcp_config_path = getattr(route, "mcp_config_path", "") or None
        # A code route declares its per-repo test runner (a KEY into cc._CODE_TEST_RUNNERS,
        # NOT a raw command); pass it through so the grant adds ONLY that runner's reviewed test-Bash.
        # A code worker's PER-REPO runner (resolved at spawn) wins over the route default.
        test_runner = getattr(worker, "test_runner", None) or getattr(route, "test_runner", "") or None
        # general worker: a route may load the user-level rules + the classifier config via setting-sources="user"
        # (default "" = charter-blind, every other row). None here would mean "use the adapter default", so
        # coalesce an empty route value to None to preserve that default for non-general rows.
        setting_sources = getattr(route, "setting_sources", "") or None
        # A route may run its worker on a NON-default inference backend
        # (deepseek/glm) so it grinds on non-Anthropic budget. Resolve it from the shared spawn registry;
        # empty/'claude' -> keep the fleet-model.sh model + default Anthropic env. An explicitly selected
        # non-Anthropic ROW backend that won't resolve FAILS CLOSED (see below).
        eff_model, backend_env, effort = route.model, {}, None
        # An explicit backend_override (from the usage-cap fallback loop) forces THIS turn's backend
        # ('' / 'claude' = native Anthropic); the default sentinel keeps today's behavior (use route.backend).
        _backend = getattr(route, "backend", "") if backend_override is _NO_OVERRIDE else backend_override
        # A "code" worker's test-runner grant is ARBITRARY EXEC AS THE BROKER UID (pytest/
        # npm/go/venv; see _code_test_bash), gated today only by the code-reviewed runner MENU + the
        # deny-floor. Routing that worker through a non-Anthropic backend ALSO flips bypass_classifier=True
        # (below), which is a DIFFERENT trust boundary than the one the code capability was reviewed under,
        # so refuse it by default, regardless of how `_backend` was selected (an explicit <ROW>_BACKEND pin,
        # a FLEET_MODE-driven answer, or a fallback-chain override). CODE_NONCLAUDE_OK=1 is the explicit
        # opt-in; unset/0 = refused. Fails the SAME way an unresolvable backend name does (raising into _run_worker's
        # except, which alerts; nothing spawned) so this can't be mistaken for a resolvable-but-skipped rung.
        # A first-party api.anthropic.com registry backend (claude-api) has native-Anthropic trust (real
        # classifier, no bypass) so it is not "non-native" for this refusal; deepseek & co. still are.
        if capability == "code" and not self._backend_first_party(_backend) and not _env_flag("CODE_NONCLAUDE_OK"):
            raise ValueError(
                f"code capability refuses non-native backend {_backend!r} (fail-closed): its test-runner "
                f"grant is arbitrary exec as the broker UID, which a non-Anthropic backend's different trust "
                f"posture (bypass_classifier=True) was never reviewed under - set CODE_NONCLAUDE_OK=1 to opt in")
        bypass_classifier = False   # fail-closed: native Anthropic keeps the auto classifier posture
        if _backend:
            bmodel, benv, beffort = load_backend(_backend)
            if bmodel:
                eff_model, backend_env, effort = bmodel, benv, beffort
                # non-Anthropic backend: auto classifier self-blocks (see general branch). A first-party
                # api.anthropic.com backend (claude-api) keeps the real classifier, so bypass stays False.
                bypass_classifier = not self._backend_first_party(_backend)
            elif backend_override is _NO_OVERRIDE and not self._backend_native(_backend):
                # Cheap-cloud lane: the ROW explicitly selected a non-Anthropic backend (e.g.
                # GENERAL_BACKEND=deepseek, or the FLEET_MODE-driven backend name routing.Route.with_model
                # derived from fleet-model.sh). If it won't resolve (typo / registry entry gone / file unreadable)
                # FAIL CLOSED: refuse the turn (_run_worker's except raises the alert) instead of silently spending
                # the Anthropic budget the operator chose to route off. Unreachable while route.backend == ""
                # (knob unset AND a claude-* mode answer). The fallback loop passes an explicit override and pre-skips
                # unresolvable rungs itself, so it never lands here.
                raise ValueError(f"route backend {_backend!r} not resolvable in {SPAWN_MODELS_JSON} - "
                                 f"refusing to fall back to native Anthropic (fail-closed)")
            else:
                log.warning("route backend %r not resolvable in %s -> falling back to %s / default backend",
                            _backend, SPAWN_MODELS_JSON, route.model)
        # With streaming ON, pin every FRESH session (seed / rotation / non-resume fallback) to a uuid we
        # choose, so the dash stream can find ~/.claude/projects/*/<uuid>.jsonl while the turn is still running
        # (session_id is otherwise only learned post-exit). A --resume turn keeps its session (and file). OFF =
        # argv unchanged.
        stream_kw = {}
        if not resume_session and stream.enabled():
            worker.stream_session = str(uuid.uuid4())
            stream_kw["session_uuid"] = worker.stream_session   # only passed when ON: OFF call is byte-identical
            stream.remember(worker.key, worker.stream_session)  # Browsable after the worker ends
        if test_runner == "venv":                        # same only-when-needed shape as session_uuid
            stream_kw["test_repo"] = getattr(worker, "repo", None)
        if getattr(route, "site_capability", ""):        # only when set, so a built-in row's call is unchanged
            stream_kw["site_capability"] = route.site_capability
        grant_path = getattr(worker, "write_grant_path", None)   # Set at spawn only for a granted infra worker
        if grant_path and capability == "code":
            stream_kw["write_grant"] = True
        argv = self.build_argv(prompt_text, eff_model, system_prompt, resume_session,
                               capability=capability, mcp_config_path=mcp_config_path,
                               test_runner=test_runner, setting_sources=setting_sources, effort=effort,
                               bypass_classifier=bypass_classifier, **stream_kw)
        # Build the worker env from an ALLOWLIST (scrub_worker_env), NOT os.environ.copy(),
        # since the broker's full env carries GITLAB_MR_TOKEN and BROKER_HMAC_SECRET, an exfil risk under an
        # untrusted-ticket / bypass worker; the worker needs neither. route.env (ZBUS_ENV) + backend_env (a
        # non-default backend's base_url/token/model-routes) are layered on top; the mcp.sh dir is prepended to
        # PATH so `mcp.sh` resolves by bare name and wins. Default-Anthropic auth is file-based (~/.claude
        # via HOME), so no env token is dropped that a worker needs.
        env = scrub_worker_env(os.environ, route_env=route.env, backend_env=backend_env)
        if stream_kw.get("write_grant"):                 # The mcp-rw.sh wrapper finds this worker's grant here
            env["EPH_WRITE_GRANT_FILE"] = grant_path
        # A src-layout repo (e.g. game-sync) would otherwise import the package the venv has INSTALLED
        # (the base repo's copy), not the worktree's edits, giving a false green. Put the worktree's src/ first.
        _wt_src = os.path.join(worker.worktree or "", "src")
        if test_runner == "venv" and worker.worktree and os.path.isdir(_wt_src):
            env["PYTHONPATH"] = os.pathsep.join(filter(None, (_wt_src, env.get("PYTHONPATH"))))
        # A worker granted mcp-ro.sh reaches Hindsight/memory by BARE NAME, which only
        # resolves via scrub_worker_env's PATH prepend. If that prepend ever regresses, the worker
        # silently 127s and memory dies quietly. Loud-guard (non-fatal; memory is best-effort, don't kill
        # the task): if the grant includes mcp-ro.sh but it's not resolvable on the worker PATH, alert loudly.
        if any("mcp-ro.sh" in tok for tok in argv) and not shutil.which("mcp-ro.sh", path=env.get("PATH", "")):
            log.error("worker %s: granted mcp-ro.sh but it is NOT resolvable on the worker PATH "
                      "(silent-127 risk, memory broken) - check scrub_worker_env PATH prepend", worker.key)
            self._alert_ops(f"worker {worker.key}: mcp-ro.sh granted but unresolvable on worker PATH "
                            f"(silent-127 / memory-broken tripwire)", subject=f"worker:{worker.key}")
        wall_clock_s = self.wall_clock_by_capability.get(capability, self.wall_clock_s)   # per-capability
        # The RESOLVED backend for this turn (native/cloud unchanged; a kind:"local" vLLM rung is
        # measurably slower, so it gets LOCAL_WALL_CLOCK_MULT x the per-capability allotment). Checked on
        # `_backend` (the name actually dispatching this turn), never the row's unresolved default.
        local_turn = _is_local_backend(_backend)
        if local_turn:
            wall_clock_s = wall_clock_s * LOCAL_WALL_CLOCK_MULT
        if wall_clock_override is not None:
            # Bound the ONE warm-re-prompt turn after a wall-clock hard-kill so a dead-ended
            # worker releases its concurrency slot promptly instead of spending a second full allotment.
            wall_clock_s = min(wall_clock_s, wall_clock_override)
        log.info("spawn key=%s capability=%s backend=%s local=%s effective wall-clock=%ss",
                 worker.key, capability, _backend or "claude", local_turn, wall_clock_s)
        worker.turn_wall_clock = wall_clock_s            # the liveness backstop scales with it (turn_deadline_extra)
        worker.in_turn = True                            # turn-completion signal (liveness consumer)
        worker.turn_started = time.time()                # force-close timing (turn_age)
        worker.hard_killed = False                       # Reset before this turn; set True only below
        proc = subprocess.Popen(argv, cwd=worker.worktree, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        worker.proc = proc
        try:
            out, err = proc.communicate(timeout=wall_clock_s)   # wall-clock hard-kill (per-capability)
        except subprocess.TimeoutExpired:
            proc.kill()
            # Limitation: this second communicate() reaps the SIGKILLed child. In the pathological case where
            # the child never reaps (kernel D-state / uninterruptible sleep), communicate() blocks and THIS
            # thread never reaches the _turn() sem release -> that one concurrency slot leaks for this worker.
            # SIGKILL makes that ~unreachable in practice; and even then the force-close still pops and re-arms
            # the COALESCER side (state set to closed, buffer re-armed), so the SUBJECT is never wedged; at worst a
            # single sem slot is lost until process restart, not a stuck subject. No code change.
            out, err = proc.communicate()
            worker.hard_killed = True                    # _turn_with_envelope caps the warm re-prompt
            log.warning("HARD-KILL key=%s wall-clock %ss exceeded", worker.key, wall_clock_s)
            self._alert_ops(f"worker {worker.key} hard-killed: wall-clock {wall_clock_s}s",
                            subject=f"worker:{worker.key}")
        finally:
            worker.in_turn = False                       # turn complete: the signal fires here
            worker.turn_started = None                   # between turns
        worker.turns += 1
        worker.last_output = out          # keep the raw output so escalate() can wrap it UNTRUSTED
        if _detect_turn_cap(out):         # sticky: _run_worker saves a WIP branch at teardown (no MR)
            worker.turn_capped = True
            log.warning("TURN-CAP key=%s: --max-turns %s exhausted", worker.key,
                        self.max_turns_by_capability.get(capability, self.max_turns))
        # Parse WITHOUT escalating here; escalation is owned by _turn_with_envelope
        # (after the warm re-prompt). A parsed envelope IS the turn-completion signal; pid-exit is a crash.
        parse = getattr(self.envelope_parser, "try_parse", self.envelope_parser)
        result = parse(out, worker.run_id, route)        # parse {status,summary,wake}
        rc = getattr(proc, "returncode", 0)              # getattr: minimal Popen stand-ins (tests) lack it
        if result is None or rc != 0:                    # Keep the raw failed turn for postmortem
            capture_failed_turn(worker, route, "parse miss" if result is None else "non-zero exit", rc, out, err)
        if result is not None and rc != 0:
            # A process that exited non-zero is a FAILED turn even if its stdout holds a valid envelope
            # (same PASS gating as hermes.py); otherwise a cc mr-review `pass` from a failed run could reach CD.
            # That is a parse miss (warm re-prompt, then escalation); the else-branch below still keeps session_id.
            log.warning("turn key=%s exited rc=%s with a parseable envelope -> treated as parse miss",
                        worker.key, rc)
            result = None
        if result is not None:
            worker.session_id = result.get("session_id", worker.session_id)
            worker.tokens += int(result.get("tokens", 0) or 0)       # token count if exposed
        else:
            # session-resume fix: on a parse-MISS the inner {status,summary,wake} envelope was absent,
            # but the CLI `--output-format json` wrapper still carries a TOP-LEVEL session_id. Preserve it
            # so the warm re-prompt in _turn_with_envelope --resumes the SAME session (with its full
            # Gmail/ledger/classification context) instead of spawning a brand-new one: a fresh session
            # has no -p posture brief and an INBOX worker then fail-closes (constraint 12), so losing the
            # session_id here silently defeats the whole warm-re-prompt recovery net.
            try:
                _, session_id, _ = parse_cli_json(out)
                if session_id:
                    worker.session_id = session_id
            except Exception:
                pass                                     # malformed output -> leave session_id unchanged
        return result

    # ---- primitive: forward (enqueue a warm --resume turn onto the worker's command loop) ----
    # The LivenessDriver calls this at a turn boundary with the COLLAPSED buffered forwards
    # It is NON-BLOCKING (enqueue and return) so it never blocks the driver sweep; the worker thread
    # runs the actual turn, one at a time.
    def forward(self, key, brief):
        with self._lock:
            worker = self._workers.get(key)
        if not worker or worker.done:
            log.info("forward key=%s: no live worker, dropping (coalescer will spawn fresh)", key)
            return None
        worker.cmdq.put(("forward", brief))
        return True

    # ---- primitive: liveness (turn-in-progress signal; used ONLY by the force-close backstop) ----
    def liveness(self, key):
        with self._lock:
            worker = self._workers.get(key)
        return bool(worker and not worker.done and worker.in_turn)

    def turn_age(self, key):
        """Seconds the current turn has been running, or None if idle/gone. Used for force-close timing.
        pid is NOT consulted: this is 'no advertised turn progress', not 'is the pid alive'."""
        with self._lock:
            worker = self._workers.get(key)
        if worker and worker.in_turn and worker.turn_started is not None:
            return time.time() - worker.turn_started
        return None

    def turn_deadline_extra(self, key):
        """Seconds the CURRENT turn's per-capability wall-clock exceeds the global wall_clock_s (0 when
        none / idle). The LivenessDriver adds it to progress_timeout so its silent-turn backstop keeps the
        same headroom above a longer hard-kill (code=3600s) instead of force-closing a healthy long turn.
        Pairs with the progress_timeout FLOOR (liveness.resolve_progress_timeout): a global raise of
        wall_clock_s used to leave the backstop below the hard-kill and force-close healthy turns; the floor now
        guarantees the backstop sits above max(wall_clock_s, per-capability) regardless of the cfg progress_timeout_s."""
        with self._lock:
            worker = self._workers.get(key)
        wc = getattr(worker, "turn_wall_clock", None) if worker else None
        return max(0, wc - self.wall_clock_s) if wc else 0

    def worker_alive(self, key):
        """Is the worker's own loop thread still running? Lets the driver detect a worker that died with
        state=open_running (advertised running but the thread is gone), so it can force-close.

        Startup race: `self._workers[key]` is not set until AFTER worktree_mgr.make()
        (~300ms) inside `_run_worker`, but the spawn thread is registered in `self._threads[key]` BEFORE
        that (in `spawn()`, before `t.start()`). During that setup window the liveness sweep sees
        state=open_running, turn_age()=None (no turn yet) and worker_alive()=False (worker not yet
        registered), which causes a FALSE force-close of a worker that is alive and still setting up, so no claude
        ever runs and live_workers leaks a phantom. Fix: once the Worker object is registered, use its own
        liveness (same as before); until then, fall back to the spawn thread in `_threads`; both refer to
        the SAME underlying `_run_worker` thread (worker.thread is set to threading.current_thread() at the
        same point `_workers[key]` is registered), so this only WIDENS the window where worker_alive can
        see the thread as alive; it does not change steady-state behaviour. Once `_run_worker`'s finally
        pops `_workers[key]` and the thread ends, `_threads[key].is_alive()` is False too, so the result is still False."""
        with self._lock:
            worker = self._workers.get(key)
            if worker is not None:
                return bool(not worker.done and worker.thread and worker.thread.is_alive())
            t = self._threads.get(key)                   # setup window: spawned, not yet registered
            return bool(t and t.is_alive())

    # ---- primitive: teardown (hard-stop; runs LOCK-FREE with respect to the coalescer lock) ----
    # Kill any in-flight proc (pid used ONLY here: kill and reclaim the cap slot) and signal
    # the worker loop to exit; the worker thread's finally removes the worktree + pops itself. Idempotent.
    def teardown(self, key):
        with self._lock:
            worker = self._workers.get(key)
            t = self._threads.get(key)                   # the _run_worker thread that OWNS the worktree
        if not worker:
            return
        # Mark done BEFORE the kill, so the killed turn's thread (see _turn_with_envelope / hermes one_turn)
        # already sees done=True when its communicate() returns, so there's no window for a post-kill re-prompt launch.
        worker.done = True
        if worker.proc and worker.proc.poll() is None:
            if getattr(worker, "kill_group", False):     # Hermes runs in its own session, so kill the group
                from .hermes import _kill_group          # (its tool children too), not just the parent pid
                _kill_group(worker.proc)
            else:
                try:
                    worker.proc.kill()                   # pid: kill only
                except OSError:
                    pass
        worker.cmdq.put(("close",))                      # wake the loop so it exits, removes the worktree, reclaims slot
        # JOIN the worker thread here so its finally-block
        # worktree removal (_run_worker, above) has COMPLETED before teardown returns. teardown is called by
        # the LivenessDriver's _do_close IMMEDIATELY before finish_close pops live[key] (which unblocks a
        # same-subject respawn's worktree make()). Joining first makes "old worktree removed" happen-BEFORE
        # "respawn make()" BY CONSTRUCTION, not by ~1s tick latency and defensively-coded make(). Bounded: the
        # proc was already SIGKILLed so the thread should wind down promptly; if the join times out we LOG and
        # PROCEED (never block forever). LOCK-FREE with respect to the coalescer lock: teardown only ever
        # runs in the lock-free _do_close/_force_close window, and this join holds neither the coalescer lock
        # nor (during the wait) self._lock; both were released above, so the worker's own finally can pop.
        if t is not None and t is not threading.current_thread():
            t.join(self.teardown_join_s)
            if t.is_alive():
                log.warning("teardown key=%s: worker thread still alive after %.0fs join "
                            "(proc already killed) -> proceeding", key, self.teardown_join_s)

    # test/teardown helper: join the spawn thread
    def _join(self, key, timeout=None):
        with self._lock:
            t = self._threads.get(key)
        if t:
            t.join(timeout)

    def live_pid_count(self):
        """Coarse gauge of live worker processes, used ONLY for the global concurrency cap
        (NEVER for per-subject forward-vs-drain; state stays the sole sync token)."""
        with self._lock:
            return sum(1 for w in self._workers.values()
                       if w.proc and w.proc.poll() is None and not w.done)
