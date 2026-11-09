"""Module host lifecycle and proxy contracts, plus a real stdlib Unix-socket child."""
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import actions
import dash
import modhost
import stream


AUTH = {"BROKER_MODULES": "agents", "BROKER_MODULE_AGENTS_UPSTREAM": "/unused.sock",
        "BROKER_MODULE_AGENTS_CMD": "", "BROKER_MODULE_AGENTS_GROUP": "viewers",
        "BROKER_STREAM_USER_HEADER": "X-Auth-User", "BROKER_STREAM_GROUPS_HEADER": "X-Auth-Groups",
        "BROKER_ACTIONS_ENABLED": "1", "BROKER_ACTIONS_GROUP": "operators",
        "BROKER_ACTIONS_PROXY_SECRET": "synthetic-module-proxy-secret"}
HEADERS = {"X-Auth-User": "alice", "X-Auth-Groups": "viewers|operators",
           "X-Broker-Proxy-Secret": AUTH["BROKER_ACTIONS_PROXY_SECRET"],
           "X-Broker-Action": "1", "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def tmp_path():
    # Other suites exercise global TMPDIR/tempfile/umask settings. Keep module
    # state private, worktree-local and socket paths below the OS length limit.
    old_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix=".modtest-", dir=Path(__file__).resolve().parents[1]) as path:
            yield Path(path)
    finally:
        os.umask(old_umask)


@pytest.fixture
def configured(monkeypatch):
    for key, val in AUTH.items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("BROKER_ACTIONS_AUDIT_HANDLE", "")
    return modhost.Host()


class Handler:
    def __init__(self, path="/viewer/agents/events?q=1", method="GET", headers=None):
        self.path, self.command = path, method
        self.headers = Message()
        for k, v in (HEADERS if headers is None else headers).items():
            self.headers[k] = v
        self.wfile = io.BytesIO()
        self.connection = Mock()
        self.response_headers = {}
        self.flushes = 0
        self.wfile.flush = self.flush
        self.body_reads = 0

    def flush(self):
        self.flushes += 1

    def _body(self):
        self.body_reads += 1
        return b'{"hello": "world"}'

    def _reply(self, code, obj):
        self.code = code
        self._no_frame()
        return code, obj

    def send_response(self, code):
        self.code = code

    def send_header(self, k, v):
        self.response_headers[k] = v

    def _no_frame(self):
        self.response_headers["X-Frame-Options"] = "DENY"

    def end_headers(self):
        pass


def transport(host, monkeypatch, chunks=(b"data: one\n\n", b"data: two\n\n", b"")):
    module = host.modules["agents"]
    module._state("up")
    response = Mock(status=200)
    response.read1.side_effect = chunks
    response.getheader.side_effect = lambda k: {"Content-Type": "text/event-stream"}.get(k)
    conn = Mock()
    conn.getresponse.return_value = response
    monkeypatch.setattr(module, "connection", Mock(return_value=conn))
    return module, conn


def test_unset_is_inert(monkeypatch):
    monkeypatch.delenv("BROKER_MODULES", raising=False)
    host = modhost.Host()
    monkeypatch.setattr(modhost.threading, "Thread", Mock(side_effect=AssertionError("thread while off")))
    host.start()
    assert host.modules == {}
    assert dash.module_page([]) is dash.PAGE
    assert modhost.serve(Handler(), host)[0] == 404


@pytest.mark.parametrize("settings", [
    {"BROKER_MODULES": "../bad"}, {"BROKER_MODULES": "agents,agents"},
    {"BROKER_MODULE_AGENTS_CMD": "python viewer.py"},
    {"BROKER_MODULE_AGENTS_UPSTREAM": "https://127.0.0.1/"},
    {"BROKER_MODULE_AGENTS_UPSTREAM": "http://192.0.2.1/"},
    {"BROKER_MODULE_AGENTS_UPSTREAM": "http://user@127.0.0.1/"},
    {"BROKER_MODULE_AGENTS_UPSTREAM": "relative.sock"},
    {"BROKER_MODULE_AGENTS_MAX_STREAMS": "0"},
])
def test_config_refuses_unsafe_or_ambiguous(configured, monkeypatch, settings):
    for k, v in settings.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ValueError):
        modhost.configuration()


