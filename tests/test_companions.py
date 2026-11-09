"""Companion snapshots use local git fixtures only: no server, credentials or second live checkout."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import sys
import tempfile
from contextlib import contextmanager, ExitStack
from unittest import TestCase
from unittest.mock import patch

raises = TestCase().assertRaises

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("BROKER_SITE_ENV", str(HERE / "fixtures" / "site.test.env"))
os.environ.setdefault("BROKER_PROVENANCE_PATH", str(HERE / "fixtures" / "provenance.test.json"))
os.environ.setdefault("BROKER_CONFIG", str(HERE / "fixtures" / "broker.test.json"))
os.environ.setdefault("BROKER_INBOX_ACCOUNTS", str(HERE / "fixtures" / "inbox-accounts.test.json"))

import repo_companions as C
import repo_catalog
from adapters import cc, codex, codex_gate
from adapters.hermes_gate import gate as G
from broker_state import BrokerState
from hooks.companion_guard import reason
from worktree import WorktreeManager


@contextmanager
def workspace():
    # Isolate a pre-existing socket test's leaked umask, and never write the user's git config.
    old = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}))
            os.environ.pop("REPO_COMPANIONS_FILE", None)
            patches = SimpleNamespace(
                setenv=lambda k, v: stack.enter_context(patch.dict(os.environ, {k: v})),
                setattr=lambda obj, k, v: stack.enter_context(patch.object(obj, k, v)))
            yield Path(d), patches
    finally:
        os.umask(old)


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def init(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "source.txt").write_text("first\n")
    git(path, "add", "source.txt")
    git(path, "commit", "-qm", "first")
    return path


def configured(tmp_path, patches, entries=None):
    base = init(tmp_path / "site")
    core = init(tmp_path / "core")
    config = tmp_path / "companions.json"
    config.write_text(json.dumps({"example/site": entries if entries is not None else [
        {"repo": "example/core", "ref": "main", "as": "core"}]}))
    patches.setenv("REPO_COMPANIONS_FILE", str(config))
    cat = repo_catalog.Catalog([
        repo_catalog.Repo("example/site", "site", local=str(base)),
        repo_catalog.Repo("example/core", "core", http_url=core.as_uri()),
    ], tier_b=[], deny=[])
    patches.setattr(repo_catalog, "get_catalog", lambda: cat)
    patches.setattr(repo_catalog, "broad_cred_git_args", lambda r: [])
    return base, core, config, cat


def test_cut_pins_shallow_ignored_readonly_sources_and_records_run():
    with workspace() as (tmp_path, patches):
        base, core, config, cat = configured(tmp_path, patches)
        sha = git(core, "rev-parse", "HEAD")
        manager = WorktreeManager(str(base), str(tmp_path / "workers"))
        path = manager.make("sample")
        calls = []
        run = subprocess.run

        def observed(argv, **kw):
            calls.append(argv)
            return run(argv, **kw)

        patches.setattr(subprocess, "run", observed)
        try:
            records = C.cut(path, str(base))
            dest = Path(records[0]["path"])
            assert records == [{"repo": "example/core", "ref": "main", "as": "core", "sha": sha,
                                "path": str(Path(path) / ".companions" / "core")}]
            assert (dest / "source.txt").read_text() == "first\n"
            assert not (dest / ".git").exists()
            assert (dest / "source.txt").stat().st_mode & 0o222 == 0
            assert dest.stat().st_mode & 0o222 == 0
            assert any("fetch" in c and "--depth=1" in c and "--no-tags" in c for c in calls)
            assert git(path, "check-ignore", ".companions/core/source.txt")
            git(path, "add", "-A")
            assert git(path, "diff", "--cached", "--name-only") == ""
            worker = SimpleNamespace(companions=records)
            assert C.environment(worker) == {"COMPANION_CORE": str(dest)}
            state_path = str(tmp_path / "state.sqlite")
            state = BrokerState(state_path)
            C.record_run(state, "sample", "run-sample", manager.cut_sha("sample"), records)
            state.close()
            (core / "source.txt").write_text("second\n")
            git(core, "commit", "-qam", "second")
            state = BrokerState(state_path)
            assert state.get("run:run-sample")["companions"][0]["sha"] == sha
            state.close()
            assert (dest / "source.txt").read_text() == "first\n"
        finally:
            manager.remove("sample")
        assert not Path(path).exists()


def test_worktree_preserves_disabled_global_config():
    """Root CI must not replace /dev/null when companions disable global Git config.

    Intercept writes instead of executing them: on a root runner Git's lock/rename would replace
    the device with a regular file, breaking subprocess.DEVNULL for every later test/process.
    """
    with workspace() as (tmp_path, patches):
        base = init(tmp_path / "site")
        manager = WorktreeManager(str(base), str(tmp_path / "workers"))
        alias = tmp_path / "null-link"
        alias.symlink_to(os.devnull)
        writes = []
        run = subprocess.run

        def observed(argv, **kw):
            if argv[:3] == ["git", "config", "--global"] and "safe.directory" in argv:
                writes.append(argv)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return run(argv, **kw)

        patches.setattr(subprocess, "run", observed)
        patches.setenv("HOME", str(tmp_path))  # clean HOME, as in the CI container
        for config in (os.devnull, str(alias)):
            patches.setenv("GIT_CONFIG_GLOBAL", config)
            path = manager.make("sample")
            try:
                assert (Path(path) / "source.txt").read_text() == "first\n"
            finally:
                manager.remove("sample")
            assert not Path(path).exists()
        assert writes == [], writes


def test_worktree_updates_private_global_config():
    """A real global config still registers/unregisters only this worktree's safe.directory."""
    with workspace() as (tmp_path, patches):
        config = tmp_path / "gitconfig"
        config.write_text("[safe]\n\tdirectory = /unrelated/repo\n")
        patches.setenv("GIT_CONFIG_GLOBAL", str(config))
        patches.setenv("HOME", str(tmp_path))
        base = init(tmp_path / "site")
        manager = WorktreeManager(str(base), str(tmp_path / "workers"))
        path = manager.make("sample")
        try:
            assert git(base, "config", "--global", "--get-all", "safe.directory").splitlines() == [
                "/unrelated/repo", path]
        finally:
            manager.remove("sample")
        assert git(base, "config", "--global", "--get-all", "safe.directory") == "/unrelated/repo"


