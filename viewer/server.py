"""Separate UI and agent listeners. No broker configuration or secrets imported."""
import base64
import hmac
import ipaddress
import json
import logging
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import adapters, assets, transcripts
from .directory import Refused

ASSETS = Path(__file__).parent
log = logging.getLogger('eph-broker.viewer')
# The agent view's renderer files (fixed names; nothing else is served from AGENTDIR_HERMES_UI_DIR).
# logo.svg is the stylesheet's own mark: operator-installed like the rest, never anything an agent sends.
VIEW_UI = {'/hermes-ui/view.js': 'text/javascript; charset=utf-8', '/hermes-ui/app.css': 'text/css; charset=utf-8',
           '/hermes-ui/logo.svg': 'image/svg+xml'}
PAGE = b'''<!doctype html><meta charset="utf-8"><title>Agents</title>
<style>:root{color-scheme:light dark}body{font:16px system-ui;margin:2rem}
td,th{text-align:left;padding:.5rem}table{border-collapse:collapse}</style>
<h1>Agents</h1><main id="agents"></main><script type="module">
import {mount} from '/ui.js'; mount(document.getElementById('agents'), {});
</script>'''


class UIAuth:
    def __init__(self, socket_peer=True, header='X-Forwarded-User', proxies='', basic=None):
        self.socket_peer, self.header, self.basic = socket_peer, header, basic
        self.proxies = [ipaddress.ip_network(v.strip()) for v in proxies.split(',') if v.strip()]

    def trusted(self, peer):
        try:
            return any(ipaddress.ip_address(peer) in net for net in self.proxies)
        except ValueError:
            return False

    def user(self, headers, peer=''):
        """The authenticated name accepts() admitted: the identity header from a trusted source, else the basic user."""
        name = headers.get(self.header, '').strip() if self.socket_peer or self.trusted(peer) else ''
        if not name and self.basic and not self.socket_peer:
            name = self.basic.split(b':', 1)[0].decode('utf-8', 'replace')
        return name

    def can_act(self, headers):
        # Only the broker module host (socket mode) sets this; a standalone client could forge it.
        return self.socket_peer and headers.get('X-Viewer-Can-Act', '') == '1'

    def accepts(self, headers, peer):
        if self.socket_peer:
            return bool(headers.get(self.header, '').strip())
        if self.trusted(peer) and headers.get(self.header, '').strip():
            return True
        if self.basic:
            expected = b'Basic ' + base64.b64encode(self.basic)
            # Bytes: compare_digest refuses non-ASCII str, and headers arrive latin-1 decoded.
            return hmac.compare_digest(headers.get('Authorization', '').encode('latin-1', 'replace'), expected)
        return False


