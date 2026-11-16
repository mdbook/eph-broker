import io
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from viewer.allowlist import Allowlist
from viewer.directory import Directory, Refused
from viewer_support import KEY, ViewerTest, enrolled, signed


def resolver(ip):
    return lambda *args, **kwargs: [(None, None, None, None, (ip, 8080))]


class Egress(ViewerTest):
    def test_blocked_dns_does_not_block_heartbeat_or_snapshot(self):
        entered, release = threading.Event(), threading.Event()

        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return resolver('192.0.2.1')()

        policy = Allowlist('192.0.2.0/24:8080', blocked)
        directory = Directory(KEY, self.tmp_path, policy, clock=lambda: 100)
        slow, other = enrolled(directory, 'slow'), enrolled(directory, 'other')
        data = dict(name='slow', kind='generic', caps=[], ttl=30,
                    view_url='http://slow.test:8080/')
        directory.request('/v1/register', *signed(other, '/v1/register', dict(
            name='other', kind='generic', caps=[], ttl=30)))

        def heartbeat_and_snapshot():
            directory.request('/v1/beat', *signed(other, '/v1/beat', {'status': 'busy'}, '100.1'))
            return directory.snapshot()

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(directory.request, '/v1/register', *signed(slow, '/v1/register', data))
            try:
                assert entered.wait(1)
                snapshot = pool.submit(heartbeat_and_snapshot).result(timeout=1)
                assert [(e['name'], e['status']) for e in snapshot] == [('other', 'busy')]
                assert not pending.done()
            finally:
                release.set()
            assert pending.result(timeout=1) == {'ok': True}

    def test_registration_rechecks_state_after_dns(self):
        for change, reason in [('revoke', 'revoked'), ('owner', 'another kid'),
                               ('register', 'superseded'), ('beat', 'superseded')]:
            with self.subTest(change=change):
                entered, release = threading.Event(), threading.Event()

                def blocked(*args, **kwargs):
                    entered.set()
                    release.wait(5)
                    return resolver('192.0.2.1')()

                policy = Allowlist('192.0.2.0/24:8080', blocked)
                directory = Directory(KEY, self.tmp_path / change, policy, clock=lambda: 100)
                credentials = enrolled(directory)
                data = dict(name='demo', kind='generic', caps=[], ttl=30)
                if change == 'beat':
                    directory.request('/v1/register', *signed(credentials, '/v1/register', data, '99.9'))
                delayed = dict(data, view_url='http://slow.test:8080/')

                def update():
                    if change == 'revoke':
                        return directory.request('/v1/deregister', *signed(
                            credentials, '/v1/deregister', {}, '100.1'))
                    if change == 'beat':
                        return directory.request('/v1/beat', *signed(
                            credentials, '/v1/beat', {'status': 'busy'}, '100.1'))
                    owner = enrolled(directory) if change == 'owner' else credentials
                    return directory.request('/v1/register', *signed(
                        owner, '/v1/register', dict(data, kind='codex'), '100.1'))

                with ThreadPoolExecutor(max_workers=2) as pool:
                    pending = pool.submit(directory.request, '/v1/register', *signed(
                        credentials, '/v1/register', delayed))
                    try:
                        assert entered.wait(1)
                        assert pool.submit(update).result(timeout=1) == {'ok': True}
                    finally:
                        release.set()
                    with self.assertRaisesRegex(Refused, reason):
                        pending.result(timeout=1)
                snapshot = directory.snapshot()
                if change == 'revoke':
                    assert snapshot == []
                elif change == 'beat':
                    assert snapshot[0]['status'] == 'busy'
                else:
                    assert snapshot[0]['kind'] == 'codex'

    def test_dns_deadline_and_outstanding_lookup_limit(self):
        release = threading.Event()

        def blocked(*args, **kwargs):
            release.wait(5)
            return resolver('192.0.2.1')()

        policy = Allowlist('192.0.2.0/24:8080', blocked, resolve_timeout=0.05)
        directory = Directory(KEY, self.tmp_path, policy, clock=lambda: 100)
        credentials = enrolled(directory)
        data = dict(name='demo', kind='generic', caps=[], ttl=30,
                    view_url='http://slow.test:8080/')
        try:
            for i in range(4):
                with self.assertRaisesRegex(Refused, 'DNS resolution timed out'):
                    directory.request('/v1/register', *signed(credentials, '/v1/register', data, str(100 + i)))
            with self.assertRaisesRegex(Refused, 'DNS resolver is busy'):
                directory.request('/v1/register', *signed(credentials, '/v1/register', data, '104'))
            assert directory.snapshot() == []
        finally:
            release.set()

    def test_allowlist_registration_and_dns_rebind(self):
        policy = Allowlist('192.0.2.0/24:8080-8081', resolver('192.0.2.1'))
        directory = Directory(KEY, self.tmp_path, policy, clock=lambda: 100)
        credentials = enrolled(directory)
        data = dict(name='demo', kind='generic', caps=['view'], ttl=30,
                    view_url='http://agent.test:8080/view')
        directory.request('/v1/register', *signed(credentials, '/v1/register', data))
        assert not directory.snapshot()[0]['can_view']
        policy.resolver = resolver('203.0.113.1')
        with self.assertRaisesRegex(ValueError, 'outside'):
            policy.fetch(data['view_url'])
        with self.assertRaisesRegex(Refused, 'outside'):
            directory.request('/v1/register', *signed(credentials, '/v1/register', data, '100.1'))

    def test_refused_urls(self):
        for url in ['ftp://192.0.2.1:8080/', 'http://u:p@192.0.2.1:8080/',
                    'http://192.0.2.1:8080/#fragment', 'http://192.0.2.1:80/',
                    'http://192.0.2.1:0/', 'http://192.0.2.1:8080/\nx']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Allowlist('192.0.2.0/24:8080', resolver('192.0.2.1')).resolve(url)

    def test_empty_policy_and_ipv6(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            Allowlist().resolve('http://localhost/')
        assert Allowlist('2001:db8::/32:443', resolver('2001:db8::1')).resolve('https://agent.test/')[1] == 443
        policy = Allowlist('192.0.2.0/24:8080', lambda *a, **kw:
                           resolver('192.0.2.1')() + resolver('203.0.113.1')())
        with self.assertRaisesRegex(ValueError, 'outside'):
            policy.resolve('http://mixed.test:8080/')

    def test_fetch_pins_checked_address_and_never_redirects(self):
        connected = []

        class Socket:
            def makefile(self, *args):
                return io.BytesIO(b'HTTP/1.1 302 Found\r\nLocation: http://other.test/\r\nContent-Length: 0\r\n\r\n')

            def sendall(self, data):
                assert b'Host: agent.test:8080' in data

            def close(self):
                pass

        def connect(address, **kwargs):
            connected.append(address)
            return Socket()

        with patch('viewer.allowlist.socket.create_connection', connect):
            with self.assertRaisesRegex(ValueError, 'redirects'):
                Allowlist('192.0.2.0/24:8080', resolver('192.0.2.1')).fetch('http://agent.test:8080/')
        assert connected == [('192.0.2.1', 8080)]


if __name__ == '__main__':
    unittest.main()
