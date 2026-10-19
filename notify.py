#!/usr/bin/env python3
"""notify.py: wake the escalation handle over the bus when an ephemeral worker escalates.

The broker's escalation sinks used to be unwired: EnvelopeParser.escalate()
(envelope.py), the CCAdapter HARD-KILL (cc.py), and the LivenessDriver force-close (liveness.py) all
had log-only defaults, so a degraded or wedged worker only ever wrote to the journal and nobody
learned eph was rotting. This module is the LOUD sink: it shells out to the `zbus` CLI to DM
the escalation handle, so an escalation actually reaches a human or agent.

VALUE-FREE: no secrets/credentials here; zbus owns auth. The broker process itself carries no
ZBUS_ENV (it runs from a systemd --user unit that doesn't set it), so we point zbus at the coder-agent
bot-env file BY PATH in the subprocess env; the file's bytes are never read into this process. Structured
(like poster.py) as a pure argv builder (unit-testable without executing anything) plus a thin exec
wrapper that shells out and NEVER raises: an escalation-notify failure is logged and swallowed, because
a lost DM must never wedge the worker path that is already trying to report a problem.
"""
import hashlib, hmac, ipaddress, json, logging, os, re, smtplib, ssl, subprocess, threading, time
import urllib.parse, urllib.request
from email.message import EmailMessage

import site_env  # noqa: F401  (loads config/site.env before the NOTIFY_ZBUS / OPS_HANDLE reads below)
import events

log = logging.getLogger("eph-broker.notify")

# The zbus binary and default bot-env PATH come from site.env. Unset NOTIFY_ZBUS means the exec fails (logged, False).
ZBUS = os.getenv("NOTIFY_ZBUS", "")
# The escalation target (site.env). Unset means escalations are logged only, never DMed anywhere.
OPS_HANDLE = os.getenv("OPS_HANDLE", "")
# The broker has no ZBUS_ENV of its own, so point zbus at the coder-agent bot-env file BY PATH (zbus reads
# $ZBUS_ENV as a LITERAL path to the per-handle bot-env; the secret bytes never enter this process).
ZBUS_ENV = os.getenv("NOTIFY_ZBUS_ENV", "")   # unset -> ZBUS_ENV left as inherited (zbus's own default)
# Identifiable prefix so an escalation DM is greppable and obviously from eph, not a stray bus message.
MSG_PREFIX = "[eph-broker escalation] "

# Where escalations and hand-off DMs go: "zbus" (the default, everything above), "webhook" or "email". The
# handle (OPS_HANDLE, CD_ANSIBLE_HANDLE, ...) is the recipient on every channel: a bus handle, the webhook's
# `to` field, or an email address. An unknown value stops the broker (a typo must not drop every escalation).
NOTIFY_BACKEND = os.getenv("NOTIFY_BACKEND", "zbus").strip().lower()
NOTIFY_BACKENDS = ("zbus", "webhook", "email")
if NOTIFY_BACKEND not in NOTIFY_BACKENDS:
    raise ValueError(f"NOTIFY_BACKEND={NOTIFY_BACKEND!r}: expected one of {', '.join(NOTIFY_BACKENDS)}")
# webhook: POST {"to", "kind", "text"} as JSON. https, or http to a loopback host only. With the secret set (env
# var named by NOTIFY_WEBHOOK_SECRET_ENV) the body is signed: X-Eph-Signature: sha256=<hex HMAC of the body>.
NOTIFY_WEBHOOK_URL = os.getenv("NOTIFY_WEBHOOK_URL", "")
NOTIFY_WEBHOOK_SECRET_ENV = os.getenv("NOTIFY_WEBHOOK_SECRET_ENV", "NOTIFY_WEBHOOK_SECRET")   # NAME only
# email: one message per notification over SMTP (STARTTLS unless NOTIFY_SMTP_STARTTLS=0, with the server certificate
# verified; login when the user and the password env var, named by NOTIFY_SMTP_PASSWORD_ENV, are both set). A login
# with STARTTLS off is refused, so the password never goes out in clear.
NOTIFY_SMTP_HOST = os.getenv("NOTIFY_SMTP_HOST", "")
NOTIFY_SMTP_PORT = int(os.getenv("NOTIFY_SMTP_PORT", "587"))
NOTIFY_SMTP_STARTTLS = os.getenv("NOTIFY_SMTP_STARTTLS", "1").strip().lower() in ("1", "true", "yes", "on")
NOTIFY_SMTP_USER = os.getenv("NOTIFY_SMTP_USER", "")
NOTIFY_SMTP_PASSWORD_ENV = os.getenv("NOTIFY_SMTP_PASSWORD_ENV", "NOTIFY_SMTP_PASSWORD")   # NAME only
NOTIFY_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", "")


