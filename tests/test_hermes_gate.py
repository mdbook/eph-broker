#!/usr/bin/env python3
"""eph-gate self-tests: the hermes pre_tool_call gate (adapters/hermes_gate) and its adapter wiring.

stdlib assert only, same runner shape as test_hermes.py. The decision table runs against policies built by the
REAL adapter path (hermes.gate_policy -> cc.capability_tool_grant), over a real temp git worktree with real
symlinks. The adapter half fakes hermes AND its interpreter: the fake `python` answers the pre-flight probe by
loading the copied plugin from the private HERMES_HOME exactly as hermes does (package import + register(ctx)).
"""
import atexit, contextlib, json, os, shlex, shutil, stat, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
# The suite's own made-up site overlay + roster (tests/fixtures), never the deployment's config/site.env
# or config/provenance.json, so it runs the same here and in the public export
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
import site_env  # noqa: F401
# Gate calls with no HERMES_HOME record denials relative to the cwd: run from a scratch dir, not the checkout
_CWD = tempfile.mkdtemp(prefix="eph-gate-test-")
os.chdir(_CWD)
atexit.register(shutil.rmtree, _CWD, ignore_errors=True)

import adapters.cc as CC
from adapters.cc import CCAdapter, Worker
from adapters import hermes as H
# Tests must NEVER reach the live policy judge (real wrapper and its gate dir: its decisions.jsonl,
# allow-once, the operator ask DM). Point the judge at a path that cannot exist; a test that needs a judge builds a fake.
H.GENERAL_JUDGE.update(wrapper="/nonexistent/eph-test/gate-wrapper.sh", gate_dir="/nonexistent/eph-test/gate",
                       project_dir="/nonexistent/eph-test/gate")
from adapters import hermes_gate as PLUGIN
from adapters.hermes_gate import gate as G
from envelope import EnvelopeParser
from routing import Route

PASS = []


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


# =====================================================================================================
# fixture: a worktree with secrets + symlinks, and policies from the real adapter path
# =====================================================================================================
class Fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = os.path.realpath(self.tmp.name)
        self.d = d
        self.wt = os.path.join(d, "p", "wt")
        os.makedirs(os.path.join(self.wt, "content"))
        os.makedirs(os.path.join(self.wt, "sec"))
        os.makedirs(os.path.join(self.wt, "src", "pkg"))
        subprocess.run(["git", "init", "-q", self.wt], check=True)
        w = lambda p, s="x\n": open(os.path.join(self.wt, p), "w").write(s)
        w("f.txt"); w("content/page.md"); w("sec/.db-env", "TOKEN=nope\n"); w("src/pkg/m.py"); w("t.sh", "echo hi\n")
        w("quartz.config.ts")
        os.makedirs(os.path.join(d, "outside"))
        open(os.path.join(d, "x.env"), "w").write("S=1\n")
        open(os.path.join(d, "outside", "secret.txt"), "w").write("CANARY\n")
        os.symlink("sec/.db-env", os.path.join(self.wt, "lnk_secret"))
        os.symlink(os.path.join(d, "outside", "secret.txt"), os.path.join(self.wt, "lnk_out"))
        os.symlink("f.txt", os.path.join(self.wt, "lnk_ok"))
        os.symlink(os.path.join(d, "outside"), os.path.join(self.wt, "content", "dirlink_out"))
        self.write = G.Policy(H.gate_policy("write", self.wt, nonce="N-W"))
        self.code = G.Policy(H.gate_policy("code", self.wt, test_runner="pytest", nonce="N-C"))
        self.code_bashn = G.Policy(H.gate_policy("code", self.wt, test_runner="bash-n"))
        self.wiki = G.Policy(H.gate_policy("write", os.path.join(self.wt, "content")))
        # mr-review: cc's pure-read grant (Read/Grep/Glob, no acceptEdits, Bash denied), read tools only
        self.mr = G.Policy(H.gate_policy("mr-review", self.wt, nonce="N-R"))
        # the same mr-review policy WIDENED (edits on, Bash/Edit/Write allowed, no imported denies): rows on this
        # policy prove the READ-ONLY hard deny holds on its own, whatever the imported lists say
        self.mr_widened = G.Policy(dict(H.gate_policy("mr-review", self.wt), edits=True, deny=[],
                                        allow=["Read", "Grep", "Glob", "Edit", "Write", "Bash", "Bash(git status:*)"]))
        # the pre-narrowing broad grants and NO imported deny rules: rows on this policy prove the gate's OWN
        # hard layer (_git_hard / _shell_hard / _pytest_hard / paths) with nothing in cc.py masking it
        self.broad = G.Policy({"cwd": self.wt, "edits": True, "deny": [],
                               "allow": ["Read", "Grep", "Glob", "Bash(git add:*)", "Bash(git commit:*)",
                                         "Bash(git diff:*)", "Bash(git log:*)", "Bash(git status:*)",
                                         "Bash(pytest:*)", "Bash(python -m pytest:*)", "Bash(bash -n:*)"]})

    def close(self):
        self.tmp.cleanup()


FX = None


def fx():
    global FX
    if FX is None:
        FX = Fixture()
    return FX


def T(cmd, **kw):
    return dict(command=cmd, **kw)


# (tool, args, policy-name) that MUST be allowed / denied. Shared with the mutation check below.
def allow_table(f):
    return [
        ("terminal", T("git commit -m 'feat: legit change'"), "write"),
        ("terminal", T('git commit -m "fix #12: a/b path, F t O S"'), "write"),
        ("terminal", T("git add f.txt && git commit -m t"), "write"),
        ("terminal", T("git status --short; git log --oneline -5 HEAD~1..HEAD"), "write"),
        ("terminal", T("git diff; git diff --cached --stat; git diff HEAD"), "write"),
        ("terminal", T("python -m pytest -q"), "code"),
        ("terminal", T("pytest tests/test_x.py::test_a -k 'a and not b'"), "code"),
        ("terminal", T("bash -n t.sh"), "code_bashn"),
        ("terminal", T("git status", workdir=f.wt), "write"),
        ("read_file", {"path": "f.txt"}, "write"),
        ("read_file", {"path": os.path.join(f.wt, "src/pkg/m.py"), "offset": 1, "limit": 10}, "code"),
        ("read_file", {"path": "lnk_ok"}, "write"),
        ("read_file", {"path": "../quartz.config.ts"}, "wiki"),          # read_root = the repo, not the cwd
        ("search_files", {"pattern": "a..b", "path": "src"}, "code"),
        ("search_files", {"pattern": "*.py", "target": "files"}, "code"),
        ("write_file", {"path": "new.py", "content": "x"}, "code"),
        ("write_file", {"path": os.path.join(f.wt, "src/pkg/m.py"), "content": "y"}, "code"),
        ("patch", {"path": "f.txt", "old_string": "x", "new_string": "y"}, "write"),
        ("write_file", {"path": "page2.md", "content": "x"}, "wiki"),
        ("write_file", {"path": "cfg.json", "content": "{}"}, "code"),       # in-process linted: no program
        ("terminal", T("pytest -rA -q tests/test_x.py --deselect=tests/test_x.py::test_b,tests/test_y.py"), "code"),
        ("terminal", T("pytest -k 'x=1 or y'"), "code"),
        ("terminal", T("git status", timeout=30), "write"),
        # --- mr-review read tools: reads/searches inside the checkout ---
        ("read_file", {"path": "f.txt"}, "mr"),
        ("read_file", {"path": os.path.join(f.wt, "src/pkg/m.py"), "offset": 1, "limit": 10}, "mr"),
        ("read_file", {"path": "lnk_ok"}, "mr"),
        ("search_files", {"pattern": "a..b", "path": "src"}, "mr"),
        ("search_files", {"pattern": "*.py", "target": "files"}, "mr"),
        ("read_file", {"path": "f.txt"}, "mr_widened"),
    ] + [("terminal", T(c), "broad") for c in BROAD_ALLOW]


# broad (no imported rules) rows: allowed by the gate's own layer / denied by it alone
BROAD_ALLOW = ("git commit -am t", "git commit -mFix", "git commit -m 'uses -F and --file words'",
               "bash -n t.sh", "pytest -q", "git add f.txt",
               # commit -C/-c/-u take an attached value: the F/t/O/S scan stops there (`stable` holds a t)
               "git commit -Cstable", "git commit -uno -Cstable")
BROAD_DENY = (
    "git add --pathspec-from-file=/abs/x", "git commit -m x --pathspec-fr=/abs/x", "git add --paths=x",
    "git add --pathspec-file-nul", "git commit -aF x", "git commit -nF /etc/passwd", "git commit --fil x",
    "git commit --file=x", "git commit -t tmpl", "git commit -S", "git commit --gpg-sign",
    "git diff --no-index a b", "git diff --no-i a b", "git diff --output=o", "git log --out=o",
    "git diff -O/etc/passwd", "git diff --ext-diff", "git log -L1,2:/x/abc", "git diff /dev/null /abs",
    "git diff lnk_out", "git add ../../x.env", "git add sec/.db-env", "git diff -- ../../x.env",
    "git log --work-tree=/x", "git commit --template=x", "git commit --reuse-message=HEAD --file x",
    # additional rows from review
    "git add -f sec", "git add --force sec", "git add --for sec", "git add -nf sec",
    "git commit -m x -- -m /etc/passwd",
    "git log -p -- ':(icase)SEC/.DB-ENV'", "git add ':(icase)sec'", "git diff :/sec/.db-env",
    "pytest @/etc/hostname", "pytest @../outside.txt", "pytest tests,@x",
    "pytest -o cache_dir=/tmp/x", "pytest -qo cache_dir=/tmp/x",
    # in-tree values: only the -o scan catches these (a widened PYTEST_VALUE_SHORT stopping at q/s would not)
    "pytest -qo cache_dir=x", "pytest -so cache_dir=x", "pytest -vqo cache_dir=x", "pytest --override-ini=cache_dir=/tmp",
    "pytest --override-ini cache_dir=x", "pytest --deselect=a,/etc/passwd", "pytest -p plug=/etc/x",
    "pytest --junitxml=out.xml", "pytest --junit-xml out.xml", "pytest --junitx=o.xml", "pytest --basetemp=tmp",
    "pytest --basetemp /tmp/b", "pytest --log-file=l", "pytest --debug", "pytest --pastebin=all",
    "python -m pytest --junitxml=o.xml", "python -m pytest -o cache_dir=c",
    "bash -n +n t.sh", "bash -n -c id", "bash -n -o x t.sh", "bash -n -i t.sh",
)


