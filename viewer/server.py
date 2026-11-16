"""Separate UI and agent listeners. No broker configuration or secrets imported."""
import base64
import hmac
import ipaddress
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import adapters
from .directory import Refused

ASSETS = Path(__file__).parent
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

    def accepts(self, headers, peer):
        if self.socket_peer:
            return bool(headers.get(self.header, '').strip())
        try:
            trusted = any(ipaddress.ip_address(peer) in net for net in self.proxies)
        except ValueError:
            trusted = False
        if trusted and headers.get(self.header, '').strip():
            return True
        if self.basic:
            expected = b'Basic ' + base64.b64encode(self.basic)
            # Bytes: compare_digest refuses non-ASCII str, and headers arrive latin-1 decoded.
            return hmac.compare_digest(headers.get('Authorization', '').encode('latin-1', 'replace'), expected)
        return False


def handler(directory, agent_api=False, auth=None, standalone=False):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass  # Do not log enrollment tokens, signed bodies or query strings.

        def send(self, status, data, content_type='application/json'):
            if not isinstance(data, bytes):
                data = json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if status == 401 and not agent_api:
                self.send_header('WWW-Authenticate', 'Basic realm="Agent directory"')
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not agent_api or self.path not in ('/v1/enroll', '/v1/register', '/v1/beat', '/v1/deregister'):
                self.close_connection = True
                return self.send(404, {'error': 'not found'})
            self.connection.settimeout(10)
            try:
                length = int(self.headers.get('Content-Length', '-1'))
                if (self.headers.get('Transfer-Encoding') or
                        len(self.headers.get_all('Content-Length', [])) != 1 or not 0 <= length <= 16384):
                    raise Refused('body must have Content-Length, at most 16 KiB', 413)
                body = self.rfile.read(length)
                if len(body) != length:
                    raise Refused('incomplete body')
                result = directory.request(self.path, body, self.headers)
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
            peer = self.client_address[0] if isinstance(self.client_address, tuple) else ''
            if not auth or not auth.accepts(self.headers, peer):
                return self.send(401, {'error': 'authentication required'})
            if path == '/ui.js':
                return self.send(200, (ASSETS / 'ui.js').read_bytes(), 'text/javascript; charset=utf-8')
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
                if action == 'view' and entries[0]['can_view']:
                    with directory.lock:
                        entry = dict(directory.entries.get(name) or {})
                    if not entry:
                        return self.send(404, {'error': 'agent not found'})
                    try:
                        result = adapters.get(entry['kind']).view(entry, directory.allowlist)
                        return self.send(200, result)
                    except (OSError, ValueError):
                        return self.send(502, {'error': 'view connection refused'})
            self.send(404, {'error': 'not found'})

    return Handler


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def serve(directory, socket_path=None, bind=('127.0.0.1', 8790), port=None, auth=None):
    agent = ThreadingHTTPServer(bind, handler(directory, agent_api=True))
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
