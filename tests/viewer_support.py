"""Shared stdlib fixtures for the viewer protocol suites."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from viewer import auth

KEY = b'x' * 32


class ViewerTest(unittest.TestCase):
    def setUp(self):
        # Broker socket self-tests change the process-wide mask (including directory
        # search bits). Isolate our credentials and restore the caller's mask.
        mask = os.umask(0o077)
        self.addCleanup(os.umask, mask)
        temporary = tempfile.TemporaryDirectory(prefix='viewer-', dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.tmp_path = Path(temporary.name)


def enrolled(directory, name='demo'):
    token = auth.mint(KEY, name, clock=directory.clock)
    return directory.request('/v1/enroll', json.dumps(dict(name=name, token=token)).encode(), {})


def signed(credentials, path, data, ts='100'):
    body = json.dumps(data).encode()
    headers = {'X-Agentdir-Kid': credentials['kid'], 'X-Agentdir-Ts': ts,
               'X-Agentdir-Sig': auth.signature(auth.decode(credentials['key']), 'POST', path, ts, body)}
    return body, headers
