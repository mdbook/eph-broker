#!/usr/bin/env python3
"""tools/vikunja_bridge.py: the shipped BROKER_MCP_SH talks to a (fake, local) Vikunja and prints what
ticket_fetch / poster expect. stdlib assert only, same runner shape as test_trackers.py."""
import json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SEEN = []
TASK = {"id": 9001, "title": "Fix it", "description": "<p>repo:/srv/git/app</p>\n<p>go</p>", "done": False,
        "project_id": 3, "assignees": [{"id": 13, "username": "eph-code"}],
        "labels": [{"id": 2, "title": "eph:code-change"}], "created_by": {"id": 5, "username": "alice"}}


class H(BaseHTTPRequestHandler):
    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        SEEN.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(n).decode()))
        body = json.dumps(TASK if self.path.endswith("/9001") else {}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    do_GET = do_PUT = _go

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
ENV = dict(os.environ, VIKUNJA_URL=f"http://127.0.0.1:{srv.server_port}", VIKUNJA_TOKEN="fake-test-value")
MCP = os.path.join(ROOT, "tools", "vikunja_bridge.py")


def run(payload, env=ENV):
    p = subprocess.run([sys.executable, MCP, "vikunja-bridge", "tools/call", json.dumps(payload)],
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, json.loads(p.stdout)


WRAPPER = os.path.join(ROOT, "tools", "mcp.sh")
for _p in (MCP, WRAPPER):
    assert os.access(_p, os.X_OK), f"{_p} must be executable: the broker execs BROKER_MCP_SH directly"
print("  PASS  both scripts are executable")

# The broker execs BROKER_MCP_SH directly (no interpreter), so run the wrapper exactly that way.
_p = subprocess.run([WRAPPER, "vikunja-bridge", "tools/call", json.dumps(
    {"name": "vikunja_task_crud", "arguments": {"operation": "get", "id": 9001}})],
    capture_output=True, text=True, env=ENV, timeout=30)
assert _p.returncode == 0 and "**id:** 9001" in json.loads(_p.stdout)["result"]["content"][0]["text"], _p.stderr
print("  PASS  wrapper runs directly, not via sys.executable")

rc, out = run({"name": "vikunja_task_crud", "arguments": {"operation": "get", "id": 9001}})
text = out["result"]["content"][0]["text"]
assert rc == 0 and text.startswith("**id:** 9001\n**title:** Fix it\n**description:** "), text
assert "\n" not in text.split("**description:** ")[1].split("\n**done:**")[0]
assert '"username": "alice"' in text and '"title": "eph:code-change"' in text
assert SEEN[-1][:3] == ("GET", "/api/v1/tasks/9001", "Bearer fake-test-value")
print("  PASS  get renders the ticket text")

rc, out = run({"name": "vikunja_task_comments", "arguments": {"operation": "comment", "id": 9001, "comment": "<p>hi</p>"}})
assert rc == 0 and SEEN[-1][:2] == ("PUT", "/api/v1/tasks/9001/comments")
assert json.loads(SEEN[-1][3]) == {"comment": "<p>hi</p>"}
print("  PASS  comment posts")

rc, out = run({"name": "vikunja_task_labels", "arguments": {"operation": "apply-label", "id": 9001, "labels": [7]}})
assert rc == 0 and json.loads(SEEN[-1][3]) == {"label_id": 7}
print("  PASS  apply-label posts")

rc, out = run({"name": "vikunja_task_crud", "arguments": {"operation": "get", "id": 9001}},
              env={k: v for k, v in ENV.items() if k != "VIKUNJA_TOKEN"})
assert rc == 1 and "error" in out and "VIKUNJA_TOKEN" in out["error"]["message"]
print("  PASS  missing token fails with the contract's error shape")

print("ok")