def test_config_defaults_and_loopback(configured, monkeypatch, tmp_path):
    monkeypatch.setenv("BROKER_MODULE_AGENTS_UPSTREAM", "")
    monkeypatch.setenv("BROKER_MODULE_AGENTS_CMD", 'python "my viewer.py"')
    monkeypatch.setenv("BROKER_STATE_PATH", str(tmp_path / "state.db"))
    c = modhost.configuration()["agents"]
    assert c["endpoint"] == str(tmp_path / "modules" / "agents.sock")
    assert c["cap"] == 8 and c["cmd"] == ["python", "my viewer.py"]
    conn = modhost.UnixConnection(c["endpoint"])
    assert conn.host == "localhost" and conn.path == c["endpoint"]
    monkeypatch.setenv("BROKER_MODULE_AGENTS_CMD", "")
    monkeypatch.setenv("BROKER_MODULE_AGENTS_UPSTREAM", "http://[::1]:9000/base")
    c = modhost.configuration()["agents"]
    assert (c["host"], c["port"], c["base"]) == ("::1", 9000, "/base")


@pytest.mark.parametrize("method,headers", [
    ("GET", {k: v for k, v in HEADERS.items() if k != "X-Auth-User"}),
    ("GET", {**HEADERS, "X-Auth-Groups": "viewers-other|operators"}),
    ("POST", {k: v for k, v in HEADERS.items() if k != "X-Broker-Proxy-Secret"}),
    ("POST", {**HEADERS, "X-Auth-Groups": "viewers"}),
    ("POST", {**HEADERS, "X-Broker-Action": "0"}),
    ("POST", {**HEADERS, "Content-Type": "text/plain"}),
    ("POST", {**HEADERS, "Sec-Fetch-Site": "cross-site"}),
])
def test_proxy_gates_before_connection_or_body(configured, method, headers):
    h = Handler(method=method, headers=headers)
    assert modhost.serve(h, configured)[0] == 403
    assert h.body_reads == 0


def test_fail_closed_and_duplicate_identity(configured, monkeypatch):
    h = Handler()
    h.headers["X-Auth-User"] = "forged"
    assert modhost.serve(h, configured)[0] == 403
    for key in ("BROKER_STREAM_USER_HEADER", "BROKER_MODULE_AGENTS_GROUP"):
        with monkeypatch.context() as m:
            m.setenv(key, "")
            assert modhost.serve(Handler(), configured)[0] == 403
    monkeypatch.setenv("BROKER_STREAM_USER_HEADER", "X-Forwarded-User")
    assert modhost.serve(Handler(headers={**HEADERS, "X-Forwarded-User": "alice"}), configured)[0] == 403


def test_proxy_sse_header_stripping_and_slot_isolation(configured, monkeypatch):
    monkeypatch.setattr(stream, "_active", stream.max_streams())
    before = stream.active()
    module, conn = transport(configured, monkeypatch)
    h = Handler(headers={**HEADERS, "X-Viewer-User": "mallory", "X-Viewer-Can-Act": "forged",
                         "Authorization": "do-not-forward", "Cookie": "do-not-forward"})
    modhost.serve(h, configured)
    assert h.code == 200 and h.wfile.getvalue() == b"data: one\n\ndata: two\n\n"
    assert h.flushes == 2 and h.response_headers["X-Frame-Options"] == "DENY"
    args, kwargs = conn.request.call_args
    assert args == ("GET", "/events?q=1")
    assert kwargs["headers"]["X-Viewer-User"] == "alice"
    assert kwargs["headers"]["X-Viewer-Can-Act"] == "1"
    assert set(kwargs["headers"]) == {"Content-Type", "X-Viewer-User", "X-Viewer-Can-Act"}
    assert stream.active() == before
    assert module.slots.acquire(blocking=False)
    module.slots.release()


