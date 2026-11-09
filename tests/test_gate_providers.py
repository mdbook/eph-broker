#!/usr/bin/env python3
"""exec-gate provider tests: selection, the fail-closed dispatch contract, a stub provider, the built-ins
(deny / none / exec) and the Portcullis reference provider, incl. an identity pin against the pre-wiring.

stdlib assert only, same runner shape as the other suites. No real gate is ever run: every subprocess gate here
is a fake script in a temp dir.
"""
import contextlib, json, os, re, subprocess, sys, tempfile, types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
for k in [k for k in os.environ if k.startswith("EXEC_GATE")]:
    del os.environ[k]                   # every test states its own gate env; never the deployment's
import site_env  # noqa: F401

from adapters import hermes as H
from adapters.hermes_gate import gate as G, providers as P
from adapters.hermes_gate.providers import portcullis as PC

PASS = []


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


@contextlib.contextmanager
def gate_env(**kw):
    """Run with exactly these EXEC_GATE* keys set (all others removed), restored after."""
    saved = {k: v for k, v in os.environ.items() if k.startswith("EXEC_GATE")}
    for k in saved:
        del os.environ[k]
    os.environ.update(kw)
    try:
        yield
    finally:
        for k in [k for k in os.environ if k.startswith("EXEC_GATE")]:
            del os.environ[k]
        os.environ.update(saved)


@contextlib.contextmanager
def repo():
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        wt = os.path.join(d, "wt")
        os.makedirs(wt)
        subprocess.run(["git", "init", "-q", wt], check=True)
        open(os.path.join(wt, "f.txt"), "w").write("x\n")
        yield d, wt


def script(d, name, body):
    p = os.path.join(d, name)
    open(p, "w").write("#!/usr/bin/env python3\n" + body)
    os.chmod(p, 0o700)
    return p


# ==========================================================================================================
# selection
# ==========================================================================================================
def test_selection_default_known_unknown():
    with gate_env():
        assert P.selected().NAME == "deny", "EXEC_GATE unset must select deny"
        assert P.judge_config()["provider"] == "deny"
        assert P.floor() == {"files": [], "terms": []}, "deny adds no floors"
    for name in ("deny", "none", "exec", "portcullis"):
        assert P.get(name).NAME == name
    for bad in ("Portcullis", "gatehome", "", None, "none "):
        try:
            P.get(bad)
        except ValueError:
            continue
        raise AssertionError("unknown provider %r must raise" % bad)
    ok("selection: unset -> deny; the four built-ins resolve by NAME; unknown / mis-cased / empty -> ValueError")


def test_gate_policy_refuses_misconfig():
    with repo() as (d, wt):
        for env, caps in (({"EXEC_GATE": "nope"}, ("general", "code", "write", "mr-review")),
                          ({"EXEC_GATE": "portcullis"}, ("general",)),
                          ({"EXEC_GATE": "portcullis", "EXEC_GATE_PORTCULLIS_WRAPPER": "rel/w",
                            "EXEC_GATE_PORTCULLIS_HOOK": "/h", "EXEC_GATE_PORTCULLIS_PYTHON": "/p",
                            "EXEC_GATE_PORTCULLIS_DIR": "/g"}, ("general",)),
                          ({"EXEC_GATE": "exec"}, ("general",)),
                          ({"EXEC_GATE": "exec", "EXEC_GATE_COMMAND": "relative-gate"}, ("general",)),
                          ({"EXEC_GATE": "exec", "EXEC_GATE_COMMAND": "/bin/gate 'unterminated"}, ("general",)),
                          ({"EXEC_GATE": "none", "EXEC_GATE_TIMEOUT": "0"}, ("general",)),
                          ({"EXEC_GATE": "none", "EXEC_GATE_TIMEOUT": "soon"}, ("general",)),
                          ({"EXEC_GATE": "none", "EXEC_GATE_INHERIT_ENV": "OK,NOT-OK"}, ("general",))):
            with gate_env(**env):
                for cap in caps:
                    try:
                        H.gate_policy(cap, wt, test_runner="pytest", nonce="N")
                    except H.HermesRefused as e:
                        assert "fail-closed" in str(e), e
                        continue
                    raise AssertionError("%s with %r must refuse the turn" % (cap, env))
        with gate_env(EXEC_GATE="portcullis"):     # paths missing: only a JUDGED capability needs them
            pol = H.gate_policy("code", wt, test_runner="pytest", nonce="N")
            assert "judge" not in pol and pol["floor"]["files"] == [r"AUTHORIZED\.md"], pol.get("floor")
    ok("gate_policy: unknown gate refuses EVERY capability; a general turn with missing / relative / "
       "unparseable gate config, a bad timeout or a bad env name is refused (HermesRefused); a code turn needs no "
       "judge config but still carries the gate's floors")


