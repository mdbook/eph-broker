import unittest

from viewer.directory import Directory, Refused
from viewer_support import KEY, ViewerTest, enrolled, signed


class TTL(ViewerTest):
    def test_reap_boundaries_and_reregister(self):
        now = [100]
        directory = Directory(KEY, self.tmp_path, clock=lambda: now[0])
        credentials = enrolled(directory)
        register = dict(name='demo', kind='hermes', caps=['view'], ttl=9)
        directory.request('/v1/register', *signed(credentials, '/v1/register', register))
        assert directory.snapshot()[0]['status'] == 'live'
        now[0] = 109
        assert directory.snapshot()[0]['status'] == 'grey'
        directory.request('/v1/beat', *signed(credentials, '/v1/beat', {'status': 'busy'}, '109'))
        assert directory.snapshot()[0]['status'] == 'busy'
        now[0] = 136
        assert directory.snapshot() == []
        with self.assertRaisesRegex(Refused, 'register'):
            directory.request('/v1/beat', *signed(credentials, '/v1/beat', {}, '136'))
        directory.request('/v1/register', *signed(credentials, '/v1/register', register, '136.1'))
        assert directory.snapshot()[0]['status'] == 'live'
        assert 'kid' not in directory.snapshot()[0]
        assert 'view_url' not in directory.snapshot()[0]

    def test_bad_ttl(self):
        for ttl in [0, -1, True, 1.5, 86401, '30']:
            with self.subTest(ttl=ttl):
                directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
                credentials = enrolled(directory)
                with self.assertRaises(Refused):
                    directory.request('/v1/register', *signed(credentials, '/v1/register',
                                      dict(name='demo', kind='generic', caps=[], ttl=ttl)))
                assert directory.snapshot() == []


if __name__ == '__main__':
    unittest.main()