def test_module_caps_are_independent(configured, monkeypatch):
    first = configured.modules["agents"]
    configured.modules["other"] = modhost.Module(dict(first.config, name="other"))
    for _ in range(first.config["cap"]):
        assert first.slots.acquire(blocking=False)
    _, conn = transport(configured, monkeypatch, (b"",))
    configured.modules["other"].connection = configured.modules["agents"].connection
    configured.modules["other"]._state("up")
    h = Handler(path="/viewer/other/ui.js")
    modhost.serve(h, configured)
    assert h.code == 200
    assert modhost.serve(Handler(), configured)[0] == 503


def test_can_act_requires_proxy_and_actions_group(configured, monkeypatch):
    module, conn = transport(configured, monkeypatch)
    h = Handler(headers={"X-Auth-User": "alice", "X-Auth-Groups": "viewers"})
    modhost.serve(h, configured)
    assert conn.request.call_args.kwargs["headers"]["X-Viewer-Can-Act"] == "0"
    monkeypatch.setenv("BROKER_ACTIONS_ENABLED", "")
    assert modhost.serve(Handler(method="POST"), configured)[0] == 403


def test_proxy_post_audit_and_body(configured, monkeypatch):
    module, conn = transport(configured, monkeypatch, (b"{}", b""))
    audit = Mock()
    monkeypatch.setattr(actions, "audit", audit)
    h = Handler(method="POST")
    modhost.serve(h, configured)
    assert h.code == 200 and h.body_reads == 1
    assert conn.request.call_args.kwargs["body"] == b'{"hello": "world"}'
    audit.assert_called_once_with("alice", "module_post", "agents", "200")


def test_proxy_cap_and_failure_release(configured, monkeypatch):
    module, conn = transport(configured, monkeypatch)
    for _ in range(module.config["cap"]):
        assert module.slots.acquire(blocking=False)
    assert modhost.serve(Handler(), configured)[0] == 503
    post = Handler(method="POST")
    assert modhost.serve(post, configured)[0] == 503
    assert post.body_reads == 0
    conn.connect.assert_not_called()
    for _ in range(module.config["cap"]):
        module.slots.release()
    conn.connect.side_effect = socket.timeout()
    assert modhost.serve(Handler(), configured)[0] == 502
    assert module.slots.acquire(blocking=False)
    module.slots.release()
    module._state("down")
    assert modhost.serve(Handler(), configured)[0] == 503


def test_deadline_closes_both_sockets_even_after_http_connection_detaches(configured, monkeypatch):
    module, conn = transport(configured, monkeypatch, (b"",))
    sock = conn.sock
    timers = []
    class Timer:
        def __init__(self, interval, callback):
            self.interval, self.callback = interval, callback
            timers.append(self)
        def start(self):
            pass
        def cancel(self):
            pass
    monkeypatch.setattr(modhost.threading, "Timer", Timer)
    conn.getresponse.side_effect = lambda: (setattr(conn, "sock", None) or
                                           timers[0].callback() or Mock(status=200, read1=Mock(return_value=b""),
                                                                       getheader=Mock(return_value=None)))
    h = Handler()
    modhost.serve(h, configured)
    sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    h.connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)


def test_upload_deadline_interrupts_body_and_releases_slot(configured, monkeypatch):
    module, conn = transport(configured, monkeypatch, (b"",))
    module.slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(stream, "max_s", lambda: 0.1)
    reading, expired = threading.Event(), threading.Event()
    h = Handler(method="POST")
    h.connection.shutdown.side_effect = lambda how: expired.set()
    def slow_body():
        reading.set()
        assert expired.wait(2), "body read was not interrupted by the absolute deadline"
        return b"partial"
    h._body = slow_body
    errors = []
    def request():
        try:
            modhost.serve(h, configured)
        except Exception as exc:
            errors.append(exc)
    worker = threading.Thread(target=request)
    started = time.monotonic()
    worker.start()
    try:
        assert reading.wait(1)
        assert modhost.serve(Handler(), configured)[0] == 503
        worker.join(timeout=1)
        assert not worker.is_alive() and not errors
        assert time.monotonic() - started < 1
        assert expired.is_set() and h.code == 502
        module.connection.assert_not_called()  # Never forward a partial upload.
        assert module.slots.acquire(blocking=False)
        module.slots.release()
    finally:
        expired.set()
        worker.join(timeout=2)


