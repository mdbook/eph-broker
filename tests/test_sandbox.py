#!/usr/bin/env python3
"""Sandbox argv contracts and real kernel canaries; no model calls or third-party deps."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for key, name in (("BROKER_SITE_ENV", "site.test.env"), ("BROKER_CONFIG", "broker.test.json"),
                  ("BROKER_PROVENANCE_PATH", "provenance.test.json"),
                  ("BROKER_INBOX_ACCOUNTS", "inbox-accounts.test.json")):
    os.environ.setdefault(key, str(ROOT / "tests" / "fixtures" / name))

import sandbox
import site_env
import steer


class SandboxTests(unittest.TestCase):
    def patched(self, obj, name, value):
        p = patch.object(obj, name, value)
        p.start()
        self.addCleanup(p.stop)

    def setUp(self):
        # A socket-denied legacy test may exit before restoring its process-wide umask.
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in ("WORKER_SANDBOX", "WORKER_SANDBOX_EXTRA_RO", "WORKER_SANDBOX_HIDE"):
            os.environ.pop(key, None)
        directory = tempfile.TemporaryDirectory(prefix="sandbox-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home, self.config, self.wt, self.git = (self.root / "home", self.root / "config",
                                                   self.root / "worktree", self.root / "base" / ".git")
        for path in (self.home, self.config, self.wt, self.git):
            path.mkdir(parents=True)
        os.environ["HOME"] = str(self.home)
        self.patched(site_env, "SITE_ENV", str(self.config / "broker.env"))
        self.patched(site_env, "BROKER_CONFIG", str(self.config / "broker.yaml"))

    def fake_bwrap(self):
        self.patched(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")

    def test_default_argv_and_real_knobs(self):
        self.fake_bwrap()
        credentials = self.home / ".git-credentials"
        credentials.write_text("sentinel")
        private = self.home / ".config" / "eph-broker"
        private.mkdir(parents=True)
        claude = self.home / ".claude"
        claude.mkdir()
        hidden, extra = self.wt / "private directory", self.wt / "read only"
        hidden.mkdir()
        extra.mkdir()
        os.environ["WORKER_SANDBOX_EXTRA_RO"] = str(extra)
        os.environ["WORKER_SANDBOX_HIDE"] = os.pathsep.join((str(hidden), str(credentials)))
        argv = ["claude", "--model", "m", "a value with spaces"]
        result = sandbox.wrap(argv, self.wt, self.git, writable_paths=(claude,))
        self.assertEqual(argv, ["claude", "--model", "m", "a value with spaces"])
        self.assertEqual(result[:2], ["/usr/bin/bwrap", "--die-with-parent"])
        self.assertEqual(result[result.index("--") + 1:], argv)
        for flag in ("--new-session", "--unshare-user", "--unshare-pid"):
            self.assertIn(flag, result)
        triples = list(zip(result, result[1:], result[2:]))
        for triple in (("--ro-bind", "/", "/"), ("--bind", str(self.wt), str(self.wt)),
                       ("--bind", str(self.git), str(self.git)), ("--bind", str(claude), str(claude)),
                       ("--ro-bind", str(extra), str(extra)), ("--ro-bind", "/dev/null", str(credentials))):
            self.assertIn(triple, triples)
        pairs = list(zip(result, result[1:]))
        for pair in (("--proc", "/proc"), ("--dev", "/dev"), ("--tmpfs", "/tmp"),
                     ("--tmpfs", str(self.config)), ("--tmpfs", str(private)), ("--tmpfs", str(hidden))):
            self.assertIn(pair, pairs)
        self.assertNotIn(("--tmpfs", str(claude)), pairs)
        self.assertGreater(result.index(str(hidden)), result.index(str(self.wt)))
        with patch.object(sandbox.shutil, "which", lambda _: "bin/bwrap"):
            self.assertEqual(sandbox.check(), os.path.abspath("bin/bwrap"))

    def test_hides_win_over_extra_ro_and_symlinks(self):
        self.fake_bwrap()
        target = self.wt / "secret"
        target.write_text("sentinel")
        (self.home / ".git-credentials").symlink_to(target)
        os.environ["WORKER_SANDBOX_EXTRA_RO"] = str(self.wt)
        result = sandbox.wrap(["worker"], self.wt, self.git)
        self.assertGreater(result.index(str(target)), max(i for i, v in enumerate(result) if v == str(self.wt)))
        self.assertEqual(result[-5:-2], ["--ro-bind", "/dev/null", str(target)])

    def test_symlinked_site_files_hide_targets_outside_parent(self):
        self.fake_bwrap()
        targets = [self.wt / "env-target", self.wt / "config-target"]
        for source, target in zip((site_env.SITE_ENV, site_env.BROKER_CONFIG), targets):
            target.write_text("secret sentinel")
            Path(source).symlink_to(target)
        result = sandbox.wrap(["worker"], self.wt, self.git)
        self.assertIn(("--tmpfs", str(self.config)), list(zip(result, result[1:])))
        for target in targets:
            self.assertIn(("--ro-bind", "/dev/null", str(target)), list(zip(result, result[1:], result[2:])))

    def test_steer_paths_are_subject_scoped_and_optional(self):
        os.environ["BROKER_STEER_ROOT"] = str(self.root / "steer")
        os.environ["BROKER_STEER_MID_TURN"] = "1"
        key = "sandbox-paths"
        self.assertEqual(steer.sandbox_paths(key), (steer.subject_dir(key),))
        self.assertTrue(Path(steer.subject_dir(key), "inbox").is_dir())
        os.environ["BROKER_STEER_MID_TURN"] = "0"
        self.assertEqual(steer.sandbox_paths("disabled"), ())
        self.assertFalse(Path(steer.subject_dir("disabled")).exists())

    def test_steer_claim_permission_failure_is_logged(self):
        os.environ["BROKER_STEER_ROOT"] = str(self.root / "steer")
        key = "claim-failure"
        self.assertTrue(steer.enqueue(key, "failed", "kept for the next turn"))
        with patch.object(steer.os, "rename", side_effect=PermissionError("read-only inbox")):
            with self.assertLogs("eph-broker.steer", level="WARNING") as logs:
                self.assertIsNone(steer.deliver(steer.subject_dir(key)))
        self.assertIn("PermissionError", logs.output[0])
        self.assertTrue(steer.reclaim(key, "failed"))

    def test_extra_ro_cannot_restore_host_proc(self):
        self.fake_bwrap()
        os.environ["WORKER_SANDBOX_EXTRA_RO"] = "/proc"
        result = sandbox.wrap(["worker"], self.wt, self.git)
        self.assertGreater(result.index("--proc"), result.index("/proc"))

    def test_off_needs_no_binary_or_paths(self):
        os.environ["WORKER_SANDBOX"] = "off"
        self.patched(sandbox.shutil, "which", Mock(side_effect=AssertionError("must not look for bwrap")))
        self.assertIsNone(sandbox.check())
        self.assertEqual(sandbox.wrap(["worker", "arg"], "relative", None), ["worker", "arg"])

    def test_missing_binary_and_invalid_modes_fail_closed(self):
        self.patched(sandbox.shutil, "which", lambda _: None)
        for mode in (None, "bwrap", "typo", ""):
            with self.subTest(mode=mode):
                if mode is None:
                    os.environ.pop("WORKER_SANDBOX", None)
                else:
                    os.environ["WORKER_SANDBOX"] = mode
                with self.assertRaises(sandbox.SandboxError):
                    sandbox.check()
                with self.assertRaises(sandbox.SandboxError):
                    sandbox.wrap(["worker"], "/worktree", None)

    def test_startup_refuses_before_constructing_broker(self):
        import app
        os.environ["WORKER_SANDBOX"] = "bwrap"
        self.patched(sandbox.shutil, "which", lambda _: None)
        broker = Mock(side_effect=AssertionError("must refuse before Broker()"))
        self.patched(app, "Broker", broker)
        with self.assertRaisesRegex(sandbox.SandboxError, "requires bubblewrap"):
            app.main()
        broker.assert_not_called()

    def test_unsafe_paths_refused(self):
        self.fake_bwrap()
        for knob, value in (("WORKER_SANDBOX_EXTRA_RO", "relative"), ("WORKER_SANDBOX_HIDE", "/")):
            with self.subTest(knob=knob), patch.dict(os.environ, {knob: value}):
                with self.assertRaises(sandbox.SandboxError):
                    sandbox.wrap(["worker"], self.wt, self.git)

    def test_gitdir_snapshot_not_worker_repoint(self):
        linked = self.git / "worktrees" / "worker"
        linked.mkdir(parents=True)
        (linked / "commondir").write_text("../..\n")
        (self.wt / ".git").write_text(f"gitdir: {linked}\n")
        from adapters.cc import Worker
        worker = Worker("key", "run", str(self.wt))
        self.assertEqual(worker.sandbox_gitdir, str(self.git))
        (self.wt / ".git").write_text("gitdir: /\n")
        self.assertEqual(worker.sandbox_gitdir, str(self.git))
        self.assertEqual(sandbox.gitdir(self.git.parent), str(self.git))
        self.assertIsNone(sandbox.gitdir(None))

    def test_worker_launch_sites_use_wrapper(self):
        """Stop at Popen: each real turn must pass sandbox argv, not just the unit builder."""
        from adapters import cc, hermes, codex
        from envelope import EnvelopeParser
        from routing import Route
        os.environ["BROKER_STEER_ROOT"] = str(self.root / "steer")
        os.environ["BROKER_STEER_MID_TURN"] = "1"
        real_which = shutil.which
        self.patched(shutil, "which", lambda name, **kw:
                     "/usr/bin/bwrap" if name == "bwrap" else real_which(name, **kw))
        for harness in ("cc", "hermes", "codex"):
            with self.subTest(harness=harness):
                worker = cc.Worker("key", "run", str(self.wt))
                route = Route(domain="test", repo=str(self.wt), prompt_class="p", agent="a",
                              capability="mr-review", adapter=harness)
                adapter = cc.CCAdapter(router=None, worktree_mgr=None, prompt_loader=None,
                                       envelope_parser=EnvelopeParser(), claude_bin="/bin/true")
                launches = []

                def stop(argv, **kwargs):
                    if argv[0] != "git":   # gate_policy also probes the repo root (failure is harmless)
                        launches.append(argv)
                    raise OSError("stop at launch")

                if harness == "cc":
                    turn = lambda: adapter._one_turn(worker, route, "SYS", "P", resume=False)
                elif harness == "hermes":
                    home = self.wt / "hermes-home"
                    home.mkdir()
                    runner = hermes.HermesHarness.__new__(hermes.HermesHarness)
                    runner.hermes_bin = "/bin/true"
                    self.patched(runner, "_home", lambda _: str(home))
                    self.patched(runner, "_install_gate", lambda *a: "nonce")
                    self.patched(hermes, "hermes_provider", lambda _: ("provider", "model", "token", {}))
                    turn = lambda: runner.one_turn(adapter, worker, route, "SYS", "P", None)
                else:
                    home = self.wt / "codex-home"
                    home.mkdir()
                    runner = codex.CodexHarness.__new__(codex.CodexHarness)
                    runner.codex_bin = "/bin/true"
                    self.patched(runner, "_check_version", lambda: None)
                    self.patched(runner, "_home", lambda _: str(home))
                    self.patched(runner, "_link_auth", lambda _: None)
                    self.patched(runner, "_install_gate", lambda *a: "nonce")
                    self.patched(codex, "codex_model", lambda _: ("model", "high", "test"))
                    turn = lambda: runner.one_turn(adapter, worker, route, "SYS", "P", None)
                with patch.object(subprocess, "Popen", stop):
                    with self.assertRaisesRegex(OSError, "stop at launch"):
                        turn()
                self.assertEqual(len(launches), 1)
                self.assertEqual(launches[0][0], "/usr/bin/bwrap")
                self.assertIn("--unshare-pid", launches[0])
                self.assertIn("--", launches[0])
                binds = [(a, b) for flag, a, b in zip(launches[0], launches[0][1:], launches[0][2:])
                         if flag == "--bind"]
                delivery = steer.subject_dir(worker.key)
                self.assertEqual((delivery, delivery) in binds, harness in ("cc", "codex"))
                self.assertNotIn((steer.root(), steer.root()), binds)
                if harness in ("cc", "codex"):
                    self.assertTrue(Path(delivery, "inbox").is_dir())
                self.assertFalse(worker.in_turn)

    def test_worker_launch_with_unavailable_steer_storage(self):
        from adapters import cc, codex
        from routing import Route
        os.environ["BROKER_STEER_MID_TURN"] = "1"
        real_which = shutil.which
        self.patched(shutil, "which", lambda name, **kw:
                     "/usr/bin/bwrap" if name == "bwrap" else real_which(name, **kw))
        for harness in ("cc", "codex"):
            for mode in ("off", "bwrap"):
                for failure in ("permission", "symlink"):
                    with self.subTest(harness=harness, mode=mode, failure=failure):
                        os.environ["WORKER_SANDBOX"] = mode
                        storage = self.root / f"steer-{harness}-{mode}-{failure}"
                        os.environ["BROKER_STEER_ROOT"] = str(storage)
                        if failure == "symlink":
                            storage.symlink_to(self.root, target_is_directory=True)
                        worker = cc.Worker("vikunja:task:9001", "run", str(self.wt))
                        route = Route(domain="test", repo=str(self.wt), prompt_class="p", agent="a",
                                      capability="mr-review", adapter=harness)
                        parser = Mock()
                        parser.try_parse.return_value = {"status": "done"}
                        adapter = cc.CCAdapter(router=None, worktree_mgr=None, prompt_loader=None,
                                               envelope_parser=parser, claude_bin="/bin/true")
                        if harness == "cc":
                            turn = lambda: adapter._one_turn(worker, route, "SYS", "P", resume=False)
                        else:
                            home = self.wt / f"codex-home-{mode}-{failure}"
                            home.mkdir()
                            runner = codex.CodexHarness.__new__(codex.CodexHarness)
                            runner.codex_bin = "/bin/true"
                            self.patched(runner, "_check_version", lambda: None)
                            self.patched(runner, "_home", lambda _: str(home))
                            self.patched(runner, "_link_auth", lambda _: None)
                            self.patched(runner, "_install_gate", lambda *a: "nonce")
                            self.patched(codex, "codex_model", lambda _: ("model", "high", "test"))
                            self.patched(codex, "_stream_process", lambda *a:
                                         ('{"type":"turn.completed"}', "", False))
                            turn = lambda: runner.one_turn(adapter, worker, route, "SYS", "P", None)
                        proc = Mock(returncode=0)
                        proc.communicate.return_value = ("{}", "")
                        launches = []

                        def launch(argv, **kwargs):
                            if argv[0] == "git":      # harmless gate_policy repo-root probe
                                raise OSError("no git probe in this test")
                            launches.append(argv)
                            return proc

                        real_private_dir = steer._private_dir

                        def prepare(directory):
                            if failure == "permission":
                                raise PermissionError("inaccessible steering storage")
                            return real_private_dir(directory)

                        with patch.object(steer, "_private_dir", prepare), \
                                patch.object(subprocess, "Popen", launch), \
                                self.assertLogs("eph-broker.steer", level="WARNING") as logs:
                            self.assertEqual(turn(), {"status": "done"})
                            # Unavailable storage still leaves messages for the next turn.
                            self.assertFalse(steer.enqueue(worker.key, "pending", "next turn"))
                            self.assertTrue(steer.reclaim(worker.key, "pending"))
                        self.assertEqual(len(logs.output), 1)
                        self.assertEqual(len(launches), 1)
                        argv = launches[0]
                        self.assertEqual(argv[0], "/bin/true" if mode == "off" else "/usr/bin/bwrap")
                        binds = [(a, b) for flag, a, b in zip(argv, argv[1:], argv[2:]) if flag == "--bind"]
                        self.assertNotIn((steer.subject_dir(worker.key),) * 2, binds)
                        self.assertNotIn((steer.root(),) * 2, binds)
                        self.assertFalse(worker.in_turn)

    def require_bwrap(self):
        binary = shutil.which("bwrap")
        if not binary:
            self.skipTest("bubblewrap not installed")
        probe = subprocess.run([binary, "--ro-bind", "/", "/", "--unshare-user", "--unshare-pid",
                                "--proc", "/proc", "--", "/bin/true"], capture_output=True, text=True)
        if probe.returncode:
            if any(s in probe.stderr.lower() for s in ("operation not permitted", "permission denied",
                                                       "no permissions to create", "creating new namespace failed")):
                self.skipTest("host disallows bubblewrap namespaces: " + probe.stderr.strip())
            self.fail(probe.stderr)

    def kernel_boundary(self, kind):
        self.require_bwrap()
        hidden = self.root / "hidden"
        if kind == "directory":
            hidden.mkdir()
            sentinel = hidden / "sentinel"
        else:
            sentinel = hidden
        sentinel.write_text("must not be visible")
        os.environ["WORKER_SANDBOX_HIDE"] = str(hidden)
        code = """
