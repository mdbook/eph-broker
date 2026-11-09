#!/usr/bin/env python3
"""actions.py: the dashboard's write path, `POST /actions/<verb>`. DEFAULT OFF.

The dash is a human surface behind an SSO reverse proxy, so it can't hold the operator token /rerun and /drain
use. Instead every request must pass ALL of these, checked before the body is parsed (any miss = 403, nothing
changes, one WEB-ACTION REJECT line):

  1. BROKER_ACTIONS_ENABLED = "1"/"true". Unset = /actions/* 404s and the dash hides its buttons.
  2. The proxy secret. The broker binds loopback, and anything on the host can reach loopback and set any
     header it likes. So the proxy proves it is the proxy: it adds a header (BROKER_ACTIONS_PROXY_HEADER,
     default X-Broker-Proxy-Secret) carrying a secret only it and the broker know. The broker reads the bytes
     from the env var NAMED by BROKER_ACTIONS_PROXY_SECRET_ENV (default BROKER_ACTIONS_PROXY_SECRET).
     Unset or empty = every action 403s. The proxy must also strip any client-sent copy of that header.
  3. The SSO identity: the same username / groups headers live streaming trusts (BROKER_STREAM_USER_HEADER,
     BROKER_STREAM_GROUPS_HEADER, BROKER_STREAM_GROUPS_SEP), and BROKER_ACTIONS_GROUP must be an exact member
     of the groups header. Unset group = every action 403s.
  4. A same-origin browser request: `X-Broker-Action: 1` and a JSON Content-Type (a cross-site form or
     no-cors fetch can send neither without a CORS preflight, which the broker never answers), and a
     Sec-Fetch-Site, when the browser sends one, of same-origin.

Verbs (JSON bodies):
  cancel     {"subject_key"}                     stop a live worker now (its turn is killed, the subject is
                                                 marked completed, buffered events are dropped)
  message    {"subject_key", "text"}             hand a live worker a message at its next turn boundary,
                                                 framed exactly like a forwarded ticket comment (UNTRUSTED)
  rerun      {"task_id", "note"?, "model"?}      Broker.ops_rerun, the same path as POST /rerun; `model` runs that
                                                 one run on a model or registry backend (refused on a pinned row)
  drain      {"enabled": bool}                   Broker.set_drain, the same path as POST /drain
  move-to-front {"subject_key"}                  select the next eligible waiting subject (one-shot)
  reconcile  {"subject_key", "decision", "note"?} Broker.reconcile_recovery, the same path as POST /reconcile
  settings   {"rows"?, "row_concurrency"?}       edit the routing overrides file (routing_overrides.edit): per-row
                                                 backend / model_pin, per-group concurrency caps; null = default.
                                                 GET /actions/settings (same proxy + identity check) reads them.

Audit: every authorised request logs one `WEB-ACTION user=.. verb=.. target=.. result=..` line, emits an
`action.taken` event (events.py, when EVENTS_WEBHOOK_URL is set) and, when BROKER_ACTIONS_AUDIT_HANDLE is set,
DMs that handle the same line through notify.dm. A verb that throws is audited as error-<exception class>.
Refusals always log a WEB-ACTION REJECT line; at most one a minute also goes to the event and the handle.
"""
import hmac, json, logging, os, re, threading, time

import events
import notify
import stream
from routing_overrides import OVERRIDES

log = logging.getLogger("eph-broker.actions")

VERBS = ("cancel", "message", "rerun", "drain", "move-to-front", "reconcile", "settings")
MESSAGE_MAX = 4000
SECRET_MIN = 16            # a shorter proxy secret is refused (fail-closed), logged once
REJECT_SINK_EVERY = 60     # seconds: at most one refusal per window goes to the event/audit sink
_warned_short = set()
_last_reject = [float("-inf")]
_SAFE = re.compile(r"[^A-Za-z0-9._:/@,+-]")


def _safe(v, n=120):
    return _SAFE.sub("?", str(v))[:n] or "-"


def enabled():
    return (os.getenv("BROKER_ACTIONS_ENABLED") or "").strip().lower() in ("1", "true")


def _proxy_ok(headers):
    name = os.getenv("BROKER_ACTIONS_PROXY_SECRET_ENV") or "BROKER_ACTIONS_PROXY_SECRET"
    secret = os.getenv(name)
    if secret and len(secret) < SECRET_MIN:
        if name not in _warned_short:
            _warned_short.add(name)
            log.error("%s is under %d characters -> every action 403s (fail-closed)", name, SECRET_MIN)
        return False
    got = headers.get(os.getenv("BROKER_ACTIONS_PROXY_HEADER") or "X-Broker-Proxy-Secret")
    return bool(secret and got) and hmac.compare_digest(secret.encode(), str(got).encode("utf-8", "replace"))


