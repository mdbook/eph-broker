"""The source and Docker previews use neutral config without account or socket access."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextmanager
def _preview_directory():
    # Other tests start a socket daemon that changes the process-wide umask. Keep our private
    # directories searchable and restore the caller's mask rather than relying on test order.
    mask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix="quickstart-", dir=ROOT) as state:
            yield Path(state)
    finally:
        os.umask(mask)


def _check_preview(root, state):
    # Match the README's explicit paths, with no inherited deployment settings or tokens.
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(state),
        "TMPDIR": str(state),
        "BROKER_SITE_ENV": os.devnull,
        "BROKER_CONFIG": "config/demo/broker.example.json",
        "EVENTS_LOG": str(state / "events.jsonl"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    probe = r'''
import io, os, sys
from unittest.mock import patch

# Observe real startup writes, including imports and the cleanup sweeps. The
# relocated state directory represents the volume; TMPDIR represents /tmp.
writable = os.path.realpath(os.environ["HOME"])
def check_write(path):
    if isinstance(path, int):
        return
    path = os.path.realpath(os.fsdecode(path))
    assert os.path.commonpath([writable, path]) == writable, ("startup write outside state/tmp", path)

def audit(event, args):
    if event == "open" and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        check_write(args[0])
    elif event == "sqlite3.connect" and args[0] != ":memory:":
        check_write(args[0])
    elif event in ("os.mkdir", "os.chmod", "os.remove", "os.rmdir"):
        check_write(args[0])
    elif event == "os.rename":
        check_write(args[0])
        check_write(args[1])

sys.addaudithook(audit)
import app, forge, routing, site_env
from adapters import cc

assert app.CONFIG_DIR == "config/demo"
assert not site_env.push_hosts()
assert not routing.ISSUE_AUTHOR_ALLOWLIST
assert not forge.verify_gitlab_token(None)
assert not app.verify_hmac(b"{}", None)
assert not app.verify_rerun_token(None)
assert app.HMAC_SECRET_ENV == "BROKER_HMAC_SECRET"

class Server:
    def __init__(self, address, handler):
        assert address == ("127.0.0.1", 8787)
        assert handler is app.Handler

    def serve_forever(self):
        # Exercise the real startup and handlers without opening a network socket.
        h = object.__new__(app.Handler)
        h.path = "/health"
        replies = []
        h._reply = lambda code, body: replies.append((code, body))
        h.do_GET()
        code, body = replies.pop()
        assert code == 200 and body["ok"] is True, body
        assert body["live_workers"] == 0
        assert body["issue_author_allowlist"] == []
        assert body["hmac"]["mode"] == "enforce"
        assert body["drain"]["durable"] is True
        h.path = "/dash.html"
        h.send_response = lambda code: replies.append(code)
        h.send_header = lambda *args: None
        h.end_headers = lambda: None
        h.wfile = io.BytesIO()
        h.do_GET()
        assert replies == [200]
        assert b"<html" in h.wfile.getvalue().lower()
        assert os.path.isdir(cc._ZULIP_THREAD_ROOT)
        app.BROKER.state.close()

with patch.object(app, "ThreadingHTTPServer", Server), \
     patch("threading.Thread.start"), \
     patch.object(app.LivenessDriver, "start"), \
     patch("zulip_wake.ZulipWake.start"):
    app.main()
'''
    result = subprocess.run([sys.executable, "-c", probe], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (state / "broker-state.sqlite3").is_file()


def test_source_preview_starts_without_site_overlay():
    with _preview_directory() as state:
        _check_preview(ROOT, state)


def test_image_runtime_files_are_sufficient():
    with _preview_directory() as state:
        _check_image(state)


def _check_image(tmp_path):
    # Reproduce the Dockerfile's COPY inputs, not the full deployment checkout.
    dockerfile = (ROOT / "Dockerfile").read_text()
    defaults = dict(re.findall(r"\b([A-Z_]+)=([^\s\\]+)", dockerfile))
    assert defaults["HOME"] == "/var/lib/eph-broker"
    assert defaults["EVENTS_LOG"].startswith(defaults["HOME"] + "/")
    assert defaults["WORKTREES_ROOT"].startswith("/tmp/")
    assert "USER 10001:10001" in dockerfile
    assert (dockerfile.rindex("COPY ") < dockerfile.index("chmod -R a+rX /opt/eph-broker")
            < dockerfile.index("USER "))
    image = tmp_path / "image"
    image.mkdir()
    copies = [line.split()[1:] for line in (ROOT / "Dockerfile").read_text().splitlines()
              if line.startswith("COPY ")]
    assert copies == [["*.py", "./"], ["adapters/", "./adapters/"],
                      ["trackers/", "./trackers/"],
                      ["config/demo/broker.example.json", "./config/demo/broker.example.json"]]
    for source, dest in copies:
        target = image / dest
        if source == "*.py":
            for file in ROOT.glob(source):
                shutil.copyfile(file, image / file.name)
        elif source.endswith("/"):
            shutil.copytree(ROOT / source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, target)
    state = tmp_path / "runtime"
    state.mkdir()
    # Map the Docker HOME/state volume to a writable directory outside the code.
    _check_preview(image, state)


def _smoke_commands(case):
    # Check script lifecycle without claiming a real container ran. The separate
    # documented smoke command is the Docker runtime gate for reviewers.
    with _preview_directory() as state:
        docker = state / "docker"
        docker.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys
args = sys.argv[1:]
with open(os.environ["SMOKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
case = os.environ["SMOKE_CASE"]
if args == ["volume", "create"]:
    print("smoke-volume")
elif args[0] == "create":
    print("smoke-container")
elif args[0] == "inspect":
    print("exited unhealthy" if case == "startup-failed" else "running healthy")
elif args[0] == "exec" and case == "http-failed":
    sys.exit(1)
''')
        docker.chmod(0o700)
        log = state / "commands.jsonl"
        env = {"PATH": str(state) + os.pathsep + os.environ.get("PATH", ""),
               "SMOKE_LOG": str(log), "SMOKE_CASE": case}
        result = subprocess.run(["sh", str(ROOT / "tools/docker-smoke.sh")], env=env,
                                capture_output=True, text=True, timeout=15)
        commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert (result.returncode == 0) == (case == "healthy"), (result.stdout, result.stderr)
    assert ["rm", "-f", "smoke-container"] in commands
    assert ["volume", "rm", "smoke-volume"] in commands
    assert commands[-1][:2] == ["image", "rm"]
    return commands


def test_docker_smoke_isolated_and_cleans_up():
    commands = _smoke_commands("healthy")
    assert commands[0][0] == "build" and commands[0][-1] == str(ROOT)
    create = next(args for args in commands if args[0] == "create")
    for flag, value in (("--network", "none"), ("--cap-drop", "ALL"),
                        ("--security-opt", "no-new-privileges:true"),
                        ("--tmpfs", "/tmp:mode=1777"),
                        ("--mount", "type=volume,source=smoke-volume,target=/var/lib/eph-broker")):
        assert create[create.index(flag) + 1] == value
    assert "--read-only" in create
    assert not any(arg.startswith(("--publish", "--privileged", "--user")) for arg in create)
    probe = next(args[-1] for args in commands if args[0] == "exec")
    assert '("/health", "/")' in probe and "response.status == 200" in probe


def test_docker_smoke_failures_report_logs_and_clean_up():
    for case in ("startup-failed", "http-failed"):
        commands = _smoke_commands(case)
        assert ["logs", "smoke-container"] in commands
        if case == "startup-failed":
            assert not any(args[0] == "exec" for args in commands)


def test_quickstart_document_links_exist():
    pages = [ROOT / "README.md", *(ROOT / "docs" / name for name in
             ("docker.md", "first-merge-request.md", "routing-overrides.md", "worker-runtime.md"))]
    for page in pages:
        text = page.read_text()
        assert "\u2014" not in text
        for link in re.findall(r"\]\(([^)]+)\)", text):
            if "://" not in link and not link.startswith("#"):
                assert (page.parent / link.split("#", 1)[0]).exists(), (page, link)


def test_screenshot_demo_is_synthetic_and_read_only():
    with _preview_directory() as state:
        probe = r'''
import json, os, sys
from pathlib import Path
from unittest.mock import patch
from scripts import screenshots

state = Path(sys.argv[1])
os.environ.update({"BROKER_CONFIG": "/nonexistent-live-config",
                   "DASH_INSTANCE_NAME": "must not appear",
                   "BROKER_ACTIONS_ENABLED": "1", "FLEET_STATUS_GEN": "/must-not-run"})
real_import = __import__
def isolated_import(name, *args, **kwargs):
    if name == "app":
        assert os.environ == screenshots.demo_environment(state)
        app = real_import(name, *args, **kwargs)
        class Server:
            server_address = ("127.0.0.1", 12345)
            def __init__(self, address, handler):
                assert address == ("127.0.0.1", 0)
                self.handler = handler
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def serve_forever(self):
                first = app.dash.snapshot(app.BROKER, cap=5)
                assert first == app.dash.snapshot(app.BROKER, cap=5)
                assert first["now"] == screenshots.NOW
                assert first["instance"] == "Synthetic demo"
                assert len(first["workers"]) == 3 and len(first["live"]) == 4
                assert len(first["pending"]) == 1 and len(first["queue"]) == 2
                assert first["stream_enabled"] is False
                assert len(first["fleet"]["agents"]) == 5
                assert not first["fleet"]["stale"] and not first["fleet"].get("error")
                for worker in first["workers"]:
                    assert worker["title"].startswith("Demo: ")
                    assert 9000 <= int(worker["key"].split(":")[-1]) <= 9999
                for agent in first["fleet"]["agents"]:
                    assert agent["name"].startswith("demo-")
                    assert agent["context_state"] == "ok"
                    assert agent["model_state"] == "running"
                    assert agent["ledger_age"] == 90
                assert sum(a["busy"] is True for a in first["fleet"]["agents"]) == 3
                handler = object.__new__(self.handler)
                replies = []
                handler._reply = lambda *args: replies.append(args)
                handler.do_POST()
                assert replies[0][0] == 405
                print("synthetic snapshot verified")
        app.ThreadingHTTPServer = Server
        return app
    return real_import(name, *args, **kwargs)

with patch("builtins.__import__", isolated_import), \
     patch("threading.Thread.start", side_effect=AssertionError("no background workers")):
    screenshots.serve_demo(state)
assert (state / "ready.json").is_file()
'''
        result = subprocess.run([sys.executable, "-c", probe, str(state)], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert "synthetic snapshot verified" in result.stdout


def test_screenshot_broker_cleanup_on_capture_failure():
    from scripts import screenshots
    from unittest.mock import patch

    processes = []

    class Process:
        def __init__(self, args, **kwargs):
            self.state = Path(args[-1])
            assert kwargs["env"] == screenshots.demo_environment(self.state)
            (self.state / "ready.json").write_text("12345")
            self.terminated = self.killed = False
            processes.append(self)

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            if timeout:
                raise subprocess.TimeoutExpired("demo", timeout)
            assert self.killed

        def kill(self):
            self.killed = True

    with patch.object(screenshots.subprocess, "Popen", side_effect=Process) as start:
        try:
            with screenshots.demo_broker() as origin:
                assert origin == "http://127.0.0.1:12345"
                process = start.call_args
                state = Path(process.args[0][-1])
                raise ValueError("simulated capture failure")
        except ValueError:
            pass
        assert processes[0].terminated and processes[0].killed
        assert not state.exists()


def test_screenshot_capture_views_and_browser_cleanup():
    from scripts import screenshots
    from types import ModuleType
    from unittest.mock import MagicMock, patch

    api = ModuleType("playwright.sync_api")
    api.Error = RuntimeError
    manager = MagicMock()
    api.sync_playwright = lambda: manager
    playwright = manager.start.return_value
    browser = playwright.chromium.launch.return_value
    context = browser.new_context.return_value
    page = context.new_page.return_value
    page.locator.return_value.is_visible.return_value = False
    broker = MagicMock()
    broker.__enter__.return_value = "http://127.0.0.1:12345"
    with _preview_directory() as output, \
         patch.dict(sys.modules, {"playwright.sync_api": api}), \
         patch.object(screenshots, "demo_broker", return_value=broker):
        screenshots.capture(output)
        options = browser.new_context.call_args.kwargs
        assert options["viewport"] == {"width": 1400, "height": 900}
        assert options["timezone_id"] == "UTC" and options["locale"] == "en-US"
        assert [call.args[0] for call in page.goto.call_args_list] == [
            "http://127.0.0.1:12345/dash.html#" + view for view in ("runs", "fleet", "now")]
        assert [Path(call.kwargs["path"]).name for call in page.screenshot.call_args_list] == [
            "dashboard-workers.png", "dashboard-fleet.png", "dashboard-now.png"]
        policy = context.route.call_args.args[1]
        route = MagicMock()
        route.request.url = "https://example.com/must-not-load"
        policy(route)
        route.abort.assert_called_once()
        route.continue_.assert_not_called()
        route.request.url = "http://127.0.0.1:12345/state"
        policy(route)
        route.continue_.assert_called_once()
        page.locator.return_value.is_visible.return_value = True
        try:
            screenshots.capture(output)
            assert False, "must reject a failed render"
        except RuntimeError as exc:
            assert "Dashboard render failed" in str(exc)
        assert browser.close.call_count == playwright.stop.call_count == 2
        assert broker.__exit__.call_count == 2


if __name__ == "__main__":
    test_source_preview_starts_without_site_overlay()
    test_image_runtime_files_are_sufficient()
    test_docker_smoke_isolated_and_cleans_up()
    test_docker_smoke_failures_report_logs_and_clean_up()
    test_quickstart_document_links_exist()
    test_screenshot_demo_is_synthetic_and_read_only()
    test_screenshot_broker_cleanup_on_capture_failure()
    test_screenshot_capture_views_and_browser_cleanup()
    print("quickstart preview checks passed")