import pathlib, sys
broker_pid, sentinel, worktree, git = sys.argv[1:]
try:
    pathlib.Path('/proc/' + broker_pid + '/environ').read_bytes()
except OSError:
    pass
else:
    raise AssertionError('broker environ visible')
assert not pathlib.Path('/proc/' + broker_pid).exists()
try:
    data = pathlib.Path(sentinel).read_bytes()
except OSError:
    data = b''
assert not data, 'hidden sentinel visible'
pathlib.Path(worktree, 'written').write_text('ok')
pathlib.Path(git, 'written').write_text('ok')
try:
    pathlib.Path(worktree).parent.joinpath('outside-worktree').write_text('not allowed')
except OSError:
    pass
else:
    raise AssertionError('host root writable')
"""
        # Pytest itself can be PID 2 in a container, colliding with bwrap's worker PID 2.
        # A real broker stand-in at another PID avoids accidentally reading self.
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as broker:
            try:
                command = [sys.executable, "-c", code, str(broker.pid), str(sentinel), str(self.wt), str(self.git)]
                result = subprocess.run(sandbox.wrap(command, self.wt, self.git), cwd=self.wt,
                                        capture_output=True, text=True)
            finally:
                broker.kill()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.wt / "written").read_text(), "ok")
        self.assertEqual((self.git / "written").read_text(), "ok")

    def test_bwrap_kernel_boundary_file(self):
        self.kernel_boundary("file")

    def test_bwrap_kernel_boundary_directory(self):
        self.kernel_boundary("directory")

    def test_bwrap_symlinked_site_files(self):
        self.require_bwrap()
        paths = []
        for name, source in (("env-target", site_env.SITE_ENV), ("config-target", site_env.BROKER_CONFIG)):
            target = self.wt / name           # visible through the worktree bind unless explicitly masked
            target.write_text("secret sentinel")
            Path(source).symlink_to(target)
            paths.extend((source, str(target)))
        code = """
