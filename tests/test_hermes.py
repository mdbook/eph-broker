#!/usr/bin/env python3
"""hermes harness self-tests: stdlib assert only, same runner shape as test_broker.py.

hermes is FAKED: a small script stands in for the `hermes` CLI (like test_broker's fake claude), records its
argv / stdin / env-key names / HERMES_HOME config, and prints stream-json. No real hermes, model or endpoint.
"""
import gc, hashlib, json, os, stat, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
# The suite's own made-up site overlay + roster (tests/fixtures), never the deployment's config/site.env
# or config/provenance.json, so it runs the same here and in the public export
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
import site_env  # noqa: F401

import coalescer as C
from coalescer import Coalescer
import adapters.cc as CC
from adapters.cc import CCAdapter, Worker
from adapters import hermes as H
# Tests must NEVER reach the live policy judge (real wrapper and its gate dir: its decisions.jsonl,
# allow-once, the operator ask DM). Point the judge at a path that cannot exist; a test that needs a judge builds a fake.
H.GENERAL_JUDGE.update(wrapper="/nonexistent/eph-test/gate-wrapper.sh", gate_dir="/nonexistent/eph-test/gate",
                       project_dir="/nonexistent/eph-test/gate")
from envelope import EnvelopeParser
from prompts import PromptLoader
from routing import Route, Router, ADAPTERS
from worktree import WorktreeManager

PASS = []


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


# eph-gate loader shared by the fakes: loads the plugin the adapter copied into the private HERMES_HOME the way
# hermes does (package import + register(ctx)); register() writes the post-run sentinel.
_LOAD_GATE = r'''
def _load_gate(home):
    import importlib.util, json, os, sys
    cfg = json.load(open(os.path.join(home, "config.yaml")))
    d = os.path.join(home, "plugins", "eph-gate")
    if "eph-gate" not in (cfg.get("plugins") or {}).get("enabled", []) or not os.path.isdir(d):
        return []
    spec = importlib.util.spec_from_file_location("eph_gate_plugin", os.path.join(d, "__init__.py"),
                                                  submodule_search_locations=[d])
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
    hooks = []
    class Ctx:
        def register_hook(self, name, cb): hooks.append(cb)
    m.register(Ctx())
    return hooks
def _directive(hooks, tool, args):
    for cb in hooks:
        r = cb(tool_name=tool, args=args)
        if isinstance(r, dict) and r.get("action") == "block":
            return ["block", r["message"]]
    return [None, None]
'''

# ---- fake hermes CLI: records the call, optionally sleeps / forks a grandchild, prints stream-json ----
FAKE_HERMES = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
''' + _LOAD_GATE + r'''
prompt = sys.stdin.read()
rec = os.environ.get("FAKE_RECORD")
home = os.environ.get("HERMES_HOME", "")
hooks = _load_gate(home) if home and not os.environ.get("FAKE_NO_GATE") else []
pol = os.path.join(home, "eph-gate-policy.json")
policy = json.load(open(pol)) if home and os.path.exists(pol) else None
cfg = open(os.path.join(home, "config.yaml")).read() if home and os.path.exists(os.path.join(home, "config.yaml")) else ""
if rec:
    with open(rec, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "prompt": prompt, "env_keys": sorted(os.environ),
                            "key": os.environ.get("AUXILIARY_VLLM_API_KEY"), "home": home, "config": cfg,
                            "cwd": os.getcwd(), "policy": policy, "terminal_cwd": os.environ.get("TERMINAL_CWD"),
                            "verdicts": {t: _directive(hooks, t, a)[0] for t, a in (
                                ("write_file", {"path": "x.txt", "content": "x"}),
                                ("patch", {"path": "x.txt", "old_string": "a", "new_string": "b"}),
                                ("terminal", {"command": "git status"}))}}) + "\n")
first = not (os.environ.get("FAKE_CHILD") and os.path.exists(os.environ["FAKE_CHILD"]))
if os.environ.get("FAKE_CHILD") and first:
    p = subprocess.Popen(["sleep", "30"])
    open(os.environ["FAKE_CHILD"], "w").write(str(p.pid))
