#!/usr/bin/env python3
"""poster.py: post the worker's result-envelope summary as a Vikunja ticket comment.

The broker's EnvelopeParser already extracts {status, summary, wake} out of a worker's
result, but nothing ever POSTS `summary` anywhere: the worker is charter-free and tool-minimal (no
Vikunja MCP) and cannot comment on the ticket itself. This module is the missing sink: it shells out
to `mcp.sh vikunja-bridge` (the bridge holds the scoped write token) to post `summary` (the envelope's
HTML comment body) onto the originating ticket.

VALUE-FREE: no secrets/credentials here; mcp.sh owns auth. Structured as a pure argv/json builder
(unit-testable without executing anything) plus a thin exec wrapper that shells out and NEVER raises
(a poster failure is logged and swallowed: the worker's turn already happened, and a lost comment post
is not a lost turn and must never wedge the worker path).

The posted body is stamped with an HTML marker `<!-- broker-run:<run_id> -->` so echo_guard's
run_id-based echo filter (see echo_guard.RunRegistry / EchoGuard.is_echo) can positively match the
broker's OWN comment by run_id, as belt and suspenders on top of the agent-bot `doer` drop that already
covers this at ingest.
"""
import json, logging, os, subprocess

import site_env  # noqa: F401  (loads config/site.env before the BROKER_MCP_SH / PARKED_LABEL_ID reads below)

log = logging.getLogger("eph-broker.poster")

MCP_SH = os.getenv("BROKER_MCP_SH", "")   # from site.env; unset means every call fails to exec (logged, False)
TOOL_NAME = "vikunja_task_comments"   # confirmed live via `mcp.sh vikunja-bridge tools/list`:
                                       # operation enum incl. "comment" (create); requires id + comment


def stamp(html, run_id):
    """Append the echo-guard run_id marker to the HTML comment body."""
    marker = f"<!-- broker-run:{run_id} -->"
    return f"{html}\n{marker}" if html else marker


def build_call(task_id, html, run_id):
    """Pure builder: the mcp.sh `tools/call` JSON payload for posting a comment. Unit-testable
    without executing anything. Body key is `comment` (not `text`) per the live
    vikunja_task_comments schema: operation "comment" + id + comment are the create shape.

    `id` is coerced to int (as in ticket_fetch.build_call): the bridge's schema
    types it `number`, and callers can pass a numeric STRING (e.g. a stringified task id from a rerun,
    or a comment event's native task_id). An uncoerced string is rejected with
    -32602 BEFORE the tool runs, so the comment is lost."""
    return {
        "name": TOOL_NAME,
        "arguments": {
            "operation": "comment",
            "id": int(task_id),
            "comment": stamp(html, run_id),
        },
    }


def _is_success(resp):
    """Pure classifier: does an mcp.sh JSON-RPC response represent success?

    Two distinct failure shapes to catch: a transport-level top-level `error` key, AND a
    tool-level failure surfaced as `result.isError == true` (e.g. bad id / permission). The
    latter is how `mcp.sh vikunja-bridge` reports a failed vikunja_task_comments call, and a
    naive "no top-level error" check would misread it as success."""
    if not isinstance(resp, dict):
        return False
    if "error" in resp:
        return False
    result = resp.get("result")
    if not isinstance(result, dict):
        # A malformed/degenerate `result` (missing/null/wrong-type) is not an *apparent success*;
        # we require the well-formed shape to positively confirm success, never assume it.
        return False
    if result.get("isError"):
        return False
    return True


