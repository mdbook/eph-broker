"""Hermes view proxy against a stdlib fixture that enforces the sidecar's Host, auth and Origin checks."""
import io
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from viewer import auth
from viewer.adapters import hermes
from viewer.allowlist import Allowlist
from viewer.directory import Directory
from viewer.server import UIAuth, handler
from viewer_support import KEY, ViewerTest, enrolled, signed

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16
SECRET = b'fixture-secret'


class Sidecar(BaseHTTPRequestHandler):
    """Mimics the sidecar: Host -> 421, forward auth + proxy secret -> 401, POST JSON + Origin == Host."""
    protocol_version = 'HTTP/1.1'
    seen = []
    hostnames = {'agent.test'}

    def log_message(self, *args):
        pass

    def reply(self, status, data, kind='application/json', extra=()):
        data = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        for key, value in extra:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def gate(self):
        Sidecar.seen.append((self.command, self.path, dict(self.headers)))
        if self.headers.get('Host', '').rpartition(':')[0] not in self.hostnames:
            self.reply(421, b'unknown host name\n', 'text/plain')
            return False
        if not self.headers.get('Remote-User') or self.headers.get('X-View-Proxy-Secret', '').encode() != SECRET:
            self.reply(401, b'login required\n', 'text/plain')
            return False
        return True

    def do_GET(self):
        if not self.gate():
            return
        path = self.path.split('?')[0]
        if path == '/rows':
            return self.reply(200, {'rows': [{'id': 1, 'role': 'user', 'content': 'hi'}], 'waiting': False})
        if path == '/img':
            if self.headers.get('If-None-Match') == '"v1"':
                return self.reply(304, b'', 'image/png', [('ETag', '"v1"')])
            if 'n=9' in self.path:
                return self.reply(200, b'<svg onload=alert(1)>', 'image/svg+xml')
            return self.reply(200, PNG, 'image/png', [('ETag', '"v1"'), ('Cache-Control', 'private, no-cache')])
        if path == '/ctx':
            return self.reply(200, b'[' * 200000 + b']' * 200000)  # Deep enough to blow the JSON parser's stack.
        if path == '/tools':
            return self.reply(200, {'tools': []}, extra=[('ETag', '"a"\r\n Set-Cookie: x=1')])
        if path == '/brand':
            return self.reply(200, b'<svg/>', 'image/svg+xml')
        if path == '/skill':
            return self.reply(200, b'<script>alert(1)</script>', 'text/html')
        if path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'retry: 2000\n\nid: 5\ndata: {"rows":[]}\n\n')
            self.wfile.flush()
            time.sleep(0.5)
            self.wfile.write(b'id: 6\ndata: {"rows":[{"id":6}]}\n\n')
            self.wfile.flush()
            self.close_connection = True  # End the stream so the test does not wait for the idle timeout.
            return None
        self.reply(404, {'error': 'not found'})

    def do_POST(self):
        if not self.gate():
            return
        origin = self.headers.get('Origin')
        if origin is not None and origin.split('://', 1)[-1] != self.headers.get('Host'):
            return self.reply(403, {'error': 'cross-origin blocked'})
        if self.headers.get('Content-Type') != 'application/json':
            return self.reply(415, {'error': 'send application/json'})
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/cmd' and body.get('name') == 'slow':
            time.sleep(0.6)
            return self.reply(502, {'error': 'agent offline'})
        if self.path == '/say':
            return self.reply(202, {'ok': True, 'echo': body})
        self.reply(200, {'messages': ['done'], 'echo': body})


class Socket:
    def __init__(self, request):
        self.input, self.output = io.BytesIO(request), io.BytesIO()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, seconds):
        pass


