#!/usr/bin/env python3
"""zulip_wake.py: Zulip DMs into the broker. Two features, one listener per bot, both DEFAULT OFF.

  (1) Zulip-wake: a 1:1 DM from an allowlisted HUMAN to a wake-mapped bot (config/zulip-wake.json, first:
      mail-bot) is ingested as {source:"zulip"} -> coalescer key zulip:<bot>:<sender_id> -> an
      ephemeral inbox-chat worker (routing._ZULIP_INBOX_ROW). The BROKER DMs the worker's envelope summary
      back to the sender as that bot (reply(), wired into cc._post_envelope via chat_reply).
  (2) Retired-agent auto-reply: a 1:1 DM from a human to a stood_down agent's bot gets ONE canned reply per
      (bot, sender) per window (text: ZULIP_AUTOREPLY_TEXT, site.env; the neutral default names no contact).

INGEST = one broker-managed child per bot running the fleet's existing `zulip-mcp.py --channel` (the same
wake path every standing agent's .mcp.json runs), spoken to over MCP stdio: we write initialize +
notifications/initialized, and read `notifications/claude/channel` lines. That listener already arms at the
newest message (no history replay), refuses to arm at cursor 0 (a fix for an earlier message flood), and skips the bot's own
messages. No new HTTP route; BROKER.ingest is called in-process with internal=True, the ONLY way a
source=zulip event is admitted (POST /event and every other ingest caller refuse it). The reply and
account sinks re-check the wake conditions (wake_admits) on their own.

KEYS: value-free. The broker only ever hands a child the bot-env PATH as ZBUS_ENV (the bash wrapper in the
child sources it, same contract as notify.build_env); it never opens a bot env file or zuliprc. The child env
is otherwise the scrubbed worker allowlist (adapters.cc.scrub_worker_env), so no broker secret leaks into it.

GUARDS, strictly in this order, each fail-closed (handle()):
  1. 1:1 DM only (kind == "dm", no `recipients` = not a huddle; mentions/streams ignored).
  2. Bot sender dropped: `-bot@` email OR Zulip is_bot from a cached `zbus users` lookup; a lookup failure or
     an unknown sender id counts as a bot. Deliberately NOT provenance.classify: its bus-domain rule would call
     the operator (a human on the bus domain) an agent. Exception, WAKE mode only: a sender id in
     ZULIP_WAKE_BOT_SENDERS passes, unless it is the user id of any wake-mapped bot (resolved via `zbus users`
     email local-part; unresolvable means refused). Auto-reply mode still drops every bot.
  3. msg_id dedup (TTL) + max age (ZULIP_MAX_AGE_S): belt and suspenders against any listener replay regression.
  4. wake: sender id in ZULIP_WAKE_SENDERS or admitted by the bot exception (else dropped, no reply) / auto-reply: rate limit 1 per
     (bot, sender) per ZULIP_AUTOREPLY_WINDOW_S, persisted atomically (restart-safe), recorded BEFORE send.

WHICH BOTS: registry (`ZULIP_REGISTRY_PATH`, the supervisor registry JSON) entries with stood_down: true + a bot_env,
re-read with a TTL. Wake-mapped bots listen in wake mode (needs ZULIP_WAKE_ENABLED); the rest minus
ZULIP_AUTOREPLY_EXCLUDE listen in auto-reply mode (needs ZULIP_AUTOREPLY_ENABLED). A bot whose stood_down
flips false loses its listener at the next reconcile (the standing agent owns its bot again). Both flags 0 ->
start() returns without starting a thread or a child.

Env knobs (read at call time): ZULIP_WAKE_ENABLED=0 · ZULIP_AUTOREPLY_ENABLED=0 · ZULIP_WAKE_SENDERS= (empty: nobody wakes) ·
ZULIP_WAKE_BOT_SENDERS= (empty; numeric BOT ids allowed to wake, e.g. a monitoring bot) · ZULIP_MAX_AGE_S=300 · ZULIP_AUTOREPLY_WINDOW_S=86400 · ZULIP_AUTOREPLY_EXCLUDE= ·
ZULIP_REGISTRY_TTL_S=300 · ZULIP_REGISTRY_PATH · ZULIP_WAKE_POLL=2 / ZULIP_AUTOREPLY_POLL=10 (the child's
ZULIP_CHANNEL_POLL) · ZULIP_RECONCILE_S=60 · ZULIP_BOT_CACHE_TTL_S=3600 · ZULIP_MCP_SCRIPT · ZULIP_UV_BIN ·
ZULIP_AUTOREPLY_TEXT ({bot} placeholder) · ZULIP_HISTORY_TIMEOUT_S=3 (prior-DM fetch).

HISTORY: a bot-map row may opt in with "history": N (<= HISTORY_MAX). On a COLD chat start only (cc's seed
turn; a warm forward gets nothing), history() fetches the last N DMs of that 1:1 thread via `zbus dm <sender> --read`
as the bot (bot-env PATH, value-free) and cc injects them into the first DM's UNTRUSTED block. Fail-soft.
"""
import json, logging, os, re, shutil, signal, subprocess, threading, time

