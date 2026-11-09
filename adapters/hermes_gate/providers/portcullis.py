"""`portcullis`: the reference provider. One Portcullis decision per judged call, through Portcullis' hook-mode
wrapper (gate-wrapper.sh -> gate-hook.py), armed so any no-decision is a block.

Config (all paths absolute, `~` expanded):
    EXEC_GATE_PORTCULLIS_WRAPPER       gate-wrapper.sh
    EXEC_GATE_PORTCULLIS_HOOK          gate-hook.py
    EXEC_GATE_PORTCULLIS_PYTHON        the interpreter the hook runs under
    EXEC_GATE_PORTCULLIS_DIR           the gate (switch) dir for broker workers: decisions.jsonl, allow-once/
    EXEC_GATE_PORTCULLIS_PROJECT_DIR   the judge's CLAUDE_PROJECT_DIR (default: the gate dir). gate-hook reads
                                       <dir>/AUTHORIZED.md and .claude/settings*.json there as the agent's own
                                       policy, so it must be a NEUTRAL dir the worker cannot write: judge() refuses
                                       one that overlaps the worker's write root 

The wrapper gets a Claude Code PreToolUse event on stdin and answers with hookSpecificOutput.permissionDecision.
On a deny the allow-once token is the hook's OWN subject_sha from the final decisions.jsonl row (_allow_once_sha),
byte-exact, never recomputed.

CEILING: one gate dir = ONE concurrent judged worker. Two workers would share ask-dm-sent / clearance-pending /
m4b-pin state and interleave decisions.jsonl. Per-worker isolation = a gate dir per worker; not built.
"""
import json, os, re, subprocess

NAME = "portcullis"
LABEL = "Portcullis"
# Portcullis' own policy input (gate-hook reads <project>/AUTHORIZED.md). Hard floor, never mintable.
FLOOR_FILES = (r"AUTHORIZED\.md",)
FLOOR_TERMS = (r"authori[sz]ed\.md",)
_KEYS = ("wrapper", "hook", "python", "gate_dir", "project_dir")


def config(env):
    def path(key, default=None):
        v = env.get(key) or default
        if not v:
            raise ValueError("EXEC_GATE=portcullis needs %s" % key)
        v = os.path.expanduser(v)
        if not os.path.isabs(v):
            raise ValueError("%s must be an absolute path, got %r" % (key, v))
        return v
    gate_dir = path("EXEC_GATE_PORTCULLIS_DIR")
    return {"wrapper": path("EXEC_GATE_PORTCULLIS_WRAPPER"), "hook": path("EXEC_GATE_PORTCULLIS_HOOK"),
            "python": path("EXEC_GATE_PORTCULLIS_PYTHON"), "gate_dir": gate_dir,
            "project_dir": path("EXEC_GATE_PORTCULLIS_PROJECT_DIR", gate_dir)}


def floor_terms(cfg):
    """Literal names the general terminal floor protects whenever portcullis is selected: Portcullis' own home, so
    dropping EXEC_GATE_PROTECTED_TERMS can never drop it. The home is the deepest dir holding the gate dir, wrapper
    and hook (`~/.portcullis/{bin,eph}` -> `portcullis`, leading dots stripped so `.portcullis` and `portcullis`
    both hit). When they share nothing narrower than $HOME or / (`/opt/portcullis/bin` + `/var/lib/portcullis/eph`),
    the gate dir path itself. A belt like the rest of the terminal floor: a computed name gets past it."""
    gate_dir = cfg["gate_dir"].rstrip("/") or "/"
    home = os.path.commonpath([gate_dir, os.path.dirname(cfg["wrapper"]), os.path.dirname(cfg["hook"])])
    name = os.path.basename(home).lstrip(".")
    if not name or home in ("/", os.path.expanduser("~").rstrip("/")):
        return [gate_dir]
    return [name]


def override_hint(cfg):
    d = os.path.join(cfg["gate_dir"], "allow-once") + "/"
    home = os.path.expanduser("~").rstrip("/") + "/"
    return "mint in %s if warranted" % ("~/" + d[len(home):] if d.startswith(home) else d)


