import json
import os
import stat
import unittest

from viewer import auth
from viewer.directory import Directory, Refused
from viewer_support import KEY, ViewerTest


class Tokens(ViewerTest):
    def test_tokens_bound_expiring_and_one_use(self):
        token = auth.mint(KEY, 'demo', 10, clock=lambda: 100)
        assert auth.verify_token(KEY, token, 'demo', 109)['name'] == 'demo'
        for key, value, name, now in [(KEY, token, 'other', 100), (KEY, token, 'demo', 110),
                                      (b'y' * 32, token, 'demo', 100), (KEY, 12, 'demo', 100),
                                      (KEY, token + 'x', 'demo', 100)]:
            with self.subTest(value=value, name=name, now=now), self.assertRaises(ValueError):
                auth.verify_token(key, value, name, now)
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        payload = json.dumps({'name': 'demo', 'token': token}).encode()
        result = directory.request('/v1/enroll', payload, {})
        assert auth.decode(result['key']) == auth.agent_key(KEY, result['kid'])
        assert directory.snapshot() == []
        with self.assertRaisesRegex(Refused, 'already used'):
            directory.request('/v1/enroll', payload, {})
        # A restart must not reopen a used token (the kid and key would be re-derived).
        restarted = Directory(KEY, self.tmp_path, clock=lambda: 100)
        with self.assertRaisesRegex(Refused, 'already used'):
            restarted.request('/v1/enroll', payload, {})
        assert stat.S_IMODE((self.tmp_path / 'used-tokens.json').stat().st_mode) == 0o600

    def test_private_file_atomic_replacement(self):
        path = self.tmp_path / 'credential'
        auth.private_write(path, b'first')
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        auth.private_write(path, b'second')
        assert auth.private_read(path) == b'second'
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, '0600'):
            auth.private_read(path)

    def test_state_dir_is_searchable_under_module_host_umask(self):
        # The broker module host starts children with umask 0177.
        mask = os.umask(0o177)
        try:
            state = self.tmp_path / 'new' / 'agents-state'
            directory = Directory(KEY, state)
            directory.request('/v1/enroll', json.dumps(dict(
                name='demo', token=auth.mint(KEY, 'demo', clock=directory.clock))).encode(), {})
        finally:
            os.umask(mask)
        for level in (state, state.parent):
            assert stat.S_IMODE(level.stat().st_mode) == 0o700

    def test_deeply_nested_json_is_refused(self):
        directory = Directory(KEY, self.tmp_path)
        with self.assertRaisesRegex(Refused, 'nested'):
            directory.request('/v1/enroll', b'[' * 16000, {})

    def test_bad_names(self):
        for name in ['../escape', '', 'a b', '<script>', 'a' * 65]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                auth.mint(KEY, name)


if __name__ == '__main__':
    unittest.main()
