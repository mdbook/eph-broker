#!/usr/bin/env python3
"""events.py: signed outbound run events, for n8n or any other automation that wants to react to the broker.

Off unless EVENTS_WEBHOOK_URL is set. Each event is one JSON POST:

    {"v": 1, "id": "<hex>", "event": "run.started", "ts": 1760000000, "subject_key": "vikunja:task:42", ...}

with the headers X-Eph-Event (the event name), X-Eph-Delivery (the id) and X-Eph-Signature: sha256=<hex HMAC-SHA256
of the raw body>, keyed by the secret in the env var named by EVENTS_WEBHOOK_SECRET_ENV. Signing is not optional:
with the URL set and the secret empty nothing is sent (logged), so a receiver can always verify. `ts` and `id` sit
inside the signed body, so a receiver can also refuse a stale or replayed delivery.

Events (docs/events.md has the full field list):
  run.started    a worker was spawned for a subject
  run.finished   a worker's subject closed: outcome closed | exited | rejected | force_closed
  run.escalated  an escalation went to the operator (the same throttle as notify.wake_ops)
  mr.opened      a code worker's branch got its merge request

Delivery is off the caller's thread: emit() only queues (it is called under the coalescer lock), one sender thread
POSTs, retrying a 5xx or network error twice. A full queue drops the event (logged, counted). emit() never raises:
a lost event must never break the worker path that produced it.
"""
import collections, hashlib, hmac, ipaddress, json, logging, os, queue, threading, time, uuid
import urllib.error, urllib.parse, urllib.request

import site_env  # noqa: F401  (loads config/site.env before the reads below)

log = logging.getLogger("eph-broker.events")

KINDS = ("run.started", "run.finished", "run.escalated", "mr.opened")
EVENTS_WEBHOOK_URL = os.getenv("EVENTS_WEBHOOK_URL", "").strip()
EVENTS_WEBHOOK_SECRET_ENV = os.getenv("EVENTS_WEBHOOK_SECRET_ENV", "EVENTS_WEBHOOK_SECRET")   # NAME only
# Comma list of the events to send (default all). An unknown name stops the import: a typo must not silently mute one.
_kinds = [k.strip() for k in os.getenv("EVENTS_WEBHOOK_KINDS", "").split(",") if k.strip()]
for _k in _kinds:
    if _k not in KINDS:
        raise ValueError(f"EVENTS_WEBHOOK_KINDS: unknown event {_k!r}, expected some of {', '.join(KINDS)}")
EVENTS_WEBHOOK_KINDS = frozenset(_kinds or KINDS)

_QUEUE_MAX = 1000
_RETRY_DELAYS = (2, 10)        # after the first try; only a 5xx or a network error retries
_TIMEOUT_S = 10

_q = queue.Queue(maxsize=_QUEUE_MAX)
_start_lock = threading.Lock()
_sender = None
STATS = collections.Counter()  # queued, sent, failed, dropped, unsigned (surfaced on /health)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):   # a 3xx is a failure: following it could leave the url_ok() host
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def url_ok(url):
    """https anywhere, plain http only to a loopback host (the same rule as the notify webhook)."""
    u = urllib.parse.urlparse(url)
    if u.scheme == "https" and u.hostname:
        return True
    if u.scheme != "http" or not u.hostname:
        return False
    try:
        return u.hostname == "localhost" or ipaddress.ip_address(u.hostname).is_loopback
    except ValueError:
        return False


def enabled():
    return bool(EVENTS_WEBHOOK_URL)


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def emit(kind, subject_key=None, **fields):
    """Queue one event. Returns True if queued. Never raises, never blocks."""
    try:
        if not EVENTS_WEBHOOK_URL or kind not in EVENTS_WEBHOOK_KINDS:
            return False
        evt = {"v": 1, "id": uuid.uuid4().hex, "event": kind, "ts": int(time.time()), "subject_key": subject_key,
               **fields}
        try:
            _q.put_nowait(evt)
        except queue.Full:
            STATS["dropped"] += 1
            log.warning("events: queue full, dropped %s for %s", kind, subject_key)
            return False
        STATS["queued"] += 1
        _ensure_sender()
        return True
    except Exception as e:
        log.warning("events: emit %s failed: %s", kind, e)
        return False


def _ensure_sender():
    global _sender
    with _start_lock:
        if _sender is None or not _sender.is_alive():
            _sender = threading.Thread(target=_run, name="eph-events", daemon=True)
            _sender.start()


def _run():
    while True:
        evt = _q.get()
        try:
            deliver(evt)
        except Exception as e:   # deliver never raises; belt and braces so the thread survives
            log.warning("events: sender error: %s", e)
        finally:
            _q.task_done()


def deliver(evt, sleep=time.sleep):
    """POST one event (with retries). True on a 2xx. Never raises."""
    if not url_ok(EVENTS_WEBHOOK_URL):
        STATS["failed"] += 1
        log.warning("events: EVENTS_WEBHOOK_URL is not https or loopback http, %s not sent", evt.get("event"))
        return False
    secret = os.getenv(EVENTS_WEBHOOK_SECRET_ENV, "")
    if not secret:
        STATS["unsigned"] += 1
        log.warning("events: $%s is empty, %s not sent (events are always signed)",
                    EVENTS_WEBHOOK_SECRET_ENV, evt.get("event"))
        return False
    body = json.dumps(evt, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "eph-broker-events/1",
               "X-Eph-Event": str(evt.get("event")), "X-Eph-Delivery": str(evt.get("id")),
               "X-Eph-Signature": sign(secret, body)}
    delays = list(_RETRY_DELAYS)
    while True:
        try:
            with _opener.open(urllib.request.Request(EVENTS_WEBHOOK_URL, data=body, headers=headers,
                                                      method="POST"), timeout=_TIMEOUT_S) as r:
                code = r.status
            err = None
        except urllib.error.HTTPError as e:
            code, err = e.code, e
        except Exception as e:   # URLError, timeout, connection reset
            code, err = None, e
        if code is not None and 200 <= code < 300:
            STATS["sent"] += 1
            log.info("events: %s %s for %s (status=%d)", evt.get("event"), evt.get("id"), evt.get("subject_key"), code)
            return True
        if (code is not None and code < 500) or not delays:   # a 4xx won't get better on retry
            STATS["failed"] += 1
            log.warning("events: %s %s for %s failed: %s", evt.get("event"), evt.get("id"), evt.get("subject_key"),
                        err or f"status {code}")
            return False
        sleep(delays.pop(0))


def stats():
    return {"enabled": enabled(), "pending": _q.qsize(), **STATS}
