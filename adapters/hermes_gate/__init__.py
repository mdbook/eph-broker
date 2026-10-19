"""eph-gate: the hermes plugin half of the worker gate (see gate.py for the policy and tool mapping).

Installed by adapters/hermes.py into a worker's PRIVATE HERMES_HOME as plugins/eph-gate/ and enabled by name
(`plugins.enabled: [eph-gate]`). On registration it loads <HERMES_HOME>/eph-gate-policy.json (written by the
adapter for THIS turn), registers ONE `pre_tool_call` hook and writes <HERMES_HOME>/eph-gate.loaded with the
policy nonce, the adapter's post-turn DETECTION that the gate was live in the real run (the pre-flight probe is
the pre-turn proof). The sentinel is detection, not prevention: it is checked after the tools ran, and an
ungated run's own terminal could forge it (the nonce sits in the same-uid policy file). Prevention is the
probe plus a deterministic load (plugins.load_timeout_seconds: 0, see adapters/hermes.py).
It also empties hermes' post-write SHELL-linter table (neutralise_shell_lint).

FAIL-CLOSED: hermes treats a hook that RAISES as "no verdict" (the tool runs: model_tools._pre_dispatch_guards
logs and proceeds), so the hook never raises: every error path returns a block. A missing / unreadable /
malformed policy registers the hook anyway with policy=None, which blocks every call.
"""
import importlib, json, os, sys

from . import gate as _gate            # always a package import (hermes loads plugins as packages); no fallback

POLICY_FILE = "eph-gate-policy.json"
SENTINEL_FILE = "eph-gate.loaded"
# One JSON line per refused call this turn (swept with the home before every turn, see adapters/hermes.py
# _HOME_KEEP). The adapter folds it into the result envelope as `gate_denials` (deny-and-report).
DENIALS_FILE = "eph-gate-denials.jsonl"
_SUBJECT_CAP = 4000     # a longer command is reported by sha only (a truncated subject could never reproduce bytes)


def _home():
    return os.environ.get("HERMES_HOME") or ""


def record_denial(tool_name, args, v, home=None):
    """Append one denial row. Never raises. `subject` = the exact terminal command (only for a judged deny that
    carries an allow-once sha, so a reviewer can check sha256(subject) == sha before minting); `sha` byte-exact from the
    hook. kind: floor | unmapped-tool | judge."""
    try:
        row = {"kind": getattr(v, "kind", "floor"), "tool": str(tool_name), "reason": (v.reason or "")[:600]}
        sha = getattr(v, "sha", None)
        if sha:
            row["sha"] = sha
            cmd = (args or {}).get("command") if tool_name == "terminal" else None
            if isinstance(cmd, str) and len(cmd) <= _SUBJECT_CAP:
                row["subject"] = cmd
            elif tool_name != "terminal":
                row["path"] = str((args or {}).get("path") or "")[:400]
        fd = os.open(os.path.join(home or _home(), DENIALS_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:                   # noqa: BLE001 - reporting must never turn a deny into an error path
        pass


def load_policy(home=None):
    try:
        with open(os.path.join(home or _home(), POLICY_FILE), encoding="utf-8") as f:
            return _gate.Policy(json.load(f))
    except Exception:                   # noqa: BLE001 - no/garbled policy: block everything
        return None


def make_hook(policy):
    def pre_tool_call(tool_name=None, args=None, **_kw):
        try:
            v = _gate.decide(tool_name, args, policy)
            if not v.allowed:
                if tool_name != _gate.PROBE_TOOL:
                    record_denial(tool_name, args, v)
                return {"action": "block", "message": v.reason or "eph-gate: DENIED"}
            if v.modified_args:
                return {"action": "modify", "args": dict(v.modified_args)}
            return None
        except BaseException:           # noqa: BLE001 - never raise out of the hook (hermes would fail OPEN)
            return {"action": "block", "message": "eph-gate: DENIED - hook error (fail-closed)"}
    return pre_tool_call


def neutralise_shell_lint():
    """B1: hermes runs a SHELL linter (node / npx tsc / go vet / rustfmt) after an allowed write_file / patch,
    outside pre_tool_call, and no config key turns that tier off (tools/file_operations_lint.py LINTERS is a
    module table read at call time). Empty it in THIS process (hermes loads plugins in-process, before any tool
    runs). Only inside hermes (hermes_cli loaded); never raises. The gate re-checks the live table on every
    write, so a failure here denies those file types instead of opening them."""
    if "hermes_cli" not in sys.modules:
        return False
    try:
        mod = importlib.import_module(_gate.LINT_MODULE)
        mod.LINTERS.clear()
        return not mod.LINTERS
    except Exception:                   # noqa: BLE001
        return False


def register(ctx):
    home = _home()
    neutralise_shell_lint()
    policy = load_policy(home)
    ctx.register_hook("pre_tool_call", make_hook(policy))
    if policy is not None and home:
        try:
            tmp = os.path.join(home, SENTINEL_FILE + ".%d.tmp" % os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"nonce": policy.nonce, "pid": os.getpid()}, f)
            os.replace(tmp, os.path.join(home, SENTINEL_FILE))
        except OSError:
            pass                        # no sentinel: the adapter's post-turn check fails the turn
