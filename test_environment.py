"""Broker-side test setup. Configuration is operator-owned, never ticket input."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading

_LOCK = threading.Lock()
_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\[\],<>=!~+\-]*\Z")


def prepare(repo, runner, env):
    """Provision an explicitly configured repo's .venv; return (runner, environment gap).

    No discovery/install hooks from the repo are executed. A gap never prevents editing;
    the worker keeps the named runner and reports what could not be verified.
    """
    config = os.getenv("CODE_TEST_DEPS_CONFIG", "")
    if not config:
        return runner, None
    try:
        with open(config, encoding="utf-8") as f:
            mapping = json.load(f)
        if not isinstance(mapping, dict):
            raise ValueError("expected a repo-to-requirements object")
        if repo not in mapping:
            return runner, None
        deps = mapping[repo]
        if (not os.path.isabs(repo) or not isinstance(deps, list)
                or any(not isinstance(d, str) or not _REQUIREMENT.fullmatch(d) for d in deps)):
            raise ValueError("expected an absolute repo path and a list of package requirements")
    except (OSError, ValueError) as e:
        return runner, f"test dependency configuration unavailable ({type(e).__name__})"
    runner = "venv"
    venv = os.path.join(repo, ".venv")
    python = os.path.join(venv, "bin", "python")
    marker = os.path.join(venv, ".eph-test-deps.json")
    clean_env = {k: v for k, v in env.items() if k in ("PATH", "HOME", "LANG", "TZ")}
    clean_env.update(PIP_CONFIG_FILE=os.devnull, PIP_NO_INPUT="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    stage = "dependency cache"
    try:
        with _LOCK:
            if os.access(python, os.X_OK) and os.path.isfile(marker):
                with open(marker, encoding="utf-8") as f:
                    if json.load(f) == deps:
                        return runner, None
            if not os.access(python, os.X_OK):
                stage = "venv creation"
                command = [sys.executable, "-m", "venv", venv]
                subprocess.run(command, check=True, capture_output=True, env=clean_env, timeout=120)
            if deps:
                stage = "pip install"
                command = [python, "-m", "pip", "install", "--", *deps]
                subprocess.run(command, check=True, capture_output=True, env=clean_env, timeout=240)
            stage = "dependency cache"
            with open(marker, "w", encoding="utf-8") as f:
                json.dump(deps, f)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        # The worker may publish this gap. Exclude host paths, package values and subprocess output.
        return runner, (f"test dependency provisioning failed during {stage} "
                        f"({type(e).__name__}, exit {getattr(e, 'returncode', 'unavailable')})")
    return runner, None


def apply_tmpdir(env, worker, capability):
    """Give each code worker a private, writable, short temp dir across all harnesses."""
    if capability != "code" or not worker.worktree:
        return
    if not getattr(worker, "test_tmpdir", None):
        worker.test_tmpdir = tempfile.mkdtemp(prefix="t-", dir=os.path.dirname(worker.worktree))
    env["TMPDIR"] = worker.test_tmpdir
