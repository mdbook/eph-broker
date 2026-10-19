#!/usr/bin/env python3
"""provenance.py: classify an event's `doer` as user / agent / system.

Replaces echo_guard's old `is_fleet_bot()` regex (`(-bot\\b|@<bus-domain>)`), which MISSED the shared
Vikunja service account (e.g. `svc-agent`) that the broker's own comments post under, so the
broker could not recognise its own comment echoes authored by that account. Provenance is the
correct long-term discriminator:

  * "agent"  - any fleet bot/service identity: the shared service account, the named fleet
               agents (per the roster, in bare/-agent/-bot form), anything matching
               `-bot\\b`, or any bus address matched by the roster agent_pattern. A superset of the old
               regex, and it closes the service-account gap.
  * "system" - an empty/None/blank doer (SAFE default: an unattributed event must never be
               treated as a human that re-arms), plus an (initially empty) designated-system set
               for future use.
  * "user"   - everything else (a real human).

Value-free: identity NAMES only, no secrets, no network calls.
"""
import json, logging, os, re

import site_env

log = logging.getLogger("eph-broker.provenance")

# Limitation: load-once at construct; add reload only if the roster must change without a broker
# restart. No hot-reload/watcher loop here on purpose.
# BROKER_PROVENANCE_PATH: an explicit roster file, e.g. the tests' own fixture
_OVERRIDE_PATH = os.getenv("BROKER_PROVENANCE_PATH") or os.path.join(
    os.getenv("CONFIG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")),
    "provenance.json",
)

# The deployment's roster lives in config/provenance.json (a private overlay; the public tree ships only
# config/provenance.example.json):
#   agent_basenames  REQUIRED. names whose bare/-agent/-bot forms are "agent" doers (the shared service account +
#                    the standing agents; provenance is load-bearing for comment-forward admission)
#   handoff_doers    REQUIRED. exact Vikunja usernames admitted by is_agent_handoff (and trusted by write_grant)
#   agent_pattern    REQUIRED. regex for any other agent/bus-address doer; ADDED to the built-in `-bot\b`, never
#                    replaces it (config can only widen "agent", the fail-closed direction)
#   untrusted_authors      REQUIRED. exact usernames write_grant never trusts even when listed in handoff_doers
#                          (a shared service account that pool TFAs also write as)
#   handoff_parked_labels  REQUIRED. label titles that stop is_agent_handoff from spawning (parked tickets)
#                          Required, not defaulted: a forgotten key would silently widen trust / spawning.
#   agent / system   optional extra exact identities.  _comment: ignored.  Any other key RAISES (typo guard).
# Explicit empty lists are a deliberate choice and allowed. A missing file RAISES unless BROKER_PROVENANCE_NEUTRAL=1
# (then: row identities, pool-NN, -bot only). Both raise at import: a classifier that silently lost its roster
# would read agent writes as human ones (echo loops, forged comment-forward admission).
# The code keeps only the broker's own row/worker identities. pool-NN: _POOL_RE below.
_ROW_BASENAMES = frozenset(filter(None, {
    "eph", "wiki", "code-worker", "general-worker", "mr-reviewer",
    # the dispatch-row assignees (same knobs and defaults as routing.DEV_ASSIGNEE / FLEET_ASSIGNEE /
    # FLEET_ASSIGNEE_ALT): a bot the site named, never a human
    os.getenv("DEV_ASSIGNEE", "eph-code").strip().lower(),
    os.getenv("FLEET_ASSIGNEE", "eph").strip().lower(),
    os.getenv("FLEET_ASSIGNEE_ALT", "").strip().lower(),
}))
_DEFAULT_AGENT_PATTERN = r"-bot\b"


_REQUIRED_KEYS = ("agent_basenames", "handoff_doers", "agent_pattern", "untrusted_authors", "handoff_parked_labels")
_KNOWN_KEYS = frozenset(_REQUIRED_KEYS + ("agent", "system", "_comment"))
_HUMAN_PROBES = ("", "alice", "bob smith", "carol")   # a site pattern matching any of these would swallow humans


def _str_set(data, key):
    v = data.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValueError(f"{key} must be a list of strings")
    return frozenset(x.strip().lower() for x in v if x.strip())


