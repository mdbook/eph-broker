"""Inert regressions at durable intake and worker retirement boundaries."""
import sqlite3
from unittest.mock import patch

from test_drain import DrainTests, StateError, app, event, os, raises, ticket


class Interrupted(BaseException):
    """A process stops without returning an external-effect outcome."""


class BoundaryTests(DrainTests):
    def test_immediate_tfa_http_retry_after_launch_write_failure_is_held(self):
        b = self.broker()
        launches = []

        def launch(*args, **kwargs):
            launches.append('launch')
            with sqlite3.connect(self.path) as db:
                assert db.execute("SELECT phase FROM intake").fetchall() == [('dispatching',)]
            b.state.db.execute('PRAGMA query_only=ON')
            return object()

        with patch.object(app, 'check_hmac', return_value=(True, 'synthetic')), \
                patch.object(app, '_live_tfa_session', return_value=False), \
                patch.object(app.subprocess, 'Popen', side_effect=launch):
            assert self.handler(b, '/event', event(9201, assignees=['fleet'])).do_POST()[0] == 503
            b = self.restart(b)
            assert b.drain_status()['dispatching'] == 1
            assert self.handler(b, '/health').do_GET()[0] == 503
            self.handler(b, '/event', event(9201, assignees=['fleet'])).do_POST()
            self.pump(b)
        assert launches == ['launch']

    def test_immediate_tfa_crash_before_receipt_persistence_is_held(self):
        b = self.broker()
        launches = []

        def launch(*args, **kwargs):
            launches.append('launch')
            raise Interrupted()

        with patch.object(app, '_live_tfa_session', return_value=False), \
                patch.object(app.subprocess, 'Popen', side_effect=launch):
            with raises(Interrupted):
                b.ingest(event(9202, assignees=['fleet']))
        b = self.restart(b)
        assert b.drain_status()['dispatching'] == 1
        assert launches == ['launch']

    def test_fresh_tfa_waits_for_pending_fifo_head(self):
        b = self.broker()
        b.set_drain(True)
        b.ingest(event(9203))
        b.set_drain(False)
        b.tick()
        assert 'vikunja:task:9203' in b.coalescer.pending
        launched = []
        with patch.object(app, '_live_tfa_session', return_value=False), \
                patch.object(app, '_tfa_self_assign', return_value=None), \
                patch.object(b.adapter.tracker('vikunja:task:9204'), 'comment', return_value=True), \
                patch.object(app.subprocess, 'Popen', side_effect=lambda *a, **k: launched.append(len(self.calls)) or object()):
            b.ingest(event(9204, assignees=['fleet']))
            assert launched == []
            assert b.drain_status()['queued'] == 1
            b.tick()
        assert launched == [1]

    def test_same_subject_pending_burst_still_collects(self):
        b = self.broker()
        key = b.ingest(event(9205))
        b.ingest(event(9205, detail='second'))
        assert len(b.coalescer.pending[key]['events']) == 2
        assert b.drain_status()['queued'] == 0

    def _buffered_retirement(self, source, method):
        b = self.broker()
        if source == 'gmail':
            first = {'source': 'gmail', 'account': 'primary', 'event_name': 'gmail.new', 'doer': 'owner'}
        elif source == 'zulip':
            first = {'source': 'zulip', 'bot': 'test-chat', 'sender_id': '9', 'msg_id': '201',
                     'event_name': 'zulip.dm', 'doer': 'zulip:9', 'content': 'first private body'}
        else:
            first = event(9210)
        key = b.ingest(dict(first), internal=source == 'zulip')
        b.tick()
        followup = {**first, 'msg_id': '202', 'content': 'retained private followup', 'detail': 'followup'}
        b.ingest(followup, internal=source == 'zulip')
        assert len(b.coalescer.live[key]['fwd_buffer']) == 1
        b.set_drain(True)
        if method == 'finish_close':
            b.coalescer.begin_close(key)
        # The callback runs after the removal commit, before any caller can rearm.
        def checkpoint(_key):
            code, body = self.handler(b, '/health').do_GET()
            assert code == 200 and body['live_workers'] == 0
            assert body['drain']['pending'] == 1
            with sqlite3.connect(self.path) as db:
                import json
                saved = json.loads(db.execute("SELECT value FROM state WHERE name='scheduler'").fetchone()[0])
            assert key in dict(saved['pending'])
            assert key not in dict(saved['active'])
            raise Interrupted()

        with patch.object(b.coalescer, '_notify_close', side_effect=checkpoint):
            with raises(Interrupted):
                getattr(b.coalescer, method)(key)
        b = self.restart(b)
        pending = b.coalescer.pending[key]
        assert pending['events'] == [followup]
        assert b.route_stamps[key] == pending['row']
        assert not b.coalescer.recovery
        self.pump(b)
        assert len(self.calls) == 1
        b.set_drain(False)
        self.pump(b)
        assert len(self.calls) == 2

    def test_force_pop_commits_buffer_before_alert_or_rearm(self):
        self._buffered_retirement('vikunja', 'force_pop')

    def test_self_settle_gmail_commits_buffer_before_rearm(self):
        self._buffered_retirement('gmail', 'settle_self')

    def test_self_settle_zulip_commits_buffer_before_rearm(self):
        self._buffered_retirement('zulip', 'settle_self')

    def test_clean_close_gmail_commits_buffer_before_rearm(self):
        self._buffered_retirement('gmail', 'finish_close')

    def test_clean_close_zulip_commits_buffer_before_rearm(self):
        self._buffered_retirement('zulip', 'finish_close')

    def test_terminal_ticket_settle_keeps_terminal_drop_policy(self):
        b = self.broker()
        key = b.ingest(event(9211))
        b.tick()
        b.ingest(event(9211, detail='terminal noise'))
        b.coalescer.settle_self(key)
        b = self.restart(b)
        assert key in b.coalescer.completed
        assert key not in b.coalescer.pending

    def test_force_close_caller_does_not_duplicate_atomic_rearm(self):
        b = self.broker()
        key = b.ingest(event(9212))
        b.tick()
        b.ingest(event(9212, detail='one followup'))
        b.set_drain(True)
        with patch.object(b.adapter, 'teardown'), patch.object(b.liveness, 'alert_ops'):
            b.liveness._force_close(key, 0, 'synthetic force close')
        assert len(b.coalescer.pending[key]['events']) == 1
        b = self.restart(b)
        assert len(b.coalescer.pending[key]['events']) == 1

    def test_retirement_interrupted_checkpoint_rolls_back_to_held_active(self):
        b = self.broker()
        key = b.ingest(event(9213))
        b.tick()
        b.ingest(event(9213, detail='must survive transfer rollback'))
        checkpoint = b.coalescer._checkpoint
        def interrupt():
            checkpoint()
            raise Interrupted()
        with patch.object(b.coalescer, '_checkpoint', side_effect=interrupt):
            with raises(Interrupted):
                b.coalescer.force_pop(key)
        b = self.restart(b)
        assert key not in b.coalescer.pending
        assert len(b.coalescer.recovery[key]['events']) == 2
        assert self.handler(b, '/health').do_GET()[0] == 503

    def _mention(self, assign):
        b = self.broker()
        mh = app.mention_handoff
        class InlineThread:
            def __init__(self, target, args=(), **kwargs):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
        self.mention_patchers = [
            patch.dict(os.environ, {'MENTION_HANDOFF_ENABLED': '1', 'COMMENT_RESPAWN_ENABLED': '0'}),
            patch.object(mh, 'MENTION_USER', 'dev'), patch.object(mh, 'MENTION_UID', 913),
            patch.object(mh, 'MENTION_HANDOFF_PROJECTS', frozenset({9999})),
            patch.object(mh.threading, 'Thread', InlineThread),
            patch.object(b.mention_handoff, '_assign', side_effect=assign)]
        for p in self.mention_patchers:
            p.start()
        b.set_drain(True)
        b.ingest(event(9220, event_name='task.comment.created', assignees=[], project_id=9999,
                       comment='<mention-user data-id="dev">take this</mention-user>'))
        b.set_drain(False)
        return b

    def tearDown(self):
        for p in reversed(getattr(self, 'mention_patchers', [])):
            p.stop()
        super().tearDown()

    def test_mention_interruption_before_assignment_retains_intent(self):
        def assign(*args):
            raise Interrupted()
        b = self._mention(assign)
        class DeferredThread:
            def __init__(self, **kwargs):
                pass
            def start(self):
                pass
        with patch.object(app.mention_handoff.threading, 'Thread', DeferredThread):
            with raises(Interrupted):
                b.tick()
        b = self.restart(b)
        assert b.drain_status()['dispatching'] == 1
        assert self.handler(b, '/health').do_GET()[0] == 503

    def test_mention_unknown_assignment_outcome_remains_held(self):
        b = self._mention(lambda *args: False)
        with raises(StateError):
            b.tick()
        b = self.restart(b)
        assert b.drain_status()['dispatching'] == 1

    def test_mention_receipt_failure_after_assignment_remains_held(self):
        calls = []
        def assign(*args):
            calls.append(args)
            b.state.db.execute('PRAGMA query_only=ON')
            return True
        b = self._mention(assign)
        with raises(StateError):
            b.tick()
        b = self.restart(b)
        assert calls == [(9220, 913)]
        assert b.drain_status()['dispatching'] == 1

    def test_mention_known_success_finishes_only_after_assignment(self):
        calls = []
        def assign(*args):
            with sqlite3.connect(self.path) as db:
                assert db.execute("SELECT phase FROM intake").fetchall() == [('dispatching',)]
            calls.append(args)
            return True
        b = self._mention(assign)
        b.tick()
        assert calls == [(9220, 913)]
        b = self.restart(b)
        assert b.drain_status()['dispatching'] == 0
        # HTTP retries with the old assignee snapshot must honor the committed TTL receipt.
        with patch.object(b.mention_handoff, '_assign', side_effect=AssertionError('duplicate assignment')):
            b.ingest(event(9220, event_name='task.comment.created', assignees=[], project_id=9999,
                           comment='<mention-user data-id="dev">take this</mention-user>'))
        assert b.drain_status()['dispatching'] == 0

    def test_duplicate_queued_rerun_refusal_does_not_hold_unrelated_follower(self):
        b = self.broker()
        b.set_drain(True)
        with patch.dict(os.environ, {app.RERUN_TOKEN_ENV: 'synthetic-admin'}):
            headers = {'X-Broker-Rerun-Token': 'synthetic-admin'}
            replies = []
            for _ in range(2):
                handler = self.handler(b, '/rerun', {'task_id': 9610, 'note': 'retry'}, headers)
                handler._reply = lambda code, body: replies.append((code, body))
                handler.do_POST()
            assert [code for code, _ in replies] == [202, 202]
        b.ingest(event(9230))
        b.set_drain(False)
        self.pump(b)
        assert [key for key, _ in self.calls] == ['vikunja:task:9610', 'vikunja:task:9230']
        status = b.drain_status()
        assert status['error'] is None and status['dispatching'] == status['queued'] == 0
        outcome = b.state.get('last_rerun_result')
        assert outcome['status'] == 409 and outcome['result']['ok'] is False

    def test_queued_rerun_becoming_ineligible_has_durable_terminal_result(self):
        b = self.broker()
        b.set_drain(True)
        assert b.ops_rerun(9610)[0] == 202
        b.ingest(event(9231))
        b.adapter._ticket_fetcher = lambda tid: ticket().replace('**done:** false', '**done:** true')
        b.set_drain(False)
        self.pump(b)
        assert [key for key, _ in self.calls] == ['vikunja:task:9231']
        b = self.restart(b)
        assert b.state.get('last_rerun_result')['status'] == 409
        assert b.drain_status()['error'] is None and b.drain_status()['dispatching'] == 0

    def test_unexpected_queued_rerun_failure_still_holds_for_reconciliation(self):
        b = self.broker()
        b.set_drain(True)
        b.ops_rerun(9610)
        b.set_drain(False)
        with patch.object(b, '_ops_rerun', return_value=(500, {'ok': False, 'error': 'unknown'})):
            with raises(StateError):
                b.tick()
        b = self.restart(b)
        assert b.drain_status()['dispatching'] == 1
        assert self.handler(b, '/health').do_GET()[0] == 503

    def test_durable_force_close_keeps_forward_only_comment_drop_policy(self):
        b = self.broker()
        key = b.ingest(event(9232))
        b.tick()
        b.ingest(event(9232, event_name='task.comment.created', comment_id=12, comment='ticket comment'))
        popped = b.coalescer.force_pop(key)
        assert popped['rearmed'] == 0
        b = self.restart(b)
        assert key not in b.coalescer.pending and key not in b.coalescer.recovery


if __name__ == '__main__':
    import sys
    import traceback
    failures = []
    tests = sorted(n for n in BoundaryTests.__dict__ if n.startswith('test_'))
    for name in tests:
        fixture = BoundaryTests()
        fixture.setUp()
        try:
            getattr(fixture, name)()
            print('PASS ' + name)
        except BaseException:
            failures.append(name)
            traceback.print_exc()
            print('FAIL ' + name)
        finally:
            fixture.tearDown()
    print(f'{len(tests) - len(failures)} passed, {len(failures)} failed')
    sys.exit(bool(failures))
