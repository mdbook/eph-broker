"""Plugin check for viewer/clients/hermes-plugin/agentdir (stdlib only, no Hermes needed).

Imports the plugin by path and drives it against the real directory code through the plugin's
``post`` seam.
Run from the repository root: python3 viewer/clients/hermes-plugin/test_agentdir_plugin.py
"""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
from urllib.parse import urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', '..')))

from viewer import auth  # noqa: E402
from viewer.directory import Directory, Refused as DirectoryRefused  # noqa: E402

PKG = os.path.join(HERE, 'agentdir')
spec = importlib.util.spec_from_file_location('agentdir_plugin', os.path.join(PKG, '__init__.py'),
                                              submodule_search_locations=[PKG])
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)

KEY = b'k' * 32


class FakeCtx:
    """The slice of Hermes' PluginContext the plugin uses."""

    def __init__(self, config=None):
        self.hooks, self.unloads, self.config = {}, [], config or {}

    def register_hook(self, name, cb):
        self.hooks.setdefault(name, []).append(cb)

    def on_unload(self, cb):
        self.unloads.append(cb)

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def through(directory, log=None):
    """The plugin's post seam, backed by the real directory dispatch."""
    def post(url, body, headers):
        path = urlsplit(url).path
        if log is not None:
            log.append((path, json.loads(body)))
        try:
            return directory.request(path, body, headers)
        except DirectoryRefused as exc:
            if exc.status >= 500:
                raise OSError(str(exc))
            raise P.Refused(exc.status, str(exc))
    return post


def config(home, **extra):
    base = dict(url='http://directory.example', name='books', ttl=3, view_url=None,
                token=None, token_file=None, key_file=os.path.join(home, P.KEY_FILE))
    base.update(extra)
    return base


