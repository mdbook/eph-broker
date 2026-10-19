#!/usr/bin/env python3
"""app.py: the Phase-1 broker (HTTP front door, tick loop and wiring).

Extends the Phase-0 Handler/tick-loop lift with:
  * HMAC check on the Handler: `X-Broker-HMAC` = hex HMAC-SHA256(raw body), optional `sha256=` prefix.
    VALUE-FREE: the secret is read from an env-var NAME at runtime (the bytes are provisioned outside this
    repo). BROKER_HMAC_MODE off|log|enforce, default enforce (reject a missing or bad signature); see the
    HMAC block below.
  * ingest path: HMAC, then echo-guard, then coalescer.on_event (echo-guard runs BEFORE on_event so
    the coalescer's locked island stays verbatim).
  * concurrency semaphore (~3-5) owned here, injected into the CC adapter.
  * config/ JSON hot-reload on SIGHUP (windows / routing / constraints).
  * --selftest runs the coalescer's verbatim self-test (CI gate).

Value-free mechanism only: NO secrets, NO deploy, NO prompt content.
"""
import hashlib, hmac, json, os, re, signal, subprocess, sys, threading, time, uuid, logging
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import site_env         # load config/site.env BEFORE any module reads env at import time
import coalescer as C
from coalescer import Coalescer
from echo_guard import EchoGuard, RunRegistry
from routing import Router, NON_TRIGGERING_EVENTS
import routing
from prompts import PromptLoader
from envelope import EnvelopeParser
from worktree import WorktreeManager
from adapters.cc import CCAdapter, scrub_worker_env
from trackers.vikunja import VikunjaTracker
from liveness import LivenessDriver, resolve_progress_timeout
import poster
import notify
import events          # signed outbound run events (EVENTS_WEBHOOK_URL; off when unset)
import ticket_fetch
import tickets
import gitlab_mr
import dash
import forge
import cd
import stream
import mention_handoff   # a human @dev mention assigns dev (MENTION_HANDOFF_ENABLED)
import zulip_wake        # Zulip DM wake and retired-agent auto-reply (ZULIP_WAKE_ENABLED / ZULIP_AUTOREPLY_ENABLED)

log = logging.getLogger("eph-broker.app")

PORT = int(os.getenv("PORT", "8787"))  # match the reverse proxy, systemd unit and CI (was 8080, a leftover from the old prototype)
# Loopback ONLY. A reverse proxy on the same host (IP-guarded to the webhook sender) is the sole front door; it
# reaches this via host loopback. Binding 0.0.0.0 bypasses that guard and exposes /event unauthenticated to the
# whole LAN (HMAC is defense-in-depth, not always set).
BIND_HOST = "127.0.0.1"
CONFIG_DIR = os.getenv("CONFIG_DIR", os.path.join(os.path.dirname(__file__), "config"))
PROMPTS_DIR = os.getenv("PROMPTS_DIR", os.path.join(os.path.dirname(__file__), "prompts"))
APPROVED_PROMPTS = os.getenv("APPROVED_PROMPTS", os.path.join(os.path.dirname(__file__), "approved-prompts.json"))
WORKTREES_ROOT = os.getenv("WORKTREES_ROOT", os.path.join(os.getenv("TMPDIR", "/tmp"), "eph-broker-worktrees"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "5"))                  # global cap (was 3)
# @fleet full-TFA launch: shell out to spawn.sh (real detached tmux TFA), NOT a claude -p
# worker. TFA_DEDUP_S collapses a create+update burst for one ticket into a SINGLE launch (the coalescer
# does this for ephemeral rows; a tfa row is launched outside it, so it needs its own small guard).
SPAWN_SH = os.getenv("SPAWN_SH", "")   # from site.env; unset means the TFA launch fails (logged)
TFA_DEDUP_S = int(os.getenv("TFA_DEDUP_S", "300"))   # Limitation: fixed TTL; a burst is seconds apart, re-assign after 5m re-spawns
RERUN_SEEN_TTL_S = 86400   # comment rerun: a (key, comment_id) is remembered this long (bounded dedup)
HMAC_SECRET_ENV = os.getenv("HMAC_SECRET_ENV", "BROKER_HMAC_SECRET")  # NAME only; the bytes are provisioned elsewhere
# Largest POST body read, in bytes. Content-Length is read before any auth check, so a bigger one gets a 413.
MAX_BODY_BYTES = int(os.getenv("BROKER_MAX_BODY", str(1024 * 1024)))
# Live transcript streaming (GET /stream/<key>), DEFAULT OFF. Env NAMES + defaults (read at call time in
# stream.py; full semantics in its docstring):
#   BROKER_STREAM_ENABLED  unset  ("1"/"true" = on; off = /stream/* 404 + no --session-id on worker argv)
#   BROKER_STREAM_GROUP    unset  (group allowed to stream; REQUIRED when on, unset = 403 fail-closed)
#   BROKER_STREAM_USER_HEADER / BROKER_STREAM_GROUPS_HEADER (proxy identity headers; REQUIRED when on, site.env)
#   BROKER_STREAM_MAX_S    600    (server-side stream close so EventSource reconnect re-runs forward_auth)
#   BROKER_STREAM_MAX      =CONCURRENCY (max concurrent streams; over cap = 503; tracks the cap)


# ---------------- HMAC check (value-free) ----------------
# BROKER_HMAC_MODE (read at CALL time, like stream.py) = off | log | enforce:
#   off      no verification, accept everything (explicit kill-switch / rollback).
#   log      verify when the secret is set, log one HMAC-CHECK line per event, ALWAYS accept. For rolling
#            signing out to an existing tap: set it explicitly, watch for PASS from every sender, then enforce.
#   enforce  DEFAULT. Reject 401 on a missing/bad signature. Secret unset under enforce = fail CLOSED (every
#            /event rejected and logged). The body names the event's author, so an unsigned /event would let
#            anyone who can reach it through the reverse proxy claim any author and pass the author gates.
# An unknown value falls back to `enforce` (a typo never opens the endpoint); the per-event line carries the
# EFFECTIVE mode and startup logs an ERROR, so a typo is visible.
# Grep contract (one line per POST /event):
#   HMAC-CHECK mode=<log|enforce> result=<PASS|FAIL-MISSING|FAIL-BAD|NOSECRET> action=<accept|reject>
#              src=<body.source> acct=<body.account|-> peer=<ip> xff=<X-Forwarded-For|-> ua=<User-Agent|->
# src/acct are identifiers only (never the body, never the header value). Counters: GET /health "hmac".
HMAC_MODE_ENV = "BROKER_HMAC_MODE"
HMAC_MODES = ("off", "log", "enforce")
HMAC_STATS = {"pass": 0, "fail_missing": 0, "fail_bad": 0, "nosecret": 0, "rejected": 0}
_HMAC_STATS_LOCK = threading.Lock()
_SAFE = re.compile(r"[^A-Za-z0-9._:/@,+-]")


def _hmac_secret():
    """Read the HMAC secret from its env var. Returns bytes or None (unconfigured)."""
    val = os.getenv(HMAC_SECRET_ENV)
    return val.encode() if val else None


def hmac_mode():
    """Effective mode. Unset or unknown gives 'enforce' (see block comment)."""
    m = (os.getenv(HMAC_MODE_ENV) or "enforce").strip().lower()
    return m if m in HMAC_MODES else "enforce"


def verify_hmac(body_bytes, header_hex):
    """Pure check: X-Broker-HMAC == hex HMAC-SHA256(body, secret), bare or with a `sha256=` prefix (the form
    the outbound signatures use). False if secret unset or header missing. Bytes compare, so a non-ASCII
    header can't raise inside compare_digest."""
    secret = _hmac_secret()
    if secret is None or not header_hex:
        return False
    expected = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest().encode()
    got = header_hex.strip().lower().removeprefix("sha256=")
    return hmac.compare_digest(expected, got.encode("utf-8", "replace"))