_INVALID_ENTRIES = [
    {"repo": "../escape", "ref": "main", "as": "core"},
    {"repo": "example/core", "ref": "--upload-pack=bad", "as": "core"},
    {"repo": "example/core", "ref": "main", "as": "../escape"},
    {"repo": "example/core", "ref": "main", "as": "CORE"},
    {"repo": "example/core", "ref": "main", "as": "core", "url": "untrusted"},
]


def test_invalid_config_refuses():
    with workspace() as (tmp_path, patches):
        for entry in _INVALID_ENTRIES:
            config = tmp_path / "companions.json"
            config.write_text(json.dumps({"example/site": [entry]}))
            with raises(ValueError):
                C.load(str(config))


def test_missing_duplicate_denied_and_collision_refuse():
    with workspace() as (tmp_path, patches):
        base, core, config, cat = configured(tmp_path, patches)
        with raises(FileNotFoundError):
            C.load(str(tmp_path / "missing.json"))
        entry = {"repo": "example/core", "ref": "main", "as": "core"}
        config.write_text(json.dumps({"example/site": [entry, entry]}))
        with raises(ValueError):
            C.load()
        config.write_text(json.dumps({"example/site": [entry]}))
        cat.by_path.pop("example/core")
        with raises(ValueError):
            C.cut(str(base), str(base))
        cat.by_path["example/core"] = repo_catalog.Repo("example/core", "core", http_url=core.as_uri())
        (base / ".companions").symlink_to(core, target_is_directory=True)
        with raises(FileExistsError):
            C.cut(str(base), str(base))
        assert not (core / "core").exists()


