"""transcripts.py: bounded, redacted transcript projection and in-memory SSE replay.

Only signed agents with tail capability can write. Buffers and rate budgets belong
to registrations, never disk. Epoch negotiation requests a re-push after a reload.
"""
import json
import os
import uuid
from collections import deque

from . import wake

BATCH_BYTES = 65536
LINE_BYTES = 24576
BATCH_LINES = 100


def require_shared():
    # The core RPM owns these pure modules. Presence-only installations of the
    # optional viewer still work without it; refuse tail registration clearly.
    import transcript_assets
    import transcript_codex
    import transcript_redaction


def _redact_secretish(text):
    from transcript_redaction import _redact_secretish as redact
    return redact(text)


def item_detail(item):
    from transcript_codex import item_detail as detail
    return detail(item)


def line_limit():
    value = int(os.environ.get('AGENTDIR_TAIL_LINES', '2000'))
    if not 1 <= value <= 10000:
        raise ValueError('AGENTDIR_TAIL_LINES must be 1..10000')
    return value


def _text(value, cap=4000):
    return _redact_secretish(value)[:cap] if isinstance(value, str) else ''


def _codex(record, calls):
    typ, payload = record.get('type'), record.get('payload')
    stamp = record.get('timestamp')
    if typ == 'response_item' and isinstance(payload, dict):
        typ = payload.get('type')
        if typ == 'message' and payload.get('role') in ('user', 'assistant'):
            content = payload.get('content', [])
            text = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict)
                             and p.get('type') in ('input_text', 'output_text') and isinstance(p.get('text'), str))
            return dict(type=payload['role'], timestamp=stamp, message={'content': _text(text)})
        if typ == 'reasoning':
            text = '\n'.join(p.get('text', '') for p in payload.get('summary', [])
                             if isinstance(p, dict) and isinstance(p.get('text'), str))
            return _view(dict(type='item.completed', item_type='reasoning', text=_text(text)), stamp)
        if typ in ('function_call', 'custom_tool_call'):
            name = payload.get('name', '')
            inp = {}
            if name in ('shell', 'shell_command', 'exec_command'):
                if isinstance(payload.get('call_id'), str):
                    calls.append(payload['call_id'])
                try:
                    args = json.loads(payload.get('arguments', '{}'))
                    command = args.get('command', args.get('cmd'))
                    if isinstance(command, list):
                        command = ' '.join(str(s) for s in command)
                    inp = {'command': _text(command, 2000)}
                except (ValueError, TypeError, AttributeError):
                    pass
            return dict(type='assistant', timestamp=stamp, message={'content': [dict(
                type='tool_use', id=payload.get('call_id'), name='Bash' if inp else _text(name, 160), input=inp)]})
        if typ == 'function_call_output':
            if payload.get('call_id') not in calls:
                return None  # Never forward MCP results.
            output = _redact_secretish(payload.get('output', ''))
            return dict(type='user', timestamp=stamp, message={'content': [dict(
                type='tool_result', tool_use_id=payload.get('call_id'), content=output[-3000:])]})
        return None
    if typ == 'event_msg':
        # Messages/reasoning also appear in response_item; do not draw them twice.
        if isinstance(payload, dict) and payload.get('type') == 'exec_command_end':
            if payload.get('call_id') in calls:
                return None  # The tool_use/tool_result pair already owns this command card.
            item = dict(type='command_execution', command=payload.get('command'),
                        aggregated_output=payload.get('aggregated_output'), exit_code=payload.get('exit_code'))
            return _view(dict(type='item.completed', item_type='command_execution', **item_detail(item)), stamp)
        return None
    if typ in ('item.completed', 'item.started', 'item.updated') and isinstance(record.get('item'), dict):
        item = record['item']
        detail = item_detail(item) if typ == 'item.completed' else {}
        return _view(dict(type=typ, item_type=_text(item.get('type'), 80), **detail), stamp)
    if typ == 'turn.completed':
        usage = record.get('usage') or {}
        usage = {k: usage[k] for k in ('input_tokens', 'output_tokens')
                 if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
        return _view(dict(type=typ, usage=usage), stamp)
    if typ in ('thread.started', 'turn.started', 'turn.failed', 'error'):
        return _view(dict(type=typ, message=_text(record.get('message') or
                     (record.get('error') or {}).get('message'))), stamp)
    if typ == 'system' and record.get('subtype') == 'codex':
        view = json.loads(record.get('content', '{}'))
        # Already projected by this client. Keep only the broker's display fields.
        if not isinstance(view, dict):
            return None
        keys = ('type', 'item_type', 'text', 'command', 'exit_code', 'output', 'output_lines',
                'paths', 'files', 'tool', 'todo', 'query', 'message', 'usage')
        return _view({k: view[k] for k in keys if k in view}, stamp)
    if typ in ('assistant', 'user'):
        # Canonical rollout message/tool cards. Only shell commands, never MCP args.
        content = (record.get('message') or {}).get('content')
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get('type') == 'tool_use':
                    parts.append(dict(type='tool_use', id=p.get('id'), name=_text(p.get('name'), 160),
                                      input={'command': _text((p.get('input') or {}).get('command'), 2000)}))
                elif isinstance(p, dict) and p.get('type') == 'tool_result':
                    parts.append(dict(type='tool_result', tool_use_id=p.get('tool_use_id'),
                                      content=_redact_secretish(p.get('content', ''))[-3000:]))
            content = parts
        elif isinstance(content, str):
            content = _text(content)
        else:
            return None
        return dict(type=typ, timestamp=stamp, message={'content': content})
    return None


def _view(view, stamp):
    return dict(type='system', subtype='codex', timestamp=stamp, content=json.dumps(view))


def normalize(line, kind, calls=None):
    """One raw Claude jsonl, Codex rollout or Codex --json record to a safe line."""
    try:
        record = json.loads(line)
        if not isinstance(record, dict):
            return None
        if kind == 'codex':
            # Canonical text parts are deliberately not displayed. Preserve only receipt IDs
            # before projection, including on the second pass at the directory.
            receipts = wake.consumed_ids([line])
            record = _codex(record, calls if calls is not None else deque(maxlen=500))
            if record is not None and record.get('type') == 'user' and receipts:
                record['wake_receipts'] = sorted(receipts)
        if record is None:
            return None
        result = _redact_secretish(json.dumps(record))
        if len(result.encode()) > LINE_BYTES:
            return json.dumps(dict(type='system', content='Transcript record omitted: size limit.'))
        return result
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None


class Buffer:
    def __init__(self, lines):
        self.epoch = uuid.uuid4().hex
        self.lines = deque(maxlen=lines)
        self.source, self.sequence = None, -1
        self.serial = 0
        self.tokens, self.updated = 8.0, None

    def admit(self, now):
        if self.updated is not None:
            self.tokens = min(8, self.tokens + max(0, now - self.updated) * 4)
        self.updated = now
        if self.tokens < 1:
            return False
        self.tokens -= 1
        return True

    def append(self, source, start, lines):
        if source != self.source:
            if self.source is not None:
                self.epoch = uuid.uuid4().hex
                self.serial = 0
            self.lines.clear()
            self.source, self.sequence = source, start - 1
        if start > self.sequence + 1:
            return False
        for seq, line in enumerate(lines, start):
            if seq <= self.sequence:
                continue
            self.sequence = seq
            if line is not None:
                self.serial += 1
                self.lines.append((self.serial, line))
        return True

    def replay(self, cursor):
        """Cursor is epoch:serial. A gap/reload requests a reset and bounded replay."""
        epoch, _, number = (cursor or '').partition(':')
        serial = int(number) if number.isdigit() and len(number) < 20 else -1
        oldest = self.lines[0][0] if self.lines else self.serial + 1
        reset = epoch != self.epoch or serial < oldest - 1 or serial > self.serial
        if reset:
            serial = -1
        frames = [(self.epoch + ':' + str(n), line) for n, line in self.lines if n > serial]
        return reset, frames, self.epoch + ':' + str(self.serial)
