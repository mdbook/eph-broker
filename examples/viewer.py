#!/usr/bin/env python3
"""Runnable stdlib module example. No broker imports, accounts or dependencies."""
import json
import os
from http.server import BaseHTTPRequestHandler
from socketserver import ThreadingMixIn, UnixStreamServer


UI = b"""
export function mount(el, ctx) {
  const h = document.createElement('h1');
  h.textContent = ctx.agentName ? 'Agent: ' + ctx.agentName : 'Example agent viewer';
  el.appendChild(h);
  const p = document.createElement('p');
  p.textContent = 'Native broker DOM. Module upgrades do not restart the broker.';
  el.appendChild(p);
}
export function quickView(el, agentName, ctx) {
  const p = document.createElement('p');
  p.textContent = 'Module quick view for ' + agentName;
  el.appendChild(p);
}
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/ui.js":
            return self.reply(200, UI, "text/javascript")
        if path == "/healthz":
            return self.reply(200, b'{}')
        if path == "/fleet-index":
            return self.reply(200, json.dumps(["demo"]).encode())
        if path == "/identity":
            return self.reply(200, json.dumps({"user": self.headers.get("X-Viewer-User"),
                                               "can_act": self.headers.get("X-Viewer-Can-Act")}).encode())
        if path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: first\n\ndata: second\n\n")
            return self.wfile.flush()
        return self.reply(404, b'{}')

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.reply(200, body)

    def reply(self, code, body, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True


if __name__ == "__main__":
    path = os.environ["BROKER_MODULE_SOCKET"]
    with Server(path, Handler) as server:
        os.chmod(path, 0o600)
        server.serve_forever()
