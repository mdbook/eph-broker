"""Open the GitLab MR for a code change-proposer's pushed feature branch.

worktree.publish_mr pushes the worker's committed HEAD to a fresh eph/code-change/* feature
branch and returns its name (or None on a no-op). This module does the ONE credentialed step that
publish_mr deliberately leaves out: open the branch -> mr_target_ref MR via the GitLab API so a human reviews and
merges. It NEVER merges; it only opens the review.

Secret hygiene (mirrors app.py's HMAC pattern): the API token is read from an env var at RUNTIME by NAME
only; no secret bytes live in this file. If the token env is unset, MR-open is skipped gracefully (the
branch is already up for manual review), so a first code ticket is never blocked; auto-MR-open just
lights up once the token is provisioned.
"""
import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import site_env

log = logging.getLogger("eph-broker.gitlab_mr")

# NAME only; the value is filled into the broker env separately. Never the value here.
GITLAB_MR_TOKEN_ENV = os.getenv("GITLAB_MR_TOKEN_ENV", "GITLAB_MR_TOKEN")
GITLAB_API = site_env.gitlab_api_url()   # GITLAB_API / GITLAB_WEB_URL (site.env)
_HTTP_TIMEOUT = float(os.getenv("GITLAB_MR_TIMEOUT", "15"))


