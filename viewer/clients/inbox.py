"""inbox.py: private, retry-safe local delivery for the PostToolUse hook.

The credential state retains messages until an output-confirmed delivered record is reported. A hook
that dies after claiming leaves .delivered; after a grace period we rename it back to
.msg. The hook then either loses its claim or produces at most a duplicate, never a loss.
Run one beat process per state file and give each agent its own steer directory.
"""
import json
import os
import re
import stat
import time
from pathlib import Path

from .. import steer

CLAIM_GRACE = 30
FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC


def private_directory(path):
    """Anchor every component, including existing parents, without following symlinks."""
    path = Path(os.path.abspath(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | FLAGS)
    try:
        for component in path.parts[1:]:
            created = False
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
                created = True
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=fd)
            if created:
                os.fchmod(child, 0o700)  # a restrictive umask may remove directory search bits
            os.close(fd)
            fd = child
        os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_log(fd):
    try:
        source = os.open('log.jsonl', os.O_RDONLY | os.O_NONBLOCK | FLAGS, dir_fd=fd)
    except FileNotFoundError:
        return set()
    with os.fdopen(source, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('steer log must be a regular file')
        offset = max(0, info.st_size - steer.LOG_MAX_BYTES)
        stream.seek(offset)
        if offset:
            stream.readline()  # discard a partial first record
        data = stream.read(steer.LOG_MAX_BYTES)
    delivered = set()
    for line in data.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if (isinstance(record, dict) and record.get('state') == 'delivered' and
                record.get('output_confirmed') is True and
                isinstance(record.get('id'), str)):
            delivered.add(record['id'])
    return delivered


def info_at(fd, name):
    try:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('steer inbox entries must be regular files')
    return info


def sync(path, messages):
    """Ensure durable pending messages have an inbox file; return states for the next beat."""
    root = private_directory(path)
    inbox = None
    try:
        try:
            os.mkdir('inbox', 0o700, dir_fd=root)
        except FileExistsError:
            pass
        inbox = os.open('inbox', os.O_RDONLY | os.O_DIRECTORY | FLAGS, dir_fd=root)
        os.fchmod(inbox, 0o700)
        for message in messages.values():
            mid, ns = message['id'], message['ns']
            if not re.fullmatch(r'[a-f0-9]{16}', mid) or not re.fullmatch(r'[0-9]{1,24}', ns):
                raise ValueError('invalid steering message id')
            if message.get('state') == 'delivered':
                continue
            name = ns + '-' + mid + '.msg'
            claim = name[:-4] + '.delivered'
            queued, claimed = info_at(inbox, name), info_at(inbox, claim)
            if claimed:
                if time.time() - message.setdefault('claim_seen', time.time()) >= CLAIM_GRACE:
                    os.rename(claim, name, src_dir_fd=inbox, dst_dir_fd=inbox)
                    os.fsync(inbox)
                    message.pop('claim_seen', None)
            elif not queued:
                # A normal hook flushes output, then logs before unlinking. Re-read after observing no file
                # so a hook racing our first log read cannot cause a second delivery.
                if mid in read_log(root):
                    message['state'] = 'delivered'
                    continue
                tmp = '.' + name
                if info_at(inbox, tmp):
                    os.unlink(tmp, dir_fd=inbox)  # incomplete atomic write from a crashed beat
                target = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FLAGS, 0o600, dir_fd=inbox)
                try:
                    os.fchmod(target, 0o600)
                    with os.fdopen(target, 'w', encoding='utf-8') as stream:
                        json.dump({'id': mid, 'doer': str(message.get('doer') or '')[:64],
                                   'text': str(message['text'])}, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.rename(tmp, name, src_dir_fd=inbox, dst_dir_fd=inbox)
                    os.fsync(inbox)
                finally:
                    try:
                        os.unlink(tmp, dir_fd=inbox)
                    except FileNotFoundError:
                        pass
            message['state'] = 'queued'
    finally:
        if inbox is not None:
            os.close(inbox)
        os.close(root)
