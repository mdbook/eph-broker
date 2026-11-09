"""`exec`: bring your own gate, in any language. EXEC_GATE_COMMAND is run once per judged call.

In:  the request as one JSON object on stdin: {"tool", "tool_input", "cwd", "session_id", "capability",
     "write_root"} (tool is Claude Code shaped: Bash / Write / Edit; tool_input is that tool's input).
Out: one JSON object on stdout: {"decision": "allow" | "deny", "reason": "..."}, exit 0.
Anything else is a deny: a non-zero exit, a timeout (EXEC_GATE_TIMEOUT), no output, non-JSON, a decision other than
"allow". The command runs with cwd = the call's cwd and a minimal env (PATH=/usr/bin:/bin, HOME, plus the names in
EXEC_GATE_INHERIT_ENV), never the worker's own env.
"""
import json, os, shlex, subprocess

NAME = "exec"
LABEL = "exec gate"
FLOOR_FILES = ()
FLOOR_TERMS = ()
_REASON_CAP = 400


def config(env):
    raw = env.get("EXEC_GATE_COMMAND") or ""
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        raise ValueError("EXEC_GATE_COMMAND does not parse (%s)" % e)
    if not argv:
        raise ValueError("EXEC_GATE=exec needs EXEC_GATE_COMMAND")
    argv[0] = os.path.expanduser(argv[0])
    if not os.path.isabs(argv[0]):
        raise ValueError("EXEC_GATE_COMMAND must start with an absolute path, got %r" % argv[0])
    return {"argv": argv}


def _deny(reason):
    return {"decision": "deny", "token": None, "reason": "eph-gate: DENIED by %s: %s" % (LABEL, reason)}


def judge(request, cfg):
    argv = cfg.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        return _deny("%s: no command configured, fail-closed" % request.get("tool"))
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")}
    env.update({k: os.environ[k] for k in cfg.get("inherit_env") or () if os.environ.get(k)})
    try:
        r = subprocess.run(argv, input=json.dumps(request), env=env, cwd=request.get("cwd") or None,
                           capture_output=True, text=True, timeout=float(cfg.get("timeout") or 5.0))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return _deny("%s: the gate did not run (%s), fail-closed" % (request.get("tool"), type(e).__name__))
    if r.returncode != 0:
        return _deny("%s: gate exit %s (%s), fail-closed" % (
            request.get("tool"), r.returncode, (r.stderr or "").strip()[:_REASON_CAP]))
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = None
    if not isinstance(out, dict):
        return _deny("%s: the gate printed no JSON object, fail-closed" % request.get("tool"))
    reason = str(out.get("reason") or "")[:_REASON_CAP]
    if out.get("decision") == "allow":
        return {"decision": "allow", "token": None, "reason": "allowed by %s: %s" % (LABEL, reason)}
    return _deny(reason or "no decision, fail-closed")