# ==========================================================================================================
# the dispatch contract, with a stub provider
# ==========================================================================================================
@contextlib.contextmanager
def stub_provider(fn):
    calls = []
    mod = types.SimpleNamespace(NAME="stub", LABEL="Stub Gate", FLOOR_FILES=(r"GATE\.cfg",), FLOOR_TERMS=(r"stubgate",),
                                config=lambda env: {"knob": env.get("EXEC_GATE_STUB_KNOB", "k")})

    def judge(request, cfg):
        calls.append((request, cfg))
        return fn(request, cfg)
    mod.judge = judge
    P.PROVIDERS["stub"] = mod
    try:
        yield calls
    finally:
        del P.PROVIDERS["stub"]


def _general(wt, **env):
    with gate_env(**env):
        return G.Policy(H.gate_policy("general", wt, nonce="N-S"))


def test_stub_provider_contract():
    outcomes = {"allow": {"decision": "allow", "reason": "stub ok"},
                "deny": {"decision": "deny", "reason": "eph-gate: DENIED by stub: no", "token": "tok-1"},
                "badtoken": {"decision": "deny", "reason": "no", "token": "../../etc/x"},
                "maybe": {"decision": "ask", "reason": "hmm"},
                "list": ["allow"], "none": None, "raise": RuntimeError("boom")}
    with repo() as (d, wt):
        for mode, want in (("allow", (True, None)), ("deny", (False, "tok-1")), ("badtoken", (False, None)),
                           ("maybe", (False, None)), ("list", (False, None)), ("none", (False, None)),
                           ("raise", (False, None))):
            def fn(request, cfg, mode=mode):
                o = outcomes[mode]
                if isinstance(o, BaseException):
                    raise o
                return o
            with stub_provider(fn) as calls:
                p = _general(wt, EXEC_GATE="stub", EXEC_GATE_STUB_KNOB="v")
                v = G.decide("terminal", {"command": "ls -la > out.txt"}, p)
                assert (v.allowed, v.sha) == want and v.kind == "judge", (mode, v)
                if not v.allowed and mode not in ("deny", "badtoken"):
                    assert "fail-closed" in v.reason or mode == "maybe", (mode, v.reason)
                (req, cfg), = calls
                assert req == {"tool": "Bash", "tool_input": {"command": "ls -la > out.txt"}, "cwd": wt,
                               "session_id": "eph-N-S", "capability": "general", "write_root": wt}, req
                assert cfg["knob"] == "v" and cfg["provider"] == "stub" and cfg["timeout"] == "5.0", cfg
                assert cfg["inherit_env"] == ["TMPDIR", "LANG", "TZ"], "neutral default env pass-through"
        with stub_provider(lambda r, c: {"decision": "allow"}) as calls:
            p = _general(wt, EXEC_GATE="stub")
            assert G.decide("write_file", {"path": "f.txt", "content": "y"}, p).allowed
            assert calls[-1][0]["tool"] == "Write" and calls[-1][0]["tool_input"]["file_path"] == wt + "/f.txt"
            for tool, args in (("write_file", {"path": "GATE.cfg", "content": "y"}),
                               ("write_file", {"path": "sub/gate.CFG", "content": "y"}),
                               ("terminal", {"command": "cat ~/.stubgate/state"}),
                               ("terminal", {"command": "git push origin HEAD"})):
                n = len(calls)
                v = G.decide(tool, args, p)
                assert not v.allowed and v.kind == "floor" and len(calls) == n, (tool, args, v)
            p.judge = dict(p.judge, provider="gone")
            v = G.decide("terminal", {"command": "ls"}, p)
            assert not v.allowed and v.kind == "judge" and "no usable exec gate" in v.reason, v
    ok("dispatch: the provider gets the CC-shaped request + its config + shared keys; only "
       "decision==allow passes; deny / ask / non-dict / None / raise all deny (kind judge); a token is passed "
       "through only if plain; the provider's FLOOR_FILES / FLOOR_TERMS are hard floors before the judge; an "
       "unknown provider at call time denies")