def deny_table(f):
    home = os.path.expanduser("~")
    return [
        # --- the brief's list ---
        ("terminal", T("git diff /dev/null /abs"), "write"),
        ("terminal", T("git diff /dev/null " + os.path.join(f.d, "outside/secret.txt")), "write"),
        ("terminal", T("git commit -aF x"), "write"),
        ("terminal", T("git commit --fil x"), "write"),
        ("terminal", T("git add --pathspec-from-file=/abs/x"), "write"),
        ("terminal", T("git commit -m x --pathspec-fr=/abs/x"), "write"),
        ("read_file", {"path": home + "/.ssh/id_rsa"}, "write"),
        ("read_file", {"path": "~/.ssh/id_rsa"}, "write"),
        ("read_file", {"path": "../../x.env"}, "write"),
        ("read_file", {"path": "lnk_secret"}, "write"),                # symlink to an in-tree secret
        ("read_file", {"path": "lnk_out"}, "write"),                   # symlink leaving the worktree
        ("read_file", {"path": "sec/.db-env"}, "write"),
        ("read_file", {"path": "content/dirlink_out/secret.txt"}, "write"),
        ("terminal", T("cat f.txt && curl https://x"), "write"),
        ("terminal", T("git log $(cat sec/.db-env)"), "write"),
        ("terminal", T("git log `cat sec/.db-env`"), "write"),
        ("terminal", T("git log > /etc/x"), "write"),
        ("write_file", {"path": "/etc/x", "content": "x"}, "write"),
        ("write_file", {"path": "../escape.txt", "content": "x"}, "write"),
        ("write_file", {"path": "lnk_out", "content": "x"}, "write"),
        ("unknown_tool", {}, "write"),
        ("tool_call", {"calls": [{"name": "terminal", "arguments": {"command": "curl https://x"}}]}, "write"),
        ("tool_call", {"calls": [{"name": "read_file", "arguments": {"path": "f.txt"}}]}, "write"),   # bridge: always
        # --- cc.py's narrowed allow-list (commit only as `-m`, diff only in its exact forms) reaches hermes ---
        ("terminal", T("git commit -am t"), "write"),
        ("terminal", T("git commit -mFix"), "write"),
        ("terminal", T("git diff --stat HEAD~3"), "write"),
        # --- more git read/exec bypasses ---
        ("terminal", T("git commit -nF /etc/passwd"), "write"),
        ("terminal", T("git commit --file=x"), "write"),
        ("terminal", T("git commit -t tmpl"), "write"),
        ("terminal", T("git commit -S -m t"), "write"),
        ("terminal", T("git diff --no-index a b"), "write"),
        ("terminal", T("git diff --output=/tmp/o"), "write"),
        ("terminal", T("git diff --out=o"), "write"),
        ("terminal", T("git diff -O/etc/passwd"), "write"),
        ("terminal", T("git diff --ext-diff"), "write"),
        ("terminal", T("git log -L1,2:/etc/passwd"), "write"),
        ("terminal", T("git add --pathspec-file-nul x"), "write"),
        ("terminal", T("git add --paths=x"), "write"),
        ("terminal", T("git -C /tmp status"), "write"),
        ("terminal", T("git --git-dir=/x status"), "write"),
        ("terminal", T("git diff lnk_out"), "write"),
        ("terminal", T("git add ../../x.env"), "write"),
        ("terminal", T("git add sec/.db-env"), "write"),
        ("terminal", T("git push"), "write"),
        ("terminal", T("git status && git push origin main"), "write"),
        ("terminal", T("git commit -m t || rm -rf ."), "write"),
        ("terminal", T("git status | sh"), "write"),
        # --- shell constructs the lexer refuses ---
        ("terminal", T("git log; (curl x)"), "write"),
        ("terminal", T("git log & curl x"), "write"),
        ("terminal", T("git log |& cat"), "write"),
        ("terminal", T("git status\ncurl x"), "write"),
        ("terminal", T('git log "$HOME"'), "write"),
        ("terminal", T("git log $'\\x41'"), "write"),
        ("terminal", T("git add *"), "write"),
        ("terminal", T("git add {a,..}"), "write"),
        ("terminal", T("git add ~/x"), "write"),
        ("terminal", T("git add --x=~/y"), "write"),
        ("terminal", T("git log # c"), "write"),
        ("terminal", T("git log <<EOF"), "write"),
        ("terminal", T("git log 'unterminated"), "write"),
        ("terminal", T("git log \x1b[31m"), "write"),
        ("terminal", T(""), "write"),
        ("terminal", T("FOO=1 git status"), "write"),
        ("terminal", T("cat /tmp/canary-out/outside.txt"), "write"),
        # --- terminal args ---
        ("terminal", T("git status", background=True), "write"),
        ("terminal", T("git status", pty=True), "write"),
        ("terminal", T("git status", background="false"), "write"),    # hermes coerces AFTER the hook
        ("terminal", T("git status", pty="false"), "write"),
        ("terminal", T("git status", timeout="42"), "write"),
        # --- B1: hermes would start a shell linter (node / npx tsc / go vet / rustfmt) after these writes ---
        ("write_file", {"path": "x.ts", "content": "x"}, "code"),
        ("write_file", {"path": "x.JS", "content": "x"}, "code"),
        ("write_file", {"path": "content/x.go", "content": "x"}, "write"),
        ("patch", {"path": "quartz.config.ts", "old_string": "x", "new_string": "y"}, "code"),
        ("write_file", {"path": "lib.rs", "content": "x"}, "wiki"),
        ("terminal", T("git status", workdir="/tmp"), "write"),
        ("terminal", T("git status", env={"A": "1"}), "write"),
        # --- capability scope ---
        ("terminal", T("python -m pytest -q"), "write"),               # write has no test runner
        ("terminal", T("bash -n +n t.sh"), "code_bashn"),              # +n turns -n back into execution
        ("terminal", T("bash -n -c 'id'"), "code_bashn"),
        ("terminal", T("pytest --rootdir=/tmp"), "code"),
        ("terminal", T("pytest -c /etc/x.ini"), "code"),
        ("terminal", T("pytest ../../other"), "code"),
        ("terminal", T("mcp.sh hindsight tools/list"), "code"),
        ("write_file", {"path": ".git/hooks/pre-commit", "content": "id"}, "code"),
        ("write_file", {"path": ".git/config", "content": "x"}, "code"),
        ("write_file", {"path": "x.txt", "content": "x", "cross_profile": True}, "code"),
        ("write_file", {"path": "../quartz.config.ts", "content": "x"}, "wiki"),   # edits confined to the cwd
        ("write_file", {"path": ".new-env", "content": "x"}, "code"),
        ("patch", {"mode": "patch", "patch": "*** Begin Patch\n*** Update File: /etc/x\n"}, "write"),
        ("search_files", {"pattern": "TOKEN"}, "write"),               # tree holds sec/.db-env + symlinks out
        ("search_files", {"pattern": "x", "path": "content"}, "write"),   # content/dirlink_out leaves the tree
        ("search_files", {"pattern": "x", "path": "/etc"}, "write"),
        ("search_files", {"pattern": "../../*", "target": "files"}, "write"),
        ("search_files", {"pattern": "x", "target": "bogus"}, "write"),
        ("execute_code", {"code": "print(1)"}, "code"),
        ("delegate_task", {}, "code"),
        ("todo_list", {}, "code"),
        ("process_manage", {}, "code"),
        # --- mr-review: READ-ONLY (no writes, no terminal, reads confined, secret floor) ---
        ("write_file", {"path": "f.txt", "content": "x"}, "mr"),
        ("write_file", {"path": "new.md", "content": "x"}, "mr"),
        ("patch", {"path": "f.txt", "old_string": "x", "new_string": "y"}, "mr"),
        ("terminal", T("git status"), "mr"),
        ("terminal", T("git diff"), "mr"),
        ("terminal", T("cat f.txt"), "mr"),
        ("read_file", {"path": os.path.join(f.d, "outside/secret.txt")}, "mr"),
        ("read_file", {"path": "../../x.env"}, "mr"),
        ("read_file", {"path": "sec/.db-env"}, "mr"),
        ("read_file", {"path": "lnk_secret"}, "mr"),
        ("read_file", {"path": "lnk_out"}, "mr"),
        ("read_file", {"path": home + "/.ssh/id_rsa"}, "mr"),
        ("search_files", {"pattern": "TOKEN"}, "mr"),                  # tree holds sec/.db-env + symlinks out
        ("search_files", {"pattern": "x", "path": "/etc"}, "mr"),
        ("search_files", {"pattern": "../../*", "target": "files"}, "mr"),
        ("todo_list", {}, "mr"),
        ("execute_code", {"code": "print(1)"}, "mr"),
        ("tool_call", {"calls": [{"name": "write_file", "arguments": {"path": "f.txt"}}]}, "mr"),
        # the hard READ-ONLY deny alone (edits on + Bash/Edit/Write allowed in the widened policy)
        ("write_file", {"path": "f.txt", "content": "x"}, "mr_widened"),
        ("patch", {"path": "f.txt", "old_string": "x", "new_string": "y"}, "mr_widened"),
        ("terminal", T("git status"), "mr_widened"),
    ] + [("terminal", T(c), "broad") for c in BROAD_DENY]


def run_tables(f):
    """Raise AssertionError on the first row whose verdict is wrong (the mutation check relies on this)."""
    pol = {"write": f.write, "code": f.code, "code_bashn": f.code_bashn, "wiki": f.wiki, "broad": f.broad,
           "mr": f.mr, "mr_widened": f.mr_widened}
    old = os.getcwd()
    os.chdir(f.d)                    # relative paths must resolve against the POLICY cwd, not the process cwd
    try:
        for tool, args, p in allow_table(f):
            v = G.decide(tool, args, pol[p])
            assert v.allowed, ("must ALLOW", tool, args, p, v)
        for tool, args, p in deny_table(f):
            v = G.decide(tool, args, pol[p])
            assert not v.allowed and "DENIED" in v.reason, ("must DENY", tool, args, p, v)
    finally:
        os.chdir(old)
    return len(allow_table(f)), len(deny_table(f))


# =====================================================================================================
def test_decision_table():
    f = fx()
    na, nd = run_tables(f)
    ok(f"eph-gate: decision table - {na} allow rows + {nd} deny rows over write/code/code(bash-n)/wiki/"
       "mr-review policies built from cc.capability_tool_grant + a broad no-imported-rules policy")


def test_hard_git_denies_without_imported_rules():
    """The gate's OWN denies hold with a broad allow-list and an EMPTY imported deny-list (the pre-narrowing
    `git commit:*` / `git diff:*` grants), so they don't depend on cc.py's rule set being complete. The same rows
    are in the decision table (policy "broad"), so the mutation check proves each hard-deny function."""
    f = fx()
    for c in BROAD_ALLOW:
        assert G.decide("terminal", T(c), f.broad).allowed, c
    for c in BROAD_DENY:
        v = G.decide("terminal", T(c), f.broad)
        assert not v.allowed and "not on the allow-list" not in v.reason, (c, v)
    ok(f"eph-gate: {len(BROAD_DENY)} hard denies (git file options incl. prefix abbreviations, -F/-t/-S/-O, "
       "add -f, `--` then -m, pathspec magic, @files, pytest writers/-o, bash -n escapes, out-of-tree/secret "
       "paths) hold with a broad allow-list and NO imported deny rules")


def test_shell_lint_live_table():
    """The write deny follows hermes' LIVE shell-linter table when that module is loaded (the plugin empties
    it), and the static SHELL_LINT_EXTS otherwise."""
    import types
    f = fx()
    assert G.LINT_MODULE not in sys.modules
    assert not G.decide("write_file", {"path": "x.ts", "content": "x"}, f.code).allowed
    pkg, mod = types.ModuleType("tools"), types.ModuleType(G.LINT_MODULE)
    mod.LINTERS = {".py": "python -m py_compile {file}", ".ts": "npx tsc"}
    mod.LINTERS_INPROC = {".py": None}
    saved = {k: sys.modules.get(k) for k in ("tools", G.LINT_MODULE, "hermes_cli")}
    sys.modules.update({"tools": pkg, G.LINT_MODULE: mod})
    try:
        assert not G.decide("write_file", {"path": "x.ts", "content": "x"}, f.code).allowed
        assert G.decide("write_file", {"path": "x.go", "content": "x"}, f.code).allowed      # not in the live table
        assert G.decide("write_file", {"path": "x.py", "content": "x"}, f.code).allowed      # in-process wins
        assert not PLUGIN.neutralise_shell_lint()          # outside hermes (no hermes_cli): untouched
        assert mod.LINTERS
        sys.modules["hermes_cli"] = types.ModuleType("hermes_cli")
        assert PLUGIN.neutralise_shell_lint() and mod.LINTERS == {}
        for p in ("x.ts", "x.go", "x.JS", "lib.rs"):
            assert G.decide("write_file", {"path": p, "content": "x"}, f.code).allowed, p
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    ok("eph-gate: B1 shell-lint write deny follows the live hermes LINTERS table (plugin empties it inside "
       "hermes only); static .js/.ts/.go/.rs deny without it")


def test_register_neutralises_shell_lint():
    """register() itself must empty hermes' shell-linter table (the probe's `linters: []` is
    only a check; the fake hermes doesn't model it). Inside hermes (hermes_cli loaded) with a live table, after
    register the table is empty and a .ts write is allowed, not left to the static deny."""
    import types
    f = fx()
    pkg, mod = types.ModuleType("tools"), types.ModuleType(G.LINT_MODULE)
    mod.LINTERS = {".ts": "npx tsc", ".go": "go vet {file}"}
    mod.LINTERS_INPROC = {}
    saved = {k: sys.modules.get(k) for k in ("tools", G.LINT_MODULE, "hermes_cli")}
    sys.modules.update({"tools": pkg, G.LINT_MODULE: mod, "hermes_cli": types.ModuleType("hermes_cli")})
    try:
        with tempfile.TemporaryDirectory() as home, _env(HERMES_HOME=home):
            json.dump(H.gate_policy("code", f.wt, test_runner="pytest", nonce="L"),
                      open(os.path.join(home, PLUGIN.POLICY_FILE), "w"))

            class Ctx:
                def register_hook(self, name, cb):
                    pass
            PLUGIN.register(Ctx())
        assert mod.LINTERS == {}, mod.LINTERS
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    ok("eph-gate: register() empties the live hermes shell-linter table (neutralise_shell_lint is called)")