def test_gate_read_allow_write_deny_and_secret_floor():
    with workspace() as (tmp_path, patches):
        patches.setattr(G, "_shell_linted", lambda p: False)
        root = tmp_path / "worker"
        root.mkdir()
        source = root / ".companions" / "core"
        source.mkdir(parents=True)
        (source / "source.txt").write_text("source")
        (source / "private.env").write_text("test fixture, not credentials")
        (root / "alias").symlink_to(source, target_is_directory=True)
        (source / "outside").symlink_to(tmp_path / "outside")
        policy = G.Policy({"cwd": str(root), "capability": "code", "edits": True,
                           "allow": ["Read", "Grep", "Glob", "Bash(git:*)", "Bash(python -m pytest:*)"],
                           "test_allow": ["Bash(python -m pytest:*)"]})
        for path in (".companions/core/source.txt", "alias/source.txt", "alias/../new.txt"):
            assert G.decide("read_file", {"path": path}, policy).allowed
            assert not G.decide("write_file", {"path": path, "content": "bad"}, policy).allowed
            assert not G.decide("patch", {"path": path, "old_string": "source", "new_string": "bad"}, policy).allowed
            for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                assert reason({"tool_name": tool, "tool_input": {"file_path": path}}, str(root))
            assert not codex_gate.evaluate({"tool_name": "apply_patch", "tool_input": {
                "input": "*** Begin Patch\n*** Update File: " + path + "\n@@\n-source\n+bad\n*** End Patch"}}, policy)[2].allowed
        for path in (".companions/core/private.env", ".companions/core/outside"):
            assert not G.decide("read_file", {"path": path}, policy).allowed
        assert G.decide("write_file", {"path": "primary.txt", "content": "ok"}, policy).allowed
        for cmd in ("git add .companions/core/source.txt", "git mv primary.txt alias/new", "sed -i x alias/source.txt"):
            assert not G.decide("terminal", {"command": cmd}, policy).allowed
            assert reason({"tool_name": "Bash", "tool_input": {"command": cmd}}, str(root))
        cmd = codex_gate._cd_prefix(str(root)) + "cat .companions/core/source.txt"
        assert codex_gate.evaluate({"tool_name": "exec_command", "tool_input": {"cmd": cmd}}, policy)[2].allowed
        assert G.decide("terminal", {"command": "python -m pytest .companions/core/source.txt"}, policy).allowed


def test_cc_guard_and_codex_shell_env_wiring():
    with workspace() as (tmp_path, patches):
        adapter = cc.CCAdapter.__new__(cc.CCAdapter)
        adapter.claude_bin = "claude"
        adapter.setting_sources = ""
        adapter.max_turns = 1
        adapter.max_turns_by_capability = {}
        argv = adapter.build_argv("brief", "model", "system", capability="code", test_runner="pytest",
                                  worktree_root=str(tmp_path), companions=True)
        settings = json.loads(argv[argv.index("--settings") + 1])
        guard = [h for h in settings["hooks"]["PreToolUse"] if "NotebookEdit" in h["matcher"]]
        assert len(guard) == 1 and "companion_guard.py" in guard[0]["hooks"][0]["command"]
        config = codex.codex_config("model", None, "workspace-write", "/instructions",
                                    companion_env={"COMPANION_CORE": "/worker/.companions/core"})
        assert 'COMPANION_CORE = "/worker/.companions/core"' in config


def test_code_spawn_cuts_and_passes_provenance_to_worker():
    with workspace() as (tmp_path, patches):
        from test_broker import _run_continue_code_worker
        from unittest.mock import patch
        records = [{"repo": "example/core", "ref": "main", "as": "core", "sha": "b" * 40,
                    "path": "/worker/.companions/core"}]
        launches = []
        popen = subprocess.Popen

        def capture(argv, **kw):
            if "fake_claude_done.py" in str(argv):
                launches.append((argv, kw.get("env", {})))
            return popen(argv, **kw)

        patches.setattr(subprocess, "Popen", capture)
        with patch.object(C, "cut", return_value=records) as cut, patch.object(C, "record_run") as record:
            calls, comments, alerts = _run_continue_code_worker("<p>companion sources</p>")
        cut.assert_called_once()
        record.assert_called_once()
        assert record.call_args.args[3:] == ("a" * 40, records)
        brief = next(b for name, b in calls if name == "brief")
        assert brief["companions"] == records
        assert launches and launches[0][1]["COMPANION_CORE"] == records[0]["path"]
        assert any("fixed" in text for _, text in comments), alerts


