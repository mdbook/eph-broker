#!/usr/bin/env python3
"""stream.py: live worker transcript streaming (SSE) for the read-only operator dash.

`GET /stream/<url-quoted subject_key>` tails the live Claude Code transcript of a CURRENT live worker
(~/.claude/projects/<cwd-slug>/<session uuid>.jsonl) as `text/event-stream`, one jsonl line per event.

DEFAULT OFF. Env (names + defaults; values live in eph-broker.env or the site overlay config/site.env):
  BROKER_STREAM_ENABLED  unset  -> off: /stream/* 404s AND the CC adapter passes no --session-id (inert).
                         "1"/"true" -> on: every fresh worker session is spawned with --session-id <uuid4>,
                         recorded on the adapter Worker (stream_session) so the stream can find its file.
  BROKER_STREAM_GROUP    unset  -> REQUIRED when enabled. Group (as the auth proxy reports it) whose members
                         may stream. Unset while enabled = every /stream request 403s (fail-closed, logged once).
  BROKER_STREAM_USER_HEADER / BROKER_STREAM_GROUPS_HEADER  unset -> REQUIRED when enabled: the request
                         headers the auth proxy sets for the username and the group list. No built-in default,
                         so no header is trusted by accident; unset = every /stream 403s (fail-closed, logged once).
                         A name a client or a generic proxy sets (Host, Cookie, User-Agent, Referer, Origin,
                         Authorization, Forwarded, X-Real-IP, X-Forwarded-*) counts as unset: a proxy that only
                         strips its own auth headers would pass a client's copy straight through.
  BROKER_STREAM_GROUPS_SEP  "|"  -> separator of the group-list header (some SSO proxies pipe-separate).
  BROKER_STREAM_MAX_S    600    -> server closes a stream after this many seconds, so the EventSource
                         reconnect re-runs the reverse proxy's forward-auth.
  BROKER_STREAM_MAX      =CONCURRENCY (5) -> max concurrent streams; over cap -> 503.

`GET /stream-status/<url-quoted key>` returns (always HTTP 200) JSON {status, reason, max}: the status
/stream/<key> would answer, without taking a slot, so the dash can label a closed EventSource precisely.

AUTH: header-trust. The reverse proxy MUST strip client-sent copies of both headers and its forward-auth
re-set them, so a request reaching here through the proxy carries the real identity. 403 unless the
username header is non-empty AND BROKER_STREAM_GROUP is an exact member of the groups header.

PATH SAFETY: the request key is only ever used as an exact dict lookup against the coalescer's live map.
The filesystem path comes ONLY from the uuid the broker itself recorded (format-validated), globbed under
PROJECTS_DIR, so the cwd-slug rule Claude Code uses can't drift out from under us.

PREVIOUS SESSIONS: every fresh stream session the CC adapter pins is also remember()ed in a bounded
history (last HISTORY_MAX, newest wins), persisted as jsonl at BROKER_STREAM_HISTORY (default:
stream-history.jsonl beside an explicitly-configured EVENTS_LOG; unset both = in-memory only). `/state` lists
it as key + 8-char session prefix + start time (same exposure as the live rows), and
`GET /history/<url-quoted key>/<8-hex prefix>` replays that recorded transcript (last PAST_LINES lines) as
the same SSE framing, then `event: end`. Same auth + concurrency cap as /stream; the path again comes only
from a uuid the broker recorded for that exact key; the request never names a file.
"""
import collections, glob, json, logging, os, re, socket, threading, time, urllib.parse

log = logging.getLogger("eph-broker.stream")

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")   # module attr so tests can repoint it
TAIL_LINES = 200            # backfill on connect
BACKFILL_BYTES = 1 << 20    # read at most the last 1 MiB for the backfill (bounded memory on big transcripts)
POLL_S = 0.5
PING_S = 15.0

# Previous-session history (module attrs so tests can repoint them)
HISTORY_PATH = os.getenv("BROKER_STREAM_HISTORY") or (
    os.path.join(os.path.dirname(os.environ["EVENTS_LOG"]), "stream-history.jsonl") if os.getenv("EVENTS_LOG") else "")
