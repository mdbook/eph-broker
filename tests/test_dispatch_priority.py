"""Offline dispatch ordering: real durable handoff, fake workers, no external services."""
import html
import json
import os
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_drain import DrainTests as _DrainFixture, app, C, event, ticket
from test_drain_boundaries import BoundaryTests as _BoundaryFixture
from adapters.stub import StubAdapter
from adapters import cc
from dataclasses import replace
from dispatch import RepoLock, reviews_first


def isolated_setup(fixture):
    # The legacy script suites normally run in separate processes. When exposed to
    # pytest, a sandbox-induced failure there can leave their temporary config path
    # in os.environ; pin our inert inputs independently, and restore afterwards.
    root = Path(__file__).resolve().parent / 'fixtures'
    env = patch.dict(os.environ, {
        'BROKER_SITE_ENV': str(root / 'site.test.env'),
        'BROKER_PROVENANCE_PATH': str(root / 'provenance.test.json'),
        'BROKER_INBOX_ACCOUNTS': str(root / 'inbox-accounts.test.json'),
        'BROKER_CONFIG': str(root / 'broker.test.json'),
        'CONFIG_DIR': str(root / 'no-config'),
        'ROUTES_PATH': '/nonexistent/eph-test/routes.json',
        'BROKER_DRAIN_ON_NEW_JOURNAL': '0',
    })
    env.start()
    # A legacy socket self-test sets umask before bind; a sandbox-denied bind can
    # leave it restrictive in this pytest process. Scope our fixture's mask and restore it.
    mask = os.umask(0o077)
    fixture.addCleanup(os.umask, mask)
    _DrainFixture.setUp(fixture)
    fixture.patchers.insert(0, env)


