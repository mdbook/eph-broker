"""Run the directory as a module socket service or authenticated standalone UI."""
import argparse
import os
import signal

from . import auth
from .allowlist import Allowlist
from .directory import Directory
from .server import UIAuth, serve


def stop(signum, frame):
    raise SystemExit(0)  # Unwind serve's finally block and remove our UI socket.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    # The broker module host passes BROKER_MODULE_SOCKET; AGENTDIR_UI_SOCKET is for other hosts.
    parser.add_argument('--socket', default=os.environ.get('AGENTDIR_UI_SOCKET') or
                        os.environ.get('BROKER_MODULE_SOCKET'))
    parser.add_argument('--state-dir', default=os.environ.get('AGENTDIR_STATE_DIR'))
    parser.add_argument('--standalone', action='store_true')
    parser.add_argument('--port', type=int, default=8791)
    args = parser.parse_args()
    if args.state_dir is None:
        # Beside the module socket (the host's private state dir), else the working directory.
        name = os.environ.get('BROKER_MODULE_NAME', 'agentdir')
        args.state_dir = (os.path.join(os.path.dirname(os.path.abspath(args.socket)), name + '-state')
                          if args.socket and not args.standalone else 'agentdir-state')
    try:
        key = auth.private_read(os.environ['AGENTDIR_ENROLL_KEY_FILE'])
        directory = Directory(key, args.state_dir, Allowlist(os.environ.get('AGENTDIR_VIEW_ALLOW', '')))
        basic_file = os.environ.get('AGENTDIR_BASIC_AUTH_FILE')
        basic = auth.private_read(basic_file).strip() if basic_file else None
        if basic is not None and (b':' not in basic or not basic.split(b':', 1)[1]):
            raise ValueError('basic auth file requires user:password')
        # X-Viewer-User is what the broker module host injects after its own SSO check.
        ui_auth = UIAuth(not args.standalone,
                         os.environ.get('AGENTDIR_IDENTITY_HEADER',
                                        'X-Forwarded-User' if args.standalone else 'X-Viewer-User'),
                         os.environ.get('AGENTDIR_TRUSTED_PROXIES', ''), basic)
        if args.standalone and not (basic or ui_auth.proxies):
            raise ValueError('standalone requires basic auth or trusted forward-auth proxies')
        if not args.standalone and not args.socket:
            raise ValueError('--socket or AGENTDIR_UI_SOCKET is required')
        host, port = os.environ.get('AGENTDIR_AGENT_BIND', '127.0.0.1:8790').rsplit(':', 1)
        signal.signal(signal.SIGTERM, stop)
        serve(directory, args.socket, (host.strip('[]'), int(port)),
              args.port if args.standalone else None, ui_auth)
    except (KeyError, ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