def _inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _deny(reason, token=None):
    return {"decision": "deny", "reason": reason, "token": token}


def judge(request, cfg):
    cc_tool, cwd = request.get("tool"), request.get("cwd")
    if not isinstance(cfg, dict) or not all(cfg.get(k) for k in _KEYS):
        return _deny("eph-gate: DENIED, %s: no Portcullis judge configured, fail-closed" % cc_tool)
    write_root = os.path.realpath(request.get("write_root") or cwd or "/")
    pdir = os.path.realpath(cfg["project_dir"])
    if _inside(pdir, write_root) or _inside(write_root, pdir):
        return _deny("eph-gate: DENIED, %s: judge project_dir overlaps the worker's write root, fail-closed" % cc_tool)
    #): CLAUDE_PROJECT_DIR is a NEUTRAL dir outside the worktree, never the cwd.
    timeout = str(cfg.get("timeout") or "5.0")
    env = {"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~"), "PORTCULLIS_HOOK": cfg["hook"],
           "PORTCULLIS_PY": cfg["python"], "PORTCULLIS_GATE_DIR": cfg["gate_dir"], "PORTCULLIS_HOOK_TIMEOUT": timeout,
           "CLAUDE_PROJECT_DIR": pdir}
    env.update({k: os.environ[k] for k in cfg.get("inherit_env") or () if os.environ.get(k)})
    ev = {"hook_event_name": "PreToolUse", "tool_name": cc_tool, "tool_input": request.get("tool_input"),
          "cwd": cwd, "session_id": request.get("session_id")}
    log = os.path.join(cfg["gate_dir"], "decisions.jsonl")
    try:
        log_off = os.path.getsize(log)
    except OSError:
        log_off = 0
    try:
        r = subprocess.run([cfg["wrapper"]], input=json.dumps(ev), env=env, cwd=cwd, capture_output=True, text=True,
                           timeout=float(timeout) + 5)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return _deny("eph-gate: DENIED, %s: Portcullis judge did not run (%s), fail-closed"
                     % (cc_tool, type(e).__name__))
    decision, reason = None, ""
    if r.returncode == 0:
        try:
            hso = json.loads(r.stdout)["hookSpecificOutput"]
            decision, reason = hso.get("permissionDecision"), str(hso.get("permissionDecisionReason") or "")
        except (ValueError, KeyError, TypeError):
            decision = None
    else:
        reason = (r.stderr or "").strip()[:400] or "wrapper rc=%s" % r.returncode
    if decision == "allow":
        return {"decision": "allow", "reason": "allowed by Portcullis: " + reason[:200], "token": None}
    sha = _allow_once_sha(log, log_off) if r.returncode == 0 and decision == "deny" else None
    return _deny("eph-gate: DENIED by Portcullis, %s%s" % (
        reason or "no decision (fail-closed)",
        " | reported to ops in your result; do not rephrase, split or route around it, carry on with other work "
        "or finish blocked" if sha else ""), token=sha)


def _allow_once_sha(log, off):
    """the allow-once token name for the call just judged, read from the hook's OWN `kind:"final"` row
    (gate-hook.py emit_outcome: subject_sha = sha256(subject) = the exact name approve_once_consume looks for; the
    hook's stdout reason never carries it, only its ops DM). Byte-exact, never recomputed. Only an allow-once-capable
    final yields one: outcome "ask" (ask/escalate path) or a "4b:" deny (a 4B false-deny is overridable); a tier0 /
    gate_rules hard deny, an operator decline or an expired clearance -> None. Reads only the bytes appended since `off`
    (the one-gate-dir ceiling above: a concurrent worker could interleave; then the LAST final wins. A wrong sha only
    makes the operator's mint a no-op, never an over-grant, because the token must match the retried bytes)."""
    try:
        with open(log, "rb") as f:
            f.seek(off)
            tail = f.read(1 << 20)
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("kind") != "final":
            continue
        reason, sha = str(row.get("reason") or ""), row.get("subject_sha")
        ok = row.get("outcome") == "ask" or (row.get("outcome") == "deny" and reason.startswith("4b:"))
        return sha if ok and isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else None
    return None