def test_floor_validation_fail_closed():
    with repo() as (d, wt):
        base = H.gate_policy("code", wt, test_runner="pytest", nonce="N")
        for bad in ({"files": "x"}, {"files": [""]}, {"terms": [1]}, ["x"], {"files": ["("]}):
            try:
                G.Policy(dict(base, floor=bad))
            except (ValueError, re.error):
                continue
            raise AssertionError("floor %r must be refused" % (bad,))
        assert G.Policy(dict(base, floor=None)).policy_source.search("a/.claude/settings.json")
    ok("policy floor: a malformed / non-compiling floor makes the Policy refuse to load (the plugin then "
       "blocks every call); no floor block = core floors only")


# ==========================================================================================================
# built-ins
# ==========================================================================================================
def test_deny_and_none():
    with repo() as (d, wt):
        p = _general(wt)
        v = G.decide("terminal", {"command": "ls"}, p)
        assert not v.allowed and v.kind == "judge" and "no exec gate configured" in v.reason and v.sha is None, v
        assert not G.decide("write_file", {"path": "f.txt", "content": "y"}, p).allowed
        assert G.decide("read_file", {"path": "f.txt"}, p).allowed, "reads never need the judge"
        p = _general(wt, EXEC_GATE="none")
        assert G.decide("terminal", {"command": "ls -la > out.txt"}, p).allowed
        assert G.decide("write_file", {"path": "f.txt", "content": "y"}, p).allowed
        for tool, args in (("terminal", {"command": "git push origin HEAD"}), ("terminal", {"command": "sudo ls"}),
                           ("write_file", {"path": ".git/config", "content": "y"}),
                           ("write_file", {"path": ".claude/settings.json", "content": "{}"})):
            v = G.decide(tool, args, p)
            assert not v.allowed and v.kind == "floor", (tool, v)
    ok("deny (default): every judged call denied, reads still work; none: floors only, every eph-gate floor "
       "(general deny list, .git, .claude settings) still holds")


