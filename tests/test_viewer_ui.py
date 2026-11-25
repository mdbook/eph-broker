import contextlib
import http.client
import io
import json
import shutil
import socket
import subprocess
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from viewer.directory import Directory
from viewer.server import AgentServer, PAGE, UIAuth, handler
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


@contextlib.contextmanager
def agent_listener(directory, timeout=10):
    started, finished = threading.Event(), threading.Event()

    class Handler(handler(directory, agent_api=True)):
        def setup(self):
            super().setup()
            started.set()

    class Server(AgentServer):
        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                finished.set()  # The slot is released before the test proceeds.

    Handler.timeout = timeout
    with Server(('127.0.0.1', 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        worker.start()
        try:
            yield server.server_address, started, finished
        finally:
            server.shutdown()
            worker.join(timeout=2)


class UI(ViewerTest):
    def test_pull_steer_route_is_action_gated_and_presence_only(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        directory.request('/v1/register', *signed(credentials, '/v1/register',
                          dict(name='demo', kind='claude-code', caps=['steer-pull'], ttl=30)))
        body = json.dumps({'text': 'Check the next tool result'}).encode()
        headers = 'X-Forwarded-User: operator\r\nContent-Type: application/json\r\nContent-Length: %d\r\n' % len(body)
        route = '/api/agents/demo/steer'
        assert request(directory, route, headers, method='POST', body=body)[0] == 403
        headers += 'X-Viewer-Can-Act: 1\r\n'
        status, data = request(directory, route, headers, method='POST', body=body)
        assert status == 200 and json.loads(data)['ok']
        message = directory.entries['demo']['messages'][0]
        assert message['text'] == 'Check the next tool result' and message['doer'] == 'operator'
        assert directory.snapshot()[0]['can_steer_pull']
        # No credential, text, identity or inbox filename leaks in the list API.
        public = json.dumps(directory.snapshot())
        assert message['text'] not in public and 'operator' not in public and message['ns'] not in public
        # The limit is Unicode characters, not bytes or UTF-16 code units.
        body = json.dumps({'text': '𝒜' * 4000}).encode()
        headers = ('X-Forwarded-User: operator\r\nX-Viewer-Can-Act: 1\r\n'
                   'Content-Type: application/json\r\nContent-Length: %d\r\n') % len(body)
        assert request(directory, route, headers, method='POST', body=body)[0] == 200
        directory.entries['demo']['caps'] = []
        assert request(directory, route, headers, method='POST', body=body)[0] == 403
        assert request(directory, '/api/agents/missing/steer', headers, method='POST', body=body)[0] == 404
        # Standalone users cannot manufacture the host's action grant.
        assert not UIAuth(socket_peer=False, basic=b'demo:password').can_act({'X-Viewer-Can-Act': '1'})
        script = request(directory, '/ui.js', 'X-Forwarded-User: operator\r\n')[1]
        assert b'Queued' in script and b'next tool call' in script and b'Delivered' in script
        assert b'input.maxLength = opts.max || 4000' in script and b'comp.update(ctx.canAct, READ_ONLY' in script
        assert b'if (agent.can_steer_pull)' in script and b'b.disabled = !canSend || busy' in script
        assert b"const ok = !!ctx.canAct && (mode === 'tail' ? agent.can_steer_pull : agent.can_steer)" in script

    def test_idle_no_wake_response_has_visible_not_delivered_state(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        directory.request('/v1/register', *signed(credentials, '/v1/register',
                          dict(name='demo', kind='claude-code', caps=['steer-pull'], ttl=30)))
        directory.request('/v1/beat', *signed(credentials, '/v1/beat', {'status': 'idle'}, ts='101'))
        body = json.dumps({'text': 'Wrap up'}).encode()
        headers = ('X-Forwarded-User: operator\r\nX-Viewer-Can-Act: 1\r\n'
                   'Content-Type: application/json\r\nContent-Length: %d\r\n') % len(body)
        status, response = request(directory, '/api/agents/demo/steer', headers, method='POST', body=body)
        assert status == 200
        result = json.loads(response)
        assert result['state'] == 'not_delivered' and result['reason'] == 'agent idle, no wake channel'
        script = request(directory, '/ui.js', 'X-Forwarded-User: operator\r\n')[1]
        assert b'Not delivered: agent idle, no wake channel' in script
        assert b'Sent, awaiting consumption' in script and b'receipt(m.id, rec)' in script
        renderer = request(directory, '/transcript.js', 'X-Forwarded-User: operator\r\n')[1]
        assert b"o.state === 'not_delivered'" in renderer and b'Not delivered: ' in renderer

    def test_legacy_steer_cap_has_no_pull_composer_or_queue_access(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        directory.request('/v1/register', *signed(credentials, '/v1/register',
                          dict(name='demo', kind='hermes', caps=['view', 'steer'], ttl=30)))
        assert directory.snapshot()[0]['can_steer']  # existing sidecar actions are unchanged
        assert not directory.snapshot()[0]['can_steer_pull']
        body = json.dumps({'text': 'Cannot deliver via pull'}).encode()
        headers = ('X-Forwarded-User: operator\r\nX-Viewer-Can-Act: 1\r\n'
                   'Content-Type: application/json\r\nContent-Length: %d\r\n') % len(body)
        status, response = request(directory, '/api/agents/demo/steer', headers, method='POST', body=body)
        assert status == 403 and not json.loads(response)['ok']
        assert directory.entries['demo']['messages'] == []

    def test_agent_timeout_covers_idle_request_line_headers_and_body(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        assert handler(directory, agent_api=True).timeout == 10
        assert handler(directory).timeout is None
        for partial in (b'', b'POST /v1/beat', b'POST /v1/beat HTTP/1.1\r\nHost: localhost\r\n',
                        b'POST /v1/beat HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\n\r\n{'):
            with self.subTest(partial=partial), agent_listener(directory, timeout=.15) as (address, started, finished):
                with socket.create_connection(address, timeout=2) as peer:
                    if partial:
                        peer.sendall(partial)
                    assert started.wait(2)
                    before = time.monotonic()
                    assert peer.recv(1) == b''
                    assert time.monotonic() - before < 2
                    assert finished.wait(2)

    def test_agent_connection_cap_and_signed_beats_after_slots_free(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        credentials = enrolled(directory)
        directory.request('/v1/register', *signed(credentials, '/v1/register',
                          dict(name='demo', kind='generic', caps=[], ttl=30)))
        with patch.dict('os.environ', AGENTDIR_AGENT_MAX_CONNECTIONS='2'), \
                agent_listener(directory) as (address, started, finished), contextlib.ExitStack() as peers:
            idle = []
            for _ in range(2):
                started.clear()
                idle.append(peers.enter_context(socket.create_connection(address, timeout=2)))
                assert started.wait(2)
            # A TCP handshake can succeed, but excess sockets close without starting a worker.
            with socket.create_connection(address, timeout=2) as excess:
                assert excess.recv(1) == b''
            idle[0].close()
            assert finished.wait(2)
            for ts in ('100.1', '100.2'):
                finished.clear()
                body, headers = signed(credentials, '/v1/beat', {'status': 'idle'}, ts)
                headers['Connection'] = 'close'
                conn = http.client.HTTPConnection(*address, timeout=2)
                try:
                    conn.request('POST', '/v1/beat', body, headers)
                    reply = conn.getresponse()
                    assert reply.status == 200 and json.loads(reply.read()) == {'ok': True}
                finally:
                    conn.close()
                assert finished.wait(2)  # Normal request completion also returns the slot.
            assert directory.snapshot()[0]['status'] == 'idle'

    def test_agent_keep_alive_timeout_returns_slot(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        with patch.dict('os.environ', AGENTDIR_AGENT_MAX_CONNECTIONS='1'), \
                agent_listener(directory, timeout=.15) as (address, started, finished):
            conn = http.client.HTTPConnection(*address, timeout=2)
            try:
                conn.request('GET', '/')
                reply = conn.getresponse()
                assert reply.status == 404
                reply.read()
                assert finished.wait(2)
                assert conn.sock.recv(1) == b''
            finally:
                conn.close()
            started.clear()
            finished.clear()
            with socket.create_connection(address, timeout=2) as peer:
                assert started.wait(2)
                assert peer.recv(1) == b''
                assert finished.wait(2)

    def test_agent_slots_return_on_worker_and_thread_start_failures(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        with patch.dict('os.environ', AGENTDIR_AGENT_MAX_CONNECTIONS='1'), \
                AgentServer(('127.0.0.1', 0), handler(directory, agent_api=True)) as server:
            with patch.object(ThreadingHTTPServer, 'process_request', side_effect=RuntimeError('thread start')):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, 'thread start'):
                        server.process_request(Mock(), ('127.0.0.1', 1))
            with patch.object(server, 'finish_request', side_effect=ValueError('handler failed')), \
                    patch.object(server, 'handle_error') as error, patch.object(server, 'shutdown_request') as close:
                assert server._slots.acquire(blocking=False)
                server.process_request_thread(Mock(), ('127.0.0.1', 1))
                error.assert_called_once()
                close.assert_called_once()
                assert server._slots.acquire(blocking=False)
                server._slots.release()

    def test_agent_connection_limit_defaults_and_invalid_settings(self):
        directory = Directory(KEY, self.tmp_path, clock=lambda: 100)
        for value in (None, '', '0', '-1', 'no', '1.5'):
            env = {} if value is None else {'AGENTDIR_AGENT_MAX_CONNECTIONS': value}
            with self.subTest(value=value), patch.dict('os.environ', env, clear=True), \
                    patch('viewer.server.log.error') as error, \
                    AgentServer(('127.0.0.1', 0), handler(directory, agent_api=True)) as server:
                for _ in range(64):
                    assert server._slots.acquire(blocking=False)
                assert not server._slots.acquire(blocking=False)
                assert error.call_count == (0 if value is None else 1)

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

    def test_native_view_uses_broker_styles(self):
        # Ticket 3193: the hermes view is broker DOM in the dash styles, not the sidecar's page in a shadow root.
        import dash
        root = Path(__file__).parent.parent
        script = (root / 'viewer' / 'ui.js').read_text()
        for gone in ('hermes-ui/view.js', 'attachShadow', 'gradient', 'Steer'):
            assert gone not in script, gone
        # Ticket 3322 FR1: a transcript, not a chat. No bubbles, no right-aligned turns, no fill behind user turns.
        for gone in ('.ad-ev', 'align-self:flex-end', 'border-radius:14px'):
            assert gone not in script, gone
        # The mockup's layout: a gutter column, a 2px rule in the sender's colour, replies in its reading serif.
        assert '.ov-row{display:grid;grid-template-columns:var(--gut) minmax(0,1fr)' in script
        assert 'border-right:2px solid var(--rail,var(--rule2));text-align:right' in script
        assert '.ov-reply{font:1.06rem/1.62 var(--serif)' in script and '--gut:5.4rem' in script
        # One control height, raised to the tap target on phones.
        assert '.adir,.aqv{--ctl:36px;' in script and '.adir,.aqv{--ctl:44px}' in script
        assert 'height:var(--ctl);min-height:var(--ctl)' in script and 'width:var(--ctl);height:var(--ctl)' in script
        assert '--ctl:36px;' in dash.PAGE and 'height:var(--ctl);padding:0 16px' in dash.PAGE
        # Steps start folded; Stop, Interrupt and Queue live in the composer while the agent works.
        assert "let open = allOpen || openKeys.has(g.key)" in script and ".ov-row.ov-f{display:none}" in script
        assert "el('button', 'ov-cb ov-stop', 'Stop')" in script and "el('button', 'ov-cb ov-force', 'Interrupt')" in script
        # The mockup's demo strip does not ship.
        assert 'Mockup' not in script and 'Agent state' not in script
        assert (root / 'docs' / 'mockups' / 'agent-view.html').is_file()

    def test_send_split_offers_interrupt_only_while_working(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable')
        check = """
import {sendMode, viewActions} from %s;
const assert = (c, m) => { if (!c) { console.error('FAIL', m); process.exit(1); } };
let mode = sendMode(false, true, 'atlas');
assert(!mode.split && mode.hint === '' && mode.label === 'Send', 'idle: one plain Send');
mode = sendMode(true, true, 'atlas');
assert(mode.split && mode.label === 'Queue' &&
  mode.hint === 'atlas is busy. Queue waits for this turn to end; Stop or Interrupt ends it now.', 'working: Stop, Interrupt, Queue');
assert(!sendMode(true, false, 'atlas').split, 'no interrupt route: no split');
const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push([url.split('/').pop(), JSON.parse(init.body), init.headers.get('X-Broker-Action')]);
  return new Response('{"ok":true}', {status: 200, headers: {'Content-Type': 'application/json'}});
};
const acts = viewActions('/api/a/atlas/', () => 's1');
await acts.say('hello');
assert(JSON.stringify(calls.shift()) === '["say",{"session":"s1","message":"hello"},"1"]', 'Send says');
await acts.interrupt('now');
assert(JSON.stringify(calls) === '[["stop",{"session":"s1"},"1"],["say",{"session":"s1","message":"now"},"1"]]',
  'Interrupt and send stops the turn, then says: ' + JSON.stringify(calls));
globalThis.fetch = async () => new Response('{"error":"agent offline"}', {status: 502, headers: {'Content-Type': 'application/json'}});
let failed = '';
try { await acts.interrupt('x'); } catch (e) { failed = e.message; }
assert(failed === 'agent offline', 'a failed stop does not send');
console.log('ok');
""" % json.dumps((Path(__file__).parent.parent / 'viewer' / 'ui.js').as_uri())
        out = subprocess.run([node, '--input-type=module', '-e', check], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and out.stdout.strip() == 'ok', out.stderr + out.stdout

    def test_view_in_browser(self):
        # Optional developer check, not a dependency: the native hermes view on a stubbed sidecar, measured at
        # 1440 and 390 in both themes. Text box and Send share --ctl; steps start collapsed; nothing overflows.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright unavailable; browser view check not run')
        import re
        import dash
        css = re.search(r'<style>(.*?)</style>', dash.PAGE, re.S).group(1)
        script = (Path(__file__).parent.parent / 'viewer' / 'ui.js').read_text()
        stub = """() => {
          const json = (v) => new Response(JSON.stringify(v), {headers: {'Content-Type': 'application/json'}});
          window.fetch = async (url) => {
            url = String(url);
            if (url.endsWith('/api/agents')) return json({quick_replies: [], agents: [{name: 'atlas', kind: 'hermes',
              status: 'busy', age: 12, can_view: true, can_steer: true}]});
            if (url.includes('/cfg')) return json({session: 'main', main: 'main', sessions: ['main', 'fix-composer']});
            return json({rows: []});
          };
          window.EventSource = class {
            constructor() { setTimeout(() => this.onmessage && this.onmessage({data: JSON.stringify({
              live: {run_id: 'r1', status: 'running', tool: 'terminal'},
              rows: [{id: 1, role: 'user', content: 'Why does the composer lose focus?', ts: 1700000000},
                {id: 2, role: 'assistant', reasoning: 'The form resets the height.', ts: 1700000001,
                 tool_calls: [{name: 'read_file', args: {path: 'viewer/ui.js'}}]},
                {id: 3, role: 'assistant', content: 'Found it. Keeping focus is a two-line change.', ts: 1700000038}]})}), 10); }
            close() {}
          };
        }"""
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest('Chromium unavailable: ' + str(exc))
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 900})
                page.route('**/*', lambda route: route.fulfill(
                    body=script if route.request.url.endswith('/ui.js') else '<!doctype html><style>' + css + '</style>',
                    content_type='text/javascript' if route.request.url.endswith('/ui.js') else 'text/html'))
                page.goto('http://view.example.test/#atlas')
                page.evaluate(stub)
                page.evaluate("""async () => {
                  const {mount} = await import('/ui.js');
                  const box = document.createElement('main'); document.body.append(box);
                  mount(box, {canAct: true});
                  window.buttonContrast = (button) => {
                    const style = getComputedStyle(button);
                    const luminance = (color) => {
                      const rgb = color.match(/[\\d.]+/g).map(Number);
                      if (rgb.length !== 3) throw Error('Expected opaque RGB: ' + color);
                      return rgb.map(v => v / 255).map(v => v <= .04045 ? v / 12.92
                        : ((v + .055) / 1.055) ** 2.4).reduce((sum, v, i) =>
                          sum + v * [.2126, .7152, .0722][i], 0);
                    };
                    for (let node = button; node; node = node.parentElement) {
                      if (getComputedStyle(node).opacity !== '1') throw Error('Dimmed button');
                    }
                    const a = luminance(style.color), b = luminance(style.backgroundColor);
                    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                  };
                }""")
                page.wait_for_selector('.ov-ag .ov-reply')
                for theme, scheme in (('light', 'dark'), ('dark', 'light'), ('auto', 'dark'), ('auto', 'light')):
                    page.emulate_media(color_scheme=scheme)
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    for width, ctl in ((1440, 36), (390, 44)):
                        page.set_viewport_size({'width': width, 'height': 900})
                        m = page.evaluate("""() => {
                          const shown = (s) => [...document.querySelectorAll(s)].filter((e) => e.getClientRects().length);
                          const h = (s) => { const e = shown(s)[0]; return e ? e.getBoundingClientRect().height : null; };
                          // Working: Queue, plus Stop and Interrupt as buttons (desktop) or behind ··· (phone).
                          const row = ['.ov-msg', '.ov-send', '.ov-stop', '.ov-force', '.ov-cmore'].filter((s) => shown(s).length);
                          return {box: h('.ov-msg'), send: h('.ov-send'), row: row.join(),
                            heights: new Set(row.map((s) => h(s))).size,
                            // One row: the text box and every button share their top edge, not just a height.
                            edges: new Set(row.map((s) => shown(s)[0].getBoundingClientRect().top)).size,
                            bar: h('.ov-bar'), label: document.querySelector('.ov-send').textContent,
                            open: document.querySelectorAll('.ov-stepsrow:not(.ov-f)').length,
                            fits: document.documentElement.scrollWidth <= innerWidth,
                            hint: document.querySelector('.ov-cnotes').textContent,
                            send_contrast: window.buttonContrast(document.querySelector('.ov-send'))};
                        }""")
                        print('view %s %s: %s' % (theme, width, m))
                        row = '.ov-msg,.ov-send,.ov-stop,.ov-force' if width > 720 else '.ov-msg,.ov-send,.ov-cmore'
                        assert m['box'] == m['send'] == ctl and m['heights'] == 1 and m['edges'] == 1 and m['row'] == row, m
                        assert m['bar'] >= ctl and m['open'] == 0 and m['fits'] and m['label'] == 'Queue', m
                        assert m['hint'] == 'atlas is busy. Queue waits for this turn to end; Stop or Interrupt ends it now.', m
                        assert m['send_contrast'] >= 4.5, m
                # Phone: Stop and Interrupt sit behind the composer's secondary control.
                page.set_viewport_size({'width': 390, 'height': 900})
                page.click('.ov-cmore')
                assert page.inner_text('.ov-acts .ad-menu').startswith('Stop')
                assert 'Interrupt' in page.inner_text('.ov-acts .ad-menu')
                page.keyboard.press('Escape')
                # A turn's steps open in place from the tally and show the tool wells.
                page.set_viewport_size({'width': 1440, 'height': 900})
                page.click('.ov-tally .ov-tgl')
                assert page.inner_text('.ov-tally .ov-tgl') == 'Hide steps'
                assert page.locator('.ov-stepsrow:not(.ov-f) .ov-term').count() == 1
                assert page.locator('.ov-stepsrow:not(.ov-f) .ov-think').count() == 1
                # The Fleet quick view, in the dash's detail sheet: the same transcript rows and the same composer.
                for width, ctl in ((1440, 36), (390, 44)):
                    page.set_viewport_size({'width': width, 'height': 900})
                    q = page.evaluate("""async () => {
                      const old = document.querySelector('dialog'); if (old) old.remove();
                      const {quickView} = await import('/ui.js');
                      const d = document.createElement('dialog'), side = document.createElement('div');
                      d.className = 'detail-sheet quick'; side.className = 'side'; d.append(side);
                      document.body.append(d); d.showModal();
                      quickView(side, 'atlas', {canAct: true});
                      await new Promise((r) => setTimeout(r, 300));
                      const h = (s) => d.querySelector(s).getBoundingClientRect();
                      return {box: h('.ov-msg').height, send: h('.ov-send').height,
                        edges: new Set(['.ov-msg', '.ov-send'].map((s) => h(s).top)).size,
                        steps: d.querySelectorAll('.ov-tally').length, open: d.querySelectorAll('.ov-stepsrow:not(.ov-f)').length,
                        rows: d.querySelectorAll('.ov-row.ov-in .ov-gut .ov-who, .ov-row.ov-ag .ov-gut .ov-who').length >= 2,
                        fits: d.scrollWidth <= d.clientWidth};
                    }""")
                    print('quick view %s: %s' % (width, q))
                    assert q == {'box': ctl, 'send': ctl, 'edges': 1, 'steps': 1, 'open': 0, 'rows': True, 'fits': True}, q
            finally:
                browser.close()

    def test_agents_tab_fits_the_viewport(self):
        # Ticket 3322: in the real dash page the Agents tab is exactly the viewport. The page never scrolls, only the
        # transcript does; the card header and composer stay on screen; no page title; 44px tap targets on a phone.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright unavailable; layout check not run')
        import dash

        class Coalescer:
            lock = threading.Lock(); live = {}; pending = {}; deduped = 0

        class Broker:
            coalescer = Coalescer(); echo_guard = type('E', (), {'dropped': 0})(); rejected = 0
        with patch.object(dash, 'fleet_snapshot', return_value={'agents': []}):
            state = json.dumps(dash.snapshot(Broker()), default=str)
        page_html = dash.module_page([{'name': 'agents', 'label': 'Agents'}])
        script = (Path(__file__).parent.parent / 'viewer' / 'ui.js').read_text()
        agents = {'quick_replies': [{'label': 'Ask for status', 'text': 'Status?'}], 'agents': [
            {'name': 'atlas', 'kind': 'hermes', 'status': 'busy', 'age': 12, 'can_view': True, 'can_steer': True},
            {'name': 'kestrel', 'kind': 'claude-code', 'status': 'idle', 'age': 300}]}
        answers = {'/': (page_html, 'text/html'), '/state': (state, 'application/json'),
                   '/actions': ('{"allowed": true}', 'application/json'), '/actions/settings': ('{}', 'application/json'),
                   '/viewer-status': ('{"agents": {"state": "up", "version": "1", "restarts": 0, "since": 1}}',
                                      'application/json'),
                   '/viewer/agents/ui.js': (script, 'text/javascript'),
                   '/viewer/agents/fleet-index': ('["atlas", "kestrel"]', 'application/json'),
                   '/viewer/agents/api/agents': (json.dumps(agents), 'application/json'),
                   '/viewer/agents/api/a/atlas/cfg': ('{"session": "main", "main": "main"}', 'application/json'),
                   '/viewer/agents/api/a/atlas/earlier': ('{"rows": []}', 'application/json')}

        def answer(route):
            from urllib.parse import urlsplit
            body, kind = answers.get(urlsplit(route.request.url).path, ('{}', 'application/json'))
            route.fulfill(body=body, content_type=kind)
        # Enough turns that the transcript must scroll inside its card at every size.
        rows = []
        for i in range(30):
            rows += [{'id': 3 * i + 1, 'role': 'user', 'content': 'Question %d about the composer?' % i, 'ts': 1700000000 + i * 60},
                     {'id': 3 * i + 2, 'role': 'assistant', 'reasoning': 'Check grow().', 'ts': 1700000001 + i * 60,
                      'tool_calls': [{'name': 'terminal', 'args': {'command': 'rg -n grow viewer/ui.js'}},
                                     {'name': 'read_file', 'args': {'path': 'viewer/ui.js'}}]},
                     {'id': 3 * i + 3, 'role': 'assistant', 'content': 'Answer %d: keep focus after a send. ' % i * 3,
                      'ts': 1700000038 + i * 60}]
        frame = json.dumps({'live': {'run_id': 'r1', 'status': 'running', 'tool': 'terminal'}, 'rows': rows})
        stream = """window.EventSource = class {
          constructor() { setTimeout(() => this.onmessage && this.onmessage({data: %s}), 10); }
          close() {}
        };""" % json.dumps(frame)
        mockups = Path(__file__).parent.parent / 'docs' / 'mockups'
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest('Chromium unavailable: ' + str(exc))
            try:
                for width, height in ((1440, 900), (1366, 650), (390, 844)):
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    page.add_init_script(stream)
                    page.route('**/*', answer)
                    page.goto('http://broker.example.test/#module/agents/atlas')
                    page.wait_for_selector('.adir-detail .ov-ag .ov-reply')
                    m = page.evaluate("""() => {
                      scrollTo(0, 99999);
                      const r = (s) => document.querySelector(s).getBoundingClientRect();
                      const log = document.querySelector('.adir-detail .log');
                      const view = document.querySelector('main > section:not([hidden])');
                      const small = [...document.querySelectorAll('.adir-detail :is(button,a.ov-back)')]
                        .filter((e) => e.getClientRects().length && e.getBoundingClientRect().height < 44)
                        .map((e) => e.className || e.tagName);
                      // A transcript, not a chat: every turn, user and agent alike, starts at the same left edge, is
                      // left-aligned, and has nothing painted behind it.
                      const turns = [...log.querySelectorAll('.ov-row.ov-in, .ov-row.ov-ag:not(.ov-f)')];
                      const clear = (e) => { const s = getComputedStyle(e);
                        return /^rgba\\(0, 0, 0, 0\\)$|^transparent$/.test(s.backgroundColor) && s.backgroundImage === 'none'; };
                      const body = (t) => t.querySelector(':scope > .ov-bd');
                      const chat = turns.filter((t) => ['right', 'end'].includes(getComputedStyle(body(t)).textAlign) ||
                        ['right', 'end'].includes(getComputedStyle(t).textAlign) || !clear(t) || !clear(body(t)) ||
                        parseFloat(getComputedStyle(body(t)).borderTopLeftRadius) > 0);
                      return {doc: document.documentElement.scrollHeight, win: innerHeight, scrolled: scrollY,
                        wide: document.documentElement.scrollWidth > innerWidth,
                        head: r('.ov-bar').top, bar: r('.ov-bar').height, comp: [r('.ov-compose').top, r('.ov-compose').bottom],
                        log: [log.scrollHeight > log.clientHeight, r('.adir-detail .log').bottom, r('.adir-detail .log').height],
                        h1: view.querySelectorAll('h1').length, pad: getComputedStyle(document.querySelector('main')).paddingTop,
                        open: document.querySelectorAll('.ov-stepsrow:not(.ov-f)').length,
                        toggle: document.querySelector('.ov-tally').textContent, small,
                        users: turns.filter((t) => t.classList.contains('ov-in')).length, chat: chat.length,
                        lefts: new Set(turns.map((t) => Math.round(body(t).getBoundingClientRect().left))).size,
                        // The sender's rule: down the gutter on desktop, down the turn's left edge on a phone.
                        rule: innerWidth > 720 ? getComputedStyle(log.querySelector('.ov-in > .ov-gut')).borderRightWidth
                          : getComputedStyle(log.querySelector('.ov-in')).borderLeftWidth,
                        days: log.querySelectorAll('.ov-day').length, card: getComputedStyle(document.querySelector('.adir-detail')).borderTopLeftRadius,
                        reply: getComputedStyle(log.querySelector('.ov-reply')).fontSize,
                        mock: /Mockup|Agent state/.test(view.textContent)};
                    }""")
                    print('agents tab %sx%s: %s' % (width, height, m))
                    assert m['doc'] == m['win'] and m['scrolled'] == 0 and not m['wide'], m
                    assert m['head'] >= 0 and 0 < m['comp'][0] < m['comp'][1] <= m['win'], m
                    assert m['log'][0] and m['log'][1] <= m['comp'][0] + 1, m
                    assert m['h1'] == 0 and m['open'] == 0 and not m['mock'], m
                    assert m['toggle'] == '2 tool calls, 38s·Show 3 steps', m
                    # No bubbles: no turn is right-aligned or filled, and user and agent turns share one left edge.
                    assert m['users'] and m['chat'] == 0 and m['lefts'] == 1 and m['rule'] == '2px' and m['days'] >= 1, m
                    if width > 720:
                        assert m['pad'] == '16px' and m['bar'] < 60, m
                    else:
                        assert not m['small'] and m['pad'] == '0px' and m['card'] == '0px', m
                        assert 15 <= float(m['reply'][:-2]) <= 16, m
                        # Not cramped: the transcript gets at least 60% of the phone's height.
                        assert m['log'][2] >= .6 * m['win'], m
                    page.screenshot(path=str(mockups / ('agents-tab-%dx%d.png' % (width, height))))
                    page.close()
                # The approved mockup at the same sizes, for a side-by-side review.
                for width, height in ((1440, 900), (390, 844)):
                    page = browser.new_page(viewport={'width': width, 'height': height})
                    page.goto((mockups / 'agent-view.html').as_uri())
                    page.wait_for_selector('#view-slot .ov')
                    page.screenshot(path=str(mockups / ('mockup-%dx%d.png' % (width, height))))
                    page.close()
            finally:
                browser.close()

    def test_ui_routes_and_slow_command_status(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable')
        script = """
import {parseRoute, makeRoute, viewFetch, rowSummary, tailRows, plainText, splitNow, describe, toolLine, steerState, steerHint,
  transcriptLines, rowItems, groupTurns, stepsSummary, workState, sessionList, dur, attachResults, toolResult, dayLabel,
  mmss, secs, liveState} from %s;
const assert = (c, m) => { if (!c) { console.error('FAIL', m); process.exit(1); } };
assert(steerState('not_delivered', 'agent idle, no wake channel') === 'Not delivered: agent idle, no wake channel', 'failure');
assert(steerState('sent') === 'Sent, awaiting consumption' && steerState('delivered') === 'Delivered', 'wake states');
assert(steerHint({status: 'idle', wake: null}) === 'Not delivered: agent idle, no wake channel', 'idle without wake');
assert(steerHint({status: 'idle', wake: {kind: 'zulip-dm'}}).includes('Wakes'), 'idle wake hint');
assert(steerHint({status: 'busy', wake: {kind: 'zulip-dm'}}).includes('next tool call'), 'busy hint');
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
const md = {id: 7, role: 'assistant', content: 'See **[#12](https://x.test/t/12)**, `cmd` and __this__; [a] (b) stays'};
assert(rowSummary(md, 'books')[1] === 'See #12, cmd and this; [a] (b) stays', 'markdown to plain text');
assert(plainText('[x](javascript:alert(1)) 2**3') === 'x) 2**3', 'label kept, nothing rendered');
let tail = tailRows([], [user, call, result], 2);
assert(tail.map((r) => r.id).join() === '1,2', 'results dropped, order kept');
tail = tailRows(tail, [{...call, content: 'edited'}, said, img], 3);
assert(tail.map((r) => r.id).join() === '2,4,5' && tail[0].content === 'edited', 'merge by id, newest n');
let now = splitNow('Fixing  sync, waiting on Sam to test (TV).');
assert(now.doing === 'Fixing sync.' && now.waiting === 'Waiting on Sam to test (TV', JSON.stringify(now));
assert(JSON.stringify(splitNow('Idle. Last: shipped X.')) === '{"doing":"Idle. Last: shipped X.","waiting":""}', 'no wait');
assert(splitNow('blocked by #12').waiting === 'Blocked by #12' && splitNow(null).doing === '', 'blocked, empty');
let d = describe({status: 'busy', kind: 'codex', age: 3}, null);
assert(d.state === 'run' && d.label === 'Working' && d.detail === 'Codex' && d.tokens === null && d.age === 3, 'presence only');
d = describe({status: 'idle', kind: 'claude-code'}, {state: 'wait', label: 'Needs you', doing: 'Shipping, waiting for CI',
  model: 'opus', context_tokens: 70000, context_limit: 200000, detail: 'Temporary'});
assert(d.state === 'wait' && d.label === 'Needs you' && d.waiting === 'Waiting for CI' && d.limit === 200000, 'summary wins');
assert(describe({status: 'grey'}, {state: 'evil', doing: 7}).state === 'off' && describe(null, null).label === 'Unknown', 'untrusted');
assert(JSON.stringify(toolLine('Bash', {command: 'ls', description: 'List files'})) === '["Bash","List files"]', 'desc');
assert(JSON.stringify(toolLine('Edit', {file_path: '/a/b/ui.js', old_string: 'secret'})) === '["Edited","ui.js"]', 'edit');
assert(toolLine('Bash', {command: 'x'.repeat(500)})[1].length === 120 && toolLine('Task', 5)[1] === '', 'short, odd input');
const rec = (type, content, extra = {}) => ({type, timestamp: '2026-01-01T00:00:00Z', message: {content}, ...extra});
let ls = transcriptLines(rec('assistant', [{type: 'text', text: '**Done**'}, {type: 'tool_use', name: 'Read',
  input: {file_path: '/x/y.py'}}]), 'tail');
assert(ls.length === 2 && ls[0].who === 'tail' && ls[0].text === 'Done' && ls[1].code === 'y.py' && ls[0].ts, 'assistant');
assert(transcriptLines(rec('user', 'hi'))[0].kind === 'user', 'user text');
assert(!transcriptLines(rec('user', '<command-name>/clear</command-name>')).length, 'harness tags');
assert(!transcriptLines(rec('user', [{type: 'tool_result', content: 'x'}])).length, 'tool results');
assert(!transcriptLines(rec('user', 'x', {isMeta: true})).length && !transcriptLines({type: 'system'}).length, 'meta');
assert(dur(38000) === '38s' && dur(72000) === '1m 12s' && dur(125000) === '2m 05s' && dur(3720000) === '1h 02m', 'durations');
// Reasoning and tool calls fold into one quiet steps line; replies and user messages stay messages.
const turn = [{id: 1, role: 'user', content: 'why?', ts: 100}, {id: 2, role: 'assistant', reasoning: 'hm', ts: 100,
  tool_calls: [{function: {name: 'terminal', arguments: '{"command":"python3 tests/test_viewer_ui.py"}'}}]},
  {id: 3, role: 'assistant', tool_calls: [{name: 'read_file', args: {path: '/repo/viewer/ui.js'}}], ts: 120},
  {id: 4, role: 'tool', content: 'ok', ts: 121}, {id: 5, role: 'assistant', content: 'Found it.', ts: 138}];
const groups = groupTurns(turn.flatMap((r) => rowItems(r, 'atlas')));
assert(groups.map((g) => g.type).join() === 'msg,steps,msg', JSON.stringify(groups));
assert(groups[0].who === 'you' && groups[2].who === 'atlas' && groups[2].id === 5, 'who');
assert(groups[1].steps.map((s) => s.verb + ':' + (s.code || '')).join() === 'Thought:,Ran:python3 tests/test_viewer_ui.py,Read:ui.js', 'steps');
// The time is the turn's: from the ask (100) to the reply (138), not only between the first and last step.
assert(groups[1].summary === '2 tool calls, 38s', groups[1].summary);
assert(stepsSummary([{verb: 'Edited'}, {verb: 'Edited'}, {verb: 'search'}]) === '3 tool calls', 'counts');
assert(stepsSummary([{verb: 'Ran', ts: 5000}]) === '1 tool call' && stepsSummary([{verb: 'Thought'}]) === 'Thinking', 'one, none');
assert(stepsSummary([{verb: 'Ran', failed: true}, {verb: 'Ran'}], 0, 38000) === '2 tool calls, 38s, 1 failed', 'failed');
// Tool results join their calls: by tool_call_id, else the next result after the call's row. Exit code, output, time.
const calls = [{id: 10, role: 'assistant', ts: 200, tool_calls: [{id: 'a', name: 'terminal', args: {command: 'ls -la'}},
  {id: 'b', name: 'read_file', args: {path: '/r/viewer/ui.js', content: 'secret'}}]},
  {id: 11, role: 'tool', tool_call_id: 'b', content: 'ninety lines', ts: 200.1},
  {id: 12, role: 'tool', tool_call_id: 'a', content: '{"output": "total 0", "exit_code": 2}', ts: 203.6}];
const its = attachResults(calls.flatMap((r) => rowItems(r, 'atlas')), calls.filter((r) => r.role === 'tool'));
assert(its[0].cmd === 'ls -la' && its[0].exit === 2 && its[0].failed && its[0].out === 'total 0' && its[0].ms === 3600, JSON.stringify(its[0]));
assert(its[1].out === 'ninety lines' && !its[1].failed && JSON.stringify(its[1].args) === '[["path","/r/viewer/ui.js"]]', 'read');
const loose = attachResults(rowItems({id: 20, role: 'assistant', ts: 1, tool_calls: [{name: 'x'}]}),
  [{id: 19, role: 'tool', content: 'old'}, {id: 21, role: 'tool', content: '{"error": "boom"}', ts: 2}]);
assert(loose[0].out === 'boom' && loose[0].failed && loose[0].ms === 1000, JSON.stringify(loose[0]));
assert(toolResult({content: 'x\\n'.repeat(30)}).out.split('\\n').length === 21, 'clipped');
assert(rowItems({id: 1, role: 'user', content: 'hi', source: 'zulip'})[0].source === 'zulip', 'source line');
const noon = new Date(2026, 10, 25, 12).getTime();
assert(dayLabel(noon, noon + 3600e3) === 'Today' && dayLabel(noon - 864e5, noon) === 'Yesterday', 'days');
assert(!/Today|Yesterday/.test(dayLabel(noon - 3 * 864e5, noon)), 'older day');
assert(mmss(31000) === '0:31' && mmss(72000) === '1:12' && secs(300) === '0.3s' && secs(27400) === '27.4s', 'clocks');
assert(liveState({what: ''}, 1000, null, 32000) === 'thinking for 0:31', 'thinking');
assert(liveState({what: 'running terminal'}, 1000, null, 2000) === 'running terminal for 0:01', 'running');
assert(liveState(null, null, null) === 'idle' && liveState(null, null, noon).startsWith('idle, last step '), 'idle');
assert(workState({live: {run_id: 'r', status: 'running', tool: 'terminal'}}).what === 'running terminal', 'live run');
assert(workState({waiting: true, phase: 'model', since_ts: 5}).since === 5000 && workState({rows: []}) === null, 'waiting, idle');
assert(workState({live: {run_id: 'r', status: 'running', ended: true}}) === null, 'ended run');
assert(JSON.stringify(sessionList({sessions: ['main', {id: 'b', title: 'Fix', ts: 9}, {}, 7]})) ===
  '[{"id":"main","label":"main","ts":null},{"id":"b","label":"Fix","ts":9}]', 'sessions');
console.log('ok');
""" % json.dumps((Path(__file__).parent.parent / 'viewer' / 'ui.js').as_uri())
        out = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and out.stdout.strip() == 'ok', out.stderr + out.stdout

    def test_quick_replies_setting(self):
        from viewer import server
        assert server.quick_replies('') == server.QUICK_REPLIES and len(server.QUICK_REPLIES) == 3
        assert server.quick_replies('[]') == []
        assert server.quick_replies('["Status?", {"label": " Go ", "text": "Proceed"}]') == [
            {'label': 'Status?', 'text': 'Status?'}, {'label': 'Go', 'text': 'Proceed'}]
        for bad in ('{"a": 1}', 'not json', '[{"label": ""}]', '[%s]' % ','.join(['"x"'] * 9), '["%s"]' % ('x' * 41)):
            with self.assertLogs('eph-broker.viewer', 'ERROR'):
                assert server.quick_replies(bad) == server.QUICK_REPLIES, bad

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