def test_multiple_companions_and_failed_ref():
    with workspace() as (d, patches):
        base, core, config, cat = configured(d, patches)
        cat.by_path["example/core"].remote = core.as_uri()
        cat.by_path["example/core"].http_url = (d / "unused-clone-url").as_uri()
        sha = git(core, "rev-parse", "HEAD")
        config.write_text(json.dumps({"example/site": [
            {"repo": "example/core", "ref": "main", "as": "core"},
            {"repo": "example/core", "ref": sha, "as": "tools"}]}))
        try:
            records = C.cut(str(base), str(base))
            assert len(records) == 2 and {r["sha"] for r in records} == {sha}
            assert set(C.environment(SimpleNamespace(companions=records))) == {"COMPANION_CORE", "COMPANION_TOOLS"}
        finally:
            C.prepare_remove(str(base))
        # A separate checkout attempts a nonexistent ref, never falls back to the source's HEAD.
        other = init(d / "other")
        cat.by_path["example/site"].local = str(other)
        config.write_text(json.dumps({"example/site": [{"repo": "example/core", "ref": "missing", "as": "core"}]}))
        with raises(RuntimeError):
            C.cut(str(other), str(other))


def test_cc_directory_floor_and_test_environment_operand():
    with workspace() as (root, patches):
        (root / ".companions" / "core").mkdir(parents=True)
        (root / "alias").symlink_to(root / ".companions" / "core", target_is_directory=True)
        for command in ("cd src; touch ../alias/source.txt", "builtin cd src; git add ../alias/source.txt"):
            assert reason({"tool_name": "Bash", "tool_input": {"command": command}}, str(root))
        command = "CORE_SRC=.companions/core python -m pytest tests"
        assert reason({"tool_name": "Bash", "tool_input": {"command": command}}, str(root),
                      ["Bash(python -m pytest:*)"]) is None


def test_cc_guard_denies_alias_redirection_on_parse_failure():
    with workspace() as (root, patches):
        source = root / ".companions" / "core"
        source.mkdir(parents=True)
        (source / "source.txt").write_text("source")
        (root / "alias").symlink_to(source, target_is_directory=True)
        command = "chmod u+w alias/source.txt; printf changed > alias/source.txt"
        with raises(G.Deny):
            G.split_command(command)
        assert reason({"tool_name": "Bash", "tool_input": {"command": command}}, str(root)) == \
            "companion command cannot be safely parsed"
        # Literal reads through the same alias still pass the realpath-based floor.
        for command in ("cat alias/source.txt", "cat 'alias/source.txt'"):
            assert reason({"tool_name": "Bash", "tool_input": {"command": command}}, str(root)) is None


def test_cc_guard_fails_closed_for_all_rejected_shell_syntax():
    with workspace() as (root, patches):
        # No reserved name is needed: aliases and computed paths can reach companions.
        for command in (
            "printf changed >> alias/source.txt",
            "cat < alias/source.txt",
            "chmod u+w $(printf alias/source.txt)",
            "chmod u+w `printf alias/source.txt`",
            "chmod u+w alias/*.txt",
            "(chmod u+w alias/source.txt)",
            "chmod u+w alias/source.txt &",
            "chmod u+w alias/source.txt # comment",
            "cat 'unterminated",
            "printf safe > primary.txt",
            "",
            None,
        ):
            with raises(G.Deny):
                G.split_command(command)
            assert reason({"tool_name": "Bash", "tool_input": {"command": command}}, str(root)) == \
                "companion command cannot be safely parsed", command


if __name__ == "__main__":
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