import notify
import routing
from adapters.cc import scrub_worker_env

log = logging.getLogger("eph-broker.zulip_wake")

ZBUS = notify.ZBUS
REGISTRY_PATH = os.getenv("ZULIP_REGISTRY_PATH", "")        # from site.env
ZULIP_MCP_SCRIPT = os.getenv("ZULIP_MCP_SCRIPT", "")        # from site.env
# The broker's systemd unit PATH may not carry ~/.local/bin, so resolve uv to an absolute path up front.
UV_BIN = os.getenv("ZULIP_UV_BIN") or shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
# Same launch as every agent's .mcp.json zulip-channel entry. uv + script ride as $0/$1 (no string interpolation
# into the shell). The CHILD sources $ZBUS_ENV; the broker never reads it.
_CHILD_SH = 'set -a; . "$ZBUS_ENV"; set +a; exec "$0" run "$1" --channel'
WAKE_ROWS = {"inbox-chat"}   # row names a wake-map entry may name
ACCOUNT_ROWS = {"inbox-chat"}       # rows whose worker acts on a mailbox: the entry MUST name its account
# Rows that exist only behind a routing flag; a map entry naming one is ignored while the flag is off (no
# listener, no wake), so a mapped-but-disabled bot can never fall through to the generic zulip (inbox) row.
_ROW_ENABLED = {}
# Neutral default; a site names its own contact + docs link in ZULIP_AUTOREPLY_TEXT (site.env).
DEFAULT_AUTOREPLY = "{bot} is no longer in use, so nobody reads messages here."
DEDUP_TTL_S = 3600
HISTORY_MAX = 20                    # hard ceiling on a row's "history" opt-in
_BOT_EMAIL_RE = re.compile(r"-bot@", re.IGNORECASE)
_AGE_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s+ago\s*$")
_AGE_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _flag(name):
    return os.getenv(name, "0").strip().lower() in ("1", "true", "yes", "on")


def wake_enabled():
    return _flag("ZULIP_WAKE_ENABLED")


def autoreply_enabled():
    return _flag("ZULIP_AUTOREPLY_ENABLED")