def post_comment(task_id, html, run_id, mcp_sh=MCP_SH):
    """Exec wrapper: shells out to `mcp.sh vikunja-bridge tools/call <json>`. Returns True on
    apparent success, False on ANY failure (logged, never raised). mcp.sh owns auth; no secrets
    touched here."""
    if task_id is None:
        log.warning("poster: no task_id on brief -> skipping comment post (run_id=%s)", run_id)
        return False
    try:
        tid = int(task_id)          # int() coercion can raise on a non-numeric id; never wedge the post
    except (TypeError, ValueError):
        log.warning("poster: non-numeric task_id=%r -> skipping comment post (run_id=%s)", task_id, run_id)
        return False
    call = build_call(tid, html, run_id)
    try:
        out = subprocess.run(
            [mcp_sh, "vikunja-bridge", "tools/call", json.dumps(call)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("poster: mcp.sh exec failed task_id=%s run_id=%s: %s", task_id, run_id, e)
        return False
    if out.returncode != 0:
        log.warning("poster: mcp.sh exit=%d task_id=%s run_id=%s stderr=%s",
                    out.returncode, task_id, run_id, (out.stderr or "").strip()[:500])
        return False
    try:
        resp = json.loads(out.stdout or "{}")
    except (ValueError, TypeError) as e:
        log.warning("poster: mcp.sh non-JSON response task_id=%s run_id=%s: %s", task_id, run_id, e)
        return False
    if not _is_success(resp):
        log.warning("poster: mcp.sh reported failure task_id=%s run_id=%s: %s",
                    task_id, run_id, resp)
        return False
    log.info("poster: posted comment task_id=%s run_id=%s", task_id, run_id)
    return True


def assign_user(task_id, user_id, mcp_sh=MCP_SH):
    """Add Vikunja user `user_id` as an assignee of `task_id` via `vikunja_task_assignees` "assign"
    (ADDS an assignee, never replaces). Both ids are schema-typed `number` (a string is -32602-rejected).
    Same contract as post_comment: True on apparent success, False on ANY failure (logged, never raised)."""
    try:
        call = {"name": "vikunja_task_assignees",
                "arguments": {"operation": "assign", "id": int(task_id), "assignees": [int(user_id)]}}
        out = subprocess.run([mcp_sh, "vikunja-bridge", "tools/call", json.dumps(call)],
                             capture_output=True, text=True, timeout=30)
        resp = json.loads(out.stdout or "{}") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as e:
        log.warning("poster: assign exec failed task_id=%s user_id=%s: %s", task_id, user_id, e)
        return False
    if not _is_success(resp):
        log.warning("poster: assign failed task_id=%s user_id=%s: %s", task_id, user_id, resp)
        return False
    log.info("poster: assigned task_id=%s -> user_id=%s", task_id, user_id)
    return True


# The parked label is applied to a ticket whenever a worker result comes back `status:"blocked"` (a ticket
# parked on the operator always
# carries this label). The id is site config (PARKED_LABEL_ID in config/site.env); unset/0 means no label.
_PARKED = (os.getenv("PARKED_LABEL_ID") or "").strip()
PARKED_LABEL = int(_PARKED) if _PARKED.isdigit() else 0


def apply_label(task_id, label_id=PARKED_LABEL, mcp_sh=MCP_SH):
    """Apply a Vikunja label (default: the parked label, PARKED_LABEL_ID) to `task_id` via `vikunja_task_labels`
    "apply-label" (ADDS a label; the live Vikunja apply-label is itself idempotent: re-applying an
    already-present label is a no-op, not an error, so callers may call this unconditionally on every
    rejected). Same contract as assign_user/post_comment: True on apparent success, False on ANY
    failure (logged, never raised). A label_id of 0 (PARKED_LABEL_ID unset) is logged, with no call."""
    if not label_id:
        log.warning("poster: PARKED_LABEL_ID unset, task_id=%s not labelled", task_id)
        return False
    try:
        call = {"name": "vikunja_task_labels",
                "arguments": {"operation": "apply-label", "id": int(task_id), "labels": [int(label_id)]}}
        out = subprocess.run([mcp_sh, "vikunja-bridge", "tools/call", json.dumps(call)],
                             capture_output=True, text=True, timeout=30)
        resp = json.loads(out.stdout or "{}") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as e:
        log.warning("poster: apply_label exec failed task_id=%s label_id=%s: %s", task_id, label_id, e)
        return False
    if not _is_success(resp):
        log.warning("poster: apply_label failed task_id=%s label_id=%s: %s", task_id, label_id, resp)
        return False
    log.info("poster: applied label %s -> task_id=%s", label_id, task_id)
    return True
