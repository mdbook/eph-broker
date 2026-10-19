#!/usr/bin/env python3
"""events.py self-tests: signed outbound run events.

stdlib assert only, same runner shape as test_notify.py. Deliveries go to a local http.server, so nothing leaves
the host.
"""
import hashlib, hmac, http.server, json, os, subprocess, sys, threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
import site_env  # noqa: F401

import events as E
import notify as N
from coalescer import Coalescer

PASS = []
SECRET_ENV = "EPH_TEST_EVENTS_SECRET"
os.environ[SECRET_ENV] = "s3cret"


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


class _Patch:
    """Set events module attributes for one block, restore after."""
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(E, k)
            setattr(E, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(E, k, v)
        E.STATS.clear()


class _Hook(http.server.BaseHTTPRequestHandler):
    got, statuses = [], []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Hook.got.append((dict(self.headers), body))
        code = _Hook.statuses.pop(0) if _Hook.statuses else 204
        self.send_response(code)
        if code in (301, 302, 307):
            self.send_header("Location", "/elsewhere")
        self.end_headers()

    def log_message(self, *a):
        pass


def _server(statuses=()):
    _Hook.got, _Hook.statuses = [], list(statuses)
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/hook"


def _evt(kind="run.started"):
    return {"v": 1, "id": "abc123", "event": kind, "ts": 1, "subject_key": "vikunja:task:1"}


def test_deliver_posts_signed_json():
    srv, url = _server()
    try:
        with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_SECRET_ENV=SECRET_ENV):
            assert E.deliver(_evt()) is True and E.STATS["sent"] == 1
    finally:
        srv.shutdown()
    (headers, body), = _Hook.got
    assert json.loads(body) == _evt(), body
    want = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["X-Eph-Signature"] == want, headers
    assert headers["X-Eph-Event"] == "run.started" and headers["X-Eph-Delivery"] == "abc123", headers
    ok("deliver POSTs the event as JSON with X-Eph-Event, X-Eph-Delivery and an HMAC-SHA256 X-Eph-Signature of the body")


def test_unsigned_or_bad_url_never_connects():
    srv, url = _server()
    try:
        with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_SECRET_ENV="EPH_TEST_EVENTS_UNSET"):
            assert E.deliver(_evt()) is False and E.STATS["unsigned"] == 1
        for bad in ("http://example.test/hook", "ftp://127.0.0.1/x", "not a url", ""):
            with _Patch(EVENTS_WEBHOOK_URL=bad, EVENTS_WEBHOOK_SECRET_ENV=SECRET_ENV):
                assert E.deliver(_evt()) is False and E.STATS["failed"] == 1, bad
    finally:
        srv.shutdown()
    assert _Hook.got == [], _Hook.got
    assert E.url_ok("https://n8n.example.test/webhook/x") and E.url_ok("http://localhost:5678/w")
    assert E.url_ok("http://[::1]:5678/w") and not E.url_ok("http://192.0.2.5/w")
    ok("no secret = nothing sent (events are always signed); non-https, non-loopback http never connects")


def test_retry_policy():
    slept = []
    srv, url = _server([503, 500, 200])
    try:
        with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_SECRET_ENV=SECRET_ENV):
            assert E.deliver(_evt(), sleep=slept.append) is True
    finally:
        srv.shutdown()
    assert len(_Hook.got) == 3 and slept == list(E._RETRY_DELAYS), (len(_Hook.got), slept)
    ids = {h["X-Eph-Delivery"] for h, _ in _Hook.got}
    assert ids == {"abc123"}, ids   # a retry is the same delivery, so a receiver can dedupe
    for codes, tries in (([400], 1), ([302], 1), ([500, 500, 500], 3)):
        slept = []
        srv, url = _server(codes)
        try:
            with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_SECRET_ENV=SECRET_ENV):
                assert E.deliver(_evt(), sleep=slept.append) is False and E.STATS["failed"] == 1, codes
        finally:
            srv.shutdown()
        assert len(_Hook.got) == tries, (codes, len(_Hook.got))   # 302 is not followed to /elsewhere
    ok("5xx retries twice with the same delivery id; 4xx and redirects fail at once (a redirect is never followed)")


def test_retry_on_connection_refused():
    import socket
    with socket.socket() as s:   # a port nothing listens on once this socket closes
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}/hook"
    _Hook.got, _Hook.statuses = [], []
    slept = []
    with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_SECRET_ENV=SECRET_ENV):
        assert E.deliver(_evt(), sleep=slept.append) is False and E.STATS["failed"] == 1
    assert slept == list(E._RETRY_DELAYS) and _Hook.got == [], (slept, _Hook.got)
    srv = []

    def listen_then_sleep(s):   # the receiver comes up during the first back-off
        slept.append(s)
        if not srv:
            srv.append(http.server.HTTPServer(("127.0.0.1", port), _Hook))
            threading.Thread(target=srv[0].serve_forever, daemon=True).start()
    slept = []
    try:
        with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_SECRET_ENV=SECRET_ENV):
            assert E.deliver(_evt(), sleep=listen_then_sleep) is True and E.STATS["sent"] == 1
    finally:
        for x in srv:
            x.shutdown()
    assert slept == list(E._RETRY_DELAYS[:1]) and len(_Hook.got) == 1, (slept, len(_Hook.got))
    ok("connection refused retries with the same back-off as a 5xx and fails after the last try; a receiver "
       "that comes up during a back-off gets the event once")