if first or not os.environ.get("FAKE_SLEEP_ONCE"):
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
sid = "sess-h1"
print(json.dumps({"type": "system", "subtype": "init", "model": "m", "session_id": sid}))
print("not json noise")
print(json.dumps({"type": "text", "text": "partial"}))
text = os.environ.get("FAKE_TEXT", json.dumps({"status": "pass", "summary": "<p>reviewed</p>", "wake": []}))
res = {"type": "result", "session_id": sid, "exit_code": int(os.environ.get("FAKE_EXIT_CODE", "0")), "text": text,
       "tokens": {"input": 30, "output": 12, "total": 42}}
if os.environ.get("FAKE_ERROR"):
    res["error"] = os.environ["FAKE_ERROR"]
if os.environ.get("FAKE_NO_EXIT_CODE"):
    del res["exit_code"]
if not os.environ.get("FAKE_NO_RESULT"):
    print(json.dumps(res))
print("\nsession_id: " + sid, file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


# the hermes interpreter stand-in (the shebang above has a space, so the adapter uses the sibling `python`): runs
# the adapter's REAL interpreter-path guard under whatever python3 runs the tests (3.9 has no -P),
# then, for the pre-flight probe, answers it by loading eph-gate from the private home (every honoured
# capability, mr-review included, is gated)
FAKE_PYTHON = r'''#!/usr/bin/env python3
import json, os, sys
del sys.path[0]         # this script's dir (the fixture keeps it inside the worktree); stands in for -P
code = sys.argv[sys.argv.index("-c") + 1]
if sys.argv[-1] == "eph-path-check":
    exec(code); sys.exit(0)
end = "del _eph_bad, _eph_mod, _eph_cwd, _eph_under_cwd\n"
exec(code[:code.index(end) + len(end)])
''' + _LOAD_GATE + r'''
home = os.environ["HERMES_HOME"]
hooks = _load_gate(home)
print("EPH_GATE_PROBE " + json.dumps({
    "probe": _directive(hooks, sys.argv[-1], {}),
    "floor": _directive(hooks, "terminal", {"command": "git push https://example.invalid HEAD"}),
    "env": {k: v for k, v in os.environ.items() if k.startswith("HERMES_")}, "home": home, "home_resolved": home,
    "container": False, "install_env": False, "linters": [], "lsp": False}))
'''


def _fake_hermes(d):
    p = os.path.join(d, "fake_hermes.py")
    with open(p, "w") as f:
        f.write(FAKE_HERMES)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    py = os.path.join(d, "python")
    with open(py, "w") as f:
        f.write(FAKE_PYTHON)
    os.chmod(py, os.stat(py).st_mode | stat.S_IEXEC)
    return p


class _Registry:
    """A temp spawn-models.json with one cloud backend ("fakebe") + its token-only env file."""
    def __init__(self, d):
        self.path = os.path.join(d, "spawn-models.json")
        envf = os.path.join(d, ".fakebe-env")
        with open(envf, "w") as f:
            f.write("ANTHROPIC_AUTH_TOKEN=tok-test\nUNRELATED_SECRET=nope\n")
        with open(self.path, "w") as f:
            json.dump({"backends": {"fakebe": {"kind": "cloud", "model": "fake-model", "context_tokens": 1000,
                                                "base_url": "https://llm.example/", "env": envf}}}, f)

    def __enter__(self):
        self.old = CC.SPAWN_MODELS_JSON
        CC.SPAWN_MODELS_JSON = self.path
        return self

    def __exit__(self, *a):
        CC.SPAWN_MODELS_JSON = self.old


def _route(capability="mr-review", backend="fakebe", adapter="hermes", **env):
    return Route(domain="mr-review", repo="/srv/x", prompt_class="agent", agent="coder-agent", read_only=True,
                 env={"ZBUS_ENV": "x", **env}, model="claude-sonnet-5", capability=capability,
                 backend=backend, adapter=adapter)


def _adapter(d, router=None, worktree_mgr=None, prompt_loader=None, **kw):
    return CCAdapter(router=router, worktree_mgr=worktree_mgr, prompt_loader=prompt_loader,
                     hermes=H.HermesHarness(hermes_bin=_fake_hermes(d), home_root=os.path.join(d, "homes")), **kw)


def _records(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


class _StubWT:
    """Just enough WorktreeManager for the mr-review branch of _run_worker."""
    def __init__(self, d):
        self.path = os.path.join(d, "wt"); self.removed = []

    def make(self, key, **kw):
        os.makedirs(self.path, exist_ok=True); return self.path

    def mr_diff(self, key, ref):
        return "diff --git a/f b/f\n+x\n"

    def cut_sha(self, key):
        return "abc123"

    def remove(self, key):
        self.removed.append(key)


def _seam_adapter(d, row, alerts, parser=None):
    pdir = os.path.join(d, "prompts")
    os.makedirs(os.path.join(pdir, row.domain), exist_ok=True)
    pf = os.path.join(pdir, row.domain, row.prompt_class + ".md")
    open(pf, "w").write("STUB\n")
    approved = os.path.join(d, "approved.json")
    json.dump({f"{row.domain}/{row.prompt_class}": hashlib.sha256(open(pf, "rb").read()).hexdigest()},
              open(approved, "w"))
    yes = lambda evt: True
    return _adapter(d, router=Router(rows=[("gitlab:mr", yes, yes, row)]), worktree_mgr=_StubWT(d),
                    prompt_loader=PromptLoader(pdir, approved), envelope_parser=parser or EnvelopeParser(),
                    alert_ops=lambda m, subject=None: alerts.append(m))


_MR_BRIEF = {"mr": {"project": "x/y", "mr_iid": 7, "repo_path": "/srv/x", "source_branch": "b", "target_ref": "main"}}


def _gone(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


# =====================================================================================
def test_selector_default_cc_unchanged():
    assert ADAPTERS == ("cc", "hermes")
    r = Route(domain="ansible", repo="/srv/ansible", prompt_class="read-report", agent="ansible")
    assert r.adapter == "cc"
    rows = Router().rows
    assert rows and all(tmpl.adapter == "cc" for *_x, tmpl in rows), [t.adapter for *_x, t in rows]
    # with_model carries the selector through (it rebuilds Route positionally)
    assert _route().with_model().adapter == "hermes"
    assert rows[0][3].with_model().adapter == "cc"
    # a cc route never touches the hermes harness (the claude path runs; harness stays unbuilt)
    with tempfile.TemporaryDirectory() as d:
        fake_claude = os.path.join(d, "fake_claude.py")
        with open(fake_claude, "w") as f:
            f.write("#!/usr/bin/env python3\nimport json\nprint(json.dumps({'result': json.dumps("
                    "{'status': 'done', 'summary': 's', 'wake': []}), 'session_id': 's1'}))\n")
        os.chmod(fake_claude, 0o755)
        a = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=EnvelopeParser(),
                      claude_bin=fake_claude)
        env = a._one_turn(Worker("vikunja:task:1", "run-1", d), _route(capability="read", backend="",
                                                                       adapter="cc"), "SYS", "P", resume=False)
    assert env and env["status"] == "done" and a.hermes is None, (env, a.hermes)
    ok("hermes: selector defaults to cc on every row; with_model keeps it; a cc turn never builds hermes")


def test_unknown_adapter_fails_closed():
    try:
        Route(domain="x", repo="/r", prompt_class="p", agent="a", adapter="opencode")
        raise AssertionError("unknown adapter must raise at load")
    except ValueError as e:
        assert "unknown adapter" in str(e), e
    # spawn-time belt: a Route mutated after load still refuses in _one_turn (_run_worker's except raises the alert)
    r = _route(); r.adapter = "bogus"
    a = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=EnvelopeParser())
    try:
        a._one_turn(Worker("k", "run", "/tmp"), r, "SYS", "P", resume=False)
        raise AssertionError("unknown adapter must raise at spawn")
    except ValueError as e:
        assert "unknown worker adapter" in str(e), e
    ok("hermes: unknown adapter fails closed at load (Route) and at spawn (_one_turn)")


def test_hermes_happy_path_parses_envelope():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        rec = os.path.join(d, "rec.jsonl")
        a = _adapter(d, envelope_parser=EnvelopeParser(), max_turns=15, max_turns_by_capability={"mr-review": 7},
                     wall_clock_s=60)
        w = Worker("gitlab:mr:1", "run-h", d)
        env = a._one_turn(w, _route(FAKE_RECORD=rec), "SIGNED SYSTEM PROMPT 😀", "BRIEF", resume=False)
        assert env and env["status"] == "pass" and env["summary"] == "<p>reviewed</p>", env
        assert w.session_id == "sess-h1" and w.tokens == 42 and w.turns == 1 and not w.in_turn, vars(w)
        (r1,) = _records(rec)
        argv = r1["argv"]
        assert argv[:6] == ["chat", "--query-file", "-", "--format", "stream-json", "-Q"], argv
        for flag, val in (("-t", "file"), ("--max-turns", "7"), ("-m", "fake-model"), ("--provider", "eph"),
                          ("--run-budget", "60"), ("--source", "tool")):
            assert argv[argv.index(flag) + 1] == val, (flag, argv)
        assert "--ignore-rules" in argv and not [x for x in argv if x.startswith("--resume")], argv
        assert os.path.isabs(a.hermes.hermes_bin) and w.kill_group is True
        assert not {"--yolo", "-z", "--oneshot", "--ignore-user-config"} & set(argv), argv
        assert r1["prompt"] == "BRIEF" and r1["cwd"] == os.path.realpath(d), r1
        # token reaches hermes ONLY as KEY_ENV; no ANTHROPIC_* / unrelated secret / broker env in the process
        assert r1["key"] == "tok-test", r1["key"]
        assert not [k for k in r1["env_keys"] if k.startswith("ANTHROPIC_") or k == "UNRELATED_SECRET"], r1
        cfg = json.loads(r1["config"])           # JSON-as-YAML private config
        assert cfg["agent"]["system_prompt"] == "SIGNED SYSTEM PROMPT 😀", cfg["agent"]
        assert cfg["providers"]["eph"] == {"api": "https://llm.example/v1", "key_env": "AUXILIARY_VLLM_API_KEY",
                                           "transport": "chat_completions", "default_model": "fake-model",
                                           "context_length": 1000}, cfg["providers"]
        assert cfg["auth"]["adopt_external_logins"] is False and cfg["mcp_servers"] == {}, cfg
        # mr-review read tools: `file` only (no terminal), under eph-gate with cc's pure-read grant; the gate
        # the run loaded hard-denies write_file / patch / terminal
        assert "terminal" in cfg["agent"]["disabled_toolsets"] and "file" not in cfg["agent"]["disabled_toolsets"]
        assert cfg["plugins"]["enabled"] == ["eph-gate"], cfg["plugins"]
        assert r1["policy"]["capability"] == "mr-review" and r1["policy"]["edits"] is False, r1["policy"]
        assert r1["policy"]["allow"] == ["Read", "Grep", "Glob"], r1["policy"]["allow"]
        assert r1["terminal_cwd"] == os.path.realpath(d), r1["terminal_cwd"]
        assert r1["verdicts"] == {"write_file": "block", "patch": "block", "terminal": "block"}, r1["verdicts"]
        assert "tok-test" not in r1["config"], "the token value must never be written to disk"
        home = r1["home"]
        assert home.startswith(os.path.join(d, "homes")), home
        # forward / re-prompt turn: --resume the session in the SAME private home
        a._one_turn(w, _route(FAKE_RECORD=rec), "SYS", "FWD", resume=True)
        r2 = _records(rec)[1]
        assert "--resume=sess-h1" in r2["argv"] and r2["home"] == home, r2
        del w; gc.collect()
        assert not os.path.exists(home), "private HERMES_HOME must go with the Worker"
    ok("hermes: happy path - chat -q stream-json argv, mr-review `-t file` under eph-gate (read-only), caps, key-by-name only, "
       "private home, result -> SAME EnvelopeParser, --resume on the next turn, home cleaned up")


def test_parse_stream():
    assert H.parse_stream("") is None and H.parse_stream("noise\n{bad json\n") is None
    out = "\n".join([json.dumps({"type": "result", "session_id": "a", "text": "old", "exit_code": 1}),
                     json.dumps({"type": "result", "session_id": "b", "text": "new", "exit_code": 0,
                                 "tokens": {"input": 2, "output": 3}, "error": "e"})])
    w = H.parse_stream(out)
    assert w["result"] == "new" and w["session_id"] == "b" and w["error"] == "e", w
    assert w["usage"] == {"input_tokens": 2, "output_tokens": 3}, w
    ok("hermes: parse_stream takes the LAST result record, ignores noise, repacks into the CC wrapper")


def test_hermes_timeout_hard_kill_and_escalate():
    alerts, escalations = [], []

    class _P(EnvelopeParser):
        def escalate(self, run_id, route, reason, task_id=None, worker_text=None):
            escalations.append((run_id, reason, task_id))

    with tempfile.TemporaryDirectory() as d, _Registry(d):
        child = os.path.join(d, "child.pid")
        a = _adapter(d, envelope_parser=_P(), wall_clock_s=1, alert_ops=lambda m, subject=None: alerts.append(m))
        w = Worker("vikunja:task:55", "run-t", d)
        t0 = time.time()
        env = a._turn(w, _route(FAKE_SLEEP="5", FAKE_CHILD=child), "SYS", "P", resume=False, run_id="run-t")
        dt = time.time() - t0
        gpid = int(open(child).read())
        time.sleep(0.2)
        try:
            with open(f"/proc/{gpid}/stat") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            state = "gone"
    assert env is None and dt < 5, (env, dt)                     # seed + re-prompt both killed at ~1s
    assert sum("hard-killed" in m for m in alerts) == 2, alerts
    assert escalations and escalations[0][0] == "run-t" and escalations[0][2] == 55, escalations
    assert state in ("gone", "Z"), f"tool grandchild must die with the process group, state={state}"
    assert w.in_turn is False and w.turn_started is None
    ok(f"hermes: wall-clock hard-kill of the whole process group (~{dt:.1f}s for seed + re-prompt), "
       "ops alerted, then the SAME envelope escalation")


def test_hermes_refuses_ungrantable():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        rec = os.path.join(d, "rec.jsonl")
        a = _adapter(d, envelope_parser=EnvelopeParser())
        # write/code are honoured under eph-gate now (tests/test_hermes_gate.py); code still refuses here because
        # CODE_NONCLAUDE_OK is unset
        os.environ.pop("CODE_NONCLAUDE_OK", None)
        cases = [dict(capability=c) for c in ("read", "code", "inbox", "inbox-chat", "tfa", "")]
        cases += [dict(backend=""), dict(backend="claude"), dict(backend="nope")]
        for kw in cases:
            try:
                a._one_turn(Worker("k", "run", d), _route(FAKE_RECORD=rec, **kw), "SYS", "P", resume=False)
                raise AssertionError(f"must refuse {kw}")
            except H.HermesRefused:
                pass
        assert not os.path.exists(rec), "a refused route must never launch hermes"
        assert H.hermes_toolsets("mr-review") == ["file"] and sorted(H.HERMES_GRANTS) == ["code", "general", "mr-review", "write"]
        # --ignore-rules is the ONE flag keeping worktree AGENTS.md/.hermes.md/SOUL.md (worker-writable) out of the
        # prompt: on every argv shape, fresh and resumed, read-only and read-write
        for ts, res in ((["file"], None), (["file", "terminal"], None), (["file", "terminal"], "sess-1")):
            assert "--ignore-rules" in H.build_argv("/bin/h", "m", ts, 1, 1, resume_session=res), (ts, res)
        for bad in ("a b", "a;b", "../x", "a/b", "é", "abc\n", "abc\ndef"):
            try:
                H.build_argv("/bin/h", "m", ["todo"], 1, 1, resume_session=bad)
                raise AssertionError(f"must refuse session id {bad!r}")
            except H.HermesRefused:
                pass
        w = Worker("k", "run", d); w.session_id = "bad id"
        try:
            a._one_turn(w, _route(FAKE_RECORD=rec), "SYS", "P", resume=True)
            raise AssertionError("must refuse a malformed resume id")
        except H.HermesRefused:
            pass
        assert not os.path.exists(rec), "a refused route must never launch hermes"
        # a missing binary is refused at the turn, not a PATH lookup
        a2 = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=EnvelopeParser(),
                       hermes=H.HermesHarness(hermes_bin=os.path.join(d, "nope"), home_root=os.path.join(d, "h2")))
        try:
            a2._one_turn(Worker("k", "run", d), _route(), "SYS", "P", resume=False)
            raise AssertionError("missing binary must refuse")
        except H.HermesRefused:
            pass
    ok("hermes: refuses read/code(no CODE_NONCLAUDE_OK)/general/inbox/inbox-chat/unknown capabilities, native/unresolvable "
       "backends, malformed resume ids and a missing binary - all before any launch")


