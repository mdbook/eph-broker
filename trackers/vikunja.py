#!/usr/bin/env python3
"""trackers/vikunja.py: the Vikunja tracker (docs/trackers.md, step 2).

A thin wrapper over the existing internals: poster.py (comment, label, assign) and ticket_fetch.py (the
rendered `get` blob and its fail-closed parsers). The ref becomes the integer task id at the edge
(tickets.vikunja_id) and the internals get exactly the arguments they got before refs, so every mcp.sh call
is byte-identical. A ref that isn't a Vikunja ticket makes no call.

The internals are injectable (CCAdapter passes its own comment_poster / ticket_fetcher / label_poster, which
take an int task id). The defaults look the module function up at CALL time, so a test that patches
poster.post_comment or ticket_fetch.fetch_ticket still lands here.
"""
import re

import poster
import site_env
import ticket_fetch
import tickets
from trackers import Ticket, Tracker


# Done-ticket drop: the `vikunja_task_crud` "get" rendering (ticket_fetch._extract_text)
# emits the task's done state as a LINE-ANCHORED bold field: `**done:** true` / `**done:** false`, inline
# in the same blob it returns as the worker brief. This matches ONLY a positively-true done field at line
# start, so a mid-line "…done: true…" in a description can never match (fail-safe toward NOT dropping).
# (?i) added: a formatter drift to `**Done:**` / `**done:** True` still drops. Still
# line-anchored (?m)^ and still requires a positive `true`; case-insensitivity does NOT loosen either.
_TICKET_DONE_RE = re.compile(r'(?im)^\*\*done:\*\*\s+true\b')


def _ticket_text_is_done(ticket_text):
    """Pure predicate: True IFF the fetched vikunja-bridge ticket blob shows the task is DONE. FAIL-SAFE:
    returns True ONLY on a positively-observed line-anchored `**done:** true`; None/empty/absent-field/false
    all return False, so a fetch miss or an unparseable body NEVER drops real work (only a done ticket does).
    The done field is read via ticket_fetch.ticket_done (the anchoring contract), NOT a first-match
    regex over the blob; a fake `**done:** true` line in a description can no longer drop the spawn."""
    return bool(ticket_text) and bool(_TICKET_DONE_RE.search(ticket_text)) and ticket_fetch.ticket_done(ticket_text)


# The title renders before the description in both the get blob and the list-fallback blob, so the first
# line-anchored match is the real one. Display only: no trust decision reads it.
_TITLE_RE = re.compile(r'(?im)^\*\*title:\*\*[ \t]*(.*)$')


class VikunjaTracker(Tracker):
    name = kind = "vikunja"

    def __init__(self, post_comment=None, fetch_text=None, apply_label=None, assign_user=None):
        self._post_comment = post_comment or (lambda tid, html, run_id: poster.post_comment(tid, html, run_id))
        self._fetch_text = fetch_text or (lambda tid: ticket_fetch.fetch_ticket(tid))
        self._apply_label = apply_label or (lambda tid: poster.apply_label(tid))
        self._assign_user = assign_user or (lambda tid, uid: poster.assign_user(tid, uid))

    def fetch_text(self, ref):
        """The raw rendered ticket text (what a brief embeds), or None (no Vikunja ref, or the fetch failed)."""
        tid = tickets.vikunja_id(ref)
        return None if tid is None else self._fetch_text(tid)

    def fetch(self, ref):
        text = self.fetch_text(ref)
        if text is None:
            return None
        assignees, creator = ticket_fetch.ticket_people(text)
        title = _TITLE_RE.search(text)
        # No trusted description parser exists for the rendered view yet, so `body` stays empty and readers
        # use `text` (the whole rendered block), as they did before refs.
        return Ticket(ref=ref, title=title.group(1).strip() if title else "", done=_ticket_text_is_done(text),
                      labels=ticket_fetch.ticket_labels(text), assignees=assignees, creator=creator,
                      project=ticket_fetch.ticket_field(text, "project_id"), url=self.url(ref), text=text)

    def url(self, ref):
        tid = tickets.vikunja_id(ref)
        return "" if tid is None else site_env.ticket_url(tid)

    def comment(self, ref, html_body, run_id):
        tid = tickets.vikunja_id(ref)
        return False if tid is None else self._post_comment(tid, html_body, run_id)

    def label(self, ref, name):
        """Only "parked" (PARKED_LABEL_ID) is wired today; any other name is refused."""
        tid = tickets.vikunja_id(ref)
        return False if tid is None or name != "parked" else self._apply_label(tid)

    def assign(self, ref, user):
        """`user` is the numeric Vikunja user id (the assignee tool takes ids only)."""
        tid = tickets.vikunja_id(ref)
        return False if tid is None else self._assign_user(tid, user)
