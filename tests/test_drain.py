"""Isolated drain acceptance tests. No worker, tracker, bus, or production state access."""
import io
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for (name, filename) in (('BROKER_SITE_ENV', 'site.test.env'), ('BROKER_PROVENANCE_PATH', 'provenance.test.json'), ('BROKER_INBOX_ACCOUNTS', 'inbox-accounts.test.json'), ('BROKER_CONFIG', 'broker.test.json')):
    os.environ[name] = str(ROOT / 'tests' / 'fixtures' / filename)
os.environ['CONFIG_DIR'] = str(ROOT / 'tests' / 'fixtures' / 'no-config')
import app
import coalescer as C
import zulip_wake
from broker_state import BrokerState, StateError
from adapters.stub import StubAdapter

def event(tid, **extra):
    return {'source': 'vikunja', 'task_id': tid if tid >= 9000 else tid + 9000, 'event_name': 'task.updated', 'doer': 'owner', 'assignees': ['eph'], 'labels': [], **extra}

def ticket():
    return '**id:** 9610\n**title:** fix\n**description:** <p>fix</p>\n**done:** false\n**assignees:** [\n  {\n    "id": 1,\n    "username": "dev"\n  }\n]\n**hex_color:** \n**created_by:** {\n  "id": 6,\n  "username": "owner"\n}\n'

@contextmanager
def raises(error):
    try:
        yield
    except error:
        return
    raise AssertionError(f'expected {error.__name__}')