def test_exec_provider():
    body = r'''import json, os, sys, time
req = json.load(sys.stdin)
d = os.path.dirname(os.path.abspath(__file__))
open(os.path.join(d, "seen.jsonl"), "a").write(json.dumps({"req": req, "env": dict(os.environ), "cwd": os.getcwd(),
                                                           "argv": sys.argv[1:]}) + "\n")
mode = open(os.path.join(d, "mode")).read().strip()
if mode == "allow": print(json.dumps({"decision": "allow", "reason": "fine"}))
elif mode == "deny": print(json.dumps({"decision": "deny", "reason": "nope"}))
elif mode == "rc1": print(json.dumps({"decision": "allow"})); sys.exit(1)
elif mode == "garbage": print("allow")
elif mode == "list": print(json.dumps(["allow"]))
elif mode == "sleep": time.sleep(5)
'''
    with repo() as (d, wt):
        g = script(d, "gate.py", body)
        os.environ["EPH_TEST_KEEP"] = "passed"
        os.environ["EPH_TEST_LEAK"] = "leaked"
        try:
            for mode, allowed in (("allow", True), ("deny", False), ("rc1", False), ("garbage", False),
                                  ("list", False), ("silent", False), ("sleep", False)):
                open(os.path.join(d, "mode"), "w").write(mode)
                p = _general(wt, EXEC_GATE="exec", EXEC_GATE_COMMAND=g + " --profile 'eph workers'",
                             EXEC_GATE_TIMEOUT="1", EXEC_GATE_INHERIT_ENV="EPH_TEST_KEEP")
                v = G.decide("terminal", {"command": "ls -la > out.txt"}, p)
                assert v.allowed is allowed and v.kind == "judge" and v.sha is None, (mode, v)
                if mode == "deny":
                    assert v.reason == "eph-gate: DENIED by exec gate: nope", v.reason
            seen = [json.loads(l) for l in open(os.path.join(d, "seen.jsonl"))]
            s = seen[0]
            assert s["req"]["tool"] == "Bash" and s["req"]["tool_input"] == {"command": "ls -la > out.txt"}
            assert s["argv"] == ["--profile", "eph workers"] and s["cwd"] == wt, s
            assert s["env"].get("EPH_TEST_KEEP") == "passed" and "EPH_TEST_LEAK" not in s["env"], s["env"]
            assert s["env"]["PATH"] == "/usr/bin:/bin", s["env"]
        finally:
            del os.environ["EPH_TEST_KEEP"], os.environ["EPH_TEST_LEAK"]
        p = _general(wt, EXEC_GATE="exec", EXEC_GATE_COMMAND=os.path.join(d, "missing-gate"))
        v = G.decide("terminal", {"command": "ls"}, p)
        assert not v.allowed and "did not run" in v.reason, v
    ok("exec: request JSON on stdin, argv from EXEC_GATE_COMMAND (shell-quoted), cwd = the call's cwd, "
       "minimal env + EXEC_GATE_INHERIT_ENV only; allow needs rc 0 + decision allow; deny / rc!=0 / garbage / "
       "non-object / silent / timeout / missing binary all deny")


# ==========================================================================================================
# portcullis: the reference provider, pinned against the pre-hardcoded wiring
# ==========================================================================================================
# A deployment shaped like the one the pre-code hardcoded (same layout, made-up names): gate bin + switch dir
# under one dot-dir, the hook interpreter in its own venv, one extra env name passed through.
SITE = dict(EXEC_GATE="portcullis", EXEC_GATE_PORTCULLIS_WRAPPER="~/.gatehome/bin/gate-wrapper.sh",
            EXEC_GATE_PORTCULLIS_HOOK="~/.gatehome/bin/gate-hook.py",
            EXEC_GATE_PORTCULLIS_PYTHON="~/gate-venv/bin/python",
            EXEC_GATE_PORTCULLIS_DIR="~/.gatehome/eph-general", EXEC_GATE_TIMEOUT="5.0",
            EXEC_GATE_INHERIT_ENV="BUS_ENV,TMPDIR,LANG,TZ", EXEC_GATE_PROTECTED_TERMS="gatehome")
# hermes.GENERAL_JUDGE and gate.py's floors exactly as main 841a7ee built them, with the names above swapped in
OLD_JUDGE = {"wrapper": os.path.expanduser("~/.gatehome/bin/gate-wrapper.sh"),
             "hook": os.path.expanduser("~/.gatehome/bin/gate-hook.py"),
             "python": os.path.expanduser("~/gate-venv/bin/python"),
             "gate_dir": os.path.expanduser("~/.gatehome/eph-general"),
             "project_dir": os.path.expanduser("~/.gatehome/eph-general"), "timeout": "5.0"}
OLD_POLICY_SOURCE = re.compile(r"(?i)(^|/)(AUTHORIZED\.md|\.claude/+settings[^/]*\.json)$")
OLD_TERMINAL_FLOOR = re.compile(r"(?i)authori[sz]ed\.md|\.claude/+(\./+)*settings|gatehome")
OLD_INHERIT_ENV = ("BUS_ENV", "TMPDIR", "LANG", "TZ")