def test_real_slow_upload_is_bounded_and_releases_slot(configured, monkeypatch):
    import app
    try:
        # Some sandboxes allow socketpair() but deny send/recv or shutdown.
        # Verify those operations before relying on them to interrupt a read.
        probe_client, probe_server = socket.socketpair()
        try:
            probe_client.settimeout(1)
            probe_server.settimeout(1)
            probe_client.sendall(b"x")
            assert probe_server.recv(1) == b"x"
            probe_server.shutdown(socket.SHUT_RDWR)
        finally:
            probe_client.close()
            probe_server.close()
    except PermissionError as exc:
        pytest.skip("sandbox blocks socket operations: " + str(exc))
    client, server = socket.socketpair()
    module, conn = transport(configured, monkeypatch, (b"",))
    module.slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(stream, "max_s", lambda: 0.1)
    h = Handler(method="POST")
    h.connection = server
    server.settimeout(1)  # Idle timeout is longer than the absolute deadline.
    h.rfile = server.makefile("rb")
    h.headers["Content-Length"] = "100"
    h._body = lambda: app.Handler._body(h)
    done = threading.Event()
    def trickle():
        while not done.is_set():
            try:
                client.sendall(b"x")
            except OSError:
                break
            done.wait(0.02)
    writer = threading.Thread(target=trickle)
    writer.start()
    try:
        started = time.monotonic()
        modhost.serve(h, configured)
        assert time.monotonic() - started < 0.5
        assert h.code == 502
        module.connection.assert_not_called()
        assert module.slots.acquire(blocking=False)
        module.slots.release()
    finally:
        done.set()
        writer.join(timeout=2)
        h.rfile.close()
        client.close()
        server.close()


@pytest.fixture
def child(configured, monkeypatch, tmp_path):
    c = dict(configured.modules["agents"].config, cmd=["python", "viewer.py"], endpoint=str(tmp_path / "viewer.sock"),
             version_file=str(tmp_path / "version"), disabled_file=str(tmp_path / "disabled"))
    module = modhost.Module(c)
    processes = []
    def launch(*args, **kwargs):
        p = Mock(pid=100 + len(processes))
        p.poll.return_value = None
        processes.append(p)
        assert kwargs["umask"] == 0o177 and kwargs["start_new_session"]
        assert kwargs["env"]["BROKER_MODULE_SOCKET"] == c["endpoint"]
        return p
    monkeypatch.setattr(modhost.subprocess, "Popen", launch)
    monkeypatch.setattr(modhost.os, "killpg", Mock())
    monkeypatch.setattr(modhost.os, "chmod", Mock())
    monkeypatch.setattr(modhost, "STOP_GRACE_S", 0)
    monkeypatch.setattr(module, "_health", Mock(return_value=True))
    return module, processes


def test_supervisor_backoff_and_healthy_reset(child):
    m, processes = child
    now = 0
    for delay in (1, 2, 4, 8, 16, 32, 60, 60):
        m.step(now)
        assert m.state == "up"
        processes[-1].poll.return_value = -9
        now += 0.1
        m.step(now)
        assert m.state == "down" and m.next_start == now + delay
        count = len(processes)
        m.step(now + delay - 0.01)
        assert len(processes) == count
        now += delay
    m.step(now)
    m.step(now + 61)
    assert m.backoff == 1 and m.restarts == len(processes) - 1
    m.stop()


def test_health_three_failures_and_restart(child):
    m, processes = child
    m.step(0)
    m._health.return_value = False
    m.step(10)
    m.step(20)
    assert processes[0].wait.call_count == 0
    m.step(30)
    assert m.child is None and m.state == "down"
    assert "three consecutive" in m.last_error
    m._health.return_value = True
    m.step(31)
    assert m.state == "up" and m.restarts == 1