def test_hermes_seam_launch_and_refusal():
    """End-to-end through the real _run_worker: an mr-review hermes row launches the fake hermes and posts; an
    unhonourable capability is refused and alerted with no launch. Separate rec per case; counted after join."""
    results, alerts = {}, []
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        for cap in ("mr-review", "read"):
            rec = os.path.join(d, f"rec-{cap}.jsonl")
            row = Route(domain="mr-review", repo="/srv/x", prompt_class="agent", agent="coder-agent", read_only=True,
                        env={"FAKE_RECORD": rec}, capability=cap, backend="fakebe", adapter="hermes")
            os.makedirs(os.path.join(d, cap))
            a = _seam_adapter(os.path.join(d, cap), row, alerts)
            key = "gitlab:mr:7"
            a.spawn(key, dict(_MR_BRIEF))
            for _ in range(100):                    # seed turn finished (idle), or the refusal alert fired
                if (_records(rec) and not a.liveness(key)) or any("refuses" in m for m in alerts):
                    break
                time.sleep(0.05)
            time.sleep(0.1)
            a.teardown(key); a._join(key, timeout=5)
            results[cap] = len(_records(rec))
    assert results == {"mr-review": 1, "read": 0}, results
    assert any("refuses capability 'read'" in m for m in alerts), alerts
    ok("hermes: full seam - an mr-review hermes row launches the fake hermes; a read row is refused and "
       "ops-alerted with no launch")