def test_modify_pins_resolved_paths():
    f = fx()
    v = G.decide("read_file", {"path": "lnk_ok"}, f.write)
    assert v.allowed and v.modified_args == {"path": os.path.join(f.wt, "f.txt")}, v
    v = G.decide("terminal", T("git status"), f.wiki)
    assert v.allowed and v.modified_args == {"workdir": os.path.join(f.wt, "content")}, v
    v = G.decide("write_file", {"path": "sub/new.md", "content": "x"}, f.wiki)
    assert v.allowed and v.modified_args == {"path": os.path.join(f.wt, "content/sub/new.md")}, v
    ok("eph-gate: an allowed call is pinned to the resolved absolute path / the policy cwd (modify)")


def test_rule_matching_semantics():
    m = G.bash_rule_matches
    assert m("git add:*", ["git", "add"], True) and m("git add:*", ["git", "add", "x"], True)
    assert not m("git add:*", ["git", "addx"], True) and not m("git add:*", ["git"], True)
    assert m("git status", ["git", "status"], True) and not m("git status", ["git", "status", "-s"], True)
    # mid-string * (the row-backends deny `Bash(git * --paths*)`)
    assert m("git * --paths*", ["git", "add", "--pathspec-from-file=/x"], False)
    assert m("git * --paths*", ["git", "commit", "-m", "x", "--pathspec-fr=/x"], False)
    assert not m("git * --paths*", ["git", "add", "x"], False)
    # an ALLOW wildcard never matches through a quoted space; a DENY wildcard does
    assert not m("npm run *", ["npm", "run", "a b"], True) and m("npm run *", ["npm", "run", "ab"], True)
    assert m("git * --paths*", ["git", "commit", "-m", "a --paths=x"], False)
    assert m(None, ["anything"], False)
    assert G.split_rules("Read Bash(git add:*) Bash(python -m pytest:*) WebFetch") == \
        ["Read", "Bash(git add:*)", "Bash(python -m pytest:*)", "WebFetch"]
    # deny beats allow; a policy with that wildcard deny + the brief's two pathspec commands
    f = fx()
    base = H.gate_policy("write", f.wt)
    p = G.Policy(dict(base, allow=base["allow"] + ["Bash(git push:*)"], deny=base["deny"] + ["Bash(git * --paths*)"]))
    assert not G.decide("terminal", T("git push"), p).allowed
    orig = G.GIT_DENY_LONG
    G.GIT_DENY_LONG = tuple(x for x in orig if not x.startswith("pathspec"))   # imported rule alone must hold
    try:
        for c in ("git add --pathspec-from-file=/abs/x", "git commit -m x --pathspec-fr=/abs/x"):
            v = G.decide("terminal", T(c), p)
            assert not v.allowed and "--paths" in v.reason, v
    finally:
        G.GIT_DENY_LONG = orig
    ok("eph-gate: CC rule semantics - :* word-prefix, exact, mid-string * (deny `git * --paths*`), "
       "deny beats allow, paren-aware rule split")


def test_lexer():
    S = G.split_command
    assert S("git add a && git commit -m 'x y' ; git status\n") == [["git", "add", "a"], ["git", "commit", "-m", "x y"],
                                                                  ["git", "status"]]
    assert S('git commit -m "a \\"q\\" b"') == [["git", "commit", "-m", 'a "q" b']]
    assert S("git log a\\ b") == [["git", "log", "a b"]]
    assert S("git log \\\n --oneline") == [["git", "log", "--oneline"]]
    assert S("git log HEAD~1") == [["git", "log", "HEAD~1"]]
    assert S("git status | git log || git diff") == [["git", "status"], ["git", "log"], ["git", "diff"]]
    for bad in ("a $(b)", "a `b`", "a > b", "a < b", "a >> b", "a 2>&1", "a &> b", "a & b", "(a)", "a <(b)", "a $X",
                "a \"$X\"", "a \"`b`\"", "a *", "a ?", "a [x]", "a {x,y}", "~/x", "a ~", "a #c", "'a", '"a', "a\\",
                "a\rb", "a\x00", "   ", "a |& b"):
        try:
            S(bad)
            raise AssertionError(f"lexer must refuse {bad!r}")
        except G.Deny:
            pass
    ok("eph-gate: lexer splits ; && || | newline, honours quotes/escapes, refuses $ ` ( ) < > & globs ~ # ctl")


def test_plugin_hook_and_register():
    f = fx()
    with tempfile.TemporaryDirectory() as home:
        json.dump(H.gate_policy("write", f.wt, nonce="NONCE-1"), open(os.path.join(home, PLUGIN.POLICY_FILE), "w"))
        hooks = []

        class Ctx:
            def register_hook(self, name, cb):
                hooks.append((name, cb))
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        try:
            PLUGIN.register(Ctx())
        finally:
            if old is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old
        assert [n for n, _ in hooks] == ["pre_tool_call"], hooks
        assert json.load(open(os.path.join(home, PLUGIN.SENTINEL_FILE)))["nonce"] == "NONCE-1"
        hook = hooks[0][1]
        assert hook(tool_name=G.PROBE_TOOL, args={}) == {"action": "block", "message": "eph-gate probe ok NONCE-1"}
        assert hook(tool_name="terminal", args={"command": "curl x"})["action"] == "block"
        assert hook(tool_name="terminal", args={"command": "git status"}) == \
            {"action": "modify", "args": {"workdir": f.wt}}
        assert hook(tool_name="nope", args=None, task_id="t")["action"] == "block"
        # garbled / missing policy: the hook still registers and blocks EVERYTHING
        open(os.path.join(home, PLUGIN.POLICY_FILE), "w").write("{not json")
        assert PLUGIN.load_policy(home) is None
        dead = PLUGIN.make_hook(None)
        assert dead(tool_name="terminal", args={"command": "git status"})["action"] == "block"
        assert dead(tool_name="read_file", args={"path": "f.txt"})["action"] == "block"
        # an exception inside decide is a block, never a raise (hermes fails OPEN on a raising hook)
        orig = G.decide
        G.decide = lambda *a: 1 / 0
        try:
            with _env(HERMES_HOME=home):   # the denial record goes to the test home, not the cwd
                r = PLUGIN.make_hook(G.Policy(H.gate_policy("write", f.wt)))(tool_name="terminal", args={})
        finally:
            G.decide = orig
        assert r["action"] == "block", r
        assert not G._decide("read_file", {"path": "f.txt"}, None).allowed
    ok("eph-gate: plugin registers ONE pre_tool_call hook, writes the nonce sentinel, answers the probe; "
       "block/modify/None mapping; no/garbled policy or an internal error blocks everything")


def test_mutation_removing_deny_fails_tables():
    """Mutation check: if the gate's deny collapses to allow, the decision table MUST fail."""
    f = fx()
    names = ("_deny", "check_segment", "_check_read_path", "_git_hard", "_shell_hard", "_pytest_hard",
             "_check_write_path", "_walk_clean", "_shell_linted", "_check_args_paths")
    mutants = (("_deny -> allow", "_deny", lambda r: G.Verdict(True, r)),
               ("check_segment -> None", "check_segment", lambda *a: None),
               ("_check_read_path -> allow", "_check_read_path", lambda pol, raw, base=None: (raw, None)),
               ("_git_hard -> no-op", "_git_hard", lambda argv: (None, set())),
               ("_shell_hard -> None", "_shell_hard", lambda argv: None),
               ("_pytest_hard -> None", "_pytest_hard", lambda argv: None),
               ("_check_write_path -> allow", "_check_write_path", lambda pol, raw: (raw, None)),
               ("_walk_clean -> None", "_walk_clean", lambda pol, root: None),
               ("_shell_linted -> False", "_shell_linted", lambda path: False),
               ("_check_args_paths -> None", "_check_args_paths", lambda *a: None))
    mutants += (("PYTEST_VALUE_SHORT += q,s,v", "PYTEST_VALUE_SHORT", G.PYTEST_VALUE_SHORT | set("qsv")),
                ("READ_ONLY_DENIED_TOOLS -> ()", "READ_ONLY_DENIED_TOOLS", ()))
    names += ("PYTEST_VALUE_SHORT", "READ_ONLY_DENIED_TOOLS")
    for label, name, fn in mutants:
        patch = (lambda n=name, v=fn: setattr(G, n, v))
        saved = {k: getattr(G, k) for k in names}
        patch()
        try:
            try:
                run_tables(f)
                caught = False
            except AssertionError:
                caught = True
        finally:
            for k, v in saved.items():
                setattr(G, k, v)
        assert caught, f"mutation {label!r} went unnoticed by the decision table"
    # hook-level mutation: a hook that no longer blocks must fail the probe contract
    hook = PLUGIN.make_hook(G.Policy(H.gate_policy("write", f.wt, nonce="Z")))
    assert hook(tool_name="terminal", args={"command": "curl x"})["action"] == "block"
    orig = G.decide
    G.decide = lambda *a: G.Verdict(True, "x")
    try:
        assert hook(tool_name="terminal", args={"command": "curl x"}) is None   # what the probe's floor check catches
    finally:
        G.decide = orig
    run_tables(f)                                        # restored: green again
    ok(f"eph-gate: mutation check - each of {len(mutants)} mutants (deny collapse, every hard-deny function "
       "no-op'd incl. _git_hard, a widened PYTEST_VALUE_SHORT, the mr-review READ-ONLY deny emptied) makes the "
       "decision table fail")


# =====================================================================================================
# adapter wiring (hermes faked: fake hermes CLI + fake interpreter answering the probe)
# =====================================================================================================
_LOAD_PLUGIN = r'''
import importlib.util, json, os, sys
def load_gate(home):
    cfg = json.load(open(os.path.join(home, "config.yaml")))
    if "eph-gate" not in (cfg.get("plugins") or {}).get("enabled", []):
        return []
    d = os.path.join(home, "plugins", "eph-gate")
    spec = importlib.util.spec_from_file_location("eph_gate_plugin", os.path.join(d, "__init__.py"),
                                                  submodule_search_locations=[d])
    m = importlib.util.module_from_spec(spec); sys.modules[spec.name] = m; spec.loader.exec_module(m)
    hooks = []
    class Ctx:
        def register_hook(self, name, cb): hooks.append(cb)
    m.register(Ctx())
    return hooks
def directive(hooks, tool, args):
    for cb in hooks:
        r = cb(tool_name=tool, args=args)
        if isinstance(r, dict) and r.get("action") == "block":
            return ["block", r["message"]]
    return [None, None]
'''

FAKE_PY = "#!/usr/bin/env python3\n" + _LOAD_PLUGIN + r"""
import stat
mode = os.environ.get("FAKE_PROBE", "")
rec = os.environ.get("FAKE_PROBE_RECORD")
def _devnull():
    try:
        st = os.fstat(0)
        return stat.S_ISCHR(st.st_mode) and st.st_rdev == os.stat("/dev/null").st_rdev
    except OSError:
        return False
code = sys.argv[sys.argv.index("-c") + 1]
kind = "pathcheck" if sys.argv[-1] == "eph-path-check" else "probe"
if rec:
    open(rec, "a").write(json.dumps({"kind": kind, "argv": sys.argv[1:], "cwd": os.getcwd(),
                                     "stdin_devnull": _devnull(),
                                     "pythonpath": os.environ.get("PYTHONPATH")}) + "\n")
# the REAL interpreter-path guard (the head of the -c code, up to its `del` line) runs here too; mode badpath
# models an editable install's relative placeholder entry
_guard_end = "del _eph_bad, _eph_mod, _eph_cwd, _eph_under_cwd\n"
if mode == "badpath":
    sys.path.append("__editable__.hermes_agent-0.21.5.finder.__path_hook__")
exec(code[:code.index(_guard_end) + len(_guard_end)] if kind == "probe" else code)
if kind == "pathcheck":
    sys.exit(0)
# the real probe is `python -P -c <code> <run argv json> <tool>`
flags = sys.argv[1:sys.argv.index("-c")]
run_argv, tool = json.loads(sys.argv[-2]), sys.argv[-1]
home = orig_home = os.environ["HERMES_HOME"]
# stand-in for hermes' startup prelude: <HERMES_HOME>/.env lines land in os.environ (override), then the
# profile override re-homes on <root>/active_profile (a custom HERMES_HOME is its own root), and the scratch env
if os.path.isfile(os.path.join(home, ".env")):
    for line in open(os.path.join(home, ".env")):
        if "=" in line:
            k, v = line.strip().split("=", 1); os.environ[k] = v
if os.path.isfile(os.path.join(home, "active_profile")):
    home = os.environ["HERMES_HOME"] = os.path.join(home, "profiles", open(os.path.join(home, "active_profile")).read().strip())
os.environ["HERMES_SCRATCH_DIR"] = os.path.join(home, "cache", "scratch")
if mode == "otherhome":
    os.environ["HERMES_HOME"] = home = home + "-other"
if mode == "scratchout":
    os.environ["HERMES_SCRATCH_DIR"] = orig_home + "-scratch"
if mode == "extraenv":
    os.environ["HERMES_BUNDLED_PLUGINS"] = "/x"
# the gate still loads from the ORIGINAL home here, so only the home check can catch a re-home
_now, os.environ["HERMES_HOME"] = os.environ["HERMES_HOME"], orig_home
hooks = [] if mode == "unloaded" else load_gate(orig_home)
os.environ["HERMES_HOME"] = _now
out = {"probe": directive(hooks, tool, {}),
       "floor": directive(hooks, "terminal", {"command": "git push https://example.invalid HEAD"}),
       "env": {k: v for k, v in os.environ.items() if k.startswith("HERMES_")},
       "home": os.environ.get("HERMES_HOME"), "home_resolved": home + ("-x" if mode == "resolvedother" else ""),
       "container": mode == "container" or os.path.exists(os.path.join(home, ".container-mode")),
       "install_env": mode == "installenv", "linters": [".ts"] if mode == "lint" else [], "lsp": mode == "lsp",
       "run_argv": run_argv, "flags": flags}
if mode == "startup":
    out["startup"] = "SystemExit(1)"
if mode == "wrongnonce":
    out["probe"][1] = (out["probe"][1] or "") + "-stale"
if mode == "crash":
    sys.exit(3)
print("EPH_GATE_PROBE " + json.dumps(out))
"""

