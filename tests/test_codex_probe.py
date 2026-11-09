#!/usr/bin/env python3
"""codex OFFLINE real-binary probe (opt-in by presence of the binary, the workdir-bypass fix): ports an earlier
manual probe's mock-server/harness scripts into a repeatable test, so that probe's findings (its report's
section 5, items (a)-(f)) stay proven against the REAL `codex` CLI, not just the fake one tests/test_codex.py
drives.

Unlike tests/test_codex_canary.py (which needs a real ChatGPT login, CODEX_CANARY=1 opt-in), this probe needs NO
login and NO network: it points the installed binary at a throwaway localhost mock of the OpenAI Responses API
(model_provider = "mock" in config.toml, exactly as the broker's own codex_config() writes it otherwise) and
scripts the "model"'s turns as a fixed sequence of tool calls. Codex still runs its REAL sandbox and its REAL
code-mode JS runtime; only the far end (what the "model" says next) is canned. Skipped (and passes) with no
`codex` binary on PATH / CODEX_BIN - this file is written so ops (or CI with the binary installed) can run it;
it has not been run against the real binary from this sandbox (no codex install here).

Design note: rather than parsing Codex's `--json` event shapes to learn what ran inside a nested JS cell (that
format is not stable ground to build a test on), each scenario below has the JS report what it saw by embedding
it as the COMMAND TEXT of one final `tools.exec_command` call. That call reaches PreToolUse (every nested call
does) and OUR OWN hook script logs the raw event before deciding anything - so the JS's introspection results
are visible to the test whether that final call is itself allowed or denied.

(a) nested exec_command / apply_patch calls are hooked (tool_use_id starting "exec-") and a deny-all hook blocks
    every one of them; the worktree is left unchanged.
(b) the nested `tools` object never exceeds codex_gate.EXPECTED_NESTED_TOOLS (catches a new tool on upgrade).
(c) the JS runtime has no ambient reach: require/process/fetch/Deno/import are all unavailable.
(d) `tty: true` is rejected by Codex's own config (unified_exec_tty = false) and write_stdin then fails "stdin is
    closed", under an ALLOW-all hook (so the real runtime behaviour, not just the gate, is observed).
(e) the workdir-bypass fix, HIGH: against the REAL gate, `exec_command({cmd:"cat hostname", workdir:"/etc"})` is
    denied (the workdir is stripped from what the hook sees but Codex still honours it - this was ALLOWED before
    the fix); a properly `cd <worktree> || exit 1; `-prefixed read is not denied.
(f) against the REAL gate, a namespaced `collaboration.spawn_agent` call is denied as unmapped-tool.
"""
import json, os, shlex, shutil, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("BROKER_SITE_ENV", os.path.join(HERE, "fixtures", "site.test.env"))
os.environ.setdefault("BROKER_CONFIG", os.path.join(HERE, "fixtures", "broker.test.json"))
os.environ.setdefault("BROKER_PROVENANCE_PATH", os.path.join(HERE, "fixtures", "provenance.test.json"))
import site_env  # noqa: F401

from adapters import codex as X
from adapters import codex_gate as G
from adapters import hermes as H
from adapters.hermes_gate import DENIALS_FILE, POLICY_FILE

PASS = []
MODEL = "gpt-6.1-sol"          # code_mode_only on 0.164.0 (every 6.x/5.6 model in the catalog is)
# Every probe CODEX_HOME must live OUTSIDE $TMPDIR: against the real binary, exec_command failed
# "bwrap: execvp codex-linux-sandbox: No such file or directory" while CODEX_HOME sat under tempfile's default
# /tmp dir, even though a directly run codex sandbox subcommand against /usr/bin/true succeeds - Codex does not
# wire up its own arg0 codex-linux-sandbox helper for a home it considers transient. adapters.codex.CODEX_HOME_ROOT
# (production) already defaults to ~/.cache/eph-codex-homes, never a temp dir, so only this probe harness needed
# the fix.
PROBE_HOME_ROOT = os.path.expanduser("~/.cache/eph-codex-probe-homes")
# the real binary's own "No such file or directory" when its codex-linux-sandbox helper does not run for a
# session (whatever the cause) - detected so a probe scenario can skip with a clear reason instead of asserting
# a misleading pass/fail against output that was never going to reflect the thing being tested.
SANDBOX_HELPER_ERR = "codex-linux-sandbox"