def _safe(v, n=80):
    return _SAFE.sub("_", str(v))[:n] if v not in (None, "") else "-"


def check_hmac(body_bytes, header_hex, peer=None, xff=None, ua=None):
    """Mode-aware gate for POST /event. Returns (accept, result) and logs the HMAC-CHECK line."""
    mode = hmac_mode()
    if mode == "off":
        return True, "OFF"
    if _hmac_secret() is None:
        result = "NOSECRET"
    elif not header_hex:
        result = "FAIL-MISSING"
    else:
        result = "PASS" if verify_hmac(body_bytes, header_hex) else "FAIL-BAD"
    accept = mode == "log" or result == "PASS"
    try:                                       # sender identifiers only, never the body or the sig
        b = json.loads(body_bytes or b"{}")
        src, acct = (b.get("source"), b.get("account")) if isinstance(b, dict) else (None, None)
    except (ValueError, TypeError, RecursionError):   # the body may be unauthenticated here
        src, acct = "unparseable", None
    with _HMAC_STATS_LOCK:
        HMAC_STATS[result.lower().replace("-", "_")] += 1
        if not accept:
            HMAC_STATS["rejected"] += 1
    (log.info if result == "PASS" else log.warning)(
        "HMAC-CHECK mode=%s result=%s action=%s src=%s acct=%s peer=%s xff=%s ua=%s",
        mode, result, "accept" if accept else "reject", _safe(src, 32), _safe(acct, 32),
        _safe(peer, 45), _safe(xff, 64), _safe(ua, 64))
    return accept, result


def log_hmac_startup():
    raw = os.getenv(HMAC_MODE_ENV)
    if raw is not None and raw.strip().lower() not in HMAC_MODES:
        log.error("%s=%s is not one of %s - falling back to 'enforce'", HMAC_MODE_ENV,
                  _safe(raw, 16), "|".join(HMAC_MODES))
    mode, have = hmac_mode(), _hmac_secret() is not None
    log.info("HMAC mode=%s secret=%s (%s)", mode, "set" if have else "unset", HMAC_SECRET_ENV)
    if mode == "enforce" and not have:
        log.error("HMAC mode=enforce but %s unset - EVERY POST /event will be rejected (fail-closed)",
                  HMAC_SECRET_ENV)


# ---------------- operator-only rerun control path (value-free) ----------------
# POST /rerun {"task_id": N, "note": "<optional html>"} re-arms a PARKED / spun-down subject (no live worker)
# so the normal spawn path runs. It's the operator-side twin of the human comment rerun, without faking a
# human comment. Caller identity = a dedicated token that ONLY the operator bot's env carries, sent as
# X-Broker-Rerun-Token and constant-time compared server-side. FAIL-CLOSED: token var unset/empty (the
# default, so the path is OFF until the token is minted) or header missing/wrong gives 401, nothing re-armed.
# NAME only here; the secret bytes are written elsewhere. Loopback bind is unchanged (no new surface).
# Audit: one OPS-RERUN line per accepted-auth request: key, caller, result, ts.
RERUN_TOKEN_ENV = os.getenv("RERUN_TOKEN_ENV", "BROKER_RERUN_TOKEN")   # NAME only; the bytes are provisioned elsewhere
RERUN_CALLER = os.getenv("RERUN_CALLER", "operator")   # the only holder of the token, hence the audited caller
RERUN_NOTE_MAX = 4000


def verify_rerun_token(header_val):
    """Fail-closed constant-time compare of X-Broker-Rerun-Token against the operator-only secret."""
    secret = os.getenv(RERUN_TOKEN_ENV)
    if not secret or not header_val:
        return False
    return hmac.compare_digest(secret.encode(), str(header_val).encode("utf-8", "replace"))


# ---------------- TFA liveness dedup helper ----------------
def _live_tfa_session(task_id) -> bool:
    """True if a live tmux TFA session already exists for this ticket. spawn.sh names cloud TFA
    sessions `tfa-<backend>-task-<id>` (with a `-<n>` collision suffix), so match on an exact
    `-task-<id>` boundary: `task-93` must NOT match `task-9315`. Module-level (not a method) so
    tests can monkeypatch it. Robust by design: a None id, an absent/erroring tmux, or no running
    server all read as 'no live session' (return False); this NEVER raises into the ingest thread.
    Uses plain `tmux list-sessions` (default socket), the same user and socket spawn.sh's `tmux
    new-session` writes to (the broker and spawn.sh both run as the broker user)."""
    if task_id is None:
        return False
    try:
        out = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                             capture_output=True, text=True, check=True).stdout
    except Exception:   # CalledProcessError (no server/non-zero) / FileNotFoundError (tmux absent) / anything:
        return False    # fail-OPEN to "no live session": NEVER raise into the ingest thread (worst case: a
                        # redundant spawn, the pre-fix behavior; the TTL check still collapses the real burst)
    pat = re.compile(rf'-task-{task_id}(-\d+)?$')
    return any(pat.search(name.strip()) for name in out.splitlines())


# ---------------- TFA self-assign ----------------
# the spawner echoes "leased pool bot pool-NN → <env>" (POOL lease) on stdout before it launches tmux, so
# the broker learns the TFA's pool handle from spawn.sh's own output, with no pool-registry coupling.
_POOL_LEASE_RE = re.compile(r"\b(?:leased|claimed) pool bot (pool-\d+)\b")
TFA_SPAWN_WAIT_S = 120   # spawn.sh exits in ~1s once tmux is up (tmux and the backgrounded clearer don't hold the pipe)


def _tfa_self_assign(proc, task_id, pool_ids, tracker=None):
    """Assign the ticket to the Vikunja user of the pool bot the TFA leased. Runs on a daemon thread
    (never the ingest thread): drains and reaps spawn.sh, parses the leased handle, maps it to a numeric
    Vikunja user id via config/pool-vikunja-ids.json (`{"pool-NN": <id>}`; the assignee tool takes ids
    only and the bridge has no user search), then assigns. Best-effort by design: no lease line, no mapped
    id (pool users not minted yet), or a failed assign all log and return False. NEVER raises; the launched
    TFA is never touched. Module-level so tests can call/monkeypatch it. `tracker` is the ticket's Tracker
    (the adapter's registry entry); None means a plain VikunjaTracker over poster.assign_user."""
    try:
        out, _ = proc.communicate(timeout=TFA_SPAWN_WAIT_S)   # drain to EOF: an early close would SIGPIPE spawn.sh
        if not task_id:
            return False
        if proc.returncode:   # leased, then failed to launch: don't pin the ticket on a bot that isn't working it
            log.warning("TFA self-assign: spawn.sh exited rc=%s -> skip (task=%s)", proc.returncode, task_id)
            return False
        m = _POOL_LEASE_RE.search(out or "")
        if not m:
            log.warning("TFA self-assign: no pool lease in spawn.sh output -> skip (task=%s)", task_id)
            return False
        handle, uid = m.group(1), (pool_ids or {}).get(m.group(1))
        if not uid:
            log.warning("TFA self-assign: no Vikunja id for %s in pool-vikunja-ids config -> skip (task=%s)",
                        handle, task_id)
            return False
        done = (tracker or VikunjaTracker()).assign(tickets.vikunja_ref(task_id), uid)
        log.info("TFA self-assign task=%s -> %s (vikunja id %s): %s", task_id, handle, uid, "ok" if done else "FAILED")
        return done
    except Exception as e:   # TimeoutExpired / a proc without communicate / anything: log, never raise
        log.warning("TFA self-assign failed task=%s: %s", task_id, e)
        return False