def test_hermes_pass_gating():
    calls = []

    class _P(EnvelopeParser):
        def try_parse(self, raw, run_id, route):
            calls.append(raw); return super().try_parse(raw, run_id, route)

    pass_text = json.dumps({"status": "pass", "summary": "s", "wake": []})
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a = _adapter(d, envelope_parser=_P())
        for env_kw in (dict(FAKE_EXIT_CODE="1", FAKE_ERROR="x"), dict(FAKE_ERROR="x"), dict(FAKE_EXIT_CODE="1"),
                       dict(FAKE_RC="3"), dict(FAKE_NO_EXIT_CODE="1")):
            w = Worker("k", "run", d)
            assert a._one_turn(w, _route(FAKE_TEXT=pass_text, **env_kw), "SYS", "P", resume=False) is None, env_kw
            assert w.session_id == "sess-h1", "session kept so the re-prompt can --resume"
        w = Worker("k", "run", d)
        assert a._one_turn(w, _route(FAKE_NO_RESULT="1"), "SYS", "P", resume=False) is None
        assert "not json noise" in w.last_output or "partial" in w.last_output, w.last_output   # raw tail kept
        assert calls == [], "the parser is only called for a clean run with a result record"
        assert a._one_turn(Worker("k", "run", d), _route(FAKE_TEXT=pass_text), "SYS", "P", resume=False)["status"] \
            == "pass" and len(calls) == 1
    ok("hermes: PASS gating - exit_code!=0 / missing exit_code / error / rc!=0 / no result record -> parse "
       "miss (parser not called); a clean run parses")


