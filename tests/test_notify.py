#!/usr/bin/env python3
"""notify.py self-tests: the NOTIFY_BACKEND channels (zbus, webhook, email).

stdlib assert only, same runner shape as test_routes.py. The webhook goes to a local http.server, the email to a
fake smtplib.SMTP and zbus to a fake script, so nothing leaves the host.
"""
import hashlib, hmac, http.server, json, os, ssl, stat, subprocess, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
import site_env  # noqa: F401

import notify as N

PASS = []
_TMP = tempfile.TemporaryDirectory()


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


class _Patch:
    """Set notify module attributes for one block, restore after."""
    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(N, k)
            setattr(N, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(N, k, v)
        N._subject_ts.clear(); N._subject_suppressed.clear(); N._global_ts.clear()


class _Hook(http.server.BaseHTTPRequestHandler):
    got, status = [], 204

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Hook.got.append((dict(self.headers), body))
        self.send_response(_Hook.status)
        if 300 <= _Hook.status < 400:
            self.send_header("Location", "/elsewhere")
        self.end_headers()

    def do_GET(self):
        _Hook.got.append((dict(self.headers), b"GET " + self.path.encode()))
        self.send_response(200); self.end_headers()

    def log_message(self, *a):
        pass


def _server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/hook"


def test_webhook_posts_json_and_signs_it():
    srv, url = _server()
    _Hook.got, _Hook.status = [], 204
    os.environ["EPH_TEST_HOOK_SECRET"] = "s3cret"
    try:
        with _Patch(NOTIFY_BACKEND="webhook", NOTIFY_WEBHOOK_URL=url, NOTIFY_WEBHOOK_SECRET_ENV="EPH_TEST_HOOK_SECRET",
                    OPS_HANDLE="ops@chat.example.test"):
            assert N.wake_ops("worker stuck", subject="t1") is True
            assert N.dm("deploy@chat.example.test", "DEPLOY-HANDOFF x") is True
            assert N.flush_subject() == 0
        (h1, b1), (h2, b2) = _Hook.got
        assert json.loads(b1) == {"to": "ops@chat.example.test", "kind": "escalation",
                                  "text": N.MSG_PREFIX + "worker stuck"}, b1
        assert json.loads(b2) == {"to": "deploy@chat.example.test", "kind": "dm", "text": "DEPLOY-HANDOFF x"}, b2
        for h, b in ((h1, b1), (h2, b2)):
            assert h["X-Eph-Signature"] == "sha256=" + hmac.new(b"s3cret", b, hashlib.sha256).hexdigest()
        _Hook.got, _Hook.status = [], 500
        with _Patch(NOTIFY_BACKEND="webhook", NOTIFY_WEBHOOK_URL=url, NOTIFY_WEBHOOK_SECRET_ENV="EPH_TEST_UNSET_X"):
            assert N.dm("a@b.test", "x") is False                      # non-2xx -> False, never raises
        assert "X-Eph-Signature" not in _Hook.got[0][0]                 # no secret -> unsigned
    finally:
        os.environ.pop("EPH_TEST_HOOK_SECRET", None)
        srv.shutdown()
    ok("webhook: POSTs {to, kind, text}, HMAC-signs it when the secret is set, a 500 is a logged False")


def test_webhook_redirect_is_a_failed_send():
    srv, url = _server()
    try:
        for status in (302, 307):
            _Hook.got, _Hook.status = [], status
            with _Patch(NOTIFY_BACKEND="webhook", NOTIFY_WEBHOOK_URL=url, OPS_HANDLE="ops@chat.example.test"):
                assert N.dm("a@b.test", "x") is False, status
            assert len(_Hook.got) == 1 and not _Hook.got[0][1].startswith(b"GET "), _Hook.got   # not followed
    finally:
        _Hook.status = 204
        srv.shutdown()
    ok("webhook: a 302 or 307 answer is a failed send and the redirect is not followed")


def test_webhook_refuses_plain_http_off_loopback():
    for url, good in (("https://hooks.example.test/x", True), ("http://127.0.0.1:9/x", True),
                      ("http://localhost:9/x", True), ("http://[::1]:9/x", True), ("http://hooks.example.test/x", False),
                      ("http://192.0.2.5/x", False), ("ftp://127.0.0.1/x", False), ("", False), ("https:///x", False)):
        assert N._webhook_url_ok(url) is good, url
    with _Patch(NOTIFY_BACKEND="webhook", NOTIFY_WEBHOOK_URL="http://hooks.example.test/x"):
        opener = N.events._opener
        N.events._opener = None                                         # any connect attempt raises
        try:
            assert N.dm("a@b.test", "x") is False
        finally:
            N.events._opener = opener
    ok("webhook: https anywhere, http only to loopback; anything else is refused without connecting")


class _FakeSMTP:
    sent, fail = [], False

    def __init__(self, host, port, timeout=None):
        self.calls = [("connect", host, port)]
        if _FakeSMTP.fail:
            raise OSError("connection refused")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        _FakeSMTP.sent.append(self.calls)

    def starttls(self, context=None):
        assert isinstance(context, ssl.SSLContext), context
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname, "unverified STARTTLS context"
        self.calls.append(("starttls",))

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def send_message(self, msg):
        self.calls.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content()))


