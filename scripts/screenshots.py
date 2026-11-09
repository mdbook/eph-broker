#!/usr/bin/env python3
"""Capture the real dashboard against an isolated, synthetic demo broker.

Playwright is an optional developer tool, not a broker dependency. There is
deliberately no URL option: this must never capture a running deployment.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
NOW = 1893501000  # Fixed demo clock, independent of the machine's date.
VIEWS = {"workers": "runs", "fleet": "fleet", "now": "now"}


def demo_environment(state):
    """Allowlist, not a copy of the caller's potentially live broker settings."""
    return {
        "PATH": os.defpath,
        "HOME": str(state),
        "TMPDIR": str(state),
        "PYTHONDONTWRITEBYTECODE": "1",
        "BROKER_SITE_ENV": os.devnull,
        "BROKER_CONFIG": str(ROOT / "config/demo/broker.example.json"),
        "EVENTS_LOG": str(state / "events.jsonl"),
        "BROKER_STATE_PATH": str(state / "broker-state.sqlite3"),
        "WORKTREES_ROOT": str(state / "worktrees"),
        "FLEET_STATUS_PATH": str(state / "fleet.json"),
        "FLEET_MD_PATH": str(state / "FLEET.md"),
        "SUPERVISOR_STATUS_PATH": str(state / "supervisor.json"),
        "DASH_INSTANCE_NAME": "Synthetic demo",
    }


def seed_demo(broker, state):
    """Seed the normal snapshot inputs, without ingesting or launching anything."""
    for index, (title, model, harness) in enumerate([
        ("Demo: add comet search", "claude-sonnet-5", "cc"),
        ("Demo: review moon garden colors", "gpt-6.1", "codex"),
        ("Demo: test paper rocket sorter", "claude-opus-5-5", "hermes"),
    ]):
        key = "vikunja:task:" + str(9101 + index)
        broker.coalescer.live[key] = {"state": "open_running", "events": [{}],
                                      "forwards": index, "expire": NOW + 45}
        broker.route_stamps[key] = "code/demo"
        broker.adapter._workers[key] = SimpleNamespace(
            title=title, route="code/demo", model=model, backend=model, harness=harness,
            session_id="demo-session-" + str(index), run_id="demo-run-" + str(index),
            turns=index + 2, tokens=12000 + 8000 * index, items=8 + index,
            in_turn=True, turn_started=NOW - 120 - index * 60, last_activity=NOW - 2,
            model_at=NOW - 120 - index * 60)
    broker.coalescer.live["vikunja:task:9104"] = {
        "state": "open_running", "events": [{}], "expire": NOW + 45}
    broker.adapter._waiting["vikunja:task:9104"] = "repo_lock"
    broker.route_stamps["vikunja:task:9104"] = "code/demo"
    broker.coalescer.pending["vikunja:task:9105"] = {"events": [{}, {}], "spawn_at": NOW + 3}
    for ticket in (9106, 9107):
        broker.state.enqueue("rerun", {"task_id": ticket})

    agents, sessions = {}, {}
    for index, (name, role, status) in enumerate([
        ("demo-comet", "Builder", "Demo: adding comet search"),
        ("demo-orbit", "Reviewer", "Demo: reviewing moon garden colors"),
        ("demo-nova", "Tester", "Demo: checking paper rocket fixtures"),
        ("demo-lunar", "Planner", "Demo: ready for the next exercise"),
        ("demo-pixel", "Designer", "Demo: waiting for a simulated restart"),
    ]):
        ledger = state / name
        ledger.mkdir()
        (ledger / "STATE.md").write_text(status, encoding="utf-8")
        os.utime(ledger / "STATE.md", (NOW - 90, NOW - 90))
        model = "claude-sonnet-5" if index % 2 else "claude-opus-5-5"
        agents[name] = {
            "role": role, "status": status, "status_source": "now", "cwd": str(ledger),
            "agent_type": "tfa" if index == 2 else "standing", "alive": index != 4,
            "model": model, "runtime_model": model, "runtime_model_at": NOW - 10,
            "effort": "high", "context_tokens": 24000 + index * 9000,
            "context_window": 200000, "context_ceiling": 160000, "context_floor": 120000,
            "context_sampled_at": NOW - 10,
        }
        sessions[name] = {"alive": index != 4, "busy": index < 3,
                          "busy_since": NOW - 240, "attempts": 1 if index == 4 else 0}
    for filename, data in [
        ("fleet.json", {"generated_at": NOW, "agents": agents}),
        ("supervisor.json", {"generated_at": NOW, "tmux_server_up": True, "sessions": sessions}),
    ]:
        path = state / filename
        path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(path, (NOW, NOW))
    (state / "FLEET.md").write_text("FLEET_MODE: normal\n", encoding="utf-8")


