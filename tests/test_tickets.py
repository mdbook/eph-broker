#!/usr/bin/env python3
"""Ticket ref self-tests (tickets.py, docs/trackers.md step 1): the ref grammar, parity with the old
`_task_id_from_key`, the ref stamped at ingest, and the echo guard reading it.

stdlib assert only, same runner shape as test_routes.py.
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ["BROKER_SITE_ENV"] = os.path.join(HERE, "fixtures", "site.test.env")
os.environ["BROKER_CONFIG"] = os.path.join(HERE, "fixtures", "broker.test.json")
os.environ["BROKER_PROVENANCE_PATH"] = os.path.join(HERE, "fixtures", "provenance.test.json")
os.environ["ROUTES_PATH"] = "/nonexistent/eph-test/routes.json"
import site_env  # noqa: F401

import tickets
import coalescer as C
from echo_guard import EchoGuard, RunRegistry

PASS = []


def ok(name):
    PASS.append(name); print(f"  PASS  {name}")


def _old_task_id_from_key(key):
    """adapters/cc.py before step 1, kept verbatim as the parity oracle."""
    if not isinstance(key, str) or not key.startswith("vikunja:task:"):
        return None
    try:
        return int(key.split(":")[-1])
    except (TypeError, ValueError):
        return None


KEYS = ["vikunja:task:42", "vikunja:task:0", "vikunja:task:-3", "vikunja:task:?", "vikunja:task:None",
        "vikunja:task:", "vikunja:task:4x", "vikunja:task: 42", "vikunja:project:42", "vikunja:task:42.0",
        "gitlab:group/app#7", "gitlab:unknown", "github:owner/repo#45", "github:unknown", "mr:group/app!3",
        "mr:unknown", "zulip:bot:9", "gmail:me", "gmail:unknown", "alert:raw", "", None, 42]


# Keys the old parser read through int(): a sign, a space or a non-canonical digit string. Now not a ticket.
LOOSE = ("vikunja:task:-3", "vikunja:task: 42")


def test_vikunja_id_matches_old_parser():
    for key in KEYS:
        want = None if key in LOOSE else _old_task_id_from_key(key)
        assert tickets.vikunja_id(key) == want, key
    for tid in (1, 42, 9352, 10 ** 9):                    # every key coalescer.normalize builds from a real id
        key = C.normalize({"source": "vikunja", "task_id": tid})
        assert tickets.vikunja_id(key) == tid == _old_task_id_from_key(key)
    ok("vikunja_id agrees with the old _task_id_from_key on every key shape normalize can build; a signed or "
       "spaced id is not a ticket")


def test_vikunja_ref():
    for tid in (42, "42", 0, "0", "042", 10 ** 9):
        ref = tickets.vikunja_ref(tid)
        assert ref == f"vikunja:task:{int(tid)}" and tickets.vikunja_id(ref) == int(tid), tid
    for bad in (None, "", "4x", "42.0", "vikunja:task:42", [], {}, -3, "-3", "+42", "4_2", " 42", "42 ",
                "\u0664\u0662", "\uff14\uff12", True, False, 42.0, 42.9):
        assert tickets.vikunja_ref(bad) is None and tickets.task_id(bad) is None, bad
    ok("vikunja_ref takes a non-negative int or ASCII digits (\"042\" is 42); a sign, underscore, space, "
       "non-ASCII digits, a bool or a float give None")


def test_task_id_aliases_share_one_subject_key():
    keys = {C.normalize({"source": "vikunja", "task_id": t}) for t in (42, "42", "042", "0042")}
    assert keys == {"vikunja:task:42"}, keys
    for bad in ("+42", "4_2", "\u0664\u0662", "-3", -3, " 42", True, 42.0, "", None):
        assert C.normalize({"source": "vikunja", "task_id": bad}) == "vikunja:task:?", bad
    ok("normalize: 42, \"42\" and \"042\" give one key vikunja:task:42; +42, 4_2, Arabic-Indic digits, -3, a bool "
       "or a float take the unknown-id key vikunja:task:?")


def test_ref_from_key():
    refs = {k: k for k in ("vikunja:task:42", "vikunja:task:0", "gitlab:group/app#7", "github:owner/repo#45")}
    for key in KEYS:
        assert tickets.ref_from_key(key) == refs.get(key), key
    for bad in ("github:a/b#" + "9" * 200, "github:a b#1", "gitlab:g/a#7\n", "github:a/b#1\x00", "jira:X-1"):
        assert tickets.ref_from_key(bad) is None, bad
    assert [tickets.tracker_of(r) for r in refs] == ["vikunja"] * 2 + ["gitlab", "github"]
    assert tickets.tracker_of("mr:group/app!3") is None and tickets.tracker_of(None) is None
    for evt in ({"source": "vikunja", "task_id": 7}, {"source": "gitlab", "project": "g/a", "issue_iid": 7},
                {"source": "github", "repo": "o/r", "issue": 45}):
        assert tickets.ref_from_key(C.normalize(evt)) == C.normalize(evt)    # the ref IS the subject key
    ok("ref_from_key: ticket keys are their own ref; sentinels, non-tickets, long or spaced refs give None")


def test_ingest_stamps_ref_from_key_not_body():
    import app
    seen = []

    class Guard:
        def filter(self, evt):
            seen.append(evt); return None
    b = app.Broker.__new__(app.Broker)
    b.echo_guard = Guard()
    caller = {"source": "vikunja", "task_id": 7, "ref": "github:evil/repo#1", "event_name": "task.updated"}
    assert b.ingest(dict(caller)) is None
    assert seen[-1]["ref"] == "vikunja:task:7" and seen[-1]["task_id"] == 7
    b.ingest({"source": "zulip", "bot": "b", "sender_id": 1, "ref": "vikunja:task:1"}, internal=True)
    assert "ref" not in seen[-1]                          # not a ticket: no ref, and a body-supplied one is dropped
    b.ingest({"source": "gitlab", "project": "g/a", "issue_iid": 7})
    assert seen[-1]["ref"] == "gitlab:g/a#7"
    ok("ingest stamps the ref from the subject key and drops any ref the body supplied")


def test_echo_guard_reads_ref():
    import provenance, routing
    reg = RunRegistry()
    calls = []
    g = EchoGuard(reg, classify=lambda k, e: calls.append(k) or None)
    saved = provenance.is_agent_handoff, routing.assignee_dispatch_domains
    try:
        provenance.is_agent_handoff = lambda evt: True
        routing.assignee_dispatch_domains = lambda evt: set()
        import echo_guard
        saved_bot = echo_guard.is_fleet_bot
        echo_guard.is_fleet_bot = lambda doer: True
        try:
            assert g.is_echo({"source": "vikunja", "task_id": 7, "ref": "vikunja:task:7", "doer": "x"})
            assert calls == ["vikunja:task:7"]
            assert g.is_echo({"source": "vikunja", "task_id": 7, "doer": "x"})     # no ref: legacy key
            assert calls == ["vikunja:task:7", "vikunja:task:7"]
            assert g.is_echo({"source": "github", "ref": "github:o/r#1", "doer": "x"})
            assert len(calls) == 2                        # a non-Vikunja bot event never reaches the handoff check
        finally:
            echo_guard.is_fleet_bot = saved_bot
    finally:
        provenance.is_agent_handoff, routing.assignee_dispatch_domains = saved
    ok("echo guard keys the agent-handoff check on the ref and keeps it Vikunja-only")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"running {len(tests)} ticket ref self-tests...")
    FAILURES = []
    for t in tests:
        try:
            t()
        except BaseException as e:   # noqa: BLE001
            FAILURES.append((t.__name__, e))
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    if FAILURES:
        print(f"\n{len(FAILURES)} of {len(tests)} CHECKS FAILED ({len(PASS)} passed):")
        for name, e in FAILURES:
            print(f"  - {name}: {type(e).__name__}: {e}")
        sys.exit(1)
    print(f"\nALL {len(PASS)} CHECKS PASSED")
