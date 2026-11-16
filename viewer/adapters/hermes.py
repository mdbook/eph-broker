"""Hermes view: an agent's view sidecar as a JSON/SSE data API.

The browser never loads sidecar HTML or JavaScript, so a compromised agent cannot run
script in the broker's origin. Every connection goes through allowlist.open, which
re-checks and pins the registered address. Responses are re-typed here: JSON routes
always answer JSON, image routes only raster bytes, the stream only text/event-stream.
"""
import base64
import http.client
import json
import os
import queue
import re
import threading
import time
from urllib.parse import urlsplit

from .. import auth

READ = frozenset({'cfg', 'rows', 'events', 'earlier', 'img', 'ctx', 'tools', 'search', 'browse',
                  'skill', 'queue', 'cmds', 'brand'})
ACT = frozenset({'say', 'stop', 'steer', 'cmd'})
RASTER = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
BRAND = RASTER + ('image/svg+xml',)
JSON_MAX = 8 << 20
IMAGE_MAX = 20 << 20
BRAND_MAX = 1 << 20
# The broker module host drops a response that is idle for 10 s; the sidecar heartbeats every 15 s.
KEEPALIVE_S = 5
# Longest a non-stream reply may take in total (the /cmd upstream timeout plus slack).
ANSWER_MAX_S = 130
HEADER = re.compile(r'[A-Za-z0-9-]{1,64}')


class Reply:
    """What the server writes back: JSON or bytes in body, or an SSE chunk iterator in stream."""

    def __init__(self, status, content_type='application/json', body=b'', stream=None, headers=None):
        self.status, self.content_type, self.body, self.stream = status, content_type, body, stream
        self.headers = headers or {}


def error(status, message):
    return Reply(status, body=json.dumps({'error': message}).encode())


def body_limit(route):
    return 24 << 20 if route == 'say' else 1 << 20  # /say carries up to four images


def settings(env=None):
    env = os.environ if env is None else env
    found = {'auth_header': env.get('AGENTDIR_HERMES_AUTH_HEADER', 'Remote-User'),
             'secret_header': env.get('AGENTDIR_HERMES_PROXY_SECRET_HEADER', 'X-View-Proxy-Secret'),
             'secret_file': env.get('AGENTDIR_HERMES_PROXY_SECRET_FILE', '')}
    for key in ('auth_header', 'secret_header'):
        if not HEADER.fullmatch(found[key]):
            raise ValueError('AGENTDIR_HERMES_%s must be a header name' % key.upper())
    return found


def target(view_url, route, query):
    parts = urlsplit(view_url)
    base = parts.path if parts.path.endswith('/') else parts.path + '/'
    url = parts.scheme + '://' + parts.netloc + base + route
    return (url + '?' + query) if query else url, parts


def upstream_headers(config, parts, request):
    headers = {'Host': parts.netloc, config['auth_header']: request['user']}
    if config['secret_file']:
        # Read per request so rotation needs no restart. Never logged or returned.
        secret = auth.private_read(config['secret_file']).rstrip(b'\r\n')
        if not secret:
            raise ValueError('empty proxy secret file')
        headers[config['secret_header']] = secret
    for key in ('Accept', 'Last-Event-ID', 'If-None-Match'):
        if request['headers'].get(key):
            headers[key] = request['headers'][key]
    if request['method'] == 'POST':
        # The broker already enforced same-origin for the browser; the sidecar checks Origin against Host.
        headers.update({'Content-Type': 'application/json', 'Origin': parts.scheme + '://' + parts.netloc})
    return headers


def view(entry, allowlist, request, env=None):
    """request: route, method, query, body, user, headers (the copies the module host forwards)."""
    route = request['route']
    if route not in (ACT if request['method'] == 'POST' else READ):
        return error(404, 'not found')
    try:
        config = settings(env)
        url, parts = target(entry['view_url'], route, request['query'])
        headers = upstream_headers(config, parts, request)
    except (OSError, ValueError):
        return error(502, 'view proxy is misconfigured')
    if route == 'events':
        return stream(allowlist, url, headers)
    return answer(route, allowlist, url, headers, request)


def opened(allowlist, url, headers, request, timeout):
    return allowlist.open(url, request['method'], headers, request['body'] or None, timeout)


def refused(status):
    if status in (401, 421):
        return error(502, 'the agent view refused the proxy (HTTP %d): check its auth header, '
                          'proxy secret and host names' % status)
    return None