# ---------------- config hot-reload ----------------
class Config:
    """Hot-reloadable config from CONFIG_DIR/*.json and the broker.yaml sections below. SIGHUP re-reads. Value-free."""
    SECTIONS = ("constraints", "windows", "zulip-wake", "cd-tier", "deploy-paths", "pool-vikunja-ids")
    # read on every use, so a SIGHUP edit takes effect; every other broker.yaml section is read once at startup
    LIVE = ("windows", "zulip-wake", "cd-tier", "deploy-paths", "pool-vikunja-ids")

    def __init__(self, config_dir):
        self.config_dir = config_dir
        self.data = {}
        self.yaml = site_env.CONFIG
        self.reload()

    @staticmethod
    def windows_problem(win):
        """None when `win` is a usable windows mapping ({"initial"|"receive": {class: seconds}}), else why not."""
        if not isinstance(win, dict):
            return "not a mapping"
        for part in ("initial", "receive"):
            sub = win.get(part, {})
            if not isinstance(sub, dict):
                return f"{part} is not a mapping"
            for k, v in sub.items():
                try:
                    ok = not isinstance(v, bool) and 0 <= float(v) < float("inf")
                except (TypeError, ValueError):
                    ok = False
                if not ok:
                    return f"{part}.{k} = {v!r} is not a number of seconds"
        return None

    def reload(self):
        """Never raises. A file that can't be read or parsed, or a windows value of the wrong shape, keeps the
        last good contents of that file (logged), so a bad hand-edit plus SIGHUP can't take the broker down."""
        prev, loaded = self.data, {}
        try:
            for fn in sorted(os.listdir(self.config_dir)):
                if fn.endswith(".json") and not fn.endswith(".example.json"):   # examples are templates
                    try:
                        with open(os.path.join(self.config_dir, fn)) as f:
                            loaded[fn[:-5]] = json.load(f)
                    except (OSError, ValueError) as e:   # a hand-edit typo keeps the last good copy
                        if fn[:-5] in prev:
                            loaded[fn[:-5]] = prev[fn[:-5]]
                        log.error("config reload: %s unreadable, keeping the last good copy%s: %s", fn,
                                  "" if fn[:-5] in prev else " (none, so it's left out)", e)
        except OSError as e:
            log.warning("config reload: %s (using defaults)", e)
        # broker.yaml sections replace the same-named json file; a bad edit keeps the last good file contents
        try:
            doc = site_env.read_config()
        except site_env.BrokerConfigError as e:
            log.error("config reload: %s, keeping the previous broker.yaml", e)
        else:
            stale = sorted(k for k in set(doc) | set(site_env.CONFIG)
                           if k not in self.LIVE and doc.get(k) != site_env.CONFIG.get(k))
            if stale:
                log.warning("config reload: broker.yaml %s changed; those apply at the next restart", stale)
            self.yaml = doc
        for name in self.SECTIONS:
            sec = site_env.section(name, self.yaml)
            if sec is not None:
                if name in loaded:
                    log.warning("config reload: broker.yaml sets %s, %s.json is ignored", name, name)
                loaded[name] = sec
        win = loaded.get("windows", {})
        why = self.windows_problem(win)
        if why:
            win = prev.get("windows", {})
            log.error("config reload: windows %s, keeping the last good windows", why)
            if win:
                loaded["windows"] = win
            else:
                loaded.pop("windows", None)
        self.data = loaded
        # apply window overrides live onto the coalescer module globals (same shape as env override)
        C.INITIAL_WINDOWS.update({k: float(v) for k, v in win.get("initial", {}).items()})
        C.RECEIVE_WINDOWS.update({k: float(v) for k, v in win.get("receive", {}).items()})
        log.info("config reloaded: %s", list(loaded))
        return self.data


