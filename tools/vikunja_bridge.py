#!/usr/bin/env python3
"""vikunja_bridge.py - a working BROKER_MCP_SH for Vikunja, standard library only.

The broker runs `$BROKER_MCP_SH vikunja-bridge tools/call '<json>'` (contract: docs/tracker-script.md).
Set BROKER_MCP_SH to the absolute path of the wrapper tools/mcp.sh, which execs this file (both are mode 0755). Settings come from the broker's environment:

    VIKUNJA_URL    base URL of your Vikunja, e.g. https://vikunja.example.com (no /api/v1)
    VIKUNJA_TOKEN  an API token (Settings > API Tokens) with tasks read/write; a secret, so it goes in the
                   broker's secrets file, never in site.env

Prints one JSON-RPC style object on stdout; exits 0 on success and 1 on failure (with {"error": ...}).
"""
import json
import os
import sys
import urllib.error
import urllib.request

TIMEOUT = 25


def api(method, path, body=None):
    base = os.environ.get("VIKUNJA_URL", "").rstrip("/")
    token = os.environ.get("VIKUNJA_TOKEN", "")
    if not base or not token:
        raise RuntimeError("VIKUNJA_URL and VIKUNJA_TOKEN must be set in the broker's environment")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}/api/v1{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"vikunja HTTP {e.code} on {method} {path}") from None


def _user(u):
    return {"id": u.get("id"), "username": u.get("username", "")}


def render_task(t):
    """The `get` text the broker parses (docs/tracker-script.md, 'The ticket text a get returns'). Field order matters."""
    desc = (t.get("description") or "").replace("\n", " ")
    lines = [
        f"**id:** {t['id']}",
        f"**title:** {(t.get('title') or '').replace(chr(10), ' ')}",
        f"**description:** {desc}",
        f"**done:** {'true' if t.get('done') else 'false'}",
        f"**project_id:** {t.get('project_id', 0)}",
        f"**assignees:** {json.dumps([_user(u) for u in t.get('assignees') or []])}",
        f"**labels:** {json.dumps([{'id': l.get('id'), 'title': l.get('title', '')} for l in t.get('labels') or []])}",
        f"**created_by:** {json.dumps(_user(t.get('created_by') or {}))}",
    ]
    return "\n".join(lines)


def render_list(tasks):
    out = []
    for i, t in enumerate(tasks, 1):
        status = "✅ Done" if t.get("done") else "❌ Not Done"
        desc = (t.get("description") or "").replace("\n", " ")
        out.append(f"### {i}. **{t.get('title', '')}** (ID: {t['id']})\n- **Status:** {status}\n- **Description:** {desc}")
    return "\n\n".join(out)


def call(name, a):
    op, tid = a.get("operation"), a.get("id")
    if name == "vikunja_task_crud":
        if op == "get":
            return render_task(api("GET", f"/tasks/{int(tid)}"))
        if op == "list":
            return render_list(api("GET", "/tasks/all?per_page=200") or [])
    elif name == "vikunja_task_comments" and op == "comment":
        api("PUT", f"/tasks/{int(tid)}/comments", {"comment": a["comment"]})
        return "comment posted"
    elif name == "vikunja_task_labels" and op == "apply-label":
        for lid in a["labels"]:
            api("PUT", f"/tasks/{int(tid)}/labels", {"label_id": int(lid)})
        return "labels applied"
    elif name == "vikunja_task_assignees" and op == "assign":
        for uid in a["assignees"]:
            api("PUT", f"/tasks/{int(tid)}/assignees", {"user_id": int(uid)})
        return "assigned"
    raise RuntimeError(f"unsupported call {name} {op}")


def main(argv):
    if len(argv) != 4 or argv[1] != "vikunja-bridge" or argv[2] != "tools/call":
        print(json.dumps({"error": {"code": -32600, "message": "usage: vikunja-bridge tools/call '<json>'"}}))
        return 1
    try:
        req = json.loads(argv[3])
        text = call(req["name"], req.get("arguments") or {})
    except Exception as e:  # noqa: BLE001 - every failure is reported in the contract's error shape
        print(json.dumps({"error": {"code": -32000, "message": str(e)}}))
        return 1
    print(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": text}]}}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