def _neutral_roster():
    # empty untrusted/parked sets are safe here: with no handoff_doers nothing is admitted or write-trusted
    # beyond routing's own creators
    return {"agent_basenames": frozenset(), "handoff_doers": frozenset(), "agent": frozenset(),
            "system": frozenset(), "untrusted_authors": frozenset(), "handoff_parked_labels": frozenset(),
            "agent_pattern": re.compile(_DEFAULT_AGENT_PATTERN, re.IGNORECASE)}


def _load_roster(path=None):
    """-> dict of frozensets (agent_basenames, handoff_doers, agent, system, untrusted_authors,
    handoff_parked_labels) + agent_pattern (compiled, `-bot\\b` OR the site pattern). Missing file ->
    RuntimeError unless BROKER_PROVENANCE_NEUTRAL=1 (neutral defaults, logged). Malformed JSON / unknown or
    missing key / wrong types / bad or match-all regex -> RuntimeError."""
    data = None
    if path is None and not os.getenv("BROKER_PROVENANCE_PATH"):   # an explicit roster path wins over broker.yaml
        data = site_env.section("provenance")
    if data is not None:
        path = f"{site_env.BROKER_CONFIG} provenance"
        if os.path.exists(_OVERRIDE_PATH):
            log.warning("provenance: broker.yaml sets the roster, %s is ignored", _OVERRIDE_PATH)
    path = path or _OVERRIDE_PATH
    if data is None and not os.path.exists(path):
        if os.getenv("BROKER_PROVENANCE_NEUTRAL") != "1":
            raise RuntimeError(f"provenance roster {path} missing, refusing to start (copy config/provenance."
                               "example.json, or set BROKER_PROVENANCE_NEUTRAL=1 for no site agents)")
        log.warning("provenance: no roster at %s (BROKER_PROVENANCE_NEUTRAL=1); only broker row identities, "
                    "pool-NN and %s classify as agent", path, _DEFAULT_AGENT_PATTERN)
        return _neutral_roster()
    try:
        if data is None:
            with open(path) as f:
                data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level must be an object")
        unknown = sorted(set(data) - _KNOWN_KEYS)
        if unknown:
            raise ValueError(f"unknown key(s) {unknown}")
        missing = [k for k in _REQUIRED_KEYS if k not in data]
        if missing:
            raise ValueError(f"missing required key(s) {missing}")
        out = {k: _str_set(data, k) for k in ("agent_basenames", "handoff_doers", "agent", "system",
                                              "untrusted_authors", "handoff_parked_labels")}
        pat = data["agent_pattern"]
        if not isinstance(pat, str) or not pat:
            raise ValueError("agent_pattern must be a non-empty string")
        site = re.compile(pat, re.IGNORECASE)
        if any(site.search(h) for h in _HUMAN_PROBES):
            raise ValueError(f"agent_pattern {pat!r} matches everything / plain human names")
        out["agent_pattern"] = re.compile(f"(?:{_DEFAULT_AGENT_PATTERN})|(?:{pat})", re.IGNORECASE)
        return out
    except (OSError, ValueError, re.error) as e:   # json.JSONDecodeError is a ValueError
        raise RuntimeError(f"provenance roster {path} is malformed, refusing to start: {e}") from e


def _agent_identity_forms(basenames):
    forms = set()
    for b in basenames:
        forms.add(b)
        forms.add(f"{b}-agent")
        forms.add(f"{b}-bot")
    return frozenset(forms)


_ROSTER = _load_roster()
_AGENT_BASENAMES = _ROW_BASENAMES | _ROSTER["agent_basenames"]
_AGENT_IDENTITIES = _agent_identity_forms(_AGENT_BASENAMES) | _ROSTER["agent"]
# Catch-all for any other agent/bus-address doer not named above: `-bot\b` OR the roster's agent_pattern.
_AGENT_PATTERN_RE = _ROSTER["agent_pattern"]
# The pooled TFA identities (pool-NN, Vikunja users) are workers, never humans.
# RESIDUAL RISK (documented, not fixed): pool users have NO Vikunja API tokens, so TFAs write Vikunja
# via `mcp.sh vikunja-bridge` AS svc-agent, indistinguishable from any other agent. _POOL_RE (and the handoff's
# run_id check below) therefore never match live traffic, and a TFA that assigns @dev passes
# is_agent_handoff and starts one code-worker run. Bounded to ONE hop: code workers cannot write Vikunja.
# Real fix: per-pool Vikunja tokens, so pool writes carry a pool-NN doer and _POOL_RE becomes live.
_POOL_RE = re.compile(r"^pool-\d+$")
_SYSTEM_IDENTITIES = _ROSTER["system"]   # designated system identities (roster "system")