def test_version_content_mtime_reload_and_disabled(child):
    m, processes = child
    m.step(0)
    path = Path(m.config["version_file"])
    path.write_text("v1")
    m.step(1)
    old_mtime = path.stat().st_mtime_ns
    path.write_text("v2")
    os.utime(path, ns=(old_mtime, old_mtime))
    m.step(2)
    assert len(processes) == 3 and m.version
    os.utime(path, ns=(old_mtime + 1000000000, old_mtime + 1000000000))
    m.step(3)
    assert len(processes) == 4
    disabled = Path(m.config["disabled_file"])
    disabled.touch()
    m.step(4)
    m.reload_event.set()
    m.step(5)
    assert m.state == "disabled" and m.child is None and len(processes) == 4
    disabled.unlink()
    m.step(6)
    assert len(processes) == 5 and m.state == "up"
    m.reload_event.set()
    m.step(7)
    assert len(processes) == 6
    m.stop()
    assert m.child is None


def test_launch_failure_is_backed_off(child, monkeypatch):
    m, _ = child
    launch = Mock(side_effect=OSError("synthetic launch failure"))
    monkeypatch.setattr(modhost.subprocess, "Popen", launch)
    m.step(0)
    m.step(0.5)
    assert launch.call_count == 1 and m.next_start == 1 and m.state == "down"
    m.step(1)
    assert launch.call_count == 2 and m.next_start == 3


def test_stop_escalates_process_group_after_grace(child):
    m, processes = child
    m.step(0)
    processes[0].wait.side_effect = [modhost.subprocess.TimeoutExpired("viewer", 2), 0]
    m.stop()
    assert modhost.os.killpg.call_args_list[-1].args == (processes[0].pid, signal.SIGKILL)
    assert m.child is None


@pytest.mark.parametrize("leader_crashed", [False, True])
def test_real_stubborn_descendant_dies_even_when_leader_exits(configured, monkeypatch, leader_crashed):
    monkeypatch.setattr(modhost, "STOP_GRACE_S", 0.1)
    # The descendant keeps stdout open after the leader exits. EOF proves it
    # stopped, without confusing an unreaped orphan zombie with a live process.
    descendant = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    leader_code = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', sys.argv[1]]); time.sleep(60)"
    module = configured.modules["agents"]
    leader = modhost.subprocess.Popen([sys.executable, "-c", leader_code, descendant],
                                     start_new_session=True, stdout=modhost.subprocess.PIPE,
                                     stderr=modhost.subprocess.DEVNULL)
    module.child = leader
    try:
        assert select.select([leader.stdout], [], [], 3)[0], "descendant did not start"
        assert os.read(leader.stdout.fileno(), 1024) == b"ready\n"
        if leader_crashed:
            leader.kill()
            leader.wait(timeout=2)
            module._failed(time.monotonic(), "child exited")
        else:
            module.stop()
        assert module.child is None and leader.poll() is not None
        assert select.select([leader.stdout], [], [], 1)[0], "descendant survived cleanup"
        assert os.read(leader.stdout.fileno(), 1024) == b""
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait(timeout=2)
        leader.stdout.close()
        module.child = None


def test_external_health_status_and_reload(configured, monkeypatch):
    m = configured.modules["agents"]
    monkeypatch.setattr(m, "_health", Mock(return_value=True))
    monkeypatch.setattr(modhost.subprocess, "Popen", Mock(side_effect=AssertionError("external launch")))
    m.step(0)
    assert configured.status(Handler().headers)["agents"]["state"] == "up"
    assert configured.status(Handler(headers={}).headers) == {}
    assert configured.reload("agents")[0] == 409
    assert configured.reload("missing")[0] == 404
    m._health.return_value = False
    for now in (10, 20, 30):
        m.step(now)
    assert m.state == "down" and m.child is None


