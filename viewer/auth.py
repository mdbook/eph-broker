"""Stateless enrollment and per-agent request authentication."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
from pathlib import Path


def private_read(path):
    with open(path, 'rb') as source:
        mode = os.fstat(source.fileno()).st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
            raise ValueError('credential file must be a regular 0600 file')
        return source.read()


def private_dir(path):
    """mkdir -p that leaves every level it creates 0700, whatever the umask (module hosts use 0177)."""
    path = Path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for level in reversed(missing):
        try:
            level.mkdir(mode=0o700)
        except FileExistsError:
            continue
        os.chmod(str(level), 0o700)


def private_write(path, data):
    """Atomic replacement, including when the destination already exists."""
    path = Path(path)
    tmp = path.with_name(path.name + '.' + secrets.token_hex(8))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink()


def encode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip('=')


def decode(data):
    return base64.b64decode(data + '=' * (-len(data) % 4), altchars=b'-_', validate=True)


def name_ok(name):
    return isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name)


def digest(key, message):
    return hmac.new(key, message.encode(), hashlib.sha256).hexdigest()


def mint(key, name, ttl=600, clock=time.time):
    if not name_ok(name) or not 1 <= ttl <= 86400:
        raise ValueError('invalid name or enrollment TTL (1..86400 seconds)')
    payload = encode(json.dumps({'name': name, 'jti': secrets.token_hex(16),
                                 'exp': int(clock()) + ttl}, separators=(',', ':')).encode())
    return payload + '.' + digest(key, 'enroll|' + payload)


def verify_token(key, token, name, now):
    try:
        if not isinstance(token, str):
            raise ValueError()
        payload, signature = token.split('.')
        if not hmac.compare_digest(signature, digest(key, 'enroll|' + payload)):
            raise ValueError()
        claims = json.loads(decode(payload))
        if (not name_ok(name) or claims['name'] != name or
                not isinstance(claims['jti'], str) or len(claims['jti']) != 32 or
                type(claims['exp']) is not int or claims['exp'] <= now):
            raise ValueError()
        return claims
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise ValueError('invalid, expired, or wrong-name enrollment token') from None


def make_kid(name, enrollment_id):
    return encode(json.dumps([name, enrollment_id], separators=(',', ':')).encode())


def kid_name(kid):
    try:
        name, enrollment_id = json.loads(decode(kid))
        if not name_ok(name) or not isinstance(enrollment_id, str) or not re.fullmatch(r'[0-9a-f]{32}', enrollment_id):
            raise ValueError()
        if make_kid(name, enrollment_id) != kid:
            raise ValueError()  # No alternate encoding may bypass revocation or replay state.
        return name
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('invalid kid') from None


def agent_key(server_key, kid):
    name = kid_name(kid)
    enrollment_id = json.loads(decode(kid))[1]
    return hmac.new(server_key, (name + '|' + enrollment_id).encode(), hashlib.sha256).digest()


def signature(key, method, path, ts, body):
    return digest(key, '|'.join((method, path, ts, hashlib.sha256(body).hexdigest())))
