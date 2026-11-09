"""Operator ordering across durable intake, pending bursts and real per-turn slot waiters."""
import os
import threading
import time
import unittest
from unittest.mock import patch

import test_dispatch_priority as priority
from test_drain import event
from test_drain import DrainTests
from test_actions import ON, GOOD
from dispatch import PrioritySlots
from dispatch import RepoLock
import actions
import app
import dash


class MoveToFrontTests(unittest.TestCase):
    tearDown = DrainTests.tearDown
    broker = DrainTests.broker
    handler = DrainTests.handler
    wait_metadata = priority.DispatchTests.wait_metadata
    ready_tick = priority.DispatchTests.ready_tick
    review = priority.DispatchTests.review

    def setUp(self):
        priority.isolated_setup(self)

    def post(self, b, path, body, headers=None):
        return self.handler(b, path, body, headers or {}).do_POST()

    def test_pending_choice_overrides_reviews_and_fifo_without_changing_the_burst(self):
        for policy in ('fifo', 'reviews-first'):
            with self.subTest(policy=policy), patch.dict(os.environ, {'DISPATCH_PRIORITY': policy}):
                self.path = os.path.join(self.tmp.name, policy + '.sqlite3')
                b = self.broker()
                older = b.coalescer.on_event(event(9901))
                chosen = b.coalescer.on_event(event(9902))
                b.coalescer.on_event(self.review())
                burst = b.coalescer.pending[chosen]
                assert b.move_to_front(chosen)[0] == 200
                assert b.coalescer.pending[chosen] is burst
                self.ready_tick(b)
                assert self.calls[0][0] == chosen
                assert older in b.coalescer.live
                b.state.close()
                self.brokers.remove(b)
                self.calls.clear()

    def test_durable_intake_choice_stays_paused_then_crosses_a_pending_fifo_head(self):
        b = self.broker()
        older = b.ingest(event(9903))
        b.set_drain(True)
        chosen = b.ingest(event(9904))
        assert b.move_to_front(chosen)[0] == 200
        b.tick()
        assert not self.calls and b.state.status()['queued'] == 1
        b.set_drain(False)
        b.tick()
        assert self.calls[0][0] == chosen
        assert [k for k, _ in self.calls] == [chosen, older]

    def test_caps_debounce_recovery_and_ambiguous_intake_are_not_bypassed(self):
        b = self.broker()
        chosen = b.ingest(event(9905))
        b.coalescer.pending[chosen]['spawn_at'] = time.time() + 60
        assert b.move_to_front(chosen)[0] == 200
        b.tick()
        assert not self.calls
        assert b.semaphore.acquire(blocking=False, key='unrelated')
        b.semaphore.release()
        b.coalescer.pending[chosen]['spawn_at'] = 0
        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'general=0'}):
            assert b.move_to_front(chosen)[0] == 200
            b.tick()
            assert not self.calls
            assert b.semaphore.acquire(blocking=False, key='unrelated')
            b.semaphore.release()
        b.coalescer.recovery['unknown'] = {}
        b.tick()
        assert not self.calls
        b.coalescer.recovery.clear()
        b.state.enqueue('event', {'event': event(9906), 'internal': False})
        b.state.claim(b.state.head()[0])
        b.tick()
        assert not self.calls

    def check_ineligible_choice_preserves_fifo(self, reason):
        b = self.broker()
        co = b.coalescer
        older = co.on_event(event(9920))
        barrier = co.on_event(event(9921))
        chosen = co.on_event(event(9922))
        younger = co.on_event(event(9923))
        co.pending[barrier]['spawn_at'] = time.time() + 60
        if reason == 'debounce':
            co.pending[chosen]['spawn_at'] = time.time() + 60
        caps = {'ROW_CONCURRENCY': 'selected=0' if reason == 'cap' else ''}
        with patch.object(co, 'row_of', side_effect=lambda key: 'selected' if key == chosen else 'other'):
            with patch.dict(os.environ, caps):
                assert b.move_to_front(chosen)[0] == 200
                if reason == 'live':
                    # Model a rearm parked behind a closing worker after selection.
                    co.live[chosen] = {'state': 'closing', 'expire': time.time() + 60}
                co.tick()
                # An ineligible selection must not become a NEW FIFO barrier ahead of older work.
                assert [k for k, _ in self.calls] == [older]
                assert list(co.pending) == [barrier, chosen, younger]
                assert b.semaphore.front == chosen
                assert b.semaphore.acquire(key='unrelated', blocking=False)
                b.semaphore.release()
            co.pending[chosen]['spawn_at'] = 0
            if reason == 'live':
                co.live.pop(chosen)
            co.tick()
            # Once eligible it still jumps the ordinary (debouncing) FIFO head.
            assert [k for k, _ in self.calls] == [older, chosen]
            assert list(co.pending) == [barrier, younger]
            assert not b.semaphore.acquire(key='unrelated', blocking=False)
            assert b.semaphore.acquire(key=chosen, blocking=False)
            assert b.semaphore.front is None
            b.semaphore.release()

    def test_debouncing_choice_does_not_stall_older_fifo_work(self):
        self.check_ineligible_choice_preserves_fifo('debounce')

    def test_capped_choice_does_not_stall_older_fifo_work(self):
        self.check_ineligible_choice_preserves_fifo('cap')

    def test_live_choice_does_not_stall_older_fifo_work(self):
        self.check_ineligible_choice_preserves_fifo('live')

    def test_operator_auth_is_fail_closed_and_bad_or_nonwaiting_subjects_are_noops(self):
        b = self.broker()
        chosen = b.ingest(event(9907))
        path = '/move-to-front'
        with patch.dict(os.environ, {app.RERUN_TOKEN_ENV: 'synthetic-operator-token'}):
            for headers in ({}, {'X-Broker-Rerun-Token': 'wrong'}):
                assert self.post(b, path, {'subject_key': chosen}, headers)[0] == 401
                assert b.semaphore.front is None
            auth = {'X-Broker-Rerun-Token': 'synthetic-operator-token'}
            assert self.post(b, path, {'subject_key': chosen}, auth)[0] == 200
            for key in (None, [], 7, '', 'bad key', 'x' * 513):
                assert self.post(b, path, {'subject_key': key}, auth)[0] == 400
            assert self.post(b, path, {'subject_key': 'github:owner/repo#999'}, auth)[0] == 409
            assert b.semaphore.front == chosen
            assert self.post(b, path, [], auth)[0] == 400
        with patch.dict(os.environ, {app.RERUN_TOKEN_ENV: ''}):
            assert self.post(b, path, {'subject_key': chosen}, auth)[0] == 401
        # Already running (not waiting at the slot gate) must not boost a future turn/run.
        b.coalescer.live['running'] = {'state': 'open_running'}
        assert b.move_to_front('running')[0] == 409
        assert b.semaphore.front == chosen

    def test_web_action_reuses_all_existing_auth_gates_and_audits(self):
        b = self.broker()
        chosen = b.ingest(event(9908))
        path = '/actions/move-to-front'
        with patch.dict(os.environ, {**ON, 'BROKER_ACTIONS_ENABLED': ''}):
            assert self.post(b, path, {'subject_key': chosen}, GOOD)[0] == 404
        with patch.dict(os.environ, ON), patch.object(actions.events, 'emit') as emit:
            for name, value in (('X-Broker-Proxy-Secret', 'wrong'), ('X-Auth-Groups', 'users'),
                                ('X-Broker-Action', ''), ('Sec-Fetch-Site', 'cross-site'),
                                ('Content-Type', 'text/plain'), ('X-Auth-Username', '')):
                assert self.post(b, path, {'subject_key': chosen}, {**GOOD, name: value})[0] == 403
                assert b.semaphore.front is None
            assert self.post(b, path, {'subject_key': chosen}, GOOD)[0] == 200
            assert emit.call_args.kwargs == {'subject_key': chosen, 'user': 'alice',
                                             'verb': 'move-to-front', 'result': 'ok'}
            assert self.post(b, path, {}, GOOD)[0] == 400
            state = dash.snapshot(b)
            assert state['next_subject'] == chosen
        assert 'id="v-queue"' in dash.PAGE
        assert "actBtn('Move to front', 'move-to-front'" in dash.PAGE
        assert "l.waiting === 'concurrency'" in dash.PAGE

    def test_pending_selection_reserves_a_freed_slot_until_setup_reaches_the_gate(self):
        b = self.broker()
        chosen = b.ingest(event(9909))
        slots = b.semaphore = b.adapter._sem = b.coalescer.slots = PrioritySlots(1)
        assert slots.acquire(key='running')
        assert b.move_to_front(chosen)[0] == 200
        slots.release()
        assert not slots.acquire(key='older', blocking=False)
        assert slots.acquire(key=chosen, blocking=False)
        assert slots.front is None
        slots.release()
        assert slots.acquire(key='older', blocking=False)
        slots.release()

    def test_cleanup_of_a_rejected_setup_releases_its_reservation(self):
        b = self.broker()
        chosen = b.ingest(event(9910))
        assert b.move_to_front(chosen)[0] == 200
        self.ready_tick(b)
        b.adapter._settle_self(chosen, mark_completed=False)
        assert b.semaphore.front is None

    def test_failed_intent_checkpoint_does_not_strand_existing_slot_waiters(self):
        b = self.broker()
        chosen = b.ingest(event(9911))
        assert b.move_to_front(chosen)[0] == 200
        with patch.object(b.coalescer, '_checkpoint', side_effect=app.StateError('synthetic disk failure')):
            with self.assertRaises(app.StateError):
                b.tick()
        assert not self.calls and chosen in b.coalescer.pending
        assert b.semaphore.acquire(key='existing-warm-turn', blocking=False)
        b.semaphore.release()

    def test_repo_setup_defers_reservation_without_losing_the_selection(self):
        b = self.broker()
        chosen = b.ingest(event(9912))
        assert b.move_to_front(chosen)[0] == 200
        repo = RepoLock()
        # An uncontended repository must NOT surrender the selected next slot.
        b.adapter._acquire_wait(chosen, repo, 'repo_lock')
        assert not b.semaphore.acquire(key='older', blocking=False)
        repo.release()
        repo.acquire()
        waiting, errors = threading.Event(), []

        def setup():
            try:
                b.adapter._acquire_wait(chosen, repo, 'repo_lock')
                repo.release()
            except BaseException as exc:
                errors.append(exc)

        defer = b.semaphore.defer

        def deferred(key):
            defer(key)
            waiting.set()

        t = threading.Thread(target=setup)
        try:
            with patch.object(b.semaphore, 'defer', side_effect=deferred):
                t.start()
                assert waiting.wait(5)
                assert b.semaphore.front == chosen
                assert b.semaphore.acquire(key='repo-holder', blocking=False)
                b.semaphore.release()
        finally:
            repo.release()
            t.join(5)
        assert not t.is_alive() and not errors