def until(predicate, seconds=3.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def names(directory):
    return [(e['name'], e['status']) for e in directory.snapshot()]


def test_signature_matches_viewer():
    assert P.signature(KEY, 'POST', '/v1/beat', '1.5', b'{}') == auth.signature(KEY, 'POST', '/v1/beat', '1.5', b'{}')


def test_enroll_once_beats_and_key_reused_across_restart(tmp):
    state, home = os.path.join(tmp, 'state'), os.path.join(tmp, 'home')
    os.makedirs(home)
    directory = Directory(KEY, state)
    log = []
    agent = P.Agent(config(home, token=auth.mint(KEY, 'books')), post=through(directory, log))
    agent.step()
    assert [p for p, _ in log] == ['/v1/enroll', '/v1/register', '/v1/beat'], log
    assert log[1][1] == {'name': 'books', 'kind': 'hermes', 'caps': [], 'ttl': 3}
    assert names(directory) == [('books', 'idle')]
    assert stat.S_IMODE(os.stat(agent.config['key_file']).st_mode) == 0o600
    agent.busy(session_id='s1')
    assert agent.wake.is_set()
    agent.step()
    assert names(directory) == [('books', 'busy')]
    agent.idle()
    agent.step()
    assert names(directory) == [('books', 'idle')]

    # Gateway restart against a restarted directory: no token needed, same kid, re-registers.
    restarted = Directory(KEY, state)
    log2 = []
    again = P.Agent(config(home), post=through(restarted, log2))
    again.step()
    assert [p for p, _ in log2] == ['/v1/register', '/v1/beat'], log2
    assert again.credentials['kid'] == agent.credentials['kid']
    assert names(restarted) == [('books', 'idle')]

    # The directory forgets us (reap or restart while running): a 409 beat re-registers.
    restarted.entries.clear()
    log2.clear()
    again.step()
    assert [p for p, _ in log2] == ['/v1/beat', '/v1/register', '/v1/beat'], log2
    assert names(restarted) == [('books', 'idle')]


def test_shutdown_deregisters_without_revoking(tmp):
    directory = Directory(KEY, os.path.join(tmp, 'state'))
    agent = P.Agent(config(tmp, token=auth.mint(KEY, 'books')), post=through(directory))
    agent.start()
    assert until(lambda: names(directory) == [('books', 'idle')])
    agent.busy()
    assert until(lambda: names(directory) == [('books', 'busy')]), 'status change beats at once'
    agent.stop()
    assert not agent.thread.is_alive()
    assert directory.snapshot() == []
    assert agent.credentials['kid'] not in directory.revoked


def test_directory_down_never_raises_and_recovers(tmp):
    directory = Directory(KEY, os.path.join(tmp, 'state'))
    real = through(directory)
    calls = {'n': 0}

    def flaky(url, body, headers):
        calls['n'] += 1
        if calls['n'] <= 2:
            raise ConnectionRefusedError('down')
        return real(url, body, headers)

    agent = P.Agent(config(tmp, token=auth.mint(KEY, 'books')), post=flaky)
    agent.start()
    assert until(lambda: names(directory) == [('books', 'idle')], 6), 'recovers after backoff'
    assert agent.thread.is_alive()
    agent.stop()


def test_refused_key_stops_with_one_error(tmp):
    directory = Directory(KEY, os.path.join(tmp, 'state'))
    agent = P.Agent(config(tmp, token=auth.mint(KEY, 'books')), post=through(directory))
    agent.step()
    directory.request('/v1/deregister', *signed(agent))  # revoke it behind the agent's back
    agent.registered = False
    agent.start()
    assert until(lambda: not agent.thread.is_alive())
    assert agent._logged == 'refused'
    assert os.path.exists(agent.config['key_file']), 'never re-enrolls or deletes the key'


def signed(agent):
    body = b'{}'
    ts = '%.6f' % time.time()
    key = P.b64decode(agent.credentials['key'])
    return body, {'X-Agentdir-Kid': agent.credentials['kid'], 'X-Agentdir-Ts': ts,
                  'X-Agentdir-Sig': P.signature(key, 'POST', '/v1/deregister', ts, body)}


def test_no_token_does_not_register(tmp):
    log = []
    agent = P.Agent(config(tmp), post=lambda *a: log.append(a))
    agent.start()
    assert until(lambda: not agent.thread.is_alive())
    assert log == [] and agent._logged == 'refused'


def test_view_url_adds_view_cap_and_settings(tmp):
    log = []
    agent = P.Agent(config(tmp, view_url='http://192.0.2.10:8080/'), post=lambda u, b, h: log.append(json.loads(b)) or {})
    agent.credentials = {'name': 'books', 'kid': 'x', 'key': auth.encode(KEY)}
    agent.register()
    assert log[0]['caps'] == ['view', 'steer'] and log[0]['view_url'] == 'http://192.0.2.10:8080/'
    env = {k: v for k, v in os.environ.items() if not k.startswith('AGENTDIR_')}
    env.update(AGENTDIR_NAME='notes', AGENTDIR_TTL='2', HERMES_HOME=tmp)
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        s = P.settings(FakeCtx({'url': 'http://198.51.100.1:8790/', 'name': 'ignored'}))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert s['name'] == 'notes' and s['ttl'] == 3 and s['url'] == 'http://198.51.100.1:8790'
    assert s['key_file'] == os.path.join(tmp, 'agentdir.key')


def test_register_hooks(tmp):
    saved = dict(os.environ)
    os.environ.update(HERMES_HOME=tmp, AGENTDIR_URL='http://127.0.0.1:9')
    os.environ.pop('AGENTDIR_ENROLL_TOKEN', None)
    try:
        ctx = FakeCtx()
        agent = P.register(ctx)
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert set(ctx.hooks) == {'pre_llm_call', 'post_llm_call'} and len(ctx.unloads) == 1
    ctx.hooks['pre_llm_call'][0](session_id='s', user_message='hi')
    assert agent.status == 'busy'
    ctx.unloads[0]()
    assert not agent.thread.is_alive()


if __name__ == '__main__':
    mask = os.umask(0o077)
    try:
        test_signature_matches_viewer()
        for test in (test_enroll_once_beats_and_key_reused_across_restart,
                     test_shutdown_deregisters_without_revoking,
                     test_directory_down_never_raises_and_recovers,
                     test_refused_key_stops_with_one_error,
                     test_no_token_does_not_register,
                     test_view_url_adds_view_cap_and_settings,
                     test_register_hooks):
            with tempfile.TemporaryDirectory(prefix='agentdir-plugin-') as tmp:
                test(tmp)
            print('ok', test.__name__)
    finally:
        os.umask(mask)
    print('agentdir plugin: all checks passed')
