#!/usr/bin/env python3
"""Native GitHub Issues intake and summary comments (ticket 1151).

This first increment only admits issues.opened. PRs, comments and reviews are deliberately
ignored until there is a separate, read-only PR route. No payload URL is used for API calls.
Secrets are looked up by environment-variable NAME at call time; missing secrets fail closed.
"""
import hashlib
import hmac
import json
import logging
import os
import re
import urllib.request

import gitlab_mr
import poster
import tickets
from ticket_fetch import MAX_TICKET_TEXT
from trackers import Tracker

log = logging.getLogger("eph-broker.trackers.github")

GITHUB_WEBHOOK_SECRET_ENV = os.getenv("GITHUB_WEBHOOK_SECRET_ENV", "GITHUB_WEBHOOK_SECRET")
GITHUB_TOKEN_ENV = os.getenv("GITHUB_TOKEN_ENV", "GITHUB_TOKEN")
_REF_RE = re.compile(r"github:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([1-9][0-9]*)", re.ASCII)


def parse_ref(ref):
    """(owner/repo, number), or None; refuse path/query injection and noncanonical numbers."""
    if tickets.ref_from_key(ref) is None:
        return None
    m = _REF_RE.fullmatch(ref)
    if not m or any(p in (".", "..") for p in m[1].split("/")):
        return None
    return m[1].lower(), int(m[2])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                       # never forward the bearer token to a redirect target


class GitHubTracker(Tracker):
    name = kind = "github"

    def verify(self, headers, raw):
        secret = os.getenv(GITHUB_WEBHOOK_SECRET_ENV)
        signature = headers.get("X-Hub-Signature-256")
        if not secret or not isinstance(signature, str) or not isinstance(raw, bytes):
            return False
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected.encode(), signature.encode())

    def normalize(self, headers, payload):
        """Opened issues only, using the actor (not the issue author) for the existing author gate."""
        if headers.get("X-GitHub-Event") != "issues" or not isinstance(payload, dict) \
                or payload.get("action") != "opened":
            return []
        issue, repo, sender = (payload.get(k) for k in ("issue", "repository", "sender"))
        if not all(isinstance(v, dict) for v in (issue, repo, sender)):
            return []
        n, project, doer = issue.get("number"), repo.get("full_name"), sender.get("login")
        if type(n) is not int or n <= 0 or not isinstance(project, str) \
                or not isinstance(doer, str) or not doer or len(doer) > 100 \
                or any(c.isspace() or ord(c) < 32 for c in doer):
            return []
        ref = f"github:{project.lower()}#{n}"
        if parse_ref(ref) is None or "pull_request" in issue or issue.get("state") != "open":
            return []
        title, body = issue.get("title"), issue.get("body")
        if not isinstance(title, str) or not isinstance(body, (str, type(None))):
            return []
        evt = {"source": "github", "event_name": "issues.opened", "ref": ref,
               "repo": project.lower(), "issue": n, "title": title[:MAX_TICKET_TEXT],
               "body": (body or "")[:MAX_TICKET_TEXT], "web_url": self.url(ref),
               "doer": doer, "sender_type": sender.get("type")}
        return [] if self.is_self(evt) else [evt]

    def is_self(self, evt):
        bot = os.getenv("GITHUB_BOT_LOGIN", "").lower()
        return evt.get("sender_type") == "Bot" or bool(
            bot and str(evt.get("doer") or "").lower() == bot)

    def url(self, ref):
        p = parse_ref(ref)
        return f"https://github.com/{p[0]}/issues/{p[1]}" if p else ""

    def comment(self, ref, html_body, run_id):
        """Best-effort issue comment. No token, invalid ref or HTTP failure means no write.

        Keep the run marker for future comment intake; defuse mentions and cross-references just
        like GitLab issue notes. Never log response bodies, which may contain reflected input.
        """
        p, token = parse_ref(ref), os.getenv(GITHUB_TOKEN_ENV)
        if p is None or not token:
            return False
        try:
            body = poster.stamp(gitlab_mr.defuse_text(gitlab_mr.break_refs(
                str(html_body or "")[:MAX_TICKET_TEXT])), run_id)
            req = urllib.request.Request(
                f"https://api.github.com/repos/{p[0]}/issues/{p[1]}/comments",
                data=json.dumps({"body": body}).encode(), method="POST",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                         "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=15) as response:
                return response.status == 201
        except Exception as e:             # never wedge a broker turn on a lost comment
            log.warning("github comment failed for %s (%s)", ref, type(e).__name__)
        return False