FAKE_HERMES = "#!/usr/bin/env python3\n" + _LOAD_PLUGIN + r'''
home = os.environ["HERMES_HOME"]
prompt = sys.stdin.read()
hooks = [] if os.environ.get("FAKE_NO_GATE") else load_gate(home)
pol = os.path.join(home, "eph-gate-policy.json")
rec = os.environ.get("FAKE_RECORD")
if rec:
    open(rec, "a").write(json.dumps({
        "argv": sys.argv[1:], "config": json.load(open(os.path.join(home, "config.yaml"))),
        "policy": json.load(open(pol)) if os.path.exists(pol) else None,
        "plugin_files": sorted(os.listdir(os.path.join(home, "plugins", "eph-gate")))
        if os.path.isdir(os.path.join(home, "plugins", "eph-gate")) else None,
        "terminal_cwd": os.environ.get("TERMINAL_CWD"), "cwd": os.getcwd(),
        "hermes_env": sorted(k for k in os.environ if k.startswith("HERMES_")),
        "tirith_env": sorted(k for k in os.environ if k.startswith("TIRITH_")),
        "pythonpath": os.environ.get("PYTHONPATH"), "home_ls": sorted(os.listdir(home)),
        "home_dotenv": [n for n in (".env", ".op.env") if os.path.lexists(os.path.join(home, n))],
        "curl_verdict": directive(hooks, "terminal", {"command": "curl x"})}) + "\n")
text = json.dumps({"status": os.environ.get("FAKE_STATUS", "done"), "summary": "<p>ok</p>", "wake": []})
print(json.dumps({"type": "result", "session_id": "sess-g1", "exit_code": 0, "text": text,
                  "tokens": {"input": 1, "output": 1}}))
'''


def _exe(path, body):
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class _Registry:
    def __init__(self, d):
        self.path = os.path.join(d, "spawn-models.json")
        envf = os.path.join(d, ".fakebe-env")
        open(envf, "w").write("ANTHROPIC_AUTH_TOKEN=tok-test\n")
        json.dump({"backends": {"fakebe": {"kind": "cloud", "model": "fake-model", "base_url": "https://llm.example/",
                                           "env": envf}}}, open(self.path, "w"))

    def __enter__(self):
        self.old = CC.SPAWN_MODELS_JSON; CC.SPAWN_MODELS_JSON = self.path; return self

    def __exit__(self, *a):
        CC.SPAWN_MODELS_JSON = self.old


