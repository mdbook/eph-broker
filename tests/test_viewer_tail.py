"""Signed push, bounded replay, file following and shared renderer contracts."""
import base64
import json
import shutil
import subprocess
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

from viewer import assets, transcripts
from viewer.clients.agentdir import Client
from viewer.clients.tail import Follower, Pusher
from viewer.directory import Directory, Refused
from viewer_support import KEY, ViewerTest
from test_viewer_ui import request


def message(text):
    return json.dumps(dict(type='assistant', message={'content': text}))


class Tail(ViewerTest):
    def client(self, kind='claude-code', caps=None):
        self.now = 100
        self.directory = Directory(KEY, self.tmp_path, clock=lambda: self.now)
        client = Client(self.tmp_path / 'demo.json', transport=self.directory.request,
                        clock=lambda: int(self.now * 1000000000))
        from viewer import auth
        client.call('enroll', dict(name='demo', token=auth.mint(KEY, 'demo', clock=lambda: self.now)))
        client.call('register', dict(name='demo', kind=kind, caps=['tail'] if caps is None else caps, ttl=30))
        return client

    def pump(self, pusher, times=1):
        for _ in range(times):
            self.now += 1
            pusher.step()

    def test_follow_partial_rotate_truncate_and_bounds(self):
        path = self.tmp_path / 'session.jsonl'
        path.write_text(message('first') + '\n' + message('partial')[:20])
        follower = Follower(path, 'claude-code', lines=2)
        self.addCleanup(follower.close)
        assert follower.poll() == 1
        with path.open('a') as out:
            out.write(message('partial')[20:] + '\n' + message('third') + '\n')
        assert follower.poll() == 2
        assert len(follower.lines) == 2 and 'partial' in follower.lines[0][1]
        old_source = follower.source
        path.rename(self.tmp_path / 'old.jsonl')
        assert follower.poll() == 0  # rotation's missing-path window
        path.write_text(message('rotated') + '\n')
        assert follower.poll() == 1 and follower.source != old_source
        assert len(follower.lines) == 1 and 'rotated' in follower.lines[0][1]
        old_source = follower.source
        path.write_text(message('x') + '\n')
        assert follower.poll() == 1 and follower.source != old_source
        assert '"x"' in follower.lines[0][1]

    def test_follow_large_lines_are_discarded_without_fragments(self):
        from viewer.clients.tail import READ_BYTES
        path = self.tmp_path / 'session.jsonl'
        path.write_bytes(b'x' * (READ_BYTES * 3) + b'\n' + message('safe').encode() + b'\n')
        follower = Follower(path, 'claude-code')
        self.addCleanup(follower.close)
        for _ in range(5):
            follower.poll()
        assert len(follower.lines) == 1 and 'safe' in follower.lines[0][1]

    def follow_rewrite(self, extra):
        path = self.tmp_path / 'session.jsonl'
        old = json.dumps(dict(type='response_item', payload=dict(
            type='function_call', call_id='old-call', name='exec_command',
            arguments=json.dumps({'cmd': 'echo old'})))) + '\n' + message('unfinished')[:20]
        path.write_text(old)
        follower = Follower(path, 'codex')
        self.addCleanup(follower.close)
        assert follower.poll() == 1
        assert follower.partial and list(follower.calls) == ['old-call']
        old_source, old_inode = follower.source, path.stat().st_ino
        replacement = (message('new') + '\n' + message('second')).ljust(len(old) - 1) + '\n' + extra
        path.write_text(replacement)
        assert path.stat().st_ino == old_inode
        assert path.stat().st_size == len(old) + len(extra)
        expected = ['new', 'second'] + (['third'] if extra else [])
        assert follower.poll() == len(expected)
        assert follower.source != old_source
        assert follower.sequence == len(expected)
        assert not follower.partial and not follower.discard and not follower.calls
        assert [n for n, _ in follower.lines] == list(range(len(expected)))
        assert [json.loads(line)['message']['content'] for _, line in follower.lines] == expected
        new_source = follower.source
        assert follower.poll() == 0 and follower.source == new_source
        with path.open('a') as out:
            out.write(message('appended') + '\n')
        assert follower.poll() == 1 and follower.source == new_source
        assert follower.lines[-1][0] == len(expected)
        assert json.loads(follower.lines[-1][1])['message']['content'] == 'appended'

    def test_follow_same_size_rewrite(self):
        self.follow_rewrite('')

    def test_follow_larger_rewrite(self):
        self.follow_rewrite(message('third') + '\n')

    def test_codex_interleaved_commands_render_once(self):
        calls = deque(maxlen=500)

        def project(typ, **payload):
            return transcripts.normalize(json.dumps(dict(type=typ, payload=payload)), 'codex', calls)

        cards = []
        for call in ('first', 'second'):
            line = project('response_item', type='function_call', call_id=call, name='exec_command',
                           arguments=json.dumps({'cmd': 'echo ' + call}))
            cards.append(json.loads(line)['message']['content'][0])
        for call in ('second', 'first'):
            assert project('event_msg', type='exec_command_end', call_id=call, command='echo ' + call,
                           aggregated_output=call, exit_code=0) is None
            line = project('response_item', type='function_call_output', call_id=call, output=call)
            result = json.loads(line)['message']['content'][0]
            assert result == dict(type='tool_result', tool_use_id=call, content=call)
        assert [card['id'] for card in cards] == ['first', 'second']
        assert all(card['type'] == 'tool_use' and card['name'] == 'Bash' for card in cards)
        # Backfill may start after a command's function_call. Keep that standalone completion.
        line = project('event_msg', type='exec_command_end', call_id='earlier', command='echo earlier',
                       aggregated_output='earlier', exit_code=0)
        view = json.loads(json.loads(line)['content'])
        assert view['item_type'] == 'command_execution' and view['command'] == 'echo earlier'

    def test_codex_rollout_projection_and_redaction(self):
        calls = deque(maxlen=500)
        rollout = lambda payload: json.dumps(dict(type='response_item', payload=payload))
        project = lambda payload: transcripts.normalize(rollout(payload), 'codex', calls)
        secret = 'ghp_' + 'q' * 24
        line = project(dict(type='message', role='assistant', content=[dict(type='output_text', text=secret)]))
        assert secret not in line and '[redacted]' in line
        line = project(dict(type='function_call', call_id='c', name='exec_command',
                            arguments=json.dumps({'cmd': 'printf ' + secret})))
        assert secret not in line and 'command' in line
        line = project(dict(type='function_call_output', call_id='c', output='begin\n' + secret + '\n' + 'x' * 4000))
        assert len(json.loads(line)['message']['content'][0]['content']) <= 3000
        assert secret not in line
        assert project(dict(type='function_call', call_id='m', name='mcp__test', arguments=secret))
        assert project(dict(type='function_call_output', call_id='m', output='private MCP result')) is None
        assert project(dict(type='reasoning', encrypted_content='private blob', summary=[dict(text='A thought')]))
        assert project(dict(type='unknown', data='private')) is None
        assert transcripts.normalize('{bad', 'codex') is None
        raw = json.dumps(dict(type='item.completed', item=dict(type='mcp_tool_call', server='test', tool='tool',
                                                             arguments='private args', result='private result')))
        assert 'private' not in transcripts.normalize(raw, 'codex')
        command = dict(type='command_execution', command='echo ' + secret, aggregated_output='ok\n' + secret,
                       exit_code=0)
        line = transcripts.normalize(json.dumps(dict(type='item.completed', item=command)), 'codex')
        view = json.loads(json.loads(line)['content'])
        from transcript_codex import item_detail
        assert view['command'] == item_detail(command)['command']
        assert secret not in line
        normalized = json.loads(transcripts.normalize(line, 'codex'))
        assert json.loads(normalized['content']) == view
        large = transcripts.normalize(message('x' * 30000), 'claude-code')
        assert 'size limit' in large and len(large.encode()) < transcripts.LINE_BYTES

    def test_unsigned_oversize_caps_rate_and_signing(self):
        client = self.client()
        assert request(self.directory, '/v1/events', 'Content-Length: 2\r\n', True, 'POST', b'{}')[0] == 401
        assert request(self.directory, '/v1/events', 'Content-Length: 65537\r\n', True, 'POST')[0] == 413
        data = dict(source='test', start=0, lines=[message('hello')])
        epoch = client.call('events', data)['epoch']
        client.call('events', dict(data, epoch=epoch))
        assert 'hello' in self.directory.tails['demo'].lines[0][1]
        transport = client.transport
        client.transport = lambda path, body, headers: transport(path, body.replace(b'hello', b'other'), headers)
        with self.assertRaises(Refused) as error:
            client.call('events', dict(data, epoch=epoch))
        assert error.exception.status == 401  # The signature covers every batch byte.
        client.transport = transport
        with self.assertRaises(Refused) as error:
            client.call('events', dict(data, lines=['x' * 65536]))
        assert error.exception.status == 413
        for _ in range(6):
            client.call('events', dict(data, epoch=epoch))
        with self.assertRaises(Refused) as error:
            client.call('events', dict(data, epoch=epoch))
        assert error.exception.status == 429
        self.now += 1
        client.call('events', dict(data, epoch=epoch))
        client.call('register', dict(name='demo', kind='claude-code', caps=[], ttl=30))
        with self.assertRaises(Refused) as error:
            client.call('events', data)
        assert error.exception.status == 403 and not self.directory.tails
        with patch.dict('os.environ', AGENTDIR_TAIL_LINES='0'), self.assertRaises(ValueError):
            Directory(KEY, self.tmp_path)

    def test_bounded_push_idempotency_replay_and_module_reload(self):
        with patch.dict('os.environ', AGENTDIR_TAIL_LINES='3'):
            client = self.client()
        path = self.tmp_path / 'session.jsonl'
        path.write_text(''.join(message(str(i)) + '\n' for i in range(5)))
        follower = Follower(path, 'claude-code', lines=4)
        self.addCleanup(follower.close)
        follower.poll()
        pusher = Pusher(client, follower)
        self.pump(pusher, 2)  # epoch negotiation, then signed upload
        tail = self.directory.tails['demo']
        assert [json.loads(s)['message']['content'] for _, s in tail.lines] == ['2', '3', '4']
        reset, frames, cursor = tail.replay(None)
        assert reset and len(frames) == 3
        assert tail.replay(cursor)[:2] == (False, [])
        assert len(tail.replay(frames[0][0])[1]) == 2
        assert tail.replay(tail.epoch + ':0')[0]  # evicted cursor
        serial = tail.serial
        pusher.acked = 0  # ambiguous reply: resend the same source and sequence
        self.pump(pusher)
        assert tail.serial == serial and len(tail.lines) == 3
        old_epoch = tail.epoch
        self.directory = Directory(KEY, self.tmp_path, clock=lambda: self.now)
        client.transport = self.directory.request
        self.pump(pusher, 2)  # auto-register and replay an idle file
        tail = self.directory.tails['demo']
        assert tail.epoch != old_epoch and len(tail.lines) == 4
        assert self.directory.snapshot()[0]['can_tail']
        sizes = []
        dispatch = client.transport

        def capture(path, body, headers):
            sizes.append(len(body))
            return dispatch(path, body, headers)

        client.transport = capture
        with path.open('a') as out:
            for _ in range(8):
                out.write(message('x' * 20000) + '\n')
        follower.poll()
        self.pump(pusher, 3)
        assert len(tail.lines) == 4 and all(n <= transcripts.BATCH_BYTES for n in sizes)
        assert sum(n > 20000 for n in sizes) == 2  # The replay ring needed two bounded batches.

    def test_http_sse_live_resume_reset_and_auth(self):
        client = self.client()
        path = self.tmp_path / 'session.jsonl'
        path.write_text(message('first') + '\n')
        follower = Follower(path, 'claude-code')
        self.addCleanup(follower.close)
        follower.poll()
        pusher = Pusher(client, follower)
        self.pump(pusher, 2)
        tail = self.directory.tails['demo']
        cursor = tail.replay(None)[2]
        assert request(self.directory, '/api/agents/demo/transcript')[0] == 401
        count = [0]

        def live(_):
            count[0] += 1
            if count[0] == 1:
                with path.open('a') as out:
                    out.write(message('second') + '\n')
                follower.poll()
                self.pump(pusher)
            else:
                raise BrokenPipeError()

        with patch('viewer.server.time.sleep', side_effect=live):
            status, body = request(self.directory, '/api/agents/demo/transcript', 'X-Forwarded-User: reader\r\n')
        assert status == 200 and b'event: reset' in body and b'first' in body and b'second' in body
        with patch('viewer.server.time.sleep', side_effect=BrokenPipeError):
            status, body = request(self.directory, '/api/agents/demo/transcript',
                                   'X-Forwarded-User: reader\r\nLast-Event-ID: %s\r\n' % cursor)
        assert b'first' not in body and b'second' in body and b'event: reset' not in body
        with patch('viewer.server.time.sleep', side_effect=BrokenPipeError):
            _, body = request(self.directory, '/api/agents/demo/transcript',
                              'X-Forwarded-User: reader\r\nLast-Event-ID: old:0\r\n')
        assert b'event: reset' in body and b'first' in body
        with patch('viewer.server.time.sleep', side_effect=lambda _: client.call('deregister')):
            _, body = request(self.directory, '/api/agents/demo/transcript', 'X-Forwarded-User: reader\r\n')
        assert b'event: end' in body and not self.directory.tails

    def test_shared_assets_are_authenticated_and_renderer_mounts(self):
        client = self.client()
        for path in ('/transcript.js', '/transcript.css'):
            assert request(self.directory, path)[0] == 401
            assert request(self.directory, path, 'X-Forwarded-User: reader\r\n')[0] == 200
        import dash
        assert assets.transcript_renderer().strip() in dash.PAGE
        assert assets.transcript_text().strip() in dash.PAGE
        assert assets.transcript_css().strip() in dash.PAGE
        ui = (Path(__file__).parents[1] / 'viewer/ui.js').read_text()
        assert "import(base + 'transcript.js')" in ui
        node = shutil.which('node')
        if not node:
            self.skipTest('node unavailable; shared source identity checked')
        url = 'data:text/javascript;base64,' + base64.b64encode(assets.transcript_module().encode()).decode()
        script = r'''
const {mount} = await import(URL);
function mk(tag) { return {tag, children: [], className: '', textContent: '', scrollTop: 0,
  scrollHeight: 0, clientHeight: 0, setAttribute() {},
  get childNodes() {return this.children;},
  append(...nodes) {for (const n of nodes) this.appendChild(n);},
  appendChild(n) {this.children.push(n); n.parent = this; return n;},
  replaceChildren(...nodes) {this.children = []; this.append(...nodes);},
  set innerHTML(s) {this.markup = s; if (!s) this.children = [];},
  get classList() {return {toggle: () => true};}
}; }
globalThis.document = {createElement: mk};
let stream;
let reconnect;
globalThis.setTimeout = fn => {reconnect = fn; return 1;};
globalThis.clearTimeout = () => {reconnect = null;};
globalThis.EventSource = class {
  static CLOSED = 2;
  constructor(url) {this.url = url; this.listeners = {}; stream = this;}
  addEventListener(k, fn) {this.listeners[k] = fn;}
  close() {this.closed = true;}
};
const host = mk('host'); host.attachShadow = () => host;
const mounted = mount(host, {url: '/live', css: '/transcript.css'});
stream.onopen();
const log = host.children.at(-1);
stream.onmessage({data: JSON.stringify({type:'assistant', message:{content:'hello <script>bad()</script>'}})});
stream.onmessage({data: JSON.stringify({type:'system', subtype:'codex', content:JSON.stringify({
  type:'item.completed', item_type:'agent_message', text:'Codex says hello'})})});
stream.onmessage({data: JSON.stringify({type:'system', subtype:'codex', content:JSON.stringify({
  type:'turn.completed', usage:{input_tokens:12, output_tokens:4}})})});
if (log.children.length !== 3) throw Error('both harnesses and usage render live');
const body = log.children[0].children.at(-1);
if (body.markup.includes('<script>') || !body.markup.includes('&lt;script&gt;')) throw Error('escape first');
stream.listeners.reset();
if (log.children.length) throw Error('replay resets timeline');
const oldStream = stream;
stream.readyState = EventSource.CLOSED; stream.onerror();
if (!reconnect) throw Error('retry a reload gap that closed EventSource');
reconnect();
if (!oldStream.closed || stream === oldStream) throw Error('one new stream after reload gap');
mounted.destroy();
if (!stream.closed || reconnect) throw Error('unmount closes stream and retry timer');
console.log('ok');
'''.replace('import(URL)', 'import(' + json.dumps(url) + ')')
        result = subprocess.run([node, '--input-type=module', '-e', script], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0 and result.stdout.strip() == 'ok', result.stderr[-2000:]


if __name__ == '__main__':
    unittest.main()
