#!/usr/bin/env python3
"""trackers: the Tracker interface, the Ticket record and the registry (docs/trackers.md, step 2).

A tracker is addressed by a ticket ref (`tickets.py`): `vikunja:task:42`, `gitlab:group/app#7`. Every method
takes the ref and only the adapter parses its native half. Every default here FAILS CLOSED (False, [], None,
"", and True for is_self) and never raises, so a tracker that doesn't implement a method simply does nothing.
An adapter's own methods may raise (a sink or parse error in VikunjaTracker gets through), so callers keep
their try/except. Writes return bool, matching poster.py.
"""
import html
from dataclasses import dataclass, field

import tickets


@dataclass
class Ticket:
    """One fetched ticket. `text` is the rendered block a worker's brief embeds; the fail-closed checks read
    the fields. `assignees` and `creator` are None when the tracker can't trust its own metadata (callers
    fail closed on None, never on [])."""
    ref: str
    title: str = ""
    body: str = ""
    done: bool = False
    labels: list = field(default_factory=list)
    assignees: list = None
    creator: str = None
    project: str = None
    url: str = ""
    is_pr: bool = False
    text: str = ""


class Tracker:
    name = ""        # registry name, the ref prefix
    kind = ""        # implementation: "vikunja", "gitlab", "github"

    # intake
    def verify(self, headers, raw):
        return False

    def normalize(self, headers, payload):
        return []

    # read
    def fetch(self, ref):
        return None

    def url(self, ref):
        return ""

    # write back
    def comment(self, ref, html_body, run_id):
        return False

    def label(self, ref, name):
        return False

    def assign(self, ref, user):
        return False

    def link_mr(self, ref, mr_url, title, run_id):
        """Default: a comment naming the MR, which is all the broker does today."""
        return self.comment(ref, f"<p>MR: <a href=\"{html.escape(str(mr_url or ''))}\">"
                                 f"{html.escape(str(title or mr_url or ''))}</a></p>", run_id)

    def review(self, ref, html_body, run_id):
        """Default: a plain comment. Only a pull-request tracker overrides it, and never to approve or block."""
        return self.comment(ref, html_body, run_id)

    def close(self, ref):
        return False                      # the broker never closes tickets

    # echo guard
    def is_self(self, evt):
        """True when the event is the broker's own write. Default True: a tracker that can't tell drops the event
        as an echo instead of spawning on it (fail closed); an adapter overrides this in step 3."""
        return True


class Registry:
    """The enabled trackers by registry name. `get(ref)` is the one lookup every caller uses."""

    def __init__(self, trackers=()):
        self._by_name = {t.name: t for t in trackers}

    def get(self, ref):
        """The tracker registered under the ref's prefix, or None (no ref, not a ticket, or not registered)."""
        return self._by_name.get(tickets.tracker_of(ref))
