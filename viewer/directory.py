"""In-memory presence with durable revocation and steering queues; clock injected for reaping."""
import hmac
import json
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import adapters, auth, transcripts, wake
from .allowlist import Allowlist
from .steer import TEXT_MAX_CHARS

MESSAGE_LIMIT = 32
TERMINAL = ('delivered', 'not_delivered')


class Refused(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Directory:
    def __init__(self, key, state_dir, allowlist=None, clock=time.time, wake_sender=None):
        if len(key) < 32:
            raise ValueError('enrollment key must contain at least 32 bytes')
        self.key, self.clock = key, clock
        self.wake_channels = wake.channels()
        self.wake_sender = wake_sender or wake.send
        self.allowlist = allowlist if allowlist is not None else Allowlist()
        self.lock = threading.RLock()
        self.entries, self.seen, self.timestamps = {}, {}, {}
        self.tails = {}
        self.tail_lines = transcripts.line_limit()
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
        self.steering_path = Path(state_dir) / 'steering.json'
        try:
            self.steering = json.loads(auth.private_read(self.steering_path))
            if not isinstance(self.steering, dict) or not all(
                    isinstance(k, str) and isinstance(v, list) and len(v) <= MESSAGE_LIMIT
                    for k, v in self.steering.items()):
                raise ValueError('invalid steering queue file')
        except FileNotFoundError:
            self.steering = {}

    def reap(self):
        with self.lock:
            now = self.clock()
            self.entries = {n: e for n, e in self.entries.items()
                            if now - e['last_beat'] < 3 * e['ttl']}
            self.tails = {n: t for n, t in self.tails.items() if n in self.entries}
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
            if path == '/v1/events' and len(body) > transcripts.BATCH_BYTES:
                raise Refused('transcript batch exceeds 64 KiB', 413)
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
                elif path == '/v1/events':
                    return self._events(kid, name, data)
                elif path == '/v1/beat':
                    entry = self.entries.get(name)
                    if entry is None or entry['kid'] != kid:
                        raise Refused('register before beating', 409)
                    if 'status' in data:
                        if data['status'] not in ('busy', 'idle'):
                            raise Refused('status must be busy or idle')
                        entry['status'] = data['status']
                    entry['last_beat'] = self.clock()
                    return self._beat_messages(entry, data)
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
                        self.tails.pop(name, None)
                    if kid in self.steering:
                        self._save_messages(kid, [])
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
        if 'status' in data and data['status'] not in ('busy', 'idle'):
            raise Refused('status must be busy or idle')
        if (data.get('name') != name or data.get('kind') not in adapters.KINDS or
                type(ttl) is not int or not 3 <= ttl <= 86400 or
                not isinstance(caps, list) or
                not all(c in ('view', 'steer', 'steer-pull', 'tail') for c in caps)):
            raise Refused('invalid name, kind, caps, or ttl (3..86400 seconds)')
        url = data.get('view_url')
        if 'tail' in caps and data['kind'] in ('claude-code', 'codex'):
            try:
                transcripts.require_shared()
            except ImportError:
                raise Refused('transcript tail needs the core eph-broker package or a source checkout', 503) from None
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
            messages = self.steering.get(kid, [])
            previous = self.entries.get(name)
            if previous and (previous['kind'] != data['kind'] or 'tail' not in caps):
                self.tails.pop(name, None)
            self.entries[name] = dict(name=name, kid=kid, kind=data['kind'], caps=caps,
                                      view_url=url, ttl=ttl,
                                      status=data.get('status', previous['status'] if previous else 'live'),
                                      last_beat=self.clock(), messages=messages, wake=self.wake_channels.get(name))
        return {'ok': True}

    def queue_message(self, name, text, doer):
        """Called only by the action-gated module listener. IDs belong to this registration, not the browser."""
        from . import steer
        with self.lock:
            self.reap()
            entry = self.entries.get(name)
            if entry is None:
                raise Refused('agent not found', 404)
            if 'steer-pull' not in entry['caps']:
                raise Refused('this agent does not accept pull steering', 403)
            if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX_CHARS:
                raise Refused('message must contain 1..4000 characters')
            messages = list(entry['messages'])
            if sum(m['state'] not in TERMINAL for m in messages) >= MESSAGE_LIMIT:
                raise Refused('agent message queue is full', 409)
            message = dict(id=steer.new_id(), ns=str(time.time_ns()), text=text,
                           doer=str(doer)[:64], state='pending')
            channel = entry['wake'] if entry['status'] == 'idle' else None
            if entry['status'] == 'idle':
                message['via'] = 'wake'
                if channel:
                    # Persist before sending; a fast consumption receipt can race the transport reply.
                    message['state'] = 'waking'
                else:
                    message.update(state='not_delivered', reason='agent idle, no wake channel')
            messages.append(message)
            # Keep a bounded recent history without evicting undelivered messages.
            while len(messages) > MESSAGE_LIMIT:
                messages.remove(next(m for m in messages if m['state'] in TERMINAL))
            self._save_messages(entry['kid'], messages)
            entry['messages'] = messages
            kid = entry['kid']
        # Never hold the directory lock across external I/O: beats and consumption receipts must progress.
        if channel:
            sent = self.wake_sender(channel, message)
            with self.lock:
                messages = [dict(m) for m in self.steering.get(kid, [])]
                for current in messages:
                    if current['id'] == message['id']:
                        if current['state'] == 'waking':
                            current['state'] = 'sent' if sent else 'not_delivered'
                            if not sent:
                                current['reason'] = 'wake channel failed'
                        message = current
                self._save_messages(kid, messages)
                current_entry = self.entries.get(name)
                if current_entry and current_entry['kid'] == kid:
                    current_entry['messages'] = messages
        return dict(ok=True, **{k: message[k] for k in ('id', 'state', 'via', 'reason') if k in message})

    def _save_messages(self, kid, messages):
        updated = dict(self.steering)
        if messages:
            updated[kid] = messages
        else:
            updated.pop(kid, None)
        auth.private_write(self.steering_path, json.dumps(updated).encode())
        self.steering = updated

    def _beat_messages(self, entry, data):
        if 'steer-pull' not in entry['caps']:
            return {'ok': True}
        reports = {}
        for field in ('steer_queued', 'steer_delivered'):
            ids = data.get(field, [])
            if (not isinstance(ids, list) or len(ids) > MESSAGE_LIMIT or
                    not all(isinstance(mid, str) and len(mid) <= 64 for mid in ids)):
                raise Refused('invalid steering acknowledgement')
            reports[field] = set(ids)
        messages = [dict(m) for m in entry['messages']]
        for message in messages:
            if (message['id'] in reports['steer_delivered'] and
                    (message['state'] != 'not_delivered' or message.get('reason') == 'wake channel failed')):
                message['state'] = 'delivered'
                message.pop('reason', None)
            elif message['id'] in reports['steer_queued'] and message['state'] == 'pending':
                message['state'] = 'queued'
        if messages != entry['messages']:
            self._save_messages(entry['kid'], messages)
            entry['messages'] = messages
        pending = [{k: m[k] for k in ('id', 'ns', 'text', 'doer')}
                   for m in entry['messages'] if m['state'] == 'pending']
        return dict(ok=True, steer=pending) if pending else {'ok': True}

    def _events(self, kid, name, data):
        entry = self.entries.get(name)
        if entry is None or entry['kid'] != kid:
            raise Refused('register before pushing events', 409)
        if 'tail' not in entry['caps'] or entry['kind'] not in ('claude-code', 'codex'):
            raise Refused('agent requires tail capability and a transcript harness', 403)
        lines, source, start = data.get('lines'), data.get('source'), data.get('start')
        if (not isinstance(lines, list) or len(lines) > transcripts.BATCH_LINES or
                not all(isinstance(s, str) and len(s.encode()) <= transcripts.LINE_BYTES for s in lines) or
                not isinstance(source, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', source) or
                type(start) is not int or not 0 <= start <= 2 ** 53):
            raise Refused('invalid transcript source, start or lines')
        tail = self.tails.setdefault(name, transcripts.Buffer(self.tail_lines))
        if not tail.admit(self.clock()):
            raise Refused('transcript rate limit (4 batches/s, burst 8)', 429)
        if data.get('epoch') != tail.epoch:
            return dict(ok=True, epoch=tail.epoch, replay=True)
        normalized = [transcripts.normalize(line, entry['kind']) for line in lines]
        if not tail.append(source, start, normalized):
            return dict(ok=True, epoch=tail.epoch, replay=True)
        # Clients carry receipt IDs across projection. Native rollout messages also need
        # their normalized user-role form; raw canonical records remain supported.
        consumed = wake.consumed_ids(lines) | wake.consumed_ids(normalized)
        messages = [dict(m) for m in entry['messages']]
        for message in messages:
            if (message['id'] in consumed and (message['state'] in ('waking', 'sent') or
                    message.get('reason') == 'wake channel failed')):
                message['state'] = 'delivered'
                message.pop('reason', None)
        if messages != entry['messages']:
            self._save_messages(kid, messages)
            entry['messages'] = messages
        entry['last_beat'] = self.clock()
        return dict(ok=True, epoch=tail.epoch, replay=False)

    def snapshot(self):
        with self.lock:
            self.reap()
            now = self.clock()
            result = []
            for entry in sorted(self.entries.values(), key=lambda e: e['name']):
                public = {k: v for k, v in entry.items() if k not in ('kid', 'view_url', 'messages')}
                public['steer_messages'] = [{k: m[k] for k in ('id', 'state', 'via', 'reason') if k in m}
                                            for m in entry['messages']]
                public['age'] = max(0, now - entry['last_beat'])
                if public['age'] >= entry['ttl']:
                    public['status'] = 'grey'
                adapter = adapters.get(entry['kind'])
                public['can_view'] = bool(entry['view_url'] and 'view' in entry['caps'] and
                                          callable(getattr(adapter, 'view', None)))
                public['can_steer'] = 'steer' in entry['caps']
                public['can_steer_pull'] = 'steer-pull' in entry['caps']
                public['can_tail'] = 'tail' in entry['caps'] and entry['kind'] in ('claude-code', 'codex')
                result.append(public)
            return result