def test_portcullis_identity_with_site_config():
    """The site config reproduces the old hardcoded judge, floors, env and alert hint exactly."""
    with gate_env(**SITE):
        j = P.judge_config()
        assert {k: j[k] for k in OLD_JUDGE} == OLD_JUDGE, j
        assert tuple(j["inherit_env"]) == OLD_INHERIT_ENV and j["provider"] == "portcullis"
        assert P.override_hint() == "mint in ~/.gatehome/eph-general/allow-once/ if warranted", P.override_hint()
        assert P.label() == "Portcullis"
        with repo() as (d, wt):
            p = G.Policy(H.gate_policy("general", wt, nonce="N"))
    corpus = ["AUTHORIZED.md", "x/authorized.MD", "AUTHORISED.md", "a/AUTHORIZED.md.bak", ".claude/settings.json",
              ".claude//settings.local.json", ".claude/./settings", "cat ~/.gatehome/eph-general/decisions.jsonl",
              "ls /srv/u/.GATEHOME", "gate_home", "gate home", "echo hi >> AUTHORIZED.md", "cat x/authorised.md",
              "settings.json", "claude/settings.json", "/a/b/.claude/settings-x.json", "README.md", "ls -la"]
    for s in corpus:
        assert bool(p.policy_source.search(s)) == bool(OLD_POLICY_SOURCE.search(s)), ("write floor", s)
        assert bool(p.terminal_floor.search(s)) == bool(OLD_TERMINAL_FLOOR.search(s)), ("terminal floor", s)
    ok("portcullis identity: the site keys give the exact old GENERAL_JUDGE paths/timeout/project_dir, the "
       "old env pass list, the old allow-once hint, and write/terminal floors that match the old regexes on a "
       "corpus (incl. the protected term, AUTHORISED, .claude/./settings)")


def test_portcullis_protects_its_own_home():
    """N1: the terminal belt on the gate's own dir comes from the portcullis config, not a site key."""
    bare = {k: v for k, v in SITE.items() if k != "EXEC_GATE_PROTECTED_TERMS"}
    for env in (bare, dict(SITE), dict(SITE, EXEC_GATE_PROTECTED_TERMS="")):
        with gate_env(**env):
            assert P.floor()["terms"] == [r"authori[sz]ed\.md", "gatehome"], (env, P.floor())
            with repo() as (d, wt):
                p = G.Policy(H.gate_policy("general", wt, nonce="N"))
        for s in ("cat ~/.gatehome/eph-general/decisions.jsonl", "ls /srv/u/.GATEHOME", "touch gatehome/x"):
            assert p.terminal_floor.search(s), (env, s)
    with gate_env(**dict(SITE, EXEC_GATE_PROTECTED_TERMS="gatehome,Extra.Name, extra.name")):
        assert P.floor()["terms"] == [r"authori[sz]ed\.md", "gatehome", r"Extra\.Name"], P.floor()
    # nothing shared narrower than / or $HOME: the gate dir path itself
    for gd, wrapper in (("/var/lib/pc/eph", "/opt/pc/bin/w.sh"), ("~/pc-state", "~/pc-bin/w.sh")):
        with gate_env(**dict(bare, EXEC_GATE_PORTCULLIS_DIR=gd, EXEC_GATE_PORTCULLIS_WRAPPER=wrapper,
                             EXEC_GATE_PORTCULLIS_HOOK=os.path.dirname(wrapper) + "/h.py")):
            assert P.floor()["terms"][1:] == [re.escape(os.path.expanduser(gd))], P.floor()
    with gate_env(**dict(bare, EXEC_GATE_PORTCULLIS_DIR="/srv/pc/eph", EXEC_GATE_PORTCULLIS_WRAPPER="/srv/pc/b/w",
                         EXEC_GATE_PORTCULLIS_HOOK="/srv/pc/b/h")):
        assert P.floor()["terms"][1:] == ["pc"], P.floor()
    # a misconfigured portcullis still builds its floor (non-judged turns run as before); the judged turn is refused
    with gate_env(EXEC_GATE="portcullis", EXEC_GATE_PROTECTED_TERMS="gatehome"):
        assert P.floor()["terms"] == [r"authori[sz]ed\.md", "gatehome"], P.floor()
        with repo() as (d, wt):
            try:
                H.gate_policy("general", wt, nonce="N")
                raise AssertionError("misconfigured portcullis built a general policy")
            except H.HermesRefused:
                pass
    ok("N1: portcullis protects its own home dir (deepest shared dir of gate dir / wrapper / hook, dots "
       "stripped; else the gate dir path) with or without EXEC_GATE_PROTECTED_TERMS, which is purely additive and "
       "deduped; a misconfig keeps the floor and refuses the judged turn")