def _num(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        log.error("%s is not a number -> using %s", name, default)
        return float(default)


def _csv(name, default):
    return {x.strip() for x in os.getenv(name, default).split(",") if x.strip()}


def wake_senders():
    return _csv("ZULIP_WAKE_SENDERS", "")       # from site.env; unset means no human wakes a bot


def wake_bot_senders():
    """BOT user ids allowed to wake a wake-mapped bot (e.g. a monitoring bot). Default empty = no bot ever wakes."""
    return {x for x in _csv("ZULIP_WAKE_BOT_SENDERS", "") if x.isdigit()}


def autoreply_exclude():
    # Bots stood down by policy but not retired (site.env).
    # coder-bot is NOT excluded: once dev is stood_down a DM to it should auto-reply.
    return _csv("ZULIP_AUTOREPLY_EXCLUDE", "")


def bot_handle(bot_env):
    """/etc/site/bots/.mail-bot-env -> mail-bot (path string only; the file is never opened)."""
    name = os.path.basename(str(bot_env or ""))
    if name.startswith("."):
        name = name[1:]
    return name[:-4] if name.endswith("-env") else ""


def _history_n(bot, raw):
    """A wake row's opt-in "history" (prior DMs injected on a COLD chat start) -> 0..HISTORY_MAX. Absent /
    malformed -> 0 (off; malformed logs). A bool is not a count."""
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        log.warning("zulip-wake: bot map entry %r history=%r ignored (want an int >= 0)", bot, raw)
        return 0
    return min(raw, HISTORY_MAX)


# `zbus dm <id> --read N` line: '#<id> [<sender_full_name>] [<YYYY-mm-dd HH:MM:SS>|?]: <content>' (content
# whitespace-collapsed + capped at 400 by zbus). Oldest first. The name is matched greedily (it may hold ']').
_READ_LINE_RE = re.compile(r"^#(\d+) \[(.*)\] \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|\?)\]: ?(.*)$")


def parse_dm_read(text):
    """`zbus dm --read` stdout -> [{"msg_id": int, "sender", "ts", "text"}] in output order; junk lines skipped."""
    out = []
    for line in (text or "").splitlines():
        m = _READ_LINE_RE.match(line)
        if m:
            out.append({"msg_id": int(m.group(1)), "sender": m.group(2), "ts": m.group(3), "text": m.group(4)})
    return out


def parse_age(age):
    """zulip-mcp.py `age` meta ('42s ago' / '3m ago' / '2h ago' / '5d ago') -> seconds, or None (unparseable)."""
    m = _AGE_RE.match(str(age or ""))
    return int(m.group(1)) * _AGE_UNIT[m.group(2)] if m else None


# ---------------------------------------------------------------------------------------------------------
class RegistryCache:
    """stood_down bots from the supervisor registry, re-read at most every ZULIP_REGISTRY_TTL_S. Returns
    {handle: bot_env_path}. A read/parse failure keeps the LAST good set (and logs); a transient registry
    rewrite must not flap every listener; the very first failure yields {} (nothing listens)."""
    def __init__(self, path=None, clock=time.time):
        self.path = path or REGISTRY_PATH
        self.clock = clock
        self._at = None
        self._retired = {}

    def retired(self):
        now = self.clock()
        if self._at is not None and now - self._at < _num("ZULIP_REGISTRY_TTL_S", 300):
            return dict(self._retired)
        self._at = now
        try:
            with open(self.path) as f:
                data = json.load(f)
            sessions = data.get("sessions", []) if isinstance(data, dict) else data
            out = {}
            for e in sessions or []:
                if isinstance(e, dict) and e.get("stood_down") is True and e.get("bot_env"):
                    h = bot_handle(e["bot_env"])
                    if h:
                        out[h] = str(e["bot_env"])
            self._retired = out
        except Exception as e:
            log.warning("zulip-wake: registry read failed (%s): %s - keeping last set", self.path, e)
        return dict(self._retired)


class BotDirectory:
    """Zulip is_bot per user id, from `zbus users` (read-only), cached ZULIP_BOT_CACHE_TTL_S. Fail-closed:
    an unknown id or an expired cache triggers a refresh (attempts rate-limited to one a minute, so a down bus
    can't make every DM shell out); still unknown or the lookup failed -> treated as a bot."""
    REFRESH_MIN_S = 60

    def __init__(self, runner=None, clock=time.time):
        self.runner = runner or self._run_zbus_users
        self.clock = clock
        self._users = None          # {user_id(str): is_bot(bool)}
        self._handles = {}          # {email local-part: user_id(str)}
        self._at = None             # last SUCCESSFUL load
        self._tried = None          # last attempt (success or failure)
        self._lock = threading.Lock()

    @staticmethod
    def _run_zbus_users(zbus_env):
        out = subprocess.run([ZBUS, "users"], env=notify.build_env(zbus_env),
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(f"zbus users exit={out.returncode}")
        return out.stdout

    @staticmethod
    def parse(text):
        """`zbus users` lines: '<id> | <name> | <email> | bot=<True|False> | active=<..>'. Name may hold '|'."""
        users = {}
        for line in (text or "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5 or not parts[0].isdigit() or not parts[-2].startswith("bot="):
                continue
            users[parts[0]] = parts[-2] != "bot=False"   # anything but an explicit False counts as a bot
        return users

    @staticmethod
    def parse_handles(text):
        """`zbus users` lines -> {email local-part (lowercased): user_id(str)}, i.e. how a bot handle resolves to its id."""
        out = {}
        for line in (text or "").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5 or not parts[0].isdigit() or "@" not in parts[-3]:
                continue
            out[parts[-3].split("@", 1)[0].lower()] = parts[0]
        return out

    def _refresh(self, zbus_env, now):
        self._tried = now
        try:
            text = self.runner(zbus_env)
            users = self.parse(text)
            if not users:
                raise RuntimeError("empty user list")
            self._users, self._handles, self._at = users, self.parse_handles(text), now
        except Exception as e:
            log.warning("zulip-wake: `zbus users` lookup failed: %s (senders treated as bots)", e)

    def user_id(self, handle, zbus_env):
        """A bot handle (e.g. mail-bot) -> its Zulip user id, or None (unknown / lookup failed)."""
        h = str(handle or "").lower()
        with self._lock:
            now = self.clock()
            need = (self._users is None or h not in self._handles
                    or now - self._at >= _num("ZULIP_BOT_CACHE_TTL_S", 3600))
            if need and (self._tried is None or now - self._tried >= self.REFRESH_MIN_S):
                self._refresh(zbus_env, now)
            return self._handles.get(h) if self._users is not None else None

    def is_bot(self, sender_id, email, zbus_env):
        if _BOT_EMAIL_RE.search(str(email or "")):
            return True
        sid = str(sender_id or "")
        if not sid.isdigit():
            return True
        with self._lock:
            now = self.clock()
            need = (self._users is None or sid not in self._users
                    or now - self._at >= _num("ZULIP_BOT_CACHE_TTL_S", 3600))
            if need and (self._tried is None or now - self._tried >= self.REFRESH_MIN_S):
                self._refresh(zbus_env, now)
            users = self._users
        if users is None or sid not in users:
            return True
        return users[sid]


class AutoReplyLimiter:
    """1 auto-reply per (bot, sender) per window. {"bot:sender": ts} persisted atomically (tmp + os.replace,
    the repo_parked.json pattern) at `path`; None -> in-memory only. Pruned on every write."""
    def __init__(self, path=None, clock=time.time):
        self.path = path
        self.clock = clock
        self._lock = threading.Lock()
        self._seen = {}
        if path:
            try:
                with open(path) as f:
                    self._seen = {str(k): float(v) for k, v in json.load(f).items()}
            except FileNotFoundError:
                pass
            except Exception as e:
                log.warning("zulip-wake: autoreply state load failed (%s): %s - starting empty", path, e)

    def admit(self, bot, sender_id):
        """True -> send (already recorded + persisted, BEFORE the send, so a send failure never retry-storms)."""
        window = _num("ZULIP_AUTOREPLY_WINDOW_S", 86400)
        key, now = f"{bot}:{sender_id}", self.clock()
        with self._lock:
            self._seen = {k: t for k, t in self._seen.items() if now - t < window}
            if key in self._seen:
                return False
            self._seen[key] = now
            self._save()
            return True

    def _save(self):
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._seen, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("zulip-wake: autoreply state persist failed (%s): %s", self.path, e)


# ---------------------------------------------------------------------------------------------------------
class _Listener:
    """One `zulip-mcp.py --channel` child for one bot, with restart + backoff. Its reader thread hands every
    `notifications/claude/channel` payload to on_event(bot, params)."""
    def __init__(self, bot, bot_env, mode, on_event, popen, state_dir):
        self.bot, self.bot_env, self.mode = bot, bot_env, mode
        self.on_event, self.popen, self.state_dir = on_event, popen, state_dir
        self.stop_evt = threading.Event()
        self.proc = None
        self.thread = None

    def argv(self):
        return ["bash", "-c", _CHILD_SH, UV_BIN, ZULIP_MCP_SCRIPT]

    def env(self):
        poll = os.getenv("ZULIP_WAKE_POLL", "2") if self.mode == "wake" else os.getenv("ZULIP_AUTOREPLY_POLL", "10")
        dbg = os.path.join(self.state_dir or os.getenv("TMPDIR", "/tmp"), f"zulip-channel-{self.bot}.log")
        return scrub_worker_env(os.environ, route_env={"ZBUS_ENV": self.bot_env, "ZULIP_CHANNEL_POLL": str(poll),
                                                       "CHANNEL_DEBUG_LOG": dbg})

    def start(self):
        self.thread = threading.Thread(target=self._run, name=f"zulip-listen-{self.bot}", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    def _run(self):
        backoff = 2.0
        while not self.stop_evt.is_set():
            started = time.time()
            self.proc = None
            try:
                self.proc = self.popen(self.argv(), env=self.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True, bufsize=1)
                if self.stop_evt.is_set():
                    # stop() ran between the loop check and popen (it saw proc=None, so it could
                    # not terminate this child), so never handshake; the finally below terminates it.
                    break
                log.info("zulip-wake: listener UP bot=%s mode=%s pid=%s", self.bot, self.mode,
                         getattr(self.proc, "pid", None))
                self._handshake()
                for line in self.proc.stdout:
                    if self.stop_evt.is_set():
                        break
                    self._line(line)
            except Exception as e:
                log.warning("zulip-wake: listener bot=%s error: %s", self.bot, e)
            finally:
                p = self.proc
                if p is not None:
                    try:
                        p.terminate(); p.wait(timeout=10)
                    except Exception:
                        pass
            if self.stop_evt.is_set():
                break
            backoff = 2.0 if time.time() - started > 300 else min(backoff * 2, 300.0)
            log.warning("zulip-wake: listener bot=%s exited -> restart in %.0fs", self.bot, backoff)
            self.stop_evt.wait(backoff)
        log.info("zulip-wake: listener STOPPED bot=%s", self.bot)

    def _handshake(self):
        for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "eph-broker", "version": "1"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}):
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def _line(self, line):
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("method") == "notifications/claude/channel":
            try:
                self.on_event(self.bot, msg.get("params") or {})
            except Exception as e:                   # one bad event must never kill the listener
                log.warning("zulip-wake: handle failed bot=%s: %s", self.bot, e)


class ZulipWake:
    def __init__(self, ingest, wake_map=None, state_dir=None, registry=None, bots=None, dm=None,
                 popen=subprocess.Popen, clock=time.time, history_runner=None):
        self.ingest = ingest                                  # Broker.ingest (in-process)
        self.history_runner = history_runner or self._run_zbus_dm_read   # (sender_id, n, zbus_env) -> stdout
        self._wake_map = wake_map or (lambda: {})             # -> the live config/zulip-wake.json dict
        self.state_dir = state_dir
        self.registry = registry or RegistryCache(clock=clock)
        self.bots = bots or BotDirectory(clock=clock)
        self.dm = dm or notify.dm
        self.popen = popen
        self.clock = clock
        self.limiter = AutoReplyLimiter(os.path.join(state_dir, "zulip_autoreply.json") if state_dir else None,
                                        clock=clock)
        self._seen = {}                                       # msg_id -> ts (dedup, TTL)
        self._lock = threading.Lock()
        self._ctr_lock = threading.Lock()
        self.listeners = {}                                   # bot -> _Listener
        self.counters = {"events": 0, "not_1to1": 0, "bot_sender": 0, "dup": 0, "stale": 0,
                         "not_allowlisted": 0, "woken": 0, "rate_limited": 0, "autoreplied": 0, "unmapped": 0}
        self._thread = None

    # ---- config ----
    def wake_map(self):
        """{bot: {"row", "account", "history"}} for well-formed entries only (keys starting "_" are comments). The
        account is required only for an ACCOUNT_ROWS row (inbox-chat); an account-less row maps to
        None. "history" is the cold-start prior-DM count, 0 = off (the default)."""
        raw = self._wake_map() or {}
        out = {}
        for bot, v in raw.items() if isinstance(raw, dict) else ():
            if str(bot).startswith("_") or not isinstance(v, dict):
                continue
            row = v.get("row")
            if row not in WAKE_ROWS or (row in ACCOUNT_ROWS and not v.get("account")):
                log.warning("zulip-wake: bot map entry %r ignored (row must be one of %s; account required for %s)",
                            bot, sorted(WAKE_ROWS), sorted(ACCOUNT_ROWS))
                continue
            if row in _ROW_ENABLED and not _ROW_ENABLED[row]():
                continue
            out[str(bot)] = {"row": row, "account": str(v["account"]) if v.get("account") else None,
                             "history": _history_n(bot, v.get("history"))}
        return out

    def wake_admits(self, bot, sender_id):
        """The wake conditions are re-checked at EVERY sink, not just in handle(): wake flag
        on, bot wake-mapped AND stood_down in the registry, sender a bare numeric id in ZULIP_WAKE_SENDERS. So an
        event that somehow reached the coalescer without passing handle() still spawns nothing and DMs no one."""
        sid = str(sender_id or "")
        if not (wake_enabled() and sid.isdigit() and bot in self.wake_map()):
            return False
        bot_env = self.registry.retired().get(bot)
        return bool(bot_env) and (sid in wake_senders() or self.bot_sender_admits(sid, bot_env))

    def bot_sender_admits(self, sender_id, zbus_env):
        """A BOT sender may wake only if its id is in ZULIP_WAKE_BOT_SENDERS AND it is not the user id of
        any wake-mapped bot (no bot wakes itself or another wake-mapped bot). Fail closed: a wake-mapped bot
        whose id can't be resolved -> refuse. Empty knob -> False with no lookup at all."""
        sid = str(sender_id or "")
        if not sid.isdigit() or sid not in wake_bot_senders():
            return False
        for b in self.wake_map():
            own = self.bots.user_id(b, zbus_env)
            if own is None or own == sid:
                return False
        return True

    def account_for(self, bot, sender_id=None):
        """cc spawn gate (zulip_account): the mapped inbox account, or None -> fail closed. None whenever
        wake_admits() fails (flag off / unmapped / not retired / sender not allowlisted)."""
        if not self.wake_admits(bot, sender_id):
            return None
        return (self.wake_map().get(bot) or {}).get("account")

    def row_admits(self, bot, sender_id, row):
        """cc spawn gate for an account-less chat row: wake_admits() AND the bot map names exactly `row`."""
        return self.wake_admits(bot, sender_id) and (self.wake_map().get(bot) or {}).get("row") == row

    # ---- prior-conversation history for a COLD chat start ----
    @staticmethod
    def _run_zbus_dm_read(sender_id, n, zbus_env):
        """`zbus dm <sender_id> --read <n>` AS the DMed bot (bot-env PATH only; never opened here). Raises on a
        non-zero exit; a timeout (ZULIP_HISTORY_TIMEOUT_S, default 3s) kills the whole process group (zbus's curl
        grandchild too, so a held pipe can't stretch the wait) and raises subprocess.TimeoutExpired."""
        p = subprocess.Popen([ZBUS, "dm", str(sender_id), "--read", str(int(n))], env=notify.build_env(zbus_env),
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                             start_new_session=True)
        try:
            stdout, _ = p.communicate(timeout=_num("ZULIP_HISTORY_TIMEOUT_S", 3))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.communicate()
            raise
        if p.returncode != 0:
            raise RuntimeError(f"zbus dm --read exit={p.returncode}")
        return stdout

    def history(self, bot, sender_id, before_msg_id, skip=1):
        """The last N messages (N = the bot map row's "history"; 0/absent -> none, no fetch) of the 1:1 DM
        thread between `bot` and `sender_id`, BOTH sides, oldest first, strictly OLDER than `before_msg_id` (the
        triggering DM is already in the brief). `skip` = how many triggering DMs the brief carries, so the fetch
        still yields N prior ones. Only while wake_admits(bot, sender). Fail-soft: any fetch/parse error or the
        timeout -> [] + ONE log line (history is context, not a control). Returns parse_dm_read() rows."""
        sid = str(sender_id or "")
        n = (self.wake_map().get(bot) or {}).get("history") or 0
        if n <= 0:
            return []
        try:
            before = int(before_msg_id)
            if not self.wake_admits(bot, sid):
                return []
            rows = parse_dm_read(self.history_runner(sid, n + max(int(skip), 1), self.registry.retired().get(bot)))
        except Exception as e:
            log.warning("zulip-wake: history fetch failed bot=%s sender=%s (%s: %s) -> no history",
                        bot, sid or "-", type(e).__name__, e)
            return []
        return [r for r in rows if r["msg_id"] < before][-n:]

    def desired(self):
        """{bot: (bot_env, mode)} this broker should be listening on right now."""
        retired, wmap = self.registry.retired(), self.wake_map()
        out = {}
        if wake_enabled():
            for bot in wmap:
                if bot in retired:                    # a wake-mapped bot must ALSO be stood_down
                    out[bot] = (retired[bot], "wake")
        if autoreply_enabled():
            skip = set(wmap) | autoreply_exclude()
            for bot, env in retired.items():
                if bot not in skip:
                    out[bot] = (env, "autoreply")
        return out

    # ---- listener lifecycle ----
    def reconcile(self):
        want = self.desired()
        for bot in list(self.listeners):
            li = self.listeners[bot]
            if bot not in want or want[bot] != (li.bot_env, li.mode):
                log.info("zulip-wake: stopping listener bot=%s (no longer %s)", bot, li.mode)
                li.stop()
                del self.listeners[bot]
        for bot, (env, mode) in want.items():
            if bot not in self.listeners:
                li = _Listener(bot, env, mode, self.handle, self.popen, self.state_dir)
                self.listeners[bot] = li
                li.start()
        return want

    def start(self):
        """Start the reconcile loop. Both flags 0 -> nothing at all (no thread, no child)."""
        if not (wake_enabled() or autoreply_enabled()):
            log.info("zulip-wake: ZULIP_WAKE_ENABLED=0 and ZULIP_AUTOREPLY_ENABLED=0 -> no listeners")
            return False
        self._thread = threading.Thread(target=self._loop, name="zulip-wake-reconcile", daemon=True)
        self._thread.start()
        return True

    def _loop(self):
        while True:
            try:
                self.reconcile()
            except Exception as e:
                log.exception("zulip-wake: reconcile failed: %s", e)
            time.sleep(_num("ZULIP_RECONCILE_S", 60))

    # ---- the guard pipeline (order is load-bearing; see module docstring) ----
    def handle(self, bot, params):
        self._count("events")
        meta = params.get("meta") or {}
        sid, email, mid = str(meta.get("sender_id") or ""), meta.get("sender"), str(meta.get("msg_id") or "")
        # 1. 1:1 DM only
        if meta.get("kind") != "dm" or meta.get("recipients"):
            return self._drop("not_1to1", bot, sid, mid)
        # 2. bot sender dropped BEFORE anything else looks at it (loop guard; lookup failure = bot), except, in
        # wake mode only, an id in ZULIP_WAKE_BOT_SENDERS that is not itself a wake-mapped bot
        li = self.listeners.get(bot)
        bot_env = li.bot_env if li else self.registry.retired().get(bot)
        bot_ok = bool(li and li.mode == "wake" and self.bot_sender_admits(sid, bot_env))
        if not bot_ok and self.bots.is_bot(sid, email, bot_env):
            return self._drop("bot_sender", bot, sid, mid)
        # 3. dedup + age
        now = self.clock()
        with self._lock:
            self._seen = {m: t for m, t in self._seen.items() if now - t < DEDUP_TTL_S}
            if not mid or mid in self._seen:
                return self._drop("dup", bot, sid, mid)
            self._seen[mid] = now
        age = parse_age(meta.get("age"))
        if age is None or age > _num("ZULIP_MAX_AGE_S", 300):
            return self._drop("stale", bot, sid, mid)
        # 4. per-mode gate, against the CURRENT config (a reload may have moved the bot since its listener began)
        mode = li.mode if li else None
        if mode == "wake":
            if not wake_enabled() or bot not in self.wake_map():
                return self._drop("unmapped", bot, sid, mid)
            if sid not in wake_senders() and not bot_ok:
                return self._drop("not_allowlisted", bot, sid, mid)
            self._count("woken")
            log.info("zulip-wake: WAKE bot=%s sender=%s msg=%s -> ingest", bot, sid, mid)
            self.ingest({"source": "zulip", "event_name": "zulip.dm", "bot": bot, "sender_id": sid,
                         "sender": email, "msg_id": mid, "content": params.get("content") or "",
                         "doer": f"zulip:{sid}"})   # doer: a human id the echo-guard lets through
            return "woken"
        if mode == "autoreply":
            if not autoreply_enabled() or bot in self.wake_map() or bot in autoreply_exclude():
                return self._drop("unmapped", bot, sid, mid)
            if not self.limiter.admit(bot, sid):
                return self._drop("rate_limited", bot, sid, mid)
            text = os.getenv("ZULIP_AUTOREPLY_TEXT", DEFAULT_AUTOREPLY).replace("{bot}", bot)
            self._count("autoreplied")
            log.info("zulip-wake: AUTO-REPLY bot=%s sender=%s msg=%s", bot, sid, mid)
            self.dm(sid, text, zbus_env=bot_env)
            return "autoreplied"
        return self._drop("unmapped", bot, sid, mid)

    def _count(self, k):
        with self._ctr_lock:                                  # NOT self._lock: _drop runs inside the dedup lock
            self.counters[k] += 1

    def _drop(self, why, bot, sid, mid):
        self._count(why)
        log.info("zulip-wake: DROP %s bot=%s sender=%s msg=%s", why, bot, sid or "-", mid or "-")
        return why

    # ---- the chat reply sink (cc.chat_reply) ----
    def reply(self, bot, sender_id, text, run_id=None):
        """DM the chat worker's reply to the original sender AS the DMed bot, ONLY if wake_admits(bot, sender)
        (flag on, bot wake-mapped + stood_down, sender allowlisted numeric id). The bot env PATH comes from the
        registry. The auto-reply path never uses this (it DMs from handle() only, which only a listener feeds).
        Never raises; False on any refusal/failure."""
        sid = str(sender_id or "")
        if not self.wake_admits(bot, sid):
            log.warning("zulip-wake: reply REFUSED bot=%s sender=%s run_id=%s (wake off / unmapped / not retired / "
                        "sender not allowlisted)", bot, sid or "-", run_id)
            return False
        bot_env = self.registry.retired().get(bot)
        try:
            return bool(self.dm(sid, text, zbus_env=bot_env))
        except Exception as e:
            log.warning("zulip-wake: reply failed bot=%s run_id=%s: %s", bot, run_id, e)
            return False

    def health(self):
        return {"wake_enabled": wake_enabled(), "autoreply_enabled": autoreply_enabled(),
                "listeners": {b: li.mode for b, li in self.listeners.items()}, **self.counters}