HISTORY_MAX = 200
PAST_LINES = 1000           # replay of a previous session: more than the live backfill, still bounded
PAST_BYTES = 4 << 20

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SID8_RE = re.compile(r"^[0-9a-f]{8}$")
_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                socket.timeout)   # a stalled reader hit the handler timeout (not TimeoutError on 3.9)

_slots_lock = threading.Lock()
_active = 0
_warned_no_group = False
_warned_unsafe = set()
_CLIENT_HEADERS = {"host", "cookie", "user-agent", "referer", "origin", "authorization", "forwarded", "x-real-ip"}


# ---------------------------------------------------------------- config (read per call: cheap, testable)

def enabled():
    return (os.getenv("BROKER_STREAM_ENABLED") or "").strip().lower() in ("1", "true")


def _num(name, default, cast):
    try:
        return cast(os.getenv(name) or default)
    except ValueError:
        return cast(default)


def max_s():
    return _num("BROKER_STREAM_MAX_S", "600", float)


def max_streams():
    # Default tracks the broker's worker CONCURRENCY (default 5) so every live worker can be watched.
    return _num("BROKER_STREAM_MAX", str(_num("CONCURRENCY", "5", int)), int)


def valid_uuid(s):
    return isinstance(s, str) and bool(_UUID_RE.match(s))


# ---------------------------------------------------------------- auth / lookup / slots

def _identity_header(var):
    """The configured header name, or "" when unset or a client-settable name (fail-closed, logged once)."""
    name = (os.getenv(var) or "").strip()
    low = name.lower()
    if low in _CLIENT_HEADERS or low.startswith("x-forwarded-"):
        if (var, low) not in _warned_unsafe:
            _warned_unsafe.add((var, low))
            log.error("stream: %s=%s is a client-settable header, refusing to trust it -> every /stream 403", var, name)
        return ""
    return name


def authorized(headers):
    """(allowed, username). Fail-closed on a missing group config, a missing username, or no exact match."""
    global _warned_no_group
    user_h = _identity_header("BROKER_STREAM_USER_HEADER")
    groups_h = _identity_header("BROKER_STREAM_GROUPS_HEADER")
    user = (headers.get(user_h) or "").strip() if user_h else ""
    group = (os.getenv("BROKER_STREAM_GROUP") or "").strip()
    if not (group and user_h and groups_h):
        if not _warned_no_group:
            _warned_no_group = True
            log.error("stream: BROKER_STREAM_ENABLED but BROKER_STREAM_GROUP / BROKER_STREAM_USER_HEADER / "
                      "BROKER_STREAM_GROUPS_HEADER unset -> every /stream 403 (fail-closed)")
        return False, user
    if not user:
        return False, user
    sep = os.getenv("BROKER_STREAM_GROUPS_SEP") or "|"
    groups = [g.strip() for g in (headers.get(groups_h) or "").split(sep)]
    return group in groups, user


def lookup(broker, key):
    """The recorded session uuid for an exact CURRENT live coalescer key, else None. Takes each lock briefly
    and never both at once (same discipline as dash.snapshot)."""
    with broker.coalescer.lock:
        live = key in broker.coalescer.live
    if not live:
        return None
    adapter = getattr(broker, "adapter", None)
    lock = getattr(adapter, "_lock", None)
    if lock is None:
        return None
    with lock:
        wk = adapter._workers.get(key)
    sid = getattr(wk, "stream_session", None)
    return sid if valid_uuid(sid) else None


def _acquire():
    global _active
    with _slots_lock:
        if _active >= max_streams():
            return False
        _active += 1
        return True


def _release():
    global _active
    with _slots_lock:
        _active = max(0, _active - 1)


def active():
    with _slots_lock:
        return _active


# ---------------------------------------------------------------- previous-session history

_hist_lock = threading.Lock()
_hist = None                # deque of {"key","sid","ts"}, lazily loaded from HISTORY_PATH
_hist_appends = 0           # appends since the file was last compacted


def _valid_rec(r):
    return (isinstance(r, dict) and isinstance(r.get("key"), str) and r["key"] and valid_uuid(r.get("sid"))
            and isinstance(r.get("ts"), (int, float)))


