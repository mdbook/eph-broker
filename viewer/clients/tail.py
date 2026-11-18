"""tail.py: follow a local transcript and re-push its bounded tail after viewer reloads.

Reads only the explicitly supplied regular file. Never sends raw Codex payloads,
partial lines or records larger than the protocol budget. No checkpoint on disk:
starting again backfills the file, and a running process retains its replay ring.
"""
import http.client
import json
import os
import stat
import time
import uuid
from collections import deque

from .. import transcripts

READ_BYTES = 1 << 20
BACKFILL_BYTES = 8 << 20
ANCHOR_BYTES = 4096


class Follower:
    def __init__(self, path, kind, lines=2000):
        self.path, self.kind = path, kind
        self.lines = deque(maxlen=lines)
        self.calls = deque(maxlen=500)
        self.file, self.identity = None, None
        self.anchor = b''
        self.partial, self.discard = b'', False
        self.source, self.sequence = uuid.uuid4().hex, 0

    def close(self):
        if self.file:
            self.file.close()
            self.file = None

    def poll(self):
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return 0  # The rename/create window must not lose the old cursor.
        identity = info.st_dev, info.st_ino
        if (self.file is None or identity != self.identity or info.st_size < self.file.tell()
                or os.pread(self.file.fileno(), len(self.anchor), self.file.tell() - len(self.anchor))
                != self.anchor):
            # Size alone misses copy-truncate files that regrow between polls.
            self.close()
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK)
            except FileNotFoundError:
                return 0  # Rotation raced the stat, retry on the next poll.
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                os.close(fd)
                raise ValueError('--transcript must name a regular file')
            self.file = os.fdopen(fd, 'rb', buffering=0)
            self.anchor = b''
            self.identity = info.st_dev, info.st_ino
            self.source, self.sequence = uuid.uuid4().hex, 0
            self.lines.clear()
            self.calls.clear()
            self.partial, self.discard = b'', False
            offset = max(0, info.st_size - BACKFILL_BYTES)
            self.file.seek(offset)
            self.discard = offset > 0
        chunk = self.file.read(READ_BYTES)
        self.anchor = (self.anchor + chunk)[-ANCHOR_BYTES:]
        data = self.partial + chunk
        parts = data.split(b'\n')
        self.partial = parts.pop()
        added = 0
        for raw in parts:
            if self.discard:
                self.discard = False
                continue
            if len(raw) > READ_BYTES:
                continue
            line = transcripts.normalize(raw, self.kind, self.calls)
            if line is not None:
                self.lines.append((self.sequence, line))
                self.sequence += 1
                added += 1
        if len(self.partial) > READ_BYTES:
            self.partial, self.discard = b'', True
        return added


class Pusher:
    def __init__(self, client, follower):
        self.client, self.follower = client, follower
        self.epoch, self.source, self.acked = None, None, -1

    def step(self):
        """At most one size-bounded signed batch per step, including epoch probes."""
        follower = self.follower
        if self.source != follower.source:
            self.source, self.acked = follower.source, -1
        if follower.lines and self.acked >= 0 and self.acked < follower.lines[0][0] - 1:
            # Offline longer than the ring: replace rather than promise a gapless
            # sequence we no longer hold. SSE clients get a reset.
            follower.source = uuid.uuid4().hex
            self.source, self.acked = follower.source, -1
        pending = [(n, line) for n, line in follower.lines if n > self.acked]
        data = dict(epoch=self.epoch, source=self.source,
                    start=pending[0][0] if pending else self.acked + 1, lines=[])
        for _, line in pending[:transcripts.BATCH_LINES]:
            trial = dict(data, lines=data['lines'] + [line])
            if len(json.dumps(trial, separators=(',', ':')).encode()) > transcripts.BATCH_BYTES:
                break
            data = trial
        result = self.client.call('events', data)
        self.epoch = result['epoch']
        if result['replay']:
            follower.source = uuid.uuid4().hex
            self.source, self.acked = follower.source, -1
        elif data['lines']:
            self.acked = data['start'] + len(data['lines']) - 1


def follow(client, path, alive, interval=0.5, lines=2000):
    from .agentdir import APIError
    registration = client.state.get('registration', {})
    kind = registration.get('kind')
    if kind not in ('claude-code', 'codex') or 'tail' not in registration.get('caps', []):
        raise ValueError('register --kind claude-code or codex --caps tail first')
    try:
        transcripts.require_shared()
    except ImportError:
        raise ValueError('transcript tail needs the core eph-broker package or a source checkout') from None
    follower = Follower(path, kind, lines)
    pusher = Pusher(client, follower)
    try:
        while alive():
            follower.poll()
            try:
                pusher.step()  # Events also refresh presence, no separate beat process.
            except (OSError, http.client.HTTPException, APIError) as exc:
                if isinstance(exc, APIError):
                    if exc.status not in (408, 429) and not 500 <= exc.status < 600:
                        raise
                # Keep credentials and the unacked ring. A new timestamp signs the
                # retry; source + sequence make an ambiguous HTTP reply idempotent.
            time.sleep(interval)
    finally:
        follower.close()
