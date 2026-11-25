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

    def test_view_theme_contract_and_shadow_styles(self):
        script = (Path(__file__).parent.parent / 'viewer' / 'ui.js').read_text()
        # The host maps live broker variables, rather than freezing the current palette in JS.
        tokens = {'bg': 'bg', 'surface': 's1', 'hover': 's2', 'active': 's3', 'border': 'line',
                  'text': 't1', 'muted': 't2', 'faint': 't3', 'accent': 'brand', 'on-accent': 'onbrand',
                  'success': 'run', 'warning': 'wait', 'danger': 'block', 'shadow': 'shadow'}
        for name, broker in tokens.items():
            assert '--agent-view-%s:var(--%s,' % (name, broker) in script
        assert '.adir-detail{${VIEW_TOKENS}}' in script
        assert script.index('themeView(host);') > script.index('view = mod.mount(host,')
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable (theme token mappings checked)')
        check = """
import {themeView} from %s;
const assert = (c, m) => { if (!c) throw Error(m); };
let styles = [];
globalThis.document = {createElement: (tag) => ({tag, setAttribute(k, v) { this[k] = v; }})};
const host = {shadowRoot: {
  querySelector: () => styles[0], append: (style) => styles.push(style)
}};
themeView(host);
assert(styles.length === 1 && styles[0].tag === 'style', 'inject into the renderer shadow tree');
assert(styles[0]['data-agent-view-theme'] === '', 'marked compatibility stylesheet');
assert(styles[0].textContent.includes('var(--agent-view-surface)'), 'consume the public seam');
assert(styles[0].textContent.includes(':focus-visible'), 'native focus styling');
assert(styles[0].textContent.includes('max-width:720px'), 'phone controls');
themeView(host);
assert(styles.length === 1, 'idempotent');
themeView({}); // Non-shadow renderers can read the inherited variables themselves.
console.log('ok');
""" % json.dumps((Path(__file__).parent.parent / 'viewer' / 'ui.js').as_uri())
        out = subprocess.run([node, '--input-type=module', '-e', check], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and out.stdout.strip() == 'ok', out.stderr + out.stdout

    def test_view_theme_in_browser(self):
        # Optional developer check, not a dependency. This fixture models the installed renderer's open
        # shadow-root contract; it does not pretend to be a screenshot of the external sidecar renderer.
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest('Playwright unavailable; browser theme check not run')
        import re
        import dash
        css = re.search(r'<style>(.*?)</style>', dash.PAGE, re.S).group(1)
        script = (Path(__file__).parent.parent / 'viewer' / 'ui.js').read_text()
        # Capture the actual module stylesheet so the host token mapping is exercised too.
        script += '\nexport const testCSS = CSS;'
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest('Chromium unavailable: ' + str(exc))
            try:
                page = browser.new_page(viewport={'width': 1400, 'height': 900})
                page.route('**/*', lambda route: route.fulfill(
                    body=script if route.request.url.endswith('/ui.js') else '<!doctype html><style>' + css + '</style>',
                    content_type='text/javascript' if route.request.url.endswith('/ui.js') else 'text/html'))
                page.goto('http://theme.example.test/')
                page.evaluate("""async () => {
                  const {themeView, testCSS} = await import('/ui.js');
                  const style = document.createElement('style'); style.textContent = testCSS;
                  document.head.append(style);
                  const host = document.createElement('div'); host.className = 'adir-view';
                  const detail = document.createElement('div'); detail.className = 'adir-detail';
                  detail.append(host); document.body.append(detail);
                  const root = host.attachShadow({mode: 'open'});
                  const legacy = document.createElement('style');
                  legacy.textContent = ':host{--bg:#123;--line:pink}.ob{--panel:#123;--ink:#eee;background:var(--panel);color:var(--ink);font:16px serif}';
                  const panel = document.createElement('main'); panel.className = 'ob';
                  for (const variant of ['', 'pri', 'primary', 'submit', 'ghost', 'on',
                    'bad', 'error', 'run', 'busy', 'ok', 'wait', 'warn', 'pend']) {
                    const button = document.createElement('button');
                    button.textContent = variant || 'Default'; button.dataset.variant = variant;
                    if (variant === 'submit') button.type = 'submit'; else button.className = variant;
                    panel.append(button);
                  }
                  root.append(legacy, panel); themeView(host);
                  window.buttonContrast = (button) => {
                    const style = getComputedStyle(button);
                    const luminance = (color) => {
                      const rgb = color.match(/[\\d.]+/g).map(Number);
                      if (rgb.length !== 3) throw Error('Expected opaque RGB: ' + color);
                      return rgb.map(v => v / 255).map(v => v <= .04045 ? v / 12.92
                        : ((v + .055) / 1.055) ** 2.4).reduce((sum, v, i) =>
                          sum + v * [.2126, .7152, .0722][i], 0);
                    };
                    // An opaque computed pair is not sufficient if an ancestor dims the control.
                    for (let node = button; node; node = node.parentElement || node.getRootNode().host) {
                      if (getComputedStyle(node).opacity !== '1') throw Error('Dimmed button');
                    }
                    const a = luminance(style.color), b = luminance(style.backgroundColor);
                    return (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
                  };
                }""")
                for theme, scheme, surface, ink in (
                    ('light', 'dark', 'rgb(255, 255, 255)', 'rgb(20, 25, 32)'),
                    ('dark', 'light', 'rgb(22, 26, 32)', 'rgb(232, 236, 241)'),
                    ('auto', 'dark', 'rgb(22, 26, 32)', 'rgb(232, 236, 241)'),
                    ('auto', 'light', 'rgb(255, 255, 255)', 'rgb(20, 25, 32)'),
                ):
                    page.emulate_media(color_scheme=scheme)
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    for width in (1400, 390):
                        page.set_viewport_size({'width': width, 'height': 900})
                        actual = page.evaluate("""() => {
                          const host = document.querySelector('.adir-view'), root = host.shadowRoot;
                          const panel = getComputedStyle(root.querySelector('main'));
                          const button = getComputedStyle(root.querySelector('button'));
                          return [panel.backgroundColor, panel.color, panel.fontSize,
                            button.borderRadius, button.minHeight, host.scrollWidth <= host.clientWidth];
                        }""")
                        assert actual == [surface, ink, '14px', '8px', '48px' if width == 390 else '36px', True]
                        buttons = page.locator('.adir-view button')
                        for index in range(buttons.count()):
                            button = buttons.nth(index)
                            variant = button.get_attribute('data-variant')
                            for state in ('default', 'hover', 'focus-visible', 'active', 'disabled', 'disabled-hover'):
                                page.mouse.move(width - 1, 899)
                                button.evaluate('(button) => { button.disabled = false; button.blur(); }')
                                if state in ('disabled', 'disabled-hover'):
                                    button.evaluate('(button) => button.disabled = true')
                                if state in ('hover', 'active', 'disabled-hover'):
                                    button.hover()
                                    assert button.evaluate('(button) => button.matches("button:hover")')
                                if state == 'focus-visible':
                                    # Real keyboard modality, not just programmatic focus or a CSS class.
                                    page.keyboard.press('Tab')
                                    button.focus()
                                    assert button.evaluate('(button) => button.matches(":focus-visible")')
                                if state == 'active':
                                    page.mouse.down()
                                try:
                                    if state == 'active':
                                        assert button.evaluate('(button) => button.matches("button:active")')
                                    contrast = button.evaluate('(button) => window.buttonContrast(button)')
                                    assert contrast >= 4.5, (theme, width, variant, state, contrast)
                                finally:
                                    if state == 'active':
                                        page.mouse.up()
                            button.evaluate('(button) => button.disabled = false')
            finally:
                browser.close()

    def test_ui_routes_and_slow_command_status(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable')
        script = """
import {parseRoute, makeRoute, viewFetch, rowSummary, tailRows, plainText, splitNow, describe, toolLine, steerState, steerHint,
  transcriptLines} from %s;
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
