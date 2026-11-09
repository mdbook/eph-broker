"""Optional out-of-process UI modules. No module imports or dispatch dependencies.

Configuration is snapshotted at startup. Each module has its own supervisor and
request semaphore; reload only signals that supervisor, never waits on a child.
"""
import hashlib
import http.client
import ipaddress
import os
import re
import shlex
import signal
import socket
import stat
import subprocess
import threading
import time
import urllib.parse

import actions
import stream

CONNECT_S = 2
READ_S = 10
HEALTH_S = 10
POLL_S = 0.2
STOP_GRACE_S = 2
_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=CONNECT_S):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def configuration():
    """Reject ambiguous/unsafe config rather than silently choosing a mode."""
    state = os.path.dirname(os.path.abspath(os.environ["BROKER_STATE_PATH"])) if os.getenv("BROKER_STATE_PATH") else \
        os.path.join(os.getenv("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "eph-broker")
    out = {}
    for name in filter(None, (n.strip() for n in os.getenv("BROKER_MODULES", "").split(","))):
        if not _NAME.fullmatch(name) or name in out:
            raise ValueError("module names must be unique lowercase letters, digits or underscores, starting with a letter")
        prefix = "BROKER_MODULE_" + name.upper()
        get = lambda suffix: os.getenv(prefix + "_" + suffix, "").strip()
        cmd, upstream = get("CMD"), get("UPSTREAM")
        if bool(cmd) == bool(upstream):
            raise ValueError(f"{prefix}: configure exactly one of CMD or UPSTREAM")
        endpoint = upstream or get("SOCKET") or os.path.join(state, "modules", name + ".sock")
        base, host, port = "", None, None
        if not endpoint.startswith("/"):
            url = urllib.parse.urlsplit(endpoint)
            if url.scheme != "http" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError(f"{prefix}: expected an absolute socket path or loopback http URL")
            host = "127.0.0.1" if url.hostname == "localhost" else url.hostname
            if not ipaddress.ip_address(host).is_loopback or cmd:
                raise ValueError(f"{prefix}: URL upstreams must be loopback and external")
            port, base = url.port or 80, url.path.rstrip("/")
        cap = int(get("MAX_STREAMS") or 8)
        if cap < 1:
            raise ValueError(f"{prefix}: MAX_STREAMS must be positive")
        out[name] = dict(name=name, label=get("LABEL") or name.title(), group_var=prefix + "_GROUP",
                         cmd=shlex.split(cmd), endpoint=endpoint, host=host, port=port, base=base,
                         cap=cap, version_file=get("VERSION_FILE"), disabled_file=get("DISABLED_FILE"))
    return out


class Module:
    def __init__(self, config):
        self.config = config
        self.slots = threading.BoundedSemaphore(config["cap"])
        self.lock = threading.Lock()
        self.stop_event, self.reload_event = threading.Event(), threading.Event()
        self.thread = None
        self.child = None
        self.state, self.since = "starting", time.time()
        self.restarts, self.last_error, self.version = 0, "", ""
        self.attempts, self.failures, self.backoff = 0, 0, 1
        self.next_start, self.next_health, self.healthy_since = 0, 0, None
        self.fingerprint = self._version()

    def connection(self, timeout=CONNECT_S):
        c = self.config
        if c["host"]:
            return http.client.HTTPConnection(c["host"], c["port"], timeout=timeout)
        return UnixConnection(c["endpoint"], timeout=timeout)

    def status(self):
        with self.lock:
            return dict(state=self.state, since=self.since, restarts=self.restarts,
                        last_error=self.last_error, version=self.version)

    def _state(self, state, error=None):
        with self.lock:
            if self.state != state:
                self.state, self.since = state, time.time()
            if error is not None:
                self.last_error = error

    def _version(self):
        path = self.config["version_file"]
        if not path:
            return None
        try:
            with open(path, "rb") as f:
                digest = hashlib.sha256()
                for chunk in iter(lambda: f.read(65536), b""):
                    digest.update(chunk)
                return os.fstat(f.fileno()).st_mtime_ns, digest.hexdigest()
        except OSError:
            return None

    def _kill(self):
        child, self.child = self.child, None
        if child is not None:
            # Keep the group id even if the leader has already exited. Waiting
            # for the leader alone cannot tell us whether descendants survived.
            pgid = child.pid
            deadline = time.monotonic() + STOP_GRACE_S
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(pgid, 0)
                time.sleep(max(0, deadline - time.monotonic()))
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=2)
        self.healthy_since = None

    def _failed(self, now, error):
        self._kill()
        self._state("down", error)
        self.next_start = now + self.backoff
        self.backoff = min(60, self.backoff * 2)

    def _health(self):
        conn = self.connection(timeout=2)
        deadline = time.monotonic() + 2
        timer = None
        response = None
        try:
            conn.connect()
            sock = conn.sock
            remaining = max(0.001, deadline - time.monotonic())
            sock.settimeout(remaining)
            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            timer = threading.Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            conn.request("GET", self.config["base"] + "/healthz")
            response = conn.getresponse()
            return response.status == 200
        finally:
            if timer:
                timer.cancel()
            if response:
                response.close()
            conn.close()

    def step(self, now):
        """One supervisor iteration, separated for deterministic lifecycle tests."""
        c = self.config
        if c["disabled_file"] and os.path.lexists(c["disabled_file"]):
            self._kill()
            self._state("disabled")
            return
        version = self._version()
        reload = self.reload_event.is_set()
        if reload:
            self.reload_event.clear()
        if version != self.fingerprint or reload or self.state == "disabled":
            self.fingerprint = version
            self._kill()
            self.next_start = self.next_health = 0
            self.failures = 0
            self._state("starting")
        with self.lock:
            self.version = version[1] if version else ""
        if c["cmd"] and self.child is not None:
            code = self.child.poll()
            if code is not None:
                self._failed(now, f"child exited ({code})")
        if c["cmd"] and self.child is None:
            if now < self.next_start:
                return
            self._state("starting")
            try:
                os.makedirs(os.path.dirname(c["endpoint"]), mode=0o700, exist_ok=True)
                if os.path.lexists(c["endpoint"]):
                    if not stat.S_ISSOCK(os.lstat(c["endpoint"]).st_mode):
                        raise OSError("module socket path is not a socket")
                    os.unlink(c["endpoint"])
                env = dict(os.environ, BROKER_MODULE_SOCKET=c["endpoint"], BROKER_MODULE_NAME=c["name"])
                self.child = subprocess.Popen(c["cmd"], env=env, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                              start_new_session=True, umask=0o177)
                with self.lock:
                    self.restarts = self.attempts
                self.attempts += 1
                self.failures, self.next_health = 0, now
            except (OSError, ValueError):
                self._failed(now, "child launch failed")
                return
        if now < self.next_health:
            return
        self.next_health = now + HEALTH_S
        try:
            if c["cmd"]:
                os.chmod(c["endpoint"], 0o600)
            healthy = self._health()
        except (OSError, http.client.HTTPException):
            healthy = False
        if healthy:
            self.failures = 0
            self._state("up", "")
            if self.healthy_since is None:
                self.healthy_since = now
            if now - self.healthy_since >= 60:
                self.backoff = 1
        else:
            self.healthy_since = None
            self.failures += 1
            if self.failures >= 3:
                self._failed(now, "three consecutive health checks failed")
            elif self.state == "up":
                self._state("down", "health check failed")

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.step(time.monotonic())
                except Exception:
                    self._failed(time.monotonic(), "supervisor error")
                self.stop_event.wait(POLL_S)
        finally:
            self._kill()

    def start(self):
        self.thread = threading.Thread(target=self._run, name="module-" + self.config["name"], daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)
        else:
            self._kill()


class Host:
    def __init__(self, configs=None):
        self.modules = {name: Module(c) for name, c in (configuration() if configs is None else configs).items()}

    def start(self):
        for module in self.modules.values():
            module.start()

    def stop(self):
        for module in self.modules.values():
            module.stop()

    def reload(self, name):
        module = self.modules.get(name) if isinstance(name, str) else None
        if module is None:
            return 404, {"ok": False, "error": "unknown module"}
        if not module.config["cmd"]:
            return 409, {"ok": False, "error": "external modules are managed by their owner"}
        module.reload_event.set()
        return 200, {"ok": True, "name": name}

    def status(self, headers):
        return {n: m.status() for n, m in self.modules.items() if allowed(headers, m)[0]}


def allowed(headers, module):
    return (False, "") if actions._duplicated(headers) else stream.authorized(headers, module.config["group_var"])


def can_act(headers):
    return bool(actions.enabled() and not actions._duplicated(headers) and actions._proxy_ok(headers)
                and stream.authorized(headers, "BROKER_ACTIONS_GROUP")[0])


def serve(handler, host):
    """Authenticated, bounded proxy. Only an explicit header allowlist crosses either way."""
    raw = handler.path[len("/viewer/"):]
    name, _, rest = raw.partition("/")
    module = host.modules.get(name) if host else None
    if module is None:
        return handler._reply(404, {"error": "not found"})
    ok, user = allowed(handler.headers, module)
    if not ok:
        return handler._reply(403, {"error": "forbidden"})
    body = None
    if handler.command == "POST":
        ok, _, why = actions.check(handler.headers)
        if not actions.enabled() or not ok:
            actions.reject(user, "module_post", why or "disabled")
            return handler._reply(403, {"error": "action not authorised"})
    if not module.slots.acquire(blocking=False):
        return handler._reply(503, {"error": "module request limit"})
    conn, response, timer, sent = None, None, None, False
    deadline = time.monotonic() + max(0.01, stream.max_s())
    upstream_socket = None
    # Start before reading the body: an idle timeout cannot bound a trickling
    # upload. Retain the upstream socket after HTTPConnection detaches it, too.
    def expire():
        for sock in (upstream_socket, handler.connection):
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
    try:
        timer = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        if module.status()["state"] != "up":
            return handler._reply(503, {"error": "module unavailable"})
        if handler.command == "POST":
            body = handler._body()
            if body is None:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("module request deadline")
        conn = module.connection(timeout=min(CONNECT_S, remaining))
        conn.connect()
        upstream_socket = conn.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("module request deadline")
        upstream_socket.settimeout(min(READ_S, remaining))
        headers = {k: handler.headers[k] for k in ("Accept", "Content-Type", "Last-Event-ID", "If-None-Match")
                   if handler.headers.get(k)}
        headers.update({"X-Viewer-User": user, "X-Viewer-Can-Act": "1" if can_act(handler.headers) else "0"})
        conn.request(handler.command, module.config["base"] + "/" + rest, body=body, headers=headers)
        response = conn.getresponse()
        handler.send_response(response.status)
        sent = True
        for key in ("Content-Type", "Cache-Control", "ETag"):
            if response.getheader(key):
                handler.send_header(key, response.getheader(key))
        handler.send_header("X-Accel-Buffering", "no")
        handler._no_frame()
        handler.end_headers()
        while True:
            chunk = response.read1(65536)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
        if handler.command == "POST":
            actions.audit(user, "module_post", name, str(response.status))
    except (OSError, http.client.HTTPException):
        if handler.command == "POST":
            actions.audit(user, "module_post", name, "upstream-error")
        if not sent:
            return handler._reply(502, {"error": "module upstream unavailable"})
    finally:
        if timer:
            timer.cancel()
        if response:
            response.close()
        if conn:
            conn.close()
        handler.close_connection = True
        module.slots.release()
