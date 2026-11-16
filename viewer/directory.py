"""In-memory directory with durable revocation only; clock injected for reaping."""
import hmac
import json
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import adapters, auth
from .allowlist import Allowlist


class Refused(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Directory:
    def __init__(self, key, state_dir, allowlist=None, clock=time.time):
        if len(key) < 32:
            raise ValueError('enrollment key must contain at least 32 bytes')
        self.key, self.clock = key, clock
        self.allowlist = allowlist if allowlist is not None else Allowlist()
        self.lock = threading.RLock()
        self.entries, self.seen, self.timestamps = {}, {}, {}
        auth.private_dir(state_dir)
        self.revocation_path = Path(state_dir) / 'revoked.json'
        try:
            revoked = json.loads(auth.private_read(self.revocation_path))
            if not isinstance(revoked, list) or not all(isinstance(k, str) for k in revoked):
                raise ValueError('invalid revocation file')
            self.revoked = set(revoked)
        except FileNotFoundError:
            self.revoked = set()
        # Used token ids survive a restart so a captured token cannot re-derive its key.
        self.seen_path = Path(state_dir) / 'used-tokens.json'
        try:
            seen = json.loads(auth.private_read(self.seen_path))
            if not isinstance(seen, dict) or not all(
                    isinstance(j, str) and type(e) is int for j, e in seen.items()):
                raise ValueError('invalid used-token file')
            self.seen = seen
        except FileNotFoundError:
            pass

    def reap(self):
        with self.lock:
            now = self.clock()
            self.entries = {n: e for n, e in self.entries.items()
                            if now - e['last_beat'] < 3 * e['ttl']}
            self.seen = {j: exp for j, exp in self.seen.items() if exp > now}
            # Timestamps older than the skew window can no longer be replayed.
            self.timestamps = {k: ts for k, ts in self.timestamps.items() if ts >= now - 60}

    def enroll(self, data):
        claims = auth.verify_token(self.key, data.get('token', ''), data.get('name'), self.clock())
        self.reap()
        if claims['jti'] in self.seen:
            raise Refused('enrollment token already used', 401)
        kid = auth.make_kid(claims['name'], claims['jti'])
        if kid in self.revoked:
            raise Refused('revoked kid', 401)
        seen = dict(self.seen, **{claims['jti']: claims['exp']})
        auth.private_write(self.seen_path, json.dumps(seen, sort_keys=True).encode())
        self.seen = seen
        return {'kid': kid, 'key': auth.encode(auth.agent_key(self.key, kid))}

    def authenticate(self, path, body, headers):
        kid = headers.get('X-Agentdir-Kid', '')
        ts = headers.get('X-Agentdir-Ts', '')
        try:
            if not re.fullmatch(r'-?[0-9]{1,12}(?:\.[0-9]{1,9})?', ts):
                raise ValueError()
            stamp = Decimal(ts)
            if not stamp.is_finite() or abs(Decimal(str(self.clock())) - stamp) > 60:
                raise ValueError()
            if kid in self.revoked or stamp <= self.timestamps.get(kid, -1):
                raise ValueError()
            name = auth.kid_name(kid)
            expected = auth.signature(auth.agent_key(self.key, kid), 'POST', path, ts, body)
            if not hmac.compare_digest(expected, headers.get('X-Agentdir-Sig', '')):
                raise ValueError()
        except (ValueError, TypeError, InvalidOperation):
            raise Refused('bad signature, revoked kid, stale or replayed timestamp', 401) from None
        self.timestamps[kid] = stamp
        return kid, name

    def request(self, path, body, headers):
        """Single dispatch seam shared by HTTP and socket-free protocol tests."""
        try:
            data = json.loads(body)
            if not isinstance(data, dict):
                raise Refused('expected a JSON object')
            with self.lock:
                if path == '/v1/enroll':
                    return self.enroll(data)
                kid, name = self.authenticate(path, body, headers)
                self.reap()
                if path == '/v1/register':
                    stamp = self.timestamps[kid]
                elif path == '/v1/beat':
                    entry = self.entries.get(name)
                    if entry is None or entry['kid'] != kid:
                        raise Refused('register before beating', 409)
                    if 'status' in data:
                        if data['status'] not in ('busy', 'idle'):
                            raise Refused('status must be busy or idle')
                        entry['status'] = data['status']
                    entry['last_beat'] = self.clock()
                elif path == '/v1/deregister':
                    # {"revoke": false} only removes the entry, for agents that restart with their key.
                    if type(data.get('revoke', True)) is not bool:
                        raise Refused('revoke must be true or false')
                    if data.get('revoke', True):
                        revoked = self.revoked | {kid}
                        auth.private_write(self.revocation_path, json.dumps(sorted(revoked)).encode())
                        self.revoked = revoked
                    if name in self.entries and self.entries[name]['kid'] == kid:
                        del self.entries[name]
                else:
                    raise Refused('unknown agent endpoint', 404)
                if path != '/v1/register':
                    return {'ok': True}
            return self._register(kid, name, data, stamp)
        except Refused:
            raise
        except RecursionError:
            raise Refused('JSON nested too deeply') from None
        except (ValueError, TypeError, KeyError) as exc:
            raise Refused(str(exc)) from None

    def _register(self, kid, name, data, stamp):
        ttl = data.get('ttl', 60)
        caps = data.get('caps', [])
        if (data.get('name') != name or data.get('kind') not in adapters.KINDS or
                type(ttl) is not int or not 3 <= ttl <= 86400 or
                not isinstance(caps, list) or
                not all(c in ('view', 'steer', 'tail') for c in caps)):
            raise Refused('invalid name, kind, caps, or ttl (3..86400 seconds)')
        url = data.get('view_url')
        if url is not None:
            if not isinstance(url, str) or len(url) > 2048:
                raise Refused('invalid view_url')
            # DNS has its own deadline and must never hold the directory lock.
            self.allowlist.resolve(url)
        with self.lock:
            self.reap()
            if kid in self.revoked:
                raise Refused('revoked kid', 401)
            if self.timestamps.get(kid) != stamp:
                raise Refused('registration superseded by a newer request', 409)
            if name in self.entries and self.entries[name]['kid'] != kid:
                raise Refused('name is registered to another kid', 409)
            self.entries[name] = dict(name=name, kid=kid, kind=data['kind'], caps=caps,
                                      view_url=url, ttl=ttl, status='live', last_beat=self.clock())
        return {'ok': True}

    def snapshot(self):
        with self.lock:
            self.reap()
            now = self.clock()
            result = []
            for entry in sorted(self.entries.values(), key=lambda e: e['name']):
                public = {k: v for k, v in entry.items() if k not in ('kid', 'view_url')}
                public['age'] = max(0, now - entry['last_beat'])
                if public['age'] >= entry['ttl']:
                    public['status'] = 'grey'
                adapter = adapters.get(entry['kind'])
                public['can_view'] = bool(entry['view_url'] and 'view' in entry['caps'] and
                                          callable(getattr(adapter, 'view', None)))
                public['can_steer'] = public['can_view'] and 'steer' in entry['caps']
                result.append(public)
            return result
