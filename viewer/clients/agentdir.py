"""Run with python3 -m viewer.clients.agentdir; credentials never printed except mint."""
import argparse
import http.client
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from .. import auth


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class APIError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Client:
    def __init__(self, state, server='http://127.0.0.1:8790', transport=None, clock=time.time_ns):
        self.path, self.server, self.clock = state, server.rstrip('/'), clock
        self.transport = transport or self.http
        try:
            self.state = json.loads(auth.private_read(state))
        except FileNotFoundError:
            self.state = {}

    def http(self, path, body, headers):
        request = urllib.request.Request(self.server + path, body, headers, method='POST')
        # Local API traffic should not leak through ambient HTTP proxy settings.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            with opener.open(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise APIError(exc.code, 'agent API refused request (%s): %s' %
                           (exc.code, exc.read(16384).decode(errors='replace'))) from None

    def save(self):
        auth.private_write(self.path, json.dumps(self.state).encode())

    def call(self, action, data=None, recover=True):
        path = '/v1/' + action
        body = json.dumps(data or {}, separators=(',', ':')).encode()
        headers = {'Content-Type': 'application/json'}
        if action != 'enroll':
            if not self.state:
                raise ValueError('enroll first')
            stamp = max(self.clock() // 1000, self.state.get('last_us', 0) + 1)
            ts = '%d.%06d' % divmod(stamp, 1000000)
            headers.update({'X-Agentdir-Kid': self.state['kid'], 'X-Agentdir-Ts': ts,
                            'X-Agentdir-Sig': auth.signature(auth.decode(self.state['key']),
                                                            'POST', path, ts, body)})
            self.state['last_us'] = stamp
            self.save()
        try:
            result = self.transport(path, body, headers)
        except ValueError as exc:
            if (action in ('beat', 'events') and recover and getattr(exc, 'status', None) == 409 and
                    self.state.get('registration')):
                # The server intentionally forgets entries on restart or TTL expiry.
                self.call('register', self.state['registration'])
                return self.call(action, data, recover=False)
            raise
        if action == 'enroll':
            self.state = dict(result, name=data['name'])
            self.save()
        elif action == 'register':
            self.state['ttl'] = data['ttl']
            self.state['registration'] = data
            self.save()
        elif action == 'deregister':
            self.state = {}
            self.save()
        return result


def alive(pid=None, tmux=None):
    if pid is not None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
    if tmux is not None:
        return subprocess.run(['tmux', 'has-session', '-t', tmux],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', help='0600 credential file, required except for mint')
    parser.add_argument('--server', default=os.environ.get('AGENTDIR_SERVER', 'http://127.0.0.1:8790'))
    commands = parser.add_subparsers(dest='command', required=True)
    mint = commands.add_parser('mint')
    mint.add_argument('--name', required=True)
    mint.add_argument('--ttl', type=int, default=600)
    enroll = commands.add_parser('enroll')
    enroll.add_argument('--name', required=True)
    enroll.add_argument('--token', help='or read token from stdin')
    register = commands.add_parser('register')
    register.add_argument('--kind', choices=('hermes', 'claude-code', 'codex', 'generic'), default='generic')
    register.add_argument('--caps', default='', help='comma-separated view,steer,tail')
    register.add_argument('--view-url')
    register.add_argument('--ttl', type=int, default=60)
    beat = commands.add_parser('beat')
    beat.add_argument('--every', type=float, help='defaults to registered ttl/3')
    lifetime = beat.add_mutually_exclusive_group()
    lifetime.add_argument('--while-pid', type=int)
    lifetime.add_argument('--while-tmux')
    status = commands.add_parser('status')
    status.add_argument('status', choices=('busy', 'idle'))
    tail = commands.add_parser('tail', help='follow and push a Claude Code or Codex transcript')
    tail.add_argument('--transcript', required=True)
    tail.add_argument('--lines', type=int, default=2000, help='local replay ring (1..10000)')
    owner = tail.add_mutually_exclusive_group()
    owner.add_argument('--while-pid', type=int)
    owner.add_argument('--while-tmux')
    commands.add_parser('deregister')
    args = parser.parse_args(argv)
    try:
        if args.command == 'mint':
            key = auth.private_read(os.environ['AGENTDIR_ENROLL_KEY_FILE'])
            print(auth.mint(key, args.name, args.ttl))
            return
        if not args.state:
            raise ValueError('--state FILE is required')
        client = Client(args.state, args.server)
        if args.command == 'enroll':
            client.call('enroll', {'name': args.name, 'token': args.token or input().strip()})
        elif args.command == 'register':
            data = dict(name=client.state.get('name'), kind=args.kind,
                        caps=list(filter(None, args.caps.split(','))), ttl=args.ttl)
            if args.view_url is not None:
                data['view_url'] = args.view_url
            client.call('register', data)
        elif args.command == 'beat':
            interval = args.every if args.every is not None else client.state.get('ttl', 60) / 3
            if not 0 < interval <= client.state.get('ttl', 60) / 3:
                raise ValueError('--every must be positive and at most ttl/3')
            if args.while_pid is not None and args.while_pid <= 0:
                raise ValueError('--while-pid must be positive')
            retry_limit = min(interval, 30)
            retry_delay = min(retry_limit, 1)
            try:
                while alive(args.while_pid, args.while_tmux):
                    try:
                        client.call('beat')
                    except (OSError, http.client.HTTPException, APIError) as exc:
                        if isinstance(exc, APIError) and not (exc.status in (408, 429) or
                                                              500 <= exc.status < 600):
                            raise
                        time.sleep(retry_delay)
                        retry_delay = min(retry_delay * 2, retry_limit)
                    else:
                        retry_delay = min(retry_limit, 1)
                        time.sleep(interval)
            except KeyboardInterrupt:
                pass
            # Only a deliberate interrupt or owner disappearance revokes the kid.
            # Unexpected errors must leave credentials intact for a later restart.
            client.call('deregister')
        elif args.command == 'status':
            client.call('beat', {'status': args.status})
        elif args.command == 'tail':
            from .tail import follow
            if not 1 <= args.lines <= 10000:
                raise ValueError('--lines must be 1..10000')
            if args.while_pid is not None and args.while_pid <= 0:
                raise ValueError('--while-pid must be positive')
            follow(client, args.transcript, lambda: alive(args.while_pid, args.while_tmux), lines=args.lines)
        else:
            client.call('deregister')
    except KeyboardInterrupt:
        return
    except (ValueError, OSError, KeyError, EOFError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