@contextlib.contextmanager
def _env(**kv):
    old = {k: os.environ.get(k) for k in kv}
    for k, v in kv.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _stdin_pipe():
    """fd 0 = a pipe for the duration (a child that INHERITS stdin then sees a pipe, not /dev/null)."""
    r, w = os.pipe()
    saved = os.dup(0)
    os.dup2(r, 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        for fd in (saved, r, w):
            os.close(fd)


def _setup(d):
    bindir = os.path.join(d, "bin"); os.makedirs(bindir)
    hb = _exe(os.path.join(bindir, "hermes"), FAKE_HERMES)
    _exe(os.path.join(bindir, "python"), FAKE_PY)          # shebang has a space, so the sibling python is used
    alerts = []
    a = CCAdapter(router=None, worktree_mgr=None, prompt_loader=None, envelope_parser=EnvelopeParser(),
                  hermes=H.HermesHarness(hermes_bin=hb, home_root=os.path.join(d, "homes")),
                  alert_ops=lambda m, subject=None: alerts.append(m))
    wt = os.path.join(d, "wt"); os.makedirs(wt)
    subprocess.run(["git", "init", "-q", wt], check=True)
    return a, wt, alerts


def _r(capability, **env):
    return Route(domain="x", repo="/srv/x", prompt_class="agent", agent="coder-agent", env=env, capability=capability,
                 backend="fakebe", adapter="hermes")


def _recs(p):
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def test_adapter_gated_write_turn():
    with tempfile.TemporaryDirectory() as d, _Registry(d), _env(CODE_NONCLAUDE_OK=None):
        a, wt, alerts = _setup(d)
        rec, prec = os.path.join(d, "rec.jsonl"), os.path.join(d, "probe.jsonl")
        w = Worker("vikunja:task:1", "run-g", wt)
        with _stdin_pipe():             # the probe must get /dev/null, not the broker's stdin (N2-2)
            env = a._one_turn(w, _r("write", FAKE_RECORD=rec, FAKE_PROBE_RECORD=prec), "SYS", "P", resume=False)
        assert env and env["status"] == "done", (env, alerts)
        (r,) = _recs(rec)
        argv = r["argv"]
        assert argv[argv.index("-t") + 1] == "file,terminal", argv
        assert "--ignore-rules" in argv, argv          # the ONE flag keeping worktree AGENTS.md/SOUL.md out
        cfg = r["config"]
        assert cfg["plugins"]["enabled"] == ["eph-gate"] and cfg["tools"]["tool_search"]["enabled"] == "off", cfg
        assert cfg["plugins"]["load_timeout_seconds"] == 0, cfg["plugins"]      # B2: inline load, no deadline
        assert cfg["lsp"]["enabled"] is False, cfg.get("lsp")                    # B1: no language servers
        assert cfg["security"] == {"tirith_enabled": False, "allow_lazy_installs": False}, cfg.get("security")
        # No tirith exec, and no lazy-install uv/pip reading a worktree uv.toml
        assert not {"file", "terminal"} & set(cfg["agent"]["disabled_toolsets"]), cfg["agent"]
        assert {"code_execution", "delegation", "browser", "web", "memory"} <= set(cfg["agent"]["disabled_toolsets"])
        assert cfg["terminal"] == {"backend": "local", "cwd": os.path.realpath(wt), "auto_source_bashrc": False,
                                   "shell_init_files": []}, cfg.get("terminal")
        assert r["terminal_cwd"] == os.path.realpath(wt) and r["cwd"] == os.path.realpath(wt), r
        pol = r["policy"]
        grant = CC.capability_tool_grant("write")
        assert pol["allow"] == G.split_rules(grant[grant.index("--allowedTools") + 1]), pol["allow"]
        assert pol["deny"] == G.split_rules(grant[grant.index("--disallowedTools") + 1]), pol["deny"]
        assert pol["secret_globs"] == list(CC._SECRET_FILE_GLOBS) and pol["edits"] is True, pol
        assert pol["cwd"] == pol["write_root"] == pol["read_root"] == os.path.realpath(wt), pol
        assert "__init__.py" in r["plugin_files"] and "gate.py" in r["plugin_files"], r["plugin_files"]
        assert r["curl_verdict"][0] == "block", "the plugin the run loaded must block a floored command"
        (p,) = _recs(prec)
        # `-P` BEFORE `-c` (no cwd on sys.path), the run's own argv, the worktree cwd
        assert p["argv"][:2] == ["-P", "-c"] and p["argv"][4] == G.PROBE_TOOL and len(p["argv"]) == 5, p
        assert json.loads(p["argv"][3])[1:] == argv and os.path.isabs(json.loads(p["argv"][3])[0]), p
        assert p["cwd"] == os.path.realpath(wt) and p["pythonpath"] is None and p["stdin_devnull"] is True, p
        assert p["kind"] == "probe", p           # gated: the path guard rides IN the probe, no separate check
        assert r["hermes_env"] == ["HERMES_HOME"] and r["pythonpath"] is None, r
        assert not alerts, alerts
    ok("eph-gate: a write turn runs `-t file,terminal` with eph-gate enabled by name, tool_search off, "
       "bashrc auto-source off, a per-turn policy = cc's write grant, a passing pre-flight probe, the sentinel")


def test_adapter_policy_imports_not_copies():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a, wt, alerts = _setup(d)
        rec = os.path.join(d, "rec.jsonl")
        orig = list(CC._WRITE_DENIED)
        CC._WRITE_DENIED.append("Bash(git * --paths*)")      # what the row-backends branch adds
        try:
            a._one_turn(Worker("k", "r", wt), _r("write", FAKE_RECORD=rec), "SYS", "P", resume=False)
        finally:
            CC._WRITE_DENIED[:] = orig
        assert "Bash(git * --paths*)" in _recs(rec)[0]["policy"]["deny"]
    ok("eph-gate: the policy is DERIVED from cc.py at turn time - a tightened cc deny-list reaches hermes")


def test_adapter_gate_not_loaded_refuses():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a, wt, alerts = _setup(d)
        for mode in ("unloaded", "wrongnonce", "crash"):
            rec = os.path.join(d, f"rec-{mode}.jsonl")
            try:
                a._one_turn(Worker("k", "r", wt), _r("write", FAKE_RECORD=rec, FAKE_PROBE=mode), "SYS", "P",
                            resume=False)
                raise AssertionError(f"probe mode {mode} must refuse")
            except H.HermesRefused as e:
                assert "eph-gate" in str(e), e
            assert not os.path.exists(rec), f"{mode}: hermes must never launch without a proven gate"
        # plugin not enabled in config (e.g. a regression in hermes_config): the probe sees no gate and refuses
        orig = H.GATE_PLUGIN
        H.GATE_PLUGIN = "something-else"
        try:
            rec = os.path.join(d, "rec-disabled.jsonl")
            try:
                a._one_turn(Worker("k", "r", wt), _r("write", FAKE_RECORD=rec), "SYS", "P", resume=False)
                raise AssertionError("an unenabled gate must refuse")
            except H.HermesRefused:
                pass
            assert not os.path.exists(rec)
        finally:
            H.GATE_PLUGIN = orig
        # the run itself didn't load the gate: sentinel missing, so a parse miss and an alert
        rec = os.path.join(d, "rec-nogate.jsonl")
        assert a._one_turn(Worker("k", "r", wt), _r("write", FAKE_RECORD=rec, FAKE_NO_GATE="1"), "SYS", "P",
                           resume=False) is None
        assert _recs(rec) and any("not provably live" in m for m in alerts), alerts
    ok("eph-gate: gate not loaded / stale nonce / probe crash / plugin not enabled -> refused, no launch; "
       "gate absent from the real run -> result discarded + ops alert")


def test_adapter_b2_env_and_home_dotenv():
    """No HERMES_* but HERMES_HOME reaches hermes (popped even when route.env / the
    broker env carries them), the private home is swept to its allowlist before every turn (a planted .env /
    .op.env / active_profile + profiles/ / .container-mode is gone; the session store survives), and the probe
    refuses if the startup isn't as required (prelude error, extra HERMES_* after startup, another home incl. a
    profile re-home, container mode, install-dir .env, shell linters live, LSP on)."""
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a, wt, alerts = _setup(d)
        rec = os.path.join(d, "rec.jsonl")
        w = Worker("k", "r", wt)
        bad = {"HERMES_SAFE_MODE": "1", "HERMES_ENABLE_PROJECT_PLUGINS": "1", "HERMES_MANAGED_DIR": d,
               "HERMES_BUNDLED_PLUGINS": d, "HERMES_DEV": "1", "HERMES_YOLO_MODE": "1",
               "HERMES_DISABLE_FAST_CHAT_LAUNCH": "1", "HERMES_SCRATCH_DIR": d,
               "TIRITH_ENABLED": "1", "TIRITH_BIN": d}
        with _env(**bad):
            assert a._one_turn(w, _r("write", FAKE_RECORD=rec, **bad), "SYS", "P", resume=False), alerts
        # turn 2 on the SAME home, after same-uid code planted files that re-point hermes at startup
        home = w.hermes_home
        open(os.path.join(home, ".env"), "w").write("HERMES_SAFE_MODE=1\n")
        os.makedirs(os.path.join(home, ".op.env"))
        open(os.path.join(home, "active_profile"), "w").write("x\n")
        os.makedirs(os.path.join(home, "profiles", "x"))
        open(os.path.join(home, ".container-mode"), "w").write("backend=docker\n")
        os.symlink(d, os.path.join(home, "sessions"))                    # a kept NAME that is a symlink: removed
        for keep in ("state.db", "state.db-wal", "logs", "bin"):     # bin/ (a planted tirith) is NOT kept (N2-1)
            (os.makedirs if keep in ("logs", "bin") else lambda p: open(p, "w").close())(os.path.join(home, keep))
        assert a._one_turn(w, _r("write", FAKE_RECORD=rec), "SYS", "P", resume=False), alerts
        recs = _recs(rec)
        for r in recs:
            assert r["hermes_env"] == ["HERMES_HOME"] and r["home_dotenv"] == [] and r["curl_verdict"][0] == "block", r
            assert not r["tirith_env"], r["tirith_env"]
        ls = set(recs[-1]["home_ls"])
        assert not ls & {"active_profile", "profiles", ".container-mode", ".env", ".op.env", "sessions", "bin"}, ls
        assert {"state.db", "state.db-wal", "logs", "config.yaml", "plugins", PLUGIN.POLICY_FILE} <= ls, ls
        assert not os.path.islink(os.path.join(home, "sessions"))
        # the sweep is load-bearing: without it each planted file makes the probe refuse
        orig = H.HermesHarness.__dict__["_sweep_home"]
        H.HermesHarness._sweep_home = staticmethod(lambda home: None)
        try:
            for name, body, needle in ((".env", "HERMES_ENABLE_PROJECT_PLUGINS=1\n", "HERMES_ENABLE_PROJECT_PLUGINS"),
                                       ("active_profile", "x\n", "home_ok=False"),
                                       (".container-mode", "backend=docker\n", "container=True")):
                open(os.path.join(home, name), "w").write(body)
                try:
                    a._one_turn(w, _r("write", FAKE_RECORD=rec), "SYS", "P", resume=False)
                    raise AssertionError(f"a planted home {name} must refuse")
                except H.HermesRefused as e:
                    assert needle in str(e), (name, e)
                os.unlink(os.path.join(home, name))
        finally:
            H.HermesHarness._sweep_home = orig
        n = len(_recs(rec))
        for mode, needle in (("lint", "linters="), ("lsp", "lsp=True"), ("startup", "SystemExit"),
                             ("otherhome", "home_ok=False"), ("resolvedother", "home_ok=False"),
                             ("scratchout", "home_ok=False"), ("container", "container=True"),
                             ("installenv", "install_env=True"), ("extraenv", "HERMES_BUNDLED_PLUGINS")):
            try:
                a._one_turn(Worker("k", "r", wt), _r("write", FAKE_RECORD=rec, FAKE_PROBE=mode), "SYS", "P",
                            resume=False)
                raise AssertionError(f"probe mode {mode} must refuse")
            except H.HermesRefused as e:
                assert "not as required" in str(e) and needle in str(e), (mode, e)
        assert len(_recs(rec)) == n, "hermes must never launch after a refused probe"
        # mr-review is gated too: the same config (eph-gate, no deadline, LSP off) and the same sweep
        cfg = H.hermes_config("S", {}, "m", ("file",), gated=True, cwd=wt)
        assert cfg["plugins"] == {"enabled": ["eph-gate"], "load_timeout_seconds": 0} and cfg["lsp"]["enabled"] is False, cfg
        w2 = Worker("gitlab:mr:2", "r2", wt)
        assert a._one_turn(w2, _r("mr-review", FAKE_RECORD=rec, FAKE_STATUS="pass"), "SYS", "P", resume=False)
        open(os.path.join(w2.hermes_home, "active_profile"), "w").write("x\n")
        assert a._one_turn(w2, _r("mr-review", FAKE_RECORD=rec, FAKE_STATUS="pass"), "SYS", "P", resume=False)
        assert "active_profile" not in _recs(rec)[-1]["home_ls"]
    ok("eph-gate: B2/N-N1/N-N2 - every HERMES_* but HERMES_HOME popped; the home is swept to its allowlist "
       "each turn (.env/.op.env/active_profile/profiles/.container-mode/bin gone, state.db/logs kept, no symlink "
       "kept); probe refuses on prelude error / extra HERMES_* / other home incl. profile re-home / container / "
       "install .env / live shell linters / LSP on")


def test_adapter_grants():
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a, wt, alerts = _setup(d)
        rec = os.path.join(d, "rec.jsonl")
        for cap in ("inbox", "inbox-chat", "read", "tfa", ""):
            try:
                a._one_turn(Worker("k", "r", wt), _r(cap, FAKE_RECORD=rec), "SYS", "P", resume=False)
                raise AssertionError(f"must refuse {cap!r}")
            except H.HermesRefused:
                pass
        for val in (None, "", "0", "no", "2"):
            with _env(CODE_NONCLAUDE_OK=val):
                try:
                    a._one_turn(Worker("k", "r", wt), _r("code", FAKE_RECORD=rec), "SYS", "P", resume=False)
                    raise AssertionError(f"code must refuse with CODE_NONCLAUDE_OK={val!r}")
                except H.HermesRefused as e:
                    assert "CODE_NONCLAUDE_OK" in str(e), e
        assert not os.path.exists(rec), "a refused route must never launch"
        with _env(CODE_NONCLAUDE_OK="1"):
            w = Worker("k", "r", wt); w.test_runner = "pytest"
            assert a._one_turn(w, _r("code", FAKE_RECORD=rec), "SYS", "P", resume=False)
        (r,) = _recs(rec)
        assert r["argv"][r["argv"].index("-t") + 1] == "file,terminal"
        assert "Bash(python -m pytest:*)" in r["policy"]["allow"] and "Bash(mcp-ro.sh:*)" in r["policy"]["allow"]
        assert sorted(H.HERMES_GRANTS) == ["code", "general", "mr-review", "write"]
        assert H.GATED_CAPABILITIES == set(H.HERMES_GRANTS), "every honoured capability runs under eph-gate"
        assert a._one_turn(Worker("k", "r", wt), _r("general", FAKE_RECORD=rec), "SYS", "P", resume=False)
        g = _recs(rec)[-1]
        assert g["argv"][g["argv"].index("-t") + 1] == "file,terminal", "general: no web toolset"
        assert g["policy"]["judge"]["gate_dir"] == "/nonexistent/eph-test/gate"
        assert "Bash(git push:*)" in g["policy"]["deny"], "general carries the _GENERAL_DENIED floor"
    ok("eph-gate: grants - write + code (code only with CODE_NONCLAUDE_OK=1, read at call time, carrying the "
       "worker's test runner) + general (file,terminal + policy judge + _GENERAL_DENIED); "
       "inbox/inbox-chat/read/unknown refused before any launch")


def test_adapter_venv_pythonpath_terminal_only():
    """For the venv runner the worktree src/ reaches the TERMINAL shell only (hermes' local backend sources
    terminal.shell_init_files into its login snapshot), never the hermes process env or the probe, where a
    worker-written src/json.py would run before the gate loads."""
    with tempfile.TemporaryDirectory() as d, _Registry(d), _env(CODE_NONCLAUDE_OK="1", PYTHONPATH=None):
        a, wt, alerts = _setup(d)
        os.makedirs(os.path.join(wt, "src"))
        rec, prec = os.path.join(d, "rec.jsonl"), os.path.join(d, "probe.jsonl")
        w = Worker("k", "r", wt); w.test_runner = "venv"; w.repo = os.path.join(d, "base")
        assert a._one_turn(w, _r("code", FAKE_RECORD=rec, FAKE_PROBE_RECORD=prec), "SYS", "P", resume=False), alerts
        (r,), (p,) = _recs(rec), _recs(prec)
        assert r["pythonpath"] is None and p["pythonpath"] is None, (r["pythonpath"], p["pythonpath"])
        init = os.path.join(w.hermes_home, H.TERMINAL_ENV_FILE)
        assert r["config"]["terminal"]["shell_init_files"] == [init], r["config"]["terminal"]
        assert H.TERMINAL_ENV_FILE in r["home_ls"]
        assert any(x.endswith("/.venv/bin/python -m pytest:*)") for x in r["policy"]["allow"]), r["policy"]["allow"]
        got = subprocess.run(["bash", "-c", '. %s; printf %%s "$PYTHONPATH"' % shlex.quote(init)],
                             env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": "/keep"},
                             capture_output=True, text=True).stdout
        assert got == os.path.join(os.path.realpath(wt), "src") + ":/keep", got
        # no venv runner (or no src/): no init file, empty shell_init_files
        w2 = Worker("k2", "r2", wt); w2.test_runner = "pytest"
        assert a._one_turn(w2, _r("code", FAKE_RECORD=rec), "SYS", "P", resume=False)
        r2 = _recs(rec)[-1]
        assert r2["config"]["terminal"]["shell_init_files"] == [] and H.TERMINAL_ENV_FILE not in r2["home_ls"], r2
    ok("eph-gate: N-N3 venv runner - worktree src/ on PYTHONPATH via the terminal's shell init file only; "
       "the hermes process and the probe get none")


# a minimal hermes install for the probe: the modules the real probe imports, with the same top-level names a
# worktree can shadow (json via stdlib, utils, hermes_cli). hermes_cli.plugins imports `utils` like hermes does.
_STUB_HERMES = {
    "hermes_cli/__init__.py": "",
    "hermes_cli/main.py": "import json, os\nimport utils\n",
    "hermes_cli/_startup_fast.py": "import os\ndef project_root_str():\n    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n",
    "hermes_cli/config.py": "def get_container_exec_info():\n    return None\n",
    "hermes_cli/plugins.py": _LOAD_PLUGIN + "from utils import env_var_enabled\n"
        "_H = None\n"
        "def get_pre_tool_call_directive(tool, args):\n"
        "    global _H\n"
        "    if _H is None:\n"
        "        _H = load_gate(os.environ['HERMES_HOME'])\n"
        "    return tuple(directive(_H, tool, args))\n",
    "hermes_constants.py": "import os\ndef get_hermes_home():\n    return os.environ['HERMES_HOME']\n",
    "utils.py": "def env_var_enabled(name):\n    return False\n",
    "tools/__init__.py": "", "tools/file_operations_lint.py": "LINTERS = {}\n",
    "agent/__init__.py": "", "agent/lsp/__init__.py": "def get_service():\n    return None\n",
}


def _python311():
    """A real interpreter that has `-P` (>= 3.11), or None (CI's python:3.9 image: the test is skipped)."""
    cands = [H._hermes_python(H.HERMES_BIN), sys.executable, shutil.which("python3.13"), shutil.which("python3.12"),
             shutil.which("python3.11")]
    for c in cands:
        if c and os.access(c, os.X_OK):
            r = subprocess.run([c, "-c", "import sys; print(sys.version_info >= (3, 11))"], capture_output=True,
                               text=True)
            if r.stdout.strip() == "True":
                return c
    return None


def test_probe_ignores_worktree_modules():
    """The probe must not import modules from the worktree cwd. json.py / utils.py / hermes_cli/ planted in
    the worktree (what a write worker's write_file can create) are neither executed (no marker) nor able to break
    the probe (a benign top-level utils.py used to refuse every turn). Negative control: the same probe without
    `-P` does execute them. Runs the REAL probe code on a real python >= 3.11 over a stub hermes install."""
    py = _python311()
    if not py:
        ok("eph-gate: worktree-module probe test SKIPPED (no python >= 3.11 here; CI image is 3.9)")
        return
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        stub = os.path.join(d, "stub")
        for rel, body in _STUB_HERMES.items():
            os.makedirs(os.path.dirname(os.path.join(stub, rel)), exist_ok=True)
            open(os.path.join(stub, rel), "w").write(body)
        hb = _exe(os.path.join(d, "hermes"), "#!%s\n" % py)
        wt = os.path.join(d, "wt"); os.makedirs(wt)
        subprocess.run(["git", "init", "-q", wt], check=True)
        marker = os.path.join(d, "MARKER")
        plant = "open(%r, 'a').write(__name__ + '\\n')\n" % marker
        open(os.path.join(wt, "json.py"), "w").write(plant + "raise ImportError('planted json')\n")
        open(os.path.join(wt, "utils.py"), "w").write(plant + "def helper():\n    return 1\n")
        os.makedirs(os.path.join(wt, "hermes_cli"))
        open(os.path.join(wt, "hermes_cli", "__init__.py"), "w").write(plant)
        h = H.HermesHarness(hermes_bin=hb, home_root=os.path.join(d, "homes"))
        w = Worker("k", "r", wt)
        home = h._home(w)
        open(os.path.join(home, "config.yaml"), "w").write(json.dumps({"plugins": {"enabled": ["eph-gate"]}}))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d, "HERMES_HOME": home, "PYTHONPATH": stub}
        run_argv = H.build_argv(hb, "m", ["file", "terminal"], 5, 60)
        pol = H.gate_policy("write", wt, nonce="NB1")
        assert h._install_gate(home, pol, env, os.path.realpath(wt), run_argv) == "NB1"
        assert not os.path.exists(marker), open(marker).read()
        # Tripwire: an empty / relative PYTHONPATH component (python absolutises it to <cwd>[/rel]) refuses
        for pp in (stub + ":", stub + ":hermes_cli", ":" + stub):
            try:
                h._install_gate(home, pol, dict(env, PYTHONPATH=pp), os.path.realpath(wt), run_argv)
                raise AssertionError(f"PYTHONPATH={pp!r} must refuse")
            except H.HermesRefused as e:
                assert "NON-editable" in str(e), (pp, e)
            assert not os.path.exists(marker), (pp, open(marker).read())
        # negative controls. Without -P the guard sees '' (the cwd) and refuses before any import; the second line
        orig = H.probe_argv
        H.probe_argv = lambda py_, argv_: [x for x in orig(py_, argv_) if x != "-P"]
        try:
            try:
                h._install_gate(home, pol, env, os.path.realpath(wt), run_argv)
                raise AssertionError("without -P the guard must refuse")
            except H.HermesRefused as e:
                assert "NON-editable" in str(e), e
            assert not os.path.exists(marker), open(marker).read()
            # without -P AND without the guard the worktree modules DO run: both lines are load-bearing
            orig_probe, H._PROBE = H._PROBE, "import sys, os\n" + H._PROBE_BODY
            try:
                try:
                    h._install_gate(home, pol, env, os.path.realpath(wt), run_argv)
                except H.HermesRefused:
                    pass
            finally:
                H._PROBE = orig_probe
        finally:
            H.probe_argv = orig
        assert os.path.exists(marker), "negative control: without -P and the guard the planted module must have run"
    ok("eph-gate: planted json.py/utils.py/hermes_cli in the worktree: the -P probe neither runs them "
       "nor refuses; an empty/relative PYTHONPATH component refuses; without -P the guard refuses; without both "
       "they run (negative control)")