def _duplicated(headers):
    """A trusted header sent twice. Lookups take the first copy, so a proxy that appends instead of replacing
    would let a client's own copy win. Refuse instead of guessing."""
    get_all = getattr(headers, "get_all", None)
    if get_all is None:
        return False
    names = (os.getenv("BROKER_ACTIONS_PROXY_HEADER") or "X-Broker-Proxy-Secret",
             os.getenv("BROKER_STREAM_USER_HEADER"), os.getenv("BROKER_STREAM_GROUPS_HEADER"))
    return any(n and len(get_all(n.strip()) or []) > 1 for n in names)


def check(headers):
    """(allowed, username, reason). Pure over headers + env, so tests drive it directly."""
    if _duplicated(headers):
        return False, "", "duplicate-header"
    if not _proxy_ok(headers):
        return False, "", "proxy-secret"
    ok, user = stream.authorized(headers, group_var="BROKER_ACTIONS_GROUP")
    if not ok:
        return False, user, "identity"
    if headers.get("X-Broker-Action") != "1" or \
            not (headers.get("Content-Type") or "").split(";")[0].strip().lower() == "application/json":
        return False, user, "not-same-origin"
    if (headers.get("Sec-Fetch-Site") or "same-origin") != "same-origin":
        return False, user, "cross-site"
    return True, user, ""


def audit(user, verb, target, result):
    line = f"WEB-ACTION user={_safe(user, 64)} verb={_safe(verb, 16)} target={_safe(target, 240)} result={_safe(result, 64)}"
    log.warning(line)
    _sink(line, target, user, verb, result)


def reject(user, verb, why):
    """A refused request. Always logged; the event/audit sink gets at most one per REJECT_SINK_EVERY so a
    probe shows up off-box without letting anyone flood the bus."""
    line = f"WEB-ACTION REJECT verb={_safe(verb, 16)} reason={why} user={_safe(user, 64)}"
    log.warning(line)
    now = time.monotonic()
    if now - _last_reject[0] >= REJECT_SINK_EVERY:
        _last_reject[0] = now
        _sink(line, "-", user, verb, "rejected-" + why)


def _sink(line, target, user, verb, result):
    extra = {"change": _safe(target, 240)} if verb == "settings" else {}
    events.emit("action.taken", subject_key=str(target) if target not in (None, "-") and not extra else None,
                user=str(user)[:64], verb=_safe(verb, 16), result=result, **extra)
    handle = (os.getenv("BROKER_ACTIONS_AUDIT_HANDLE") or "").strip()
    if handle:   # a bus send shells out; never hold the request thread on it
        threading.Thread(target=notify.dm, args=(handle, line), daemon=True).start()


def run(broker, verb, body, user):
    """Dispatch one authorised action. Returns (http_code, json_body)."""
    host = getattr(broker, "module_host", None)
    if verb == "module_reload" and host:
        return host.reload(body.get("name"))
    key = body.get("subject_key")
    if verb in ("cancel", "message", "reconcile", "move-to-front") and not (isinstance(key, str) and key):
        return 400, {"ok": False, "error": "subject_key must be a non-empty string"}
    if verb == "move-to-front":
        return broker.move_to_front(key, caller=f"web:{_safe(user, 64)}")
    if verb == "cancel":
        dropped = broker.liveness.cancel(key, f"cancelled from the dashboard by {_safe(user, 64)}")
        if dropped is None:
            return 409, {"ok": False, "error": "no live worker on that subject"}
        if dropped == "starting":
            return 409, {"ok": False, "error": "still starting (for example waiting on a repository lock); "
                                               "cancel it once it is running"}
        return 200, {"ok": True, "subject_key": key, "dropped_buffered": dropped}
    if verb == "message":
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "error": "text must be a non-empty string"}
        if len(text) > MESSAGE_MAX:
            return 400, {"ok": False, "error": f"text over {MESSAGE_MAX} characters"}
        if not broker.coalescer.forward_message(key, text, f"web:{user}"):
            return 409, {"ok": False, "error": "no live worker on that subject"}
        return 200, {"ok": True, "subject_key": key, "queued": True}
    if verb == "rerun":
        return broker.ops_rerun(body.get("task_id"), body.get("note"), caller=f"web:{_safe(user, 64)}",
                                model=body.get("model"))
    if verb == "drain":
        if type(body.get("enabled")) is not bool:
            return 400, {"ok": False, "error": "enabled must be a boolean"}
        return 200, {"ok": True, "drain": broker.set_drain(body["enabled"])}
    if verb == "reconcile":
        return broker.reconcile_recovery(key, body.get("decision"), body["note"] if "note" in body else "",
                                         caller=f"web:{_safe(user, 64)}")
    if verb == "settings":
        rows, caps = body.get("rows"), body.get("row_concurrency")
        if (rows is None and caps is None) or any(x is not None and not isinstance(x, dict) for x in (rows, caps)):
            return 400, {"ok": False, "error": "send rows and/or row_concurrency objects"}
        try:
            changes = OVERRIDES.edit(rows, caps)
        except ValueError as e:         # our own messages: they name the field, never echo file contents
            return 400, {"ok": False, "error": str(e)}
        except OSError as e:
            log.error("settings edit not saved: %s", type(e).__name__)
            return 409, {"ok": False, "error": "could not save: the broker cannot write the routing overrides "
                                               "file, or it changed during the edit"}
        return 200, {"ok": True, "changes": changes, "settings": OVERRIDES.view()}
    verbs = VERBS + (("module_reload",) if host else ())
    return 404, {"ok": False, "error": "unknown action; one of " + ", ".join(verbs)}


