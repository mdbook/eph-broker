#!/usr/bin/env python3
"""forge.py: the GitLab issue -> broker ingest tap (broker side).

GitLab project webhooks ("Issues events") POST GitLab's raw Issue Hook payload. This module turns that into
the normalized issue event the broker already routes (coalescer.normalize / collapse / routing._ISSUE_CODE_ROW),
so the upstream hop (an n8n forwarder, or GitLab direct if the reverse proxy's IP guard is ever widened) is a
dumb pass-through: body as-is plus the X-Gitlab-Token header. The mapping lives here, versioned and tested,
instead of in an n8n expression.

Gates, in order (app.Handler._forge_gitlab):
  1. X-Gitlab-Token == the secret in env GITLAB_WEBHOOK_TOKEN_ENV (NAME only; the value is filled in separately).
     FAIL-CLOSED: unset means every request is rejected (unlike the HMAC stub, which is fail-open): this path
     spawns a WRITE worker from forge text, so an unauthenticated tap is not an acceptable default.
  2. normalize_gitlab_issue: a non-issue or malformed payload gives None (acked 200 and ignored, so GitLab
     does not auto-disable the hook over expected non-issue deliveries).
  3. ISSUE_AUTHOR_ALLOWLIST (routing.py): an issue whose author (user.username) is not listed is acked 200
     and ignored (one log line: author + project, no body). FAIL-CLOSED: unset means no author is listed, so
     every issue is ignored. Router.classify checks the same list again for any forge-shaped event.
  4. the existing broker gates (echo-guard on `doer`, forge-event allowlist ISSUE_TRIGGERING_EVENTS =
     issue-OPEN only, ISSUE_REPO_MAP fail-closed at spawn).

Env knobs:
  GITLAB_WEBHOOK_TOKEN_ENV  NAME of the env var holding the X-Gitlab-Token secret (default GITLAB_WEBHOOK_TOKEN).
  ISSUE_AUTHOR_ALLOWLIST    comma-separated usernames allowed to trigger (e.g. "owner"); unset/empty = nobody.
  ISSUE_REPO_MAP / ISSUE_TRIGGERING_EVENTS  see routing.py.

Value-free: no secrets, no network calls.
"""
import hmac, os

import routing
from ticket_fetch import MAX_TICKET_TEXT

GITLAB_WEBHOOK_TOKEN_ENV = os.getenv("GITLAB_WEBHOOK_TOKEN_ENV", "GITLAB_WEBHOOK_TOKEN")   # NAME only


def author_allowed(username, allowlist=None):
    """routing.issue_author_allowed: True iff `username` is listed; an empty allowlist admits nobody."""
    return routing.issue_author_allowed(username, allowlist)


def verify_gitlab_token(header_val):
    """Constant-time compare of X-Gitlab-Token against the configured secret. FAIL-CLOSED: an unset/empty
    secret or a missing header -> False."""
    secret = os.getenv(GITLAB_WEBHOOK_TOKEN_ENV)
    if not secret or not header_val:
        return False
    return hmac.compare_digest(secret.encode(), str(header_val).encode())


def normalize_gitlab_issue(payload):
    """GitLab Issue Hook payload -> broker issue event, or None if it is not a usable issue event.

    event_name = "issue.<action>" (GitLab object_attributes.action: open/reopen/update/close), so the
    default ISSUE_TRIGGERING_EVENTS ("issue.open") admits ONLY a fresh open; everything else is dropped at
    classify. project = path_with_namespace (the ISSUE_REPO_MAP key is "gitlab:<path_with_namespace>").
    doer = the GitLab username (echo-guard drops agent-bot doers). Body capped at MAX_TICKET_TEXT like a
    fetched ticket. Pure function of its input."""
    if not isinstance(payload, dict) or payload.get("object_kind") != "issue":
        return None
    attrs = payload.get("object_attributes") or {}
    project = (payload.get("project") or {}).get("path_with_namespace")
    iid = attrs.get("iid")
    if not project or iid is None:
        return None
    return {
        "source": routing.GITLAB_ISSUE_SOURCE,
        "event_name": f"issue.{attrs.get('action') or 'unknown'}",
        "project": project,
        "issue_iid": iid,
        "title": attrs.get("title") or "",
        "body": (attrs.get("description") or "")[:MAX_TICKET_TEXT],
        "web_url": attrs.get("url"),
        "doer": (payload.get("user") or {}).get("username"),
    }