import pathlib, sys
for name in sys.argv[1:]:
    try:
        data = pathlib.Path(name).read_bytes()
    except OSError:
        data = b''
    assert not data, 'env/config secret visible: ' + name
"""
        result = subprocess.run(sandbox.wrap([sys.executable, "-c", code, *paths], self.wt, self.git),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def kernel_steer_delivery(self, configured_events):
        self.require_bwrap()
        # One default TMPDIR-based inbox, one events directory on the read-only host root.
        directory = tempfile.TemporaryDirectory(prefix="sandbox-events-", dir=ROOT)
        self.addCleanup(directory.cleanup)
        tmp = tempfile.TemporaryDirectory(prefix="sandbox-steer-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        os.environ.pop("BROKER_STEER_ROOT", None)
        os.environ.pop("EVENTS_LOG", None)
        os.environ["TMPDIR"] = tmp.name
        os.environ["BROKER_STEER_MID_TURN"] = "1"
        if configured_events:
            os.environ["EVENTS_LOG"] = str(Path(directory.name) / "events.jsonl")
        key, other = "sandbox-steer", "other-subject"
        writable = steer.sandbox_paths(key)      # prepare BEFORE the first message is queued
        self.assertTrue(steer.enqueue(key, "one", "a real mid-turn message", "operator"))
        self.assertTrue(steer.enqueue(other, "two", "not for this worker"))
        event = json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Bash"})
        command = sandbox.wrap(["/bin/sh", "-c", steer.hook_command(key)], self.wt, self.git,
                               writable_paths=writable)
        # The hook gets no broker environment; the command itself carries its delivery path.
        result = subprocess.run(command, input=event, env={"PATH": os.defpath}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PostToolUse")
        self.assertIn(steer.frame("a real mid-turn message", "operator"), output["additionalContext"])
        self.assertFalse(steer.reclaim(key, "one"), "the delivery log prevents turn-boundary duplication")
        self.assertTrue(list(Path(steer.subject_dir(other), "inbox").glob("*.msg")))
        # No grant of the steer root, another inbox, or the events/state directory.
        code = """
import pathlib, sys
for directory in sys.argv[1:]:
    try:
        pathlib.Path(directory, 'forged').write_text('no')
    except OSError:
        pass
"""
        protected = [steer.root(), steer.subject_dir(other), directory.name]
        result = subprocess.run(sandbox.wrap([sys.executable, "-c", code, steer.root(),
                                             steer.subject_dir(other), directory.name], self.wt, self.git,
                                            writable_paths=writable), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        # A private /tmp parent may be writable, but never the corresponding host state.
        for path in protected:
            self.assertFalse(Path(path, "forged").exists(), "other host broker state writable: " + path)

    def test_bwrap_steer_default_tmp_inbox(self):
        self.kernel_steer_delivery(False)

    def test_bwrap_steer_configured_events_directory(self):
        self.kernel_steer_delivery(True)


if __name__ == "__main__":
    unittest.main()
