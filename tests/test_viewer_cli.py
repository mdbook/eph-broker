import contextlib
import http.client
import io
import json
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, call, patch

from viewer import auth
from viewer.clients.agentdir import APIError, Client, main
from viewer.directory import Directory
from viewer.server import handler
from viewer_support import KEY, ViewerTest


def exercise(client, directory):
    token = auth.mint(KEY, 'demo', clock=directory.clock)
    client.call('enroll', dict(name='demo', token=token))
    assert auth.private_read(client.path)
    client.call('register', dict(name='demo', kind='claude-code', caps=[], ttl=30))
    assert directory.snapshot()[0]['status'] == 'live'
    client.call('beat', {'status': 'idle'})
    assert directory.snapshot()[0]['status'] == 'idle'
    reloaded = Client(client.path, client.server, client.transport, client.clock)
    reloaded.call('beat', {'status': 'busy'})
    assert directory.snapshot()[0]['status'] == 'busy'
    reloaded.call('deregister')
    assert directory.snapshot() == []
    assert json.loads(auth.private_read(client.path)) == {}


class CLI(ViewerTest):
    def test_round_trip_dispatch(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        client = Client(self.tmp_path / 'agent.json', transport=directory.request, clock=lambda: 100000000000)
        exercise(client, directory)

    def test_beat_rebuilds_directory_after_restart(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        client = Client(self.tmp_path / 'agent.json', transport=directory.request, clock=lambda: 100000000000)
        client.call('enroll', dict(name='demo', token=auth.mint(KEY, 'demo', clock=lambda: 100)))
        client.call('register', dict(name='demo', kind='generic', caps=[], ttl=30))
        restarted = Directory(KEY, self.tmp_path, clock=lambda: 100)
        client.transport = restarted.request
        client.call('beat', {'status': 'idle'})
        assert restarted.snapshot()[0]['status'] == 'idle'

    def test_round_trip_http(self):
        directory = Directory(KEY, self.tmp_path)
        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(directory, agent_api=True))
        except PermissionError as exc:
            self.skipTest('loopback sockets unavailable: ' + str(exc))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            exercise(Client(self.tmp_path / 'agent.json', 'http://127.0.0.1:%d' % server.server_port), directory)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_mint_prints_only_token(self):
        key_file = self.tmp_path / 'key'
        auth.private_write(key_file, KEY)
        output = io.StringIO()
        with patch.dict('os.environ', AGENTDIR_ENROLL_KEY_FILE=str(key_file)), contextlib.redirect_stdout(output):
            main(['mint', '--name', 'demo'])
        token = output.getvalue().strip()
        assert auth.verify_token(KEY, token, 'demo', 0)['name'] == 'demo'
        assert KEY.decode() not in token

    def test_pid_lifetime(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        path = self.tmp_path / 'agent.json'
        client = Client(path, transport=directory.request, clock=lambda: 100000000000)
        client.call('enroll', dict(name='demo', token=auth.mint(KEY, 'demo', clock=lambda: 100)))
        client.call('register', dict(name='demo', kind='codex', caps=[], ttl=30))
        with patch('viewer.clients.agentdir.Client', return_value=client), \
                patch('viewer.clients.agentdir.alive', side_effect=[True, False]), \
                patch('viewer.clients.agentdir.time.sleep'):
            main(['--state', str(path), 'beat', '--every', '10', '--while-pid', '1234'])
        assert directory.snapshot() == []

    def test_transient_beat_keeps_identity_until_owner_disappears(self):
        failures = [TimeoutError(), ConnectionResetError(), urllib.error.URLError('temporary'),
                    http.client.IncompleteRead(b''), APIError(503, 'temporary'),
                    APIError(408, 'timeout'), APIError(429, 'busy')]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, status=getattr(failure, 'status', None)):
                directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
                path = self.tmp_path / 'agent.json'
                client = Client(path, transport=directory.request, clock=lambda: 100000000000)
                client.call('enroll', dict(name='demo', token=auth.mint(KEY, 'demo', clock=lambda: 100)))
                client.call('register', dict(name='demo', kind='codex', caps=[], ttl=30))
                kid = client.state['kid']
                actions = []
                lifetime = iter([True, True, True, False])

                def transport(endpoint, body, headers):
                    actions.append(endpoint)
                    if len(actions) == 1:
                        raise failure
                    return directory.request(endpoint, body, headers)

                def owner_alive(*args):
                    # Even after the failed beat and subsequent successful beats,
                    # no deregistration or credential clearing is permitted.
                    assert actions == ['/v1/beat'] * len(actions)
                    assert kid not in directory.revoked
                    assert client.state['kid'] == kid
                    assert json.loads(auth.private_read(path))['kid'] == kid
                    return next(lifetime)

                client.transport = transport
                with patch('viewer.clients.agentdir.Client', return_value=client), \
                        patch('viewer.clients.agentdir.alive', side_effect=owner_alive), \
                        patch('viewer.clients.agentdir.time.sleep') as sleep:
                    main(['--state', str(path), 'beat', '--every', '10', '--while-pid', '1234'])
                assert actions == ['/v1/beat'] * 3 + ['/v1/deregister']
                assert sleep.call_args_list == [call(1), call(10), call(10)]
                assert kid in directory.revoked

    def test_retry_backoff_is_bounded_and_resets_without_deregistering_on_error(self):
        for interval, cap in [(10, 10), (60, 30)]:
            with self.subTest(interval=interval):
                client = Mock(state={'ttl': 180, 'kid': 'saved'})
                client.call.side_effect = [TimeoutError()] * 7 + [
                    {}, TimeoutError(), APIError(401, 'refused')]
                with patch('viewer.clients.agentdir.Client', return_value=client), \
                        patch('viewer.clients.agentdir.alive', return_value=True), \
                        patch('viewer.clients.agentdir.time.sleep') as sleep, \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    main(['--state', 'unused', 'beat', '--every', str(interval)])
                delays = [1, 2, 4, 8, min(16, cap), cap, cap, interval, 1]
                assert sleep.call_args_list == [call(n) for n in delays]
                assert client.call.call_args_list == [call('beat')] * 10
                assert client.state['kid'] == 'saved'

    def test_interrupt_is_intentional_shutdown(self):
        client = Mock(state={'ttl': 30})
        with patch('viewer.clients.agentdir.Client', return_value=client), \
                patch('viewer.clients.agentdir.alive', return_value=True), \
                patch('viewer.clients.agentdir.time.sleep', side_effect=KeyboardInterrupt):
            main(['--state', 'unused', 'beat'])
        assert client.call.call_args_list == [call('beat'), call('deregister')]


if __name__ == '__main__':
    unittest.main()
