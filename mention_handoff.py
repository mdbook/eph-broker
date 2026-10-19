#!/usr/bin/env python3
"""mention_handoff.py: a human's @dev mention on a ticket dev isn't assigned to becomes a handoff.

Today a Vikunja `task.comment.created` that @mentions dev (`<mention-user data-id="dev" ...>`) on a ticket NOT
assigned to dev has no row, so ingest drops it ("INGEST REJECT (allowlist)") and the ping goes nowhere. With
MENTION_HANDOFF_ENABLED=1 the broker instead ASSIGNS dev (Vikunja user MENTION_HANDOFF_UID, default 13) through
its existing Vikunja write path (poster.assign_user -> mcp.sh as svc-agent). That assignment emits a svc-agent
`task.updated` carrying the dev assignee, which the normal agent-handoff path (provenance.is_agent_handoff +
echo_guard) admits and spawns. This module never spawns anything itself.

Fail-closed predicate (every clause must hold): vikunja comment event, numeric task_id, HUMAN doer
(provenance "user", not an agent bot / svc-agent), no run_id stamp or broker-run marker, the body carries an
HTML mention of MENTION_HANDOFF_USER, dev is not already among the event's assignees, AND the ticket's
project is in MENTION_HANDOFF_PROJECTS (site.env; comma-separated ids; unset -> none). DEFAULT OFF.
Known limit: the agent-handoff path refuses tickets with a parked label (awaiting-input/deferred/blocked), so a mention on a parked ticket assigns
dev but does not spawn until the label is removed.

Project gate: the tap's `task.comment.created` payload carries no confirmed project field
today (only task_id/comment/comment_id/run_id/doer; see routing.py's NON_TRIGGERING_EVENTS block), so
`_project_id` checks, in order: (1) a flat `project_id` the tap may enrich with, matching task_id's own flat
style; (2) the raw Vikunja webhook's nested `data.task.project_id` shape, in case an un-flattened event ever
reaches ingest; (3) the same task-details fetch the broker already uses at spawn time (ticket_fetch.fetch_ticket),
parsed for a rendered project field. Any project id that can't be determined this way is fail-closed to NO
handoff, same as an empty/unset MENTION_HANDOFF_PROJECTS list.
"""
import logging, os, re, threading, time

import provenance
import site_env  # noqa: F401  (loads config/site.env before the env reads below)
import ticket_fetch
from echo_guard import is_fleet_bot

log = logging.getLogger("eph-broker.mention_handoff")

MENTION_USER = os.getenv("MENTION_HANDOFF_USER", os.getenv("DEV_ASSIGNEE", "eph-code")).strip().lower()
# DEV_VIKUNJA_UID (site.env) belongs to the dev assignee only: an overridden MENTION_HANDOFF_USER without
# its own MENTION_HANDOFF_UID fails closed (0)
MENTION_UID = int(os.getenv("MENTION_HANDOFF_UID")
                  or ((os.getenv("DEV_VIKUNJA_UID") or "0")
                      if MENTION_USER == os.getenv("DEV_ASSIGNEE", "eph-code").strip().lower() else "0"))
DEDUP_S = float(os.getenv("MENTION_HANDOFF_DEDUP_S", "300"))
_MARKER_RE = re.compile(r"broker-run:", re.IGNORECASE)
# comma-separated Vikunja project ids allowed to handoff (style matches routing.ISSUE_AUTHOR_ALLOWLIST); a
# non-numeric token is dropped, not fatal. Empty or unset (the value lives in site.env) means no handoff at all.
MENTION_HANDOFF_PROJECTS = frozenset(
    int(x.strip()) for x in os.getenv("MENTION_HANDOFF_PROJECTS", "").split(",") if x.strip().isdigit()
)


