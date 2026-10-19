#!/usr/bin/env python3
"""write_grant.py: default-on live-write grants for the infra-eph rows.

At spawn of an infra row (routing._INFRA_ROW / _INFRA_GENERAL_ROW, domain "infra") the broker decides,
DETERMINISTICALLY and with ZERO LLM in the path, whether the worker gets a write grant. If so it writes a
grant file {ticket, bundles, nonce, expires} that the infra repo's `mcp-rw.sh` wrapper
checks before it passes a write tool through. cc.capability_tool_grant allow-lists `Bash(mcp-rw.sh:*)` ONLY
when a grant file was written for this worker; with no grant the wrapper token stays on the deny-floor.

Decision (make_grant), every input broker-side (never worker-derived):
  * the rollback flag EPH_WRITE_GRANTS_ENABLED is on (DEFAULT OFF until the canary; it rides the live
    20-flags.conf drop-in, read at call time so a drop-in edit plus restart flips it);
  * provenance is TRUSTED: the ticket's creator (off the fetched ticket's `created_by`) AND the event doer
    that assigned it (brief["last_doer"], when present) are each the owner or a named standing
    fleet agent.
    Trusted set = routing.model_label_trusted_creators() (site MODEL_LABEL_TRUSTED_CREATORS, the model-label
    check) + the named standing agents in provenance._HANDOFF_DOERS (the handoff check), MINUS the roster's
    untrusted_authors (e.g. a shared service account; pool TFAs write Vikunja as svc-agent, so it
    is not a trustworthy author for a live-write grant). An unknown or absent
    creator means NO grant (fail-closed);
  * the ticket does not carry the opt-out label `write:none`.
Then bundles = DEFAULT_BUNDLES. Anything else gives None (no grant; the worker keeps its read-only bridges).

Value-free: identity NAMES only, no secrets. The grant file holds no credential; the wrapper owns auth.
"""
import json, os, re, secrets, subprocess, time, logging

import provenance
import routing

log = logging.getLogger("eph-broker.write-grant")

DEFAULT_BUNDLES = ("pve-guest-ops", "grafana-edit", "n8n-workflow-edit")
OPT_OUT_LABEL = "write:none"
AUDIT_TAG = "mcp-rw"                     # journal SYSLOG_IDENTIFIER the mcp-rw.sh wrapper logs under
AUDIT_MAX_LINES = 50                     # cap on audit lines carried into the closing comment
_UNTRUSTED_AUTHORS = provenance._UNTRUSTED_AUTHORS   # roster "untrusted_authors"


def enabled():
    """Rollback flag (DEFAULT OFF). Read at call time."""
    return os.getenv("EPH_WRITE_GRANTS_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def grant_dir():
    return os.getenv("EPH_WRITE_GRANT_DIR", os.path.expanduser("~/.local/state/eph-broker/write-grants"))


def grant_ttl_s():
    try:
        return max(60, int(os.getenv("EPH_WRITE_GRANT_TTL_S", "3600")))
    except ValueError:
        return 3600


def trusted_identities():
    return (set(routing.model_label_trusted_creators()) | set(provenance._HANDOFF_DOERS)) - _UNTRUSTED_AUTHORS


def trusted_provenance(creator, assigner=None):
    """True iff the creator is trusted AND the assigner (when known) is trusted. Pure, fail-closed."""
    trusted = trusted_identities()
    c = str(creator or "").strip().lower()
    if not c or c not in trusted:
        return False
    a = str(assigner or "").strip().lower()
    return not a or a in trusted


def make_grant(task_id, labels, creator, assigner=None, now=None):
    """-> grant dict {ticket, bundles, nonce, expires} or None. Pure apart from the nonce/clock + flag read."""
    if not enabled() or task_id is None:
        return None
    if OPT_OUT_LABEL in {str(l).strip().lower() for l in (labels or [])}:
        return None
    if not trusted_provenance(creator, assigner):
        return None
    now = time.time() if now is None else now
    return {"ticket": int(task_id), "bundles": list(DEFAULT_BUNDLES),
            "nonce": secrets.token_hex(16), "expires": int(now + grant_ttl_s())}


def write_grant_file(grant, directory=None):
    """Atomically write the grant (mode 0600, dir 0700) as <dir>/<ticket>.json. -> path."""
    d = directory or grant_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, f"{int(grant['ticket'])}.json")
    tmp = f"{path}.{grant['nonce']}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(grant, f)
    os.replace(tmp, path)
    return path


def remove_grant_file(path):
    """Best-effort revoke at worker teardown. Never raises."""
    try:
        if path:
            os.unlink(path)
    except OSError:
        pass


def _journal_reader(since_epoch):
    out = subprocess.run(["journalctl", "-t", AUDIT_TAG, "--since", f"@{int(since_epoch)}",
                          "-o", "short-iso", "--no-pager", "-q"],
                         capture_output=True, text=True, timeout=15)
    return out.stdout if out.returncode == 0 else None


def audit_lines(task_id, nonce, since_epoch, reader=None):
    """This ticket's mcp-rw audit lines since `since_epoch` (journal tag mcp-rw), capped. A line belongs to
    the ticket if it carries the grant nonce or a `ticket=<id>` / `ticket: <id>` field. -> list[str], or None
    when the journal could not be read."""
    try:
        raw = (reader or _journal_reader)(since_epoch)
    except Exception as e:                                 # noqa: BLE001; an audit read never breaks a post
        log.warning("mcp-rw audit read failed task=%s: %s", task_id, e)
        return None
    if raw is None:
        return None
    tid_re = re.compile(rf"\bticket[=:]\s*#?{int(task_id)}\b")
    lines = [l for l in raw.splitlines() if l.strip() and ((nonce and nonce in l) or tid_re.search(l))]
    return lines[-AUDIT_MAX_LINES:]
