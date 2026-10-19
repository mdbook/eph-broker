#!/usr/bin/env python3
"""tickets.py: the ticket ref grammar (docs/trackers.md, step 1).

A ref is `<tracker>:<native>`, and it is the coalescer's subject key for a ticket: `vikunja:task:42`,
`gitlab:group/app#7`, `github:owner/repo#45`. Only this module (and later the tracker adapter) splits one
apart. Keys that aren't tickets (`zulip:`, `gmail:`, `mr:`, the `:?` and `:unknown` sentinels) have no ref.
"""
import re

TRACKERS = ("vikunja", "gitlab", "github")
REF_MAX = 200
_BAD = re.compile(r"[\s\x00-\x1f\x7f]")
_VIKUNJA = "vikunja:task:"


def task_id(v):
    """A Vikunja task id as an int: a non-negative int (not a bool) or a string of ASCII digits ("042" is 42).
    Anything else ("+42", "4_2", " 42", "-3", non-ASCII digits, a float) gives None, so one ticket can't get
    two subject keys (never raises)."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v if v >= 0 else None
    if isinstance(v, str) and v.isascii() and v.isdigit():
        return int(v)
    return None


def vikunja_id(ref):
    """The Vikunja task id in a `vikunja:task:<n>` ref, or None for any other ref or key (never raises)."""
    if not isinstance(ref, str) or not ref.startswith(_VIKUNJA):
        return None
    return task_id(ref[len(_VIKUNJA):])


def vikunja_ref(tid):
    """The `vikunja:task:<n>` ref for a bare Vikunja task id (see task_id), or None when it's None or not a
    task id (never raises)."""
    n = task_id(tid)
    return None if n is None else f"{_VIKUNJA}{n}"


def ref_from_key(key):
    """The ticket ref for a subject key, or None when the key isn't a ticket. A ref over REF_MAX chars or with
    whitespace or control characters is refused, never truncated."""
    if not isinstance(key, str) or len(key) > REF_MAX or _BAD.search(key):
        return None
    tracker, _, native = key.partition(":")
    if tracker == "vikunja":
        return key if vikunja_id(key) is not None else None
    if tracker in ("gitlab", "github") and native and native != "unknown" and "#" in native:
        return key
    return None


def tracker_of(ref):
    """The registry name a ref belongs to ("vikunja", "gitlab", "github"), or None."""
    return ref.partition(":")[0] if ref_from_key(ref) else None
