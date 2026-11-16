"""agentdir: keep a Hermes gateway listed in an eph-broker agent directory.

On load it starts one daemon thread that enrolls (only when no key is stored), registers, and then
sends a signed beat every ttl/3. ``pre_llm_call`` marks the agent busy and ``post_llm_call`` idle,
each sending a beat at once. Unload removes the entry without revoking the key, so the next gateway
start reuses ``$HERMES_HOME/agentdir.key``. The hooks only set a flag; every network call runs on the
thread, and no error ever reaches the gateway: transient failures retry with backoff and are logged
once per change, a refused key stops the thread with one clear error.

Enable it in ``config.yaml`` (plugins are opt-in)::

    plugins:
      enabled:
        - agentdir

Settings (environment first, then this plugin's config): ``AGENTDIR_URL`` (default
``http://127.0.0.1:8790``), ``AGENTDIR_NAME`` (default the host name), ``AGENTDIR_TTL`` (default 30),
``AGENTDIR_VIEW_URL`` (optional, a view sidecar that serves both the ``view`` and ``steer`` caps), and the one-time token in
``AGENTDIR_ENROLL_TOKEN`` or ``AGENTDIR_ENROLL_TOKEN_FILE``. The token is only read when no key is
stored. Mint one with ``python3 -m viewer.clients.agentdir mint --name NAME``.

Stdlib only. The signing below is a copy of ``viewer/auth.py`` (the plugin is installed on its own,
without the viewer package); the tests check both produce the same signature.
"""
import atexit
import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

KEY_FILE = 'agentdir.key'
DEFAULT_URL = 'http://127.0.0.1:8790'
MAX_BACKOFF = 60


def signature(key, method, path, ts, body):
    message = '|'.join((method, path, ts, hashlib.sha256(body).hexdigest()))
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def b64decode(data):
    return base64.b64decode(data + '=' * (-len(data) % 4), altchars=b'-_', validate=True)