def test_hermes_teardown_kills_group():
    alerts = []
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        child = os.path.join(d, "child.pid")
        rec = os.path.join(d, "rec.jsonl")          # the fake sleeps on EVERY call: a re-prompt launch would hang
        row = _route(capability="mr-review", FAKE_SLEEP="30", FAKE_CHILD=child, FAKE_RECORD=rec)
        a = _seam_adapter(d, row, alerts)
        a.wall_clock_s = 60
        key = "gitlab:mr:7"
        a.spawn(key, dict(_MR_BRIEF))
        for _ in range(100):
            if os.path.exists(child) and open(child).read():
                break
            time.sleep(0.05)
        gpid = int(open(child).read())
        t0 = time.time()
        a.teardown(key); a._join(key, timeout=10)
        dt = time.time() - t0
        time.sleep(0.2)
        assert _gone(gpid), "teardown must kill the hermes process GROUP (tool grandchild too)"
        assert len(_records(rec)) == 1, "no re-prompt launch after teardown"
    assert dt < 5, dt
    assert not any("hard-killed" in m for m in alerts), alerts
    ok(f"hermes: teardown kills the whole process group (grandchild gone, {dt:.1f}s), no re-prompt launch")


def test_hermes_home_lifecycle():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        # root hygiene: symlink refused; existing root tightened to 0700; stale homes swept, fresh kept
        real = os.path.join(d, "real"); os.makedirs(real)
        link = os.path.join(d, "link"); os.symlink(real, link)
        try:
            H.HermesHarness(hermes_bin=_fake_hermes(d), home_root=link)
            raise AssertionError("symlinked home root must be refused")
        except H.HermesRefused:
            pass
        root = os.path.join(d, "homes"); os.makedirs(root, mode=0o755); os.chmod(root, 0o755)
        old, fresh = os.path.join(root, "old"), os.path.join(root, "fresh")
        os.makedirs(os.path.join(old, "sessions")); os.makedirs(fresh)
        t = time.time() - H.HOME_MAX_AGE_S - 60
        os.utime(old, (t, t))
        H.HermesHarness(hermes_bin=_fake_hermes(d), home_root=root)
        assert stat.S_IMODE(os.stat(root).st_mode) == 0o700
        assert not os.path.exists(old) and os.path.isdir(fresh)
        # exception path: the home is created, then the exec fails and _run_worker's finally removes it
        # (weakref.finalize disabled here, so only the explicit cleanup can pass this)
        alerts, made = [], []
        a = _seam_adapter(d, _route(capability="mr-review"), alerts)
        orig_fin = H.weakref.finalize
        H.weakref.finalize = lambda obj, fn, path, *x: made.append(path)
        try:
            os.chmod(a.hermes.hermes_bin, 0o644)      # resolvable at init, not executable, so Popen raises
            a.spawn("gitlab:mr:7", dict(_MR_BRIEF)); a._join("gitlab:mr:7", timeout=10)
        finally:
            H.weakref.finalize = orig_fin
        assert made and not os.path.exists(made[0]), made
        assert any("failed" in m for m in alerts), alerts
    ok("hermes: home root symlink refused, chmod 0700, stale homes swept; exec failure -> home removed by "
       "_run_worker's finally (not just the weakref)")


