"""Explicit egress policy, checked at registration and pinned again at connection."""
import http.client
import ipaddress
import queue
import socket
import ssl
import threading
from urllib.parse import urlsplit


class Allowlist:
    def __init__(self, value='', resolver=socket.getaddrinfo, resolve_timeout=5):
        self.resolver = resolver
        self.resolve_timeout = resolve_timeout
        # OS DNS calls cannot be cancelled. Bound abandoned lookups as well as
        # caller wait time, and never let a stuck lookup keep the process alive.
        self._resolver_slots = threading.BoundedSemaphore(4)
        self.rules = []
        for item in filter(None, (v.strip() for v in value.split(','))):
            network, ports = item, None
            # A port suffix follows the prefix length, so IPv6 remains unambiguous.
            if '/' in item and ':' in item.split('/')[-1]:
                network, ports = item.rsplit(':', 1)
                if not ports:
                    raise ValueError('empty allowlist port')
            net = ipaddress.ip_network(network, strict=False)
            lo, hi = 1, 65535
            if ports:
                bounds = ports.split('-')
                lo, hi = int(bounds[0]), int(bounds[-1])
                if len(bounds) > 2 or not 1 <= lo <= hi <= 65535:
                    raise ValueError('invalid allowlist port range')
            self.rules.append((net, lo, hi))

    def _lookup(self, hostname, port):
        if not self._resolver_slots.acquire(blocking=False):
            raise ValueError('DNS resolver is busy')
        result = queue.Queue(maxsize=1)

        def lookup():
            try:
                result.put((True, self.resolver(hostname, port, type=socket.SOCK_STREAM)))
            except Exception as exc:
                result.put((False, exc))
            finally:
                self._resolver_slots.release()

        threading.Thread(target=lookup, daemon=True).start()
        try:
            success, answer = result.get(timeout=self.resolve_timeout)
        except queue.Empty:
            raise ValueError('DNS resolution timed out') from None
        if not success:
            raise answer
        return answer

    def resolve(self, url):
        try:
            if not self.rules:
                raise ValueError('AGENTDIR_VIEW_ALLOW is empty (presence only)')
            if any(c.isspace() for c in url):
                raise ValueError('view_url must not contain whitespace')
            parsed = urlsplit(url)
            if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                    parsed.username is not None or parsed.password is not None or parsed.fragment):
                raise ValueError('view_url requires http/https without credentials or fragment')
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
            if not 1 <= port <= 65535:
                raise ValueError('invalid port')
            addresses = {r[4][0] for r in self._lookup(parsed.hostname, port)}
            if not addresses:
                raise ValueError('view_url did not resolve')
            for address in addresses:
                ip = ipaddress.ip_address(address)
                if not any(ip in net and lo <= port <= hi for net, lo, hi in self.rules):
                    raise ValueError('view_url resolved address/port is outside AGENTDIR_VIEW_ALLOW')
            return parsed, port, sorted(addresses)
        except (OSError, ValueError) as exc:
            raise ValueError('view_url refused: ' + str(exc)) from None

    def fetch(self, url):
        """Adapters must use this seam, not urlopen. No redirects, no second DNS lookup."""
        parsed, port, addresses = self.resolve(url)
        raw = socket.create_connection((addresses[0], port), timeout=5)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=5)
        try:
            if parsed.scheme == 'https':
                raw = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
            connection.sock = raw
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            connection.request('GET', path)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ValueError('view redirects are refused')
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError('view response exceeds 1 MiB')
            return response.status, data
        finally:
            raw.close()
            connection.close()
