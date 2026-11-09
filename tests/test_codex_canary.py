#!/usr/bin/env python3
"""codex REAL-BINARY canary (opt-in): proves the installed Codex routes every tool it offers through PreToolUse,
on EVERY model, code_mode_only ones (the code-mode gating fix) included.

The fake-codex tests (test_codex.py) cannot prove what the real CLI does; this one drives the installed binary with
the broker's own config.toml (adapters/codex.codex_config: hosted / MCP-backed surfaces disabled, code_mode_host
enabled, strict config) in a THROWAWAY CODEX_HOME whose ONE PreToolUse hook denies everything and logs each event.
The model is asked to apply_patch a file, web-search, run a shell command and try to open an interactive shell and
feed it a command via write_stdin; each must be denied (or absent) and nothing may run. If the model is
code_mode_only (every ChatGPT model on 0.164.0, per the code-mode gating fix), its shell call reaches the hook as
`exec_command`
(nested tools.exec_command() from its JS code), not the classic `Bash` - both are accepted. Run it and paste the
report into the MR / bump adapters/codex.CODEX_VERIFIED before any codex row:

    CODEX_CANARY=1 CODEX_AUTH_FILE=/abs/path/auth.json CODEX_CANARY_MODEL=<codex model id> python tests/test_codex_canary.py

Off (the default, CI): reports SKIPPED and passes. It spends one model turn on the operator's ChatGPT login; the
auth file is only symlinked, never read (the report says whether a refresh rewrote it through the link).

This canary proves hook COVERAGE (every tools.* call the model makes reaches PreToolUse). It does NOT and CANNOT
prove the deeper code-mode-gating-fix claim that the code-mode JS runtime has no reach to the filesystem /
network / processes / env outside tools.* - that needs a dedicated probe (e.g. asking the model to run JS that
attempts a direct filesystem read and confirming it fails, or Codex's own documentation of the code-mode sandbox)
which a generic "ask the model to call some tools" prompt cannot exercise. That dedicated probe is
tests/test_codex_probe.py (the workdir-bypass fix): no login needed (a local mock of the OpenAI Responses API),
so it also covers the workdir-bypass HIGH fix, `unified_exec_tty`/write_stdin, and the nested tool allow-set
against the REAL binary.
Record both this canary's report AND that probe's result before trusting code_mode_host on a code_mode_only row.

test_codex_real_binary_steer_canary (same opt-in) proves mid-turn steering (steer.py): a queued dashboard message
is claimed by the PostToolUse hook after a shell call and reaches the main model. Run it after a Codex bump;
Codex rows are steerable only on a version it has passed (CODEX_VERIFIED).
"""
import json, os, shlex, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("BROKER_SITE_ENV", os.path.join(HERE, "fixtures", "site.test.env"))
os.environ.setdefault("BROKER_CONFIG", os.path.join(HERE, "fixtures", "broker.test.json"))
os.environ.setdefault("BROKER_PROVENANCE_PATH", os.path.join(HERE, "fixtures", "provenance.test.json"))
import site_env  # noqa: F401

from adapters import codex as X
from adapters import codex_gate as G

PASS = []
MARK = "CANARY-RAN-42"
STDIN_MARK = "WROTE-STDIN-99"

DENY_ALL = r'''import json, sys
ev = sys.stdin.read()
with open(sys.argv[1], "a") as f:
    f.write(ev.replace("\n", " ") + "\n")
sys.stderr.write("eph-canary: DENIED (deny-all canary hook)\n")
sys.exit(2)
'''

PROMPT = f"""This is a tool-routing test. Make exactly these four tool calls, one after another, even if one fails:
1. Use apply_patch to create the file canary.txt containing the single line: hello
2. Use web search to look up: openai codex cli changelog
3. Run this shell command: echo CANARY-RAN-$((6*7))
4. Start an interactive shell with no command at all (just `bash`), then send it the command
   `echo {STDIN_MARK}` through whatever mechanism feeds a running session (e.g. write_stdin)
Then reply with one line per step saying whether it succeeded or was blocked."""


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