def answer(route, allowlist, url, headers, request):
    # /cmd can wait 90 s on the agent. Answer normally if the sidecar is quick; otherwise
    # commit to 200 and trickle JSON-legal whitespace until it replies, then send its body
    # with any failure status inside it as "status".
    done = queue.Queue(maxsize=1)

    timeout = 120 if route == 'cmd' else 30
    deadline = time.monotonic() + ANSWER_MAX_S

    def run():
        try:
            response, close = opened(allowlist, url, headers, request, timeout)
            try:
                done.put(read(route, response, time.monotonic() + timeout))
            finally:
                close()
        except (OSError, ValueError, http.client.HTTPException) as exc:
            done.put(error(502, 'view connection failed') if not str(exc).startswith('view')
                     else error(502, str(exc)))
        except Exception:  # noqa: BLE001 (RecursionError on deep JSON, anything else): never leave done empty
            done.put(error(502, 'the agent view sent a reply the broker could not read'))

    threading.Thread(target=run, daemon=True).start()
    try:
        # Image bytes cannot be trickled; they must arrive inside the module host's idle window.
        return done.get(timeout=35 if route in ('img', 'brand') else KEEPALIVE_S)
    except queue.Empty:
        if route in ('img', 'brand'):
            return error(504, 'the agent view did not answer')

    def slow():
        while True:
            try:
                reply = done.get(timeout=KEEPALIVE_S)
            except queue.Empty:
                if time.monotonic() < deadline:
                    yield b' '
                    continue
                reply = error(504, 'the agent view did not answer')
            body = reply.body
            if reply.status >= 400 or reply.content_type != 'application/json':
                data = json.loads(body) if reply.content_type == 'application/json' else {}
                data = data if isinstance(data, dict) else {'error': 'unexpected reply'}
                data.setdefault('error', 'HTTP %d' % reply.status)
                body = json.dumps(dict(data, status=reply.status)).encode()
            yield body
            return

    return Reply(200, stream=slow())


def read(route, response, deadline):
    status, kind = response.status, (response.getheader('Content-Type') or '').split(';')[0].strip().lower()
    bad = refused(status)
    if bad:
        return bad
    if route == 'img':
        if status == 304:
            return Reply(304, kind or 'application/octet-stream', headers=cache(response))
        if status == 200:
            if kind not in RASTER:
                return error(502, 'the agent view sent a non-raster image')
            data = limited(response, IMAGE_MAX, deadline)
            return Reply(200, kind, data, headers=cache(response))
    if route == 'brand' and status == 200:
        # As a data URL: an SVG at a broker URL would be a script-capable document in our origin.
        if kind not in BRAND:
            return error(502, 'the agent view sent an unsupported brand image')
        data = limited(response, BRAND_MAX, deadline)
        url = 'data:%s;base64,%s' % (kind, base64.b64encode(data).decode())
        return Reply(200, body=json.dumps({'data_url': url}).encode())
    data = limited(response, JSON_MAX, deadline)
    try:
        json.loads(data)
    except ValueError:
        text = data[:300].decode('utf-8', 'replace').strip()
        if status < 400:
            return error(502, 'the agent view sent a non-JSON reply')
        return Reply(status, body=json.dumps({'error': text or 'HTTP %d' % status}).encode())
    return Reply(status, body=data)


def cache(response):
    # Images are per-user (behind auth), so never shared-cacheable whatever the sidecar says.
    found = {'Cache-Control': 'private, no-cache'}
    etag = response.getheader('ETag') or ''
    if etag and not re.search(r'[\r\n]', etag):
        found['ETag'] = etag
    return found


def limited(response, cap, deadline):
    # Chunked, with a total deadline: the socket timeout is per recv, so a dripping sidecar could otherwise hold a thread.
    parts, size = [], 0
    while True:
        if time.monotonic() > deadline:
            raise ValueError('view response took too long')
        chunk = response.read(min(65536, cap + 1 - size))
        if not chunk:
            return b''.join(parts)
        parts.append(chunk)
        size += len(chunk)
        if size > cap:
            raise ValueError('view response exceeds %d bytes' % cap)


def stream(allowlist, url, headers):
    try:
        # Longer than the sidecar's 15 s heartbeat: a silent sidecar ends the stream and the browser reconnects.
        response, close = allowlist.open(url, 'GET', headers, None, 45)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return error(502, str(exc) if str(exc).startswith('view') else 'view connection failed')
    kind = (response.getheader('Content-Type') or '').split(';')[0].strip().lower()
    if response.status != 200 or kind != 'text/event-stream':
        try:
            return refused(response.status) or error(502, 'the agent view stream failed (HTTP %d)' % response.status)
        finally:
            close()
    return Reply(200, 'text/event-stream', stream=relay(response, close))


def relay(response, close):
    lines, stopped = queue.Queue(maxsize=256), threading.Event()

    def put(item):
        while not stopped.is_set():
            try:
                return lines.put(item, timeout=1)
            except queue.Full:
                continue

    def pump():
        try:
            while not stopped.is_set():
                line = response.readline(1 << 20)
                if not line:
                    break
                put(line)
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            put(None)

    threading.Thread(target=pump, daemon=True).start()
    boundary = True
    try:
        while True:
            try:
                line = lines.get(timeout=KEEPALIVE_S)
            except queue.Empty:
                if boundary:  # A comment between frames only; never inside one.
                    yield b': keepalive\n\n'
                continue
            if line is None:
                return
            boundary = line in (b'\n', b'\r\n')
            yield line
    finally:
        stopped.set()
        close()