FAKE_WRAPPER = r'''import json, os, sys
ev = json.load(sys.stdin)
d = os.environ["PORTCULLIS_GATE_DIR"]
open(os.path.join(d, "calls.jsonl"), "a").write(json.dumps({"ev": ev, "env": dict(os.environ)}) + "\n")
import hashlib
sha = hashlib.sha256(ev["tool_input"]["command"].encode()).hexdigest()
mode = open(os.path.join(d, "fake-mode")).read().strip()
open(os.path.join(d, "decisions.jsonl"), "a").write(json.dumps(
    {"kind": "final", "outcome": mode, "reason": "4b: x" if mode == "deny" else "r", "subject_sha": sha}) + "\n")
print(json.dumps({"hookSpecificOutput": {"permissionDecision": "allow" if mode == "allow" else "deny",
                                         "permissionDecisionReason": "gate: " + mode}}))
'''


def test_portcullis_wire_payload_and_env():
    """What the wrapper receives (event + env) is what the old gate.judge() sent."""
    with repo() as (d, wt):
        gd = os.path.join(d, "gate")
        os.makedirs(gd)
        w = script(d, "wrapper.py", FAKE_WRAPPER)
        env = dict(SITE, EXEC_GATE_PORTCULLIS_WRAPPER=w, EXEC_GATE_PORTCULLIS_DIR=gd)
        os.environ["BUS_ENV"] = "/x/bot-env"
        try:
            for mode in ("allow", "deny"):
                open(os.path.join(gd, "fake-mode"), "w").write(mode)
                p = _general(wt, **env)
                v = G.decide("terminal", {"command": "make test"}, p)
                assert v.allowed is (mode == "allow"), (mode, v)
                if mode == "allow":
                    assert v.reason == "allowed by Portcullis: gate: allow", v.reason
                else:
                    assert v.reason.startswith("eph-gate: DENIED by Portcullis, gate: deny | reported to ops"), v
                    assert v.sha == __import__("hashlib").sha256(b"make test").hexdigest()
            c = json.loads(open(os.path.join(gd, "calls.jsonl")).readline())
        finally:
            del os.environ["BUS_ENV"]
        assert c["ev"] == {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "make test"},
                           "cwd": wt, "session_id": "eph-N-S"}, c["ev"]
        want = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PORTCULLIS_HOOK": OLD_JUDGE["hook"],
                "PORTCULLIS_PY": OLD_JUDGE["python"], "PORTCULLIS_GATE_DIR": gd, "PORTCULLIS_HOOK_TIMEOUT": "5.0",
                "CLAUDE_PROJECT_DIR": os.path.realpath(gd), "BUS_ENV": "/x/bot-env"}
        got = {k: v for k, v in c["env"].items() if k in want or k.startswith(("LAYA_", "PORTCULLIS_", "CLAUDE_"))}
        extra = set(c["env"]) - set(want) - {"TMPDIR", "LANG", "TZ", "PWD", "SHLVL", "_", "LC_CTYPE"}
        assert got == want and not extra, (got, extra)
    ok("portcullis wire: the wrapper gets the same PreToolUse event (tool, exact input, cwd, eph-<nonce>) and "
       "the same env (PORTCULLIS_HOOK / PORTCULLIS_PY / PORTCULLIS_GATE_DIR / PORTCULLIS_HOOK_TIMEOUT / neutral CLAUDE_PROJECT_DIR + "
       "pass-through) as before; allow / deny reasons and the allow-once sha are unchanged")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} exec-gate provider tests...")
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