def _events(out):
    return [ev for _s, ev in X._events(out)]


def test_codex_config_strict_on_real_binary():
    """No model, no login: the installed binary loads both sandboxes' config.toml under --strict-config (every key,
    every DISABLED_FEATURES name known to it and really off (incl. `goals` / `sleep_tool`, the workdir-bypass
    fix), code_mode_host known and really ON - the code-mode gating fix: a code_mode_only model needs it to call
    any tool at all - and `unified_exec_tty` known and really OFF, the workdir-bypass fix: makes write_stdin
    inert whatever the gate does, stronger than any gate-side heuristic). Skipped without a binary (CI)."""
    bin_ = shutil.which(os.environ.get("CODEX_BIN") or "codex")
    if not bin_:
        return ok("codex strict-config check SKIPPED (no codex binary; set CODEX_BIN)")
    for sandbox in ("read-only", "workspace-write"):
        d = tempfile.mkdtemp(prefix="eph-codex-cfg-", dir=os.path.expanduser("~/.cache"))
        try:
            home = os.path.join(d, "home"); os.makedirs(home, mode=0o700)
            open(os.path.join(home, X.INSTRUCTIONS_FILE), "w").write("strict-config check\n")
            open(os.path.join(home, X.CONFIG_FILE), "w").write(
                X.codex_config("gpt-5", "high", sandbox, os.path.join(home, X.INSTRUCTIONS_FILE),
                               shell_path=os.environ.get("PATH", "/usr/bin:/bin")))
            env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "OPENAI_"))}
            env["CODEX_HOME"] = home
            # d is no git repo: exec stops at the trusted-directory check, AFTER the strict config load
            r = subprocess.run([bin_, "exec", "--strict-config", "--json", "-s", sandbox, "-"], input="x", cwd=d,
                               env=env, capture_output=True, text=True, timeout=60)
            assert "config" not in r.stderr.lower() and "trusted directory" in r.stderr, (sandbox, r.stderr[-800:])
            r = subprocess.run([bin_, "features", "list"], cwd=d, env=env, capture_output=True, text=True, timeout=60)
            state = {ln.split()[0]: ln.split()[-1] for ln in r.stdout.splitlines() if ln.split()}
            on = [f for f in X.DISABLED_FEATURES if state.get(f) != "false"]
            assert r.returncode == 0 and not on, ("features still on", on)
            assert state.get("code_mode_host") == "true", ("code_mode_host not reported on", state.get("code_mode_host"))
            assert state.get("unified_exec_tty") == "false", \
                ("unified_exec_tty not reported off (the workdir-bypass fix: write_stdin would stay live)", state.get("unified_exec_tty"))
            hooks_path = os.path.join(home, X.HOOKS_FILE)        # and the hook still resolves with them off
            with open(hooks_path, "w") as f:
                json.dump(X.hooks_config("true"), f)
            hooks, err = X.list_hooks(bin_, env, os.path.realpath(d))
            assert not err and [h for h in hooks if X._same(h.get("sourcePath"), hooks_path)
                                and h.get("eventName") == "preToolUse"], (err, hooks)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    ok("codex config.toml loads under --strict-config on the installed binary, every DISABLED_FEATURES is off and "
       "code_mode_host is on")