class DispatchTests(unittest.TestCase):
    tearDown = _DrainFixture.tearDown
    broker = _DrainFixture.broker
    restart = _DrainFixture.restart

    def setUp(self):
        isolated_setup(self)
        p = patch.dict(os.environ, {'DISPATCH_PRIORITY': 'reviews-first'})
        p.start()
        self.patchers.append(p)

    def wait_metadata(self, co):
        deadline = time.monotonic() + 5
        while True:
            with co.lock:
                if not co._dispatch_resolving:
                    return
            assert time.monotonic() < deadline, 'metadata resolver did not finish'
            time.sleep(0.001)

    def ready_tick(self, b):
        # These ordering tests need known targets. Model the production debounce/cap
        # wait explicitly, rather than depending on background-thread scheduling speed.
        co = getattr(b, 'coalescer', b)
        with patch.object(co, '_row_capped', return_value=True):
            b.tick()
        self.wait_metadata(co)
        b.tick()

    def pump(self, b, n=12):
        for _ in range(n):
            self.ready_tick(b)

    def review(self, iid=1, repo='/srv/git/sample-app', **extra):
        return {'source': 'mr', 'event_name': 'mr.opened', 'project': 'owner/sample-app',
                'mr_iid': iid, 'repo_path': repo, 'source_branch': 'eph/fix', **extra}

    def configured_broker(self):
        b = self.broker()
        b.adapter._ticket_fetcher = lambda tid: ticket().replace('<p>fix</p>', '<p>repo: /srv/git/sample-app</p>')
        return b

    def check_model_label_dispatch(self, path, label):
        """Exercise intake through first-turn admission, not just apply_model_label (ticket 3062)."""
        import routing_overrides

        class CapturedTurn(Exception):
            pass

        seen = []

        def capture(worker, route, *args, **kwargs):
            seen.append((route.adapter, route.backend, route.model))
            raise CapturedTurn()  # stop before any CLI, publication or warm-worker loop

        def default(route):
            return replace(route, backend='sol', adapter='codex', pin=False, model_pin='')

        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'code=4', 'REPO_INFER_ENABLED': '0'}), \
                patch.object(routing_overrides.OVERRIDES, 'apply', side_effect=default), \
                patch.object(app.routing, 'resolve_model', return_value='claude-sonnet-5'), \
                patch.object(app.routing, 'CODE_REPO_RUNNERS', {}), \
                patch.object(app.routing, 'code_repo_allowed', return_value=True), \
                patch.object(cc.repo_companions, 'cut', return_value=[]), \
                patch.object(cc.steer, 'clear_inbox'):
            b = self.configured_broker()
            b.adapter._ticket_fetcher = lambda tid: ticket().replace('9610', str(tid)).replace(
                '<p>fix</p>', '<p>repo: /srv/git/sample-app</p>').replace(
                '**created_by:**', '**labels:** ' + json.dumps([{'title': label}]) +
                '\n**created_by:**')
            key = 'vikunja:task:9864'
            if path == 'capacity':
                for tid in range(9860, 9864):
                    b.ingest(event(tid, assignees=['dev']))
                    self.ready_tick(b)
                assert len(self.calls) == 4
            if path in ('rerun', 'comment'):
                b.coalescer.completed[key] = time.time()
            if path == 'rerun':
                assert b.ops_rerun(9864, 'try again')[0] == 200
            elif path == 'comment':
                b.ingest(event(9864, event_name='task.comment.created', assignees=[],
                               comment='try again', comment_id=9864))
            else:
                # No model label in the event/brief: the launch must use the fresh ticket fetch.
                b.ingest(event(9864, assignees=['dev']))
            self.ready_tick(b)
            if path == 'capacity':
                assert key in b.coalescer.pending and len(self.calls) == 4
                assert b.coalescer.settle_self('vikunja:task:9860') is not None
                self.ready_tick(b)
            assert [k for k, _ in self.calls].count(key) == 1
            brief = next(brief for k, brief in self.calls if k == key)
            b.adapter.worktree_mgr = SimpleNamespace(make=lambda *a, **kw: self.tmp.name,
                                                     remove=lambda *a: None)
            b.adapter.prompt_loader = SimpleNamespace(load=lambda *a: 'SYSTEM')
            with patch.object(b.adapter, '_one_turn', side_effect=capture), \
                    patch.object(b.adapter, '_save_patch'), patch.object(b.adapter, '_alert_ops'), \
                    patch.object(b.adapter, '_flush_ops'):
                b.adapter._run_worker(key, brief, 'model-label-regression')
            assert seen == [('cc', 'claude', 'opus')], (path, label, seen)

    def test_model_label_immediate_dispatch(self):
        self.check_model_label_dispatch('immediate', 'opus')

    def test_model_label_capacity_deferred_dispatch(self):
        self.check_model_label_dispatch('capacity', 'opus')

    def test_model_label_rerun_dispatch(self):
        self.check_model_label_dispatch('rerun', 'model:opus')

    def test_model_label_comment_respawn_dispatch(self):
        self.check_model_label_dispatch('comment', 'opus')

    def check_untrusted_labels_park(self, blob, cause, fallback=None):
        import ticket_fetch as TF

        b = self.configured_broker()
        key = 'vikunja:task:9864'
        b.adapter._ticket_fetcher = TF.fetch_ticket
        with patch.object(TF, '_call_mcp', side_effect=lambda call, mcp, kind, tid:
                          fallback if kind == 'list' else blob), \
                patch.object(b.adapter, '_tracker_comment', return_value=True) as comment, \
                patch.object(b.adapter, '_tracker_label', return_value=True) as label, \
                patch.object(b.adapter, '_alert_ops') as alert, \
                patch.object(b.adapter.worktree_mgr, 'make') as cut, \
                patch.object(b.adapter, '_one_turn') as turn:
            fetched = b.adapter._tracker_fetch(key)
            assert fetched.creator is None and fetched.labels == [], 'never trust the rejected labels'
            # The real fetch retains the cause and label presence even when labels are after the cut.
            assert TF.ticket_has_labels(fetched.text)
            assert cause in TF.ticket_meta_error(fetched.text)
            b.adapter._run_worker(key, {'task_id': 9864}, 'untrusted-label-regression')
            cut.assert_not_called()
            turn.assert_not_called()
            assert key not in b.coalescer.completed, 'a fixable park must stay re-armable'
            comment.assert_called_once()
            assert cause in html.unescape(comment.call_args.args[1])
            label.assert_called_once_with(key, 'parked')
            alert.assert_called_once()
            assert cause in alert.call_args.args[0]
            assert alert.call_args.kwargs['subject'] == 'reject:' + key
            # /rerun must identify the same cause, not just say "ticket unreadable".
            status, out = b.ops_rerun(9864)
            assert status == 409 and cause in out['error'], out
            assert not self.calls
            comment.reset_mock(); label.reset_mock(); alert.reset_mock()
            # Multiple cases in one test share the durable fixture; each is a new comment event.
            cid = 9864 + len(b._rerun_seen)
            evt = event(9864, event_name='task.comment.created', comment='retry', comment_id=cid)
            assert not b._comment_respawn(key, evt)
            assert not b._comment_respawn(key, evt), 'webhook retries must not repeat rejection notices'
            # The rejection reservation is durable, not just an in-memory notification guard.
            assert any(row[:2] == [key, cid] for row in b.state.get('rerun_seen'))
            comment.assert_called_once()
            label.assert_called_once_with(key, 'parked')
            alert.assert_called_once()
            assert cause in html.unescape(comment.call_args.args[1]) and cause in alert.call_args.args[0]
            cut.assert_not_called()
            turn.assert_not_called()
        b.state.close()
        self.brokers.remove(b)

    def test_label_list_get_fallback_parks_without_trusting_label_values(self):
        import ticket_fetch as TF

        fallback = ('## Success\nFound 1 task\n\n'
                    '### 1. **Fix it** (ID: 9864)\n- **Status:** ❌ Not Done\n'
                    '- **Description:** <p>repo: /srv/git/sample-app</p>\n')
        label_list = '## Success\n**Results:** 1 item(s)\n\n1. **opus** (ID: 20)'
        self.check_untrusted_labels_park(label_list, 'missing ticket done/created_by fields', fallback)
        # A rejected get body must preserve presence too, not just the label-list bug rendering.
        wrong_id = ticket().replace('**created_by:**', '**labels:** [{"title": "opus"}]\n**created_by:**')
        self.check_untrusted_labels_park(wrong_id, 'missing ticket done/created_by fields', fallback)
        with patch.object(TF, '_call_mcp', side_effect=[label_list, fallback]):
            fetched = TF.fetch_ticket(9864)
        assert 'opus' not in fetched and TF.ticket_meta(fetched) is None
        assert TF.ticket_labels(fetched) == [] and TF.ticket_has_labels(fetched)
        assert not TF.ticket_has_labels('## Success\n**Results:** 0 item(s)')

    def test_over_cap_ticket_labels_park_with_length_and_cap(self):
        import ticket_fetch as TF

        blob = ticket().replace('9610', '9864').replace('<p>fix</p>', 'x' * TF.MAX_TICKET_TEXT).replace(
            '**created_by:**', '**labels:** [{"title": "opus"}]\n**created_by:**')
        assert blob.index('**labels:**') > TF.MAX_TICKET_TEXT
        self.check_untrusted_labels_park(
            blob, f'ticket blob length {len(blob)} >= MAX_TICKET_TEXT cap {TF.MAX_TICKET_TEXT}')
        # Increasing the configured cap permits a complete, otherwise valid read, not spoofed metadata.
        with patch.object(TF, 'MAX_TICKET_TEXT', len(blob) + 1), \
                patch.object(TF, '_call_mcp', return_value=blob):
            assert TF.ticket_meta(TF.fetch_ticket(9864)) == (['dev'], 'owner', ['opus'])

    def test_duplicate_ticket_fields_park_without_honoring_labels(self):
        blob = ticket().replace('9610', '9864').replace(
            '**created_by:**', '**labels:** [{"title": "opus"}]\n**created_by:**')
        for field, value in (('created_by', '{"username": "owner"}'), ('labels', '[{"title": "opus"}]'),
                             ('assignees', '[]'), ('done', 'false'), ('description', 'lookalike')):
            with self.subTest(field=field):
                duplicate = blob.replace('<p>fix</p>', f'<p>fix</p>\n**{field}:** {value}\n')
                self.check_untrusted_labels_park(duplicate, 'duplicate ticket metadata fields: ' + field)

    def test_ticket_text_cap_setting_and_unlabelled_metadata_failure(self):
        import ticket_fetch as TF

        for value, expected in (('30000', 30000), ('1', 1), ('0', 20000), ('-1', 20000), ('bad', 20000)):
            with patch.dict(os.environ, {'MAX_TICKET_TEXT': value}), patch.object(TF.log, 'error') as error:
                assert TF._ticket_text_cap() == expected
                assert error.call_count == int(value in ('0', '-1', 'bad'))
        with patch.dict(os.environ):
            os.environ.pop('MAX_TICKET_TEXT', None)
            assert TF._ticket_text_cap() == 20000
        b = self.configured_broker()
        with patch.object(b.adapter, '_post_ticket_note') as comment, patch.object(b.adapter, '_alert_ops') as alert:
            for blob in (ticket().replace('**created_by:**', '**description:** duplicate\n**created_by:**'),
                         ticket().replace('**created_by:**', '**labels:** []\n**description:** x\n**created_by:**')):
                b.adapter._ticket_fetcher = lambda tid: blob
                fetched = b.adapter._tracker_fetch('vikunja:task:9864')
                assert fetched.creator is None
                assert not b.adapter._park_untrusted_ticket_labels(fetched.ref, fetched, 'unlabelled')
            comment.assert_not_called()
            alert.assert_not_called()

    def test_repo_pin_wins_over_untrusted_labels_after_target_resolution(self):
        import routing_overrides
        import ticket_fetch as TF

        class CapturedTurn(Exception):
            pass

        base = ticket().replace('9610', '9864').replace(
            '<p>fix</p>', '<p>repo: /srv/git/sample-app</p>').replace(
            '**created_by:**', '**labels:** [{"title": "sonnet"}]\n**created_by:**')
        blobs = [TF._TicketText(base.replace('</p>', '</p>' + 'x' * TF.MAX_TICKET_TEXT)),
                 TF._TicketText(base.replace('**done:**', '**labels:** []\n**done:**')),
                 TF._TicketText('**title:** fix\n**done:** false\n'
                                '**description:** <p>repo: /srv/git/sample-app</p>', label_presence=True)]
        path = Path(self.tmp.name) / 'routing.json'
        path.write_text(json.dumps({'repo_model_pins': {'/srv/git/sample-app': 'opus'}}))
        overrides = routing_overrides.RoutingOverrides(str(path))
        with patch.object(routing_overrides, 'OVERRIDES', overrides), \
                patch.dict(os.environ, {'REPO_INFER_ENABLED': '0'}), \
                patch.object(app.routing, 'code_repo_allowed', return_value=True), \
                patch.object(app.routing, 'CODE_REPO_RUNNERS', {}), \
                patch.object(cc.repo_companions, 'cut', return_value=[]), \
                patch.object(cc.steer, 'clear_inbox'):
            b = self.configured_broker()
            b.route_stamps['vikunja:task:9864'] = 'code'
            b.adapter.worktree_mgr = SimpleNamespace(make=lambda *a, **kw: self.tmp.name,
                                                     remove=lambda *a: None)
            b.adapter.prompt_loader = SimpleNamespace(load=lambda *a: 'SYSTEM')
            for blob in blobs:
                with self.subTest(cause=TF.ticket_meta_error(blob)):
                    assert TF.ticket_meta(blob) is None and TF.ticket_has_labels(blob)
                    b.adapter._ticket_fetcher = lambda tid: blob
                    with patch.object(b.adapter, '_one_turn', side_effect=CapturedTurn) as turn, \
                            patch.object(b.adapter, '_post_ticket_note') as comment, \
                            patch.object(b.adapter, '_save_patch'), patch.object(b.adapter, '_alert_ops'), \
                            patch.object(b.adapter, '_flush_ops'):
                        b.adapter._run_worker('vikunja:task:9864', {'task_id': 9864}, 'repo-pin-regression')
                        turn.assert_called_once()
                        route = turn.call_args.args[1]
                        assert (route.adapter, route.backend, route.model) == ('cc', 'claude', 'opus')
                        assert route.repo_model_pin == '/srv/git/sample-app'
                        comment.assert_not_called()

    def check_adapter_lock_order(self, policy, followups):
        with patch.dict(os.environ):
            if policy is None:
                os.environ.pop('DISPATCH_PRIORITY', None)
            else:
                os.environ['DISPATCH_PRIORITY'] = policy
            adapter = self.configured_broker().adapter
            lock = RepoLock()
            lock.acquire()
            held = True
            seen, errors, threads = [], [], []
            retry, timed_out, dispatched = [threading.Event() for _ in range(3)]
            acquire = lock.acquire

            def observed_acquire(*args, **kwargs):
                acquired = acquire(*args, **kwargs)
                if not acquired and threading.current_thread() is threads[0]:
                    # Model descheduling between a timed-out call and its retry.
                    # A single cancellable acquisition never opens this FIFO gap.
                    timed_out.set()
                    assert retry.wait(5)
                return acquired

            def run(key):
                try:
                    adapter._acquire_wait(key, lock, 'repo_lock')
                    try:
                        seen.append(key)
                        dispatched.set()
                    finally:
                        lock.release()
                except BaseException as exc:
                    errors.append(exc)

            def wait_queued(count):
                deadline = time.monotonic() + 5
                while True:
                    with lock._condition:
                        if len(lock._waiters) == count:
                            return
                    assert time.monotonic() < deadline, 'adapter waiter did not register'
                    time.sleep(0.001)

            adapter._dispatch_followup.update(zip(('older', 'younger'), followups))
            with patch.object(lock, 'acquire', side_effect=observed_acquire):
                try:
                    threads.append(threading.Thread(target=run, args=('older',)))
                    threads[0].start()
                    wait_queued(1)
                    timed_out.wait(0.25)  # contention exceeds the old 0.1s poll interval
                    with lock._condition:
                        queued = len(lock._waiters)
                    threads.append(threading.Thread(target=run, args=('younger',)))
                    threads[1].start()
                    wait_queued(queued + 1)
                    time.sleep(0.15)
                    lock.release()
                    held = False
                    assert dispatched.wait(2), 'neither waiter dispatched'
                    assert seen[0] == 'older', seen
                finally:
                    if held:
                        lock.release()
                    retry.set()
                    for thread in threads:
                        thread.join(5)
            assert not any(thread.is_alive() for thread in threads)
            assert not errors, errors
            assert seen == ['older', 'younger']
            assert not adapter._waiting and not lock._waiters and not lock.locked()

    def test_adapter_fifo_wait_keeps_older_ticket_ahead_of_review(self):
        self.check_adapter_lock_order('fifo', (False, True))

    def test_adapter_default_wait_keeps_review_ahead_of_later_fix(self):
        self.check_adapter_lock_order(None, (True, True))

    def test_adapter_default_wait_keeps_new_work_fifo(self):
        self.check_adapter_lock_order(None, (False, False))

    def test_default_and_fifo_configuration(self):
        with patch.dict(os.environ):
            os.environ.pop('DISPATCH_PRIORITY', None)
            assert reviews_first()
            os.environ['DISPATCH_PRIORITY'] = 'fifo'
            assert not reviews_first()
            os.environ['DISPATCH_PRIORITY'] = 'typo'
            with self.assertRaises(ValueError):
                app.Broker(state_path=self.path)

    def test_pending_review_precedes_ticket_and_holds_it_until_review_closes(self):
        b = self.configured_broker()
        key = b.ingest(event(9801))
        review = b.ingest(self.review())  # behind the older ticket in durable intake
        self.ready_tick(b)
        assert [k for k, _ in self.calls] == [review]
        assert key in b.coalescer.pending
        b.coalescer.settle_self(review)
        self.pump(b)
        assert [k for k, _ in self.calls] == [review, key]

    def test_ready_burst_reserves_shared_row_slot_until_review_or_fix_metadata_arrives(self):
        for kind in ('review', 'fix'):
            with self.subTest(kind=kind):
                adapter = StubAdapter()
                entered, release = threading.Event(), threading.Event()

                def resolve(key, brief):
                    assert not co.lock._is_owned(), 'metadata I/O ran under the coalescer lock'
                    followup = bool(brief['mr']) or brief['task_id'] == 9841
                    if followup:
                        entered.set()
                        assert release.wait(5)
                    return '/srv/git/sample-app', followup

                adapter.dispatch_context = resolve
                co = C.Coalescer(adapter=adapter, row_of=lambda key: (
                    'shared/reviews' if key.startswith('mr:') else 'shared/tickets'), completed_path='')
                with patch.dict(os.environ, {'ROW_CONCURRENCY': 'shared=1'}), \
                        patch.object(C, 'INITIAL_DEFAULT', 60):
                    ordinary = co.on_event(event(9840))
                    self.wait_metadata(co)  # an older, KNOWN ordinary ticket must not take the slot
                    followup = co.on_event(self.review(40) if kind == 'review' else event(9841))
                    try:
                        assert entered.wait(1), 'lookup did not start during debounce, before any tick'
                        with co.lock:
                            for pending in co.pending.values():
                                pending['spawn_at'] = 0
                        co.tick()  # actual first ready tick; no artificial row-cap warm-up
                        assert not adapter.spawns
                    finally:
                        release.set()
                        self.wait_metadata(co)
                    co.tick()
                    assert [k for k, _ in adapter.spawns] == [followup]
                    assert ordinary in co.pending

    def test_metadata_grace_is_bounded_and_does_not_hold_another_row(self):
        adapter = StubAdapter()
        entered, release = threading.Event(), threading.Event()

        def resolve(key, brief):
            if brief['task_id'] == 9842:
                entered.set()
                assert release.wait(5)
            return None, False

        adapter.dispatch_context = resolve
        co = C.Coalescer(adapter=adapter, row_of=lambda key: (
            'stalled' if key.endswith('9842') else 'other'), completed_path='')
        with patch.dict(os.environ, {'DISPATCH_METADATA_WAIT': '5'}), \
                patch.object(C.time, 'monotonic', return_value=100):
            stalled = co.on_event(event(9842))
            other = co.on_event(event(9843))
        try:
            assert entered.wait(1)
            with patch.object(C.time, 'monotonic', return_value=104.9):
                co.tick()
            # The other lookup might not yet have published on its first tick.
            deadline = time.monotonic() + 5
            while other not in co._dispatch_cache:
                assert time.monotonic() < deadline
                time.sleep(0.001)
            with patch.object(C.time, 'monotonic', return_value=104.9):
                co.tick()
            assert [k for k, _ in adapter.spawns] == [other]
            with patch.object(C.time, 'monotonic', return_value=105):
                co.tick()
            assert [k for k, _ in adapter.spawns] == [other, stalled]
        finally:
            release.set()
            self.wait_metadata(co)

    def test_late_metadata_updates_live_followup_and_holds_same_repo_ticket(self):
        adapter = StubAdapter()
        entered, release = threading.Event(), threading.Event()

        def resolve(key, brief):
            if brief['mr']:
                entered.set()
                assert release.wait(5)
            return '/srv/git/sample-app', bool(brief['mr'])

        adapter.dispatch_context = resolve
        co = C.Coalescer(adapter=adapter, completed_path='')
        with patch.dict(os.environ, {'DISPATCH_METADATA_WAIT': '0'}):
            review = co.on_event(self.review(41))
            try:
                assert entered.wait(1)
                co.tick()  # grace disabled: admit with unknown metadata
                assert co.live[review]['dispatch'] == (None, False)
                co.on_event(self.review(41))  # live forwards must not invalidate the admitted burst
            finally:
                release.set()
                self.wait_metadata(co)
        assert co.live[review]['dispatch'] == ('/srv/git/sample-app', True)
        ordinary = co.on_event(event(9844))
        self.wait_metadata(co)
        co.tick()
        assert [k for k, _ in adapter.spawns] == [review]
        assert ordinary in co.pending  # followup_repos includes the late-published live result
        co.settle_self(review)
        co.tick()
        assert [k for k, _ in adapter.spawns] == [review, ordinary]

    def test_extended_burst_admitted_after_grace_refreshes_live_followup(self):
        for kind in ('review', 'fix'):
            with self.subTest(kind=kind):
                adapter = StubAdapter()
                entered, release = threading.Event(), threading.Event()
                refreshed, finish = threading.Event(), threading.Event()
                sizes = []

                def resolve(key, brief):
                    sizes.append(brief['n_events'])
                    if brief['n_events'] == 1:
                        entered.set()
                        assert release.wait(5)
                        return '/srv/git/stale-app', False
                    refreshed.set()
                    assert finish.wait(5)
                    return '/srv/git/sample-app', True

                adapter.dispatch_context = resolve
                co = C.Coalescer(adapter=adapter, completed_path='')
                seed = self.review(47) if kind == 'review' else event(9847)
                with patch.dict(os.environ, {'DISPATCH_METADATA_WAIT': '5'}):
                    with patch.object(C.time, 'monotonic', return_value=100):
                        followup = co.on_event(seed)
                    try:
                        assert entered.wait(1)
                        co.on_event(seed)  # extend pending while its first lookup is blocked
                        with patch.object(C.time, 'monotonic', return_value=105):
                            co.tick()  # grace expires; admit the two-event burst as unknown
                        assert co.live[followup]['dispatch'] == (None, False)
                        co.on_event(seed)  # a live forward is NOT part of the admitted snapshot
                        release.set()
                        assert refreshed.wait(1), 'admitted burst never got a fresh lookup'
                        assert co.live[followup]['dispatch'] == (None, False)
                        assert sizes == [1, 2]
                    finally:
                        release.set()
                        finish.set()
                        self.wait_metadata(co)
                assert co.live[followup]['dispatch'] == ('/srv/git/sample-app', True)
                adapter.dispatch_context = lambda key, brief: ('/srv/git/sample-app', False)
                ordinary = co.on_event(event(9848))
                self.wait_metadata(co)
                co.tick()
                assert [k for k, _ in adapter.spawns] == [followup]
                assert ordinary in co.pending
                co.settle_self(followup)
                co.tick()
                assert [k for k, _ in adapter.spawns] == [followup, ordinary]

    def test_rearm_starts_metadata_during_debounce(self):
        adapter = StubAdapter()
        adapter.dispatch_context = lambda key, brief: ('/srv/git/sample-app', True)
        co = C.Coalescer(adapter=adapter, completed_path='')
        with patch.object(C, 'INITIAL_DEFAULT', 60):
            co.rearm_pending('vikunja:task:9845', [event(9845)])
        self.wait_metadata(co)
        assert co._dispatch_cache['vikunja:task:9845'][2] == ('/srv/git/sample-app', True)
        assert not adapter.spawns

    def test_late_metadata_cannot_overwrite_replacement_live_worker(self):
        adapter = StubAdapter()
        entered, release = threading.Event(), threading.Event()
        refreshed, finish = threading.Event(), threading.Event()
        calls = []

        def resolve(key, brief):
            calls.append(brief)
            if len(calls) == 1:
                entered.set()
                assert release.wait(5)
                return '/srv/git/old-app', True
            refreshed.set()
            assert finish.wait(5)
            return '/srv/git/new-app', False

        adapter.dispatch_context = resolve
        co = C.Coalescer(adapter=adapter, completed_path='')
        with patch.dict(os.environ, {'DISPATCH_METADATA_WAIT': '0'}):
            key = co.on_event(event(9846))
            try:
                assert entered.wait(1)
                co.tick()
                co.settle_self(key, mark_completed=False)
                co.rearm_pending(key, [event(9846)])
                co.tick()
                assert len(adapter.spawns) == 2
                release.set()
                assert refreshed.wait(1), 'replacement burst never got its own lookup'
                assert co.live[key]['dispatch'] == (None, False)
            finally:
                release.set()
                finish.set()
                self.wait_metadata(co)
        assert len(calls) == 2
        assert co.live[key]['dispatch'] == ('/srv/git/new-app', False)

    def test_fix_round_precedes_new_ticket_on_same_repo(self):
        b = self.configured_broker()
        fetch = b.adapter._ticket_fetcher
        b.adapter._ticket_fetcher = lambda tid: fetch(tid).replace(
            '</p>', '</p><p>continue-mr: eph/fix</p>', 1) if tid == 9803 else fetch(tid)
        b.set_drain(True)
        ordinary = b.ingest(event(9802))
        fix = b.ingest(event(9803))
        b.set_drain(False)
        self.pump(b)
        assert [k for k, _ in self.calls] == [fix]
        b.coalescer.settle_self(fix)
        self.pump(b)
        assert [k for k, _ in self.calls] == [fix, ordinary]

    def test_informed_rereview_has_same_priority_after_restart(self):
        b = self.configured_broker()
        b.set_drain(True)
        ordinary = b.ingest(event(9804))
        review = b.ingest(self.review(prior_findings='<p>BLOCK findings</p>'))
        b = self.restart(b)
        b.adapter._ticket_fetcher = lambda tid: ticket().replace('<p>fix</p>', '<p>repo: /srv/git/sample-app</p>')
        b.set_drain(False)
        self.pump(b)
        assert [k for k, _ in self.calls] == [review]
        assert ordinary in b.coalescer.pending
        assert self.calls[0][1]['prior_findings'] == '<p>BLOCK findings</p>'

    def test_capped_review_does_not_hold_another_repo(self):
        b = self.configured_broker()
        fetch = b.adapter._ticket_fetcher
        b.adapter._ticket_fetcher = lambda tid: fetch(tid).replace(
            '/srv/git/sample-app', '/srv/git/other-app' if tid == 9806 else '/srv/git/sample-app')
        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'mr-review=0'}):
            b.set_drain(True)
            ordinary = b.ingest(event(9805))
            other = b.ingest(event(9806))
            review = b.ingest(self.review())
            b.set_drain(False)
            self.pump(b)
        assert [k for k, _ in self.calls] == [other]
        assert {ordinary, review} <= b.coalescer.pending.keys()

    def test_shared_cap_cannot_starve_other_repo_with_continuing_reviews(self):
        adapter = StubAdapter()
        adapter.dispatch_context = lambda key, brief: (
            brief['mr']['repo_path'] if brief['mr'] else '/srv/git/other-app', bool(brief['mr']))
        co = C.Coalescer(adapter=adapter, row_of=lambda key: 'shared', completed_path='')
        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'shared=1'}):
            first = co.on_event(self.review(1))
            other = co.on_event(event(9807))
            self.ready_tick(co)
            assert [k for k, _ in adapter.spawns] == [first]
            co.settle_self(first)
            second = co.on_event(self.review(2))
            self.ready_tick(co)
            assert [k for k, _ in adapter.spawns] == [first, other]
            assert second in co.pending

    def test_fresh_repo_arrivals_do_not_jump_an_older_waiting_lane(self):
        adapter = StubAdapter()
        adapter.dispatch_context = lambda key, brief: (brief['mr']['repo_path'], True)
        co = C.Coalescer(adapter=adapter, row_of=lambda key: 'shared', completed_path='')
        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'shared=1'}):
            first = co.on_event(self.review(1))
            self.ready_tick(co)
            waiting = co.on_event(self.review(2))
            self.ready_tick(co)  # register the waiting repo while its first worker holds the cap
            co.on_event(self.review(3, repo='/srv/git/fresh-app'))
            co.settle_self(first)
            self.ready_tick(co)
            assert [k for k, _ in adapter.spawns] == [first, waiting]

    def test_deferred_block_round_is_delivered_before_new_ticket(self):
        b = self.configured_broker()
        evt = self.review()
        review = b.ingest(evt)
        self.ready_tick(b)
        ordinary = b.ingest(event(9814))
        mr = C.collapse([evt])['mr']
        assert b._cd_redispatch(mr, '<p>BLOCK findings</p>', 'synthetic-run')
        # Simulate retirement before its out-of-lock close callback can deliver.
        with patch.object(b.coalescer, '_notify_close'):
            b.coalescer.settle_self(review)
        self.ready_tick(b)
        assert [k for k, _ in self.calls] == [review, review]
        assert ordinary in b.coalescer.pending

    def test_debouncing_review_holds_only_its_own_repo(self):
        b = self.configured_broker()
        ordinary = b.ingest(event(9808))
        review = b.ingest(self.review())
        # Admit while keeping every spawn in its debounce window, then make only the ticket ready.
        with patch.object(C, 'INITIAL_DEFAULT', 60):
            b.coalescer.pending[ordinary]['spawn_at'] = time.time() + 60
            self.ready_tick(b)
        b.coalescer.pending[ordinary]['spawn_at'] = 0
        self.ready_tick(b)
        assert self.calls == []
        assert review in b.coalescer.pending

    def test_drain_and_ambiguous_intake_still_stop_all_admission(self):
        b = self.configured_broker()
        b.set_drain(True)
        b.ingest(event(9809))
        b.ingest(self.review())
        self.pump(b)
        assert not self.calls and b.state.status()['queued'] == 2
        b.state.claim(b.state.head()[0])
        b.set_drain(False)
        self.pump(b)
        assert not self.calls and b.state.status()['dispatching'] == 1

    def test_fifo_rollback_preserves_strict_head_blocking(self):
        b = self.configured_broker()
        with patch.dict(os.environ, {'DISPATCH_PRIORITY': 'fifo', 'ROW_CONCURRENCY': 'general=0'}):
            ordinary = b.ingest(event(9810))
            b.ingest(self.review())
            self.pump(b)
            assert not self.calls and ordinary in b.coalescer.pending
            assert b.state.status()['queued'] == 1

    def test_context_is_advisory_cached_and_invalidated_by_new_input(self):
        b = self.configured_broker()
        with patch.object(b.adapter, 'dispatch_context', wraps=b.adapter.dispatch_context) as resolve, \
                patch.dict(os.environ, {'ROW_CONCURRENCY': 'general=0'}):
            key = b.ingest(event(9811))
            self.pump(b)
            assert resolve.call_count == 1
            b.ingest(event(9811))
            self.ready_tick(b)
            assert resolve.call_count == 2
        assert key in b.coalescer.pending

    def test_metadata_uses_issue_map_and_config_bound_repo_without_granting_authority(self):
        b = self.configured_broker()
        with patch.object(b.router, 'route', return_value=SimpleNamespace(
                capability='code', domain='code', publish={}, from_config=False)), \
                patch.object(b.adapter, '_tracker_fetch', return_value=None), \
                patch.object(app.routing, 'resolve_issue_repo', return_value='/srv/git/issue-app'):
            repo, followup = b.adapter.dispatch_context('github:owner/app#9812', {
                'issue': {'source': 'github', 'repo_ident': 'owner/app', 'body': 'continue-mr: eph/fix'}})
            assert repo == '/srv/git/issue-app' and followup
        with patch.object(b.router, 'route', return_value=SimpleNamespace(
                capability='code', domain='custom', publish={}, from_config=True, repo='/srv/git/bound-app')):
            repo, _ = b.adapter.dispatch_context('vikunja:task:9812', {'task_id': 9812})
            assert repo == '/srv/git/bound-app'

    def test_resolver_failure_does_not_drop_or_authorize_work(self):
        b = self.configured_broker()
        with patch.object(b.adapter, 'dispatch_context', side_effect=RuntimeError('unavailable')):
            key = b.ingest(event(9813))
            self.ready_tick(b)
        assert [k for k, _ in self.calls] == [key]

    def test_spawn_failure_after_metadata_deferral_is_durable_recovery(self):
        b = self.configured_broker()
        entered, release = threading.Event(), threading.Event()
        failure = RuntimeError('synthetic start failure')

        def resolve(key, brief):
            entered.set()
            assert release.wait(5)
            return '/srv/git/sample-app', False

        with patch.object(b.adapter, 'dispatch_context', side_effect=resolve), \
                patch.object(b.adapter, 'spawn', side_effect=failure) as spawn:
            key = b.ingest(event(9849))
            try:
                assert entered.wait(1)
                b.tick()                       # metadata grace, NOT a spawn attempt
                spawn.assert_not_called()
                assert key in b.coalescer.pending
                assert not b.coalescer.active_intents and not b.coalescer.recovery
            finally:
                release.set()
                self.wait_metadata(b.coalescer)
            with self.assertRaises(RuntimeError) as raised:
                b.tick()
            assert raised.exception is failure
            spawn.assert_called_once()

        assert key in b.coalescer.recovery
        assert key not in b.coalescer.pending and key not in b.coalescer.live
        assert not b.coalescer.active_intents
        assert dict(b.state.get('scheduler')['active']) == b.coalescer.recovery
        b = self.restart(b)
        assert key in b.coalescer.recovery
        other = b.ingest(event(9850))
        self.pump(b)
        assert not self.calls                  # restart must still block ALL dispatch
        assert b.drain_status()['recovery_required'] == 1
        assert b.drain_status()['queued'] == 1
        assert other not in b.coalescer.live

    def test_stalled_fetch_does_not_block_other_repo_drain_ingest_or_lifecycle(self):
        b = self.configured_broker()
        live = b.ingest(self.review(20, repo='/srv/git/live-app'))
        self.ready_tick(b)
        entered, release = threading.Event(), threading.Event()
        fetch = b.adapter._ticket_fetcher

        def stalled(tid):
            entered.set()
            assert release.wait(5), 'test did not release fetcher'
            return fetch(tid)

        b.adapter._ticket_fetcher = stalled
        pending = b.ingest(event(9820))
        other = b.ingest(self.review(21, repo='/srv/git/other-app'))
        errors = []

        def run(fn):
            try:
                fn()
            except BaseException as exc:
                errors.append(exc)

        def operations():
            b.set_drain(True)
            b.coalescer.mark_idle(live)
            assert b.coalescer.settle_self(live) is not None
            b.ingest(event(9821))
            assert b.drain_status()['queued'] == 1

        def dispatch_other():
            # A just-dequeued review may need a second tick to publish its own hint.
            # It must dispatch WITHOUT waiting for the stalled ticket's lookup/grace.
            deadline = time.monotonic() + 0.75
            while not b.coalescer.has_live_worker(other):
                b.tick()
                assert time.monotonic() < deadline, 'another repo did not dispatch promptly'
                time.sleep(0.001)

        tick = threading.Thread(target=run, args=(dispatch_other,), daemon=True)
        ops = threading.Thread(target=run, args=(operations,), daemon=True)
        with patch.dict(os.environ, {'ROW_CONCURRENCY': 'general=0'}):
            try:
                tick.start()
                assert entered.wait(1), 'metadata fetch did not start'
                tick.join(timeout=1)
                assert not tick.is_alive(), 'stalled fetch blocked dispatch to another repo'
                assert [k for k, _ in self.calls] == [live, other]
                ops.start()
                ops.join(timeout=1)
                assert not ops.is_alive(), 'stalled fetch held the admission/lifecycle lock'
                assert not errors
                assert pending in b.coalescer.pending
                assert b.drain_status()['enabled']
            finally:
                release.set()
                tick.join(timeout=5)
                if ops.ident is not None:
                    ops.join(timeout=5)
                self.wait_metadata(b.coalescer)

    def test_late_metadata_cannot_publish_for_changed_removed_or_rearmed_burst(self):
        for change in ('append', 'remove', 'rearm'):
            with self.subTest(change=change):
                adapter = StubAdapter()
                entered, release = threading.Event(), threading.Event()

                def resolve(key, brief):
                    entered.set()
                    assert release.wait(5)
                    return '/srv/git/old-app', True

                adapter.dispatch_context = resolve
                co = C.Coalescer(adapter=adapter, completed_path='')
                key = co.on_event(event(9822))
                with patch.object(co, '_row_capped', return_value=True):
                    try:
                        co.tick()
                        assert entered.wait(1)
                        with co.lock:
                            if change == 'append':
                                co.on_event(event(9822))
                            else:
                                co.pending.pop(key)
                                if change == 'rearm':
                                    co.rearm_pending(key, [event(9822)])
                    finally:
                        release.set()
                        self.wait_metadata(co)
                    assert key not in co._dispatch_cache
                    if change != 'remove':
                        adapter.dispatch_context = lambda key, brief: ('/srv/git/new-app', False)
                        self.ready_tick(co)
                        assert co._dispatch_cache[key][2] == ('/srv/git/new-app', False)

    def test_saturated_metadata_resolvers_keep_unknown_target_fifo_fallback(self):
        adapter = StubAdapter()
        release = threading.Event()
        calls = []

        def resolve(key, brief):
            calls.append(key)
            assert release.wait(5)
            return '/srv/git/sample-app', True

        adapter.dispatch_context = resolve
        co = C.Coalescer(adapter=adapter, completed_path='')
        try:
            with patch.dict(os.environ, {'DISPATCH_METADATA_WAIT': '0'}):
                keys = [co.on_event(event(9830 + i)) for i in range(5)]
                co.tick()
                assert [k for k, _ in adapter.spawns] == keys
                assert len(co._dispatch_resolving) == 4
                co.tick()
        finally:
            release.set()
            self.wait_metadata(co)
        assert len(calls) == 4
        assert not co._dispatch_cache  # all bursts were dispatched before the late results