_EDITABLE_PLACEHOLDER = "__editable__.hermes_agent-0.21.5.finder.__path_hook__"


def test_probe_refuses_editable_placeholder():
    """A real venv whose site-packages carries an EDITABLE-style .pth (a relative placeholder entry on
    sys.path BEFORE the dir that really holds hermes, exactly the order the setuptools editable finder uses). The
    worktree plants `<placeholder>/hermes_cli/__init__.py` (one gate-allowed write_file). The probe (gated) and
    the standalone check (the no-probe fallback) must refuse with NO marker; negative control: without the guard the planted
    package IS imported as hermes."""
    py = _python311()
    if not py:
        ok("eph-gate: editable-placeholder test SKIPPED (no python >= 3.11 here; CI image is 3.9)")
        return
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        venv = os.path.join(d, "venv")
        subprocess.run([py, "-m", "venv", "--without-pip", venv], check=True, capture_output=True)
        vpy = os.path.join(venv, "bin", "python")
        site = subprocess.run([vpy, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                              capture_output=True, text=True, check=True).stdout.strip()
        stub = os.path.join(d, "stub")                     # the "source checkout" the editable finder serves
        for rel, body in _STUB_HERMES.items():
            os.makedirs(os.path.dirname(os.path.join(stub, rel)), exist_ok=True)
            open(os.path.join(stub, rel), "w").write(body)
        open(os.path.join(site, "__editable__.hermes_agent-0.21.5.pth"), "w").write(
            "import sys; sys.path.append(%r); sys.path.append(%r)\n" % (_EDITABLE_PLACEHOLDER, stub))
        hb = _exe(os.path.join(d, "hermes"), "#!%s\n" % vpy)
        wt = os.path.join(d, "wt"); os.makedirs(wt)
        subprocess.run(["git", "init", "-q", wt], check=True)
        marker = os.path.join(d, "MARKER")
        os.makedirs(os.path.join(wt, _EDITABLE_PLACEHOLDER, "hermes_cli"))
        open(os.path.join(wt, _EDITABLE_PLACEHOLDER, "hermes_cli", "__init__.py"), "w").write(
            "open(%r, 'a').write(__name__ + '\\n')\n" % marker)
        h = H.HermesHarness(hermes_bin=hb, home_root=os.path.join(d, "homes"))
        w = Worker("k", "r", wt)
        home = h._home(w)
        open(os.path.join(home, "config.yaml"), "w").write(json.dumps({"plugins": {"enabled": ["eph-gate"]}}))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d, "HERMES_HOME": home}
        run_argv = H.build_argv(hb, "m", ["file", "terminal"], 5, 60)
        pol = H.gate_policy("write", wt, nonce="RB1")
        rwt = os.path.realpath(wt)
        for name, call in (("probe", lambda: h._install_gate(home, pol, env, rwt, run_argv)),
                           ("check", lambda: h._check_interpreter(env, rwt))):
            try:
                call()
                raise AssertionError(f"{name}: the editable placeholder must refuse")
            except H.HermesRefused as e:
                assert "NON-editable" in str(e) and _EDITABLE_PLACEHOLDER in str(e), (name, e)
            assert not os.path.exists(marker), (name, open(marker).read())
        # control: the same venv without the planted dir still refuses (the placeholder itself is the defect)
        shutil.rmtree(os.path.join(wt, _EDITABLE_PLACEHOLDER))
        try:
            h._check_interpreter(env, rwt)
            raise AssertionError("an editable-style install must refuse even with nothing planted")
        except H.HermesRefused:
            pass
        # belts: an ABSOLUTE `__editable__` entry, and an editable finder with no relative entry at all
        pth = os.path.join(site, "__editable__.hermes_agent-0.21.5.pth")
        open(pth, "w").write("import sys; sys.path.append(%r)\n" % stub)          # clean: passes
        h._check_interpreter(env, rwt)
        os.makedirs(os.path.join(d, _EDITABLE_PLACEHOLDER))
        try:
            h._check_interpreter(dict(env, PYTHONPATH=os.path.join(d, _EDITABLE_PLACEHOLDER)), rwt)
            raise AssertionError("an absolute __editable__ sys.path entry must refuse")
        except H.HermesRefused:
            pass
        open(os.path.join(site, "__editable___hermes_agent_0_21_5_finder.py"), "w").write(
            "import sys\nclass _EditableFinder:\n    @classmethod\n    def find_spec(cls, *a, **k):\n"
            "        return None\ndef install():\n    sys.meta_path.append(_EditableFinder)\n")
        open(pth, "w").write("import __editable___hermes_agent_0_21_5_finder; "
                             "__editable___hermes_agent_0_21_5_finder.install(); import sys; sys.path.append(%r)\n"
                             % stub)
        try:
            h._check_interpreter(env, rwt)
            raise AssertionError("an __editable__ finder must refuse")
        except H.HermesRefused as e:
            assert "__editable__" in str(e), e
        open(pth, "w").write("import sys; sys.path.append(%r); sys.path.append(%r)\n" % (_EDITABLE_PLACEHOLDER, stub))
        os.makedirs(os.path.join(wt, _EDITABLE_PLACEHOLDER, "hermes_cli"))
        open(os.path.join(wt, _EDITABLE_PLACEHOLDER, "hermes_cli", "__init__.py"), "w").write(
            "open(%r, 'a').write(__name__ + '\\n')\n" % marker)
        # negative control: without the guard the planted package is imported as hermes_cli (the model is real)
        orig_probe, H._PROBE = H._PROBE, "import sys, os\n" + H._PROBE_BODY
        try:
            try:
                h._install_gate(home, pol, env, rwt, run_argv)
            except H.HermesRefused:
                pass
        finally:
            H._PROBE = orig_probe
        assert os.path.exists(marker), "negative control: without the guard the planted hermes_cli must run"
    ok("eph-gate: editable-style relative placeholder on sys.path + planted "
       "<placeholder>/hermes_cli in the worktree: probe and standalone check refuse, no marker; without the guard "
       "the planted package runs (negative control)")