def test_module_reload_action_uses_existing_audit_path(configured, monkeypatch, child):
    m, _ = child
    configured.modules["agents"] = m
    broker = SimpleNamespace(module_host=configured)
    h = Handler(method="POST")
    h._body = lambda: b'{"name":"agents"}'
    audit = Mock()
    monkeypatch.setattr(actions, "audit", audit)
    assert actions.serve(h, broker, "module_reload")[0] == 200
    assert m.reload_event.is_set()
    audit.assert_called_once_with("alice", "module_reload", "agents", "ok")
    h = Handler(method="POST", headers={})
    assert actions.serve(h, broker, "module_reload")[0] == 403


def test_dashboard_module_shell_and_fleet_contract():
    page = dash.module_page([{"name": "agents", "label": "Agents </script>", "child": True}])
    assert "Agents </script>" not in page
    assert "if(moduleRoute()) return" in page
    assert "moduleAffordance(name) + '<span" in page
    assert "header nav" in page and "document.querySelector('.tabbar')" in page
    assert "js.mount(content, viewerContext" in page and "js.quickView(content, name" in page
    assert "Agent viewer is down" in page and "Open full view" in page
    assert "if(can && m.child)" in page and "module_reload" in page
    assert "new Date(s.since * 1000).toLocaleString()" in page
    assert "repeat(5,minmax(64px,1fr))" in page
    assert ".tbl td [data-quick-module]{display:inline-flex}" in page