def test_email_sends_one_message():
    real = N.smtplib.SMTP
    N.smtplib.SMTP = _FakeSMTP
    os.environ["EPH_TEST_SMTP_PW"] = "pw"
    try:
        _FakeSMTP.sent, _FakeSMTP.fail = [], False
        with _Patch(NOTIFY_BACKEND="email", NOTIFY_SMTP_HOST="smtp.example.test", NOTIFY_SMTP_PORT=587,
                    NOTIFY_SMTP_STARTTLS=True, NOTIFY_SMTP_USER="eph", NOTIFY_SMTP_PASSWORD_ENV="EPH_TEST_SMTP_PW",
                    NOTIFY_EMAIL_FROM="eph@example.test", OPS_HANDLE="ops@example.test"):
            assert N.wake_ops("disk full\nmore detail", subject="t2") is True
            assert N.dm("not-an-address", "x") is False                 # handle must be an address
        (calls,) = _FakeSMTP.sent
        assert calls[:3] == [("connect", "smtp.example.test", 587), ("starttls",), ("login", "eph", "pw")], calls
        _, frm, to, subj, body = calls[3]
        assert (frm, to) == ("eph@example.test", "ops@example.test")
        assert subj == "[eph-broker] escalation: " + (N.MSG_PREFIX + "disk full")[:80], subj
        assert body.strip() == N.MSG_PREFIX + "disk full\nmore detail"
        _FakeSMTP.sent = []
        with _Patch(NOTIFY_BACKEND="email", NOTIFY_SMTP_HOST="smtp.example.test", NOTIFY_SMTP_STARTTLS=False,
                    NOTIFY_SMTP_USER="", NOTIFY_EMAIL_FROM="eph@example.test"):
            assert N.dm("ops@example.test", "x") is True
            _FakeSMTP.fail = True
            assert N.dm("ops@example.test", "x") is False               # SMTP error -> False, never raises
        assert [c[0] for c in _FakeSMTP.sent[0]] == ["connect", "send"]  # no STARTTLS, no login without a user
        with _Patch(NOTIFY_BACKEND="email", NOTIFY_SMTP_HOST="", NOTIFY_EMAIL_FROM="eph@example.test"):
            assert N.dm("ops@example.test", "x") is False               # no host -> refused
    finally:
        N.smtplib.SMTP = real
        _FakeSMTP.fail = False
        os.environ.pop("EPH_TEST_SMTP_PW", None)
    ok("email: one SMTP message (STARTTLS + login when configured), bad handle/host or SMTP error is a False")