class RepoLockTests(unittest.TestCase):
    def test_nonblocking_and_timeout_match_lock_contract(self):
        lock = RepoLock()
        assert lock.acquire(blocking=False)
        assert not lock.acquire(blocking=False)
        assert not lock.acquire(timeout=0.001)
        lock.release()
        assert lock.acquire(timeout=0.001)
        lock.release()

    def check_order(self, policy, expected):
        lock = RepoLock()
        lock.acquire()
        seen, threads = [], []

        def run(name, followup):
            lock.acquire(followup=followup)
            try:
                seen.append(name)
            finally:
                lock.release()

        with patch.dict(os.environ, {'DISPATCH_PRIORITY': policy}):
            try:
                for name, followup in [('new', False), ('review', True), ('fix', True)]:
                    t = threading.Thread(target=run, args=(name, followup))
                    t.start()
                    threads.append(t)
                    deadline = time.monotonic() + 5
                    while True:
                        with lock._condition:
                            queued = len(lock._waiters)
                        if queued == len(threads):
                            break
                        assert time.monotonic() < deadline, 'waiter did not register'
                        time.sleep(0.001)
                # A queued review in one repo has no effect on another repo's lock.
                other = RepoLock()
                assert other.acquire()
                other.release()
            finally:
                lock.release()
                for t in threads:
                    t.join(timeout=5)
            assert not any(t.is_alive() for t in threads)
            assert seen == expected

    def test_review_and_fix_waiters_beat_new_work(self):
        self.check_order('reviews-first', ['review', 'fix', 'new'])

    def test_fifo_lock_rollback(self):
        self.check_order('fifo', ['new', 'review', 'fix'])


# The repository also runs these stdlib fixtures directly from tools/check.sh.
# Expose their existing drain/crash/retirement assertions to the granted pytest runner.
class TestDurableFifo(_DrainFixture, unittest.TestCase):
    def setUp(self):
        isolated_setup(self)


class TestDurableBoundaries(_BoundaryFixture, unittest.TestCase):
    def setUp(self):
        isolated_setup(self)