def _webhook_url_ok(url):
    u = urllib.parse.urlparse(url)
    if u.scheme == "https" and u.hostname:
        return True
    if u.scheme != "http" or not u.hostname:
        return False
    try:
        return u.hostname == "localhost" or ipaddress.ip_address(u.hostname).is_loopback
    except ValueError:
        return False


def _send_webhook(handle, text, kind):
    if not _webhook_url_ok(NOTIFY_WEBHOOK_URL):
        log.warning("notify: NOTIFY_WEBHOOK_URL unset or not https/loopback, %s to %s not sent", kind, handle)
        return False
    body = json.dumps({"to": handle, "kind": kind, "text": text}).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.getenv(NOTIFY_WEBHOOK_SECRET_ENV, "")
    if secret:
        headers["X-Eph-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        with events._opener.open(urllib.request.Request(NOTIFY_WEBHOOK_URL, data=body, headers=headers,
                                                        method="POST"), timeout=30) as r:
            code = r.status
    except Exception as e:   # HTTPError (non-2xx, or a 3xx: redirects are not followed), URLError, timeout
        log.warning("notify: webhook %s to %s failed: %s", kind, handle, e)
        return False
    log.info("notify: webhook %s to %s (status=%d len=%d)", kind, handle, code, len(text))
    return 200 <= code < 300


def _send_email(handle, text, kind):
    if not (NOTIFY_SMTP_HOST and NOTIFY_EMAIL_FROM and "@" in str(handle)):
        log.warning("notify: NOTIFY_SMTP_HOST/NOTIFY_EMAIL_FROM unset or %r is not an address, %s not sent",
                    handle, kind)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"] = NOTIFY_EMAIL_FROM, handle
    first = (text.strip().splitlines() or [""])[0]
    msg["Subject"] = f"[eph-broker] {kind}: {first[:80]}"
    msg.set_content(text)
    password = os.getenv(NOTIFY_SMTP_PASSWORD_ENV, "")
    if NOTIFY_SMTP_USER and password and not NOTIFY_SMTP_STARTTLS:
        log.error("notify: NOTIFY_SMTP_STARTTLS is off, refusing to send the SMTP password in clear, %s to %s "
                  "not sent", kind, handle)
        return False
    try:
        with smtplib.SMTP(NOTIFY_SMTP_HOST, NOTIFY_SMTP_PORT, timeout=30) as smtp:
            if NOTIFY_SMTP_STARTTLS:
                smtp.starttls(context=ssl.create_default_context())   # verifies the certificate and host name
            if NOTIFY_SMTP_USER and password:
                smtp.login(NOTIFY_SMTP_USER, password)
            smtp.send_message(msg)
    except Exception as e:   # smtplib/socket/ssl errors: logged, never raised
        log.warning("notify: email %s to %s failed: %s", kind, handle, e)
        return False
    log.info("notify: email %s to %s (len=%d)", kind, handle, len(text))
    return True


def _send_other(handle, text, kind):
    """The non-zbus channels. Same contract as the zbus paths: True on apparent success, never raises."""
    return (_send_webhook if NOTIFY_BACKEND == "webhook" else _send_email)(handle, text, kind)

# ------------------------------------------------------------------------------------------------
# Hardening: rate-limit and dedupe wake_ops so a FLAPPING subject can't burst-DM the escalation handle.
# ------------------------------------------------------------------------------------------------
# Every fail-closed reject / max-forwards / hard-kill / force-close / unparseable-envelope escalation
# DMs the handle, and an expensive model wakes on each one. A subject stuck in a re-spawn/hard-kill loop could
# fire dozens of DMs a minute. Throttle IN-PROCESS: at most _PER_SUBJECT_CAP DMs per distinct subject AND
# at most _GLOBAL_CAP DMs across ALL subjects, within a rolling _WINDOW_S window. Over cap we SUPPRESS the
# individual DM, but never SILENTLY: each suppression is logged (the trace is guaranteed) and counted, and
# the count is flushed as ONE summary line ("N escalation(s) suppressed for <subject> in the last <window>s")
# prepended to the next admitted DM for that subject. Thread-safe (the CC adapter calls sinks from worker
# threads) via a single lock around the counter maps. wake_ops still NEVER raises.
#
# Limitation: deliberately an in-memory dict + lock + timestamps, no external store. The broker is a single
# long-lived process, so per-process window state is sufficient; a count lost across a restart is a
# non-event (a restart already breaks any flap loop). Upgrade path if the broker ever goes multi-process:
# move the window state to a shared store (redis/sqlite) keyed the same way.
_WINDOW_S = 300           # rolling window (seconds)
_PER_SUBJECT_CAP = 3      # max individual DMs for one subject within the window
_GLOBAL_CAP = 20          # max individual DMs across ALL subjects within the window (global burst ceiling)