class DrainTests:

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='broker-drain-test-')
        self.path = os.path.join(self.tmp.name, 'state.sqlite3')
        self.saved_windows = (C.INITIAL_WINDOWS.copy(), C.RECEIVE_WINDOWS.copy(), C.INITIAL_DEFAULT)
        C.INITIAL_WINDOWS.clear()
        C.INITIAL_DEFAULT = 0
        C.RECEIVE_WINDOWS.clear()
        # These acceptance tests exercise the configurable strict-FIFO policy. Reviews-first
        # admission and the same durable safety boundaries are covered in test_dispatch_priority.py.
        self.patchers = [patch.dict(os.environ, {'DISPATCH_PRIORITY': 'fifo', 'ROW_CONCURRENCY': '', 'MENTION_HANDOFF_ENABLED': '0'}), patch.object(C, 'EVENTS_LOG', os.path.join(self.tmp.name, 'synthetic-events.jsonl')), patch.object(C, 'COMPLETED_PATH', ''), patch.object(app, 'SPAWN_SH', '/synthetic/not-executed')]
        for p in self.patchers:
            p.start()
        self.brokers = []
        self.calls = []

    def broker(self):
        b = app.Broker(state_path=self.path)
        b.adapter.spawn = lambda key, brief: self.calls.append((key, brief))
        b.adapter._ticket_fetcher = lambda tid: ticket()
        self.brokers.append(b)
        return b

    def restart(self, b):
        b.state.close()
        self.brokers.remove(b)
        return self.broker()

    def tearDown(self):
        for b in self.brokers:
            b.state.close()
        for p in reversed(self.patchers):
            p.stop()
        C.INITIAL_WINDOWS.clear()
        C.INITIAL_WINDOWS.update(self.saved_windows[0])
        C.RECEIVE_WINDOWS.clear()
        C.RECEIVE_WINDOWS.update(self.saved_windows[1])
        C.INITIAL_DEFAULT = self.saved_windows[2]
        self.tmp.cleanup()

    def pump(self, b, n=12):
        for _ in range(n):
            b.tick()

    def test_drain_restart_fifo_and_new_arrivals_cannot_overtake(self):
        b = self.broker()
        b.set_drain(True)
        for tid in (1, 2, 3):
            b.ingest(event(tid))
        self.pump(b)
        assert self.calls == []
        b = self.restart(b)
        assert b.drain_status()['enabled']
        assert b.drain_status()['queued'] == 3
        b.set_drain(False)
        b.ingest(event(4))
        self.pump(b)
        assert [k for (k, _) in self.calls] == [f'vikunja:task:{9000 + i}' for i in (1, 2, 3, 4)]
        assert b.drain_status()['queued'] == 0

    def test_already_pending_is_held_and_never_ages_out_in_journal(self):
        b = self.broker()
        key = b.ingest(event(7))
        b.coalescer.pending[key]['armed_at'] = time.time() - C.PENDING_MAX_AGE * 10
        b.coalescer._save_pending()
        b.set_drain(True)
        b = self.restart(b)
        self.pump(b)
        assert key in b.coalescer.pending
        b.set_drain(False)
        self.pump(b)
        assert [k for (k, _) in self.calls] == [key]

    def test_all_sources_deferred_private_dm_and_guards_preserved(self):
        b = self.broker()
        b.set_drain(True)
        events = [event(8), {'source': 'mr', 'project': 'owner/repo', 'mr_iid': 1, 'event_name': 'mr.opened', 'doer': 'owner'}, {'source': 'gmail', 'account': 'primary', 'event_name': 'gmail.new', 'doer': 'owner'}, {'source': 'zulip', 'bot': 'test-chat', 'sender_id': '9', 'msg_id': '101', 'content': 'synthetic private body', 'event_name': 'zulip.dm', 'doer': 'zulip:9'}, {'source': 'gitlab', 'project': 'owner/repo', 'issue_iid': 2, 'event_name': 'issue.opened', 'doer': 'owner'}]
        for e in events:
            b.ingest(e, internal=e['source'] == 'zulip')
        assert b.ingest(events[3], internal=False) is None
        assert b.drain_status()['queued'] == len(events)
        b = self.restart(b)
        rows = b.state.db.execute('SELECT payload FROM intake ORDER BY id').fetchall()
        assert 'synthetic private body' in rows[3][0]
        assert json.loads(rows[3][0])['internal']
        assert os.stat(self.path).st_mode & 511 == 384
        b.set_drain(False)
        self.pump(b)
        assert len(self.calls) == len(events)
        dm = next((brief for (key, brief) in self.calls if key.startswith('zulip:')))
        assert 'synthetic private body' in dm['dms'][0]['text']

    def test_existing_worker_finishes_new_forwards_are_deferred(self):
        b = self.broker()
        key = b.ingest(event(10))
        b.tick()
        b.coalescer.on_event(event(10, event_name='task.comment.created', comment='already buffered'))
        b.set_drain(True)
        b.ingest(event(10, event_name='task.comment.created', comment='after drain', comment_id=1))
        assert len(b.coalescer.take_forwards(key)) == 1
        assert b.drain_status()['queued'] == 1
        b.coalescer.settle_self(key)
        assert b.drain_status()['live_workers'] == 0
        self.pump(b)
        assert len(self.calls) == 1

    def test_pending_handoff_restores_route_and_comment_context(self):
        b = self.broker()
        b.set_drain(True)
        comment = event(610, event_name='task.comment.created', assignees=[], comment='<p>retry this</p>', comment_id=11)
        b.ingest(comment)
        b.set_drain(False)
        b.tick()
        assert b.state.status()['queued'] == 0
        assert 'vikunja:task:9610' in b.coalescer.pending
        b = self.restart(b)
        assert b.route_stamps['vikunja:task:9610'] == 'code'
        assert 'retry this' in b.adapter._pop_comment_respawn('vikunja:task:9610')
        assert b.adapter._pop_comment_respawn('vikunja:task:9610') is None
        self.pump(b)
        assert len(self.calls) == 1

    def test_rerun_note_persists_and_duplicate_pending_rerun_is_held(self):
        b = self.broker()
        b.set_drain(True)
        assert b.ops_rerun(9610, 'resolution')[0] == 202
        b = self.restart(b)
        b.set_drain(False)
        b.tick()
        b = self.restart(b)
        assert 'resolution' in b.adapter._pop_comment_respawn('vikunja:task:9610')
        self.pump(b)
        assert len(self.calls) == 1

    def test_completed_dedup_survives_restart(self):
        b = self.broker()
        key = b.ingest(event(12))
        b.tick()
        b.coalescer.settle_self(key)
        b = self.restart(b)
        b.set_drain(True)
        b.ingest(event(12, doer='svc-account'))
        b.set_drain(False)
        self.pump(b)
        assert len(self.calls) == 1
        assert b.coalescer.deduped == 1

    def test_row_cap_blocks_fifo_head_until_slot_frees(self):
        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'general=1'}):
            b = self.broker()
            b.set_drain(True)
            b.ingest(event(20))
            b.ingest(event(21))
            b.ingest(event(22))
            b.set_drain(False)
            self.pump(b)
            assert len(self.calls) == 1
            assert b.drain_status()['queued'] == 1
            b.coalescer.settle_self('vikunja:task:9020')
            self.pump(b)
            assert [k for (k, _) in self.calls] == ['vikunja:task:9020', 'vikunja:task:9021']
            assert len(b.coalescer.live) == 1

    def test_no_spawn_after_drain_ack_under_concurrent_ticks(self):
        b = self.broker()
        b.set_drain(True)
        errors = []

        def intake():
            try:
                for i in range(30, 60):
                    b.ingest(event(i))
            except Exception as exc:
                errors.append(exc)
        t = threading.Thread(target=intake)
        t.start()
        self.pump(b, 40)
        t.join(timeout=5)
        assert not t.is_alive()
        assert not errors
        assert b.drain_status()['queued'] == 30
        assert self.calls == []

    def test_interrupted_intake_not_replayed_and_health_is_not_green(self):
        b = self.broker()
        b.set_drain(True)
        b.ingest(event(60))
        b.state.claim(b.state.head()[0])
        b = self.restart(b)
        b.set_drain(False)
        self.pump(b)
        assert self.calls == []
        assert b.drain_status()['dispatching'] == 1
        handler = self.handler(b, '/health')
        (code, body) = handler.do_GET()
        assert code == 503
        assert not body['ok']

    def test_interrupted_launch_held_without_duplicate_and_intent_failure_never_spawns(self):
        b = self.broker()
        key = b.ingest(event(61))
        b.tick()
        b = self.restart(b)
        assert key in b.coalescer.recovery
        b.ingest(event(62))
        self.pump(b)
        assert len(self.calls) == 1
        assert b.drain_status()['recovery_required'] == 1

    def test_journal_failure_returns_503_and_never_acknowledges_or_spawns(self):
        b = self.broker()
        b.set_drain(True)
        b.state.db.execute('PRAGMA query_only=ON')
        handler = self.handler(b, '/event', event(63))
        with patch.object(app, 'check_hmac', return_value=(True, 'synthetic')):
            (code, body) = handler.do_POST()
        assert code == 503
        assert not body['ok']
        with raises(StateError):
            b.tick()
        assert self.calls == []

    def test_transaction_rollback_keeps_queue_and_does_not_spawn(self):
        b = self.broker()
        b.set_drain(True)
        b.ingest(event(64))
        b.set_drain(False)
        with patch.object(b.state, 'finish', side_effect=StateError('synthetic commit failure')):
            with raises(StateError):
                b.tick()
        assert self.calls == []
        b = self.restart(b)
        assert 'vikunja:task:9064' not in b.coalescer.pending
        assert b.drain_status()['dispatching'] == 1

    def test_spawn_failure_is_visible_not_repeated(self):
        b = self.broker()
        b.ingest(event(65))
        with patch.object(b.adapter, 'spawn', side_effect=RuntimeError('synthetic start failure')):
            with raises(RuntimeError):
                b.tick()
        self.pump(b)
        assert self.calls == []
        assert b.drain_status()['recovery_required'] == 1

    def test_intent_checkpoint_failure_does_not_launch_and_preserves_pending(self):
        b = self.broker()
        key = b.ingest(event(66))
        with patch.object(b.coalescer, '_checkpoint', side_effect=StateError('synthetic disk failure')):
            with raises(StateError):
                b.tick()
        assert self.calls == []
        assert key in b.coalescer.pending
        b = self.restart(b)
        self.pump(b)
        assert [k for (k, _) in self.calls] == [key]

    def test_liveness_rearm_keeps_row_cap_and_remains_held_by_drain(self):
        b = self.broker()
        first = b.ingest(event(90))
        b.tick()
        second = b.ingest(event(91))
        b.tick()
        popped = b.coalescer.force_pop(second)
        assert second not in b.route_stamps
        b.set_drain(True)
        b.coalescer.rearm_pending(second, [event(91)], row=popped['row'])
        self.pump(b)
        assert len(self.calls) == 2
        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'general=1'}):
            b.set_drain(False)
            self.pump(b)
            assert len(self.calls) == 2
            b.coalescer.settle_self(first)
            self.pump(b)
        assert [k for (k, _) in self.calls] == [first, second, second]
        assert b.route_stamps[second] == 'general'

    def test_queued_forward_handoff_remains_in_durable_launch_intent(self):
        b = self.broker()
        key = b.ingest(event(67))
        b.tick()
        b.set_drain(True)
        b.ingest(event(67, event_name='task.comment.created', comment_id=2, comment='queued feedback'))
        b.set_drain(False)
        b.tick()
        saved = dict(b.state.get('scheduler')['active'])
        assert saved[key]['events'][-1]['comment'] == 'queued feedback'

    def test_migration_keeps_legacy_files_and_restores_route_from_raw_events(self):
        completed = os.path.join(self.tmp.name, 'completed.json')
        pending = os.path.join(self.tmp.name, 'pending.json')
        Path(completed).write_text('[]')
        legacy = json.dumps([['vikunja:task:9068', [event(68)], time.time()]])
        Path(pending).write_text(legacy)
        with patch.object(C, 'COMPLETED_PATH', completed):
            b = self.broker()
        assert b.route_stamps['vikunja:task:9068'] == 'general'
        self.pump(b)
        assert [k for (k, _) in self.calls] == ['vikunja:task:9068']
        assert Path(pending).read_text() == legacy

    def test_zulip_failed_acceptance_is_not_a_dedup_receipt(self):

        def fail(_evt):
            raise StateError('synthetic unavailable journal')
        wake = zulip_wake.ZulipWake(ingest=fail)
        wake.wake_map = lambda : {'test-bot': {}}
        wake.bots.is_bot = lambda *args: False
        wake.bot_sender_admits = lambda *args: False
        wake.listeners['test-bot'] = type('Listener', (), {'mode': 'wake', 'bot_env': None})()
        params = {'meta': {'kind': 'dm', 'sender_id': '9', 'msg_id': '102', 'age': '0s ago'}, 'content': 'synthetic DM'}
        with patch.dict(os.environ, {'ZULIP_WAKE_ENABLED': '1', 'ZULIP_WAKE_SENDERS': '9'}):
            with raises(StateError):
                wake.handle('test-bot', params)
            assert '102' not in wake._seen
            received = []
            wake.ingest = received.append
            assert wake.handle('test-bot', params) == 'woken'
        assert len(received) == 1
        assert wake.counters['ingest_failed'] == 1

    def test_tfa_launch_deferred_ordered_and_ttl_dedup_persists(self):
        b = self.broker()
        b.set_drain(True)
        b.ingest(event(70))
        b.ingest(event(71, assignees=['fleet']))
        b.ingest(event(71, assignees=['fleet']))
        launched = []
        with patch.object(app.subprocess, 'Popen', side_effect=lambda *a, **k: launched.append(a) or object()), patch.object(app, '_live_tfa_session', return_value=False), patch.object(b.adapter.tracker('vikunja:task:9071'), 'comment', return_value=True), patch.object(app, '_tfa_self_assign', return_value=None):
            self.pump(b)
            assert launched == []
            b.set_drain(False)
            b.tick()
            assert self.calls == []
            b.tick()
            assert len(self.calls) == 1
            assert len(launched) == 1
            b.coalescer.settle_self('vikunja:task:9070')
            b = self.restart(b)
            self.pump(b)
            assert len(launched) == 1

    def test_private_state_single_owner_and_corrupt_state_fail_closed(self):
        b = self.broker()
        with raises(StateError):
            BrokerState(self.path)
        b.state.db.execute("INSERT OR REPLACE INTO state VALUES ('draining','invalid-json')")
        with raises(StateError):
            b.drain_status()

    def handler(self, b, path, body=None, headers=None):
        h = object.__new__(app.Handler)
        h.path = path
        raw = json.dumps(body).encode() if body is not None else b''
        h.rfile = io.BytesIO(raw)
        h.headers = {'Content-Length': str(len(raw)), **(headers or {})}
        h.client_address = ('127.0.0.1', 1234)
        h._reply = lambda code, obj: (code, obj)
        app.BROKER = b
        return h

    def test_admin_auth_and_boolean_validation_and_health_contract(self):
        b = self.broker()
        name = app.RERUN_TOKEN_ENV
        with patch.dict(os.environ, {name: ''}):
            assert self.handler(b, '/drain', {'enabled': True}).do_POST()[0] == 401
        with patch.dict(os.environ, {name: 'synthetic-admin'}):
            assert self.handler(b, '/drain', {'enabled': True}, {'X-Broker-Rerun-Token': 'wrong'}).do_POST()[0] == 401
            hdr = {'X-Broker-Rerun-Token': 'synthetic-admin'}
            for body in ({'enabled': 'false'}, {'enabled': 0}, {}, [], {'enabled': True, 'extra': 1}):
                assert self.handler(b, '/drain', body, hdr).do_POST()[0] == 400
            (code, body) = self.handler(b, '/drain', {'enabled': True}, hdr).do_POST()
            assert code == 200
            assert body['drain']['enabled']
            b.ingest(event(80))
            (code, body) = self.handler(b, '/health').do_GET()
            assert code == 200
            assert body['drain']['queued'] == 1
            assert body['live_workers'] == 0
            assert not body['drain']['detached_tfas_tracked']