def test_email_starttls_verifies_and_never_logs_in_cleartext():
    real = N.smtplib.SMTP
    N.smtplib.SMTP = _FakeSMTP
    os.environ["EPH_TEST_SMTP_PW"] = "pw"
    try:
        _FakeSMTP.sent, _FakeSMTP.fail = [], False
        base = dict(NOTIFY_BACKEND="email", NOTIFY_SMTP_HOST="smtp.example.test", NOTIFY_SMTP_USER="eph",
                    NOTIFY_SMTP_PASSWORD_ENV="EPH_TEST_SMTP_PW", NOTIFY_EMAIL_FROM="eph@example.test")
        with _Patch(NOTIFY_SMTP_STARTTLS=True, **base):
            assert N.dm("ops@example.test", "x") is True                # _FakeSMTP.starttls asserts the context
        assert [c[0] for c in _FakeSMTP.sent[0]] == ["connect", "starttls", "login", "send"], _FakeSMTP.sent
        _FakeSMTP.sent = []
        with _Patch(NOTIFY_SMTP_STARTTLS=False, **base):
            assert N.dm("ops@example.test", "x") is False               # user + password with TLS off: refused
        assert _FakeSMTP.sent == [], _FakeSMTP.sent                     # never even connected
    finally:
        N.smtplib.SMTP = real
        os.environ.pop("EPH_TEST_SMTP_PW", None)
    ok("email: STARTTLS gets a verifying context (CERT_REQUIRED + host name); a login with STARTTLS off is refused, "
       "nothing sent")


def test_flush_with_empty_ops_handle_sends_nothing():
    sent, calls = [], []
    real_other, real_run = N._send_other, N.subprocess.run
    N._send_other = lambda *a: sent.append(a)
    N.subprocess.run = lambda *a, **k: calls.append(a)
    try:
        for backend in ("zbus", "webhook"):
            with _Patch(NOTIFY_BACKEND=backend, OPS_HANDLE=""):
                N._subject_suppressed["t9"] = 4
                assert N.flush_subject() == 0
                assert N._subject_suppressed == {}                      # the count is still cleared
    finally:
        N._send_other, N.subprocess.run = real_other, real_run
    assert sent == [] and calls == [], (sent, calls)
    ok("flush_subject with an empty OPS_HANDLE logs, returns 0 and sends nothing on zbus or webhook")


def test_bad_backend_refuses_import():
    env = dict(os.environ, NOTIFY_BACKEND="slack")
    p = subprocess.run([sys.executable, "-c", "import notify"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert p.returncode != 0 and "NOTIFY_BACKEND='slack'" in p.stderr, p.stderr[-500:]
    env["NOTIFY_BACKEND"] = " Webhook "
    p = subprocess.run([sys.executable, "-c", "import notify; print(notify.NOTIFY_BACKEND)"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == "webhook", p.stderr[-500:]
    ok("an unknown NOTIFY_BACKEND stops the import; the value is trimmed and case-folded")


def test_zbus_default_is_unchanged():
    log = os.path.join(_TMP.name, "zbus.log")
    fake = os.path.join(_TMP.name, "zbus")
    with open(fake, "w") as f:
        f.write(f"#!/bin/sh\necho \"$*\" >> {log}\ncat >> {log}\necho >> {log}\n")
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
    assert N.NOTIFY_BACKEND == "zbus"                                   # the default with nothing set
    sent = []
    with _Patch(OPS_HANDLE="ops@chat.example.test"):
        real = N._send_other
        N._send_other = lambda *a: sent.append(a)
        try:
            assert N.wake_ops("hello", subject="t3", zbus=fake) is True
            assert N.dm("deploy@chat.example.test", "line", zbus=fake) is True
        finally:
            N._send_other = real
    assert sent == []
    assert open(log).read() == (f"dm ops@chat.example.test --stdin\n{N.MSG_PREFIX}hello\n"
                                f"dm deploy@chat.example.test --stdin\nline\n"), open(log).read()
    ok("NOTIFY_BACKEND unset = zbus, the same argv and stdin as before; the other channels are never called")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} notify self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    _TMP.cleanup()
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