class PrioritySlotsTests(unittest.TestCase):
    def wait_queued(self, slots, count):
        deadline = time.monotonic() + 5
        while True:
            with slots._condition:
                if len(slots._waiters) == count:
                    return
            assert time.monotonic() < deadline, 'slot waiter did not register'
            time.sleep(0.001)

    def test_real_adapter_waiters_promotion_is_atomic_one_shot_and_fifo_otherwise(self):
        fixture = MoveToFrontTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        b = fixture.broker()
        slots = b.adapter._sem = b.semaphore = PrioritySlots(1)
        slots.acquire(key='running')
        threads, seen, errors = [], [], []

        def run(key):
            try:
                b.adapter._acquire_wait(key, slots, 'concurrency')
                try:
                    seen.append(key)
                finally:
                    slots.release()
            except BaseException as exc:
                errors.append(exc)

        try:
            for key in ('old', 'chosen', 'new'):
                t = threading.Thread(target=run, args=(key,))
                threads.append(t)
                t.start()
                self.wait_queued(slots, len(threads))
            assert b.move_to_front('chosen')[0] == 200
            assert seen == []  # never preempt the holder
            assert b.move_to_front('new')[0] == 200  # latest operator choice replaces the old one
            assert b.move_to_front('chosen')[0] == 200
        finally:
            slots.release()
            for t in threads:
                t.join(5)
        assert not any(t.is_alive() for t in threads)
        assert not errors
        assert seen == ['chosen', 'old', 'new']
        assert slots.front is None and not b.adapter._waiting
        assert b.move_to_front('chosen')[0] == 409

    def test_reservation_defers_for_repo_dependencies_and_cancellation_keeps_capacity(self):
        slots = PrioritySlots(1)
        slots.promote('pending', pending=True, reserve=True)
        assert not slots.acquire(key='repo-holder', blocking=False)
        slots.defer('pending')
        assert slots.acquire(key='repo-holder', blocking=False)
        slots.release()
        cancelled = RuntimeError('parked by drain')

        def cancel():
            # The scheduler lock must be acquirable while a cancellation check runs.
            assert not slots._condition._is_owned()
            raise cancelled

        with self.assertRaises(RuntimeError):
            slots.acquire(key='pending', cancel=cancel)
        assert not slots._waiters
        slots.discard('pending')
        assert slots.acquire(blocking=False)
        assert not slots.acquire(timeout=0.001)
        slots.release()
        with self.assertRaises(ValueError):
            slots.release()


if __name__ == '__main__':
    unittest.main()