def handler(directory, agent_api=False, auth=None, standalone=False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        timeout = 10 if agent_api else None

        def log_message(self, *args):
            pass  # Do not log enrollment tokens, signed bodies or query strings.

        def send(self, status, data, content_type='application/json', headers=None):
            if not isinstance(data, bytes):
                data = json.dumps(data).encode()
            headers = headers or {}
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', headers.get('Cache-Control', 'no-store'))
            if headers.get('ETag'):
                self.send_header('ETag', headers['ETag'])
            self.send_header('X-Content-Type-Options', 'nosniff')
            if status == 401 and not agent_api:
                self.send_header('WWW-Authenticate', 'Basic realm="Agent directory"')
            self.end_headers()
            self.wfile.write(data)

        def view_ui(self, path):
            # The agent view's renderer, from an operator-installed directory (the view sidecar's web/ at a release).
            root = os.environ.get('AGENTDIR_HERMES_UI_DIR', '')
            if not root:
                return self.send(404, {'error': 'the agent view UI is not installed (AGENTDIR_HERMES_UI_DIR)'})
            try:
                data = (Path(root) / path.rsplit('/', 1)[1]).read_bytes()
            except OSError:
                return self.send(404, {'error': 'the agent view UI is missing ' + path.rsplit('/', 1)[1]})
            return self.send(200, data, VIEW_UI[path], {'Cache-Control': 'no-cache'})

        def peer(self):
            return self.client_address[0] if isinstance(self.client_address, tuple) else ''

        def authorized(self):
            if not auth or not auth.accepts(self.headers, self.peer()):
                self.send(401, {'error': 'authentication required'})
                return False
            return True

        def agent_view(self, method):
            """/api/a/NAME/ROUTE: the agent's adapter view, read routes GET and actions POST."""
            parts = urlsplit(self.path)
            segments = parts.path.split('/')
            if len(segments) != 5 or segments[:3] != ['', 'api', 'a']:
                return self.send(404, {'error': 'not found'})
            name, route = unquote(segments[3]), segments[4]
            with directory.lock:
                directory.reap()
                entry = dict(directory.entries.get(name) or {})
            adapter = adapters.get(entry.get('kind'))
            if not entry or not entry['view_url'] or 'view' not in entry['caps'] or not hasattr(adapter, 'READ'):
                self.close_connection = method == 'POST'
                return self.send(404, {'error': 'agent has no view'})
            body = b''
            if method == 'POST':
                self.close_connection = True  # a refusal below leaves the body unread on the socket
                if route not in adapter.ACT:
                    return self.send(404, {'error': 'not found'})
                if not auth.can_act(self.headers):
                    return self.send(403, {'error': 'read-only viewer'})
                if 'steer' not in entry['caps']:
                    return self.send(403, {'error': 'this agent does not accept actions'})
                body = self.body(adapter.body_limit(route))
                if body is None:
                    return None
                if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                    return self.send(415, {'error': 'send application/json'})
            elif route not in adapter.READ:
                return self.send(404, {'error': 'not found'})
            reply = adapter.view(entry, directory.allowlist, dict(
                route=route, method=method, query=parts.query, body=body, user=auth.user(self.headers, self.peer()),
                headers={k: self.headers[k] for k in ('Accept', 'Last-Event-ID', 'If-None-Match') if self.headers.get(k)}))
            if reply.stream is None:
                return self.send(reply.status, reply.body, reply.content_type, reply.headers)
            self.send_response(reply.status)
            self.send_header('Content-Type', reply.content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            try:
                for chunk in reply.stream:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                reply.stream.close()

        def body(self, limit):
            self.connection.settimeout(30)
            length = int(self.headers.get('Content-Length', '-1')) if (
                self.headers.get('Content-Length', '').isdigit()) else -1
            if (self.headers.get('Transfer-Encoding') or
                    len(self.headers.get_all('Content-Length', [])) != 1 or not 0 <= length <= limit):
                self.close_connection = True
                self.send(413, {'error': 'body must have Content-Length, at most %d bytes' % limit})
                return None
            data = self.rfile.read(length)
            if len(data) != length:
                self.close_connection = True
                self.send(400, {'error': 'incomplete body'})
                return None
            return data

        def do_POST(self):
            if not agent_api:
                if not self.authorized():
                    return None
                segments = urlsplit(self.path).path.split('/')
                if len(segments) == 5 and segments[:3] == ['', 'api', 'agents'] and segments[4] == 'steer':
                    self.close_connection = True
                    if not auth.can_act(self.headers):
                        return self.send(403, {'ok': False, 'error': 'read-only viewer'})
                    if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                        return self.send(415, {'ok': False, 'error': 'send application/json'})
                    body = self.body(65536)  # 4000 characters, including escaped surrogate pairs
                    if body is None:
                        return None
                    try:
                        data = json.loads(body)
                        if not isinstance(data, dict):
                            raise Refused('expected a JSON object')
                        return self.send(200, directory.queue_message(
                            unquote(segments[3]), data.get('text'), auth.user(self.headers, self.peer())))
                    except (ValueError, RecursionError) as exc:
                        return self.send(getattr(exc, 'status', 400), {'ok': False, 'error': str(exc)})
                    except OSError:
                        return self.send(503, {'ok': False, 'error': 'steering queue storage unavailable'})
                return self.agent_view('POST')
            if self.path not in ('/v1/enroll', '/v1/register', '/v1/beat', '/v1/deregister', '/v1/events'):
                self.close_connection = True
                return self.send(404, {'error': 'not found'})
            try:
                limit = transcripts.BATCH_BYTES if self.path == '/v1/events' else 16384
                length = int(self.headers.get('Content-Length', '-1'))
                if (self.headers.get('Transfer-Encoding') or
                        len(self.headers.get_all('Content-Length', [])) != 1 or not 0 <= length <= limit):
                    raise Refused('body must have Content-Length, at most %d bytes' % limit, 413)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise Refused('incomplete body')
                try:
                    result = directory.request(self.path, body, self.headers)
                except OSError:
                    self.close_connection = True
                    return self.send(503, {'ok': False, 'error': 'agent directory storage unavailable'})
                self.send(200, result)
            except (Refused, ValueError) as exc:
                self.close_connection = True
                self.send(getattr(exc, 'status', 400), {'error': str(exc)})

        def do_GET(self):
            if agent_api:
                return self.send(404, {'error': 'not found'})
            path = urlsplit(self.path).path
            if path == '/healthz':
                return self.send(200, {'ok': True})
            if not self.authorized():
                return None
            if path.startswith('/api/a/'):
                return self.agent_view('GET')
            if path == '/ui.js':
                return self.send(200, (ASSETS / 'ui.js').read_bytes(), 'text/javascript; charset=utf-8')
            if path == '/transcript.js':
                try:
                    script = assets.transcript_module().encode()
                except ImportError:
                    return self.send(503, {'error': 'install the core eph-broker package for transcript rendering'})
                return self.send(200, script, 'text/javascript; charset=utf-8')
            if path == '/transcript.css':
                try:
                    css = assets.transcript_css().encode()
                except ImportError:
                    return self.send(503, {'error': 'install the core eph-broker package for transcript rendering'})
                return self.send(200, css, 'text/css; charset=utf-8')
            if path in VIEW_UI:
                return self.view_ui(path)
            if path == '/' and standalone:
                return self.send(200, PAGE, 'text/html; charset=utf-8')
            if path == '/api/agents':
                return self.send(200, {'agents': directory.snapshot()})
            if path == '/fleet-index':
                # Module host contract: names this viewer can show in a Fleet quick view.
                return self.send(200, [e['name'] for e in directory.snapshot()])
            parts = path.split('/')
            if len(parts) == 5 and parts[:3] == ['', 'api', 'agents']:
                name, action = unquote(parts[3]), parts[4]
                entries = [e for e in directory.snapshot() if e['name'] == name]
                if not entries:
                    return self.send(404, {'error': 'agent not found'})
                if action == 'transcript':
                    if not entries[0]['can_tail']:
                        return self.send(403, {'error': 'agent has no tail capability'})
                    return self.tail_stream(name)
                if action == 'events':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Accel-Buffering', 'no')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    try:
                        while True:
                            current = next((e for e in directory.snapshot() if e['name'] == name), None)
                            self.wfile.write(('event: presence\ndata: ' + json.dumps(current) + '\n\n').encode())
                            self.wfile.flush()
                            if current is None:
                                return
                            time.sleep(5)
                    except (BrokenPipeError, ConnectionError, OSError):
                        return
            self.send(404, {'error': 'not found'})

        def tail_stream(self, name):
            # The module host owns the stream semaphore and lifetime deadline, just
            # as for adapter SSE. Keepalive stays below its upstream idle timeout.
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            cursor = self.headers.get('Last-Event-ID', '')[:128]
            try:
                while True:
                    with directory.lock:
                        directory.reap()
                        entry = directory.entries.get(name)
                        available = entry and 'tail' in entry['caps'] and entry['kind'] in ('claude-code', 'codex')
                        tail = directory.tails.get(name)
                        reset, frames, next_cursor = tail.replay(cursor) if tail else (False, [], '')
                    if not available:
                        self.wfile.write(b'event: end\ndata: {}\n\n')
                        self.wfile.flush()
                        return
                    if reset:
                        self.wfile.write(b'event: reset\ndata: {}\n\n')
                    for event_id, line in frames:
                        self.wfile.write(('id: %s\ndata: %s\n\n' % (event_id, line)).encode())
                    # Advance even on an empty buffer. A reset frame also carries
                    # a cursor so reconnects do not repeatedly reset an empty view.
                    self.wfile.write(('id: %s\nevent: cursor\ndata: {}\n\n' % next_cursor).encode())
                    self.wfile.flush()
                    cursor = next_cursor
                    time.sleep(1)
            except (BrokenPipeError, ConnectionError, OSError):
                return

    return Handler


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class AgentServer(ThreadingHTTPServer):
    """Bound agent connections before starting threads, including idle keep-alive peers."""
    def __init__(self, address, request_handler):
        try:
            limit = int(os.environ.get('AGENTDIR_AGENT_MAX_CONNECTIONS', '64'))
            if limit < 1:
                raise ValueError()
        except ValueError:
            log.error('agent listener: invalid AGENTDIR_AGENT_MAX_CONNECTIONS; using 64')
            limit = 64
        self._slots = threading.BoundedSemaphore(limit)
        super().__init__(address, request_handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # No worker owns this slot if thread creation fails.
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(directory, socket_path=None, bind=('127.0.0.1', 8790), port=None, auth=None):
    agent = AgentServer(bind, handler(directory, agent_api=True))
    ui = None
    try:
        if port is None:
            # Never unlink an existing path: it may belong to a running module.
            ui = UnixServer(socket_path, handler(directory, auth=auth))
            os.chmod(socket_path, 0o600)
        else:
            ui = ThreadingHTTPServer(('127.0.0.1', port), handler(directory, auth=auth, standalone=True))
        threading.Thread(target=agent.serve_forever, daemon=True).start()
        ui.serve_forever()
    finally:
        agent.server_close()
        if ui:
            ui.server_close()
            if port is None:
                os.unlink(socket_path)