class BootstrapTests(DrainTests):
    def test_new_flag_is_committed_before_scheduler_construction(self):
        original = app.Coalescer
        seen = []

        def construct(*args, **kwargs):
            with sqlite3.connect(self.path) as observer:
                assert observer.execute("SELECT value FROM state WHERE name='draining'").fetchone() == ('true',)
            seen.append('committed before scheduler')
            return original(*args, **kwargs)

        with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': '1'}), \
                patch.object(app, 'Coalescer', side_effect=construct):
            b = self.broker()
        assert seen == ['committed before scheduler']
        assert b.drain_status()['enabled'] is True
        activated = []

        def activate(*args, **kwargs):
            assert b.drain_status()['enabled'] is True
            activated.append('drained')

        with patch.object(app, 'Broker', return_value=b), \
                patch.object(app.signal, 'signal'), \
                patch.object(app, '_sweep_stale_inbox_chat_mcp_configs'), \
                patch.object(app, '_sweep_stale_thread_dirs'), \
                patch.object(app.threading, 'Thread') as thread, \
                patch.object(b.liveness, 'start', side_effect=activate), \
                patch.object(b.zulip_wake, 'start', side_effect=activate), \
                patch.object(app, 'ThreadingHTTPServer') as server:
            thread.return_value.start.side_effect = activate
            server.return_value.serve_forever.side_effect = activate
            app.main()
        assert len(activated) == 4

    def test_bootstrap_holds_legacy_pending_and_all_immediate_seams(self):
        completed = os.path.join(self.tmp.name, 'completed.json')
        key = 'vikunja:task:9700'
        with open(os.path.join(self.tmp.name, 'pending.json'), 'w') as f:
            json.dump([[key, [event(9700)], time.time()]], f)
        with patch.object(C, 'COMPLETED_PATH', completed), \
                patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': 'true'}):
            b = self.broker()
        assert key in b.coalescer.pending
        with patch.object(b, '_dispatch_intake', side_effect=AssertionError('dispatch while draining')):
            b.ingest(event(9701))
            b.ingest(event(9702, assignees=['fleet']))
            b.ingest({'source': 'zulip', 'bot': 'test-chat', 'sender_id': '9',
                      'msg_id': '9703', 'content': 'synthetic retained DM'}, internal=True)
            assert b.ops_rerun(9704)[0] == 202
            self.pump(b)
        assert self.calls == []
        assert b.drain_status()['queued'] == 4
        assert b.drain_status()['pending'] == 1
        b = self.restart(b)
        assert b.drain_status()['enabled'] is True
        assert b.drain_status()['queued'] == 4
        self.pump(b)
        assert self.calls == []

    def test_startup_setting_true_spellings(self):
        for i, value in enumerate(('1', 'true', 'yes', 'on', ' TRUE ', 'On')):
            self.path = os.path.join(self.tmp.name, f'true-{i}.sqlite3')
            with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': value}):
                assert self.broker().drain_status()['enabled'] is True

    def test_startup_setting_default_and_false_spellings(self):
        assert self.broker().drain_status()['enabled'] is False
        for i, value in enumerate(('0', 'false', 'no', 'off', ' FALSE ', 'Off')):
            self.path = os.path.join(self.tmp.name, f'false-{i}.sqlite3')
            with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': value}):
                assert self.broker().drain_status()['enabled'] is False

    def test_invalid_setting_stops_main_before_any_activation(self):
        for value in ('', '2', 'null', 'maybe', '[]'):
            with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': value}), \
                    patch.object(app, 'BrokerState') as store, \
                    patch.object(app.threading, 'Thread') as thread, \
                    patch.object(app, 'ThreadingHTTPServer') as server, \
                    patch.object(app, '_sweep_stale_inbox_chat_mcp_configs') as sweep:
                with raises(StateError):
                    app.main()
                assert not store.called and not thread.called and not server.called and not sweep.called

    def test_bootstrap_without_durable_path_fails_before_scheduler(self):
        with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': '1'}), \
                patch.object(app, 'Coalescer') as scheduler, \
                patch.object(app, 'BrokerState') as store:
            with raises(StateError):
                app.Broker(state_path='')
            assert not scheduler.called and not store.called

    def test_setting_uses_existing_loader_and_real_environment_precedence(self):
        name = 'BROKER_DRAIN_ON_NEW_JOURNAL'
        with patch.dict(os.environ):
            os.environ.pop(name, None)
            app.site_env.load_settings({'settings': {name: True}})
            assert self.broker().drain_status()['enabled'] is True
            self.path = os.path.join(self.tmp.name, 'environment-wins.sqlite3')
            os.environ[name] = '0'
            app.site_env.load_settings({'settings': {name: True}})
            assert self.broker().drain_status()['enabled'] is False

    def test_existing_true_false_absent_and_work_are_preserved(self):
        for i, raw in enumerate((' true ', ' false ', None)):
            path = os.path.join(self.tmp.name, f'existing-{i}.sqlite3')
            state = BrokerState(path)
            if raw is None:
                state.db.execute("DELETE FROM state WHERE name='draining'")
            else:
                state.db.execute("UPDATE state SET value=? WHERE name='draining'", (raw,))
            state.put('mention_seen', {'synthetic': 123})
            state.put('tfa_launched', {'9700': 456})
            state.enqueue('event', {'event': event(9700), 'internal': False})
            state.enqueue('rerun', {'task_id': 9701, 'note': 'synthetic'})
            state.claim(2)
            before = (state.db.execute('SELECT * FROM state ORDER BY name').fetchall(),
                      state.db.execute('SELECT * FROM intake ORDER BY id').fetchall(),
                      state.db.execute('SELECT * FROM sqlite_sequence').fetchall())
            state.close()
            reopened = BrokerState(path, drain_on_create=True)
            try:
                assert (reopened.db.execute('SELECT * FROM state ORDER BY name').fetchall(),
                        reopened.db.execute('SELECT * FROM intake ORDER BY id').fetchall(),
                        reopened.db.execute('SELECT * FROM sqlite_sequence').fetchall()) == before
                assert reopened.get('draining', False) is (raw == ' true ')
            finally:
                reopened.close()

    def test_existing_complete_empty_legacy_schema_stays_implicitly_false(self):
        self._legacy_schema(self.path)
        state = BrokerState(self.path, drain_on_create=True)
        try:
            assert state.get('draining', False) is False
            assert state.db.execute('SELECT * FROM state').fetchall() == []
        finally:
            state.close()

    def test_authenticated_drain_off_is_not_reenabled_on_restart(self):
        with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': '1'}):
            b = self.broker()
            assert b.drain_status()['enabled'] is True
            with patch.dict(os.environ, {app.RERUN_TOKEN_ENV: 'synthetic-admin'}):
                code, body = self.handler(b, '/drain', {'enabled': False},
                                          {'X-Broker-Rerun-Token': 'synthetic-admin'}).do_POST()
                assert code == 200 and body['drain']['enabled'] is False
            b = self.restart(b)
            assert b.drain_status()['enabled'] is False

    def test_removing_bootstrap_setting_does_not_clear_persisted_true(self):
        with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': 'yes'}):
            b = self.broker()
        with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': '0'}):
            b = self.restart(b)
        assert b.drain_status()['enabled'] is True

    def _legacy_schema(self, path):
        # Actual pre-bootstrap DDL; no initial draining row.
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS intake (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                       'kind TEXT NOT NULL, payload TEXT NOT NULL, phase TEXT NOT NULL, arrived REAL NOT NULL)')

    def _assert_owner_released(self, path):
        fd = os.open(path + '.lock', os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_generated_index_sequence_and_analyze_objects_are_compatible(self):
        self._legacy_schema(self.path)
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO intake(kind,payload,phase,arrived) VALUES ('event','{}','queued',1)")
            db.execute('ANALYZE')
            names = {r[0] for r in db.execute('SELECT name FROM sqlite_master')}
            assert {'sqlite_autoindex_state_1', 'sqlite_sequence', 'sqlite_stat1'} <= names
        state = BrokerState(self.path, drain_on_create=True)
        try:
            assert state.status()['queued'] == 1
            assert state.get('draining', False) is False
        finally:
            state.close()

    def test_incompatible_valid_sqlite_is_rejected_without_schema_repair(self):
        state_sql = 'CREATE TABLE state (name TEXT PRIMARY KEY, value TEXT NOT NULL)'
        intake_sql = ('CREATE TABLE intake (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, '
                      'payload TEXT NOT NULL, phase TEXT NOT NULL, arrived REAL NOT NULL)')
        variants = [
            ['CREATE TABLE unrelated (data TEXT)'], [state_sql],
            [state_sql, intake_sql.replace('kind TEXT', 'kind BLOB')],
            [state_sql.replace('value TEXT NOT NULL', "value TEXT NOT NULL CHECK(value <> '')"), intake_sql],
            [state_sql, intake_sql.replace('AUTOINCREMENT', '')],
            [state_sql, intake_sql.replace('payload TEXT', 'payload TEXT COLLATE NOCASE')],
            [state_sql, intake_sql, 'CREATE VIEW unexpected AS SELECT name FROM state'],
            [state_sql, intake_sql, 'CREATE INDEX unexpected ON intake(phase)'],
            [state_sql, intake_sql, 'CREATE TRIGGER unexpected AFTER INSERT ON state BEGIN SELECT 1; END'],
        ]
        for i, statements in enumerate(variants):
            path = os.path.join(self.tmp.name, f'incompatible-{i}.sqlite3')
            with sqlite3.connect(path) as db:
                for sql in statements:
                    db.execute(sql)
                assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
                before = db.execute('SELECT * FROM sqlite_master ORDER BY name').fetchall()
            with raises(StateError):
                BrokerState(path, drain_on_create=True)
            with sqlite3.connect(path) as db:
                assert db.execute('SELECT * FROM sqlite_master ORDER BY name').fetchall() == before
            self._assert_owner_released(path)

    def test_trigger_sharing_table_name_is_rejected_in_both_creation_orders(self):
        state_sql = 'CREATE TABLE state (name TEXT PRIMARY KEY, value TEXT NOT NULL)'
        intake_sql = ('CREATE TABLE intake (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, '
                      'payload TEXT NOT NULL, phase TEXT NOT NULL, arrived REAL NOT NULL)')
        trigger_sql = 'CREATE TRIGGER state AFTER INSERT ON intake BEGIN SELECT 1; END'

        def snapshot(path):
            with sqlite3.connect(path) as db:
                return (db.execute('SELECT * FROM sqlite_master ORDER BY type,name').fetchall(),
                        db.execute('SELECT * FROM state ORDER BY name').fetchall(),
                        db.execute('SELECT * FROM intake ORDER BY id').fetchall(),
                        db.execute('SELECT * FROM sqlite_sequence ORDER BY name').fetchall())

        for i, statements in enumerate(((intake_sql, trigger_sql, state_sql),
                                        (intake_sql, state_sql, trigger_sql))):
            path = os.path.join(self.tmp.name, f'colliding-trigger-{i}.sqlite3')
            with sqlite3.connect(path) as db:
                for sql in statements:
                    db.execute(sql)
                db.execute("INSERT INTO state VALUES ('draining', 'false')")
                db.execute("INSERT INTO state VALUES ('mention_seen', '{\"synthetic\": 123}')")
                db.execute("INSERT INTO intake(kind,payload,phase,arrived) VALUES ('event','{}','queued',1)")
                assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
            before = snapshot(path)
            rejection = None
            try:
                accepted = BrokerState(path, drain_on_create=True)
            except StateError as exc:
                rejection = str(exc)
            else:
                accepted.close()  # Release resources even when the regression exposes acceptance.
            assert snapshot(path) == before
            self._assert_owner_released(path)
            assert rejection == 'broker journal schema is incompatible', f'accepted collision order {i}'

            with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': '1', 'BROKER_STATE_PATH': path}), \
                    patch.object(app, 'Coalescer') as scheduler, \
                    patch.object(app.threading, 'Thread') as thread, \
                    patch.object(app, 'ThreadingHTTPServer') as server, \
                    patch.object(app.LivenessDriver, 'start') as liveness, \
                    patch.object(app.zulip_wake.ZulipWake, 'start') as listener, \
                    patch.object(app, '_sweep_stale_inbox_chat_mcp_configs') as sweep_inbox, \
                    patch.object(app, '_sweep_stale_thread_dirs') as sweep_threads:
                with raises(StateError):
                    app.main()
                assert not any(mock.called for mock in (scheduler, thread, server, liveness,
                                                       listener, sweep_inbox, sweep_threads))
            assert snapshot(path) == before
            self._assert_owner_released(path)

    def test_nonboolean_or_malformed_persisted_drain_never_becomes_false(self):
        for i, raw in enumerate(('null', '0', '1', '""', '"false"', '[]', '{}', 'NaN', 'true trailing')):
            path = os.path.join(self.tmp.name, f'bad-flag-{i}.sqlite3')
            state = BrokerState(path)
            state.db.execute("UPDATE state SET value=? WHERE name='draining'", (raw,))
            state.close()
            with raises(StateError):
                BrokerState(path, drain_on_create=True)
            with sqlite3.connect(path) as db:
                assert db.execute("SELECT value FROM state WHERE name='draining'").fetchone() == (raw,)
            self._assert_owner_released(path)

    def test_runtime_nonboolean_drain_reports_unhealthy(self):
        b = self.broker()
        b.state.db.execute("UPDATE state SET value='0' WHERE name='draining'")
        assert self.handler(b, '/health').do_GET()[0] == 503
        with raises(StateError):
            b.ingest(event(9700))

    def test_corrupt_journal_is_not_reset_and_owner_is_released(self):
        Path(self.path).write_bytes(b'synthetic not SQLite')
        with raises(StateError):
            BrokerState(self.path, drain_on_create=True)
        assert Path(self.path).read_bytes() == b'synthetic not SQLite'
        self._assert_owner_released(self.path)

    def test_schema_classification_holds_owner_and_write_transaction(self):
        self._legacy_schema(self.path)
        original = BrokerState._schema
        inspected = []

        def inspect(db):
            if db.execute('PRAGMA database_list').fetchone()[2] == self.path:
                assert db.in_transaction
                fd = os.open(self.path + '.lock', os.O_RDWR)
                try:
                    with raises(BlockingIOError):
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(fd)
                inspected.append(True)
            return original(db)

        with patch.object(BrokerState, '_schema', side_effect=inspect):
            state = BrokerState(self.path, drain_on_create=True)
        state.close()
        assert inspected == [True]

    def test_initialization_interruption_and_uncertain_commit_fail_closed(self):
        connect = sqlite3.connect
        for i, (target, after, error) in enumerate((
                ('CREATE TABLE intake', True, KeyboardInterrupt),
                ('INSERT INTO state', False, KeyboardInterrupt),
                ('COMMIT', False, sqlite3.OperationalError),
                ('COMMIT', True, KeyboardInterrupt))):
            path = os.path.join(self.tmp.name, f'interrupted-{i}.sqlite3')

            class Connection:
                def __init__(self, *args, **kwargs):
                    self.db = connect(*args, **kwargs)

                def __getattr__(self, name):
                    return getattr(self.db, name)

                def execute(self, sql, args=()):
                    hit = sql.startswith(target)
                    if hit and not after:
                        raise error('synthetic initialization interruption')
                    result = self.db.execute(sql, args)
                    if hit:
                        raise error('synthetic initialization interruption')
                    return result

            with patch.object(sqlite3, 'connect', side_effect=Connection):
                with raises(StateError if error is sqlite3.OperationalError else error):
                    BrokerState(path, drain_on_create=True)
            self._assert_owner_released(path)
            with connect(path) as db:
                if target == 'COMMIT' and after:
                    assert db.execute("SELECT value FROM state WHERE name='draining'").fetchone() == ('true',)
                else:
                    assert db.execute('SELECT * FROM sqlite_master').fetchall() == []
            # Retry under the still-opted-in contract must never reopen drain-off.
            state = BrokerState(path, drain_on_create=True)
            assert state.get('draining', False) is True
            state.close()

    def test_storage_validation_failure_stops_main_before_activation(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE unrelated (value TEXT)')
        with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': '1', 'BROKER_STATE_PATH': self.path}), \
                patch.object(app.threading, 'Thread') as thread, \
                patch.object(app, 'ThreadingHTTPServer') as server, \
                patch.object(app, 'Coalescer') as scheduler:
            with raises(StateError):
                app.main()
            assert not thread.called and not server.called and not scheduler.called
        self._assert_owner_released(self.path)

    def test_commit_failure_stops_main_before_activation_without_fallback(self):
        connect = sqlite3.connect

        class Connection:
            def __init__(self, *args, **kwargs):
                self.db = connect(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, sql, args=()):
                if sql == 'COMMIT':
                    raise sqlite3.OperationalError('synthetic commit failure')
                return self.db.execute(sql, args)

        with patch.dict(os.environ, {'BROKER_DRAIN_ON_NEW_JOURNAL': '1', 'BROKER_STATE_PATH': self.path}), \
                patch.object(sqlite3, 'connect', side_effect=Connection), \
                patch.object(app.threading, 'Thread') as thread, \
                patch.object(app, 'ThreadingHTTPServer') as server, \
                patch.object(app, 'Coalescer') as scheduler:
            with raises(StateError):
                app.main()
            assert not thread.called and not server.called and not scheduler.called
        self._assert_owner_released(self.path)
        with connect(self.path) as db:
            assert db.execute('SELECT * FROM sqlite_master').fetchall() == []


if __name__ == '__main__':
    failures = []
    tests = [(cls, name) for cls in (DrainTests, BootstrapTests)
             for name in sorted(cls.__dict__) if name.startswith('test_')]
    for cls, name in tests:
        fixture = cls()
        fixture.setUp()
        try:
            getattr(fixture, name)()
            print('PASS ' + name)
        except BaseException as exc:
            failures.append(name)
            import traceback
            traceback.print_exc()
            print('FAIL ' + name)
        finally:
            fixture.tearDown()
    print(f'{len(tests) - len(failures)} passed, {len(failures)} failed')
    # Keep the new scheduling regressions on the same tools/check.sh gate without
    # requiring pytest on a stdlib-only broker installation.
    import unittest
    from test_dispatch_priority import DispatchTests, RepoLockTests
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (DispatchTests, RepoLockTests))
    priority_result = unittest.TextTestRunner().run(suite)
    sys.exit(1 if failures or not priority_result.wasSuccessful() else 0)
