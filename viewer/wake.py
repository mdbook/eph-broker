"""wake.py: optional, operator-configured wake channels for idle directory agents.

No broker imports or credentials are needed for presence-only installations. A successful
DM means sent, not consumed. Only the enrolled agent can acknowledge consumption.
"""
import json
import logging
import os
import re
import subprocess

from . import steer

log = logging.getLogger('eph-broker.viewer.wake')


def channels():
    """Administrator-owned name -> channel map. Agents cannot select arbitrary DM recipients."""
    try:
        value = json.loads(os.environ.get('AGENTDIR_WAKE_CHANNELS', '{}'))
        if not isinstance(value, dict):
            raise ValueError('expected an object keyed by agent name')
        for name, channel in value.items():
            if channel is None or channel == 'none':
                continue
            if (not isinstance(channel, dict) or channel.get('kind') != 'zulip-dm' or
                    not isinstance(channel.get('handle'), str) or
                    not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+@:-]{0,253}', channel['handle'])):
                raise ValueError('expected none or {kind: zulip-dm, handle: recipient}')
        return {name: c for name, c in value.items() if isinstance(c, dict)}
    except ValueError as exc:
        log.error('AGENTDIR_WAKE_CHANNELS: %s; wake channels disabled', exc)
        return {}


def frame(message):
    return steer.frame(message['text'], message['doer'], message['id'], message['ns'], untrusted=True)


def send(channel, message):
    """Bounded argv-only transport. The credentials path is passed through, never opened here."""
    executable = os.environ.get('AGENTDIR_WAKE_ZBUS', '')
    if not executable or channel.get('kind') != 'zulip-dm':
        return False
    env = os.environ.copy()
    credentials = os.environ.get('AGENTDIR_WAKE_ZBUS_ENV', '')
    if credentials:
        env['ZBUS_ENV'] = credentials
    try:
        result = subprocess.run([executable, 'dm', channel['handle'], '--stdin'], input=frame(message),
                                text=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=5)
        if result.returncode == 0:
            return True
        log.warning('wake: DM transport exit=%s', result.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning('wake: DM transport failed (%s)', type(exc).__name__)
    return False


def consumed_ids(lines):
    """Receipt evidence from signed user-role transcript records, never tool results or assistant echoes.

    IDs must be in the forwarded metadata, not in the untrusted message body. Client projections
    carry only these IDs in wake_receipts when canonical text parts are discarded. The directory
    additionally checks that each ID belongs to this enrollment and was sent through a wake channel.
    """
    result = set()
    for line in lines:
        if line is None:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('type') != 'user':
            continue
        receipts = record.get('wake_receipts', [])
        if isinstance(receipts, list):
            result.update(mid for mid in receipts if isinstance(mid, str) and re.fullmatch(r'[a-f0-9]{16}', mid))
        message = record.get('message')
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text']
        else:
            continue
        for text in texts:
            if not isinstance(text, str):
                continue
            header, separator, _ = text.partition('<<<BEGIN UNTRUSTED COMMENT>>>')
            if separator and steer.FORWARD_PREFACE in header:
                result.update(re.findall(r'^Dashboard message id: ([a-f0-9]{16})\.', header, re.M))
    return result