def test_codex_real_binary_canary():
    if os.environ.get("CODEX_CANARY") != "1":
        return ok("codex canary SKIPPED (set CODEX_CANARY=1, CODEX_AUTH_FILE, CODEX_CANARY_MODEL to run it)")
    bin_ = shutil.which(os.environ.get("CODEX_BIN") or "codex")
    auth = os.environ.get("CODEX_AUTH_FILE", "").strip()
    model = os.environ.get("CODEX_CANARY_MODEL", "").strip()
    assert bin_ and os.path.isabs(auth) and os.path.isfile(auth) and model, \
        "CODEX_CANARY=1 needs the codex binary, an absolute CODEX_AUTH_FILE and CODEX_CANARY_MODEL"
    version = subprocess.run([bin_, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    d = tempfile.mkdtemp(prefix="eph-codex-canary-", dir=os.path.expanduser("~/.cache"))
    try:
        home = os.path.join(d, "home"); os.makedirs(home, mode=0o700)
        wt = os.path.join(d, "wt"); os.makedirs(wt)
        subprocess.run(["git", "init", "-q", wt], check=True)
        open(os.path.join(wt, "README.md"), "w").write("canary\n")
        wt = os.path.realpath(wt)
        os.symlink(auth, os.path.join(home, X.AUTH_FILE))
        auth_before = os.stat(auth).st_mtime_ns
        hook_log, hook_py = os.path.join(d, "hook-events.jsonl"), os.path.join(d, "deny_all.py")
        open(hook_py, "w").write(DENY_ALL)
        command = " ".join(shlex.quote(p) for p in (sys.executable, "-I", hook_py, hook_log))
        instructions = os.path.join(home, X.INSTRUCTIONS_FILE)
        open(instructions, "w").write("You are a test agent. Follow the user's instructions exactly.\n")
        open(os.path.join(home, X.CONFIG_FILE), "w").write(X.codex_config(model, None, "workspace-write", instructions))
        hooks_path = os.path.join(home, X.HOOKS_FILE)
        json.dump(X.hooks_config(command), open(hooks_path, "w"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "OPENAI_"))}
        env["CODEX_HOME"] = home

        hooks, err = X.list_hooks(bin_, env, wt)                          # trust exactly our hook, as the harness does
        assert not err, err
        mine = [h for h in hooks if X._same(h.get("sourcePath"), hooks_path)]
        assert len(mine) == 1, hooks
        with open(os.path.join(home, X.CONFIG_FILE), "a") as f:
            f.write(f"\n[hooks.state.{X.toml_str(mine[0]['key'])}]\ntrusted_hash = {X.toml_str(mine[0]['currentHash'])}\n")
        hooks, err = X.list_hooks(bin_, env, wt)
        h = [h for h in hooks if X._same(h.get("sourcePath"), hooks_path)][0]
        assert not err and h.get("trustStatus") == "trusted" and h.get("enabled") is True, (err, h)

        r = subprocess.run(X.build_argv(bin_, model, "workspace-write"), input=PROMPT, cwd=wt, env=env,
                           capture_output=True, text=True, timeout=900)
        evs = _events(r.stdout)
        hook_evs = [json.loads(l) for l in open(hook_log)] if os.path.exists(hook_log) else []
        items = [e["item"] for e in evs if e.get("type") in ("item.started", "item.completed")
                 and isinstance(e.get("item"), dict)]
        item_types = sorted({i.get("type") for i in items})
        tools = sorted({e.get("tool_name") for e in hook_evs})
        shell_evs = [e for e in hook_evs if e.get("tool_name") in G.EXEC_TOOL_NAMES]
        shell_keys = sorted({k for e in shell_evs for k in (e.get("tool_input") or {})})
        errors = [e.get("message") for e in evs if e.get("type") == "error"]
        auth_after = os.stat(auth).st_mtime_ns
        report = {
            "codex": version, "rc": r.returncode, "turn_completed": any(e.get("type") == "turn.completed" for e in evs),
            "hooked_tools": tools, "shell_tool_input_keys": shell_keys,
            "keys_outside_EXEC_KEYS": sorted(set(shell_keys) - G.EXEC_KEYS),
            # the code-mode gating fix: which name the shell call actually reached the hook as - "exec_command"
            # means this model is code_mode_only and code_mode_host's nested-call coverage is what made the turn
            # possible at all.
            "shell_tool_name": sorted({e.get("tool_name") for e in shell_evs}),
            # probe report section 5 item 2: a nested call's tool_use_id starts "exec-" (the probe's finding) -
            # this is the only way to tell from the hook event alone that the nested code-mode path really ran,
            # as opposed to a classic direct tool call that happens to share the same tool name.
            "nested_tool_use_ids": sorted({e.get("tool_use_id") for e in hook_evs
                                           if str(e.get("tool_use_id") or "").startswith("exec-")}),
            "write_stdin_attempted": "write_stdin" in tools,
            "item_types": item_types, "error_events": errors,
            "transient_errors": [m for m in errors if X._TRANSIENT_ERROR_RE.match(str(m))],
            "auth_still_symlink": os.path.islink(os.path.join(home, X.AUTH_FILE))
            and os.readlink(os.path.join(home, X.AUTH_FILE)) == auth,
            "auth_source_rewritten_via_link": auth_after != auth_before,
            "auth_left_in_home": sorted(n for n in os.listdir(home) if "auth" in n),
            "stderr_tail": r.stderr[-600:],
        }
        print("CODEX CANARY REPORT\n" + json.dumps(report, indent=1))
        ran = [i for i in items if i.get("type") == "command_execution"
              and (MARK in str(i.get("aggregated_output")) or STDIN_MARK in str(i.get("aggregated_output")))]
        assert not os.path.exists(os.path.join(wt, "canary.txt")), "apply_patch ran past the deny-all hook"
        assert not ran, ("a shell command (incl. a write_stdin session) ran past the deny-all hook", ran)
        assert STDIN_MARK not in r.stdout, "the write_stdin mark appeared in the transcript: something ran"
        assert "web_search" not in item_types, "web_search ran: a hosted tool never reaches the hook"
        assert any(t == "apply_patch" for t in tools), ("apply_patch never reached the hook", tools)
        assert any(t in G.EXEC_TOOL_NAMES for t in tools), \
            ("no shell call (Bash or exec_command) reached the hook - with code_mode_host on, hooked_tools=[] "
             "here is exactly the code-mode-gating bug", tools)
        if "exec_command" in report["shell_tool_name"]:          # this model is code_mode_only (the code-mode gating fix)
            assert report["nested_tool_use_ids"], \
                ("the workdir-bypass fix: a code_mode_only model's nested call never got an 'exec-'-prefixed "
                 "tool_use_id - the proof that the nested path (not some other route) actually ran is missing", report)
        assert report["auth_still_symlink"], "a login refresh replaced the auth.json symlink with a copy"
    finally:
        shutil.rmtree(d, ignore_errors=True)               # rmtree never follows the auth symlink
    ok(f"codex canary on {version}: apply_patch + shell ({report['shell_tool_name']}) reached the deny-all hook "
       f"and were denied, web_search absent, write_stdin never ran")


ALLOW_ALL = r'''import sys
sys.stdin.read()
sys.exit(0)
'''

STEER_PROMPT = """This is a messaging test. Run exactly this one shell command: echo step-one
After it returns, reply with exactly one line: the steer word from any operator message you received during this
turn, or NONE if you received none."""


def test_codex_real_binary_steer_canary():
    """Mid-turn steering (steer.py) on the REAL binary: a message queued before the turn must be claimed by the
    PostToolUse hook after the shell call AND reach the MAIN model (it echoes the nonce). A hook that fires but
    whose additionalContext the harness drops would show here as claimed-but-not-echoed: the lost-message case."""
    if os.environ.get("CODEX_CANARY") != "1":
        return ok("codex steer canary SKIPPED (set CODEX_CANARY=1, CODEX_AUTH_FILE, CODEX_CANARY_MODEL to run it)")
    import steer
    bin_ = shutil.which(os.environ.get("CODEX_BIN") or "codex")
    auth = os.environ.get("CODEX_AUTH_FILE", "").strip()
    model = os.environ.get("CODEX_CANARY_MODEL", "").strip()
    assert bin_ and os.path.isabs(auth) and os.path.isfile(auth) and model, \
        "CODEX_CANARY=1 needs the codex binary, an absolute CODEX_AUTH_FILE and CODEX_CANARY_MODEL"
    version = subprocess.run([bin_, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    d = tempfile.mkdtemp(prefix="eph-codex-steer-canary-", dir=os.path.expanduser("~/.cache"))
    old_root = os.environ.get("BROKER_STEER_ROOT")
    try:
        os.environ["BROKER_STEER_ROOT"] = os.path.join(d, "steer")      # outside the sandbox's /tmp + $TMPDIR
        home = os.path.join(d, "home"); os.makedirs(home, mode=0o700)
        wt = os.path.join(d, "wt"); os.makedirs(wt)
        subprocess.run(["git", "init", "-q", wt], check=True)
        wt = os.path.realpath(wt)
        os.symlink(auth, os.path.join(home, X.AUTH_FILE))
        allow_py = os.path.join(d, "allow_all.py")
        open(allow_py, "w").write(ALLOW_ALL)
        gate = " ".join(shlex.quote(p) for p in (sys.executable, "-I", allow_py))
        key = "canary:steer"
        steer_cmd = steer.hook_command(key)
        instructions = os.path.join(home, X.INSTRUCTIONS_FILE)
        open(instructions, "w").write("You are a test agent. Follow the user's instructions exactly.\n")
        open(os.path.join(home, X.CONFIG_FILE), "w").write(X.codex_config(model, None, "workspace-write", instructions))
        hooks_path = os.path.join(home, X.HOOKS_FILE)
        json.dump(X.hooks_config(gate, steer_cmd), open(hooks_path, "w"))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CODEX_", "OPENAI_"))}
        env["CODEX_HOME"] = home
        hooks, err = X.list_hooks(bin_, env, wt)
        assert not err, err
        mine = [h for h in hooks if X._same(h.get("sourcePath"), hooks_path)]
        assert len(mine) == 2, hooks
        with open(os.path.join(home, X.CONFIG_FILE), "a") as f:
            for h in mine:
                f.write(f"\n[hooks.state.{X.toml_str(h['key'])}]\ntrusted_hash = {X.toml_str(h['currentHash'])}\n")
        hooks, err = X.list_hooks(bin_, env, wt)
        post = [h for h in hooks if X._same(h.get("sourcePath"), hooks_path) and h.get("eventName") == "postToolUse"]
        assert not err and len(post) == 1 and post[0].get("trustStatus") == "trusted" \
            and post[0].get("command") == steer_cmd, (err, hooks)

        word = "STEER-" + os.urandom(4).hex().upper()
        assert steer.enqueue(key, "canary1", f"The steer word is {word}.", "web:canary")
        r = subprocess.run(X.build_argv(bin_, model, "workspace-write"), input=STEER_PROMPT, cwd=wt, env=env,
                           capture_output=True, text=True, timeout=900)
        evs = [e for _, e in X._events(r.stdout)]
        final = [e["item"].get("text", "") for e in evs if e.get("type") == "item.completed"
                 and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message"]
        log_ = [json.loads(l) for l in open(steer.log_path(key))] if os.path.exists(steer.log_path(key)) else []
        claimed = any(x.get("state") == "delivered" and x.get("via") == "tool" for x in log_)
        report = {"codex": version, "model": model, "rc": r.returncode, "claimed_by_hook": claimed,
                  "echoed": any(word in t for t in final), "final": final[-1:] if final else [],
                  "left_in_inbox": steer.reclaim(key, "canary1") if not claimed else False,
                  "stderr_tail": r.stderr[-400:]}
        print("CODEX STEER CANARY REPORT\n" + json.dumps(report, indent=1))
        assert claimed, "the steer hook never claimed the message (it would fall back to the turn boundary)"
        assert report["echoed"], "claimed but the main model never saw it: steering would LOSE messages here"
    finally:
        if old_root is None:
            os.environ.pop("BROKER_STEER_ROOT", None)
        else:
            os.environ["BROKER_STEER_ROOT"] = old_root
        shutil.rmtree(d, ignore_errors=True)
    ok(f"codex steer canary on {version} / {model}: the PostToolUse hook claimed a queued message after the shell "
       f"call and the main model echoed it")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001 - report
            failures.append(t.__name__)
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