def enabled():
    return os.getenv("MENTION_HANDOFF_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


# A mention inside quoted/code content (a pasted old comment, an example) is not a request. Vikunja
# comments arrive as tiptap HTML (<blockquote>, <pre><code>, <code>); API-posted ones may be markdown (``` fences,
# `inline`, `>` quote lines). Innermost-first HTML stripping so a nested blockquote can't unbalance the match.
_HTML_QUOTED_RE = re.compile(r"(?is)<(blockquote|pre|code)\b[^>]*>(?:(?!<\1\b).)*?</\1\s*>")
_MD_QUOTED_RE = re.compile(r"(?s)```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)|`[^`\n]*`|(?m:^[ \t]*(?:<p[^>]*>)?[ \t]*(?:>|&gt;).*$)")


def _unquoted(body):
    prev = None
    while prev != body:
        prev, body = body, _HTML_QUOTED_RE.sub(" ", body)
    return _MD_QUOTED_RE.sub(" ", body)


def _mention_re(user):
    return re.compile(r"<mention-user\b[^>]*\bdata-id\s*=\s*[\"']" + re.escape(user) + r"[\"']", re.IGNORECASE)


def _project_id(evt, tid):
    """Best-effort numeric project id for evt, or None if it can't be determined (see module docstring's
    "Project gate" section for the fallback order). None means the caller fails closed to no handoff."""
    data = evt.get("data")
    task = data.get("task") if isinstance(data, dict) else None
    for raw in (evt.get("project_id"), task.get("project_id") if isinstance(task, dict) else None):
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    try:
        text = ticket_fetch.fetch_ticket(tid)
    except Exception as e:                                # ticket_fetch itself never raises; belt and suspenders
        log.warning("mention handoff: ticket_fetch raised task=%s: %s", tid, e)
        text = None
    # Read the field via ticket_fetch.ticket_field (its anchoring contract), never the first regex
    # match on the rendered blob: a fake `**project_id:**` line in the description fails closed (None).
    raw = ticket_fetch.ticket_field(text, "project_id")
    return int(raw) if raw and raw.isdigit() else None


def wants_handoff(evt, user=None):
    """Should this ingest event become an assign-dev handoff? Returns the int task_id or None. Pure except
    for the project gate's ticket_fetch fallback (_project_id), which only runs for an otherwise-qualifying
    mention (see the project-gate comment below) and never raises."""
    user = (user or MENTION_USER).lower()
    if not isinstance(evt, dict) or evt.get("source") != "vikunja" \
            or evt.get("event_name") != "task.comment.created":
        return None
    try:
        tid = int(evt.get("task_id"))
    except (TypeError, ValueError):
        return None
    doer = evt.get("doer")
    if is_fleet_bot(doer) or provenance.classify(doer) != "user":
        return None
    body = str(evt.get("comment") or "")
    if MENTION_UID <= 0 or evt.get("run_id") or _MARKER_RE.search(body) \
            or not _mention_re(user).search(_unquoted(body)):
        return None
    assignees = evt.get("assignees") or []
    if isinstance(assignees, str):
        assignees = [assignees]
    if user in [str(a).strip().lower() for a in assignees]:
        return None                                   # already assigned: the normal paths own it
    # project gate last: only a genuine candidate mention pays for the ticket_fetch fallback below.
    # empty MENTION_HANDOFF_PROJECTS short-circuits _project_id entirely (no handoff at all).
    if not MENTION_HANDOFF_PROJECTS or _project_id(evt, tid) not in MENTION_HANDOFF_PROJECTS:
        return None
    return tid


class MentionHandoff:
    """Per-broker dedup (a tap retry / double delivery assigns once) + the off-ingest-thread assign call."""

    def __init__(self, assign):
        self._assign = assign                         # (task_id, user_id) -> bool   (poster.assign_user)
        self._seen = {}
        self._lock = threading.Lock()

    def maybe(self, key, evt):
        """Returns the started Thread when a handoff fires, else None. Never raises."""
        try:
            tid = wants_handoff(evt)
            if tid is None:
                return None
            now = time.time()
            with self._lock:
                if now - self._seen.get(tid, 0) < DEDUP_S:
                    log.info("mention handoff dedup: task=%s handed off %.0fs ago -> skip", tid, now - self._seen[tid])
                    return None
                self._seen = {t: ts for t, ts in self._seen.items() if now - ts < DEDUP_S}   # bounded
                self._seen[tid] = now
            log.info("mention handoff: %s @%s on task=%s (key=%s) -> assigning user %s",
                     evt.get("doer"), MENTION_USER, tid, key, MENTION_UID)
            t = threading.Thread(target=self._run, args=(tid,), name=f"mention-handoff-{tid}", daemon=True)
            t.start()
            return t
        except Exception as e:
            log.warning("mention handoff failed key=%s: %s", key, e)
            return None

    def _run(self, tid):
        try:
            ok = self._assign(tid, MENTION_UID)
        except Exception as e:
            ok = False
            log.warning("mention handoff assign raised task=%s: %s", tid, e)
        log.info("mention handoff assign task=%s -> %s", tid, "ok" if ok else "FAILED")
        if not ok:                                    # let a later mention retry
            with self._lock:
                self._seen.pop(tid, None)
