"""Exec-gate providers: the judge eph-gate consults AFTER its own floors.

eph-gate (gate.py) is the broker's built-in floor: the capability grant from cc.capability_tool_grant, the secret
floor, worktree confinement. Where a capability also needs an outside judge (today: `general` on hermes), gate.judge()
asks the provider named by EXEC_GATE. Nothing about any one gate lives in gate.py; it all lives in a module here.

A provider module declares:
    NAME                  the EXEC_GATE value that selects it
    LABEL                 human name for reasons and operator alerts
    FLOOR_FILES           regex fragments: the gate's own policy files. A write to one is a hard floor deny, every
                          capability (matched case-insensitively against the path's tail)
    FLOOR_TERMS           regex fragments: a general terminal command that mentions one is a hard floor deny
    config(env)           BROKER side, at policy build: read the provider's EXEC_GATE_* keys from `env`, return a
                          JSON-safe dict (it rides in the per-turn policy file). Raise ValueError on a misconfig:
                          the turn is refused, never run ungated
    judge(request, cfg)   PLUGIN side, inside the worker's harness: one decision. request = {tool (Claude Code
                          shaped: Bash / Write / Edit), tool_input, cwd, session_id, capability, write_root}; cfg =
                          what config() returned plus the shared keys below. Returns {"decision": "allow" | "deny",
                          "reason": str, "token": str or None}. token = an override id the operator can grant
                          (Portcullis: the allow-once sha). gate.judge() turns ANY other outcome (an exception, a
                          non-dict, a missing decision) into a deny
    floor_terms(cfg)      optional, broker side: LITERAL names (escaped here) a general terminal command may not
                          mention, derived from the provider's own config (Portcullis: its home dir), so the
                          gate protects its own state with no extra site key
    override_hint(cfg)    optional, broker side: one line telling the operator how to override a deny that carries
                          a token. None = no override path

Shared keys every provider gets in cfg: provider (NAME), timeout (EXEC_GATE_TIMEOUT, seconds as a string, default
"5.0"), inherit_env (EXEC_GATE_INHERIT_ENV: env names a subprocess gate may inherit). EXEC_GATE_PROTECTED_TERMS adds
literal names to the terminal floor on top of the provider's own, whatever the provider.

STDLIB ONLY and Python 3.9-safe, like gate.py: this package is copied into the worker's plugin dir with it.
"""
import os, re

from . import deny, exec_cmd, none, portcullis

PROVIDERS = {m.NAME: m for m in (deny, none, exec_cmd, portcullis)}
DEFAULT = deny.NAME
DEFAULT_TIMEOUT = "5.0"
DEFAULT_INHERIT_ENV = ("TMPDIR", "LANG", "TZ")
_TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def get(name):
    """Provider module for `name`; ValueError for anything not registered (a typo never runs ungated)."""
    try:
        return PROVIDERS[name]
    except (KeyError, TypeError):
        raise ValueError("unknown exec gate %r (known: %s)" % (name, ", ".join(sorted(PROVIDERS))))


def selected(env=None):
    env = os.environ if env is None else env
    return get(env.get("EXEC_GATE") or DEFAULT)


def _csv(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def judge_config(env=None):
    """The `judge` block of a per-turn policy: the selected provider's config + the shared keys. ValueError on a
    misconfig."""
    env = os.environ if env is None else env
    prov = selected(env)
    timeout = env.get("EXEC_GATE_TIMEOUT") or DEFAULT_TIMEOUT
    try:
        ok = float(timeout) > 0
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("EXEC_GATE_TIMEOUT must be a positive number of seconds, got %r" % timeout)
    inherit_env = _csv(env.get("EXEC_GATE_INHERIT_ENV")) if env.get("EXEC_GATE_INHERIT_ENV") is not None \
        else list(DEFAULT_INHERIT_ENV)
    bad = [k for k in inherit_env if not k.isidentifier()]
    if bad:
        raise ValueError("EXEC_GATE_INHERIT_ENV holds non-identifier name(s) %r" % bad)
    cfg = dict(prov.config(env))
    cfg.update(provider=prov.NAME, timeout=timeout, inherit_env=inherit_env)
    return cfg


def floor(env=None):
    """The provider-declared floors for a per-turn policy (every capability): {"files": [...], "terms": [...]},
    regex fragments. EXEC_GATE_PROTECTED_TERMS entries are literal and escaped here."""
    env = os.environ if env is None else env
    prov = selected(env)
    names = []
    fn = getattr(prov, "floor_terms", None)
    if fn:
        try:
            names = list(fn(prov.config(env)))
        except ValueError:
            pass                        # a misconfigured provider refuses every judged turn in judge_config()
    seen, literal = set(), []
    for t in names + _csv(env.get("EXEC_GATE_PROTECTED_TERMS")):
        if t.lower() not in seen:
            seen.add(t.lower()); literal.append(re.escape(t))
    return {"files": list(prov.FLOOR_FILES), "terms": list(prov.FLOOR_TERMS) + literal}


def valid_token(tok):
    return tok if isinstance(tok, str) and _TOKEN_RE.fullmatch(tok) else None


def label(env=None):
    try:
        return selected(env).LABEL
    except ValueError:
        return "the exec gate"


def override_hint(env=None):
    """How the operator overrides a token-carrying deny, or None. Never raises (it only feeds an alert)."""
    try:
        prov = selected(env)
        fn = getattr(prov, "override_hint", None)
        return fn(prov.config(os.environ if env is None else env)) if fn else None
    except Exception:                   # noqa: BLE001 - a hint is cosmetic; a misconfig is refused elsewhere
        return None