# ---------------- broker wiring ----------------
class Broker:
    def __init__(self):
        self.config = Config(CONFIG_DIR)
        self.run_registry = RunRegistry()
        self.echo_guard = EchoGuard(self.run_registry, classify=lambda k, e: self.router.classify(k, e))
        self.semaphore = threading.BoundedSemaphore(CONCURRENCY)   # global concurrency cap, owned here
        self.rejected = 0                     # count of ingest allowlist rejects
        self.route_stamps = {}                # subject_key to row-domain, stamped at ingest
        self._tfa_launched = {}               # @fleet tfa-row launch dedup: subject_key to last-launch ts
        # A human @dev mention on a non-dev ticket assigns dev (the handoff then spawns). Flag-gated.
        self.mention_handoff = mention_handoff.MentionHandoff(assign=lambda tid, uid: self._assign(tid, uid))
        # `live` lets classify() forward a human comment ONLY into a worker live on that subject.
        self.router = Router(stamps=self.route_stamps, live=lambda k: self.coalescer.has_live_worker(k))
        constraints = self.config.data.get("constraints", {})
        # One broker-managed zulip-mcp.py --channel listener per stood_down bot, feeding self.ingest
        # in-process. Built unconditionally (cheap, starts nothing); main() calls start(), a no-op with both
        # flags 0. The bot map hot-reloads with config/ (SIGHUP); rate-limit state sits beside completed.json.
        self.zulip_wake = zulip_wake.ZulipWake(
            ingest=lambda evt: self.ingest(evt, internal=True),   # the ONLY internal=True caller (see ingest)
            wake_map=lambda: self.config.data.get("zulip-wake") or {},
            state_dir=os.getenv("ZULIP_STATE_DIR") or (os.path.dirname(C.COMPLETED_PATH) if C.COMPLETED_PATH
                                                       else None))
        self.adapter = CCAdapter(
            router=self.router,
            # SEAM: this WorktreeManager is single-domain (base_repo pinned to the
            # ansible read-prover repo, worktrees_root=WORKTREES_ROOT default $TMPDIR/eph-broker-worktrees).
            # Worktree creation will need to be ROUTE-AWARE for the wiki write worker: for a
            # capability="write" wiki route it must cut the worktree off route.publish["wiki_repo_path"] @
            # route.publish["publish_ref"] under route.publish["worktree_base"], set core.hooksPath=/dev/null
            # in that worktree, and set the worker cwd to <worktree>/route.publish["content_subdir"] so
            # acceptEdits confines edits to the content tree (see routing._WIKI_DOCS_ROW seam note). For now,
            # routing only EXPOSES those fields on the Route; it does not implement the cut/suppress/push here.
            worktree_mgr=WorktreeManager(
                base_repo=os.getenv("ANSIBLE_REPO", ""),   # from site.env
                worktrees_root=WORKTREES_ROOT),
            prompt_loader=PromptLoader(PROMPTS_DIR, APPROVED_PROMPTS),
            # WIRE THE LOUD SINKS. Every escalate() (envelope.py) must reach the operator, not just the
            # journal: an unparseable-envelope turn is exactly how the broker rotted silently. wake_ops DMs the operator
            # over the bus (notify.py; value-free, the bus client owns auth). escalate() now receives the
            # numeric task_id (parsed from the subject key by the CC adapter), so ticket_comment is ALSO
            # wired: the escalation reason is posted onto the originating Vikunja ticket via
            # poster.post_comment (value-free; mcp.sh owns auth; called only when a task_id is present, and
            # it swallows every failure so a lost comment can never wedge the worker).
            envelope_parser=EnvelopeParser(wake_ops=notify.wake_ops,
                                           ticket_comment=poster.post_comment),
            run_registry=self.run_registry,
            semaphore=self.semaphore,
            # pinned_cli plumbing removed: the CLI-version pin is no longer enforced.
            max_turns=int(constraints.get("max_turns", 15)),
            # Per-capability --max-turns override (e.g. code=40), else the global max_turns.
            max_turns_by_capability={k: int(v) for k, v in
                                     (constraints.get("max_turns_by_capability") or {}).items()},
            wall_clock_s=int(constraints.get("wall_clock_s", 600)),
            # Per-capability wall-clock override (e.g. code=1200), else the global wall_clock_s.
            wall_clock_by_capability={k: int(v) for k, v in
                                      (constraints.get("wall_clock_by_capability") or {}).items()},
            max_forwards=int(constraints.get("max_forwards", 10)),
            rotate_turns=int(constraints.get("rotate_turns", 20)),
            rotate_tokens=int(constraints.get("rotate_tokens", 120_000)),
            # The broker (not the charter-free worker) posts the envelope's `summary`
            # HTML back onto the originating ticket via mcp.sh vikunja-bridge (poster.py owns no secrets).
            comment_poster=poster.post_comment,
            # The broker (not the charter-free worker) fetches the ticket's rendered content at
            # spawn and injects it into the brief via mcp.sh vikunja-bridge (ticket_fetch.py owns no secrets).
            ticket_fetcher=ticket_fetch.fetch_ticket,
            # MR-reviewer: after a code worker's MR opens, the broker RE-INGESTS a synthetic
            # mr.opened event in-process (Broker.ingest runs echo_guard, classify and normalize), which spawns the
            # PURE-READ reviewer. And the broker posts the reviewer's verdict as an MR note (gitlab_mr owns
            # no secrets; the token is read by NAME at runtime, skipped gracefully if unset).
            emit_event=self.ingest,
            mr_note_poster=gitlab_mr.post_mr_note,
            # A blocked worker turn is parked on the operator: apply the parked label (PARKED_LABEL_ID) alongside the
            # existing blocked ticket comment
            # (poster.py owns no secrets; mcp.sh owns auth).
            label_poster=poster.apply_label,
            # WIRE THE LOUD SINK. The CCAdapter's alert_ops fires on HARD-KILL (wall-clock), on
            # max-forwards, and on every spawn-time fail-closed reject; it is journal-only by default, so a
            # degraded worker was invisible. Route it to the operator DM (notify.py; value-free, the bus client owns auth).
            alert_ops=notify.wake_ops,
            # Flush a worker's suppressed-escalation tail at its exit, so a flapping-then-
            # quiet subject still DMs the operator its final 'N suppressed' count (else it's lost to the log alone).
            flush_ops=notify.flush_subject,
            # A row's publish["mr_notify_handle"] gets the MR link on open, verbatim.
            agent_dm=notify.dm,
            # Ladder-parked subjects survive restarts beside completed.json ("" means in-memory).
            repo_parked_path=(os.path.join(os.path.dirname(C.COMPLETED_PATH), "repo_parked.json")
                              if C.COMPLETED_PATH else None),
            # Continuous delivery: after a reviewer PASS, auto-merge Skip/Light-tier worker MRs (Full tier goes to an operator DM).
            # cd.on_review_verdict is a no-op unless CD_ENABLED; tier patterns hot-reload from cd-tier.json.
            on_mr_verdict=lambda mr, env, run_id: cd.on_review_verdict(
                mr, env, run_id, comment=poster.post_comment, dm_ops=notify.wake_ops,
                tier_cfg=self.config.data.get("cd-tier")),
            # A zulip chat key carries the BOT, not the account, so the spawn gate asks the bot map; the
            # chat worker has no bus identity, so the broker DMs its reply to the sender as the DMed bot.
            zulip_account=self.zulip_wake.account_for,
            chat_reply=self.zulip_wake.reply,
            # An account-less chat row: its spawn gate asks the bot map for the row itself.
            zulip_row_admits=self.zulip_wake.row_admits,
            # A COLD chat start gets the thread's last N DMs (bot map "history": N; default off).
            zulip_history=self.zulip_wake.history,
            # AUTO_MERGE_REPOS (default empty = off): merge a listed repo's worker MR once its pipeline passes.
            on_mr_opened=lambda opened, branch, tid, run_id: cd.on_mr_opened(
                opened, branch, tid, run_id, comment=poster.post_comment, dm_ops=notify.wake_ops),
        )
        self._rerun_seen = {}                 # (key, comment_id) to ts used for a rerun (TTL-pruned)
        self._rerun_lock = threading.Lock()
        # on_close prunes the route_stamps entry when a subject's lifecycle ends, so the map no longer leaks
        # one entry per distinct subject_key forever (see _prune_stamp).
        self.coalescer = Coalescer(adapter=self.adapter,   # binds adapter.set_coalescer for state advertising
                                   on_close=self._prune_stamp,
                                   completed_path=C.COMPLETED_PATH,   # dedup survives restarts
                                   row_of=self.route_stamps.get,      # ROW_CONCURRENCY (default: no cap)
                                   emit=events.emit)                  # run.started / run.finished
        # Broker-owned island-liveness driver (couples the handshake to the worker turn-lifecycle +
        # force-closes wedged workers). Built here; started in main().
        self.liveness = LivenessDriver(
            self.coalescer, self.adapter,
            # WIRE THE LOUD SINK. Force-closing a wedged worker is a defect someone must hear
            # about, so route the driver's alert_ops to the operator DM (notify.py; value-free, the bus client owns auth).
            alert_ops=notify.wake_ops,
            # The progress timeout must stay ABOVE the turn hard-kill (resolve_progress_timeout floors it), else a wall_clock
            # raise leaves it below the hard-kill and force-closes healthy long turns.
            progress_timeout=resolve_progress_timeout(
                constraints.get("progress_timeout_s", 900),
                constraints.get("wall_clock_s", 600),
                constraints.get("wall_clock_by_capability")),
            period=C.TICK,
        )

    def ingest(self, evt, internal=False):
        """HMAC already checked at the Handler. Pipeline: echo-guard, then the allowlist reject and
        route-stamp, then coalescer.on_event. echo-guard and classify run BEFORE on_event so the coalescer's island
        (the lock + state handoff in on_event/tick) stays verbatim, EXCEPT one FORWARD_ONLY
        branch inside on_event's locked block (the race-safe comment liveness re-check; `state` is still
        the sole sync token).

        A zulip-source event is admitted ONLY with internal=True, i.e. from
        zulip_wake after its guards ran (1:1 human DM, allowlisted sender, flags on). Every other caller, whether POST
        /event, /webhooks/gitlab, the MR re-ingest (emit_event), is refused here, so a signed-but-crafted body can
        never skip those guards. Later paths (forward / rerun / re-arm buffers) only replay events that already
        passed this gate."""
        if not internal and _is_zulip_event(evt):
            self.rejected += 1
            log.warning("INGEST REJECT (zulip): source=zulip is internal-only (zulip_wake) -> dropped (total=%d)",
                        self.rejected)
            return None
        key = C.normalize(evt)
        evt.pop("ref", None)                  # the ref is derived from the key, never taken from the body
        ref = tickets.ref_from_key(key)
        if ref is not None:
            evt["ref"] = ref
        kept = self.echo_guard.filter(evt)
        if kept is None:
            return None                       # dropped worker echo
        self._repo_parked_prune(key, kept)
        row = self.router.classify(key, kept)  # decide the row on the RAW event (signal present)
        if row is None:
            if self._repo_comment_rerun(key, kept):
                return None                   # human comment on a ladder-parked ticket: rerun
            if self._comment_respawn(key, kept):
                return None                   # human comment on a spun-down ticket: respawn
            if mention_handoff.enabled() and self.mention_handoff.maybe(key, kept) is not None:
                return None                   # handed off (dev assigned) instead of dropped
            self.rejected += 1
            log.info("INGEST REJECT (allowlist): key=%s is not a known row -> dropped (total=%d)",
                     key, self.rejected)
            return None                       # allowlist reject: only known rows are admitted
        # @fleet: the "tfa" domain is a FULL interactive TFA, launched OUTSIDE the coalescer:
        # the broker shells out to spawn.sh (real tmux agent, attachable in-pane), never a claude -p worker.
        # NB: classify() returns the DOMAIN STRING (not a Route), so compare the string.
        if row == "tfa":
            return self._launch_tfa(key, kept)
        # Stamp at ingest, threaded through to route(). NEVER for a forward-only comment:
        # it only REUSES an existing stamp; re-writing it could resurrect the stamp of a subject that closed
        # between classify and on_event (on_close popped it), and a stale stamp re-admits later signal-less
        # events through DEDUP-CLEAR and a spawn.
        if kept.get("event_name") not in NON_TRIGGERING_EVENTS:
            self.route_stamps[key] = row
        return self.coalescer.on_event(kept)

    def _prune_stamp(self, key):
        """on_close: drop the route stamp of a subject whose worker just closed. The close pops live[key] and then
        calls this outside the coalescer lock, so a concurrent ingest may have stamped the key again and armed a
        new spawn in between. Keep the stamp then (the new spawn needs it for its row and the row cap)."""
        with self.coalescer.lock:
            if key not in self.coalescer.live and key not in self.coalescer.pending:
                self.route_stamps.pop(key, None)

    def _ticket(self, ref):
        """The fetched Ticket for a ref through the adapter's tracker registry, or None (fail closed)."""
        t = self.adapter.tracker(ref)
        return t.fetch(ref) if t is not None else None

    def _assign(self, task_id, user_id):
        """Assign a Vikunja task (by id) to a user through the adapter's tracker registry. False when none."""
        ref = tickets.vikunja_ref(task_id)
        t = self.adapter.tracker(ref)
        return t.assign(ref, user_id) if t is not None else False

    def _repo_comment_rerun(self, key, evt):
        """With REPO_INFER_COMMENT_RERUN_ENABLED and REPO_INFER_ENABLED on (default OFF), a HUMAN comment
        on a ticket the repo ladder parked (no live worker, so classify() rejected it) re-arms ONE normal spawn;
        the worker's ladder then reads the ticket PLUS this comment. Human-only via the same gate as a live
        forward (provenance user, no run_id / broker-run marker), then _rerun_allowed (still dev's + open, and
        the commenter is the operator or the ticket creator); once per comment_id. The marker stays set
        until a ladder run resolves (a re-park keeps it), so any error here leaves the ticket parked."""
        try:
            if not (routing.repo_infer_comment_rerun_enabled() and routing.repo_infer_enabled()) \
                    or evt.get("event_name") not in NON_TRIGGERING_EVENTS \
                    or not routing.comment_forward_admissible(key, evt, lambda k: True):
                return False
            domain = self.adapter.repo_parked(key)
            if domain is None:
                return False
            seen, now = (key, evt.get("comment_id")), time.time()
            with self._rerun_lock:
                self._rerun_seen = {k: ts for k, ts in self._rerun_seen.items() if now - ts < RERUN_SEEN_TTL_S}
                if evt.get("comment_id") is not None and seen in self._rerun_seen:
                    return False
                self._rerun_seen[seen] = now
            if not self._rerun_allowed(key, evt):
                with self._rerun_lock:
                    self._rerun_seen.pop(seen, None)
                return False
            self.adapter.queue_repo_rerun(key, str(evt.get("comment") or ""))
            self.route_stamps[key] = domain
            log.info("REPO-RERUN key=%s: human comment (%s) on a ladder-parked ticket -> re-arming", key,
                     evt.get("doer"))
            self.coalescer.on_event({**evt, "event_name": "task.comment.rerun"})
            return True
        except Exception as e:
            log.warning("repo comment rerun failed key=%s: %s (left parked)", key, e)
            return False

    def _rerun_allowed(self, key, evt):
        """Enable-time review (comment rerun): at rerun time the ticket must still be open and assigned to
        dev (else the park is forgotten), and the commenter must be the operator (REPO_INFER_RERUN_USERS) or
        the ticket's creator. Anything unreadable fails closed (marker kept, no rerun)."""
        ticket = self._ticket(tickets.ref_from_key(key))
        assignees, creator = (ticket.assignees, ticket.creator) if ticket is not None else (None, None)
        if (ticket is not None and ticket.done) or \
                (assignees is not None and routing.DEV_ASSIGNEE.lower() not in assignees):
            self.adapter._set_repo_parked(key, None)
            log.info("REPO-RERUN key=%s: ticket is done or no longer assigned to %s -> park forgotten, no rerun",
                     key, routing.DEV_ASSIGNEE)
            return False
        doer = str(evt.get("doer") or "").strip().lower()
        if assignees is None or not doer or (doer != creator and doer not in routing.repo_infer_rerun_users()):
            log.info("REPO-RERUN key=%s: commenter %s is not the operator or the ticket creator (%s) -> no rerun",
                     key, evt.get("doer"), creator or "unknown")
            return False
        return True

    def _comment_respawn(self, key, evt):
        """Generalizes the repo-parked-only comment rerun (above) to EVERY
        row that routes by ASSIGNEE (dev gives code, eph gives general) or an "eph:"-prefixed LABEL
        (eph:code-change/eph:general); see routing.comment_respawn_domains. classify() already rejected
        this event (row is None: no live worker, so comment_forward_admissible's OWN liveness check
        failed). A HUMAN comment on such an OPEN ticket re-spawns the SAME row it routes to, fresh off a
        ticket fetch (the comment payload itself carries no assignees/labels to classify on). The queued
        comment feeds the next spawn's ticket text (adapter.queue_comment_respawn), so the worker sees the
        thread that woke it.

        Guards (all fail-closed): human-only / no-self-retrigger provenance, reusing
        comment_forward_admissible with is_live stubbed True (this path exists precisely BECAUSE no worker
        is live, the exact case that predicate's own liveness check would otherwise reject); once per
        comment_id (shares the _rerun_seen TTL map: same dedup notion, one store); the ticket must
        still be OPEN and still classify to the SAME domain at respawn time, and the commenter must be the
        operator (REPO_INFER_RERUN_USERS, reused) or the ticket's creator (_comment_respawn_allowed).
        Default ON; COMMENT_RESPAWN_ENABLED=0 rolls it back. Never raises: any failure here just leaves the
        comment dropped, exactly as it was before this path existed."""
        try:
            if not routing.comment_respawn_enabled() \
                    or evt.get("event_name") not in NON_TRIGGERING_EVENTS \
                    or not routing.comment_forward_admissible(key, evt, lambda k: True):
                return False
            ticket = self._ticket(tickets.ref_from_key(key))
            if ticket is None:
                return False                   # unreadable ticket: fail closed, no spawn
            if ticket.creator is None:
                return False                   # unreadable/untrusted ownership: fail closed
            domain = routing.comment_respawn_domain(ticket.assignees, ticket.labels)
            if domain is None:
                return False                   # this ticket is not one of the eligible rows: plain reject
            seen, now = (key, evt.get("comment_id")), time.time()
            with self._rerun_lock:
                self._rerun_seen = {k: ts for k, ts in self._rerun_seen.items() if now - ts < RERUN_SEEN_TTL_S}
                if evt.get("comment_id") is not None and seen in self._rerun_seen:
                    return False
                self._rerun_seen[seen] = now
            if not self._comment_respawn_allowed(evt, ticket, domain):
                with self._rerun_lock:
                    self._rerun_seen.pop(seen, None)
                return False
            self.adapter.queue_comment_respawn(key, str(evt.get("comment") or ""), evt.get("doer"))
            self.route_stamps[key] = domain
            log.info("COMMENT-RESPAWN key=%s domain=%s: human comment (%s) on a spun-down ticket -> re-spawning",
                     key, domain, evt.get("doer"))
            self.coalescer.on_event({**evt, "event_name": "task.comment.rerun"})
            return True
        except Exception as e:
            log.warning("comment respawn failed key=%s: %s (dropped)", key, e)
            return False

    def _comment_respawn_allowed(self, evt, ticket, domain):
        """Enable-time re-check (generalizes _rerun_allowed beyond the repo-parked/dev-only case): at
        respawn time the ticket must still be OPEN and still classify to the SAME domain it did a moment
        ago (else it was reassigned, relabelled, or closed and must not respawn), and the
        commenter must be the operator (REPO_INFER_RERUN_USERS) or the ticket's creator. Anything
        unreadable fails closed (no rerun)."""
        if ticket is None or ticket.creator is None:
            return False                       # untrusted blob: fail closed
        assignees, creator, labels = ticket.assignees, ticket.creator, ticket.labels
        if ticket.done or routing.comment_respawn_domain(assignees, labels) != domain:
            return False
        doer = str(evt.get("doer") or "").strip().lower()
        if not doer or (doer != creator and doer not in routing.repo_infer_rerun_users()):
            return False
        return True

    def ops_rerun(self, task_id, note=""):
        """Operator-only re-arm of a parked / spun-down subject (caller auth is checked by the Handler BEFORE
        this runs). Returns (http_code, body). Never double-spawns: a live OR pending worker on the subject returns
        409, nothing armed (and coalescer.on_event would forward/collect a racing one anyway, never spawn a
        second). The ticket must still be open and either repo-parked (marker set) or route to an eligible
        comment-respawn row. `note` (e.g. the operator's resolution of the worker's ASK) feeds the next spawn
        exactly like the triggering comment does on the human paths. Audited: one OPS-RERUN line."""
        def audit(result, key="-", domain="-"):
            log.info("OPS-RERUN key=%s caller=%s domain=%s result=%s ts=%d", key, RERUN_CALLER, domain, result,
                     int(time.time()))
        try:
            tid = int(task_id)
        except (TypeError, ValueError):
            audit("bad-task-id")
            return 400, {"ok": False, "error": "task_id must be an integer"}
        key = f"vikunja:task:{tid}"
        note = str(note or "")[:RERUN_NOTE_MAX]
        with self.coalescer.lock:
            busy = key in self.coalescer.live or key in self.coalescer.pending
        if busy:
            audit("refused-live-worker", key)
            return 409, {"ok": False, "error": "a worker is live or pending on this subject (no double-spawn)"}
        ticket = self._ticket(tickets.ref_from_key(key))
        if ticket is None or ticket.creator is None:
            audit("refused-ticket-unreadable", key)
            return 409, {"ok": False, "error": "ticket unreadable (fail-closed)"}
        if ticket.done:
            audit("refused-ticket-done", key)
            return 409, {"ok": False, "error": "ticket is done"}
        parked = self.adapter.repo_parked(key)
        domain = parked or routing.comment_respawn_domain(ticket.assignees, ticket.labels)
        if domain is None:
            audit("refused-not-eligible", key)
            return 409, {"ok": False, "error": "ticket is not parked and routes to no respawn-eligible row"}
        if parked:
            if note:
                self.adapter.queue_repo_rerun(key, note)
        else:
            self.adapter.queue_comment_respawn(key, note, RERUN_CALLER)
        with self.coalescer.lock:             # an operator rerun IS a re-engagement: clear a finished run's dedup stamp
            if key not in self.coalescer.live and self.coalescer.completed.pop(key, None) is not None:
                self.coalescer._save_completed()
        self.route_stamps[key] = domain
        self.coalescer.on_event({"source": "vikunja", "event_name": "task.comment.rerun", "task_id": tid,
                                 "doer": RERUN_CALLER, "comment": note})
        audit("re-armed", key, domain)
        return 200, {"ok": True, "subject_key": key, "domain": domain, "parked": bool(parked)}

    def _repo_parked_prune(self, key, evt):
        """A ladder park is forgotten once its ticket is unassigned from dev or closed (seen on a Vikunja task
        event; comments carry no assignees). Never raises."""
        try:
            if evt.get("source") != "vikunja" or evt.get("event_name") in NON_TRIGGERING_EVENTS \
                    or self.adapter.repo_parked(key) is None:
                return
            names = evt.get("assignees") or []
            if isinstance(names, str):
                names = [names]
            if evt.get("done") is True or routing.DEV_ASSIGNEE.lower() not in [str(a).strip().lower() for a in names]:
                self.adapter._set_repo_parked(key, None)
                log.info("REPO-PARK key=%s: %s -> park forgotten", key,
                         "ticket done" if evt.get("done") is True else f"no longer assigned to {routing.DEV_ASSIGNEE}")
        except Exception as e:
            log.warning("repo-parked prune failed key=%s: %s", key, e)

    def _launch_tfa(self, key, evt):
        """@fleet: launch a REAL interactive temp fleet agent via spawn.sh (POOL bot + detached tmux, own bus
        identity, attachable via `tmux attach`, lives until spun down) seeded with the ticket. Fire-and-forget:
        NOT a coalescer worker, no single-turn envelope. Dedups a create+update burst via a short TTL."""
        now = time.time()
        # Parse the numeric ticket id EARLY: both the tmux-liveness guard below and the ticket fetch need it.
        # NOTE: coerce to int. The key is a STRING ("vikunja:task:42"), so `key.split(":")[-1]` yields a
        # string, and vikunja_task_crud's `id` is schema-typed `number`, so passing "42" gets rejected by
        # MCP input validation (-32602 "Expected number, received string"). That rejection arrived as a
        # normal JSON-RPC result whose content[0].text was the error message, so _extract_text returned it
        # and the broker injected the ERROR TEXT as if it were the ticket body, so every TFA got an MCP error
        # where its ticket should be (seen live). Coerce at the source.
        task_id = tickets.vikunja_id(key)
        if task_id is None and key.startswith("vikunja:task:"):
            log.warning("TFA launch: non-numeric task id in key=%s -> ticket body not fetched", key)
        # TTL dedup: collapses the immediate create+update startup-race burst (the tmux session hasn't
        # appeared yet within the first couple seconds after Popen, so the liveness guard below can't see it).
        if now - self._tfa_launched.get(key, 0) < TFA_DEDUP_S:
            log.info("TFA dedup: key=%s launched recently -> skip", key)
            return key
        # Limitation: tmux ls is the liveness source of truth; TTL above still collapses the sub-second startup-race burst.
        # Catches the >TTL re-event (re-assign / later update comment) that the fixed-TTL check alone lets respawn a
        # 2nd TFA onto a ticket whose FIRST TFA is still in-flight (seen live: task-42 got a "task-42-2").
        if task_id and _live_tfa_session(task_id):
            log.info("TFA dupe-guard: live session for task=%s -> skip", task_id)
            return key
        self._tfa_launched[key] = now
        # pull the tfa row TEMPLATE (repo/backend) straight from the router, with no with_model()/fleet-model.sh
        # (a tfa row has no CCAdapter model; its model/effort come from spawn.sh's own registry).
        tmpl = next((t for _ns, _rp, _cp, t in self.router.rows if t.domain == "tfa"), None)
        base = (tmpl.repo if tmpl else os.path.expanduser("~/.cache/eph-tfa"))
        backend = getattr(tmpl, "backend", "") if tmpl else ""
        # per-ticket cwd so spawn.sh names the tmux session tfa-<backend>-task-<id> (identifiable when several
        # @fleet TFAs run at once); spawn.sh mkdir -p's it. A full TFA cd's out to any repo the ticket needs.
        cwd = os.path.join(base, f"task-{task_id}") if task_id else base
        brief = ""
        ref = tickets.vikunja_ref(task_id)
        tracker = self.adapter.tracker(ref)
        if task_id:
            try:
                ticket = tracker.fetch(ref) if tracker is not None else None
                brief = (ticket.text if ticket is not None else None) or ""
            except Exception as e:
                log.warning("TFA ticket fetch failed key=%s: %s", key, e)
            if not brief.strip():
                # An empty brief is how the fetch rotted unnoticed: it silently degraded to "" (or worse,
                # to error text that read like a ticket body) and the worker had no way to tell. Say so.
                brief = (f"(ticket body unavailable - the fetch failed. Read it yourself:\n"
                         f"  mcp.sh vikunja-bridge tools/call "
                         f"'{{\"name\":\"vikunja_task_crud\",\"arguments\":{{\"operation\":\"get\","
                         f"\"id\":{task_id}}}}}')")
        kickoff = (
            f"You are a temporary fleet agent (TFA) spawned by the eph-broker to work Vikunja ticket #{task_id}. "
            f"You are a FULL fleet agent: pooled bus identity, your user-level rules loaded (~/.claude/CLAUDE.md), "
            f"running on the {backend} backend. The operator can attach to your tmux session and interact with you directly.\n\n"
            f"=== TICKET #{task_id} ===\n{brief}\n\n"
            f"Work this ticket to completion. Coordinate with ops"
            f"{f' (zbus dm {notify.OPS_HANDLE})' if notify.OPS_HANDLE else ''} for any "
            f"decision, gate, or blocker. When done or parked, post a clear result comment on the ticket and "
            f"DM ops a one-line status. Follow your site's ticket rules; never close the ticket yourself: "
            f"recommend closure to the operator.\n\n"
            f"Ticket-tracking: you run under a leased pool bus identity (pool-NN; `zbus whoami`). The broker "
            f"assigns this ticket to your matching pool-NN Vikunja user for you, best-effort - if it isn't "
            f"assigned to you (e.g. that Vikunja user isn't minted yet), don't block on it: keep working the ticket."
        )
        try:
            # SCRUB the TFA launch env: do NOT inherit the broker's full os.environ.
            # An unscrubbed Popen leaked GITLAB_MR_TOKEN (and BROKER_HMAC_SECRET / any *TOKEN*/*SECRET*/*KEY*)
            # into the TFA, which on the deepseek backend runs off-Anthropic on untrusted ticket text, an
            # exfil risk. spawn.sh sources the backend auth ITSELF (EXTRA_ENV, from the backend's env file) and leases a
            # FRESH pool bus identity, so the TFA needs none of the broker's secrets, only
            # the allowlist (PATH, HOME, locale, via scrub_worker_env) plus the spawn-registry pointer if the
            # broker overrides it. Same allowlist mechanism the CC worker spawns already use.
            tfa_passthrough = {k: os.environ[k]
                               for k in ("SPAWN_MODELS_JSON", "VLLM_ENV", "SPAWN_SH")
                               if k in os.environ}
            tfa_env = scrub_worker_env(os.environ, route_env=tfa_passthrough)
            # detached: spawn.sh backgrounds a tmux session and returns; don't block the ingest thread.
            # stdout is PIPEd so _tfa_self_assign can read the leased pool handle off it.
            proc = subprocess.Popen([SPAWN_SH, *([f"--{backend}"] if backend else []), cwd, kickoff], env=tfa_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    encoding="utf-8", errors="replace", start_new_session=True)
            log.info("TFA LAUNCH key=%s backend=%s cwd=%s task=%s", key, backend, cwd, task_id)
        except Exception as e:
            log.exception("TFA launch failed key=%s: %s", key, e)
            return key
        try:                                   # self-assign off-thread; a failure here never touches the launch
            threading.Thread(target=_tfa_self_assign, daemon=True,
                             args=(proc, task_id, self.config.data.get("pool-vikunja-ids", {}), tracker)).start()
        except Exception as e:
            log.warning("TFA self-assign thread failed to start key=%s: %s", key, e)
        if task_id and tracker is not None:   # best-effort ticket comment (a lost post never blocks the launch)
            try:
                tracker.comment(
                    ref,
                    f"<p>🤖 Spawned a <b>temporary fleet agent</b> (backend: {backend}) to work this ticket. "
                    f"It runs in a detached tmux session on the broker host with its own bus identity, attachable in-pane, "
                    f"lives until spun down. It'll comment its result here when done.</p>",
                    uuid.uuid4().hex)
            except Exception as e:
                log.warning("TFA launch comment post failed key=%s: %s", key, e)
        return key