def test_viewer_import_failure_retries_with_backoff_and_fresh_url():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to execute the browser import retry regression")
    # Exercise the actual dashboard loader with only its import transport and
    # clock replaced. No DOM, browser, network or third-party packages required.
    loader = "async function viewerCode(m, s){" + dash.MODULE_JS.split(
        "async function viewerCode(m, s){", 1)[1].split("async function renderViewer", 1)[0]
    loader = loader.replace("import(", "loadModule(")
    script = r"""
import assert from 'node:assert/strict';
let now = 0, failures = 2;
Date.now = () => now;
const calls = [], runtime = {}, viewerRuntime = new Map([['agents', runtime]]);
const m = {name: 'agents'}, s = {version: 'v1', restarts: 0, since: 1};
let loadModule = async url => {
  calls.push(url);
  if(failures-- > 0) throw new Error('temporary 503');
  return {mount(){}};
};
""" + loader + r"""
await assert.rejects(viewerCode(m, s), /temporary 503/);
assert.equal(runtime.js, null);
assert.equal(runtime.codeVersion, null);
assert.equal(runtime.codeRetryAt, 5000);
await assert.rejects(viewerCode(m, s), /retry pending/);
assert.equal(calls.length, 1);
now = 5000;
await assert.rejects(viewerCode(m, s), /temporary 503/);
assert.equal(runtime.codeRetryAt, 15000);
now = 15000;
const [a, b] = await Promise.all([viewerCode(m, s), viewerCode(m, s)]);
assert.equal(a, b);
assert.equal(calls.length, 3); // Concurrent views share one load.
assert.equal(new Set(calls).size, 3); // Never reuse a browser-cached failed URL.
assert.equal(runtime.codeVersion, 'v1:0:1');
assert.equal(runtime.codeFailures, 0);
await viewerCode(m, s);
assert.equal(calls.length, 3); // Successful imports remain cached.
s.version = 'v2'; failures = 20;
for(let i = 0; i < 8; i++){
  now = runtime.codeRetryAt;
  await assert.rejects(viewerCode(m, s), /temporary 503/);
  assert.equal(runtime.codeRetryAt - now, Math.min(60000, 5000 * 2 ** i));
}
// A newer module bypasses the old backoff; late failures cannot evict it.
let rejectOld;
now = runtime.codeRetryAt;
loadModule = () => new Promise((resolve, reject) => { rejectOld = reject; });
const old = viewerCode(m, s);
s.version = 'v3';
loadModule = async () => a;
assert.equal(await viewerCode(m, s), a);
rejectOld(new Error('old failure'));
await assert.rejects(old, /old failure/);
assert.equal(runtime.codeVersion, 'v3:0:1');
assert.equal(await viewerCode(m, s), a);
"""
    result = modhost.subprocess.run([node, "--input-type=module"], input=script,
                                    text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


def test_app_routes_and_health_do_not_depend_on_module_health(configured, monkeypatch):
    import app
    broker = SimpleNamespace(module_host=configured, drain_status=lambda: {
        "error": None, "recovery_required": False, "dispatching": False, "live_workers": 0},
        coalescer=SimpleNamespace(pending={}, deduped=0), echo_guard=SimpleNamespace(dropped=0), rejected=0)
    monkeypatch.setattr(app, "BROKER", broker)
    def request(path):
        h = app.Handler.__new__(app.Handler)
        h.path, h.command, h.headers = path, "GET", Handler().headers
        replies = []
        def reply(code, body):
            replies.append((code, body))
            return code, body
        h._reply = reply
        result = h.do_GET()
        return result if result is not None else replies[-1]
    assert request("/viewer-status")[1]["agents"]["state"] == "starting"
    assert request("/health")[0] == 200
    proxy = Mock(return_value="proxied")
    monkeypatch.setattr(modhost, "serve", proxy)
    assert request("/viewer/agents/ui.js") == "proxied"
    broker.module_host = None
    assert request("/viewer-status")[0] == 404
    assert request("/viewer/agents/ui.js")[0] == 404
    assert actions.run(broker, "module_reload", {"name": "agents"}, "alice")[1]["error"] == \
        "unknown action; one of " + ", ".join(actions.VERBS)


def test_real_unix_child_crash_reload_disable_and_proxy(configured, monkeypatch, tmp_path):
    # Some worker sandboxes disallow all sockets. The deterministic tests above
    # still verify the lifecycle, auth, framing, deadline and slot contracts there.
    c = dict(configured.modules["agents"].config,
             cmd=[sys.executable, str(Path(__file__).resolve().parents[1] / "examples/viewer.py")],
             endpoint=str(tmp_path / "v.sock"), disabled_file=str(tmp_path / "disabled"))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.bind(c["endpoint"])
        os.unlink(c["endpoint"])
    except PermissionError as exc:
        pytest.skip("sandbox blocks Unix socket creation/bind: " + str(exc))
    m = modhost.Module(c)
    configured.modules["agents"] = m
    launch = modhost.subprocess.Popen
    log_path = tmp_path / "viewer.log"
    def launch_logged(*args, **kwargs):
        with log_path.open("ab") as log:
            kwargs["stderr"] = log
            return launch(*args, **kwargs)
    monkeypatch.setattr(modhost.subprocess, "Popen", launch_logged)
    monkeypatch.setattr(modhost, "HEALTH_S", 0.05)
    monkeypatch.setattr(modhost, "POLL_S", 0.02)
    def wait_for(predicate):
        deadline = time.monotonic() + 8
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert predicate(), (m.status(), log_path.read_text() if log_path.exists() else "no child log")
    m.start()
    try:
        wait_for(lambda: m.state == "up")
        assert os.stat(c["endpoint"]).st_mode & 0o777 == 0o600
        h = Handler(path="/viewer/agents/identity")
        modhost.serve(h, configured)
        assert json.loads(h.wfile.getvalue()) == {"user": "alice", "can_act": "1"}
        h = Handler()
        modhost.serve(h, configured)
        assert h.wfile.getvalue() == b"data: first\n\ndata: second\n\n"
        old = m.child.pid
        os.kill(old, signal.SIGKILL)
        wait_for(lambda: m.state == "down")
        wait_for(lambda: m.state == "up" and m.child.pid != old)
        old = m.child.pid
        assert configured.reload("agents")[0] == 200
        wait_for(lambda: m.state == "up" and m.child.pid != old)
        Path(c["disabled_file"]).touch()
        wait_for(lambda: m.state == "disabled")
        time.sleep(0.15)
        assert m.child is None
    finally:
        m.stop()
    assert m.child is None and not m.thread.is_alive()