FAKE_CLAUDE_REC = r'''#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ["FAKE_RECORD"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
print(json.dumps({"result": "no envelope here", "session_id": "s-cc"}))
'''


def _run_turn_then_teardown(a, key, w, route, rec, teardown):
    """Run a._turn in a thread for a registered worker; optionally tear it down once the first launch is seen."""
    import threading
    esc = []
    a.envelope_parser.escalate = lambda run_id, route, reason, **kw: esc.append(reason)
    with a._lock:
        a._workers[key] = w
    t = threading.Thread(target=lambda: a._turn(w, route, "SYS", "P", resume=False, run_id="r"), daemon=True)
    t0 = time.time(); t.start()
    if teardown:
        for _ in range(200):
            if _records(rec):
                break
            time.sleep(0.02)
        a.teardown(key)
    t.join(20)
    return len(_records(rec)), esc, time.time() - t0, t.is_alive()


def test_teardown_no_reprompt_both_adapters():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        # cc: fake claude that records + sleeps on every call and never emits an envelope
        fc = os.path.join(d, "fake_claude_rec.py"); open(fc, "w").write(FAKE_CLAUDE_REC); os.chmod(fc, 0o755)
        out = {}
        for name, teardown, sleep in (("cc-torn", True, "30"), ("cc-live", False, "0")):
            rec = os.path.join(d, f"{name}.jsonl")
            a = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=EnvelopeParser(),
                          claude_bin=fc, wall_clock_s=60)
            r = _route(capability="read", backend="", adapter="cc", FAKE_RECORD=rec, FAKE_SLEEP=sleep)
            out[name] = _run_turn_then_teardown(a, "k-" + name, Worker("k-" + name, "r", d), r, rec, teardown)
        # hermes: fake sleeps on every call
        for name, teardown, sleep in (("h-torn", True, "30"), ("h-live", False, "0")):
            rec = os.path.join(d, f"{name}.jsonl")
            os.makedirs(os.path.join(d, name))
            a = _adapter(os.path.join(d, name),
                         envelope_parser=EnvelopeParser(), wall_clock_s=60)
            r = _route(FAKE_RECORD=rec, FAKE_SLEEP=sleep, FAKE_TEXT="no envelope")
            out[name] = _run_turn_then_teardown(a, "k-" + name, Worker("k-" + name, "r", d), r, rec, teardown)
        # hermes-side belt on its own: a done worker never launches, whatever the caller
        rec = os.path.join(d, "belt.jsonl")
        w = Worker("k", "r", d); w.done = True
        assert a.hermes.one_turn(a, w, _route(FAKE_RECORD=rec), "SYS", "P", None) is None
        assert not os.path.exists(rec)
    for name in ("cc-torn", "h-torn"):
        n, esc, dt, alive = out[name]
        assert n == 1 and not esc and dt < 10 and not alive, (name, out[name])
    for name in ("cc-live", "h-live"):             # regression: an ordinary parse miss still re-prompts + escalates
        n, esc, dt, alive = out[name]
        assert n == 2 and esc and not alive, (name, out[name])
    ok("a torn-down worker gets NO warm re-prompt launch / escalation on BOTH adapters (cc + hermes); a live "
       "parse miss still re-prompts + escalates; hermes never launches for a done worker")


