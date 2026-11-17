import io
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from viewer.directory import Directory
from viewer.server import PAGE, UIAuth, handler
from viewer_support import KEY, ViewerTest, enrolled, signed


class Socket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, seconds):
        pass


def request(directory, path, headers='', agent_api=False, method='GET', body=b''):
    raw = ('%s %s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n%s\r\n' %
           (method, path, headers)).encode() + body
    socket = Socket(raw)
    handler(directory, agent_api=agent_api, auth=UIAuth(), standalone=True)(socket, '', object())
    head, data = socket.output.getvalue().split(b'\r\n\r\n', 1)
    return int(head.split()[1]), data


class UI(ViewerTest):
    def test_module_startup_configuration(self):
        from viewer import __main__ as entry, auth
        key_file = self.tmp_path / 'enrollment'
        auth.private_write(key_file, KEY)
        env = {'AGENTDIR_ENROLL_KEY_FILE': str(key_file)}
        argv = ['viewer', '--socket', str(self.tmp_path / 'ui.sock'), '--state-dir', str(self.tmp_path)]
        with patch.dict('os.environ', env, clear=True), patch('sys.argv', argv), \
                patch.object(entry, 'serve') as serve, patch.object(entry.signal, 'signal'):
            entry.main()
            args = serve.call_args.args
            assert args[1] == str(self.tmp_path / 'ui.sock')
            assert args[2] == ('127.0.0.1', 8790)
            assert args[3] is None
            assert args[4].socket_peer

    def test_server_render_assets_and_identity(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        directory.request('/v1/register', *signed(credentials, '/v1/register',
                          dict(name='demo', kind='generic', caps=[], ttl=30)))
        assert request(directory, '/healthz')[0] == 200
        assert request(directory, '/api/agents')[0] == 401
        identity = 'X-Forwarded-User: test\r\n'
        status, body = request(directory, '/api/agents', identity)
        assert status == 200 and json.loads(body)['agents'][0]['name'] == 'demo'
        assert credentials['kid'].encode() not in body
        status, script = request(directory, '/ui.js', identity)
        assert status == 200
        assert b'export function mount' in script
        assert b'textContent' in script and b'innerHTML' not in script
        assert b'5000' in script and b'var(--' in script
        assert request(directory, '/', identity) == (200, PAGE)
        assert b"from '/ui.js'" in PAGE
        assert request(directory, '/v1/enroll', identity, method='POST')[0] == 404
        assert request(directory, '/api/agents', agent_api=True)[0] == 404
        assert request(directory, '/ui.js', agent_api=True)[0] == 404
        # Only the used-token record is persisted; no agent keys or entries.
        assert [p.name for p in self.tmp_path.glob('*.json')] == ['used-tokens.json']
        assert credentials['key'].encode() not in (self.tmp_path / 'used-tokens.json').read_bytes()

    def test_module_host_contract(self):
        from viewer import __main__ as entry, auth
        key_file = self.tmp_path / 'enrollment'
        auth.private_write(key_file, KEY)
        sock = str(self.tmp_path / 'modules' / 'agents.sock')
        env = {'AGENTDIR_ENROLL_KEY_FILE': str(key_file), 'BROKER_MODULE_SOCKET': sock,
               'BROKER_MODULE_NAME': 'agents'}
        with patch.dict('os.environ', env, clear=True), patch('sys.argv', ['viewer']), \
                patch.object(entry, 'serve') as serve, patch.object(entry.signal, 'signal'):
            entry.main()
            args = serve.call_args.args
            assert args[1] == sock
            assert args[0].revocation_path.parent == self.tmp_path / 'modules' / 'agents-state'
            assert args[4].socket_peer and args[4].header == 'X-Viewer-User'
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        directory.request('/v1/register', *signed(enrolled(directory), '/v1/register',
                          dict(name='demo', kind='hermes', caps=[], ttl=30)))
        assert request(directory, '/fleet-index')[0] == 401
        status, body = request(directory, '/fleet-index', 'X-Forwarded-User: test\r\n')
        assert status == 200 and json.loads(body) == ['demo']
        script = request(directory, '/ui.js', 'X-Forwarded-User: test\r\n')[1]
        assert b'export function quickView' in script and b'ctx.signal' in script

    def test_view_ui_files_come_only_from_the_installed_dir(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        identity = 'X-Forwarded-User: test\r\n'
        assert request(directory, '/hermes-ui/view.js')[0] == 401
        with patch.dict('os.environ', {'AGENTDIR_HERMES_UI_DIR': ''}):
            status, body = request(directory, '/hermes-ui/view.js', identity)
        assert status == 404 and b'AGENTDIR_HERMES_UI_DIR' in body
        ui = self.tmp_path / 'web'
        ui.mkdir()
        (ui / 'view.js').write_text('export function mount(){}')
        (ui / 'app.css').write_text('.ob{}')
        (ui / 'logo.svg').write_text('<svg/>')
        (ui / 'secret.txt').write_text('nope')
        with patch.dict('os.environ', {'AGENTDIR_HERMES_UI_DIR': str(ui)}):
            for path, kind in (('view.js', b'text/javascript'), ('app.css', b'text/css'), ('logo.svg', b'image/svg+xml')):
                raw = ('GET /hermes-ui/%s HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n%s\r\n' % (path, identity)).encode()
                sock = Socket(raw)
                handler(directory, auth=UIAuth(), standalone=True)(sock, '', object())
                head = sock.output.getvalue().split(b'\r\n\r\n', 1)[0]
                assert b'Content-Type: ' + kind in head and b'Cache-Control: no-cache' in head, head
            assert request(directory, '/hermes-ui/app.css', identity) == (200, b'.ob{}')
            assert request(directory, '/hermes-ui/secret.txt', identity)[0] == 404
            assert request(directory, '/hermes-ui/../web/secret.txt', identity)[0] == 404
            (ui / 'app.css').unlink()
            status, body = request(directory, '/hermes-ui/app.css', identity)
            assert status == 404 and b'missing app.css' in body

    def test_ui_routes_and_slow_command_status(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable')
        script = """
import {parseRoute, makeRoute, viewFetch, rowSummary, tailRows} from %s;
const assert = (c, m) => { if (!c) { console.error('FAIL', m); process.exit(1); } };
const r = makeRoute('books', 'a/b c', '42');
assert(JSON.stringify(parseRoute(r)) === JSON.stringify({name: 'books', session: 'a/b c', id: '42'}), r);
assert(JSON.stringify(parseRoute('books')) === JSON.stringify({name: 'books', session: null, id: null}), 'bare');
assert(parseRoute('books/s/x').id === null, 'non-numeric id');
let seen;
globalThis.fetch = async (url, init) => { seen = init; return new Response(url === 'slow'
  ? '   {"error":"agent offline","status":502}' : '{"ok":true}', {status: 200, headers: {'Content-Type': 'application/json'}}); };
const slow = await viewFetch('slow', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
assert(slow.status === 502 && (await slow.json()).error === 'agent offline', 'status from body');
assert(seen.headers.get('X-Broker-Action') === '1', 'action header');
const ok = await viewFetch('fine');
assert(ok.status === 200 && seen.headers.get('X-Broker-Action') === null, 'GET untouched');
const user = {id: 1, role: 'user', content: 'hi'}, call = {id: 2, role: 'assistant', content: null,
  tool_calls: [{name: 'terminal'}, {}]}, result = {id: 3, role: 'tool', content: 'up'};
const said = {id: 4, role: 'assistant', content: 'done'}, img = {id: 5, role: 'user', content: '', images: ['x']};
assert(JSON.stringify(rowSummary(call, 'books')) === '["tool","terminal, tool"]', 'tool calls');
assert(rowSummary(result) === null && rowSummary({id: 6, role: 'assistant'}) === null, 'skipped rows');
assert(rowSummary(said, 'books')[0] === 'books' && rowSummary(img)[1] === '[image]', 'agent and image');
let tail = tailRows([], [user, call, result], 2);
assert(tail.map((r) => r.id).join() === '1,2', 'results dropped, order kept');
tail = tailRows(tail, [{...call, content: 'edited'}, said, img], 3);
assert(tail.map((r) => r.id).join() === '2,4,5' && tail[0].content === 'edited', 'merge by id, newest n');
console.log('ok');
""" % json.dumps((Path(__file__).parent.parent / 'viewer' / 'ui.js').as_uri())
        out = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and out.stdout.strip() == 'ok', out.stderr + out.stdout

    def test_http_signed_dispatch_and_body_limit(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        body, headers = signed(credentials, '/v1/register',
                               dict(name='demo', kind='codex', caps=[], ttl=30))
        serialized = ''.join('%s: %s\r\n' % item for item in headers.items())
        serialized += 'Content-Length: %d\r\n' % len(body)
        assert request(directory, '/v1/register', serialized, True, 'POST', body)[0] == 200
        assert directory.snapshot()[0]['name'] == 'demo'
        assert request(directory, '/v1/register', serialized, True, 'POST', body)[0] == 401
        assert request(directory, '/v1/enroll', 'Content-Length: 20000\r\n', True, 'POST')[0] == 413

    def test_standalone_trusts_only_configured_proxy_or_basic(self):
        auth = UIAuth(socket_peer=False, proxies='192.0.2.0/24', basic=b'demo:password')
        headers = {'X-Forwarded-User': 'forged'}
        assert not auth.accepts(headers, '203.0.113.1')
        assert auth.accepts(headers, '192.0.2.1')
        assert not auth.accepts({}, '192.0.2.1')
        assert auth.accepts({'Authorization': 'Basic ZGVtbzpwYXNzd29yZA=='}, '203.0.113.1')
        assert not auth.accepts({'Authorization': 'Basic bad'}, '203.0.113.1')
        assert not auth.accepts({'Authorization': 'Basic \u00e9'}, '203.0.113.1')
        basic = {'Authorization': 'Basic ZGVtbzpwYXNzd29yZA==', 'X-Forwarded-User': 'anyone'}
        assert auth.user(basic, '203.0.113.1') == 'demo'  # an untrusted peer cannot pick its label
        assert auth.user(headers, '192.0.2.1') == 'forged'


if __name__ == '__main__':
    unittest.main()
