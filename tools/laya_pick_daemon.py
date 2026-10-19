#!/usr/bin/env python3
"""Laya repo-pick daemon for the eph-broker.

Broker-owned: a separate policy-judge daemon, if you run one, only answers its own
question, so repo inference gets its OWN resident Laya. Laya = convaiinnovations/laya (421M ModernBERT typed-
decision classifier): ~52 s cold load, ~2.4 GB, ~4.5 s per CPU call, so keep it loaded and answer over a 0600 unix
socket (same uid only), one request at a time.

Runs under a Python venv with `laya` pip-installed (py3.11; set the venv in the service unit; LAYA_PYTHONPATH may
add a source dir read-only). The broker itself stays py3.9 stdlib-only and talks to this over the socket
(repo_infer.laya_pick). NOT started by the RPM: packaging/eph-laya-pick.service is an example user unit;
whether to deploy it is up to the site operator.

    laya_pick_daemon.py serve                      # foreground (systemd --user unit)
    laya_pick_daemon.py pick '<ticket text>' name=card [name=card ...]   # one query against the daemon
    laya_pick_daemon.py --selftest                 # offline (stub model)

Protocol: one JSON line in {"verb": "pick", "text": str, "options": {name: card}} (or {"verb": "ping"}),
one JSON line out {"choice": name, "probs": {name: p}, "ms": int} | {"ok": true} | {"error": str}.
"""
import json
import os
import socket
import sys
import time

SOCK = os.path.expanduser(os.environ.get("LAYA_PICK_SOCK", "~/.eph-broker/laya-pick.sock"))
MODEL = os.environ.get("LAYA_MODEL", "convaiinnovations/laya")
MAX_IN = 65536
MAX_TEXT = 1500          # Laya's window is 512 tokens incl. options; the broker already trims to ~1000 chars
MAX_OPTIONS = 12         # options budget 192/512 tok (laya/agent.py:243-249) -> the broker sends <= 8
INSTRUCTIONS = "Which code repository should this change request be made in?"


def _load():
    extra = os.environ.get("LAYA_PYTHONPATH")
    if extra:
        sys.path.insert(0, extra)
    import laya
    return laya.load(MODEL)


def _probs(ans):
    p = ans.get("probabilities")
    if isinstance(p, dict):
        return {str(k): float(v) for k, v in p.items()}
    if isinstance(p, list):                                 # tolerate [[name, p], ...]
        return {str(k): float(v) for k, v in p}
    raise ValueError("laya answer carried no probabilities")


def handle(agent, req):
    verb = req.get("verb")
    if verb == "ping":
        return {"ok": True}
    if verb != "pick":
        return {"error": f"unknown verb {verb!r}"}
    text, options = req.get("text"), req.get("options")
    if not isinstance(text, str) or not text.strip():
        return {"error": "text must be a non-empty string"}
    if (not isinstance(options, dict) or not 1 <= len(options) <= MAX_OPTIONS
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in options.items())):
        return {"error": f"options must be a {{name: card}} dict of 1..{MAX_OPTIONS} strings"}
    t = time.time()
    q = {"repo": {"type": "choice", "instructions": INSTRUCTIONS,
                  "criteria": {k[:80]: v[:80] for k, v in options.items()}}}
    ans = agent.predict({"ticket": text[:MAX_TEXT]}, q)["answers"]["repo"]
    return {"choice": ans.get("choice"), "probs": _probs(ans), "ms": round(1000 * (time.time() - t))}


def serve(agent=None):
    agent = agent or _load()
    handle(agent, {"verb": "pick", "text": "warm up", "options": {"a": "first", "b": "second"}})
    try:  # a second `serve` must not steal a live daemon's socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.connect(SOCK)
        sys.exit(f"laya pick daemon already listening on {SOCK}")
    except OSError:
        pass
    os.makedirs(os.path.dirname(SOCK), mode=0o700, exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    srv.bind(SOCK)  # created 0600
    os.umask(old)
    srv.listen(8)
    while True:
        conn = None
        try:
            conn, _ = srv.accept()
            deadline, buf = time.time() + 5, b""  # one overall read deadline per request
            while not buf.endswith(b"\n") and len(buf) < MAX_IN:
                conn.settimeout(max(0.05, deadline - time.time()))
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            out = handle(agent, json.loads(buf))
        except Exception as e:  # a bad request / failed predict never kills the daemon
            out = {"error": repr(e)[:200]}
        if conn is not None:
            try:
                conn.sendall((json.dumps(out) + "\n").encode())
                conn.close()
            except OSError:
                pass


def ask(req, timeout=60.0):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.settimeout(timeout)
            c.connect(SOCK)
            c.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = c.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf)
    except Exception as e:
        return {"error": repr(e)[:200]}


def selftest():
    """Offline: stub model; 0600 socket, ping, pick, bad requests survive, second serve refuses."""
    import stat
    import tempfile
    import threading
    global SOCK
    SOCK = os.path.join(tempfile.mkdtemp(), "sub", "laya-pick.sock")
    seen = []

    class Stub:
        def predict(self, state, q):
            seen.append((state, q))
            names = list(q["repo"]["criteria"])
            probs = {n: (0.9 if i == 0 else 0.1 / max(1, len(names) - 1)) for i, n in enumerate(names)}
            return {"answers": {"repo": {"choice": names[0], "probabilities": probs}}}
    threading.Thread(target=serve, args=(Stub(),), daemon=True).start()
    for _ in range(100):   # bind() creates the file before listen(): wait for an answered ping, not the path
        if ask({"verb": "ping"}, timeout=1) == {"ok": True}:
            break
        time.sleep(0.05)
    assert stat.S_IMODE(os.stat(SOCK).st_mode) == 0o600
    assert ask({"verb": "ping"}, timeout=5) == {"ok": True}
    r = ask({"verb": "pick", "text": "fix game sync", "options": {"game-sync": "game saves", "pics": "gallery"}},
            timeout=5)
    assert r["choice"] == "game-sync" and abs(sum(r["probs"].values()) - 1.0) < 1e-6, r
    assert seen[-1][0] == {"ticket": "fix game sync"} and seen[-1][1]["repo"]["type"] == "choice"
    assert "error" in ask({"verb": "pick", "text": "x", "options": {}}, timeout=5)
    assert "error" in ask({"verb": "pick", "text": "x", "options": {str(i): "c" for i in range(20)}}, timeout=5)
    assert "error" in ask({"verb": "nope"}, timeout=5)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:  # garbage request
        c.connect(SOCK)
        c.sendall(b"not json\n")
        assert "error" in json.loads(c.recv(4096))
    assert ask({"verb": "ping"}, timeout=5) == {"ok": True}      # still alive
    try:
        serve(Stub())
        raise AssertionError("second serve must refuse")
    except SystemExit as e:
        assert "already listening" in str(e)
    print("laya_pick_daemon selftest ok")
    return True


if __name__ == "__main__":
    if sys.argv[1:2] == ["--selftest"]:
        selftest()
    elif sys.argv[1:2] == ["serve"]:
        serve()
    elif sys.argv[1:2] == ["pick"] and len(sys.argv) >= 4:
        opts = dict(a.split("=", 1) for a in sys.argv[3:])
        print(json.dumps(ask({"verb": "pick", "text": sys.argv[2], "options": opts})))
    else:
        sys.exit(__doc__)
