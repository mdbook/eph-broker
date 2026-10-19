#!/usr/bin/env python3
"""trackers/gitlab.py: write-back to GitLab issues (docs/trackers.md, step 3, write half).

Intake stays in forge.py (`POST /webhooks/gitlab`). This adapter only answers on the issue: `comment` posts an
issue note and `url` links it. The token is read by NAME at call time: `GITLAB_ISSUE_TOKEN`
(`GITLAB_ISSUE_TOKEN_ENV`), falling back to the MR token, so an install that opens MRs also comments without a
second secret. No token, no call. The write goes through gitlab_mr._api, which defuses quick actions and
@mentions.
Fetch, label and assign keep the fail-closed defaults.
"""
import logging
import os
import re
import urllib.error
import urllib.parse

import gitlab_mr
import poster
import site_env
from trackers import Tracker

log = logging.getLogger("eph-broker.trackers.gitlab")

GITLAB_ISSUE_TOKEN_ENV = os.getenv("GITLAB_ISSUE_TOKEN_ENV", "GITLAB_ISSUE_TOKEN")   # NAME only
_REF_RE = re.compile(r"^gitlab:(?P<project>[^#]+)#(?P<iid>[0-9]+)$")


def parse_ref(ref):
    """(project, iid) for a `gitlab:group/app#7` ref, or None for anything else (never raises)."""
    m = _REF_RE.match(ref) if isinstance(ref, str) else None
    return (m.group("project"), int(m.group("iid"))) if m else None


def _token():
    return os.getenv(GITLAB_ISSUE_TOKEN_ENV) or os.getenv(gitlab_mr.GITLAB_MR_TOKEN_ENV)


class GitLabTracker(Tracker):
    name = kind = "gitlab"

    def url(self, ref):
        p = parse_ref(ref)
        web = site_env.gitlab_web_url()
        return f"{web}/{p[0]}/-/issues/{p[1]}" if p and web else ""

    def comment(self, ref, html_body, run_id):
        """Post `html_body` (GitLab renders the HTML subset the broker writes) as a note on the issue, stamped
        with the broker-run marker. The body's references are broken (gitlab_mr.break_refs) so a worker summary
        can't cross-link other issues or MRs; _api defuses quick actions and @mentions. False on a bad ref, no
        token or any API error; never raises."""
        p = parse_ref(ref)
        token = _token()
        if p is None or not token:
            if p is not None:
                log.info("gitlab comment skipped for %s: no %s or %s", ref, GITLAB_ISSUE_TOKEN_ENV,
                         gitlab_mr.GITLAB_MR_TOKEN_ENV)
            return False
        pid = urllib.parse.quote(p[0], safe="")
        try:
            gitlab_mr._api("POST", f"/projects/{pid}/issues/{p[1]}/notes", token,
                           {"body": poster.stamp(gitlab_mr.break_refs(html_body), run_id)})
            return True
        except urllib.error.HTTPError as e:
            log.error("gitlab comment: API %s for %s -> %s", e.code, ref, e.read().decode(errors="replace")[:300])
        except Exception as e:                     # never wedge a broker turn on a comment
            log.warning("gitlab comment failed for %s: %s", ref, e)
        return False