def test_emit_queues_and_the_sender_delivers():
    srv, url = _server()
    try:
        with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_SECRET_ENV=SECRET_ENV):
            assert E.emit("run.finished", "vikunja:task:7", outcome="closed") is True
            E._q.join()
            assert E.stats()["sent"] == 1 and E.stats()["pending"] == 0, E.stats()
    finally:
        srv.shutdown()
    (_, body), = _Hook.got
    got = json.loads(body)
    assert got["event"] == "run.finished" and got["subject_key"] == "vikunja:task:7" and got["outcome"] == "closed"
    assert got["v"] == 1 and len(got["id"]) == 32 and isinstance(got["ts"], int), got
    with _Patch(EVENTS_WEBHOOK_URL=""):
        assert E.emit("run.started", "k") is False and E.stats()["enabled"] is False
    with _Patch(EVENTS_WEBHOOK_URL=url, EVENTS_WEBHOOK_KINDS=frozenset({"mr.opened"})):
        assert E.emit("run.started", "k") is False and E.STATS["queued"] == 0
    ok("emit queues and returns at once, the sender thread delivers; off when the URL is unset; "
       "EVENTS_WEBHOOK_KINDS filters")


def test_full_queue_drops_and_counts():
    import queue
    with _Patch(EVENTS_WEBHOOK_URL="https://n8n.example.test/w", _q=queue.Queue(maxsize=1),
                _ensure_sender=lambda: None):
        assert E.emit("run.started", "a") is True
        assert E.emit("run.started", "b") is False and E.STATS["dropped"] == 1
    ok("a full queue drops the event (logged, counted) instead of blocking the caller")


def test_unknown_kind_stops_import():
    env = {**os.environ, "EVENTS_WEBHOOK_KINDS": "run.started,run.finshed"}
    r = subprocess.run([sys.executable, "-c", "import events"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "run.finshed" in r.stderr, r.stderr[-300:]
    ok("a typo in EVENTS_WEBHOOK_KINDS stops the import")


def test_coalescer_lifecycle_events():
    got = []
    c = Coalescer(emit=lambda kind, key, **f: got.append((kind, key, f)), row_of={"k1": "general"}.get)
    ev = [{"source": "vikunja", "event_name": "task.updated"}]
    for key, close, outcome in (("k1", lambda k: c.finish_close(k), "closed"),
                                ("k2", lambda k: c.force_pop(k), "force_closed"),
                                ("k3", lambda k: c.settle_self(k), "exited"),
                                ("k4", lambda k: c.settle_self(k, mark_completed=False), "rejected")):
        with c.lock:
            c._would_spawn(key, ev, 0)
        close(key)
        close(key)   # a second close finds nothing and emits nothing
    started = [g for g in got if g[0] == "run.started"]
    finished = [(g[1], g[2]["outcome"]) for g in got if g[0] == "run.finished"]
    assert started[0] == ("run.started", "k1", {"events": 1, "source": "vikunja", "row": "general"}), started
    assert [g[1] for g in started] == ["k1", "k2", "k3", "k4"], started
    assert finished == [("k1", "closed"), ("k2", "force_closed"), ("k3", "exited"), ("k4", "rejected")], finished
    c2 = Coalescer(emit=lambda *a, **k: 1 / 0)   # a raising sink never breaks spawn or close
    with c2.lock:
        c2._would_spawn("k", ev, 0)
    assert c2.finish_close("k") is not None
    ok("coalescer emits run.started at spawn and run.finished with outcome closed/force_closed/exited/rejected; "
       "a raising sink is swallowed")


def test_wake_ops_emits_run_escalated_under_the_same_throttle():
    got, saved, saved_handle = [], E.emit, N.OPS_HANDLE
    E.emit = lambda kind, key=None, **f: got.append((kind, {"subject_key": key, **f}))
    N.OPS_HANDLE = ""   # journal only: no DM leaves the test
    try:
        for i in range(N._PER_SUBJECT_CAP + 2):
            N.wake_ops(f"stuck {i}", subject="esc-1")
    finally:
        E.emit, N.OPS_HANDLE = saved, saved_handle
        N._subject_ts.clear(); N._subject_suppressed.clear(); N._global_ts.clear()
    assert [k for k, _ in got] == ["run.escalated"] * N._PER_SUBJECT_CAP, got
    assert got[0][1] == {"subject_key": "esc-1", "text": "stuck 0"}, got[0]
    ok("wake_ops emits run.escalated once per admitted escalation (throttled ones send no event either)")


def test_n8n_example_workflows_hang_together():
    """docs/n8n-*.json: valid JSON, every connection points at a node, and the header names match the broker's."""
    import glob
    files = sorted(glob.glob(os.path.join(ROOT, "docs", "n8n-*.json")))
    assert len(files) >= 4, files
    text = {}
    for f in files:
        wf = json.load(open(f))
        names = {n["name"] for n in wf["nodes"]}
        for src, outs in wf["connections"].items():
            assert src in names, (f, src)
            assert all(c["node"] in names for out in outs["main"] for c in out), (f, src)
        text[os.path.basename(f)] = open(f).read()
    assert "X-Broker-HMAC" in text["n8n-vikunja-tap.json"] and "broker-run:" in text["n8n-vikunja-tap.json"]
    assert "x-eph-signature" in text["n8n-run-events.json"]
    assert all(f'"outputKey": "{k}"' in text["n8n-run-events.json"] for k in E.KINDS)
    assert "/runs/" in text["n8n-run-status.json"]
    ok("docs/n8n-*.json parse, connect to real nodes, and use the broker's header names and event kinds")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} events self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