class Refused(Exception):
    """The directory answered with a status that retrying cannot fix."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_post(url, body, headers):
    """POST JSON; returns the decoded reply. Ambient proxies and redirects are ignored."""
    request = urllib.request.Request(url, body, headers, method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode(errors='replace')
        if exc.code in (408, 429) or exc.code >= 500:
            raise OSError('directory busy (%s)' % exc.code) from None
        raise Refused(exc.code, 'directory refused (%s): %s' % (exc.code, detail)) from None


def _hermes_home():
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.path.expanduser(os.environ.get('HERMES_HOME') or '~/.hermes')


def _read_private(path):
    with open(path, 'rb') as source:
        if os.fstat(source.fileno()).st_mode & 0o077:
            raise ValueError('%s must not be readable by group or others' % path)
        return source.read()


def _write_private(path, data):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def settings(ctx=None):
    def get(env, key, default=None):
        value = os.environ.get(env, '').strip()
        if value:
            return value
        if ctx is not None:
            try:
                value = ctx.get_config(key)
            except Exception:
                value = None
            if value not in (None, ''):
                return value
        return default
    try:
        ttl = int(get('AGENTDIR_TTL', 'ttl', 30))
    except (TypeError, ValueError):
        ttl = 30
    return dict(url=str(get('AGENTDIR_URL', 'url', DEFAULT_URL)).rstrip('/'),
                name=str(get('AGENTDIR_NAME', 'name', socket.gethostname())),
                ttl=min(max(ttl, 3), 86400), view_url=get('AGENTDIR_VIEW_URL', 'view_url'),
                token=os.environ.get('AGENTDIR_ENROLL_TOKEN', '').strip() or None,
                token_file=get('AGENTDIR_ENROLL_TOKEN_FILE', 'enroll_token_file'),
                key_file=os.path.join(_hermes_home(), KEY_FILE))


class Agent:
    """Directory client state plus the beat loop. ``post`` and ``clock`` are seams for tests."""

    def __init__(self, config, post=http_post, clock=time.time_ns):
        self.config, self.post, self.clock = config, post, clock
        self.status = 'idle'
        self.credentials = None
        self.registered = False
        self.last_us = 0
        self.wake, self.stopping = threading.Event(), threading.Event()
        self.thread = None
        self._logged = None

    # Hooks: never block, never raise.
    def set_status(self, status, **_ignored):
        if status != self.status:
            self.status = status
            self.wake.set()

    def busy(self, **_ignored):
        self.set_status('busy')

    def idle(self, **_ignored):
        self.set_status('idle')

    # Protocol.
    def call(self, action, data):
        path = '/v1/' + action
        body = json.dumps(data, separators=(',', ':')).encode()
        headers = {'Content-Type': 'application/json'}
        if action != 'enroll':
            self.last_us = max(self.clock() // 1000, self.last_us + 1)
            ts = '%d.%06d' % divmod(self.last_us, 1000000)
            key = b64decode(self.credentials['key'])
            headers.update({'X-Agentdir-Kid': self.credentials['kid'], 'X-Agentdir-Ts': ts,
                            'X-Agentdir-Sig': signature(key, 'POST', path, ts, body)})
        return self.post(self.config['url'] + path, body, headers)

    def load_or_enroll(self):
        path = self.config['key_file']
        try:
            stored = json.loads(_read_private(path))
            if stored.get('name') != self.config['name']:
                raise Refused(0, '%s belongs to %r, not %r; remove it to enroll again'
                              % (path, stored.get('name'), self.config['name']))
            self.credentials = stored
            return
        except FileNotFoundError:
            pass
        token = self.config['token']
        if token is None and self.config['token_file']:
            token = _read_private(self.config['token_file']).decode().strip()
        if not token:
            raise Refused(0, 'no stored key and no AGENTDIR_ENROLL_TOKEN(_FILE); not registering')
        result = self.call('enroll', {'name': self.config['name'], 'token': token})
        self.credentials = {'name': self.config['name'], 'kid': result['kid'], 'key': result['key']}
        _write_private(path, json.dumps(self.credentials).encode())

    def register(self):
        data = {'name': self.config['name'], 'kind': 'hermes', 'caps': [], 'ttl': self.config['ttl']}
        if self.config['view_url']:
            data.update(caps=['view', 'steer'], view_url=self.config['view_url'])
        self.call('register', data)
        self.registered = True

    def step(self):
        """One round: make sure we are enrolled and registered, then beat. Raises on failure."""
        if self.credentials is None:
            self.load_or_enroll()
        if not self.registered:
            self.register()
        try:
            self.call('beat', {'status': self.status})
        except Refused as exc:
            if exc.status != 409:
                raise
            # The directory forgot us (restart or reap): register again, then beat.
            self.registered = False
            self.register()
            self.call('beat', {'status': self.status})

    def note(self, state, message, level=logging.INFO):
        """Log once per state change, so a long outage is one line, not one per retry."""
        if state != self._logged:
            self._logged = state
            logger.log(level, 'agentdir: %s', message)

    def run(self):
        interval = self.config['ttl'] / 3.0
        delay = 1.0
        while not self.stopping.is_set():
            self.wake.clear()  # before the round, so a status change during it beats again at once
            try:
                self.step()
            except Refused as exc:
                hint = '' if exc.status == 0 else (
                    ' (mint a new token and remove %s to recover)' % self.config['key_file'])
                self.note('refused', '%s; stopping%s' % (exc, hint), logging.ERROR)
                return
            except Exception as exc:  # OSError, timeouts, bad JSON: all transient for the gateway
                self.note('down', 'directory unreachable, retrying with backoff: %s' % exc,
                          logging.WARNING)
                self.wake.wait(min(delay, interval))
                delay = min(delay * 2, MAX_BACKOFF)
                continue
            self.note('up', 'listed as %s' % self.config['name'])
            delay = 1.0
            self.wake.wait(interval)
        if self.registered:
            try:
                self.call('deregister', {'revoke': False})
            except Exception as exc:
                logger.info('agentdir: deregister on shutdown failed (%s); the entry will grey out', exc)

    def start(self):
        self.thread = threading.Thread(target=self.run, name='agentdir', daemon=True)
        self.thread.start()

    def stop(self, timeout=5.0):
        self.stopping.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout)


def register(ctx):
    """Hermes entry point."""
    agent = Agent(settings(ctx))
    ctx.register_hook('pre_llm_call', agent.busy)
    ctx.register_hook('post_llm_call', agent.idle)
    agent.start()
    try:
        ctx.on_unload(agent.stop)
    except Exception:
        pass
    atexit.register(agent.stop, 2.0)  # a gateway exit without unload still leaves the list cleanly
    return agent