class HermesView(ViewerTest):
    def setUp(self):
        super().setUp()
        Sidecar.seen = []
        self.sidecar = ThreadingHTTPServer(('127.0.0.1', 0), Sidecar)
        self.addCleanup(self.sidecar.server_close)
        self.addCleanup(self.sidecar.shutdown)
        threading.Thread(target=self.sidecar.serve_forever, daemon=True).start()
        self.port = self.sidecar.server_address[1]
        self.address = '127.0.0.1'
        secret = self.tmp_path / 'proxy-secret'
        auth.private_write(secret, SECRET + b'\n')
        env = patch.dict(os.environ, {'AGENTDIR_HERMES_PROXY_SECRET_FILE': str(secret)})
        env.start()
        self.addCleanup(env.stop)
        keepalive = patch.object(hermes, 'KEEPALIVE_S', 0.2)
        keepalive.start()
        self.addCleanup(keepalive.stop)
        policy = Allowlist('127.0.0.0/8', lambda *a, **kw: [(None, None, None, None, (self.address, 0))])
        self.directory = Directory(KEY, self.tmp_path / 'state', policy)
        self.register()

    def register(self, caps=('view', 'steer'), name='books'):
        credentials = enrolled(self.directory, name)
        data = dict(name=name, kind='hermes', caps=list(caps), ttl=30,
                    view_url='http://agent.test:%d/' % self.port)
        self.directory.request('/v1/register', *signed(credentials, '/v1/register', data, str(time.time())))

    def call(self, path, method='GET', body=None, act=True, extra=''):
        body = b'' if body is None else json.dumps(body).encode()
        headers = 'X-Viewer-User: demo\r\nX-Viewer-Can-Act: %s\r\n%s' % ('1' if act else '0', extra)
        if method == 'POST':
            headers += 'Content-Type: application/json\r\nContent-Length: %d\r\n' % len(body)
        raw = ('%s %s HTTP/1.1\r\nHost: localhost\r\n%s\r\n' % (method, path, headers)).encode() + body
        sock = Socket(raw)
        handler(self.directory, auth=UIAuth(header='X-Viewer-User'))(sock, '', object())
        head, data = sock.output.getvalue().split(b'\r\n\r\n', 1)
        lines = head.decode('latin-1').split('\r\n')
        fields = dict(line.split(': ', 1) for line in lines[1:])
        return int(lines[0].split()[1]), fields, data

    def test_read_route_rewrites_host_and_injects_auth_and_secret(self):
        status, fields, data = self.call('/api/a/books/rows?after=-1&session=s1', extra='Cookie: sso=1\r\n')
        assert status == 200 and json.loads(data)['rows'][0]['content'] == 'hi', data
        method, path, headers = Sidecar.seen[-1]
        assert path == '/rows?after=-1&session=s1'
        assert headers['Host'] == 'agent.test:%d' % self.port and headers['Remote-User'] == 'demo'
        assert headers['X-View-Proxy-Secret'] == SECRET.decode()
        assert 'Cookie' not in headers and 'X-Viewer-User' not in headers and 'Origin' not in headers

    def test_missing_secret_is_refused_by_the_sidecar(self):
        with patch.dict(os.environ, {'AGENTDIR_HERMES_PROXY_SECRET_FILE': ''}):
            status, _, data = self.call('/api/a/books/rows')
        assert status == 502 and b'HTTP 401' in data, data
        (self.tmp_path / 'proxy-secret').chmod(0o644)  # Not a private file: refused before connecting.
        count = len(Sidecar.seen)
        status, _, data = self.call('/api/a/books/rows')
        assert status == 502 and b'misconfigured' in data and len(Sidecar.seen) == count

    def test_actions_need_can_act_and_the_steer_cap(self):
        status, _, data = self.call('/api/a/books/say', 'POST', {'session': 's1', 'message': 'hello'})
        assert status == 202 and json.loads(data)['echo']['message'] == 'hello', data
        method, path, headers = Sidecar.seen[-1]
        assert headers['Origin'] == 'http://agent.test:%d' % self.port
        count = len(Sidecar.seen)
        status, _, data = self.call('/api/a/books/stop', 'POST', {'session': 's1'}, act=False)
        assert status == 403 and b'read-only' in data
        status, _, _ = self.call('/api/a/books/rows', act=False)
        assert status == 200
        self.register(caps=('view',), name='quiet')
        status, _, data = self.call('/api/a/quiet/cmd', 'POST', {'session': 's1', 'name': 'status'})
        assert status == 403 and b'does not accept' in data
        status, _, _ = self.call('/api/a/books/rows', 'POST', {})
        assert status == 404
        assert len(Sidecar.seen) == count + 1  # only the read reached the sidecar

    def test_a_refused_post_closes_instead_of_parsing_its_body_as_a_request(self):
        body = b'{"session":"s1"}'
        post = (b'POST /api/a/%s/stop HTTP/1.1\r\nHost: localhost\r\nX-Viewer-User: demo\r\nX-Viewer-Can-Act: 0\r\n'
                b'Content-Type: application/json\r\nContent-Length: %d\r\n\r\n%s')
        follow = b'GET /api/a/books/rows HTTP/1.1\r\nHost: localhost\r\nX-Viewer-User: demo\r\n\r\n'
        for name in (b'books', b'nobody'):  # read-only refusal, then an agent with no view
            sock = Socket(post % (name, len(body), body) + follow)
            handler(self.directory, auth=UIAuth(header='X-Viewer-User'))(sock, '', object())
            out = sock.output.getvalue()
            assert out.count(b'HTTP/1.1 ') == 1 and out.split(b' ', 2)[1] in (b'403', b'404'), out

    def test_standalone_ignores_a_client_can_act_header(self):
        raw = (b'POST /api/a/books/say HTTP/1.1\r\nHost: localhost\r\nAuthorization: Basic ZGVtbzpwYXNzd29yZA==\r\n'
               b'X-Viewer-Can-Act: 1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}')
        sock = Socket(raw)
        handler(self.directory, auth=UIAuth(socket_peer=False, basic=b'demo:password'))(sock, '', ('203.0.113.1', 1))
        assert b' 403 ' in sock.output.getvalue().split(b'\r\n', 1)[0]

    def test_slow_command_trickles_then_carries_its_status(self):
        status, _, data = self.call('/api/a/books/cmd', 'POST', {'session': 's1', 'name': 'slow'})
        assert status == 200 and data.startswith(b' '), data
        assert json.loads(data) == {'error': 'agent offline', 'status': 502}

    def test_images_are_raster_only_and_revalidate(self):
        status, fields, data = self.call('/api/a/books/img?id=1&n=0')
        assert status == 200 and fields['Content-Type'] == 'image/png' and data == PNG
        assert fields['ETag'] == '"v1"' and fields['Cache-Control'] == 'private, no-cache'
        status, _, _ = self.call('/api/a/books/img?id=1&n=0', extra='If-None-Match: "v1"\r\n')
        assert status == 304
        status, fields, data = self.call('/api/a/books/img?id=1&n=9')
        assert status == 502 and fields['Content-Type'] == 'application/json'
        status, fields, data = self.call('/api/a/books/brand')
        assert status == 200 and json.loads(data)['data_url'].startswith('data:image/svg+xml;base64,')

    def test_an_unreadable_reply_still_answers(self):
        status, _, data = self.call('/api/a/books/ctx')
        assert status == 502 and b'could not read' in data, data[:200]

    def test_a_reply_past_the_deadline_times_out(self):
        with patch.object(hermes, 'ANSWER_MAX_S', 0.2):
            status, _, data = self.call('/api/a/books/cmd', 'POST', {'session': 's1', 'name': 'slow'})
        assert status == 200 and json.loads(data) == {'error': 'the agent view did not answer', 'status': 504}, data

    def test_folded_etags_are_dropped(self):
        status, fields, _ = self.call('/api/a/books/tools')
        assert status == 200 and 'ETag' not in fields and 'Set-Cookie' not in fields

    def test_json_routes_never_relay_other_types(self):
        status, fields, data = self.call('/api/a/books/skill?name=x')
        assert status == 502 and fields['Content-Type'] == 'application/json' and b'<script' not in data

    def test_events_pass_through_with_keepalives_between_frames(self):
        status, fields, data = self.call('/api/a/books/events?after=4', extra='Last-Event-ID: 5\r\n')
        assert status == 200 and fields['Content-Type'] == 'text/event-stream'
        assert fields['X-Accel-Buffering'] == 'no'
        assert data.index(b'id: 5') < data.index(b': keepalive\n\n') < data.index(b'id: 6'), data
        assert Sidecar.seen[-1][2]['Last-Event-ID'] == '5'

    def test_allowlist_is_rechecked_at_connect_and_dead_sidecar_is_502(self):
        self.address = '203.0.113.9'  # DNS now answers outside the allowlist.
        status, _, data = self.call('/api/a/books/rows')
        assert status == 502 and b'outside AGENTDIR_VIEW_ALLOW' in data, data
        self.address = '127.0.0.1'
        self.sidecar.shutdown()
        self.sidecar.server_close()
        status, _, data = self.call('/api/a/books/rows')
        assert status == 502 and b'view connection failed' in data, data

    def test_unknown_agent_route_and_presence_kind(self):
        assert self.call('/api/a/nobody/rows')[0] == 404
        assert self.call('/api/a/books/../rows')[0] == 404
        assert self.call('/api/a/books/admin')[0] == 404
        snapshot = {e['name']: e for e in self.directory.snapshot()}
        assert snapshot['books']['can_view'] and snapshot['books']['can_steer']


if __name__ == '__main__':
    unittest.main()
