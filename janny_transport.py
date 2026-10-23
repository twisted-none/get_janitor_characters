"""Local CONNECT tunnel: native browser TLS with pinned public destinations.

The loopback listener exists only during one download. It does not decrypt TLS,
accept arbitrary destinations, or forward credentials. HTTP and WebSockets are
also refused by the browser route policy.
"""

from contextlib import contextmanager
import select
import socket
import socketserver
import threading
import time
from urllib.parse import urlsplit

from download_errors import SourceError
from safe_http import validate_url

HOSTS = frozenset({'jannyai.com', 'image.jannyai.com', 'api.jannyai.com', 'challenges.cloudflare.com'})


def allowed_url(url):
    try:
        address = urlsplit(url)
        return (address.scheme == 'https' and address.hostname in HOSTS
                and address.port in (None, 443) and not address.username and not address.password
                and '\\' not in url and not any(ord(char) < 33 for char in url))
    except ValueError:
        return False


def resolve_target(authority):
    if authority not in {host + ':443' for host in HOSTS}:
        raise SourceError('unsafe_url')
    _, addresses = validate_url('https://' + authority, domains=tuple(HOSTS))
    return addresses[0]


class _Tunnel(socketserver.BaseRequestHandler):
    def handle(self):
        if not self.server.slots.acquire(blocking=False):
            return
        try:
            self.request.settimeout(10)
            header = bytearray()
            # Do not over-read bytes belonging to the subsequent TLS handshake.
            while not header.endswith(b'\r\n\r\n'):
                byte = self.request.recv(1)
                if not byte or len(header) >= 8192:
                    return
                header.extend(byte)
            method, authority, protocol = header.split(b'\r\n', 1)[0].decode('ascii').split(' ')
            if method != 'CONNECT' or protocol != 'HTTP/1.1':
                return
            with self.server.lock:
                self.server.connections += 1
                if self.server.connections > 200:
                    raise SourceError('response_too_large')
            address = resolve_target(authority)
            with socket.create_connection((address, 443), timeout=10) as upstream:
                self.request.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                while time.monotonic() < self.server.deadline:
                    readable, _, _ = select.select([self.request, upstream], [], [], 1)
                    for current in readable:
                        data = current.recv(65536)
                        if not data:
                            return
                        with self.server.lock:
                            self.server.bytes += len(data)
                            if self.server.bytes > 64_000_000:
                                return
                        (upstream if current is self.request else self.request).sendall(data)
        except (OSError, ValueError, UnicodeError):
            pass  # Never print traffic, URL queries or TLS payloads to logs.
        finally:
            self.server.slots.release()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False


@contextmanager
def native_transport():
    with _Server(('127.0.0.1', 0), _Tunnel) as server:
        server.slots = threading.BoundedSemaphore(32)
        server.lock = threading.Lock()
        server.connections = server.bytes = 0
        server.deadline = time.monotonic() + 165
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_address[1]}'
        finally:
            server.shutdown()
            thread.join(timeout=2)