def test_adapter_mr_review_gated_read_only():
    """A tool-less (`-t todo`) mr-review reviewer could not read the checkout and blocked
    every MR. mr-review now runs `-t file` (no terminal) under eph-gate with cc's pure-read grant: the full gated
    path (sweep, probe with the path guard first, sentinel, env pops) and a policy that allows reads inside the
    checkout only and denies write_file / patch / terminal (rules AND the READ-ONLY hard deny)."""
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a, wt, alerts = _setup(d)
        open(os.path.join(wt, "f.txt"), "w").write("x\n")
        open(os.path.join(wt, ".db-env"), "w").write("T=1\n")
        open(os.path.join(d, "outside.txt"), "w").write("CANARY\n")
        rec, prec = os.path.join(d, "rec.jsonl"), os.path.join(d, "probe.jsonl")
        with _stdin_pipe(), _env(HERMES_SAFE_MODE="1", TIRITH_ENABLED="1"):
            assert a._one_turn(Worker("gitlab:mr:1", "r", wt),
                               _r("mr-review", FAKE_RECORD=rec, FAKE_PROBE_RECORD=prec, FAKE_STATUS="pass"),
                               "SYS", "P", resume=False)
        (r,) = _recs(rec)
        assert r["argv"][r["argv"].index("-t") + 1] == "file", r["argv"]
        cfg = r["config"]
        assert cfg["plugins"] == {"enabled": ["eph-gate"], "load_timeout_seconds": 0}, cfg["plugins"]
        assert cfg["tools"]["tool_search"]["enabled"] == "off" and cfg["lsp"]["enabled"] is False, cfg
        assert "terminal" in cfg["agent"]["disabled_toolsets"] and "file" not in cfg["agent"]["disabled_toolsets"]
        assert cfg["terminal"]["cwd"] == os.path.realpath(wt) and cfg["terminal"]["shell_init_files"] == [], cfg
        assert r["terminal_cwd"] == os.path.realpath(wt) and r["cwd"] == os.path.realpath(wt), r
        assert r["hermes_env"] == ["HERMES_HOME"] and not r["tirith_env"], r
        pol = r["policy"]
        grant = CC.capability_tool_grant("mr-review")
        assert "--permission-mode" not in grant
        assert pol["allow"] == G.split_rules(grant[grant.index("--allowedTools") + 1]) == ["Read", "Grep", "Glob"]
        assert pol["deny"] == G.split_rules(grant[grant.index("--disallowedTools") + 1]), pol["deny"]
        assert {"Edit", "Write", "Bash"} <= set(pol["deny"]), pol["deny"]
        assert pol["capability"] == "mr-review" and pol["edits"] is False, pol
        assert pol["secret_globs"] == list(CC._SECRET_FILE_GLOBS), pol
        assert pol["cwd"] == pol["read_root"] == os.path.realpath(wt), pol
        assert r["curl_verdict"][0] == "block", r["curl_verdict"]
        # the probe ran (path guard first, -P, same cwd, stdin /dev/null); no separate path check needed
        (p,) = _recs(prec)
        assert p["kind"] == "probe" and p["argv"][:2] == ["-P", "-c"] and p["argv"][4] == G.PROBE_TOOL, p
        assert json.loads(p["argv"][3])[1:] == r["argv"], p
        assert p["cwd"] == os.path.realpath(wt) and p["stdin_devnull"] is True, p
        assert not alerts, alerts
        # the policy the run got, as the plugin loads it
        P = G.Policy(pol)
        assert G.decide("read_file", {"path": "f.txt"}, P).allowed
        assert G.decide("search_files", {"pattern": "*.txt", "target": "files"}, P).allowed
        for tool, args in (("write_file", {"path": "f.txt", "content": "y"}),
                           ("patch", {"path": "f.txt", "old_string": "x", "new_string": "y"}),
                           ("terminal", {"command": "git status"}),
                           ("read_file", {"path": os.path.join(d, "outside.txt")}),
                           ("read_file", {"path": ".db-env"}),
                           ("search_files", {"pattern": "T"})):
            v = G.decide(tool, args, P)
            assert not v.allowed and "DENIED" in v.reason, (tool, args, v)
        for tool in G.READ_ONLY_DENIED_TOOLS:                 # the hard deny names the read-only capability
            assert "READ-ONLY" in G.decide(tool, {"path": "f.txt", "command": "x"}, P).reason
        # refusal paths fail closed with no launch: gate not loaded / stale nonce / probe crash / bad interpreter path
        n = len(_recs(rec))
        for mode in ("unloaded", "wrongnonce", "crash", "badpath"):
            try:
                a._one_turn(Worker("gitlab:mr:1", "r", wt), _r("mr-review", FAKE_RECORD=rec, FAKE_PROBE=mode),
                            "SYS", "P", resume=False)
                raise AssertionError(f"mr-review probe mode {mode} must refuse")
            except H.HermesRefused:
                pass
        assert len(_recs(rec)) == n, "hermes must never launch after a refused mr-review probe"
        # the run itself didn't load the gate: sentinel missing, so the result is discarded and alerted
        assert a._one_turn(Worker("gitlab:mr:1", "r", wt), _r("mr-review", FAKE_RECORD=rec, FAKE_NO_GATE="1"),
                           "SYS", "P", resume=False) is None
        assert any("not provably live" in m for m in alerts), alerts
        # belts: a widened CC mr-review grant, or a terminal toolset, is refused before any launch
        n = len(_recs(rec))
        orig_grant = CC.capability_tool_grant
        for widened in (["--permission-mode", "acceptEdits", "--allowedTools", "Read Grep Glob"],
                        ["--allowedTools", "Read Grep Glob Bash(git status:*)", "--disallowedTools", "Edit"],
                        ["--allowedTools", "Read Write"]):
            CC.capability_tool_grant = lambda cap, *x, _w=widened, **k: _w if cap == "mr-review" else orig_grant(cap, *x, **k)
            try:
                a._one_turn(Worker("gitlab:mr:1", "r", wt), _r("mr-review", FAKE_RECORD=rec), "SYS", "P", resume=False)
                raise AssertionError(f"a widened mr-review grant must refuse: {widened}")
            except H.HermesRefused as e:
                assert "pure-read" in str(e), e
            finally:
                CC.capability_tool_grant = orig_grant
        orig_ts = H.HERMES_GRANTS["mr-review"]
        H.HERMES_GRANTS["mr-review"] = ("file", "terminal")
        try:
            a._one_turn(Worker("gitlab:mr:1", "r", wt), _r("mr-review", FAKE_RECORD=rec), "SYS", "P", resume=False)
            raise AssertionError("mr-review with a terminal toolset must refuse")
        except H.HermesRefused as e:
            assert "file toolset" in str(e), e
        finally:
            H.HERMES_GRANTS["mr-review"] = orig_ts
        assert len(_recs(rec)) == n, "a refused mr-review turn must never launch"
    ok("eph-gate: mr-review - `-t file` (no terminal) under eph-gate, policy = cc's pure-read grant + secret "
       "floor, read_root = the checkout; probe (path guard first) + sentinel + env pops run; reads inside allowed, "
       "outside/secret/write_file/patch/terminal denied; probe failures / no gate / a widened grant fail closed")


def test_adapter_path_tripwire_refuses():
    """A relative (editable-placeholder) entry on the hermes interpreter's sys.path refuses the turn BEFORE
    any launch, via the guard at the head of the probe, on every honoured capability (mr-review included)."""
    with tempfile.TemporaryDirectory() as d, _Registry(d):
        a, wt, alerts = _setup(d)
        rec, prec = os.path.join(d, "rec.jsonl"), os.path.join(d, "probe.jsonl")
        for cap in ("write", "mr-review"):
            try:
                a._one_turn(Worker("k", "r", wt), _r(cap, FAKE_RECORD=rec, FAKE_PROBE="badpath",
                                                     FAKE_PROBE_RECORD=prec), "SYS", "P", resume=False)
                raise AssertionError(f"{cap}: a relative sys.path entry must refuse")
            except H.HermesRefused as e:
                assert "NON-editable" in str(e) and "__editable__" in str(e), (cap, e)
        assert [p["kind"] for p in _recs(prec)] == ["probe", "probe"], _recs(prec)
        assert not os.path.exists(rec), "hermes must never launch on a relative interpreter path"
        # a worker without a worktree never runs (every hermes turn's cwd is a worktree; the guard keys on it)
        try:
            a._one_turn(Worker("k", "r", None), _r("mr-review", FAKE_RECORD=rec), "SYS", "P", resume=False)
            raise AssertionError("mr-review without a worktree must refuse")
        except H.HermesRefused:
            pass
    ok("eph-gate: an editable-style relative sys.path entry refuses write and mr-review (probe "
       "guard) before any launch")


def test_terminal_env_file_race_refuses():
    """A same-uid re-plant of the terminal init file between the sweep and its O_EXCL create is a clean
    HermesRefused, not a raw FileExistsError."""
    with tempfile.TemporaryDirectory() as d, _Registry(d), _env(CODE_NONCLAUDE_OK="1"):
        a, wt, alerts = _setup(d)
        os.makedirs(os.path.join(wt, "src"))
        rec = os.path.join(d, "rec.jsonl")
        orig = H.HermesHarness.__dict__["_sweep_home"]

        def racy(home):
            orig.__func__(home)
            open(os.path.join(home, H.TERMINAL_ENV_FILE), "w").write("export PYTHONPATH=/evil\n")
        H.HermesHarness._sweep_home = staticmethod(racy)
        try:
            w = Worker("k", "r", wt); w.test_runner = "venv"; w.repo = os.path.join(d, "base")
            try:
                a._one_turn(w, _r("code", FAKE_RECORD=rec), "SYS", "P", resume=False)
                raise AssertionError("a re-planted terminal env file must refuse")
            except H.HermesRefused as e:
                assert H.TERMINAL_ENV_FILE in str(e), e
        finally:
            H.HermesHarness._sweep_home = orig
        assert not os.path.exists(rec)
    ok("eph-gate: a re-planted eph-terminal-env.sh after the sweep -> HermesRefused, no launch")