_throttle_lock = threading.Lock()
_subject_ts = {}          # subject -> [send timestamps still within the window]
_subject_suppressed = {}  # subject -> count of DMs suppressed since its last admitted DM
_global_ts = []           # send timestamps across ALL subjects still within the window


def _now():
    """Indirection so tests can advance the clock without sleeping (mirrors the subprocess.run monkeypatch
    the notify tests already use)."""
    return time.time()


def _subject_key(text):
    """Fallback dedupe key when a caller passes no explicit subject: normalize the text (collapse
    whitespace, mask digits and long hex tokens like run_ids so otherwise-identical escalations that differ
    only by a varying id still collapse) and hash it. Callers that HAVE a stable subject (e.g. escalate()
    passes the ticket key) should pass it explicitly; this is only the no-key backstop."""
    norm = " ".join(str(text).split()).lower()
    norm = re.sub(r"[0-9a-f]{8,}", "#", norm)   # mask hex ids (run_ids etc.)
    norm = re.sub(r"\d+", "#", norm)            # mask bare numbers
    return "auto:" + hashlib.sha256(norm.encode()).hexdigest()[:16]


def _admit(subject):
    """Rate-limit decision for one escalation. Returns (send, summary):
      send=True  -> emit the DM (this send is recorded against the window).
      send=False -> SUPPRESS the individual DM (already counted; the caller logs the trace).
      summary    -> a one-line 'N suppressed' flush to prepend to this DM, or None.
    Thread-safe; NEVER raises."""
    now = _now()
    cutoff = now - _WINDOW_S
    with _throttle_lock:
        st = [t for t in _subject_ts.get(subject, []) if t >= cutoff]   # prune aged-out
        gt = [t for t in _global_ts if t >= cutoff]
        if len(st) >= _PER_SUBJECT_CAP or len(gt) >= _GLOBAL_CAP:       # over cap -> suppress + count
            _subject_ts[subject] = st
            _global_ts[:] = gt
            _subject_suppressed[subject] = _subject_suppressed.get(subject, 0) + 1
            return False, None
        st.append(now); gt.append(now)                                  # admit -> record the send
        _subject_ts[subject] = st
        _global_ts[:] = gt
        n = _subject_suppressed.pop(subject, 0)                         # flush any pending suppressed count
        summary = (f"[{n} earlier escalation(s) for {subject} were suppressed in the last {_WINDOW_S}s]"
                   if n else None)
        return True, summary


def build_argv(zbus=ZBUS, handle=None):
    """Pure builder: the `zbus dm <handle> --stdin` argv (default handle = OPS_HANDLE). The body rides STDIN
    (not argv) so an arbitrary escalation text (newlines, quotes, HTML) is safe. Unit-testable without
    executing anything."""
    return [zbus, "dm", handle or OPS_HANDLE, "--stdin"]


def build_env(zbus_env=None):
    """Pure builder: the subprocess env with ZBUS_ENV pointed at the bot-env file by PATH (no secret bytes
    read here). Inherits the broker's env so zbus's own PATH/HOME resolve. `zbus_env` (a PATH) lets a
    caller speak as a different bot (the Zulip-wake reply goes out as the bot that was DMed); None keeps the
    default coder-agent identity."""
    env = os.environ.copy()
    if zbus_env or ZBUS_ENV:
        env["ZBUS_ENV"] = zbus_env or ZBUS_ENV
    return env


