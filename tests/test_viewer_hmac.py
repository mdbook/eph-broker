import json
import unittest

from viewer import auth
from viewer.directory import Directory, Refused
from viewer_support import KEY, ViewerTest, enrolled, signed


class HMAC(ViewerTest):
    def test_hmac_replay_and_revocation(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        data = dict(name='demo', kind='generic', caps=[], ttl=30)
        body, headers = signed(credentials, '/v1/register', data)
        for modified in [dict(headers, **{'X-Agentdir-Sig': 'bad'}),
                         dict(headers, **{'X-Agentdir-Kid': 'bad'})]:
            with self.assertRaises(Refused):
                directory.request('/v1/register', body, modified)
        for ts in ['39', '161', 'NaN', 'Infinity', '1e99999999999']:
            with self.subTest(ts=ts), self.assertRaises(Refused):
                directory.request('/v1/register', *signed(credentials, '/v1/register', data, ts))
        assert directory.snapshot() == []
        directory.request('/v1/register', body, headers)
        with self.assertRaisesRegex(Refused, 'replayed'):
            directory.request('/v1/register', body, headers)
        with self.assertRaises(Refused):
            directory.request('/v1/beat', *signed(credentials, '/v1/register', {}, '100.1'))
        directory.request('/v1/deregister', *signed(credentials, '/v1/deregister', {}, '100.2'))
        assert directory.snapshot() == []
        restarted = Directory(KEY, self.tmp_path, clock=lambda: 100)
        with self.assertRaisesRegex(Refused, 'revoked'):
            restarted.request('/v1/register', *signed(credentials, '/v1/register', data, '100.3'))
        assert restarted.snapshot() == []
        # Equivalent JSON encodings cannot bypass revocation or replay protection.
        name, jti = json.loads(auth.decode(credentials['kid']))
        alias = dict(credentials, kid=auth.encode(json.dumps([name, jti]).encode()))
        with self.assertRaises(Refused):
            restarted.request('/v1/register', *signed(alias, '/v1/register', data, '100.4'))

    def test_other_kid_cannot_take_or_remove_name(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        first, second = enrolled(directory), enrolled(directory)
        data = dict(name='demo', kind='codex', caps=['tail'], ttl=30)
        directory.request('/v1/register', *signed(first, '/v1/register', data))
        with self.assertRaisesRegex(Refused, 'another kid'):
            directory.request('/v1/register', *signed(second, '/v1/register', data))
        directory.request('/v1/deregister', *signed(second, '/v1/deregister', {}, '100.1'))
        assert len(directory.snapshot()) == 1

    def test_deregister_without_revoke_keeps_the_key(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        data = dict(name='demo', kind='hermes', caps=[], ttl=30)
        directory.request('/v1/register', *signed(credentials, '/v1/register', data))
        with self.assertRaisesRegex(Refused, 'revoke must be'):
            directory.request('/v1/deregister', *signed(credentials, '/v1/deregister', {'revoke': 'no'}, '100.1'))
        with self.assertRaisesRegex(Refused, 'revoke must be'):
            directory.request('/v1/deregister', *signed(credentials, '/v1/deregister', {'revoke': 0}, '100.15'))
        directory.request('/v1/deregister', *signed(credentials, '/v1/deregister', {'revoke': False}, '100.2'))
        assert directory.snapshot() == [] and credentials['kid'] not in directory.revoked
        restarted = Directory(KEY, self.tmp_path, clock=lambda: 100)
        restarted.request('/v1/register', *signed(credentials, '/v1/register', data, '100.3'))
        assert [e['name'] for e in restarted.snapshot()] == ['demo']


if __name__ == '__main__':
    unittest.main()