def classify(doer):
    """doer -> "user" | "agent" | "system". Pure function of the string; case-insensitive."""
    if not doer or not str(doer).strip():
        return "system"          # unattributed: SAFE default, never treated as a human
    d = str(doer).strip().lower()
    if d in _SYSTEM_IDENTITIES:
        return "system"
    if d in _AGENT_IDENTITIES or _AGENT_PATTERN_RE.search(d) or _POOL_RE.match(d):
        return "agent"
    return "user"


def is_user(doer):
    return classify(doer) == "user"


def is_agent(doer):
    return classify(doer) == "agent"


def is_system(doer):
    return classify(doer) == "system"


# Agent -> @dev handoff. Every agent Vikunja write goes through the shared `svc-agent`
# account (the payload carries no per-agent identity), so an agent-assigned @dev ticket was dropped as an echo.
# Admit ONLY: doer in _HANDOFF_DOERS, a vikunja task.created/task.updated, a dispatch assignee among assignees,
# no run_id stamp, and not done. Everything else from an agent doer still drops. The caller (echo_guard) adds the
# last gate: no worker in flight on that subject. Loop safety: code workers cannot write Vikunja (raw mcp.sh
# denied; mcp-ro vikunja is get/list only) and the broker's own posts are comments (which are dropped).
# Standing fleet agents now write Vikunja as their OWN users, so the handoff doer set is
# the shared service account + the named standing agents, by EXACT Vikunja username (roster "handoff_doers"). A
# fixed set on purpose: no -bot / @<bus-domain> pattern forms, and never a pool-NN (worker/TFA echo) doer.
_HANDOFF_DOERS = _ROSTER["handoff_doers"]   # from the roster; empty (no roster) means no agent handoffs admitted
_HANDOFF_EVENTS = frozenset({"task.created", "task.updated"})
_HANDOFF_ASSIGNEE = os.getenv("DEV_ASSIGNEE", "eph-code").strip().lower()   # same knob as routing.DEV_ASSIGNEE
# ANY dispatch-row assignee admits (@dev code row, @eph general row, @fleet TFA row), using the same env
# knobs and defaults as routing.DEV_ASSIGNEE / FLEET_ASSIGNEE / FLEET_ASSIGNEE_ALT. echo_guard then checks the
# event actually ROUTES to the row its assignee names.
_HANDOFF_ASSIGNEES = frozenset(filter(None, (
    _HANDOFF_ASSIGNEE,
    os.getenv("FLEET_ASSIGNEE", "eph").strip().lower(),
    os.getenv("FLEET_ASSIGNEE_ALT", "").strip().lower(),
    # @infra-eph (infra/infra-general rows): same knob and default as routing.INFRA_ASSIGNEE
    os.getenv("INFRA_ASSIGNEE", "").strip().lower(),
)))
# Parked tickets never spawn (tap sends label TITLES; roster "handoff_parked_labels").
_HANDOFF_PARKED_LABELS = _ROSTER["handoff_parked_labels"]
# Never write-trusted even if in _HANDOFF_DOERS (roster "untrusted_authors"; consumed by write_grant).
_UNTRUSTED_AUTHORS = _ROSTER["untrusted_authors"]


def is_agent_handoff(evt):
    """True iff evt is a handoff-doer create/update of a Vikunja ticket assigned to a dispatch-row assignee
    (dev/eph/fleet/infra-eph). Fail-closed."""
    if not isinstance(evt, dict):
        return False
    if str(evt.get("doer") or "").strip().lower() not in _HANDOFF_DOERS:
        return False
    if evt.get("source") != "vikunja" or evt.get("event_name") not in _HANDOFF_EVENTS:
        return False
    if evt.get("task_id") is None or evt.get("run_id") or evt.get("done") is True:
        return False
    labels = evt.get("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    if _HANDOFF_PARKED_LABELS & {str(l).strip().lower() for l in labels}:
        return False
    assignees = evt.get("assignees") or []
    if isinstance(assignees, str):
        assignees = [assignees]
    return bool(_HANDOFF_ASSIGNEES & {str(a).strip().lower() for a in assignees})
