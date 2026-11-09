"""`none`: an explicit opt-out. eph-gate's floors still run (capability grant, secret floor, worktree confinement,
the general deny list); whatever passes them is allowed with no outside judge. Never the default: you have to
write EXEC_GATE=none to get it."""
NAME = "none"
LABEL = "no exec gate (EXEC_GATE=none)"
FLOOR_FILES = ()
FLOOR_TERMS = ()


def config(env):
    return {}


def judge(request, cfg):
    return {"decision": "allow", "token": None, "reason": "allowed: EXEC_GATE=none (eph-gate floors only)"}