def _project_path(repo_path):
    """namespace/repo (incl nested subgroups) from the repo's origin remote (no hardcoded project).

    Full path after the host, so arbitrary subgroup nesting (group/sub/repo) derives correctly; a silent
    wrong-project derive is worse than a loud miss."""
    url = subprocess.run(["git", "-C", repo_path, "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    if "://" in url:                       # https://host/group/sub/repo(.git)
        path = urllib.parse.urlparse(url).path
    else:                                  # scp-like git@host:group/sub/repo(.git)
        path = url.split(":", 1)[-1]
    path = path.strip("/")
    path = path[:-4] if path.endswith(".git") else path
    return path or None


# GitLab runs quick actions (/merge, /approve, /close, ...) found in a note or description created through the
# API, as the token's user. A line counts only when `/` is its first character, so a zero-width space before a
# line-leading `/` (after optional spaces or tabs, at the start, or after a newline or carriage return) turns the
# command into plain text that renders the same. _api applies it to every text field it writes, so worker and
# reviewer text (untrusted) can't reach GitLab undefused, whoever the caller is. The space also lands inside fenced
# code blocks: harmless to read, but a command copied out of one carries it. A write that could carry text past
# this (a non-dict body, or a text field in the query string) is refused.
_QUICK_ACTION_RE = re.compile(r"(?m)(^|\r)([ \t]*)/")
# An @mention (`@all`, `@group`, `@user`) in a text field gets a zero-width space after the `@`, so worker or
# reviewer text can't ping anyone. An email address (a word character before the `@`) is left alone.
_MENTION_RE = re.compile(r"(?<![\w.])@(?=[\w-])")
_TEXT_FIELDS = ("body", "note", "description", "title", "commit_message", "squash_commit_message",
                "merge_commit_message")


def defuse_quick_actions(text):
    return _QUICK_ACTION_RE.sub("\\1\\2\u200b/", str(text or ""))


def defuse_text(text):
    """Quick actions and @mentions both made inert; what _api applies to every text field."""
    return _MENTION_RE.sub("@\u200b", defuse_quick_actions(text))


# GitLab links references (#7, group/app#7, !3, &2, an issue, MR or work-item URL) and reads closing patterns
# (`Closes #7`, `fixes https://host/g/p/-/work_items/7`) in a description. break_refs puts a zero-width space
# before every digit that follows `#`, `!`, `&` or `/`, so untrusted text can't cross-reference, close or spam
# anything. A numeric HTML entity (`&#8212;`) is left intact.
_REF_RE = re.compile(r"((?<!&)#|!|&|/)(?=[0-9])")


def break_refs(text):
    return _REF_RE.sub("\\1\u200b", str(text or ""))


def _api(method, path, token, body=None):
    if not GITLAB_API:
        raise RuntimeError("GITLAB_API / GITLAB_WEB_URL unset (site.env)")
    url = f"{GITLAB_API}{path}"
    if method != "GET":
        if body is not None and not isinstance(body, dict):
            raise ValueError(f"GitLab {method} {path.split('?', 1)[0]}: body must be a dict")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query, keep_blank_values=True)
        if {k.split("[", 1)[0] for k in query} & set(_TEXT_FIELDS):
            raise ValueError(f"GitLab {method} {path.split('?', 1)[0]}: text fields go in the body, not the query")
    if isinstance(body, dict):
        body = {k: defuse_text(v) if k in _TEXT_FIELDS and isinstance(v, str) else v
                for k, v in body.items()}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"PRIVATE-TOKEN": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        return json.loads(r.read() or "null")


# Backoff (seconds) for the push -> API race on a just-pushed source branch (~15s total).
_SOURCE_BRANCH_RETRY_DELAYS = (1, 2, 4, 8)


def _sleep(s):
    """Indirection so tests can skip the real backoff."""
    time.sleep(s)


class _ReadHTTPError(Exception):
    """An HTTPError whose body was already consumed (the body can only be read once)."""
    def __init__(self, code, detail):
        super().__init__(f"HTTP {code}: {detail}")
        self.code, self.detail = code, detail


def _is_missing_source_branch(code, detail):
    return code == 400 and "source_branch" in detail and "does not exist" in detail


def _escalate_lost_mr(project, branch, mr_target_ref, key, detail):
    """Alert the operator so an MR that could not be opened is never silent. NEVER raises."""
    try:
        import notify
        notify.wake_ops(f"open_mr: MR NOT opened for {project}:{branch} -> {mr_target_ref} (key={key}); "
                        f"GitLab kept answering 400 source_branch does not exist after retries. The branch "
                        f"may be on the remote - open the MR by hand. {detail}", subject=key or branch)
    except Exception as e:
        log.warning("open_mr: escalation DM failed for %s: %s", branch, e)


def open_mr(repo_path, branch, mr_target_ref, *, key=None, title=None, description=None):
    """Open (idempotently) the MR branch -> mr_target_ref for the repo at repo_path.

    Returns a dict {"web_url", "iid", "project"} on success (both the freshly-created AND the
    idempotent-existing paths), or None if skipped (no token) / on a handled error. The iid and project let
    a caller drive follow-ups (e.g. the MR reviewer's synthetic mr.opened event / an MR note). Never
    raises into the broker turn: a failed MR-open must not wedge a run (the branch is already the
    durable deliverable)."""
    token = os.getenv(GITLAB_MR_TOKEN_ENV)
    if not token:
        log.warning("open_mr: %s unset -> MR-open skipped; branch %r is up for manual review "
                    "(give the token MR-create scope to auto-open MRs)", GITLAB_MR_TOKEN_ENV, branch)
        return None
    project = _project_path(repo_path)
    if not project:
        log.error("open_mr: could not derive project from origin of %s -> skipped", repo_path)
        return None
    pid = urllib.parse.quote(project, safe="")
    q = urllib.parse.urlencode({"source_branch": branch, "target_branch": mr_target_ref, "state": "opened"})
    try:
        existing = _api("GET", f"/projects/{pid}/merge_requests?{q}", token)
        if existing:  # idempotent: a re-run for the same branch reuses the open MR
            log.info("open_mr: MR already open for %s:%s -> %s", project, branch, existing[0].get("web_url"))
            return {"web_url": existing[0].get("web_url"), "iid": existing[0].get("iid"), "project": project}
        body = {
            "source_branch": branch,
            "target_branch": mr_target_ref,
            "title": title or f"[eph] code change proposal: {key or branch}",
            "description": description or (
                f"Automated code-change proposal from the ephemeral code worker (key={key}).\n\n"
                f"Branch `{branch}` -> `{mr_target_ref}`. Review the diff and merge if good; "
                f"this MR was opened by the broker but **nothing merges without a human**."),
            "remove_source_branch": True,
        }
        # GitLab can answer 400 {"source_branch":["does not exist"]} for a branch pushed seconds ago
        # (push -> ref visible to the API is not instant). Retry ONLY that case with a short backoff; if it
        # never shows up, alert the operator so a lost MR is never silent (the journal alone tends to go unread).
        delays = list(_SOURCE_BRANCH_RETRY_DELAYS)
        while True:
            try:
                mr = _api("POST", f"/projects/{pid}/merge_requests", token, body)
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if not _is_missing_source_branch(e.code, detail):
                    raise _ReadHTTPError(e.code, detail) from e
                if not delays:
                    log.error("open_mr: source branch %r still missing on %s after retries -> %s",
                              branch, project, detail)
                    _escalate_lost_mr(project, branch, mr_target_ref, key, detail)
                    return None
                d = delays.pop(0)
                log.warning("open_mr: 400 source_branch does not exist for %s:%s (push race?) -> "
                            "retry in %ss", project, branch, d)
                _sleep(d)
        url = mr.get("web_url")
        log.info("open_mr: opened MR %s:%s -> %s (%s)", project, branch, mr_target_ref, url)
        return {"web_url": url, "iid": mr.get("iid"), "project": project}
    except (urllib.error.HTTPError, _ReadHTTPError) as e:
        detail = e.detail if isinstance(e, _ReadHTTPError) else e.read().decode(errors="replace")[:300]
        if e.code == 403:
            log.error("open_mr: 403 for %s -> the token lacks MR-create (api/MR-write) scope. "
                      "Branch %r is up for manual review. %s", project, branch, detail)
        else:
            log.error("open_mr: GitLab API %s for %s:%s -> %s", e.code, project, branch, detail)
        return None
    except Exception as e:  # never wedge a broker turn on MR-open
        log.exception("open_mr: unexpected failure for %s:%s -> %s", project, branch, e)
        return None


def post_mr_note(project, mr_iid, body, run_id=None, harness=None):
    """MR reviewer verdict sink: post `body` as a note on project!mr_iid.

    Mirrors open_mr's secret hygiene + error handling: the token is read from GITLAB_MR_TOKEN_ENV by
    NAME at runtime (no secret bytes here); if unset, the post is SKIPPED gracefully (returns None) so a
    missing token never wedges a review. NEVER raises into the broker turn. Returns the note id/url on
    success, else None. `run_id`, when given, is appended as an HTML marker (like poster.stamp) so the
    broker's own MR note is self-identifiable. `harness`, when given, is stamped into that same

    hidden marker as `harness=<name>` (the broker's Route.adapter, never model output) so a hermes-vs-cc
    verdict audit can read the harness off the note instead of inferring it from wording."""
    token = os.getenv(GITLAB_MR_TOKEN_ENV)
    if not token:
        log.warning("post_mr_note: %s unset -> MR-note skipped for %s!%s", GITLAB_MR_TOKEN_ENV, project, mr_iid)
        return None
    if not project or mr_iid is None:
        log.error("post_mr_note: missing project/mr_iid (%r!%r) -> skipped", project, mr_iid)
        return None
    tag = f"{run_id} harness={harness}" if harness else f"{run_id}"
    marked = f"{body}\n<!-- broker-run:{tag} -->" if run_id else body
    pid = urllib.parse.quote(str(project), safe="")
    try:
        note = _api("POST", f"/projects/{pid}/merge_requests/{mr_iid}/notes", token, {"body": marked})
        note_id = note.get("id") if isinstance(note, dict) else None
        log.info("post_mr_note: posted note on %s!%s (id=%s)", project, mr_iid, note_id)
        return note_id
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log.error("post_mr_note: GitLab API %s for %s!%s -> %s", e.code, project, mr_iid, detail)
        return None
    except Exception as e:  # never wedge a broker turn on an MR-note post
        log.exception("post_mr_note: unexpected failure for %s!%s -> %s", project, mr_iid, e)
        return None