def _load():
    """The in-memory history (caller holds _hist_lock). First call reads HISTORY_PATH; bad lines are skipped."""
    global _hist
    if _hist is None:
        _hist = collections.deque(maxlen=HISTORY_MAX)
        if HISTORY_PATH:
            try:
                with open(HISTORY_PATH, encoding="utf-8", errors="replace") as f:
                    for ln in f:
                        try:
                            r = json.loads(ln)
                        except ValueError:
                            continue
                        if _valid_rec(r):
                            _hist.append({"key": r["key"], "sid": r["sid"], "ts": r["ts"]})
            except OSError:
                pass
    return _hist


def remember(key, sid, now=None):
    """Record a fresh stream session for `key` (called by the CC adapter right after it pins the uuid).
    Best-effort: a write failure is logged, never raised into the worker spawn path."""
    global _hist_appends
    rec = {"key": key, "sid": sid, "ts": round(now if now is not None else time.time(), 1)}
    if not _valid_rec(rec):
        return
    with _hist_lock:
        h = _load()
        h.append(rec)
        if not HISTORY_PATH:
            return
        try:
            os.makedirs(os.path.dirname(HISTORY_PATH) or ".", exist_ok=True)
            _hist_appends += 1
            if _hist_appends >= HISTORY_MAX:            # compact: the file never grows past ~2x HISTORY_MAX
                tmp = HISTORY_PATH + ".tmp"
                with open(tmp, "w") as f:
                    f.write("".join(json.dumps(r) + "\n" for r in h))
                os.replace(tmp, HISTORY_PATH)
                _hist_appends = 0
            else:
                with open(HISTORY_PATH, "a") as f:
                    f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("stream history write failed (%s): %s", HISTORY_PATH, e)


def history():
    """Newest-first view for /state: key + 8-char session prefix + start time. Never the full uuid."""
    with _hist_lock:
        recs = list(_load())
    return [{"key": r["key"], "session": r["sid"][:8], "ts": r["ts"]} for r in reversed(recs)]


def past_lookup(key, sid8):
    """Full recorded uuid for an exact (key, 8-hex prefix) history entry, newest match first, else None."""
    if not isinstance(sid8, str) or not _SID8_RE.match(sid8):
        return None
    with _hist_lock:
        recs = list(_load())
    for r in reversed(recs):
        if r["key"] == key and r["sid"].startswith(sid8):
            return r["sid"]
    return None


# ---------------------------------------------------------------- transcript file

def find_transcript(sid):
    if not valid_uuid(sid):
        return None
    hits = glob.glob(os.path.join(glob.escape(PROJECTS_DIR), "*", sid + ".jsonl"))
    return hits[0] if hits else None