def serve_demo(state):
    # Even the private child entry point cannot inherit deployment config.
    os.environ.clear()
    os.environ.update(demo_environment(state))
    sys.path.insert(0, str(ROOT))
    import app

    class ReadOnlyDemo(app.Handler):
        def do_POST(self):
            self._reply(405, {"error": "screenshot demo is read-only"})

    # Freeze only the modules that timestamp demo state; do not freeze the
    # standard time module, which the HTTP server and subprocess timeouts need.
    clock = SimpleNamespace(time=lambda: NOW)
    with patch("dash.time", clock), patch("broker_state.time", clock):
        app.BROKER = app.Broker()
        try:
            seed_demo(app.BROKER, state)
            # No app.main(), tick loop, liveness, modules or agent processes.
            with app.ThreadingHTTPServer(("127.0.0.1", 0), ReadOnlyDemo) as server:
                (state / "ready.json").write_text(json.dumps(server.server_address[1]))
                server.serve_forever()
        finally:
            app.BROKER.state.close()


@contextmanager
def demo_broker():
    # Keep all temporary writes inside the checkout, including the child's HOME.
    with tempfile.TemporaryDirectory(prefix=".screenshots-", dir=ROOT) as directory:
        state = Path(directory)
        with (state / "broker.log").open("w+") as log:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve-demo", str(state)],
                cwd=ROOT, env=demo_environment(state), stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while not (state / "ready.json").exists():
                    if process.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise RuntimeError("Demo broker failed to start:\n" + log.read())
                    time.sleep(0.05)
                port = json.loads((state / "ready.json").read_text())
                yield "http://127.0.0.1:" + str(port)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def capture(output):
    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError as exc:
        raise RuntimeError("Python Playwright is not importable; no screenshots written. "
                           "Use an environment with Playwright and headless Chromium already available.") from exc

    try:
        playwright = sync_playwright().start()
    except Exception as exc:
        raise RuntimeError("Playwright driver could not start; no screenshots written.\n" + str(exc)) from exc
    try:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Error as exc:
            raise RuntimeError("Playwright Chromium could not launch; no screenshots written. "
                               "Use an environment with headless Chromium already available.\n" + str(exc)) from exc
        try:
            with demo_broker() as origin:
                context = browser.new_context(viewport={"width": 1400, "height": 900},
                                              device_scale_factor=1, locale="en-US",
                                              timezone_id="UTC", color_scheme="light",
                                              reduced_motion="reduce", service_workers="block")
                # Future UI assets must not introduce requests to real services.
                context.route("**/*", lambda route: route.continue_()
                              if route.request.url.startswith(origin + "/") else route.abort())
                context.add_init_script("""
                    localStorage.setItem('eph-theme', 'light');
                    const RealDate = Date;
                    window.Date = class extends RealDate {
                      constructor(...args) { super(...(args.length ? args : [NOW_MS])); }
                      static now() { return NOW_MS; }
                    };
                """.replace("NOW_MS", str(NOW * 1000)))
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                output.mkdir(parents=True, exist_ok=True)
                for name, view in VIEWS.items():
                    page.goto(origin + "/dash.html#" + view, wait_until="networkidle")
                    page.wait_for_function("typeof lastState !== 'undefined' && lastState && "
                                           "lastState.workers.length === 3 && lastState.fleet.agents.length === 5")
                    # Section ids mirror dash.py's VIEWS map; Fleet is the one without the v- prefix.
                    page.locator("#fleet-view" if view == "fleet" else "#v-" + view).wait_for(state="visible")
                    page.evaluate("document.fonts.ready")
                    if errors or page.locator("#err").is_visible():
                        raise RuntimeError("Dashboard render failed: " + repr(errors))
                    page.screenshot(path=str(output / ("dashboard-" + name + ".png")),
                                    animations="disabled", caret="hide")
                context.close()
        finally:
            browser.close()
    finally:
        playwright.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/images",
                        help="PNG destination (default: docs/images in this checkout)")
    parser.add_argument("--serve-demo", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_demo:
        serve_demo(args.serve_demo)
        return
    try:
        capture(args.output)
    except RuntimeError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
