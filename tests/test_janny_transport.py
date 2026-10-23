import socket
import unittest
from unittest.mock import patch

from download_errors import SourceError
from janny_transport import allowed_url, resolve_target, native_transport
from urllib.parse import urlsplit


class NativeTransportTests(unittest.TestCase):
    def test_only_exact_https_janny_and_challenge_hosts_are_allowed(self):
        self.assertTrue(allowed_url('https://jannyai.com/characters/abc'))
        self.assertTrue(allowed_url('https://challenges.cloudflare.com/widget'))
        for address in ('http://jannyai.com/', 'https://eviljannyai.com/',
                        'https://jannyai.com.evil.test/', 'https://jannyai.com:8443/',
                        'https://user@jannyai.com/', 'file:///etc/passwd',
                        'https://127.0.0.1/', 'https://jannyai.com\\@evil.test/'):
            self.assertFalse(allowed_url(address), address)

    def test_connect_pins_public_address_and_refuses_private_dns(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        with patch('safe_http.socket.getaddrinfo', return_value=public):
            self.assertEqual(resolve_target('jannyai.com:443'), '1.1.1.1')
        for address in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1'):
            private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
            with patch('safe_http.socket.getaddrinfo', return_value=private):
                with self.assertRaises(SourceError):
                    resolve_target('jannyai.com:443')

    def test_connect_rejects_non_allowlisted_authorities(self):
        for authority in ('localhost:443', 'jannyai.com:80', 'evil.test:443',
                          'user@jannyai.com:443', 'jannyai.com:443/path'):
            with self.assertRaises(SourceError):
                resolve_target(authority)

    def test_tunnel_keeps_browser_tls_bytes_unchanged_and_connects_to_pinned_ip(self):
        upstream, remote = socket.socketpair()
        with remote, patch('janny_transport.resolve_target', return_value='1.1.1.1'), \
                patch('janny_transport.socket.create_connection', return_value=upstream) as connect:
            with native_transport() as proxy, socket.socket() as client:
                address = urlsplit(proxy)
                client.settimeout(3)
                remote.settimeout(3)
                client.connect((address.hostname, address.port))
                client.sendall(b'CONNECT jannyai.com:443 HTTP/1.1\r\nHost: jannyai.com:443\r\n\r\n')
                self.assertEqual(client.recv(1024), b'HTTP/1.1 200 Connection Established\r\n\r\n')
                connect.assert_called_once_with(('1.1.1.1', 443), timeout=10)
                client.sendall(b'opaque-client-tls')
                self.assertEqual(remote.recv(1024), b'opaque-client-tls')
                remote.sendall(b'opaque-server-tls')
                self.assertEqual(client.recv(1024), b'opaque-server-tls')