def _settings_target(body):
    """The audit target for a REFUSED settings edit: what was asked, e.g. code.model_pin:opus,cap.code:1. A saved
    edit is audited with the file's effective before/after change list instead (routing_overrides.edit)."""
    parts = []
    for row, fields in sorted((body.get("rows") or {}).items()) if isinstance(body.get("rows"), dict) else ():
        for field, value in sorted(fields.items()) if isinstance(fields, dict) else ():
            parts.append(f"{row}.{field}:{'default' if value is None else value}")
    caps = body.get("row_concurrency")
    for group, cap in sorted(caps.items()) if isinstance(caps, dict) else ():
        parts.append(f"cap.{group}:{'default' if cap is None else cap}")
    return ",".join(parts) or "-"


def serve(handler, broker, verb):
    """POST /actions/<verb>. Auth before the body is parsed; every authorised request is audited."""
    if not enabled():
        return handler._reply(404, {"error": "not found"})
    raw = handler._body()
    if raw is None:
        return None
    ok, user, why = check(handler.headers)
    if not ok:
        reject(user, verb, why)
        return handler._reply(403, {"ok": False, "error": "action not authorised"})
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, TypeError):
        body = None
    if not isinstance(body, dict):
        audit(user, verb, "-", "bad-json")
        return handler._reply(400, {"ok": False, "error": "body must be a JSON object"})
    target = _settings_target(body) if verb == "settings" else \
        body.get("name") if verb == "module_reload" and getattr(broker, "module_host", None) else \
        body.get("subject_key") or (f"vikunja:task:{body['task_id']}" if "task_id" in body else "-")
    try:
        code, out = run(broker, verb, body, user)
    except BaseException as e:      # a verb that throws may already have changed state: audit, then let do_POST answer
        audit(user, verb, target, "error-" + type(e).__name__)
        raise
    if verb == "settings" and code < 300:     # audit what the file actually changed, not what the body claimed
        target = ",".join(out["changes"]) or "no-change"
    audit(user, verb, target, "ok" if code < 300 else f"refused-{code}")
    return handler._reply(code, out)


def whoami(handler):
    """GET /actions: should the dash draw controls for this viewer? Same proxy + identity checks as a POST
    (the CSRF half doesn't apply to a GET). Never says WHY a viewer is refused."""
    if not enabled():
        return handler._reply(200, {"enabled": False, "allowed": False})
    allowed = not _duplicated(handler.headers) and _proxy_ok(handler.headers) and stream.authorized(handler.headers, group_var="BROKER_ACTIONS_GROUP")[0]
    return handler._reply(200, {"enabled": True, "allowed": bool(allowed)})


def settings_view(handler):
    """GET /actions/settings: the Settings card's data (routing_overrides.view). Same checks as GET /actions;
    a viewer who may not act gets 403, so backend names never reach a read-only viewer."""
    if not enabled():
        return handler._reply(404, {"error": "not found"})
    if _duplicated(handler.headers) or not (_proxy_ok(handler.headers) and stream.authorized(handler.headers, group_var="BROKER_ACTIONS_GROUP")[0]):
        return handler._reply(403, {"error": "not authorised"})
    return handler._reply(200, OVERRIDES.view())
