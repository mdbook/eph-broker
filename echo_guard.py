#!/usr/bin/env python3
"""echo_guard.py: the infinite-loop guard.

A worker's own side-effect (a ticket comment/label/push) fires a webhook, which produces a same-subject
event, which would re-arm the receive window forever. The guard enforces:
  * an event whose `doer` is an AGENT BOT is dropped and counted, NEVER re-arms;
  * worker-authored side-effects are stamped with the spawn's `run_id`; an event carrying an
    active run_id is the worker's own echo, so it is discarded;
  * a HUMAN edit re-arms normally; worker side-effects never do.

This is a filter applied at INGEST (app.py), BEFORE coalescer.on_event, so the lock and state handoff in
on_event/tick stay untouched.

The agent-bot check is PROVENANCE-based (provenance.classify). The old `(-bot\\b|@<bus-domain>)` regex
missed the shared Vikunja service account (e.g. `svc-agent`), so the broker couldn't recognise its own comment
echoes authored by that account. `is_fleet_bot()` keeps its old signature and behaviour (falsy doer ->
False) as a compatibility shim; `coalescer.py`'s human-re-engagement check still imports it under that name.
"""
import threading, logging

import provenance
import tickets
import routing

log = logging.getLogger("eph-broker.echo_guard")


def is_fleet_bot(doer):
    """Compatibility shim: bot-or-system doer, via provenance. A falsy doer (no attribution at all, e.g.
    the MR reviewer's synthetic mr.opened trigger) stays False, exactly as the old regex-based check did,
    so an unattributed SYNTHETIC event (not a worker echo) still passes the echo guard."""
    return bool(doer) and provenance.classify(doer) in ("agent", "system")


class RunRegistry:
    """Tracks active run_ids so a worker's own stamped side-effects can be discarded as echoes."""
    def __init__(self):
        self._active = {}    # run_id -> key
        self._lock = threading.Lock()

    def open(self, run_id, key):
        with self._lock:
            self._active[run_id] = key

    def close(self, run_id):
        with self._lock:
            self._active.pop(run_id, None)

    def is_active(self, run_id):
        with self._lock:
            return run_id in self._active

    def has_subject(self, key):
        """Is any worker run in flight on this subject key?"""
        with self._lock:
            return key in self._active.values()


class EchoGuard:
    def __init__(self, run_registry=None, classify=None):
        self.run_registry = run_registry or RunRegistry()
        self.classify = classify    # (key, evt) -> row domain; None means no agent handoff is admitted
        self.dropped = 0

    def is_echo(self, evt):
        """True if this event is a worker's own echo and must NOT re-arm/spawn."""
        run_id = evt.get("run_id")
        if run_id and self.run_registry.is_active(run_id):
            return True                              # our own stamped side-effect
        if is_fleet_bot(evt.get("doer")):
            # An agent @dev/@eph/@fleet handoff spawns, but never while a worker runs on that subject
            # (anything then could be our own side-effect), and only if it actually routes to the row its
            # assignee dispatches to (first-match could hand it to ansible/wiki/...). Any other agent-bot
            # doer never re-arms.
            key = evt.get("ref") or f"vikunja:task:{evt.get('task_id')}"
            if tickets.tracker_of(key) == "vikunja" and provenance.is_agent_handoff(evt) \
                    and not self.run_registry.has_subject(key) \
                    and self.classify is not None \
                    and self.classify(key, evt) in routing.assignee_dispatch_domains(evt):
                log.info("echo-guard ADMIT agent handoff doer=%s task=%s event=%s",
                         evt.get("doer"), evt.get("task_id"), evt.get("event_name"))
                return False
            return True
        return False

    def filter(self, evt):
        """Return evt to ingest, or None to drop (counted). Human edits pass; echoes drop."""
        if self.is_echo(evt):
            self.dropped += 1
            log.info("echo-guard DROP doer=%s run_id=%s (worker echo, not re-armed; total_dropped=%d)",
                     evt.get("doer"), evt.get("run_id"), self.dropped)
            return None
        return evt
