#!/usr/bin/env python3
"""dash.py: read-only dash for the ephemeral-worker broker.

Scope: INSIGHT ONLY. What is running and its current state. No writes, no control verbs (those need in-app
auth, which is described in the note at the bottom of PAGE, and are a separate lane).

Why a separate module: `snapshot()` takes the broker as an argument rather than being a Broker method,
so it can be exercised against a fake without standing up a real broker (which would spawn real
workers). app.py stays a wiring file; this owns the view.

SAFETY: what this deliberately does NOT expose:
  * It counts buffered events, it never dumps them. An event body is raw ticket/webhook content; the
    dash is reachable from anywhere on the broker host via loopback, so payloads stay out of it.
  * session_id is truncated to 8 chars (correlation only, not a resumable handle).
  * No env, no paths, no tokens, no worktree contents.
"""

import datetime
import json
import os
import re
import threading
import time

import site_env  # noqa: F401  config/site.env carries the FLEET_*/SUPERVISOR_STATUS_PATH values
import stream

# ---------------------------------------------------------------- fleet
# READ-ONLY view of the fleet: fleet-status.json (a status generator rewrites it every 60s) + the
# FLEET_MODE line of the mode file (FLEET_MD_PATH). Fail-soft by contract: a missing,
# unreadable, garbage or stale source yields {"error": ...} / stale=True, NEVER an exception (so /state
# can't 500 over it). Cached ~15s so the 5s /state poll doesn't re-read the files on every hit.

# FLEET_STATUS_PATH / FLEET_MD_PATH come from site.env; unset means the fleet view reports "not configured".
FLEET_STATUS_DEFAULT = ""
FLEET_MD_DEFAULT = ""
# The session-supervisor's own per-session view (resurrect attempts, give-ups, stale beats,
# stuck dialogs), rewritten every sweep (~30s). Optional: if it's missing, no flags are shown.
SUPERVISOR_STATUS_DEFAULT = ""   # SUPERVISOR_STATUS_PATH (site.env); unset means no flags
SUPERVISOR_STALE_S = 180
SILENT_S = 3 * 3600   # Tower: busy this long with no STATE.md/TASKS.md edit = "silent while busy"
FLEET_STALE_S = 300
FLEET_CACHE_S = 15
FLEET_MAX_AGENTS = 200
# context_pct is the Claude Code pane's "Context N% (Xk/1.0M)", converted to tokens and shown against the fleet
# ceiling (FLEET_CTX_CEILING).
# Limitation: assumes every pane's window is 1.0M; if that stops holding, have the status generator emit the
# token count and read it here instead.
FLEET_CTX_WINDOW = 1_000_000
FLEET_CTX_CEILING = 200_000
FLEET_CTX_SOFT = 150_000
_MODE_RE = re.compile(r"^FLEET_MODE: (high|normal|lean|min|zero)$", re.M)
_MODEL_RE = re.compile(r"^(?:claude-)?(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?(?:-\d{8})?$")
_fleet_lock = threading.Lock()
_fleet_cache = {"key": None, "at": 0.0, "val": None}


def fleet_mode(text):
    """The FLEET_MODE value from FLEET.md text, or None when no line matches exactly."""
    m = _MODE_RE.search(text or "")
    return m.group(1) if m else None


def _ts(v):
    """Epoch seconds from an epoch number or an ISO-8601 string; None if unparseable."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) / 1000.0 if v > 1e12 else float(v)   # tolerate epoch-ms
    if isinstance(v, str) and v.strip():
        try:
            d = datetime.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.timestamp()
    return None


def _scalar(v):
    """Agent-controlled values ride /state as plain scalars only (the page escapes them all)."""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return str(v)[:200]


def _first(a, *names):
    for n in names:
        if a.get(n) is not None:
            return a.get(n)
    return None


def model_label(m):
    """Short human label for a model id: claude-opus-5-5 -> 'opus 5.5', claude-sonnet-5 -> 'sonnet 5',
    local-vllm -> 'local', anything deepseek -> 'deepseek'. Unknown ids pass through (clipped)."""
    if not isinstance(m, str) or not m.strip():
        return None
    m = m.strip().lower()
    hit = _MODEL_RE.match(m)
    if hit:
        return hit.group(1) + " " + hit.group(2) + ("." + hit.group(3) if hit.group(3) else "")
    if m.startswith("local") or "vllm" in m:
        return "local"
    if "deepseek" in m:
        return "deepseek"
    return m[:24]


def ctx_tokens(v):
    """Context in tokens from a percent of FLEET_CTX_WINDOW (37, 37.5, '37%'); None if not a percent."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip()
        try:
            v = float(v)
        except ValueError:
            return None
    if not isinstance(v, (int, float)) or not 0 <= v <= 100:
        return None
    return int(round(v * FLEET_CTX_WINDOW / 100.0))


def ledger_excerpt(s):
    """The STATE.md line the generator picked, minus list/quote markers so it reads as a sentence."""
    if s is None:
        return None
    s = re.sub(r"^\s*(?:[-*>]\s+)+", "", str(s)).strip()
    if len(s) > 2 and s[0] == s[-1] == "_":         # a whole-line _italic_ note
        s = s[1:-1].strip()
    return s[:200] or None