def test_popen_failure_clears_in_turn():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a = _adapter(d, envelope_parser=EnvelopeParser())
        os.chmod(a.hermes.hermes_bin, 0o644)
        w = Worker("k", "run", d)
        try:
            a._one_turn(w, _route(), "SYS", "P", resume=False)
            raise AssertionError("exec failure must raise")
        except OSError:
            pass
        assert w.in_turn is False and w.turn_started is None
    ok("hermes: an exec failure raises (-> ops alert) and still clears in_turn / turn_started")


def test_yaml_safe_json():
    try:
        import yaml
    except ImportError:
        ok("hermes: yaml_safe_json (PyYAML absent here; round-trip check skipped)")
        return
    s = "😀 \"q\" \\ \n\t - é \x85\x9f\x7f﻿   "
    assert yaml.safe_load(H.yaml_safe_json({"k": s}))["k"] == s
    ok("hermes: config JSON round-trips through PyYAML (astral + NEL/LS/PS/C1 escaped)")


def test_backend_hermes_block():
    """Per-backend `hermes:{transport,overrides}`: absent = the earlier behaviour; set = carried into the
    provider + model_overrides; malformed = refused (fail-closed)."""
    with tempfile.TemporaryDirectory() as d, _Registry(d) as reg:
        prov, model, tok, ov = H.hermes_provider("fakebe")           # no hermes block
        assert prov["transport"] == "chat_completions" and ov == {}, (prov, ov)
        assert "model_overrides" not in H.hermes_config("S", prov, model, ()), "absent block must add nothing"
        assert prov["api"] == "https://llm.example/v1" and tok == "tok-test"

        def put(block):
            data = json.load(open(reg.path))
            data["backends"]["fakebe"]["hermes"] = block
            json.dump(data, open(reg.path, "w"))

        put({"transport": "anthropic_messages", "overrides": {"supports_reasoning": True}})
        prov, model, tok, ov = H.hermes_provider("fakebe")
        assert prov["transport"] == "anthropic_messages" and ov == {"supports_reasoning": True}
        cfg = H.hermes_config("S", prov, model, (), overrides=ov)
        assert cfg["model_overrides"] == {"fake-model": {"supports_reasoning": True}}, cfg.get("model_overrides")
        assert cfg["model"]["provider"] == H.PROVIDER == "eph"    # custom-named, never literally `anthropic`
        # CC's load_backend must not see / care about the hermes-only block
        assert CC.load_backend("fakebe")[0] == "fake-model"

        for bad in ({"transport": "anthropic"}, {"bogus": 1}, "chat_completions",
                    {"overrides": {"supports_reasoning": "yes"}}, {"overrides": {"context_length": 5}}):
            put(bad)
            try:
                H.hermes_provider("fakebe")
            except H.HermesRefused:
                continue
            raise AssertionError(f"malformed hermes block {bad!r} was not refused")
    ok("hermes: per-backend hermes block - absent unchanged, set carried, malformed refused")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} hermes self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001 - keep going, report all
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