def wake_ops(text, subject=None, zbus=ZBUS):
    """Exec wrapper: DM the escalation handle with `[eph-broker escalation] <text>` (NOTIFY_BACKEND channel). Returns
    True on apparent success, False on ANY failure (logged, never raised); zbus owns auth, no secrets
    touched here.

    `subject` (optional) keys the rate limiter (see _admit): callers with a stable subject (the
    ticket/worker key) should pass it so a flapping subject dedupes cleanly; omitted -> a normalized hash
    of the text. An over-cap call is SUPPRESSED (logged, counted, summarised on the next admitted DM) and
    returns True (intentionally throttled, not a failure)."""
    subj = subject if subject is not None else _subject_key(text)
    send, summary = _admit(subj)
    if not send:
        log.warning("notify: SUPPRESSED escalation (rate-limit) subject=%s: %s", subj, str(text)[:200])
        return True                                   # throttled by policy, not an error
    body = f"{MSG_PREFIX}{summary}\n{text}" if summary else f"{MSG_PREFIX}{text}"
    events.emit("run.escalated", subj, text=str(text)[:2000])   # same throttle as the DM; no-op when off
    if not OPS_HANDLE:                                # no site handle: journal only, no zbus exec
        log.warning("notify: OPS_HANDLE unset, escalation not DMed: %s", body[:500])
        return False
    if NOTIFY_BACKEND != "zbus":
        return _send_other(OPS_HANDLE, body, "escalation")
    try:
        out = subprocess.run(
            build_argv(zbus), input=body, env=build_env(),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("notify: zbus exec failed: %s", e)
        return False
    if out.returncode != 0:
        log.warning("notify: zbus dm ops exit=%d stderr=%s",
                    out.returncode, (out.stderr or "").strip()[:500])
        return False
    log.info("notify: woke ops (len=%d)", len(body))
    return True


def flush_subject(subject=None, zbus=ZBUS):
    """Emit the ONE 'N suppressed' summary DM for a subject that flapped over-cap then
    went QUIET, and clear its pending count. wake_ops only flushes a suppressed count onto the NEXT admitted
    DM for that subject, but a subject whose worker just exited may never send another DM, so its final
    suppressed count would be lost to the log alone. Called at worker close (adapters/cc._run_worker finally)
    to close that gap.

    `subject=None` flushes ALL pending subjects (teardown sweep). Returns the number of suppressed escalations
    reported (0 = nothing pending = a clean no-op). Thread-safe (the counter map is read and cleared under the
    same lock wake_ops uses) and NEVER raises; a lost flush is a non-event, exactly like wake_ops.

    Limitation: the summary DM is sent directly (not routed back through _admit). It is a single terminal line
    per subject at exit, so it deliberately does not itself count against or get suppressed by the rate limiter."""
    with _throttle_lock:                              # snapshot + clear the pending counts under the lock,
        if subject is None:                          # then send OUTSIDE the lock (subprocess must not hold it)
            pending = {s: n for s, n in _subject_suppressed.items() if n}
            _subject_suppressed.clear()
        else:
            n = _subject_suppressed.pop(subject, 0)
            pending = {subject: n} if n else {}
    if pending and not OPS_HANDLE:                   # same rule as wake_ops: journal only, nothing sent
        log.warning("notify: OPS_HANDLE unset, suppressed escalation counts not flushed: %s", pending)
        return 0
    for subj, n in pending.items():
        body = (f"{MSG_PREFIX}[{n} escalation(s) for {subj} were suppressed in the last {_WINDOW_S}s "
                f"(final flush at worker exit)]")
        if NOTIFY_BACKEND != "zbus":
            _send_other(OPS_HANDLE, body, "escalation")
            continue
        try:
            out = subprocess.run(
                build_argv(zbus), input=body, env=build_env(),
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("notify: flush zbus exec failed subject=%s: %s", subj, e)
            continue
        if out.returncode != 0:
            log.warning("notify: flush zbus dm ops exit=%d subject=%s stderr=%s",
                        out.returncode, subj, (out.stderr or "").strip()[:500])
            continue
        log.info("notify: flushed %d suppressed escalation(s) for %s", n, subj)
    return sum(pending.values())


def dm(handle, text, zbus=ZBUS, zbus_env=None):
    """Send `text` VERBATIM (no escalation prefix, no rate limit) as a bus DM to `handle`, for
    machine-parsed hand-offs whose exact shape is a contract (e.g. the DEPLOY-HANDOFF line to the deploy bot).
    Same exec contract as wake_ops: True on apparent success, False on ANY failure (logged, never raised).
    `zbus_env` = the bot-env PATH to send as (default: the coder-agent identity); never read here.
    An empty handle (unset site.env value) is logged and refused, never exec'd."""
    if not handle:
        log.warning("notify: no handle configured, DM not sent: %s", str(text)[:200])
        return False
    if NOTIFY_BACKEND != "zbus":                     # zbus_env is a bus identity: nothing to map it to here
        return _send_other(handle, text, "dm")
    try:
        out = subprocess.run(
            build_argv(zbus, handle), input=text, env=build_env(zbus_env),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("notify: zbus dm %s exec failed: %s", handle, e)
        return False
    if out.returncode != 0:
        log.warning("notify: zbus dm %s exit=%d stderr=%s",
                    handle, out.returncode, (out.stderr or "").strip()[:500])
        return False
    log.info("notify: dm %s (len=%d)", handle, len(text))
    return True
