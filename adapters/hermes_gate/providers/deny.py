"""`deny`: the default when EXEC_GATE is unset. Every judged call is denied (fail-closed: no gate means no judge,
never an open door). Floors only on capabilities that need no judge."""
NAME = "deny"
LABEL = "no exec gate"
FLOOR_FILES = ()
FLOOR_TERMS = ()


def config(env):
    return {}


def judge(request, cfg):
    return {"decision": "deny", "token": None,
            "reason": "eph-gate: DENIED: %s: no exec gate configured (EXEC_GATE is unset or deny), fail-closed"
                      % request.get("tool")}