def _is_zulip_event(evt):
    """Does this event claim the zulip source or land in the zulip:* key namespace? Case/space-insensitive
    on source, plus the normalized key (an odd source like 'zulip ' normalizes to 'zulip :raw')."""
    if not isinstance(evt, dict):
        return False
    return str(evt.get("source") or "").strip().lower() == "zulip" or C.normalize(evt).lower().startswith("zulip")


BROKER = None   # set in main()


class Handler(BaseHTTPRequestHandler):
    timeout = 30   # socket timeout, so an idle or slow client can't hold a handler thread forever

    def _body(self):
        """The raw POST body, or None once a 400 (bad Content-Length) or 413 (over MAX_BODY_BYTES) has been
        sent. No Content-Length reads nothing. Runs before any auth check, so it trusts nothing."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            self._reply(400, {"error": "bad Content-Length"})
            return None
        if n > MAX_BODY_BYTES:
            self._reply(413, {"error": f"body over {MAX_BODY_BYTES} bytes"})
            return None
        return self.rfile.read(n)

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/webhooks/gitlab":
            return self._forge_gitlab()
        if path == "/forge/gitlab":
            # Deprecated alias, kept for one release so an existing webhook or forwarder keeps working.
            log.warning("DEPRECATED POST /forge/gitlab: point the webhook at /webhooks/gitlab, the old path goes away "
                        "in the next release")
            return self._forge_gitlab()
        if path == "/rerun":
            return self._rerun()
        if path != "/event":
            return self._reply(404, {"error": "POST /event, /webhooks/gitlab or /rerun only"})
        raw = self._body()
        if raw is None:
            return
        accept, _ = check_hmac(raw, self.headers.get("X-Broker-HMAC"), peer=self.client_address[0],
                               xff=self.headers.get("X-Forwarded-For"), ua=self.headers.get("User-Agent"))
        if not accept:
            return self._reply(401, {"error": "bad or missing X-Broker-HMAC"})
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, TypeError) as e:
            return self._reply(400, {"error": f"bad json: {e}"})
        if not isinstance(body, dict):
            return self._reply(400, {"error": "body must be a JSON object"})
        if _is_zulip_event(body):             # zulip events are internal-only (zulip_wake), never HTTP
            log.warning("POST /event REJECT: source=zulip is not accepted over HTTP")
            return self._reply(403, {"error": "source=zulip is internal-only"})
        key = BROKER.ingest(body)
        self._reply(200, {"ok": True, "subject_key": key, "dropped": key is None})

    def _rerun(self):
        """Operator-only POST /rerun (see the RERUN_TOKEN_ENV block). Auth first, fail-closed."""
        raw = self._body()
        if raw is None:
            return
        if not verify_rerun_token(self.headers.get("X-Broker-Rerun-Token")):
            log.warning("OPS-RERUN REJECT: bad or missing X-Broker-Rerun-Token peer=%s ts=%d",
                        _safe(self.client_address[0], 45), int(time.time()))
            return self._reply(401, {"error": "bad or missing X-Broker-Rerun-Token"})
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, TypeError) as e:
            return self._reply(400, {"error": f"bad json: {e}"})
        if not isinstance(body, dict):
            return self._reply(400, {"error": "body must be a JSON object"})
        code, out = BROKER.ops_rerun(body.get("task_id"), body.get("note"))
        self._reply(code, out)

    def _forge_gitlab(self):
        """Raw GitLab Issue Hook, through forge.normalize_gitlab_issue, into BROKER.ingest.
        Auth is X-Gitlab-Token (fail-closed, see forge.py), NOT X-Broker-HMAC: GitLab can't HMAC-sign, and
        the forwarder passes the body through untouched. A non-issue payload is acked 200 + ignored so GitLab
        doesn't auto-disable the hook (same for an author outside ISSUE_AUTHOR_ALLOWLIST, which admits nobody while
        empty); the issue-OPEN and author checks run again in Router.classify, and ISSUE_REPO_MAP at spawn."""
        raw = self._body()
        if raw is None:
            return
        if not forge.verify_gitlab_token(self.headers.get("X-Gitlab-Token")):
            return self._reply(401, {"error": "bad or missing X-Gitlab-Token"})
        try:
            payload = json.loads(raw or b"{}")
        except (ValueError, TypeError) as e:
            return self._reply(400, {"error": f"bad json: {e}"})
        if cd.enabled() and isinstance(payload, dict) and payload.get("object_kind") == "pipeline":
            # CD hand-off (only when CD_ENABLED; otherwise a pipeline hook falls through to the
            # original "not a gitlab issue event" ack below). Off-thread with an immediate 200: the GitLab API/zbus
            # calls can take seconds and GitLab disables a hook that times out.
            threading.Thread(target=cd.handle_pipeline, daemon=True,
                             args=(payload, BROKER.config.data.get("deploy-paths")),
                             kwargs={"comment": poster.post_comment, "dm": notify.dm,
                                     "dm_ops": notify.wake_ops}).start()
            return self._reply(200, {"ok": True, "cd": "pipeline event accepted"})
        evt = forge.normalize_gitlab_issue(payload)
        if evt is None:
            return self._reply(200, {"ok": True, "ignored": "not a gitlab issue event"})
        if not forge.author_allowed(evt.get("doer")):
            log.info("webhooks/gitlab IGNORE author=%s project=%s (not in ISSUE_AUTHOR_ALLOWLIST%s)",
                     evt.get("doer"), evt.get("project"),
                     "" if routing.ISSUE_AUTHOR_ALLOWLIST else ", which is empty: issue intake is off")
            return self._reply(200, {"ok": True, "ignored": "author not in ISSUE_AUTHOR_ALLOWLIST"})
        key = BROKER.ingest(evt)
        self._reply(200, {"ok": True, "subject_key": key, "dropped": key is None})

    def do_GET(self):
        path = self.path.rstrip("/")
        # Dashboard: READ-ONLY insight into what is running and its state.
        # No control verbs: those need auth IN THE APP (loopback :8787 is reachable by anything on
        # the host; the reverse-proxy IP guard on broker.internal.example.test is documented defense-in-depth only),
        # which is a separate piece of work. See dash.py.
        if path.split("?", 1)[0] in ("/dash", "/state"):
            try:
                return self._reply(200, dash.snapshot(BROKER, cap=CONCURRENCY, hist=dash.hist_param(self.path)))
            except Exception as e:
                log.exception("dash snapshot failed: %s", e)
                return self._reply(500, {"ok": False, "error": str(e)})
        if path.startswith("/runs/"):
            # read-only status of one subject (dash.run_status); same exposure as /state
            return self._reply(*dash.run_status(BROKER, urllib.parse.unquote(path.split("?", 1)[0][len("/runs/"):])))
        if path.startswith("/stream/"):
            # live transcript SSE (default OFF; auth + key/uuid checks + cap all live in stream.py).
            return stream.serve(self, BROKER, path.split("?", 1)[0][len("/stream/"):])
        if path.startswith("/stream-status/"):
            # what /stream/<key> would answer (403/404/503), without opening a stream (for dash labels).
            return stream.probe(self, BROKER, path.split("?", 1)[0][len("/stream-status/"):])
        if path.startswith("/history/"):
            # replay a PREVIOUS session's recorded transcript (same auth/cap as /stream; see stream.py).
            return stream.serve_past(self, path.split("?", 1)[0][len("/history/"):])
        if path == "/dash.html":
            b = dash.PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if path == "/health":
            health = {"ok": True,
                      "pending": len(BROKER.coalescer.pending),
                      "live_workers": len(BROKER.coalescer.live),
                      "echo_dropped": BROKER.echo_guard.dropped,
                      "allowlist_rejected": BROKER.rejected,
                      "deduped": BROKER.coalescer.deduped,
                      "hmac": {"mode": hmac_mode(), **HMAC_STATS},
                      "events": events.stats()}
            # zulip counters, only while a zulip listener can run; getattr: a minimal/fake broker may lack it
            zh = BROKER.zulip_wake.health() if getattr(BROKER, "zulip_wake", None) else None
            if zh and (zh.get("wake_enabled") or zh.get("autoreply_enabled")):
                health["zulip"] = zh
            return self._reply(200, health)
        if path == "":
            # Requests to broker.example.test/ used to return this same JSON
            # blob (it shared this branch with /health), which just looked like a dead JSON page in a
            # browser. Land humans on the HTML dash instead; the JSON stays reachable at /health (and
            # /dash, /state) for any script/consumer. A plain 302 is safe here because the reverse
            # proxy's forward-auth gates the whole vhost (`handle {}`, no per-path matcher), so redirecting
            # to /dash.html crosses no auth boundary. No caller of bare "/" was found in this repo or
            # its known consumers (checked by grep).
            self.send_response(302)
            self.send_header("Location", "/dash.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._reply(404, {"error": "not found"})

    def _reply(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):
        pass


def _tick_loop():
    while True:
        time.sleep(C.TICK)
        try:
            BROKER.coalescer.tick()
        except Exception as e:
            log.exception("tick error: %s", e)


def _reload_on_hup():
    """SIGHUP handler. Config.reload doesn't raise, but an exception here would come out of serve_forever and
    stop the broker (and every worker thread with it), so anything unexpected is logged and the old config stays."""
    try:
        BROKER.config.reload()
    except Exception:
        log.exception("config reload failed, keeping the previous config")


def main():
    global BROKER
    BROKER = Broker()
    signal.signal(signal.SIGHUP, lambda *a: _reload_on_hup())   # hot-reload
    threading.Thread(target=_tick_loop, daemon=True).start()
    BROKER.liveness.start()                                           # broker-owned liveness sweep
    BROKER.zulip_wake.start()                                         # no-op unless a ZULIP_* flag is on
    if not site_env.push_hosts():   # fail closed, but say why every worker push will be refused
        log.warning("BROKER_PUSH_HOSTS unset (no %s): every worker branch push will be refused", site_env.SITE_ENV)
    if site_env.bad_push_hosts():   # e.g. "https://h" or "h:443" never equals a URL hostname, so it allows nothing
        log.warning("BROKER_PUSH_HOSTS entries %s are not bare hostnames and will never match a push URL",
                    site_env.bad_push_hosts())
    log.info("eph-broker up on %s:%d  concurrency=%d hmac_env=%s", BIND_HOST, PORT, CONCURRENCY, HMAC_SECRET_ENV)
    log_hmac_startup()
    ThreadingHTTPServer((BIND_HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        C.selftest(); sys.exit(0)             # CI gate: the verbatim coalescer self-test
    main()