def _read_complete(path, offset):
    """(complete lines after offset, new offset). A trailing partial line (writer mid-append) is left for
    the next poll."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return [ln for ln in data[:end].split(b"\n") if ln.strip()], offset + end + 1


def _backfill(path, max_lines=None, max_bytes=None):
    """(last max_lines (default TAIL_LINES) complete lines, offset just past them)."""
    size = os.path.getsize(path)
    start = max(0, size - (max_bytes or BACKFILL_BYTES))
    lines, offset = _read_complete(path, start)
    if start > 0 and lines:
        lines = lines[1:]                        # first line was cut by the byte window
    return list(collections.deque(lines, maxlen=max_lines or TAIL_LINES)), offset


def _event(line):
    # JSON lines are single-line already; still strip CR/LF so a stray one can't split the SSE frame.
    txt = line.decode("utf-8", "replace").replace("\r", "").replace("\n", "")
    return ("data: " + txt + "\n\n").encode()


# ---------------------------------------------------------------- handler entry

def _precheck(headers, broker, key):
    """(code, error, user, sid): what /stream/<key> would answer BEFORE taking a slot. code 200 = streamable."""
    if not enabled():
        return 404, "not found", "", None
    allowed, user = authorized(headers)
    if not allowed:
        return 403, "forbidden", user, None
    sid = lookup(broker, key)
    if sid is None:
        return 404, "no live worker with a streamable session", user, None
    return 200, "", user, sid


def probe(handler, broker, raw_key):
    """GET /stream-status/<raw_key> -> JSON {status, reason, max}: the status /stream/<key> would give,
    without opening a stream or taking a slot. The dash calls it when its EventSource closes on an HTTP error
    (EventSource itself can't see the status) so it can say WHY: not authorized / worker ended / limit."""
    key = urllib.parse.unquote(raw_key)
    code, err, _, _ = _precheck(handler.headers, broker, key)
    if code == 200 and active() >= max_streams():
        code, err = 503, "too many streams"
    reason = {200: "ok", 403: "forbidden", 503: "stream-limit"}.get(code, "gone")
    return handler._reply(200, {"status": code, "reason": reason, "error": err, "max": max_streams()})


def serve(handler, broker, raw_key):
    """Called from app.Handler.do_GET for /stream/<raw_key>. Error replies go through handler._reply."""
    key = urllib.parse.unquote(raw_key)
    code, err, user, sid = _precheck(handler.headers, broker, key)
    if code == 403:
        log.warning("stream DENY user=%r key=%r", user, key)
    if code != 200:
        return handler._reply(code, {"error": err})
    if not _acquire():
        return handler._reply(503, {"error": "too many streams"})
    log.info("stream OPEN user=%s key=%s", user, key)
    why = "?"
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        why = pump(handler.wfile, broker, key, sid)
    except _CLIENT_GONE:
        why = "client-gone"
    except Exception as e:                       # never let a stream bug take a server thread down noisily
        why = f"error: {e}"
        log.exception("stream error key=%s", key)
    finally:
        _release()
        handler.close_connection = True
        log.info("stream CLOSE user=%s key=%s (%s)", user, key, why)


def serve_past(handler, raw):
    """Called from app.Handler.do_GET for /history/<quoted key>/<sid8>. One-shot replay of a recorded
    previous session's transcript, then `event: end` (the page closes its EventSource on that)."""
    if not enabled():
        return handler._reply(404, {"error": "not found"})
    allowed, user = authorized(handler.headers)
    qkey, _, sid8 = raw.rpartition("/")
    key = urllib.parse.unquote(qkey)
    if not allowed:
        log.warning("history DENY user=%r key=%r", user, key)
        return handler._reply(403, {"error": "forbidden"})
    sid = past_lookup(key, sid8)
    path = find_transcript(sid) if sid else None
    if not path:
        return handler._reply(404, {"error": "no recorded transcript for that session"})
    if not _acquire():
        return handler._reply(503, {"error": "too many streams"})
    log.info("history OPEN user=%s key=%s session=%s", user, key, sid8)
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        lines, _ = _backfill(path, PAST_LINES, PAST_BYTES)
        handler.wfile.write(b"".join(_event(ln) for ln in lines) + b"event: end\ndata: past-session\n\n")
        handler.wfile.flush()
    except _CLIENT_GONE:
        pass
    except Exception:
        log.exception("history error key=%s", key)
    finally:
        _release()
        handler.close_connection = True


def pump(wfile, broker, key, sid):
    """Backfill + tail loop. Returns why it stopped: 'max-duration' | 'worker-ended'. Follows a session
    rotation (worker got a fresh uuid) by switching files. Client disconnects raise out of wfile.write."""
    start = time.monotonic()
    deadline, last_ping = start + max_s(), start
    path, offset = None, 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            return "max-duration"                # plain close -> EventSource reconnects -> forward_auth again
        cur = lookup(broker, key)
        if cur is None:
            wfile.write(b"event: end\ndata: worker-ended\n\n")
            wfile.flush()
            return "worker-ended"
        if cur != sid:                           # session rotation: follow the new transcript
            sid, path, offset = cur, None, 0
        if path is None:
            path = find_transcript(sid)          # file may not exist yet (worker still starting): keep waiting
            lines = []
            if path:
                lines, offset = _backfill(path)
        else:
            try:
                lines, offset = _read_complete(path, offset)
            except OSError:
                lines, path, offset = [], None, 0
        out = b"".join(_event(ln) for ln in lines)
        if now - last_ping >= PING_S:
            out += b": ping\n\n"
            last_ping = now
        if out:
            wfile.write(out)
            wfile.flush()
        time.sleep(POLL_S)