def test_check_interpreter_silent_nonzero_exit_refuses():
    """Mutating `_check_interpreter`'s `rc != 0 or PATH_OK_TAG not in ...` clause to `if False and ...` once left
    every test passing: a check-interpreter subprocess that crashes or exits non-zero PRINTING NOTHING (no
    PATH_REFUSE_TAG line for `_path_refusal` to catch) went untested. Here the
    fake `python` sibling exits 1 with empty stdout; only the rc/tag fallback clause can catch it."""
    with tempfile.TemporaryDirectory() as d:
        bindir = os.path.join(d, "bin"); os.makedirs(bindir)
        hb = _exe(os.path.join(bindir, "hermes"), "#!/usr/bin/env python3\n")   # space in shebang, so the sibling python is used
        _exe(os.path.join(bindir, "python"), "#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n")
        h = H.HermesHarness(hermes_bin=hb, home_root=os.path.join(d, "homes"))
        try:
            h._check_interpreter({"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, d)
            raise AssertionError("a silent rc=1 interpreter-path check must refuse")
        except H.HermesRefused as e:
            assert "did not pass" in str(e) and "rc=1" in str(e), e
    ok("eph-gate: _check_interpreter refuses a silent non-zero-exit path check (rc/tag fallback, "
       "not just _path_refusal)")


def test_check_interpreter_ok_exit_without_tag_refuses():
    """The other half of the same gap: a check-interpreter subprocess that exits 0 but never prints
    EPH_PATH_OK (e.g. a truncated / interrupted run) must still refuse."""
    with tempfile.TemporaryDirectory() as d:
        bindir = os.path.join(d, "bin"); os.makedirs(bindir)
        hb = _exe(os.path.join(bindir, "hermes"), "#!/usr/bin/env python3\n")
        _exe(os.path.join(bindir, "python"), "#!/usr/bin/env python3\nprint('not the tag')\n")
        h = H.HermesHarness(hermes_bin=hb, home_root=os.path.join(d, "homes"))
        try:
            h._check_interpreter({"PATH": os.environ.get("PATH", "/usr/bin:/bin")}, d)
            raise AssertionError("a zero-exit check without EPH_PATH_OK must refuse")
        except H.HermesRefused as e:
            assert "did not pass" in str(e) and "rc=0" in str(e), e
    ok("eph-gate: _check_interpreter refuses a zero-exit path check missing the EPH_PATH_OK tag")


def _real_hermes_lazy_deps_python():
    """The real hermes interpreter (not just any python >= 3.11), only if it can import hermes' own
    tools.lazy_deps. Returns None (skip) off this box / CI's python:3.9 image."""
    py = H._hermes_python(H.HERMES_BIN)
    if not py or not os.access(py, os.X_OK):
        return None
    r = subprocess.run([py, "-c", "import tools.lazy_deps"], capture_output=True, text=True)
    return py if r.returncode == 0 else None


_LAZY_DEPS_PROBE_SCRIPT = (
    "import json, subprocess\n"
    "from tools import lazy_deps\n"
    "feature = next((f for f in lazy_deps.LAZY_DEPS if lazy_deps.feature_missing(f)), None)\n"
    "if feature is None:\n"
    "    print(json.dumps({'skip': 'no missing lazy feature on this box'})); raise SystemExit\n"
    "calls = []\n"
    "def _fake(cmd, **kw):\n"
    "    calls.append({'cmd': cmd, 'cwd': kw.get('cwd')})\n"
    "    return subprocess.CompletedProcess(cmd, 1, stdout='', stderr='network disabled for test')\n"
    "lazy_deps._run_installer = _fake\n"
    "try:\n"
    "    lazy_deps.ensure(feature, prompt=False)\n"
    "    blocked, reason = False, ''\n"
    "except lazy_deps.FeatureUnavailable as e:\n"
    "    blocked, reason = True, str(e)\n"
    "print(json.dumps({'feature': feature, 'blocked': blocked, 'reason': reason, 'calls': calls}))\n"
)


def test_lazy_installs_disabled_blocks_worktree_uv_toml():
    """The adapter's own hermes_config() sets security.allow_lazy_installs False for EVERY hermes turn.
    Proves (a) that key is what actually lands in the written config, and (b) it stops hermes' REAL
    tools.lazy_deps.ensure() before any uv/pip subprocess runs, even with a worker-planted uv.toml sitting in
    the turn's cwd. Mechanism: a non-editable install has no root/pyproject.toml, so _uv_policy_args("core")
    returns cwd=None and a real uv invocation would inherit the process cwd (the worktree, and its uv.toml).
    Runs hermes' real interpreter + real lazy_deps module; negative control flips the config key back to True
    and shows the same call reaches the installer with cwd=None, so reverting the fix fails this test."""
    py = _real_hermes_lazy_deps_python()
    if not py:
        ok("eph-gate: lazy-install uv.toml test SKIPPED (no real hermes tools.lazy_deps here)")
        return
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        home = os.path.join(d, "home"); os.makedirs(home)
        wt = os.path.join(d, "wt"); os.makedirs(wt)
        # a worker-planted uv.toml: an unpinned-cwd uv would pick this index up
        open(os.path.join(wt, "uv.toml"), "w").write(
            '[[index]]\nurl = "file:///nonexistent-eph-test-planted-index"\ndefault = true\n')
        provider = {"api": "https://x", "key_env": "K", "transport": "chat_completions", "default_model": "m"}

        def _run(allow_lazy_installs):
            cfg = H.hermes_config("SYS", provider, "m", (), gated=False)
            cfg["security"]["allow_lazy_installs"] = allow_lazy_installs
            open(os.path.join(home, "config.yaml"), "w").write(H.yaml_safe_json(cfg))
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d, "HERMES_HOME": home}
            r = subprocess.run([py, "-c", _LAZY_DEPS_PROBE_SCRIPT], cwd=wt, env=env, capture_output=True,
                               text=True, timeout=60)
            assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
            return json.loads(r.stdout.strip().splitlines()[-1])

        # (a) the real adapter function writes the key OFF by default
        cfg_check = H.hermes_config("SYS", provider, "m", (), gated=False)
        assert cfg_check["security"]["allow_lazy_installs"] is False, cfg_check["security"]

        # (b) with that config, ensure() refuses before any subprocess; the planted uv.toml is never touched
        off = _run(False)
        if "skip" in off:
            ok(f"eph-gate: lazy-install uv.toml test SKIPPED ({off['skip']})")
            return
        assert off["blocked"] is True and "allow_lazy_installs" in off["reason"], off
        assert off["calls"] == [], off

        # negative control: flip the key back to True and the SAME call reaches the installer (our fake still
        # fails the install on purpose, so `raise`/`reason` differ, but the installer IS invoked, with an
        # unpinned cwd=None); this proves the test would fail if the fix were reverted
        on = _run(True)
        assert on["calls"] and on["calls"][0]["cwd"] is None, on
        assert "allow_lazy_installs" not in on["reason"], on
    ok("eph-gate: hermes_config()'s allow_lazy_installs=False stops tools.lazy_deps.ensure() before "
       "any uv/pip subprocess even with a planted worktree uv.toml (real hermes interpreter + real lazy_deps); "
       "flipping the key back to True reaches the installer with an unpinned cwd (negative control)")


# =====================================================================================================
# general = eph-gate floors FIRST, then the policy judge (fake wrapper; no real hook / bus / 4B)
# =====================================================================================================
_FAKE_WRAPPER = r'''#!/usr/bin/env python3
import json, os, sys
ev = json.load(sys.stdin)
d = os.environ["JUDGE_DIR"]
ev["_proj"] = os.environ.get("CLAUDE_PROJECT_DIR")
open(os.path.join(d, "calls.jsonl"), "a").write(json.dumps(ev) + "\n")
mode = open(os.path.join(d, "fake-mode")).read().strip()
subj = ev["tool_input"].get("command") or ev["tool_input"].get("file_path") or ""
import hashlib
sha = hashlib.sha256(subj.encode()).hexdigest()
def final(outcome, reason):
    open(os.path.join(d, "decisions.jsonl"), "a").write(json.dumps(
        {"kind": "final", "outcome": outcome, "reason": reason, "subject": subj[:120], "subject_sha": sha}) + "\n")
def say(dec, why):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": dec,
                                             "permissionDecisionReason": why}}))
if mode == "allow":
    final("allow", "tier0: read-only"); say("allow", "gate: tier0: read-only")
elif mode == "ask":
    final("ask", "4b: escalate"); say("deny", "gate: this needs approval (4b: escalate). DM ops ...")
elif mode == "4bdeny":
    final("deny", "4b: looks destructive"); say("deny", "gate: 4b: looks destructive")
elif mode == "hard":
    final("deny", "tier0: secret read"); say("deny", "gate: tier0: secret read (hard deny)")
elif mode == "block":
    print("policy judge: no decision", file=sys.stderr); sys.exit(2)
elif mode == "garbage":
    print("not json")
elif mode == "silent":
    pass
'''


@contextlib.contextmanager
def _judge_policy(mode):
    with tempfile.TemporaryDirectory() as gd:
        gd = os.path.realpath(gd)
        wrapper = os.path.join(gd, "wrapper.py")
        open(wrapper, "w").write(_FAKE_WRAPPER)
        os.chmod(wrapper, 0o700)
        open(os.path.join(gd, "fake-mode"), "w").write(mode)
        pol = H.gate_policy("general", fx().wt, nonce="N-G")
        pol["judge"] = dict(pol["judge"], wrapper=wrapper, gate_dir=gd, project_dir=gd)
        yield G.Policy(pol), gd


def _calls(gd):
    p = os.path.join(gd, "calls.jsonl")
    return [json.loads(l) for l in open(p)] if os.path.exists(p) else []


def test_general_floors_run_before_the_judge():
    with _judge_policy("allow") as (p, gd):
        for tool, args in (("terminal", {"command": "git push origin HEAD"}),       # _GENERAL_DENIED floor
                           ("terminal", {"command": "sudo ls"}),
                           ("terminal", {"command": "ls", "background": True}),     # foreground only
                           ("write_file", {"path": os.path.join(fx().d, "outside", "x"), "content": "y"}),
                           ("write_file", {"path": ".git/config", "content": "y"}),
                           ("read_file", {"path": "sec/.db-env"}),                  # secret floor, no judge
                           ("patch", {"path": "f.txt", "old_string": "x", "new_string": "y", "mode": "patch"})):
            v = G.decide(tool, args, p)
            assert not v.allowed and v.kind == "floor", (tool, args, v)
        assert _calls(gd) == [], "a floor deny must never reach the judge"
        assert G.decide("read_file", {"path": "f.txt"}, p).allowed and _calls(gd) == [], "reads: no judge round-trip"
        v = G.decide("terminal", {"command": "ls -la > out.txt"}, p)                # lexer refuses: judge alone
        assert v.allowed and v.modified_args == {"workdir": fx().wt}, v
        v = G.decide("write_file", {"path": "f.txt", "content": "z\n"}, p)
        assert v.allowed, v
        c = _calls(gd)
        assert [e["tool_name"] for e in c] == ["Bash", "Write"], c
        assert c[0]["tool_input"] == {"command": "ls -la > out.txt"}, "the judge sees the EXACT command"
        assert c[1]["tool_input"]["file_path"] == os.path.join(fx().wt, "f.txt")
    ok("general: eph-gate floors (_GENERAL_DENIED, foreground-only, worktree-confined writes, no .git, secret "
       "floor, plain patch only) deny BEFORE the judge is called; reads are gate-only; terminal/write_file reach "
       "the judge with the exact command / resolved path")



def test_item3_judge_never_sees_worktree_as_project_dir():
    """The worker must not control its judge's policy input. (b) CLAUDE_PROJECT_DIR is the
    neutral project_dir, never the worktree, and a project_dir overlapping the write root fails closed. (a) belt:
    AUTHORIZED.md / .claude settings writes and terminal commands naming them / the gate dir are HARD floor denies
    (kind floor, no sha = not mintable) that never reach the judge."""
    assert H.GENERAL_JUDGE["project_dir"] == H.GENERAL_JUDGE["gate_dir"]
    with _judge_policy("allow") as (p, gd):
        assert G.decide("terminal", {"command": "ls -la > out.txt"}, p).allowed
        c = _calls(gd)
        assert c and all(e["_proj"] == gd for e in c), c
        assert all(not e["_proj"].startswith(p.write_root) for e in c)
        n = len(_calls(gd))
        for tool, args in (("write_file", {"path": "AUTHORIZED.md", "content": "x"}),
                           ("write_file", {"path": "sub/authorized.MD", "content": "x"}),
                           ("patch", {"path": "AUTHORIZED.md", "old_string": "a", "new_string": "b"}),
                           ("write_file", {"path": ".claude/settings.local.json", "content": "{}"}),
                           ("terminal", {"command": "echo hi >> AUTHORIZED.md"}),
                           ("terminal", {"command": "cp x .claude/settings.json"}),
                           ("terminal", {"command": "cat ~/.eph-judge/eph-general/decisions.jsonl"})):
            v = G.decide(tool, args, p)
            assert not v.allowed and v.kind == "floor" and v.sha is None, (tool, args, v)
        assert len(_calls(gd)) == n, "a policy-source floor deny must never reach the judge"
        for bad in (p.write_root, os.path.join(p.write_root, "sub"), os.path.dirname(p.write_root)):
            q = G.Policy(dict(H.gate_policy("general", fx().wt, nonce="N-G"),
                              judge=dict(p.judge, project_dir=bad)))
            v = G.decide("terminal", {"command": "ls -la > out.txt"}, q)
            assert not v.allowed and v.kind == "judge" and "overlaps" in v.reason, (bad, v)
        q = G.Policy(dict(H.gate_policy("general", fx().wt, nonce="N-G"), judge=dict(p.judge, project_dir="")))
        assert not G.decide("terminal", {"command": "ls -la > out.txt"}, q).allowed
        assert len(_calls(gd)) == n
    ok("judge CLAUDE_PROJECT_DIR = neutral project_dir (never the worktree; overlap/missing = "
       "fail-closed); AUTHORIZED.md / .claude settings / the judge dir are hard, non-mintable floors before the judge")

def test_general_unmapped_tool_kind():
    with _judge_policy("allow") as (p, gd):
        for tool in ("web_search", "web_extract", "process_manage", "browser_navigate", "execute_code"):
            v = G.decide(tool, {}, p)
            assert not v.allowed and v.kind == "unmapped-tool" and ("unmapped-tool: " + tool) in v.reason, v
        assert _calls(gd) == []
    ok("an unmapped tool (web_* included, fix 3) denies as kind unmapped-tool / 'unmapped-tool: <name>', "
       "distinct from a floor or judged deny, without a judge call")


def test_general_judge_fail_closed_and_sha():
    cmd = "cat <<'EOF' > notes.md\nline one\n  trailing  \nEOF\n"
    want = __import__("hashlib").sha256(cmd.encode()).hexdigest()
    for mode, sha in (("ask", want), ("4bdeny", want), ("hard", None), ("block", None), ("garbage", None),
                      ("silent", None)):
        with _judge_policy(mode) as (p, gd):
            v = G.decide("terminal", {"command": cmd}, p)
            assert not v.allowed and v.kind == "judge", (mode, v)
            assert v.sha == sha, (mode, v.sha)
    with _judge_policy("allow") as (p, gd):
        p.judge = dict(p.judge, wrapper="/nonexistent/wrapper")
        v = G.decide("terminal", {"command": "ls"}, p)
        assert not v.allowed and v.kind == "judge" and "fail-closed" in v.reason, v
        p.judge = {}
        v = G.decide("terminal", {"command": "ls"}, p)
        assert not v.allowed, "an empty judge config must not fall back to the allow-list path"
    ok("judge is fail-closed (ask / 4B deny / hard deny / rc 2 / garbage / silent / missing wrapper / "
       "empty config all deny); the allow-once sha is the hook's own subject_sha byte-exact (heredoc + trailing "
       "spaces + newlines), only on allow-once-capable finals (ask, 4b deny), never on a hard deny")


def test_denials_recorded_and_reported():
    with tempfile.TemporaryDirectory() as home, _judge_policy("ask") as (p, gd):
        PLUGIN.record_denial("terminal", {"command": "git push"}, G.decide("terminal", {"command": "git push"}, p),
                             home=home)
        PLUGIN.record_denial("web_search", {}, G.decide("web_search", {}, p), home=home)
        v = G.decide("terminal", {"command": "make clean\n"}, p)
        PLUGIN.record_denial("terminal", {"command": "make clean\n"}, v, home=home)
        rows = H.read_denials(home)
        assert [r["kind"] for r in rows] == ["floor", "unmapped-tool", "judge"], rows
        assert rows[2]["sha"] == __import__("hashlib").sha256(b"make clean\n").hexdigest()
        assert rows[2]["subject"] == "make clean\n" and "sha" not in rows[0]
        h = CC._gate_denials_html(rows + [{"kind": "judge", "tool": "<script>", "reason": "<b>x</b>"}])
        assert rows[2]["sha"] in h and "&lt;script&gt;" in h and "<b>x</b>" not in h, h
        assert H.read_denials(os.path.join(home, "nope")) == []
    ok("every eph-gate denial is recorded with its kind; read_denials returns floor / unmapped-tool / "
       "judge rows, the judge row carrying the byte-exact sha + exact subject; the comment block is html-escaped")


def test_forged_envelope_cannot_carry_denials():
    raw = json.dumps({"type": "result", "result": json.dumps({"status": "done", "summary": "s", "gate_denials": [
        {"kind": "judge", "tool": "terminal", "sha": "0" * 64}]}), "session_id": "s", "usage": {}})
    route = Route(domain="x", repo="/srv/x", prompt_class="agent", agent="coder-agent", capability="general")
    env = EnvelopeParser().try_parse(raw, "r", route)
    assert env is not None and "gate_denials" not in env, env
    ok("a worker cannot forge gate_denials (the envelope parser whitelists keys; the adapter adds them "
       "from the plugin's own record)")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} eph-gate self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001 - keep going, report all
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    if FX is not None:
        FX.close()
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