def _probe_home():
    os.makedirs(PROBE_HOME_ROOT, mode=0o700, exist_ok=True)
    return tempfile.mkdtemp(prefix="probe-", dir=PROBE_HOME_ROOT)

LOG_DENY = r'''import sys
ev = sys.stdin.read()
with open(sys.argv[1], "a") as f:
    f.write(ev.replace("\n", " ") + "\n")
sys.stderr.write("eph-probe: DENIED (deny-all probe hook)\n")
sys.exit(2)
'''

LOG_ALLOW = r'''import sys
ev = sys.stdin.read()
with open(sys.argv[1], "a") as f:
    f.write(ev.replace("\n", " ") + "\n")
sys.exit(0)
'''


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


def _bin():
    return shutil.which(os.environ.get("CODEX_BIN") or "codex")


# ---------------------------------------------------------------------------------------------------------------
# a tiny mock of the OpenAI Responses API: one scripted step per POST /responses (port of the earlier manual
# probe's own mock_server.py, trimmed to what these scenarios need)
# ---------------------------------------------------------------------------------------------------------------
def _exec_tool(tools):
    for t in tools or []:
        if t.get("name") == "exec":
            return t.get("type"), "exec"
    return None, None


def _item_for_step(step, n, body):
    cid = "call_%d_%s" % (n, os.urandom(4).hex())
    if "exec_js" in step:
        kind, name = _exec_tool(body.get("tools"))
        if kind == "function":
            return {"type": "function_call", "id": "fc_%d" % n, "call_id": cid, "name": name,
                    "arguments": json.dumps({"code": step["exec_js"]}), "status": "completed"}
        return {"type": "custom_tool_call", "id": "ctc_%d" % n, "call_id": cid, "name": name or "exec",
                "input": step["exec_js"], "status": "completed"}
    if "function_call" in step:
        fc = step["function_call"]
        item = {"type": "function_call", "id": "fc_%d" % n, "call_id": cid, "name": fc["name"],
               "arguments": json.dumps(fc.get("arguments") or {}), "status": "completed"}
        if fc.get("namespace"):
            item["namespace"] = fc["namespace"]
        return item
    return {"type": "message", "id": "msg_%d" % n, "role": "assistant", "status": "completed",
           "content": [{"type": "output_text", "text": step.get("message", "done"), "annotations": []}]}


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"models": [], "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {}
        if not self.path.rstrip("/").endswith("responses"):
            self.send_response(404); self.end_headers(); return
        with self.server.probe_lock:
            n = self.server.probe_n
            self.server.probe_n += 1
        steps = self.server.probe_steps
        step = steps[n] if n < len(steps) else {"message": "done (mock out of steps)"}
        item = _item_for_step(step, n, body)
        rid = "resp_%d" % n
        usage = {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}
        events = [{"type": "response.created", "response": {"id": rid}},
                 {"type": "response.output_item.added", "output_index": 0, "item": item},
                 {"type": "response.output_item.done", "output_index": 0, "item": item},
                 {"type": "response.completed", "response": {"id": rid, "usage": usage, "output": [item]}}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for ev in events:
            self.wfile.write(("event: %s\ndata: %s\n\n" % (ev["type"], json.dumps(ev))).encode())
            self.wfile.flush()


class _Mock:
    """A throwaway localhost mock Responses server, one scripted step per request."""
    def __init__(self, steps):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
        self.srv.probe_steps, self.srv.probe_n, self.srv.probe_lock = steps, 0, threading.Lock()
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.srv.shutdown()
        self.thread.join(5)


def _mock_config(model, sandbox, instructions_path, port, features=None, writable_roots=()):
    """adapters.codex.codex_config(), with `model_provider = "mock"` pointed at our local server and any
    `features` override applied (same string-surgery the probe's harness.py used)."""
    cfg = X.codex_config(model, None, sandbox, instructions_path, writable_roots=writable_roots)
    for k, v in (features or {}).items():
        marker = "\n%s = " % k
        if marker in cfg:
            cfg = "\n".join((("%s = %s" % (k, v)) if ln.startswith("%s = " % k) else ln) for ln in cfg.split("\n"))
        else:
            cfg = cfg.replace("[features]\n", "[features]\n%s = %s\n" % (k, v), 1)
    cfg = cfg.replace("\n\n[features]", "\nmodel_provider = \"mock\"\n\n[features]", 1)
    cfg += ("\n[model_providers.mock]\nname = \"mock\"\n"
           "base_url = \"http://127.0.0.1:%d/v1\"\nwire_api = \"responses\"\nenv_key = \"MOCK_PROBE_KEY\"\n"
           "request_max_retries = 0\nstream_max_retries = 0\nsupports_websockets = false\n" % port)
    return cfg


def _run(run_dir, bin_, model, sandbox, hook_command, steps, timeout=120, features=None, home=None,
         linked_worktree=False):
    """One offline turn against the REAL codex binary + the mock server, under `hook_command` (its own log/decide
    logic) -> (rc, stdout, stderr, wt, home). Pass `home` when the caller already prepared that directory (e.g. to
    drop POLICY_FILE into it before codex starts) so this doesn't try to re-create it and collide; otherwise a
    fresh one is made under PROBE_HOME_ROOT (never run_dir: run_dir is a tempfile.TemporaryDirectory under
    $TMPDIR, and CODEX_HOME must not be - see PROBE_HOME_ROOT) and removed again before returning."""
    own_home = home is None
    if own_home:
        home = _probe_home()
    else:
        os.makedirs(home, mode=0o700, exist_ok=True)
    wt = os.path.join(run_dir, "wt")
    if linked_worktree:      # production shape: the worktree is a linked worktree of a repo elsewhere
        main = os.path.join(run_dir, "main"); os.makedirs(main)
        g = ["git", "-C", main, "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(["git", "init", "-q", main], check=True)
        with open(os.path.join(main, "README.md"), "w") as f:
            f.write("probe\n")
        subprocess.run(g + ["add", "."], check=True)
        subprocess.run(g + ["commit", "-qm", "init"], check=True)
        subprocess.run(g + ["worktree", "add", "-q", "-b", "probe", wt], check=True)
    else:
        os.makedirs(wt)
        subprocess.run(["git", "init", "-q", wt], check=True)
        with open(os.path.join(wt, "README.md"), "w") as f:
            f.write("probe\n")
    wt = os.path.realpath(wt)
    mock = _Mock(steps)
    try:
        instructions = os.path.join(home, X.INSTRUCTIONS_FILE)
        with open(instructions, "w") as f:
            f.write("You are an offline probe agent. Call tools exactly as told.\n")
        with open(os.path.join(home, X.CONFIG_FILE), "w") as f:
            f.write(_mock_config(model, sandbox, instructions, mock.port, features,
                                 X.git_writable_roots(wt) if sandbox == "workspace-write" else ()))
        hooks_path = os.path.join(home, X.HOOKS_FILE)
        with open(hooks_path, "w") as f:
            json.dump(X.hooks_config(hook_command), f)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "OPENAI_"))}
        env["CODEX_HOME"] = home
        env["MOCK_PROBE_KEY"] = "mock-not-a-secret"

        hooks, err = X.list_hooks(bin_, env, wt)                     # trust exactly our hook, as the harness does
        assert not err, err
        mine = [h for h in hooks if X._same(h.get("sourcePath"), hooks_path)]
        assert len(mine) == 1, hooks
        with open(os.path.join(home, X.CONFIG_FILE), "a") as f:
            f.write("\n[hooks.state.%s]\ntrusted_hash = %s\n"
                    % (X.toml_str(mine[0]["key"]), X.toml_str(mine[0]["currentHash"])))
        hooks, err = X.list_hooks(bin_, env, wt)
        h = [x for x in hooks if X._same(x.get("sourcePath"), hooks_path)][0]
        assert not err and h.get("trustStatus") == "trusted" and h.get("enabled") is True, (err, h)

        r = subprocess.run(X.build_argv(bin_, model, sandbox), input="run the probe", cwd=wt, env=env,
                          capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr, wt, home
    finally:
        mock.close()
        if own_home:
            shutil.rmtree(home, ignore_errors=True)


def _log_hook(run_dir, script, log_name="hook-events.jsonl"):
    """Write a LOG_DENY / LOG_ALLOW style script to `run_dir` -> (command string, log path)."""
    path = os.path.join(run_dir, "hook.py")
    with open(path, "w") as f:
        f.write(script)
    log = os.path.join(run_dir, log_name)
    return " ".join(shlex.quote(p) for p in (sys.executable, "-I", path, log)), log


def _hook_events(log):
    return [json.loads(l) for l in open(log)] if os.path.exists(log) else []


def _reported(hook_evs, prefix):
    """The LAST nested call whose command starts with `prefix` (our JS's own introspection report, embedded as
    the command text of a final tools.exec_command call) -> the text after the prefix, or None. The real
    tools.exec_command() JS call takes `cmd`, not `command` (that is the hermes/Bash tool_input name, never the
    nested JS API's own), so the hook event this test reads back carries whichever key the JS call used."""
    for e in reversed(hook_evs):
        ti = e.get("tool_input") or {}
        cmd = ti.get("cmd", ti.get("command"))
        if isinstance(cmd, str) and cmd.startswith(prefix):
            return cmd[len(prefix):]
    return None


# =================================================================================================================
def test_codex_probe_nested_hooked_denied_and_isolated():
    """Report 5(a)+(c): every nested call is hooked (tool_use_id starting "exec-") and denied by a deny-all hook
    (worktree unchanged); the JS runtime has no require/process/fetch/Deno/import."""
    bin_ = _bin()
    if not bin_:
        return ok("codex offline probe SKIPPED (no codex binary on PATH; set CODEX_BIN)")
    with tempfile.TemporaryDirectory() as d:
        js = (
            'let probe = {};\n'
            # await fn() (an async tryit): a tool call that is never awaited lets the cell end before Codex
            # delivers the nested call to the hook at all, cancelling it un-observed (the cell, not the call,
            # is what Codex actually waits on)
            'async function tryit(name, fn) { try { probe[name] = String(await fn()); } '
            'catch (e) { probe[name] = "ERR:" + String((e && e.message) || e).slice(0, 120); } }\n'
            'await tryit("require", () => { require("fs"); return "REACHABLE"; });\n'
            'await tryit("process", () => { return JSON.stringify(process.env).length; });\n'
            'await tryit("fetch", () => { fetch("http://127.0.0.1:1/x"); return "REACHABLE"; });\n'
            'await tryit("Deno", () => { Deno.readTextFileSync("/etc/hostname"); return "REACHABLE"; });\n'
            # import() returns a Promise: without the await, the cell can finish (and Codex deliver the final
            # exec_command report) before the rejection ever lands, so the probe reports REACHABLE for an import
            # the real sandbox actually refuses - await it like every other tryit fn.
            'await tryit("import", async () => { await import("node:fs"); return "REACHABLE"; });\n'
            'await tryit("tools_keys", () => JSON.stringify(Object.keys(tools).sort()));\n'
            'await tryit("apply_patch", async () => { await tools.apply_patch('
            '"*** Begin Patch\\n*** Add File: canary.txt\\n+hello\\n*** End Patch\\n"); return "RAN"; });\n'
            # the JS API param is `cmd`, not `command` (that key is a PreToolUse tool_input artefact, not what
            # the real tools.exec_command() function itself accepts)
            'await tools.exec_command({cmd: "echo PROBE:" + JSON.stringify(probe)});\n'
        )
        command, log = _log_hook(d, LOG_DENY)
        rc, out, err, wt, home = _run(d, bin_, MODEL, "workspace-write", command, [{"exec_js": js},
                                                                                    {"message": "done"}])
        hook_evs = _hook_events(log)
        assert hook_evs, ("no hook event at all - code_mode_host off, or the hook never ran", rc, out[-2000:], err[-2000:])
        assert any(str(e.get("tool_use_id") or "").startswith("exec-") for e in hook_evs), \
            ("no nested call carried an 'exec-' tool_use_id", hook_evs)
        assert any(e.get("tool_name") == "apply_patch" for e in hook_evs), hook_evs
        assert any(e.get("tool_name") in G.EXEC_TOOL_NAMES for e in hook_evs), hook_evs
        assert not os.path.exists(os.path.join(wt, "canary.txt")), "apply_patch ran past a deny-all hook"
        reported = _reported(hook_evs, "echo PROBE:")
        assert reported is not None, ("the JS's own report never reached the hook", hook_evs)
        probe = json.loads(reported)
        for name in ("require", "process", "fetch", "Deno", "import"):
            assert probe.get(name, "").startswith("ERR:"), (name, "has ambient reach outside tools.*", probe)
        assert probe.get("apply_patch") == "ERR:Command blocked by PreToolUse hook: " \
            or (probe.get("apply_patch") or "").startswith("ERR:"), probe
        ok("codex offline probe: nested calls hooked (exec- tool_use_id), apply_patch/exec_command denied by "
           "deny-all (worktree unchanged), JS runtime has no require/process/fetch/Deno/import reach")


def test_codex_probe_nested_tools_within_expected_set():
    """Report 5(b): the nested `tools` object the real binary offers never exceeds
    codex_gate.EXPECTED_NESTED_TOOLS - a Codex upgrade exposing a new nested tool must be triaged, not discovered
    live by a worker."""
    bin_ = _bin()
    if not bin_:
        return ok("codex offline probe SKIPPED (no codex binary on PATH; set CODEX_BIN)")
    with tempfile.TemporaryDirectory() as d:
        js = 'await tools.exec_command({cmd: "echo KEYS:" + JSON.stringify(Object.keys(tools).sort())});\n'
        command, log = _log_hook(d, LOG_DENY)
        rc, out, err, wt, home = _run(d, bin_, MODEL, "workspace-write", command,
                                      [{"exec_js": js}, {"message": "done"}])
        hook_evs = _hook_events(log)
        reported = _reported(hook_evs, "echo KEYS:")
        assert reported is not None, ("the tools-keys report never reached the hook", hook_evs)
        keys = set(json.loads(reported))
        extra = keys - G.EXPECTED_NESTED_TOOLS
        assert not extra, ("nested tool(s) beyond codex_gate.EXPECTED_NESTED_TOOLS - triage before any codex row",
                           sorted(extra), sorted(keys))
        ok("codex offline probe: nested `tools` surface (%s) stays within EXPECTED_NESTED_TOOLS" % sorted(keys))


MARKER_NAME = "codex-probe-cwd-marker.txt"


def test_codex_probe_tty_and_write_stdin_inert():
    """Report 5(d) / the workdir-bypass fix, item 2: `unified_exec_tty = false` makes Codex itself reject `tty: true` and close
    every exec_command session's stdin, so write_stdin fails "stdin is closed" whatever the gate does. Runs
    under an ALLOW-all hook so the real Codex runtime behaviour (not just the gate decision) is observed.

    M2 test gap (a): a missing-helper / no-session-id outcome used to `return ok(...)`
    - a SKIP that still counts as a PASS in tools/check.sh, so a host whose codex-linux-sandbox helper does not
    run at all went green here without this scenario proving anything. Those now FAIL loudly instead (a real
    environment problem belongs in red, not in a quiet skip); only "no codex binary at all" (the module-level
    skip above, documented as such) stays a skip.
    M2 test gap (b): the old "ran in the worktree" check read the hook-reported `cwd` - exactly the field Codex
    hides/does not honour per the module docstring's workdir-bypass-fix background, so asserting it proved
    nothing about where the command actually ran. Replaced with an observable side effect: the JS writes a
    marker file via a RELATIVE path, and the test asserts the file landed on disk inside the worktree."""
    bin_ = _bin()
    if not bin_:
        return ok("codex offline probe SKIPPED (no codex binary on PATH; set CODEX_BIN)")
    with tempfile.TemporaryDirectory() as d:
        js = (
            'let out = {};\n'
            'try { await tools.exec_command({cmd: "cat", tty: true}); out.tty = "ALLOWED"; }\n'
            'catch (e) { out.tty = String((e && e.message) || e); }\n'
            'let sid = null;\n'
            # "sleep 30" (not "cat"): a command that does not itself read stdin, so the session stays alive on
            # its own account rather than exiting immediately on EOF from the already-closed stdin
            # (unified_exec_tty = false) - otherwise there is no live session left for write_stdin to hit the
            # "stdin is closed" path at all. An explicit SHORT yield_time_ms (not whatever Codex's own default
            # happens to be) makes exec_command hand control back quickly while the session is still certainly
            # running: exec_command returns once EITHER the command exits OR yield_time_ms elapses, whichever is
            # first, so a bare "sleep 5" with no yield_time_ms risked the sleep finishing (and the session
            # closing on its own account, for a reason that has nothing to do with unified_exec_tty) before this
            # await ever returned on a loaded or slow host - passing or failing the assertion below for the
            # wrong reason. 30s of sleep against a 200ms yield leaves ample margin on a healthy host.
            'try { let r = await tools.exec_command({cmd: "sleep 30", yield_time_ms: 200}); '
            'sid = r && r.session_id; out.open = "ok"; }\n'
            'catch (e) { out.open = String((e && e.message) || e); }\n'
            'out.sid = sid ? "present" : "null";\n'
            'try { await tools.write_stdin({session_id: sid, chars: "x\\n"}); out.stdin = "ALLOWED"; }\n'
            'catch (e) { out.stdin = String((e && e.message) || e); }\n'
            # M2(b): a RELATIVE-path write is the observable side effect - it can only land inside the worktree
            # if exec_command really ran with that as its cwd, unlike the hook's own self-reported `cwd` field.
            'try { await tools.exec_command({cmd: "echo marker > %s"}); out.marker = "ran"; }\n'
            'catch (e) { out.marker = String((e && e.message) || e); }\n'
            'await tools.exec_command({cmd: "echo RESULT:" + JSON.stringify(out)});\n' % MARKER_NAME
        )
        command, log = _log_hook(d, LOG_ALLOW)
        rc, out, err, wt, home = _run(d, bin_, MODEL, "workspace-write", command,
                                      [{"exec_js": js}, {"message": "done"}])
        hook_evs = _hook_events(log)
        reported = _reported(hook_evs, "echo RESULT:")
        # M2(a): a live session needs the real binary's own codex-linux-sandbox helper to start each
        # exec_command; a host where that helper does not run is a real environment problem, not something
        # this scenario can silently work around - fail loudly with a clear reason instead of a quiet skip that
        # still counts as green in tools/check.sh.
        if reported is None and SANDBOX_HELPER_ERR in (err or "") + (out or ""):
            raise AssertionError(
                "codex offline probe tty/write_stdin: the real binary's own %s helper did not run for an "
                "exec_command session in this environment - this is an ENVIRONMENT PROBLEM (fix the install), "
                "not something this scenario can skip past" % SANDBOX_HELPER_ERR)
        assert reported is not None, ("the tty/write_stdin report never reached the hook", hook_evs, out[-2000:])
        result = json.loads(reported)
        if any(SANDBOX_HELPER_ERR in str(result.get(k, "")) for k in ("tty", "open", "stdin", "marker")):
            raise AssertionError(
                ("codex offline probe tty/write_stdin: an exec_command/write_stdin call failed on the real "
                 "binary's own %s helper, not on unified_exec_tty - ENVIRONMENT PROBLEM (fix the install), not "
                 "a skip" % SANDBOX_HELPER_ERR, result))
        # M2(a): a null session_id means the open attempt gave write_stdin nothing to feed - that is itself a
        # failure to exercise this scenario (whatever its root cause), not a reason to report green.
        if result.get("sid") == "null":
            raise AssertionError(
                ("codex offline probe tty/write_stdin: exec_command returned no session_id for the "
                 "long-running sleep session, so write_stdin had nothing to feed - this scenario did not "
                 "exercise unified_exec_tty here", result))
        assert "disabled" in result.get("tty", "").lower(), \
            ("tty:true was not rejected by Codex's own config (unified_exec_tty=false) - workdir-bypass fix item 2", result)
        assert "closed" in result.get("stdin", "").lower(), \
            ("write_stdin did not report a closed session - unified_exec_tty=false should make every session's "
             "stdin closed", result)
        # M2(b): the observable side effect, not the hook's own (untrustworthy) cwd field - the marker can only
        # be on disk inside the worktree if the exec_command that wrote it really ran there.
        assert result.get("marker") == "ran", ("the marker-writing exec_command did not run", result)
        marker_path = os.path.join(wt, MARKER_NAME)
        assert os.path.exists(marker_path), \
            ("the relative-path marker write never landed inside the worktree - exec_command did not run with "
             "cwd == the worktree", wt, os.listdir(wt))
        ok("codex offline probe: unified_exec_tty=false rejects tty:true and closes exec_command stdin, so "
           "write_stdin is inert regardless of the gate (report 5d); a relative-path marker write landed "
           "inside the worktree (an observable side effect, not the hook's own reported cwd)")


def test_codex_probe_workdir_bypass_fixed():
    """The workdir-bypass fix, HIGH: against the REAL gate, exec_command({cmd:"cat hostname", workdir:"/etc"})
    must now be DENIED (it was allowed pre-fix: workdir is stripped from what the hook sees but Codex still
    honours it). A properly `cd <worktree> || exit 1; `-prefixed read must still be ALLOWED by the real gate (not denied)."""
    bin_ = _bin()
    if not bin_:
        return ok("codex offline probe SKIPPED (no codex binary on PATH; set CODEX_BIN)")
    with tempfile.TemporaryDirectory() as d:
        wt_dir = os.path.realpath(os.path.join(d, "wt"))
        # no `>` redirect here: the gate's own lexer refuses ANY redirection by design (Deny("redirection (...)"),
        # terminal: kind floor) whatever the workdir-bypass fix does, so a redirected safe_cmd would be denied for
        # the WRONG reason and never prove the fix. Per the module docstring's design note, the top-level `codex
        # exec --json` event stream's shape for what happened INSIDE a code-mode cell is not stable ground to
        # assert "it really ran" on either - so this only asserts the real gate's VERDICT: the escape is denied,
        # the properly cd-prefixed plain read is not.
        safe_cmd = G._cd_prefix(wt_dir) + "cat README.md"
        js = (
            'try { await tools.exec_command({cmd: "cat hostname", workdir: "/etc"}); } catch (e) {}\n'
            'try { await tools.exec_command({cmd: %s}); } catch (e) {}\n' % json.dumps(safe_cmd)
        )
        home = _probe_home()               # never under $TMPDIR - see PROBE_HOME_ROOT
        try:
            policy_path, denials_path = os.path.join(home, POLICY_FILE), os.path.join(home, DENIALS_FILE)
            # wt_dir must match what _run() actually creates (os.path.join(run_dir, "wt"), realpath'd) for the
            # embedded safe_cmd's cd prefix to be byte-exact with the policy's own cwd
            with open(policy_path, "w") as f:
                json.dump(H.gate_policy("code", wt_dir, None, None), f)
            command = X.hook_command(policy_path)
            rc, out, err, wt, home2 = _run(d, bin_, MODEL, "workspace-write", command,
                                           [{"exec_js": js}, {"message": "done"}], home=home)
            assert wt == wt_dir, (wt, wt_dir)
            denials = _hook_events(denials_path)
            escape = [row for row in denials
                      if row.get("tool") == "terminal" and "must start with exactly" in row.get("reason", "")]
            assert escape, ("the workdir-escape attempt was not denied by the real gate - workdir-bypass-fix "
                            "regression", denials, out[-2000:])
            other = [row for row in denials if row not in escape]
            assert not other, ("the properly cd-prefixed read was denied by the real gate - the workdir-bypass "
                               "fix should not break legitimate reads", other, out[-2000:])
            ok("codex offline probe: the real gate denies exec_command's stripped-but-honoured workdir escape "
               "(the workdir-bypass fix, HIGH) while a cd-prefixed read is not denied")
        finally:
            shutil.rmtree(home, ignore_errors=True)


def test_codex_probe_spawn_agent_denied_unmapped():
    """Report 5(f): a namespaced `collaboration.spawn_agent` function_call (tool_name "collaborationspawn_agent")
    is denied as unmapped-tool by the real gate - the default-deny is the only thing stopping an un-hooked
    sub-agent, so it needs its own regression proof."""
    bin_ = _bin()
    if not bin_:
        return ok("codex offline probe SKIPPED (no codex binary on PATH; set CODEX_BIN)")
    with tempfile.TemporaryDirectory() as d:
        home = _probe_home()               # never under $TMPDIR - see PROBE_HOME_ROOT
        try:
            wt_dir = os.path.realpath(os.path.join(d, "wt"))
            policy_path, denials_path = os.path.join(home, POLICY_FILE), os.path.join(home, DENIALS_FILE)
            with open(policy_path, "w") as f:
                json.dump(H.gate_policy("code", wt_dir, None, None), f)
            command = X.hook_command(policy_path)
            steps = [{"function_call": {"name": "spawn_agent", "namespace": "collaboration",
                                        "arguments": {"message": "hi", "task_name": "probe"}}},
                    {"message": "done"}]
            rc, out, err, wt, home2 = _run(d, bin_, MODEL, "workspace-write", command, steps, home=home)
            denials = _hook_events(denials_path)
            hit = [row for row in denials if row.get("tool") == "collaborationspawn_agent"]
            assert hit, ("collaborationspawn_agent was not denied as unmapped-tool by the real gate", denials,
                        out[-2000:])
            assert hit[0].get("kind") == "unmapped-tool", hit[0]
            ok("codex offline probe: a namespaced collaboration.spawn_agent call is denied as unmapped-tool")
        finally:
            shutil.rmtree(home, ignore_errors=True)


def test_codex_probe_workspace_write_can_git_commit_in_linked_worktree():
    """The git-commit fix: with writable_roots = the worktree's git dir + common dir, a workspace-write turn in a
    linked worktree can `git add` + `git commit` under the REAL sandbox (before: "read-only file system")."""
    bin_ = _bin()
    if not bin_:
        return ok("codex offline probe SKIPPED (no codex binary on PATH; set CODEX_BIN)")
    with tempfile.TemporaryDirectory() as d:
        js = ('await tools.exec_command({cmd: "echo change > new.txt && git add -A && '
              'git -c user.name=t -c user.email=t@t commit -qm probe-commit 2> commit.err"});\n')
        command, log = _log_hook(d, LOG_ALLOW)
        rc, out, err, wt, home = _run(d, bin_, MODEL, "workspace-write", command,
                                      [{"exec_js": js}, {"message": "done"}], linked_worktree=True)
        if SANDBOX_HELPER_ERR in (err or "") + (out or ""):
            raise AssertionError("ENVIRONMENT PROBLEM: the real binary's %s helper did not run" % SANDBOX_HELPER_ERR)
        subj = subprocess.run(["git", "-C", wt, "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
        ep = os.path.join(wt, "commit.err")
        errtxt = open(ep).read() if os.path.exists(ep) else ""
        assert subj.strip() == "probe-commit", ("git commit did not land in the sandbox", subj, errtxt, out[-1500:])
        ok("codex offline probe: workspace-write turn in a linked worktree can git add + git commit")


if __name__ == "__main__":
    tests =[v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001 - report all
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nall {len(PASS)} codex offline probe checks passed")