def _ledger_age(cwd, now):
    """Seconds since the agent's anchors (<cwd>/STATE.md or TASKS.md, newest wins) last changed: the real
    'last update' signal, since fleet-status.json's beat_age_secs is always null. Absolute paths only;
    neither file readable gives None."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    mt = []
    for fn in ("STATE.md", "TASKS.md"):
        try:
            mt.append(os.stat(os.path.join(cwd, fn)).st_mtime)
        except (OSError, ValueError):
            pass
    return _round(max(0.0, now - max(mt))) if mt else None


def _agent_row(name, a, now):
    if not isinstance(a, dict):
        a = {"status": a}
    upd = _ts(_first(a, "last_update", "updated", "updated_at", "ts", "last_seen"))
    # Real fleet-status.json rows carry `running` (bool) and `beat_age_secs` (seconds, may be null)
    beat = a.get("beat_age_secs")
    if isinstance(beat, (int, float)) and not isinstance(beat, bool):
        upd = now - max(0.0, float(beat)) if upd is None else upd
    alive = _first(a, "alive", "running")
    stood = _first(a, "stood_down", "stood-down")
    model = _scalar(_first(a, "model"))
    ctx = _first(a, "context", "context_pct", "ctx")
    return {
        "name": _scalar(_first(a, "name", "agent", "id") or name),
        "model": model,
        "model_label": model_label(model),
        "role": _scalar(_first(a, "role")),
        "effort": _scalar(_first(a, "effort")),
        "ctx_tokens": ctx_tokens(ctx),
        "ledger": ledger_excerpt(_first(a, "status", "state")),
        "ledger_age": _ledger_age(a.get("cwd"), now),
        "alive": bool(alive) if alive is not None else None,
        "stood_down": bool(stood) if stood is not None else None,
        "status": _scalar(_first(a, "status", "state")),
        "context": _scalar(ctx),
        "update_age": _round(max(0.0, now - upd)) if upd is not None else None,
    }


def _read_fleet(status_path, md_path, now):
    out = {"mode": None}
    try:
        with open(md_path, encoding="utf-8", errors="replace") as f:
            out["mode"] = fleet_mode(f.read())
        if out["mode"] is None:
            out["mode_error"] = "no FLEET_MODE line"
    except OSError as e:
        out["mode_error"] = "FLEET.md unreadable: " + (e.strerror or type(e).__name__)
    try:
        mtime = os.stat(status_path).st_mtime
        with open(status_path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        out["error"] = "fleet-status.json unreadable: " + (e.strerror or type(e).__name__)
        return out
    except ValueError:
        out["error"] = "fleet-status.json is not valid JSON"
        return out
    if not isinstance(data, dict):
        out["error"] = "fleet-status.json is not a JSON object"
        return out
    file_age = max(0.0, now - mtime)
    gen = _ts(_first(data, "generated_at", "generated", "ts", "updated"))
    agents = data.get("agents")
    if isinstance(agents, dict):
        pairs = list(agents.items())
    elif isinstance(agents, list):
        pairs = [(None, a) for a in agents]
    else:
        pairs = []
    out.update({
        "file_age": _round(file_age),
        "generated_age": _round(max(0.0, now - gen)) if gen is not None else _round(file_age),
        "stale": file_age > FLEET_STALE_S,
        "ceiling": FLEET_CTX_CEILING,
        "soft": FLEET_CTX_SOFT,
        "agents": [_agent_row(n, a, now) for n, a in pairs[:FLEET_MAX_AGENTS]],
    })
    flags, out["supervisor"], live = supervisor_flags(
        os.environ.get("SUPERVISOR_STATUS_PATH") or SUPERVISOR_STATUS_DEFAULT, now)
    for a in out["agents"]:
        a["flags"] = flags.get(a["name"], [])
        busy, since = live.get(a["name"], (None, None))
        a["busy"] = busy                                   # Tower: null = no pane verdict (no tmux pane / unknown)
        a["busy_age"] = _round(max(0.0, now - since)) if busy and since is not None else None
        la = a["ledger_age"]
        a["silent"] = bool(a["busy_age"] is not None and a["busy_age"] > SILENT_S
                           and (la is None or la > SILENT_S))
    return out


def supervisor_flags(path, now):
    """Supervisor status.json -> ({agent name: [{"t": text, "sev": "bad"|"warn"}]}, meta, live)
    where live = {agent name: (busy bool|None, busy_since epoch|None)} (Tower).
    Fail-soft like the rest of the fleet view: a missing file is normal (not deployed / not this site) and
    yields ({}, None, {}); unreadable or garbage yields ({}, {"error": ...}, {}). Flags and live are only built
    from a fresh file, so a dead supervisor can't leave old alarms or busy dots on screen (meta says stale)."""
    try:
        mtime = os.stat(path).st_mtime
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, None, {}
    except (OSError, ValueError) as e:
        return {}, {"error": "supervisor status unreadable: " + type(e).__name__}, {}
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, dict):
        return {}, {"error": "supervisor status has no sessions"}, {}
    gen = _ts(data.get("generated_at"))
    age = max(0.0, now - (gen if gen is not None else mtime))
    meta = {"age": _round(age), "stale": age > SUPERVISOR_STALE_S, "tmux_server_up": data.get("tmux_server_up")}
    if meta["stale"]:
        return {}, meta, {}
    out, live = {}, {}
    for name, st in list(sessions.items())[:FLEET_MAX_AGENTS]:
        if not isinstance(st, dict) or st.get("stood_down") is True:
            continue
        b = st.get("busy")
        if isinstance(b, bool) and st.get("alive") is True:
            live[str(name)] = (b, _ts(st.get("busy_since")) if b else None)
        flags = []
        n = st.get("attempts")
        if st.get("escalated") is True:
            flags.append({"t": "supervisor gave up resurrecting", "sev": "bad"})
        elif st.get("alive") is False and isinstance(n, int) and not isinstance(n, bool) and n > 0:
            flags.append({"t": "resurrecting, attempt %d" % n, "sev": "warn"})
        if st.get("prompt_dialog") is True:
            flags.append({"t": "stuck on a dialog", "sev": "bad"})
        if st.get("probe_failed") is True:
            flags.append({"t": "health probe unanswered", "sev": "warn"})
        since = _ts(st.get("automode_stop_since"))
        if since is not None:
            flags.append({"t": "auto-mode stopped %dm ago" % (max(0.0, now - since) // 60), "sev": "warn"})
        if st.get("beat_paged") is True:
            flags.append({"t": "heartbeat stale", "sev": "warn"})
        if flags:
            out[str(name)] = flags
    return out, meta, live


def fleet_snapshot(now=None):
    """Cached, fail-soft fleet view for /state. Paths are env-configurable (FLEET_STATUS_PATH,
    FLEET_MD_PATH); the cache is keyed on them so a path change takes effect immediately."""
    now = time.time() if now is None else now
    key = (os.environ.get("FLEET_STATUS_PATH") or FLEET_STATUS_DEFAULT,
           os.environ.get("FLEET_MD_PATH") or FLEET_MD_DEFAULT)
    with _fleet_lock:
        c = _fleet_cache
        if c["key"] == key and c["val"] is not None and 0 <= now - c["at"] < FLEET_CACHE_S:
            return c["val"]
    try:
        val = _read_fleet(key[0], key[1], now)
    except Exception as e:   # belt-and-braces: the fleet view must never take /state down
        val = {"mode": None, "error": "fleet read failed: " + type(e).__name__}
    with _fleet_lock:
        _fleet_cache.update(key=key, at=now, val=val)
    return val


# ---------------------------------------------------------------- snapshot


HISTORY_PAGE = 25


def hist_param(path):
    """`?hist=N` from a /state request path -> how many history rows to send, clamped to
    [HISTORY_PAGE, stream.HISTORY_MAX]; absent/garbage -> HISTORY_PAGE."""
    m = re.search(r"[?&]hist=(\d{1,4})(?:&|$)", path or "")
    return min(max(int(m.group(1)), HISTORY_PAGE), stream.HISTORY_MAX) if m else HISTORY_PAGE


def snapshot(broker, cap=None, hist=HISTORY_PAGE):
    """Build the JSON-safe state view. Pure read: takes each lock briefly and NEVER holds two at once
    (coalescer.lock and adapter._lock are acquired in separate blocks; holding both would deadlock
    against any path that takes them in the other order)."""
    now = time.time()

    with broker.coalescer.lock:
        live = [_live_view(key, w, now) for key, w in broker.coalescer.live.items()]
        pending = [_pending_view(key, p, now) for key, p in broker.coalescer.pending.items()]

    workers = [_worker_view(key, wk, now) for key, wk in _worker_items(broker)]

    hist_all = stream.history() if stream.enabled() else []
    return {
        "ok": True,
        "now": now,
        "stream_enabled": stream.enabled(),      # page shows per-row stream links only when on
        "concurrency": {"cap": cap, "in_use": len(workers)},
        "live": sorted(live, key=lambda r: r["key"]),
        "pending": sorted(pending, key=lambda r: r["key"]),
        "workers": sorted(workers, key=lambda r: r["key"]),
        # Previous (and current) stream sessions, newest first: key, 8-char prefix and start ts only.
        # Capped server-side (newest `hist`, default 25) so the 5s poll stays light; the page asks for
        # more via /state?hist=N. history_total = how many exist.
        "history": hist_all[:max(1, int(hist))],
        "history_total": len(hist_all),
        "counters": {
            "deduped": getattr(broker.coalescer, "deduped", 0),
            "dedup_cleared": getattr(broker.coalescer, "dedup_cleared", 0),
            "echo_dropped": getattr(broker.echo_guard, "dropped", 0),
            "allowlist_rejected": getattr(broker, "rejected", 0),
        },
        "fleet": fleet_snapshot(now),   # fail-soft, cached
    }


def _live_view(key, w, now):
    return {"key": key, "state": w.get("state"), "forwards": w.get("forwards", 0),
            "events": len(w.get("events") or []), "buffered": len(w.get("fwd_buffer") or []),
            "expire_in": _round(max(0.0, (w.get("expire") or now) - now))}


def _pending_view(key, p, now):
    return {"key": key, "events": len(p.get("events") or []),
            "spawn_in": _round(max(0.0, (p.get("spawn_at") or now) - now))}


def _worker_items(broker):
    adapter = getattr(broker, "adapter", None)
    lock = getattr(adapter, "_lock", None)
    if lock is None:
        return []
    with lock:
        return list(adapter._workers.items())


def _worker_view(key, wk, now):
    ts = getattr(wk, "turn_started", None)
    sid = getattr(wk, "session_id", None) or ""
    return {"key": key, "run_id": getattr(wk, "run_id", None), "session": sid[:8],
            "turns": getattr(wk, "turns", 0), "tokens": getattr(wk, "tokens", 0),
            "forwards": getattr(wk, "forwards", 0), "in_turn": bool(getattr(wk, "in_turn", False)),
            "done": bool(getattr(wk, "done", False)), "turn_age": _round(now - ts) if ts else None}


# What GET /runs/<subject_key> answers: the coalescer's view of one subject, newest phase first.
_RUN_STATUS = {"open_running": "running", "open_idle": "idle", "closing": "closing", "closed": "closing"}
_RUN_KEY_RE = re.compile(r"[\x21-\x7e]{1,512}")   # printable ASCII, no spaces; subject keys are never longer


def run_status(broker, key):
    """(http code, body) for GET /runs/<key>. Pure read, same lock discipline as snapshot (one lock at a time).
    status: pending (collecting a burst), running / idle / closing (a worker is live), completed (finished cleanly
    and not re-armed since), else 404 unknown. completed_at is kept for the last MAX_COMPLETED subjects only."""
    if not isinstance(key, str) or not _RUN_KEY_RE.fullmatch(key):
        return 400, {"ok": False, "error": "bad subject key"}
    now = time.time()
    c = broker.coalescer
    with c.lock:
        w, p, done = c.live.get(key), c.pending.get(key), c.completed.get(key)
        live = _live_view(key, w, now) if w else None
        pend = _pending_view(key, p, now) if p else None
    worker = next((_worker_view(k, wk, now) for k, wk in _worker_items(broker) if k == key), None)
    if live:
        status = _RUN_STATUS.get(live["state"], live["state"])
    elif pend:
        status = "pending"
    elif done:
        status = "completed"
    else:
        return 404, {"ok": False, "subject_key": key, "status": "unknown"}
    return 200, {"ok": True, "subject_key": key, "status": status, "row": (getattr(broker, "route_stamps", None) or {}).get(key),
                 "live": live, "pending": pend, "worker": worker, "completed_at": done}


def _round(v):
    return None if v is None else round(float(v), 1)


# ---------------------------------------------------------------- page

# Self-contained: inline CSS + JS, no build step, no CDN. Auto-refreshes /state every 5s.
# Limitation: a plain fetch-and-repaint loop, not a websocket. At 5s on a table this size the
# difference is invisible, and a socket is another thing to keep alive across broker restarts.
#
# Human-first layout: session list (sidebar) plus session detail; the SSE
# transcript renders as a chat/timeline. Assistant text is markdown (escape-FIRST renderer: every byte of
# transcript content goes through esc() before any tag is added, links are http(s)-only), tool calls as
# collapsed cards (name + short args summary) with their result folded in, errors highlighted. The raw
# jsonl stays one click away (Raw toggle). Light/dark/auto theme. Server side (auth, /state, /stream)
# is untouched.
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>eph-broker</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%231c232b'/%3E%3Cpath d='M16 8.5 23.5 16 16 23.5 8.5 16z' fill='%23fff'/%3E%3C/svg%3E">
<style>
  :root{
    color-scheme:light;
    --bg:#f2f4f6; --panel:#ffffff; --panel2:#f0f2f5; --ink:#1c232b; --muted:#5d6875; --faint:#939ca7;
    --line:#e1e5ea; --accent:#36659b; --green:#2f8a57; --amber:#b26b00; --red:#c23b30;
    --user:#edf2f8; --code:#f3f5f7;
    --shadow:0 1px 2px rgba(28,35,43,.04);
    --r:12px;
  }
  :root[data-theme=dark]{
    color-scheme:dark;
    --bg:#161a1f; --panel:#1d2229; --panel2:#252b33; --ink:#e4e8ed; --muted:#97a1ad; --faint:#69737f;
    --line:#2c333c; --accent:#8fb4e0; --green:#5cc08a; --amber:#e2a446; --red:#f07c70;
    --user:#222c38; --code:#191d23;
    --shadow:none;
  }
  @media (prefers-color-scheme:dark){
    :root:not([data-theme=light]){
      color-scheme:dark;
      --bg:#161a1f; --panel:#1d2229; --panel2:#252b33; --ink:#e4e8ed; --muted:#97a1ad; --faint:#69737f;
      --line:#2c333c; --accent:#8fb4e0; --green:#5cc08a; --amber:#e2a446; --red:#f07c70;
      --user:#222c38; --code:#191d23;
      --shadow:none;
    }
  }
  *{box-sizing:border-box}
  [hidden]{display:none !important}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
       background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
  code,pre,.mono{font-family:ui-monospace,SFMono-Regular,"JetBrains Mono",Menlo,monospace}
  button{font:inherit;color:inherit}

  /* ---- top bar */
  .top{position:sticky;top:0;z-index:5;backdrop-filter:saturate(1.4) blur(12px);
       background:color-mix(in srgb,var(--bg) 85%,transparent);border-bottom:1px solid var(--line)}
  .top-in{max-width:1440px;margin:0 auto;padding:12px 20px;display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px}
  .brand{display:flex;align-items:center;gap:9px;font-weight:600;font-size:15px;letter-spacing:-.01em}
  .logo{width:22px;height:22px;border-radius:6px;display:grid;place-items:center;color:var(--panel);font-size:11px;background:var(--ink)}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--green)}
  .dot.bad{background:var(--red)}
  .chips{display:flex;flex-wrap:wrap;gap:8px;margin-left:auto;align-items:center}
  .chip{display:inline-flex;align-items:baseline;gap:5px;background:transparent;border:0;
        border-radius:999px;padding:4px 6px;font-size:12.5px;color:var(--muted)}
  .chip b{color:var(--ink);font-weight:650;font-variant-numeric:tabular-nums}
  .btn{border:1px solid var(--line);background:var(--panel);border-radius:7px;padding:5px 11px;font-size:13px;
       cursor:pointer;color:var(--ink)}
  .btn:hover{border-color:color-mix(in srgb,var(--ink) 25%,var(--line))}
  .btn:focus-visible,.cmd:focus-visible,.sess:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .btn.on{background:color-mix(in srgb,var(--accent) 10%,var(--panel));border-color:color-mix(in srgb,var(--accent) 40%,var(--line));color:var(--accent)}

  /* ---- layout */
  .app{max-width:1440px;margin:0 auto;padding:18px 20px 40px;display:grid;grid-template-columns:340px minmax(0,1fr);gap:18px;align-items:start}
  @media (max-width:900px){ .app{grid-template-columns:minmax(0,1fr)}
    /* One column: the selected session sits above the lists instead of below them */
    .app:has(#d-main:not([hidden])) .detail{order:-1} }
  .card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
  .side{display:flex;flex-direction:column;gap:14px}
  .sec-h{display:flex;align-items:center;justify-content:space-between;padding:12px 14px 6px;
         font-size:13px;color:var(--ink);font-weight:600}
  .count{font-size:12.5px;font-weight:400;color:var(--muted);font-variant-numeric:tabular-nums}
  .list{padding:4px 8px 8px;display:flex;flex-direction:column;gap:4px}
  /* Previous sessions scroll inside their card instead of running the page on forever */
  #h-list{max-height:min(46vh,420px);overflow-y:auto;overscroll-behavior:contain}
  .empty{padding:10px 8px 14px;color:var(--faint);font-size:13px}
  .err-banner{max-width:1440px;margin:12px auto 0;padding:10px 14px;border-radius:12px;font-size:13px;
              border:1px solid color-mix(in srgb,var(--red) 50%,var(--line));color:var(--red);
              background:color-mix(in srgb,var(--red) 8%,var(--panel))}

  /* ---- session list */
  .sess{display:flex;flex-direction:column;gap:3px;width:100%;text-align:left;background:transparent;
        border:1px solid transparent;border-radius:11px;padding:9px 10px;cursor:pointer}
  .sess:hover{background:var(--panel2)}
  .sess.sel{background:color-mix(in srgb,var(--accent) 8%,var(--panel));border-color:color-mix(in srgb,var(--accent) 30%,var(--line))}
  .sess-top{display:flex;align-items:center;gap:8px;min-width:0}
  .sname{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
  .src{font-size:12.5px;color:var(--muted)}
  .sess-meta{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
  .qrow{display:flex;flex-direction:column;padding:7px 10px;font-size:12.5px}
  .qrow code{font-size:12px}
  .muted{color:var(--muted);font-size:12px}
  .tiles{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:4px 12px 12px}
  .tile{background:var(--panel2);border-radius:10px;padding:8px 10px;display:flex;flex-direction:column}
  .tile span{font-size:11.5px;color:var(--muted)}
  .tile b{font-size:17px;font-weight:650;font-variant-numeric:tabular-nums}

  /* ---- badges */
  .pill{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;font-weight:500;padding:1px 8px;border-radius:999px;
        white-space:nowrap;color:var(--muted);background:var(--panel2)}
  .pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
  .pill.run{color:var(--accent);background:color-mix(in srgb,var(--accent) 13%,transparent)}
  .pill.run::before{animation:pulse 1.4s ease-in-out infinite}
  .pill.idle,.pill.ok{color:var(--green);background:color-mix(in srgb,var(--green) 13%,transparent)}
  .pill.close,.pill.pend,.pill.warn{color:var(--amber);background:color-mix(in srgb,var(--amber) 13%,transparent)}
  .pill.bad{color:var(--red);background:color-mix(in srgb,var(--red) 13%,transparent)}
  @keyframes pulse{50%{opacity:.3}}

  /* ---- detail */
  .detail{min-height:60vh;display:flex;flex-direction:column;overflow:hidden}
  .empty-state{margin:auto;padding:60px 20px;text-align:center;color:var(--muted)}
  .empty-state b{display:block;color:var(--ink);font-size:16px;margin-bottom:4px}
  .d-head{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:flex-start;padding:16px 18px 10px}
  .d-title{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:650;letter-spacing:-.01em}
  .d-key{font-size:12px;color:var(--muted)}
  .d-actions{margin-left:auto;display:flex;gap:8px;align-items:center}
  .conn{font-size:12px;color:var(--muted);display:inline-flex;align-items:center;gap:6px}
  .conn::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--faint)}
  .conn.live::before{background:var(--green)} .conn.warn::before{background:var(--amber)} .conn.bad::before{background:var(--red)}
  .stats{display:flex;flex-wrap:wrap;gap:6px 22px;padding:0 18px 14px;border-bottom:1px solid var(--line)}
  .stat{display:flex;flex-direction:column}
  .stat span{font-size:12px;color:var(--muted)}
  .stat b{font-weight:600;font-variant-numeric:tabular-nums}

  /* ---- timeline */
  .tl,.rawlog{flex:1;overflow:auto;max-height:calc(100vh - 250px);min-height:320px;padding:16px 18px 24px}
  .tl{display:flex;flex-direction:column;gap:10px}
  .notice{color:var(--muted);font-size:13px;padding:14px;border:1px dashed var(--line);border-radius:12px}
  .ev{max-width:880px;border-radius:14px;padding:10px 14px;border:1px solid var(--line);background:var(--panel)}
  .ev.user{background:var(--user);border-color:color-mix(in srgb,var(--accent) 22%,var(--line));align-self:flex-end;max-width:760px}
  .ev.err{border-color:var(--red);background:color-mix(in srgb,var(--red) 7%,var(--panel))}
  .ev-meta{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--muted);margin-bottom:4px}
  .who{font-weight:650;color:var(--ink)}
  .ev.user .who{color:var(--accent)}
  .ts{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums;margin-left:auto;white-space:nowrap}
  .md{overflow-wrap:anywhere}
  .md p{margin:.35em 0} .md ul,.md ol{margin:.35em 0;padding-left:1.4em}
  .md h3,.md h4,.md h5,.md h6{margin:.6em 0 .3em;font-size:14.5px}
  .md blockquote{margin:.4em 0;padding-left:10px;border-left:3px solid var(--line);color:var(--muted)}
  .md code{background:var(--code);border:1px solid var(--line);border-radius:5px;padding:0 4px;font-size:12.5px}
  .md pre{background:var(--code);border:1px solid var(--line);border-radius:10px;padding:10px 12px;overflow:auto;font-size:12.5px}
  .md pre code{background:none;border:0;padding:0}
  .md a{color:var(--accent)}
  .md hr{border:0;border-top:1px solid var(--line)}
  details.tool{max-width:880px;border:1px solid var(--line);border-radius:12px;background:var(--panel2)}
  details.tool.err{border-color:var(--red);background:color-mix(in srgb,var(--red) 7%,var(--panel2))}
  details.tool > summary{list-style:none;display:flex;align-items:center;gap:9px;padding:7px 12px;cursor:pointer;font-size:13px;min-width:0}
  details.tool > summary::-webkit-details-marker{display:none}
  details.tool > summary::after{content:"›";color:var(--faint);transition:transform .15s}
  details.tool[open] > summary::after{transform:rotate(90deg)}
  .ticon{width:20px;height:20px;border-radius:5px;display:grid;place-items:center;font-size:11px;flex:none;
         background:color-mix(in srgb,var(--accent) 12%,transparent);color:var(--accent)}
  details.tool.err .ticon{background:color-mix(in srgb,var(--red) 16%,transparent);color:var(--red)}
  details.think .ticon{background:color-mix(in srgb,var(--amber) 16%,transparent);color:var(--amber)}
  .tname{font-weight:600;white-space:nowrap}
  .targs{color:var(--muted);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
         overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
  .tbody{padding:4px 12px 12px;border-top:1px solid var(--line)}
  .tlabel{font-size:12px;font-weight:600;color:var(--muted);margin:8px 0 4px}
  .tpre{margin:0;background:var(--code);border:1px solid var(--line);border-radius:9px;padding:9px 11px;
        max-height:360px;overflow:auto;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere}
  .tpre.bad{border-color:color-mix(in srgb,var(--red) 55%,var(--line));color:var(--red)}
  .meta-row{display:flex;align-items:baseline;gap:8px;font-size:12px;color:var(--muted);padding:0 4px;min-width:0}
  .meta-row.err{color:var(--red)}
  .mlabel{font-weight:600;white-space:nowrap}
  .mtext{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
  /* Human-readable tool cards (key/value tables, diffs, collapsed JSON details) */
  table.kv{border-collapse:collapse;width:100%;font-size:12.5px;margin:2px 0}
  table.kv th{text-align:left;vertical-align:top;font-weight:600;color:var(--muted);padding:3px 12px 3px 0;white-space:nowrap;width:1%}
  table.kv td{padding:3px 0;vertical-align:top;overflow-wrap:anywhere;min-width:0}
  table.kv tr+tr>*{border-top:1px solid var(--line)}
  table.kv table.kv{font-size:12px}
  .kvc{font-size:12px}
  details.jd{margin:2px 0}
  details.jd > summary{cursor:pointer;color:var(--muted);font-size:12px}
  details.jd > .tpre,details.jd > table.kv{margin-top:4px}
  .fpath{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;font-weight:600;margin:8px 0 2px;overflow-wrap:anywhere}
  .tdesc{color:var(--muted);font-size:12.5px;margin-top:8px}
  .tpre.diff span{display:block;min-height:1.2em}
  .tpre.diff .del{color:var(--red);background:color-mix(in srgb,var(--red) 8%,transparent)}
  .tpre.diff .add{color:var(--green);background:color-mix(in srgb,var(--green) 8%,transparent)}
  details.harness{font-size:12px;color:var(--muted);padding:0 4px}
  details.harness summary{cursor:pointer;color:var(--faint)}
  details.harness .ln{padding:1px 0 1px 16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px}
  /* JSON message bodies as structured cards */
  .jcard{display:flex;flex-direction:column;gap:2px;min-width:0}
  .jchips{display:flex;flex-wrap:wrap;gap:6px;margin:2px 0 4px}
  .jchips .chip{padding:2px 10px;max-width:100%;overflow-wrap:anywhere}
  .chip a{color:var(--accent);font-weight:650}
  .jcard details.jd{border-left:2px solid var(--line);padding-left:8px}
  /* Tool results: one-line fact in the card header, entity chips, folded empty/default fields */
  .tres{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:40%;flex:none}
  .ents{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:flex-start}
  .ents > details.jd{margin:0}
  details.ent > summary{color:var(--ink);font-weight:600}
  details.blank > summary{color:var(--faint);font-style:italic}
  details.blank table.kv{opacity:.75}
  .rawbtn{padding:1px 8px;font-size:11px}
  .btn.more{margin:4px 2px 2px;align-self:flex-start}
  .rawlog{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
  .rawlog .rl{white-space:pre-wrap;word-break:break-all;padding:4px 0;border-bottom:1px dashed var(--line)}
  /* Fleet tab = mode ladder + tallies, then a roster: one dense row per running agent (liveness
     rail, name/role, model, context meter vs the 200k fleet ceiling, STATE.md headline + its age); stood-down
     agents fold into one collapsed group. Rows expand in place to show the full ledger line. */
  .tabs{display:flex;gap:4px}
  body.on-fleet .chip.wk{display:none}
  .fleet{max-width:1440px;margin:0 auto;padding:16px 20px 40px;display:flex;flex-direction:column;gap:14px}
  .fsum{display:flex;flex-wrap:wrap;align-items:center;gap:12px 26px;padding:2px 2px 0}
  .ladder{display:flex;align-items:center;gap:10px}
  .ladder-l{font-size:12.5px;color:var(--muted)}
  .rungs{display:inline-flex;gap:2px;padding:2px;border-radius:8px;background:var(--panel2)}
  .rung{padding:3px 10px;font-size:12.5px;color:var(--faint);border-radius:6px}
  .rung.on{display:inline-flex;align-items:center;gap:6px;color:var(--ink);background:var(--panel);font-weight:600;
           box-shadow:0 0 0 1px var(--line)}
  .rung.on::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--mc)}
  .tally{order:1;font-size:15px;color:var(--muted);flex-basis:100%;margin-top:-8px}
  .fresh{margin-left:auto;font-size:12px;color:var(--muted);display:flex;gap:8px;align-items:center}
  .ag,.rhead{display:grid;grid-template-columns:minmax(150px,210px) 96px minmax(150px,200px) minmax(0,1fr);
             gap:0 20px;align-items:center;padding:0 18px 0 20px}
  .rhead{padding-top:10px;padding-bottom:8px;font-size:12px;color:var(--muted)}
  .ag{position:relative;padding-top:11px;padding-bottom:11px;border-top:1px solid color-mix(in srgb,var(--line) 70%,transparent);cursor:pointer;outline:none}
  .ag:hover{background:color-mix(in srgb,var(--panel2) 55%,transparent)}
  .ag:focus-visible{box-shadow:inset 0 0 0 2px var(--accent)}
  .ag::before{content:"";position:absolute;left:8px;top:50%;width:6px;height:6px;margin-top:-3px;border-radius:50%;background:var(--sc)}
  .ag.up{--sc:var(--green)} .ag.down{--sc:var(--red)} .ag.unk{--sc:var(--faint)} .ag.off{--sc:var(--line)}
  .ag.down{background:color-mix(in srgb,var(--red) 6%,transparent)}
  .an{min-width:0;display:flex;flex-direction:column}
  .an-top{display:flex;align-items:center;gap:8px;min-width:0}
  .an b{font-weight:600;white-space:nowrap}
  .role{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .mdl-c{display:flex;align-items:center;gap:6px;white-space:nowrap}
  .mdl{font-size:12.5px;color:var(--muted)}
  .mdl.local{color:var(--amber)}
  .eff{font-size:11.5px;color:var(--muted)}
  .meter{display:grid;grid-template-columns:minmax(0,1fr) 40px;align-items:center;gap:10px;min-width:0}
  .track{position:relative;height:5px;border-radius:3px;background:var(--panel2)}
  .fill{position:absolute;left:0;top:0;bottom:0;border-radius:3px;background:color-mix(in srgb,var(--ink) 35%,var(--panel2))}
  .fill.warm{background:var(--amber)} .fill.hot{background:var(--red)}
  .tick{position:absolute;top:-3px;bottom:-3px;width:1px;background:var(--ink);opacity:.35}
  .mval{font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--muted)}
  .mval.warm{color:var(--amber)} .mval.hot{color:var(--red)}
  .meter.none{display:block;font-size:12px;color:var(--faint)}
  .led{min-width:0;font-size:13px;line-height:1.45;color:color-mix(in srgb,var(--ink) 80%,var(--muted));overflow:hidden;
       overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
  .ag.open .led{display:block;color:var(--ink)}
  .led i{font-style:normal;color:var(--faint)}
  .lage{display:inline-block;margin-right:8px;font-size:12.5px;color:var(--faint);white-space:nowrap;font-variant-numeric:tabular-nums}
  .lage.stale{color:var(--amber)} .lage.old{color:var(--faint)}
  @media (prefers-reduced-motion:reduce){ .pill.run::before{transition:none;animation:none} }
  /* Tower: "who needs me" first. Headline counts, NEEDS YOU cards (down / supervisor alarm / over the ceiling /
     busy but silent), then the roster split IN FLIGHT (pane mid-turn) vs PARKED; side column = context runway,
     eph workers, hangar (stood down). */
  .fhead{margin:0;font-size:30px;line-height:1.15;font-weight:650;letter-spacing:-.025em;white-space:nowrap}
  .tower{display:grid;grid-template-columns:minmax(0,1fr) minmax(300px,380px);gap:16px;align-items:start}
  .tw-main,.tw-side{display:flex;flex-direction:column;gap:14px;min-width:0}
  .grp{font-size:13px;font-weight:600;color:var(--ink);padding:2px 2px 10px}
  .grp span{font-weight:400;color:var(--muted);margin-left:4px;font-variant-numeric:tabular-nums}
  .roster .grp{padding:14px 20px 8px;border-top:1px solid var(--line)}
  .roster .grp:first-child{border-top:0}
  .needs{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:12px}
  .need-c{padding:14px 16px 14px;display:flex;flex-direction:column;gap:8px;min-width:0;box-shadow:none;
          background:color-mix(in srgb,var(--sc) 5%,var(--panel));border-color:color-mix(in srgb,var(--sc) 28%,var(--line))}
  .need-c.bad{--sc:var(--red)} .need-c.warn{--sc:var(--amber)}
  .need-h{display:flex;flex-direction:column;gap:1px}
  .need-h b{font-size:16px;font-weight:600}
  .need-h span{font-size:13px;font-weight:500;color:var(--sc)}
  .need-c p{margin:0;font-size:13px;line-height:1.45;color:color-mix(in srgb,var(--ink) 80%,var(--muted));overflow-wrap:anywhere}
  .need-c p i{font-style:normal;color:var(--muted)}
  .need-f{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin-top:auto;font-size:12px;color:var(--muted)}
  .need-f .when{margin-left:auto;font-variant-numeric:tabular-nums}
  .cmd{font:12px/1 ui-monospace,SFMono-Regular,Menlo,monospace;padding:5px 8px;border:1px solid var(--line);border-radius:6px;
       background:var(--panel);cursor:copy;user-select:all;white-space:nowrap}
  .cmd:hover{border-color:color-mix(in srgb,var(--accent) 45%,var(--line))}
  .cmd.copied{color:var(--green);border-color:var(--green)}
  .ag.busy{--sc:var(--accent)}
  .busy-l{font-size:11.5px;color:var(--accent);white-space:nowrap;font-variant-numeric:tabular-nums}
  .side-c{padding:14px 18px 16px;box-shadow:none}
  .side-h{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding-bottom:10px;font-size:13px;font-weight:600;color:var(--ink)}
  .side-h span+span{font-size:12.5px;font-weight:400;color:var(--muted)}
  .rw{display:grid;grid-template-columns:minmax(0,120px) minmax(0,1fr) 44px;gap:10px;align-items:center;padding:3px 0;font-size:12.5px}
  .rw .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .rw .track .soft{position:absolute;top:-2px;bottom:-2px;width:1px;background:var(--ink);opacity:.18}
  .rw .mval{text-align:right}
  .wk-r{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 10px;padding:7px 0;border-top:1px solid color-mix(in srgb,var(--line) 70%,transparent);font-size:13px}
  .wk-r:first-child{border-top:0}
  .wk-r .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap} .wk-r .t i{font-style:normal;color:var(--faint)}
  .wk-r .a{color:var(--muted);font-variant-numeric:tabular-nums}
  .hangar{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px;color:var(--muted)}
  @media (max-width:1100px){ .tower{grid-template-columns:minmax(0,1fr)} }
  /* Mobile: header wraps cleanly with tappable tabs; each Fleet agent becomes a 3-line card
     (name + model, then the context meter, then the ledger line + its age) */
  @media (max-width:700px){
    .top-in{padding:10px 12px;gap:8px 10px}
    .tabs{margin-left:auto}
    .tabs .btn{min-height:40px;min-width:64px;padding:8px 14px;font-size:14px}
    .chips{margin-left:0;width:100%;gap:6px}
    .chips .btn{min-height:32px}
    .app,.fleet{padding:12px 12px 30px}
    .app{grid-template-columns:minmax(0,1fr)}
    .err-banner{margin:12px 12px 0}
    .qrow code{overflow-wrap:anywhere}
    .d-head,.stats{padding-left:14px;padding-right:14px}
    .tl,.rawlog{padding:12px 12px 20px}
    .fresh{margin-left:0;width:100%}
    .fhead{font-size:21px;white-space:normal}
    .rhead{display:none}
    .ag{grid-template-columns:minmax(0,1fr) auto;gap:7px 12px;padding:11px 14px 11px 16px}
    .ag:first-of-type{border-top:0}
    .mdl-c{grid-row:1;grid-column:2;align-self:start}
    .meter{grid-column:1 / -1;grid-template-columns:minmax(0,1fr) 84px}
    .led{grid-column:1 / -1}
    /* Tower on a phone = one screen, not three (too much scrolling otherwise): theme button joins the top row,
       summary shrinks to two lines (headline, live mode rung; the tally sentence and the needs-you label hide),
       needs-you cards lose the ledger + tmux chip,
       roster rows are one line (name · mini meter · model; tap a row for role + ledger), context runway is
       dropped (every row has its meter), hangar is a single scrolling strip. */
    body.on-fleet .chips{width:auto}
    body.on-fleet .top-in{flex-wrap:nowrap}
    body.on-fleet #theme{min-width:40px;min-height:40px;padding:0;font-size:16px}
    body.on-fleet .th-l{display:none}
    .fleet{gap:10px;padding-top:10px}
    .fsum{gap:6px 14px}
    .fhead{font-size:19px;width:100%}
    .tally{gap:2px 12px}
    .tally,#f-needs-w > .grp{display:none}   /* the headline + cards + roster already say it */
    .ladder-l{display:none}
    .rungs .rung:not(.on){display:none}
    .rung{padding:2px 9px;font-size:12px}
    .fresh{width:auto;margin-left:auto;font-size:11px;gap:6px}
    .fresh .pill{font-size:11px}
    .tower,.tw-main,.tw-side{gap:10px}
    .needs{gap:8px}
    .need-c{padding:9px 12px 10px;gap:3px}
    .need-h{flex-direction:row;flex-wrap:wrap;align-items:baseline;gap:0 8px;padding-right:64px}
    .need-h b{font-size:15px} .need-h span{font-size:12.5px}
    .need-c p{font-size:12.5px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
    .need-c p.nl,.need-f .cmd{display:none}
    .need-f{margin-top:0}
    .need-f .when{position:absolute;right:12px;top:10px}
    .need-c{position:relative}
    .roster .grp{padding:10px 14px 6px 22px}
    .ag{grid-template-columns:minmax(0,1fr) 108px auto;gap:2px 10px;padding:9px 12px 9px 22px;min-height:44px}
    .ag .role{display:none}
    .ag .meter{grid-column:2;grid-row:1;grid-template-columns:minmax(0,1fr) 34px;gap:6px}
    .ag .meter.none{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .ag .mval{font-size:12px;text-align:right}
    .ag .mdl-c{grid-column:3;grid-row:1;align-self:center}
    .ag .mdl{font-size:11px;padding:1px 6px}
    .ag .eff{display:none}
    .ag .led{display:none;grid-row:2;margin-top:4px}
    .ag.open .led{display:block}
    .ag.open .role{display:block;white-space:normal}
    #f-runway-c{display:none}
    .side-c{padding:9px 12px 10px}
    .side-h{padding-bottom:4px}
    .wk-r{padding:5px 0}
    .hangar{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;padding-bottom:2px}
    .hangar span{flex:none}
    footer{padding-bottom:16px}
  }
  footer{max-width:1440px;margin:0 auto;padding:0 20px 30px;font-size:11.5px;color:var(--faint);text-align:center}
</style>
</head>
<body>
<div class="top"><div class="top-in">
  <div class="brand"><span class="logo">◆</span>eph-broker <span class="dot" id="dot" title="state feed"></span></div>
  <div class="tabs">
    <button class="btn on" id="tab-workers" data-tab="workers">Workers</button>
    <button class="btn" id="tab-fleet" data-tab="fleet">Fleet</button>
  </div>
  <div class="chips">
    <span class="chip wk">workers <b id="b-workers">n/a</b>/<b id="b-cap">n/a</b></span>
    <span class="chip wk">live <b id="b-live">n/a</b></span>
    <span class="chip wk">queued <b id="b-pending">n/a</b></span>
    <span class="chip wk">updated <b id="b-age">n/a</b></span>
    <button class="btn" id="theme" title="theme: auto / light / dark">◐ Auto</button>
  </div>
</div></div>

<div id="err" class="err-banner" style="display:none"></div>

<section class="fleet" id="fleet-view" hidden>
  <div class="fsum">
    <h1 class="fhead" id="f-head"></h1>
    <div class="tally" id="f-tally"></div>
    <div class="ladder" title="FLEET_MODE in FLEET.md, loosest to tightest"><span class="ladder-l">Fleet mode</span><span class="rungs" id="f-mode"></span></div>
    <div class="fresh" id="f-gen"></div>
  </div>
  <div class="tower">
    <div class="tw-main">
      <div id="f-needs-w" hidden><div class="grp">Needs you <span id="f-needs-n"></span></div><div class="needs" id="f-needs"></div></div>
      <div class="card roster">
        <div id="f-err" class="empty" hidden></div>
        <div id="f-rows"></div>
      </div>
    </div>
    <aside class="tw-side">
      <div class="card side-c" id="f-runway-c"><div class="side-h"><span>Context runway</span><span id="f-runway-k"></span></div><div id="f-runway"></div></div>
      <div class="card side-c"><div class="side-h"><span>Eph workers</span><span>details in Workers</span></div><div id="f-wk"></div></div>
      <div class="card side-c" id="f-hangar-c" hidden><div class="side-h"><span>Hangar</span><span>stood down</span></div><div class="hangar" id="f-hangar"></div></div>
    </aside>
  </div>
</section>

<main class="app" id="workers-view">
  <aside class="side">
    <div class="card">
      <div class="sec-h">Sessions <span class="count" id="n-sessions">0</span></div>
      <div class="list" id="s-list"></div>
    </div>
    <div class="card" id="h-card" hidden>
      <div class="sec-h">Previous sessions <span class="count" id="n-history">0</span></div>
      <div class="list" id="h-list"></div>
    </div>
    <div class="card">
      <div class="sec-h">Queued (pre-spawn) <span class="count" id="n-pending">0</span></div>
      <div class="list" id="p-list"></div>
    </div>
    <div class="card">
      <div class="sec-h">Counters</div>
      <div class="tiles" id="counters"></div>
    </div>
  </aside>

  <section class="card detail">
    <div class="empty-state" id="d-empty"><b>No session selected</b>Pick a session on the left to see its state and live transcript.</div>
    <div id="d-main" hidden style="display:flex;flex-direction:column;flex:1;min-height:0">
      <div class="d-head">
        <div>
          <div class="d-title"><span id="d-title"></span><span id="d-badge"></span></div>
          <code class="d-key" id="d-key"></code>
        </div>
        <div class="d-actions">
          <span class="conn" id="sv-st"></span>
          <button class="btn" id="raw" title="show the raw jsonl transcript">Raw</button>
          <button class="btn" id="sv-x">Close</button>
        </div>
      </div>
      <div class="stats" id="d-stats"></div>
      <div class="tl" id="sv-log"></div>
      <div class="rawlog" id="sv-raw" hidden></div>
    </div>
  </section>
</main>

<footer>
  Read-only, refreshes every 5 seconds
  <!-- Control verbs (retry / kill / close) are a separate lane: they need auth IN THE APP, not just
       at the proxy. Loopback :8787 is reachable by anything on the broker host and the Caddy IP guard is
       documented defense-in-depth only. Today every write requires HMAC; a human surface has none,
       so it must verify the proxy's username header itself. -->
</footer>

<script>
const $ = id => document.getElementById(id);

function esc(s){ return String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function clip(s, n){ s = String(s ?? ''); return s.length > n ? s.slice(0, n) + '…' : s; }
function el(tag, cls, text){
  const e = document.createElement(tag);
  if(cls) e.className = cls;
  if(text !== undefined && text !== null) e.textContent = text;   // untrusted text: textContent only
  return e;
}
function fmtNum(n){ return Number(n || 0).toLocaleString(); }
function age(s){
  if(s === null || s === undefined) return 'n/a';
  return s < 60 ? s.toFixed(1) + 's' : Math.floor(s/60) + 'm' + String(Math.floor(s%60)).padStart(2,'0') + 's';
}

// ---- theme (auto follows the OS; the choice is remembered per browser)
const THEMES = ['auto', 'light', 'dark'];
let theme = 'auto';
try{ theme = localStorage.getItem('eph-theme') || 'auto'; }catch(e){}
function applyTheme(t){
  if(t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  const [ic, lb] = t === 'light' ? ['☀', 'Light'] : t === 'dark' ? ['☾', 'Dark'] : ['◐', 'Auto'];
  $('theme').replaceChildren(ic, el('span', 'th-l', ' ' + lb));
}
applyTheme(theme);
$('theme').onclick = () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try{ localStorage.setItem('eph-theme', theme); }catch(e){}
  applyTheme(theme);
};

// ---- markdown. ESCAPE FIRST: the source is untrusted transcript text, so every fragment goes
// through esc() before any tag is added; the only tags produced are the fixed ones below, and links are
// http(s)-only (quotes are already &quot; so an href can't break out of its attribute).
// Models sometimes write literal named entities ("&rarr;", "&mdash;") instead of the character
// they mean. unent() (below, shared with sanitizeHtml) decodes those FIRST so esc() re-escapes only the
// real special chars; otherwise esc() turns the literal "&" into "&amp;" and the entity text survives
// as text instead of becoming the glyph.
function inline(t){
  const codes = [], links = [];
  const link = (href, text) => {   // href/text are already escaped; stashed so bold/em/autolink can't touch them
    links.push('<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + text + '</a>');
    return '\u0001' + (links.length - 1) + '\u0001';
  };
  t = esc(unent(t)).replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000'; });
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, a, h) => link(h, a))
       // Bare http(s) URLs (ticket refs, MR links) are clickable too; trailing punctuation stays text
       .replace(/https?:\/\/(?:[^\s<&*\u0000\u0001]|&amp;)+/g, m => {
         const u = m.replace(/[.,;:!?)\]]+$/, '');
         return link(u, u) + m.slice(u.length);
       })
       .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
       .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>');
  t = t.replace(/\u0001(\d+)\u0001/g, (_, i) => links[Number(i)]);
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => '<code>' + codes[Number(i)] + '</code>');
}
function mdBlocks(s){
  const out = [];
  let para = [], list = null;
  const flush = () => {
    if(para.length){ out.push('<p>' + para.map(inline).join('<br>') + '</p>'); para = []; }
    if(list){ out.push('<' + list.tag + '>' + list.items.map(x => '<li>' + inline(x) + '</li>').join('') + '</' + list.tag + '>'); list = null; }
  };
  for(const line of s.split('\n')){
    if(!line.trim()){ flush(); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if(h){ flush(); const n = Math.min(h[1].length + 2, 6); out.push('<h' + n + '>' + inline(h[2]) + '</h' + n + '>'); continue; }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/), ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if(ul || ol){
      const tag = ul ? 'ul' : 'ol';
      if(para.length || (list && list.tag !== tag)) flush();
      if(!list) list = {tag: tag, items: []};
      list.items.push((ul || ol)[1]);
      continue;
    }
    if(/^\s*(---|\*\*\*|___)\s*$/.test(line)){ flush(); out.push('<hr>'); continue; }
    const q = line.match(/^>\s?(.*)$/);
    if(q){ flush(); out.push('<blockquote>' + inline(q[1]) + '</blockquote>'); continue; }
    if(list) flush();
    para.push(line);
  }
  flush();
  return out.join('');
}
function md(src){
  return String(src ?? '').split('```').map((b, i) => {
    if(i % 2 === 0) return mdBlocks(b);
    const nl = b.indexOf('\n');
    const code = nl >= 0 ? b.slice(nl + 1) : b;          // first line of a fence is the language tag
    return '<pre><code>' + esc(code.replace(/\n$/, '')) + '</code></pre>';
  }).join('');
}

// ---- sanitized HTML (worker `summary`, ticket `description`). ESCAPE-FIRST like md(): the source is
// tokenised, every text run is entity-decoded then re-escaped with esc(), and only allowlisted tag NAMES are
// re-emitted, never their attributes, except a vetted http(s) href on <a>. script/style/... are dropped
// together with their content; any other tag is dropped but its text kept. Pure string code (no DOM).
const SAFE_TAGS = new Set(['p', 'ul', 'ol', 'li', 'b', 'i', 'strong', 'em', 'code', 'pre', 'a', 'br']);
const DROP_TAGS = new Set(['script', 'style', 'iframe', 'object', 'embed', 'template', 'noscript', 'textarea', 'title', 'svg', 'math']);
// Named entities a model wrote literally ("&rarr;", "&mdash;") instead of the character it meant;
// unent() decodes these (plus numeric refs &#NNN;/&#xHEX;) so inline() can esc() the real specials only;
// otherwise esc() turns the literal "&" into "&amp;" and the entity text survives as text, not a glyph.
const NAMED_ENTITY_RE = /&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos|nbsp|mdash|ndash|hellip|lsquo|rsquo|ldquo|rdquo|rarr|larr|harr|uarr|darr|bull|middot|trade|copy|reg|deg|times|divide|plusmn|sect|para);/gi;
function unent(s){
  return String(s).replace(NAMED_ENTITY_RE, (m, e) => {
    const k = e.toLowerCase();
    if(k[0] === '#'){
      const n = k[1] === 'x' ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10);
      try{ return String.fromCodePoint(n); }catch(_){ return ''; }
    }
    return {amp: '&', lt: '<', gt: '>', quot: '"', apos: "'",
      mdash: '\u2014', ndash: '–', hellip: '…', lsquo: '‘', rsquo: '’', ldquo: '“', rdquo: '”',
      rarr: '→', larr: '←', harr: '↔', uarr: '↑', darr: '↓',
      bull: '•', middot: '·', trade: '™', copy: '©', reg: '®',
      deg: '°', times: '×', divide: '÷', plusmn: '±', sect: '§', para: '¶',
      nbsp: ' '}[k];
  });
}
function sanitizeHtml(src){
  const s = String(src ?? ''), out = [];
  const re = /<!--[\s\S]*?-->|<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
  let last = 0, m, drop = null;
  while((m = re.exec(s))){
    const name = (m[2] || '').toLowerCase();
    if(drop){ if(m[1] && name === drop){ drop = null; last = re.lastIndex; } continue; }
    out.push(esc(unent(s.slice(last, m.index))));
    last = re.lastIndex;
    if(!name) continue;                                              // comment
    if(DROP_TAGS.has(name)){ if(!m[1]) drop = name; continue; }      // element AND its content
    if(!SAFE_TAGS.has(name)) continue;                               // unknown tag: keep only its text
    if(m[1]){ if(name !== 'br') out.push('</' + name + '>'); continue; }
    if(name === 'a'){
      const h = m[3].match(/\shref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i);
      const url = h ? unent(h[1] ?? h[2] ?? h[3]).trim() : '';
      out.push(/^https?:\/\/\S+$/i.test(url) ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' : '<a>');
    }else out.push('<' + name + '>');
  }
  if(!drop) out.push(esc(unent(s.slice(last))));                     // an unclosed <script> eats the rest
  return out.join('');
}
// ---- end sanitizeHtml

// ---- state view
const STATES = {open_running: ['Running', 'run'], open_idle: ['Idle', 'idle'],
                closing: ['Closing', 'close'], closed: ['Closed', 'close']};
function badge(label, cls){ return '<span class="pill ' + cls + '">' + esc(label) + '</span>'; }
function stateBadge(s){
  if(s.live){ const st = STATES[s.live.state] || [s.live.state || 'unknown', '']; return badge(st[0], st[1]); }
  if(s.worker) return s.worker.in_turn ? badge('In turn', 'run') : badge('Idle', 'idle');
  return badge('Ended', 'done');
}
function title(key){
  const p = String(key).split(':');
  if(p.length < 2) return {src: '', name: String(key)};
  const last = p[p.length - 1];
  return /^\d+$/.test(last) && p.length >= 3
    ? {src: p[0], name: p.slice(1, -1).join(' ') + ' #' + last}
    : {src: p[0], name: p.slice(1).join(':')};
}
function sessions(d){
  const m = new Map();
  for(const l of d.live) m.set(l.key, {key: l.key, live: l, worker: null});
  for(const w of d.workers){ const s = m.get(w.key) || {key: w.key, live: null, worker: null}; s.worker = w; m.set(w.key, s); }
  return [...m.values()].sort((a, b) => a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
}
// Recorded stream sessions (newest first) minus each live key's newest one, since that one IS the live row.
function previous(d){
  const live = new Set(d.live.map(l => l.key)), seen = new Set();
  return (d.history || []).filter(h => {
    if(live.has(h.key) && !seen.has(h.key)){ seen.add(h.key); return false; }
    return true;
  });
}
function when(ts){ const t = new Date(ts * 1000); return isNaN(t) ? 'n/a' : t.toLocaleString(); }

let lastState = null, selected = null, selectedPast = null;
const HIST_PAGE = 25;
let histLimit = HIST_PAGE;      // Previous sessions are paged server-side (/state?hist=N)   // selectedPast = 8-char session of a previous one

function paint(d){
  lastState = d;
  $('dot').className = 'dot';
  $('b-workers').textContent = d.concurrency.in_use;
  $('b-cap').textContent = d.concurrency.cap ?? 'n/a';
  $('b-live').textContent = d.live.length;
  $('b-pending').textContent = d.pending.length;
  $('b-age').textContent = new Date(d.now * 1000).toLocaleTimeString();

  const ss = sessions(d);
  $('n-sessions').textContent = ss.length;
  $('s-list').innerHTML = ss.length ? ss.map(s => {
    const t = title(s.key), w = s.worker, l = s.live, meta = [];
    if(w){ meta.push(w.turns + ' turns', fmtNum(w.tokens) + ' tok'); if(w.in_turn && w.turn_age != null) meta.push('turn ' + age(w.turn_age)); }
    if(l){ meta.push(l.events + ' events'); if(l.expire_in != null) meta.push('expires ' + age(l.expire_in)); }
    return '<button class="sess' + (s.key === selected && !selectedPast ? ' sel' : '') + '" data-key="' + esc(s.key) + '" title="' + esc(s.key) + '">'
      + '<span class="sess-top">' + (t.src ? '<span class="src">' + esc(t.src) + '</span>' : '')
      + '<span class="sname">' + esc(t.name) + '</span>' + stateBadge(s) + '</span>'
      + '<span class="sess-meta">' + esc(meta.join(' · ')) + '</span></button>';
  }).join('') : '<div class="empty">No live sessions right now.</div>';

  const hs = previous(d);
  $('h-card').hidden = !d.stream_enabled;
  $('n-history').textContent = d.history_total ?? hs.length;
  $('h-list').innerHTML = hs.length ? hs.map(h => {
    const t = title(h.key), sel = h.key === selected && h.session === selectedPast;
    return '<button class="sess' + (sel ? ' sel' : '') + '" data-key="' + esc(h.key) + '" data-past="' + esc(h.session)
      + '" title="' + esc(h.key) + '"><span class="sess-top">' + (t.src ? '<span class="src">' + esc(t.src) + '</span>' : '')
      + '<span class="sname">' + esc(t.name) + '</span>' + badge('Ended', 'done') + '</span>'
      + '<span class="sess-meta">' + esc('started ' + when(h.ts) + ' · ' + h.session) + '</span></button>';
  }).join('') + ((d.history_total || 0) > (d.history || []).length
    ? '<button class="btn more" id="h-more">Show ' + esc(Math.min(HIST_PAGE, d.history_total - d.history.length)) + ' more</button>' : '')
    : '<div class="empty">No previous sessions recorded yet.</div>';

  $('n-pending').textContent = d.pending.length;
  $('p-list').innerHTML = d.pending.length ? d.pending.map(p =>
    '<div class="qrow"><code>' + esc(p.key) + '</code><span class="muted">' + p.events
    + ' events · spawns in ' + age(p.spawn_in) + '</span></div>').join('') : '<div class="empty">Nothing queued.</div>';

  const c = d.counters;
  $('counters').innerHTML = [
    ['Deduped', c.deduped], ['Dedup re-armed', c.dedup_cleared],
    ['Echo dropped', c.echo_dropped], ['Allowlist rejected', c.allowlist_rejected],
  ].map(([k, v]) => '<div class="tile"><span>' + k + '</span><b>' + fmtNum(v) + '</b></div>').join('');

  paintFleet(d.fleet);
  paintDetail();
}

// ---- Fleet tab: fleet-status.json + FLEET_MODE + STATE.md age, read-only.
// Every string is agent-controlled -> esc(); numbers are formatted before they touch HTML.
const MODES = ['high', 'normal', 'lean', 'min', 'zero'];
const MODE_CLR = {high: 'var(--accent)', normal: 'var(--green)', lean: 'var(--amber)', min: 'var(--amber)', zero: 'var(--red)'};
const fleetOpen = new Set();          // agents whose ledger line is expanded; survives the 5s repaint
function ago(s){
  if(s === null || s === undefined) return 'n/a';
  return s < 60 ? 'now' : s < 3600 ? Math.floor(s / 60) + 'm' : s < 172800 ? Math.floor(s / 3600) + 'h' : Math.floor(s / 86400) + 'd';
}
function kTok(n){ return Math.round(Number(n) / 1000) + 'k'; }
function agentKind(a){ return a.stood_down ? 'off' : a.alive === false ? 'down' : a.alive === true ? 'up' : 'unk'; }
function modelFamily(a){ const m = String(a.model_label || ''); return m.startsWith('opus') ? 'opus' : m === 'local' ? 'local' : ''; }
function ctxMeter(a, ceil, soft){
  const t = a.ctx_tokens;
  if(t === null || t === undefined)
    return '<div class="meter none">' + (a.stood_down ? '' : a.model_label === 'local' ? 'local runtime, no meter' : 'no pane reading') + '</div>';
  const max = ceil * 1.5, heat = t >= ceil ? 'hot' : t >= soft ? 'warm' : '';
  const pos = v => (Math.min(v, max) / max * 100).toFixed(1) + '%';
  const pane = esc(a.context ?? '?');
  return '<div class="meter" title="' + esc(fmtNum(t)) + ' tokens in context (pane reads ' + pane + '% of 1M). Fleet ceiling '
      + esc(kTok(ceil)) + ', aim to reset by ' + esc(kTok(soft)) + '.">'
    + '<span class="track"><span class="fill ' + heat + '" style="width:' + pos(t) + '"></span>'
    + '<span class="tick" style="left:' + pos(ceil) + '"></span></span>'
    + '<span class="mval ' + heat + '">' + esc(kTok(t)) + '</span></div>';
}
function agentRow(a, ceil, soft){
  const name = String(a.name ?? '?'), k = agentKind(a), open = fleetOpen.has(name);
  const eff = a.effort && a.effort !== 'default' ? '<span class="eff">' + esc(a.effort) + '</span>' : '';
  const la = a.ledger_age;
  const busy = a.busy === true && k === 'up';
  return '<div class="ag ' + k + (busy ? ' busy' : '') + (open ? ' open' : '') + '" data-agent="' + esc(name) + '" tabindex="0" role="button" aria-expanded="' + open + '">'
    + '<div class="an"><span class="an-top"><b>' + esc(a.name ?? '?') + '</b>' + (k === 'down' ? badge('down', 'bad') : '')
    + (busy ? '<span class="busy-l" title="pane mid-turn (busy turn) per the supervisor">busy ' + esc(a.busy_age == null ? '' : ago(a.busy_age)) + '</span>' : '')
    + (a.flags || []).map(x => badge(x.t, x.sev === 'bad' ? 'bad' : 'warn')).join('') + '</span>'
    + '<span class="role" title="' + esc(a.role || '') + '">' + esc(a.role || '') + '</span></div>'
    + '<div class="mdl-c"><span class="mdl ' + modelFamily(a) + '" title="' + esc(a.model ?? 'unknown model') + '">'
    + esc(a.model_label || a.model || 'n/a') + '</span>' + eff + '</div>'
    + ctxMeter(a, ceil, soft)
    + '<div class="led">' + (la == null ? '' : '<span class="lage' + (la > 86400 ? ' old' : la > 21600 ? ' stale' : '')
      + '" title="STATE.md last edited ' + esc(ago(la)) + ' ago">' + esc(ago(la)) + '</span>')
    + (a.ledger ? esc(a.ledger) : '<i>no STATE.md headline</i>') + '</div></div>';
}
// Supervisor trouble sorts with the down agents (bad) or just after them (warn)
function rank(a){
  const f = a.flags || [];
  return a.alive === false || f.some(x => x.sev === 'bad') ? 0 : f.length ? 1 : 2;
}
// Tower: why an agent needs a human now -> {sev, head, why} or null. Order = most urgent first.
function needWhy(a, ceil){
  if(a.stood_down) return null;
  const bad = (a.flags || []).filter(x => x.sev === 'bad');
  if(a.alive === false) return {sev: 'bad', head: 'Down', why: bad.length ? bad.map(x => x.t).join('; ')
    : 'Pane gone and the supervisor has not brought it back.'};
  if(bad.length) return {sev: 'bad', head: bad[0].t, why: bad.slice(1).map(x => x.t).join('; ')};
  if(a.ctx_tokens >= ceil) return {sev: 'bad', head: 'Over the ceiling at ' + kTok(a.ctx_tokens),
    why: kTok(a.ctx_tokens - ceil) + ' past the ' + kTok(ceil) + ' ceiling. Must clear at its next clean seam.'};
  if(a.silent) return {sev: 'warn', head: 'Silent for ' + ago(a.busy_age),
    why: 'Busy for ' + ago(a.busy_age) + ' but STATE.md/TASKS.md ' + (a.ledger_age == null ? 'are unreadable'
      : 'last moved ' + ago(a.ledger_age) + ' ago') + '. Possible wedge or permission prompt.'};
  return null;
}
function needCard(a, n){
  const name = String(a.name ?? '?');
  return '<div class="card need-c ' + n.sev + '"><div class="need-h"><b>' + esc(name) + '</b><span>' + esc(n.head) + '</span></div>'
    + (n.why ? '<p>' + esc(n.why) + '</p>' : '')
    + (a.ledger ? '<p class="nl"><i>Last ledger:</i> ' + esc(clip(a.ledger, 140)) + '</p>' : '')
    + '<div class="need-f">' + ((a.busy === true || a.busy === false) && /^[A-Za-z0-9_.-]+$/.test(name)   // pane verdict + shell-safe name
      ? '<code class="cmd" title="click to copy">' + esc('tmux attach -t ' + name) + '</code>' : '')
    + (a.ledger_age == null ? '' : '<span class="when" title="STATE.md/TASKS.md last edited">' + esc('ledger ' + ago(a.ledger_age)) + '</span>')
    + '</div></div>';
}
function runwayRow(a, ceil, soft){
  const max = ceil * 1.5, t = a.ctx_tokens, heat = t >= ceil ? 'hot' : t >= soft ? 'warm' : '';
  const pos = v => (Math.min(v, max) / max * 100).toFixed(1) + '%';
  return '<div class="rw" title="' + esc(fmtNum(t)) + ' tokens"><span class="nm">' + esc(a.name ?? '?') + '</span>'
    + '<span class="track"><span class="fill ' + heat + '" style="width:' + pos(t) + '"></span>'
    + '<span class="soft" style="left:' + pos(soft) + '"></span><span class="tick" style="left:' + pos(ceil) + '"></span></span>'
    + '<span class="mval ' + heat + '">' + esc(kTok(t)) + '</span></div>';
}
function workerRow(w){
  const t = title(w.key);
  return '<div class="wk-r" title="' + esc(w.key) + '"><span class="t">' + (t.src ? '<i>' + esc(t.src) + ' </i>' : '') + esc(t.name) + '</span>'
    + '<span class="a">' + esc(w.in_turn ? 'turn ' + ago(w.turn_age) : 'idle') + '</span></div>';
}
function paintFleet(f){
  f = f || {error: 'no fleet data in /state'};
  const ceil = Number(f.ceiling) || 200000, soft = Number(f.soft) || 150000;
  $('f-mode').innerHTML = MODES.map(m => '<span class="rung' + (m === f.mode ? ' on' : '') + '" style="--mc:'
    + MODE_CLR[m] + '">' + esc(m) + '</span>').join('')
    + (f.mode ? '' : '<span class="rung on" style="--mc:var(--red)">' + esc(f.mode_error || 'unknown') + '</span>');
  const rows = Array.isArray(f.agents) ? f.agents : [];
  const act = rows.filter(a => !a.stood_down), bench = rows.filter(a => a.stood_down);
  act.sort((x, y) => rank(x) - rank(y) || (y.ctx_tokens ?? -1) - (x.ctx_tokens ?? -1) || String(x.name).localeCompare(String(y.name)));
  const needs = act.map(a => [a, needWhy(a, ceil)]).filter(p => p[1]);
  const needSet = new Set(needs.map(p => p[0]));
  const rest = act.filter(a => !needSet.has(a));
  const fly = rest.filter(a => a.busy === true && a.alive !== false), park = rest.filter(a => !fly.includes(a));
  const wk = (lastState && Array.isArray(lastState.workers)) ? lastState.workers : [];
  // Tower headline reads as a sentence: who needs you, then the rest of the board in plain words
  $('f-head').textContent = !needs.length ? 'All clear' : needs.length === 1 ? '1 agent needs you' : needs.length + ' agents need you';
  $('f-tally').textContent = (fly.length ? fly.length + ' in flight' : 'None in flight') + ', ' + park.length + ' parked, '
    + act.length + ' standing. ' + (wk.length ? wk.length + ' eph worker' + (wk.length === 1 ? '' : 's') + ' running' : 'No eph workers running')
    + (bench.length ? ', ' + bench.length + ' stood down.' : '.');
  const gen = f.generated_age == null ? '' : f.generated_age < 60 ? 'updated just now'
    : 'updated ' + ago(f.generated_age) + ' ago';
  const sup = f.supervisor;   // Only speak up when the supervisor feed exists but is unhealthy
  $('f-gen').innerHTML = esc(gen) + (f.stale ? ' <span class="pill bad">stale</span>' : '')
    + (sup && sup.error ? ' <span class="pill warn">' + esc(sup.error) + '</span>'
      : sup && sup.stale ? ' <span class="pill warn">supervisor ' + esc(ago(sup.age)) + ' old</span>'
      : sup && sup.tmux_server_up === false ? ' <span class="pill bad">tmux server down</span>' : '');
  $('f-err').hidden = !f.error && rows.length > 0;
  $('f-err').textContent = f.error || 'No agents listed in fleet-status.json.';
  $('f-needs-w').hidden = !needs.length;
  $('f-needs-n').textContent = needs.length;
  $('f-needs').innerHTML = needs.map(p => needCard(p[0], p[1])).join('');
  const grp = (label, list) => list.length ? '<div class="grp">' + esc(label) + '<span>' + esc(list.length) + '</span></div>'
    + list.map(a => agentRow(a, ceil, soft)).join('') : '';
  $('f-rows').innerHTML = grp('In flight', fly) + grp('Parked', park);
  const run = act.filter(a => a.ctx_tokens != null).sort((x, y) => y.ctx_tokens - x.ctx_tokens);
  $('f-runway-c').hidden = !run.length;
  $('f-runway-k').textContent = 'reset by ' + kTok(soft) + ', ceiling ' + kTok(ceil);
  $('f-runway').innerHTML = run.map(a => runwayRow(a, ceil, soft)).join('');
  $('f-wk').innerHTML = wk.length ? wk.map(workerRow).join('') : '<div class="empty">' + esc('No eph workers running.') + '</div>';
  $('f-hangar-c').hidden = !bench.length;
  $('f-hangar').innerHTML = bench.map(a => '<span title="' + esc(a.role || '') + '">' + esc(a.name ?? '?') + '</span>').join('');
}
// Tower: click a `tmux attach` chip to copy it (user-select:all is the no-clipboard fallback)
$('f-needs').addEventListener('click', e => {
  const c = e.target.closest('.cmd');
  if(!c || !navigator.clipboard) return;
  navigator.clipboard.writeText(c.textContent).then(() => { c.classList.add('copied'); setTimeout(() => c.classList.remove('copied'), 1200); }, () => {});
});
function toggleAgent(e){
  const r = e.target.closest('.ag[data-agent]');
  if(!r || (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ')) return;
  if(e.type === 'keydown') e.preventDefault();
  const n = r.dataset.agent, open = !fleetOpen.has(n);
  open ? fleetOpen.add(n) : fleetOpen.delete(n);
  r.classList.toggle('open', open);
  r.setAttribute('aria-expanded', open);
}
['f-rows'].forEach(id => { $(id).addEventListener('click', toggleAgent); $(id).addEventListener('keydown', toggleAgent); });
function showTab(t){
  $('fleet-view').hidden = t !== 'fleet';
  $('workers-view').hidden = t === 'fleet';
  $('tab-fleet').classList.toggle('on', t === 'fleet');
  document.body.classList.toggle('on-fleet', t === 'fleet');
  $('tab-workers').classList.toggle('on', t !== 'fleet');
  try{ localStorage.setItem('eph-tab', t); }catch(e){}
}
$('tab-workers').onclick = () => showTab('workers');
$('tab-fleet').onclick = () => showTab('fleet');
try{ if(localStorage.getItem('eph-tab') === 'fleet') showTab('fleet'); }catch(e){}

function paintDetail(){
  if(!selected || !lastState) return;
  if(selectedPast){   // A previous session: static replay, only what the history recorded
    const h = (lastState.history || []).find(x => x.key === selected && x.session === selectedPast) || {};
    $('d-title').textContent = title(selected).name;
    $('d-key').textContent = selected;
    $('d-badge').innerHTML = badge('Previous', 'done');
    $('d-stats').innerHTML = [['Started', h.ts != null ? when(h.ts) : null], ['Session', selectedPast]].map(([k, v]) =>
      '<div class="stat"><span>' + k + '</span><b>' + esc(v ?? 'n/a') + '</b></div>').join('');
    return;
  }
  const s =sessions(lastState).find(x => x.key === selected) || {key: selected, live: null, worker: null};
  const w = s.worker || {}, l = s.live || {};
  $('d-title').textContent = title(selected).name;
  $('d-key').textContent = selected;
  $('d-badge').innerHTML = stateBadge(s);
  const tiles = [
    ['Turns', w.turns], ['Tokens', w.tokens != null ? fmtNum(w.tokens) : null], ['Forwards', w.forwards ?? l.forwards],
    ['Events', l.events], ['Buffered', l.buffered], ['Turn age', w.turn_age != null ? age(w.turn_age) : null],
    ['Expires in', l.expire_in != null ? age(l.expire_in) : null], ['Session', w.session || null],
  ];
  $('d-stats').innerHTML = tiles.map(([k, v]) =>
    '<div class="stat"><span>' + k + '</span><b>' + esc(v ?? 'n/a') + '</b></div>').join('');
}

function select(key, past){
  past = past || null;
  if(key === selected && past === selectedPast && es) return;
  selected = key; selectedPast = past;
  document.querySelectorAll('.sess').forEach(b =>
    b.classList.toggle('sel', b.dataset.key === key && (b.dataset.past || null) === past));
  $('d-empty').hidden = true;
  $('d-main').hidden = false;
  paintDetail();
  // On the one-column layout the detail card moves to the top of the page (order:-1); go there
  if(matchMedia('(max-width:900px)').matches) scrollTo({top:0});
  if(lastState && lastState.stream_enabled) openStream(key, past);
  else {
    closeStream(); resetLog();
    $('sv-log').appendChild(el('div', 'notice',
      'Live transcripts are turned off on this broker (BROKER_STREAM_ENABLED), so only state is shown.'));
    svStatus('stream off', '');
  }
}

// ---- live transcript (only reachable when /state says stream_enabled; the server enforces auth itself).
// Rendered as a chat/timeline; the raw jsonl is kept in a parallel pane behind the Raw toggle.
let es = null, rawMode = false, tools = new Map();
function svStatus(t, cls){ $('sv-st').textContent = t; $('sv-st').className = 'conn ' + (cls || ''); }
function atBottom(b){ return b.scrollTop + b.clientHeight >= b.scrollHeight - 20; }
function trim(b){ while(b.childNodes.length > 1000) b.removeChild(b.firstChild); }

// These top-level record `type`s are Claude Code harness plumbing, not turns; noisy runs of
// them get collapsed into one expandable "N harness records" row instead of one <div> each.
const HARNESS_TYPES = new Set(['attachment', 'atis-latch', 'last-prompt']);
let harnessGroup = null;   // the currently-open collapsed <details> run, or null between runs

function harnessLabel(o, ty){
  // Generic: a record whose top-level type is T often nests its detail under a same-named key,
  // e.g. {"type":"attachment","attachment":{"type":"hook_success",...}} -> "attachment · hook_success".
  // No such nested `.type` (atis-latch, last-prompt) -> bare record type, no invented subtype.
  const nested = o && o[ty];
  const subtype = nested && typeof nested === 'object' && typeof nested.type === 'string' ? nested.type : null;
  return subtype ? ty + ' · ' + subtype : ty;
}
function resetLog(){ $('sv-log').innerHTML = ''; $('sv-raw').innerHTML = ''; harnessGroup = null; tools = new Map(); }
function addHarness(o, ty){
  const log = $('sv-log'), stick = atBottom(log);
  if(!harnessGroup){
    const det = document.createElement('details');
    det.className = 'harness';
    const sum = document.createElement('summary');
    det.appendChild(sum);
    const body = document.createElement('div');
    det.appendChild(body);
    log.appendChild(det);
    harnessGroup = {sum, body, count: 0};
  }
  harnessGroup.count++;
  harnessGroup.sum.textContent = harnessGroup.count + ' harness records';   // literal wording
  const div = document.createElement('div');
  div.className = 'ln';
  div.textContent = harnessLabel(o, ty);   // textContent, never innerHTML: record content is untrusted
  // The record's fields sit behind a collapsed per-line toggle as a key/value table, not raw JSON
  const nested = o && o[ty] && typeof o[ty] === 'object' ? o[ty] : o;
  const more = el('details', 'jd');
  more.appendChild(el('summary', null, 'details · ' + shape(nested)));
  more.appendChild(structured(nested));
  div.appendChild(more);
  harnessGroup.body.appendChild(div);
  trim(log);
  if(stick) log.scrollTop = log.scrollHeight;
}

function timeEl(o){
  const t = o && o.timestamp ? new Date(o.timestamp) : null, ok = t && !isNaN(t);
  const e = el('time', 'ts', ok ? t.toLocaleTimeString() : '');
  if(ok) e.title = t.toLocaleString();
  return e;
}
function bubble(role, text, o, err){
  const ev = el('div', 'ev ' + role + (err ? ' err' : ''));
  const meta = el('div', 'ev-meta');
  meta.appendChild(el('span', 'who', role === 'assistant' ? 'Assistant' : role === 'user' ? 'User' : role));
  if(err) meta.appendChild(el('span', 'pill bad', 'error'));
  meta.appendChild(timeEl(o));
  ev.appendChild(meta);
  const sj = splitJSON(text);
  if(sj){   // JSON body -> structured card; the exact original stays one click away (per-message Raw)
    const view = el('div');
    if(sj.prose.trim()){ const p = el('div', 'md'); p.innerHTML = md(sj.prose); view.appendChild(p); }
    view.appendChild(jsonCard(sj.obj, 0));
    const raw = el('pre', 'tpre', String(text));
    raw.hidden = true;
    const btn = el('button', 'btn rawbtn', 'Raw');
    btn.title = 'show this message exactly as sent';
    btn.onclick = () => { raw.hidden = !raw.hidden; view.hidden = !raw.hidden; btn.classList.toggle('on', !raw.hidden); };
    meta.appendChild(btn);
    ev.appendChild(view);
    ev.appendChild(raw);
    return ev;
  }
  const body = el('div', 'md');
  body.innerHTML = md(text);   // md() escapes all transcript text before adding its own fixed tags
  ev.appendChild(body);
  return ev;
}

// ---- JSON message bodies (worker kickoff, result envelope) as a structured card: short scalars as
// key/value chips, long strings by content type (HTML -> sanitizeHtml, else markdown), nested objects in
// collapsed <details>. All text enters via el()/textContent, md() or sanitizeHtml(), both escape-first.
const CHIP_MAX = 80;
function splitJSON(text){   // the whole body is JSON, or prose ending in a trailing JSON object
  const s = String(text ?? ''), full = v => v && typeof v === 'object' && Object.keys(v).length > 0;
  const j = tryJSON(s);
  if(full(j)) return {prose: '', obj: j};
  if(!s.trimEnd().endsWith('}')) return null;
  const re = /\n\s*\{/g;
  let m;
  while((m = re.exec(s))){
    const k = tryJSON(s.slice(m.index + 1));
    if(full(k) && !Array.isArray(k)) return {prose: s.slice(0, m.index), obj: k};
  }
  return null;
}
function looksHtml(s){ return /^\s*<([a-z][a-z0-9]*)\b[^>]*>[\s\S]*<\/[a-z][a-z0-9]*\s*>/i.test(s); }
function isChip(v){
  if(typeof v === 'string') return v.length <= CHIP_MAX && !v.includes('\n') && !looksHtml(v);
  if(Array.isArray(v)) return v.length <= 12 && v.every(x => isScalar(x) && String(x).length < 60);
  return isScalar(v);
}
function linkOrText(tag, s){
  if(!/^https?:\/\/\S+$/i.test(s)) return el(tag, null, s);
  const a = el('a', null, s);
  a.href = s; a.target = '_blank'; a.rel = 'noopener noreferrer';
  return a;
}
function richText(s){
  if(!s.includes('\n') && s.includes('\\n')) s = s.replace(/\\n/g, '\n');   // double-encoded newlines
  const r = !looksHtml(s) && mdRecord(s);   // An embedded "**key:** value" record (e.g. a ticket dump)
  if(r) return recordView(r);
  const b = el('div', 'md');
  if(looksHtml(s)) b.innerHTML = sanitizeHtml(s);   // allowlist tags, http(s)-only href, everything else escaped
  else b.innerHTML = md(s);                         // escape-first markdown
  return b;
}
function jsonCard(obj, depth){
  const box = el('div', 'jcard'), chips = el('div', 'jchips');
  box.appendChild(chips);
  const ents = Array.isArray(obj) ? obj.map((x, i) => ['#' + (i + 1), x]) : Object.entries(obj);
  for(const [k, v] of ents){
    if(isChip(v)){
      const c = el('span', 'chip');
      c.appendChild(el('span', null, k));
      c.appendChild(linkOrText('b', Array.isArray(v) ? (v.length ? v.map(scalar).join(', ') : '(none)') : scalar(v)));
      chips.appendChild(c);
    }else if(typeof v === 'string'){
      box.appendChild(el('div', 'tlabel', k));
      box.appendChild(richText(v));
    }else{   // nested object / long array: collapsible, collapsed by default
      const d = el('details', 'jd');
      d.appendChild(el('summary', null, k + ' · ' + peek(v)));   // "test_runner · pytest", not "2 fields"
      d.appendChild(depth < 4 ? jsonCard(v, depth + 1) : jsonDetails(v));
      box.appendChild(d);
    }
  }
  if(!chips.childNodes.length) chips.remove();
  return box;
}
const ARG_KEYS = ['command', 'file_path', 'path', 'pattern', 'query', 'url', 'title', 'description', 'prompt', 'name'];
// mcp args read as "get · id 9369" (verb + ids + main arg), not "operation: get · id: 9369 · …" / "N fields"
const OP_KEYS = ['operation', 'action', 'subcommand', 'method', 'op'];
const ID_KEY = /^(id|ids|identifier|index|number|[a-z]+_id|[a-z]+Id)$/;
function argSummary(inp){
  if(!inp || typeof inp !== 'object' || Array.isArray(inp)) return '';
  const out = [], used = new Set();
  const op = OP_KEYS.find(k => typeof inp[k] === 'string' && inp[k]);
  if(op){ out.push(inp[op]); used.add(op); }
  for(const [k, v] of Object.entries(inp)){
    if(out.length >= 3) break;
    if(ID_KEY.test(k) && (typeof v === 'number' || (typeof v === 'string' && v && v.length < 40))){
      out.push(k.replace(/_id$|Id$/, '') + ' ' + v); used.add(k);
    }
  }
  const main = ARG_KEYS.find(k => !used.has(k) && typeof inp[k] === 'string' && inp[k]);
  if(main) out.push(inp[main].replace(/\s+/g, ' '));
  if(!out.length)
    for(const [k, v] of Object.entries(inp)){
      if(out.length >= 3) break;
      if(!isBlank(v)) out.push(k + ': ' + (isScalar(v) ? scalar(v).replace(/\s+/g, ' ') : peek(v)));
    }
  return clip(out.join(' · '), 120);
}

// ---- human-readable tool cards. Known tools (Bash, Read, Edit, Write, Grep, Glob) get purpose-built
// renderers; any other object (mcp__* args, JSON results) becomes a key/value table; long/nested JSON sits
// behind a collapsed "details" toggle (nothing ever opens it). All content enters the DOM via el()/textContent,
// or md() (escape-first) for prose text blocks, never raw innerHTML.
const CODE_TOOLS = new Set(['Bash', 'BashOutput', 'Read', 'Write', 'Edit', 'MultiEdit', 'NotebookEdit', 'Grep', 'Glob']);
function isScalar(v){ return v === null || ['string', 'number', 'boolean'].includes(typeof v); }
function scalar(v){ return v === null ? 'null' : v === undefined ? '' : String(v); }
function shape(v){
  if(Array.isArray(v)) return v.length + (v.length === 1 ? ' item' : ' items');
  if(v && typeof v === 'object'){ const n = Object.keys(v).length; return n + (n === 1 ? ' field' : ' fields'); }
  return typeof v;
}
function pre(text, cls){ return el('pre', 'tpre' + (cls ? ' ' + cls : ''), clip(text, 20000)); }
function tlabel(t){ return el('div', 'tlabel', t); }
function clipLines(s, n){
  const l = String(s ?? '').split('\n');
  return l.length > n ? l.slice(0, n).join('\n') + '\n… (' + (l.length - n) + ' more lines)' : l.join('\n');
}
function jsonDetails(v, what){   // collapsed by default: the pretty JSON is one click away, never shown up front
  const d = el('details', 'jd');
  let s; try{ s = JSON.stringify(v, null, 2); }catch(e){ s = String(v); }
  d.appendChild(el('summary', null, 'details · ' + (what || shape(v))));
  d.appendChild(pre(s));
  return d;
}
function kvValue(v, depth){
  if(typeof v === 'string') return v.includes('\n') || v.length > 160 ? pre(v) : el('span', null, v);
  if(isScalar(v)) return el('code', 'kvc', scalar(v));
  if(Array.isArray(v) && v.length <= 12 && v.every(x => isScalar(x) && String(x).length < 60))
    return el('span', null, v.length ? v.map(scalar).join(', ') : '(none)');
  // Labels / users / tasks show as their name ("opus #20"), the full record one click away
  if(entity(v) && depth < 4) return entDetails(v, depth);
  if(Array.isArray(v) && v.length && v.length <= 50 && depth < 4 && v.every(x => entity(x))){
    const box = el('div', 'ents');
    for(const x of v) box.appendChild(entDetails(x, depth));
    return box;
  }
  if(v && typeof v === 'object' && !Array.isArray(v) && depth < 2 && Object.keys(v).length <= 12) return kvTable(v, depth + 1);
  return jsonDetails(v);
}
function entDetails(v, depth){
  const d = el('details', 'jd ent');
  d.appendChild(el('summary', null, entityLabel(v)));
  d.appendChild(kvTable(v, depth + 1));
  return d;
}
function kvRows(t, ents, depth){
  for(const [k, v] of ents){
    const tr = el('tr'), td = el('td');
    tr.appendChild(el('th', null, k));
    td.appendChild(kvValue(v, depth || 0));
    tr.appendChild(td);
    t.appendChild(tr);
  }
}
function kvTable(obj, depth){
  const t = el('table', 'kv'), all = Object.entries(obj);
  // Empty / zero / default fields (hex_color: '', percent_done: 0, 0001-01-01 dates) fold into one row
  const blank = all.length > 3 ? all.filter(([, v]) => isBlank(v)) : [];
  kvRows(t, blank.length > 1 ? all.filter(([, v]) => !isBlank(v)) : all, depth);
  if(blank.length > 1){
    const tr = el('tr'), td = el('td'), d = el('details', 'jd blank'), bt = el('table', 'kv');
    td.colSpan = 2;
    d.appendChild(el('summary', null, blank.length + ' empty / default: ' + clip(blank.map(([k]) => k).join(', '), 140)));
    kvRows(bt, blank, depth);
    d.appendChild(bt);
    td.appendChild(d);
    tr.appendChild(td);
    t.appendChild(tr);
  }
  return t.childNodes.length ? t : el('span', 'muted', '(empty)');
}
function structured(v){ return v && typeof v === 'object' && !Array.isArray(v) ? kvTable(v, 0) : kvValue(v, 0); }
function tryJSON(s){
  const t = String(s ?? '').trim();
  if(!/^[\[{]/.test(t)) return undefined;
  try{ return JSON.parse(t); }catch(e){ return undefined; }
}
// ---- structured tool results. Pure string/object code (no DOM) so the tests can run it under node.
// Empty / default values (Go zero time, 0, '', null, {}, []) fold out of the way; `false` stays visible.
const ZERO_TIME = /^0001-01-01([T ]00:00:00(\.0+)?(Z|[+-]00:?00)?)?$/;
function isBlank(v){
  if(v === null || v === undefined || v === '' || v === 0) return true;
  if(typeof v === 'string') return ZERO_TIME.test(v.trim()) || !v.trim();
  if(Array.isArray(v)) return !v.length;
  if(typeof v === 'object') return !Object.keys(v).length;
  return false;
}
function entity(v){   // {id, title|name|username} -> {name, id} (labels, users, tasks); null when it isn't one
  if(!v || typeof v !== 'object' || Array.isArray(v)) return null;
  const n = ['title', 'name', 'username', 'login', 'label'].map(k => v[k]).find(x => typeof x === 'string' && x.trim());
  if(!n) return null;
  const id = (typeof v.id === 'number' || (typeof v.id === 'string' && v.id && v.id.length < 24)) ? '#' + v.id
           : typeof v.identifier === 'string' && v.identifier.length < 24 ? v.identifier : '';
  return {name: clip(n.replace(/\s+/g, ' '), 80), id};
}
function entityLabel(v){ const e = entity(v); return e ? e.name + (e.id ? ' ' + e.id : '') : ''; }   // "opus #20"
function peek(v){   // one-line gist of a nested value: entity label, else its first meaningful scalars, else its shape
  if(isScalar(v)) return clip(scalar(v).replace(/\s+/g, ' '), 60);
  const lab = entityLabel(v);
  if(lab) return lab;
  if(Array.isArray(v)){
    const labs = v.slice(0, 3).map(x => isScalar(x) ? scalar(x) : entityLabel(x));
    return labs.length && labs.every(Boolean) ? clip(labs.join(', '), 60) + (v.length > 3 ? ' +' + (v.length - 3) : '') : shape(v);
  }
  const sc = Object.values(v || {}).filter(x => isScalar(x) && !isBlank(x)).slice(0, 2);
  return sc.length ? clip(sc.map(x => scalar(x).replace(/\s+/g, ' ')).join(' · '), 60) : shape(v);
}
function jsonSpanEnd(lines, i, first){   // index of the line closing the JSON value opened on line i ({ or [), else -1
  let depth = 0, str = false, bs = false;
  for(let j = i; j < lines.length && j < i + 5000; j++){
    for(const ch of j === i ? first : lines[j]){
      if(str){ if(bs) bs = false; else if(ch === '\\') bs = true; else if(ch === '"') str = false; }
      else if(ch === '"') str = true;
      else if(ch === '{' || ch === '[') depth++;
      else if((ch === '}' || ch === ']') && --depth === 0) return j;
    }
  }
  return -1;
}
// MCP results like Vikunja's: a markdown preamble, then "**key:** value" lines whose value may open a multi-line
// JSON block (assignees, labels, created_by). Returns {prose, obj}, or null unless it's clearly such a record.
const MD_FIELD = /^\s*\*\*([^*\n]{1,60}?):\*\*[ \t]*(.*)$/;
function mdRecord(s){
  const lines = String(s ?? '').split('\n'), prose = [], obj = {};
  let n = 0;
  for(let i = 0; i < lines.length; i++){
    const m = lines[i].match(MD_FIELD);
    if(!m){ prose.push(lines[i]); continue; }
    const k = m[1].trim(), raw = m[2].trim();
    let v = raw;
    if(/^[\[{]/.test(raw)){
      const end = jsonSpanEnd(lines, i, raw);
      const j = end < 0 ? undefined : tryJSON([raw].concat(lines.slice(i + 1, end + 1)).join('\n'));
      if(j !== undefined){ v = j; i = end; }
    }else if(/^-?\d+(\.\d+)?$/.test(raw) && raw.length < 16) v = Number(raw);
    else if(raw === 'true' || raw === 'false') v = raw === 'true';
    if(!(k in obj)) n++;
    obj[k] = v;
  }
  return n >= 4 ? {prose: prose.join('\n').replace(/\n{3,}/g, '\n\n').trim(), obj} : null;
}
function fmtSize(s){
  const b = String(s ?? '').length, l = String(s ?? '').split('\n').length;
  return (l > 1 ? l + ' lines · ' : '') + (b < 1024 ? b + ' B' : (b / 1024).toFixed(b < 10240 ? 1 : 0) + ' KB');
}
function resultFact(v){   // the key fact of a parsed result for the card header: "#42 Some task title" / "12 items"
  if(Array.isArray(v)) return v.length + (v.length === 1 ? ' item' : ' items');
  if(!v || typeof v !== 'object') return '';
  const e = entity(v);
  if(e) return e.id ? e.id + ' ' + e.name : e.name;
  for(const k of ['message', 'status', 'result']) if(typeof v[k] === 'string' && v[k].trim()) return clip(v[k].replace(/\s+/g, ' '), 80);
  return '';
}
// ---- end structured tool results
function diffView(oldS, newS){
  const p = el('pre', 'tpre diff');
  const add = (pfx, s, cls) => { for(const ln of clip(s, 10000).split('\n')) p.appendChild(el('span', cls, pfx + ln)); };
  add('- ', oldS, 'del');
  add('+ ', newS, 'add');
  return p;
}
function extras(body, inp, skip){   // any args the purpose-built view didn't show, as a small table
  const r = {};
  for(const k of Object.keys(inp)) if(!skip.includes(k)) r[k] = inp[k];
  if(Object.keys(r).length) body.appendChild(kvTable(r, 0));
}
function renderInput(body, name, inp){
  if(!inp || typeof inp !== 'object' || Array.isArray(inp)){ body.appendChild(tlabel('Input')); body.appendChild(structured(inp)); return; }
  if(name === 'Bash' && typeof inp.command === 'string'){
    if(inp.description) body.appendChild(el('div', 'tdesc', inp.description));
    body.appendChild(tlabel('Command'));
    body.appendChild(pre('$ ' + inp.command));
    extras(body, inp, ['command', 'description']);
    return;
  }
  if(typeof inp.file_path === 'string' && ['Read', 'Write', 'Edit', 'MultiEdit'].includes(name)){
    body.appendChild(el('div', 'fpath', inp.file_path));
    if(name === 'Edit'){
      body.appendChild(tlabel(inp.replace_all ? 'Change (all occurrences)' : 'Change'));
      body.appendChild(diffView(inp.old_string, inp.new_string));
      extras(body, inp, ['file_path', 'old_string', 'new_string', 'replace_all']);
    }else if(name === 'MultiEdit' && Array.isArray(inp.edits)){
      inp.edits.forEach((e, i) => { body.appendChild(tlabel('Change ' + (i + 1))); body.appendChild(diffView((e || {}).old_string, (e || {}).new_string)); });
      extras(body, inp, ['file_path', 'edits']);
    }else if(name === 'Write'){
      body.appendChild(tlabel('Content'));
      body.appendChild(pre(clipLines(inp.content, 80)));
      extras(body, inp, ['file_path', 'content']);
    }else extras(body, inp, ['file_path']);   // Read: offset / limit
    return;
  }
  if((name === 'Grep' || name === 'Glob') && typeof inp.pattern === 'string'){
    body.appendChild(tlabel('Pattern'));
    body.appendChild(pre(inp.pattern));
    extras(body, inp, ['pattern']);
    return;
  }
  body.appendChild(tlabel('Arguments'));   // mcp__* and anything else: key/value table
  body.appendChild(kvTable(inp, 0));
}
function recordView(r){   // mdRecord() output -> markdown preamble + key/value table
  const box = el('div');
  if(r.prose){ const p = el('div', 'md'); p.innerHTML = md(r.prose); box.appendChild(p); }
  box.appendChild(structured(r.obj));
  return box;
}
function mdFenced(s){   // Markdown whose ```fenced``` blocks hold JSON -> those blocks become kv tables
  const parts = s.split('```');
  if(parts.length < 3) return null;
  let hit = false;
  const box = el('div');
  parts.forEach((seg, i) => {
    let j;
    if(i % 2 && i < parts.length - 1) j = tryJSON(seg.slice(Math.max(seg.indexOf('\n'), 0)));   // drop the language tag
    if(j && typeof j === 'object'){ hit = true; box.appendChild(structured(j)); return; }
    if(!seg.trim()) return;
    const b = el('div', 'md');
    b.innerHTML = md(i % 2 ? '```' + seg + '```' : seg);   // escape-first markdown, as below
    box.appendChild(b);
  });
  return hit ? box : null;
}
function renderResult(body, c, bad, name){   // returns a one-line fact about the result for the card header
  const code = CODE_TOOLS.has(String(name || ''));
  let fact = '';
  const text = s => {
    s = String(s ?? '');
    if(!s.trim()) return el('div', 'muted', '(no output)');
    if(code || bad) return pre(s, bad ? 'bad' : '');      // command output / file text / errors stay verbatim
    const j = tryJSON(s);
    if(j && typeof j === 'object'){ fact = fact || resultFact(j); return structured(j); }   // JSON-in-text -> table
    const r = mdRecord(clip(s, 200000));   // "**key:** value" records with embedded multi-line JSON
    if(r){ fact = fact || resultFact(r.obj); return recordView(r); }
    const f = mdFenced(clip(s, 20000));
    if(f) return f;
    const b = el('div', 'md');
    b.innerHTML = md(clip(s, 20000));   // md() escapes all transcript text before adding its own fixed tags
    return b;
  };
  if(typeof c === 'string'){ body.appendChild(text(c)); return fact; }
  if(Array.isArray(c)){
    if(!c.length) body.appendChild(el('div', 'muted', '(no output)'));
    for(const x of c){
      if(x && x.type === 'text') body.appendChild(text(x.text));
      else if(x && x.type === 'image') body.appendChild(el('div', 'muted', '[image' + (x.source && x.source.media_type ? ' · ' + x.source.media_type : '') + ']'));
      else body.appendChild(jsonDetails(x, '[' + ((x && x.type) || '?') + ']'));
    }
    return fact;
  }
  body.appendChild(c == null ? el('div', 'muted', '(no output)') : structured(c));
  return c == null ? '' : resultFact(c);
}
function toolName(n){
  const p = String(n || 'tool').split('__');   // mcp__claude_ai_Vikunja__vikunja_tasks -> "Vikunja › vikunja_tasks"
  return p[0] === 'mcp' && p.length >= 3
    ? p[1].replace(/^claude_ai_/, '').replace(/_/g, ' ') + ' › ' + p.slice(2).join('__') : p.join('__');
}
function card(cls, icon, name, args, o){
  const d = el('details', 'tool' + (cls ? ' ' + cls : ''));   // collapsed by default: nothing ever opens it
  const s = el('summary');
  s.appendChild(el('span', 'ticon', icon));
  s.appendChild(el('span', 'tname', name));
  s.appendChild(el('span', 'targs', args));
  d.appendChild(s);
  const body = el('div', 'tbody');
  d.appendChild(body);
  return {d, s, body};
}
function toolCard(p, o){
  const c = card('', '⚙', toolName(p.name), argSummary(p.input), o);
  const st = el('span', 'pill pend', 'running');
  c.s.appendChild(st);
  c.s.appendChild(timeEl(o));
  renderInput(c.body, p.name, p.input ?? {});
  if(p.id){ tools.set(p.id, {d: c.d, s: c.s, st, body: c.body, name: p.name}); if(tools.size > 500) tools.delete(tools.keys().next().value); }
  return c.d;
}
function resultText(c){
  if(typeof c === 'string') return c;
  if(Array.isArray(c)) return c.map(x => x && x.type === 'text' ? x.text : '[' + ((x && x.type) || '?') + ']').join('\n');
  return c == null ? '' : shape(c);   // summary line only; the body is rendered by renderResult()
}
function addResult(p, o, tl){
  const txt = resultText(p.content), bad = !!p.is_error;
  const t = p.tool_use_id && tools.get(p.tool_use_id);
  if(t){   // fold the result into its tool call's card
    t.st.textContent = bad ? 'error' : 'done';
    t.st.className = 'pill ' + (bad ? 'bad' : 'ok');
    if(bad) t.d.classList.add('err');
    t.body.appendChild(el('div', 'tlabel', bad ? 'Error' : 'Result'));
    const fact = renderResult(t.body, p.content, bad, t.name);
    // The collapsed card's header says what came back: "→ #42 Some task · 74 lines · 2.1 KB"
    const head = bad ? clip(txt.trim().split('\n')[0], 80) : fact;
    const r = el('span', 'tres', '→ ' + (head ? head + ' · ' : '') + fmtSize(txt));
    r.title = r.textContent;
    t.s.insertBefore(r, t.st);
    return;
  }
  const c = card(bad ? 'err' : '', bad ? '!' : '↳', bad ? 'Tool error' : 'Tool result', clip(txt.replace(/\s+/g, ' '), 120), o);
  c.s.appendChild(timeEl(o));
  renderResult(c.body, p.content, bad, '');
  tl.appendChild(c.d);
}
function thinkingCard(p, o){
  const txt = String(p.thinking || '');
  const c = card('think', '✦', 'Thinking', txt ? clip(txt.replace(/\s+/g, ' '), 120) : '(hidden)', o);
  c.s.appendChild(timeEl(o));
  if(txt){ const b = el('div', 'md'); b.innerHTML = md(txt); c.body.appendChild(b); }
  else c.body.remove();
  return c.d;
}
function metaRow(label, text, o, err){
  const r = el('div', 'meta-row' + (err ? ' err' : ''));
  r.appendChild(el('span', 'mlabel', label));
  r.appendChild(el('span', 'mtext', clip(text, 400)));
  r.appendChild(timeEl(o));
  return r;
}
function renderRecord(o, ty, data){
  const tl = $('sv-log');
  if(!o){ tl.appendChild(metaRow('line', data, null, false)); return; }
  const c = (o.message || {}).content;
  const err = !!(o.isApiErrorMessage || o.error || o.level === 'error');
  if(ty === 'assistant' || ty === 'user'){
    const parts = typeof c === 'string' ? [{type: 'text', text: c}] : Array.isArray(c) ? c : [];
    for(const p of parts){
      if(!p || typeof p !== 'object') continue;
      if(p.type === 'text'){ if(String(p.text ?? '').trim()) tl.appendChild(bubble(ty, p.text, o, err)); }
      else if(p.type === 'tool_use') tl.appendChild(toolCard(p, o));
      else if(p.type === 'tool_result') addResult(p, o, tl);
      else if(p.type === 'thinking' || p.type === 'redacted_thinking') tl.appendChild(thinkingCard(p, o));
      else {   // Unknown block type -> collapsed card with a field table, not a raw object
        const u = card(err ? 'err' : '', '◇', String(p.type || '?'), shape(p), o);
        u.s.appendChild(timeEl(o));
        u.body.appendChild(structured(p));
        tl.appendChild(u.d);
      }
    }
    if(!parts.length) tl.appendChild(metaRow(ty, o.summary || o.subtype || '', o, err));
    return;
  }
  const text = typeof o.content === 'string' ? o.content : (o.summary || o.subtype || '');
  tl.appendChild(metaRow(o.subtype ? ty + ' · ' + o.subtype : ty, text, o, err || /error/i.test(String(o.subtype || ''))));
}
function addRaw(data){
  const box = $('sv-raw'), stick = atBottom(box);
  box.appendChild(el('div', 'rl', data));
  trim(box);
  if(stick) box.scrollTop = box.scrollHeight;
}
function addLine(data){
  addRaw(data);
  let ty = '?', o = null;
  try{ o = JSON.parse(data); ty = (o.message && o.message.role) || o.type || '?'; }catch(e){ o = null; }
  if(o && HARNESS_TYPES.has(ty)){ addHarness(o, ty); return; }
  harnessGroup = null;   // any non-harness line ends the current collapsed run
  const log = $('sv-log'), stick = atBottom(log);
  renderRecord(o, ty, data);
  trim(log);
  if(stick) log.scrollTop = log.scrollHeight;
}
function closeStream(){ if(es){ es.close(); es = null; } }
function openStream(key, past){
  closeStream();
  resetLog(); svStatus('connecting', 'warn');
  // A previous session replays its recorded transcript once from /history/, then the server sends `end`
  const s = es = past ? new EventSource('/history/' + encodeURIComponent(key) + '/' + encodeURIComponent(past))
                      : new EventSource('/stream/' + encodeURIComponent(key));
  s.onopen = () => { resetLog(); svStatus(past ? 'loading' : 'live', past ? 'warn' : 'live'); };  // reconnect re-sends the backfill
  s.onmessage = e => addLine(e.data);
  s.addEventListener('end', () => { svStatus(past ? 'previous session' : 'worker ended', ''); s.close(); if(es === s) es = null; });
  s.onerror = () => {
    if(s.readyState === EventSource.CLOSED){ if(es === s) es = null; explainClose(key, past); }
    else svStatus('reconnecting', 'warn');
  };
}
// EventSource can't see the HTTP status of a refused connect, so ask /stream-status WHY and say it plainly
// (403 not authorized / 503 over BROKER_STREAM_MAX / 404 worker already ended) instead of a red catch-all.
async function explainClose(key, past){
  if(past){ svStatus('previous session unavailable', ''); return; }
  let j = null;
  try{ const r = await fetch('/stream-status/' + encodeURIComponent(key), {cache:'no-store'}); if(r.ok) j = await r.json(); }catch(e){}
  if(key !== selected || selectedPast || es) return;   // the operator moved on while we asked
  const st = j ? j.status : null;
  if(st === 403) svStatus('not authorized', 'bad');
  else if(st === 503) svStatus('stream limit reached (' + j.max + ')', 'warn');
  else if(st === 404){
    const h = ((lastState && lastState.history) || []).find(x => x.key === key);
    svStatus(h ? 'worker ended: open previous session' : 'worker ended', '');
    if(h){
      const b = el('button', 'notice', 'This worker has already ended. Open its previous session (' + h.session + ') →');
      b.onclick = () => select(key, h.session);
      $('sv-log').appendChild(b);
    }
  }
  else svStatus('stream closed', '');
}
function setRaw(on){
  rawMode = on;
  $('sv-log').hidden = on;
  $('sv-raw').hidden = !on;
  $('raw').classList.toggle('on', on);
  const b = on ? $('sv-raw') : $('sv-log');
  b.scrollTop = b.scrollHeight;
}
document.addEventListener('click', e => {
  const b = e.target.closest('.sess[data-key]');
  if(b) select(b.dataset.key, b.dataset.past);
  if(e.target.closest('#h-more')){ histLimit += HIST_PAGE; tick(); }
});
$('raw').onclick = () => setRaw(!rawMode);
$('sv-x').onclick = () => {
  closeStream(); selected = null; selectedPast = null;
  $('d-main').hidden = true; $('d-empty').hidden = false;
  document.querySelectorAll('.sess.sel').forEach(b => b.classList.remove('sel'));
};

async function tick(){
  try{
    const r = await fetch('/state' + (histLimit > HIST_PAGE ? '?hist=' + histLimit : ''), {cache:'no-store'});
    if(!r.ok) throw new Error('HTTP ' + r.status);
    paint(await r.json());
    $('err').style.display = 'none';
  }catch(e){
    $('dot').className = 'dot bad';
    $('err').textContent = 'state fetch failed: ' + e.message;
    $('err').style.display = '';
  }
}
tick();
setInterval(tick, 5000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- selftest


def _selftest():
    """Exercises snapshot() against a fake broker. The shapes it reads (nested dicts, a Worker-ish
    object, absent attrs) are exactly what a real one has, but nothing here spawns anything."""
    class W:
        pass

    class Co:
        pass

    class Ad:
        pass

    class B:
        pass

    b = B()
    b.coalescer, b.echo_guard, b.rejected = Co(), Co(), 3
    b.coalescer.lock = __import__("threading").Lock()
    b.coalescer.deduped, b.coalescer.dedup_cleared = 7, 2
    b.coalescer.live = {"vikunja:task:1": {"state": "open_running", "forwards": 2,
                                           "events": [1, 2, 3], "fwd_buffer": [1], "expire": time.time() + 30}}
    b.coalescer.pending = {"vikunja:task:2": {"events": [1], "spawn_at": time.time() + 5}}
    b.echo_guard.dropped = 4
    b.adapter = Ad()
    b.adapter._lock = __import__("threading").Lock()
    w = W(); w.run_id = "r1"; w.session_id = "abcdef1234567890"; w.turns = 3
    w.tokens = 1500; w.forwards = 2; w.in_turn = True; w.done = False
    w.turn_started = time.time() - 12
    b.adapter._workers = {"vikunja:task:1": w}

    d = snapshot(b, cap=3)

    assert d["concurrency"] == {"cap": 3, "in_use": 1}, d["concurrency"]
    assert d["live"][0]["state"] == "open_running" and d["live"][0]["buffered"] == 1
    assert d["pending"][0]["spawn_in"] > 0
    assert d["workers"][0]["session"] == "abcdef12", "session_id must be truncated to 8"
    assert len(d["workers"][0]["session"]) == 8
    assert d["workers"][0]["turn_age"] > 11
    assert d["counters"] == {"deduped": 7, "dedup_cleared": 2, "echo_dropped": 4,
                             "allowlist_rejected": 3}, d["counters"]
    # event bodies must never ride the view; counts only
    assert "events" not in d["live"][0] or isinstance(d["live"][0]["events"], int)
    import json
    json.dumps(d)                                   # must be JSON-serializable end to end
    assert d["stream_enabled"] is stream.enabled()
    assert "<!DOCTYPE html>" in PAGE and "/state" in PAGE and "EventSource" in PAGE
    # fleet parsing
    assert model_label("claude-opus-5-5") == "opus 5.5" and model_label("local-vllm") == "local"
    assert ctx_tokens(13) == 130000 and ctx_tokens("41%") == 410000 and ctx_tokens("x") is None
    assert ledger_excerpt("- _note_") == "note" and hist_param("/state?hist=50") == 50
    assert len(d["history"]) <= HISTORY_PAGE and "history_total" in d
    print("dash selftest: OK")


if __name__ == "__main__":
    _selftest()
