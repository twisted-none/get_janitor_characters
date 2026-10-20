from io import BytesIO
import socket
import unittest
from unittest.mock import Mock, patch

import safe_http


class Response:
    def __init__(self, body=b"ok", status=200, headers=()):
        self.status = status
        self.body = BytesIO(body)
        self.headers = headers

    def getheaders(self):
        return self.headers

    def read1(self, size):
        return self.body.read(size)


class SafeHTTPTests(unittest.TestCase):
    def test_bad_schemes_hosts_and_private_addresses_are_rejected(self):
        for url in ("http://janitorai.com/a", "https://janitorai.com.evil.invalid/a",
                    "https://user:password@janitorai.com/a", "file:///tmp/a", "https://janitorai.com:8443/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_http.validate_url(url)
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("safe_http.socket.getaddrinfo", return_value=private), self.assertRaises(ValueError):
            safe_http.validate_url("https://janitorai.com/a")

    def test_tls_connects_to_checked_ip_with_hostname_sni(self):
        connection = safe_http._PinnedHTTPS("janitorai.com", "8.8.8.8", 5)
        connection._context = Mock()
        sock = Mock()
        with patch("safe_http.socket.create_connection", return_value=sock) as connect:
            connection.connect()
        connect.assert_called_once_with(("8.8.8.8", 443), 5)
        connection._context.wrap_socket.assert_called_once_with(sock, server_hostname="janitorai.com")

    def test_redirect_revalidates_destination_and_strips_credentials(self):
        first, second = Mock(), Mock()
        first.getresponse.return_value = Response(status=302, headers=[("Location", "https://ella.janitorai.com/x")])
        second.getresponse.return_value = Response()
        with patch("safe_http.validate_url", side_effect=[("datacat.run", ["8.8.8.8"]), ("ella.janitorai.com", ["8.8.4.4"])]) as validate:
            with patch("safe_http._PinnedHTTPS", side_effect=[first, second]):
                result = safe_http.fetch("https://datacat.run/x", headers={"X-Session-Token": "fake", "Cookie": "fake"})
        self.assertEqual(validate.call_count, 2)
        self.assertEqual(result[2], b"ok")
        self.assertNotIn("Cookie", second.request.call_args.kwargs["headers"])
        self.assertNotIn("X-Session-Token", second.request.call_args.kwargs["headers"])

    def test_stream_stops_at_limit_and_closes_connection(self):
        connection = Mock()
        connection.getresponse.return_value = Response(b"too much data")
        with patch("safe_http.validate_url", return_value=("datacat.run", ["8.8.8.8"])), patch("safe_http._PinnedHTTPS", return_value=connection):
            with self.assertRaises(ValueError):
                safe_http.fetch("https://datacat.run/x", limit=3)
        connection.close.assert_called_once()

    def test_redirect_to_private_ip_never_opens_second_connection(self):
        connection = Mock()
        connection.getresponse.return_value = Response(status=302, headers=[("Location", "https://private.janitorai.com/x")])
        with patch("safe_http.validate_url", side_effect=[("datacat.run", ["8.8.8.8"]), ValueError("private")]):
            with patch("safe_http._PinnedHTTPS", return_value=connection) as connect:
                with self.assertRaises(ValueError):
                    safe_http.fetch("https://datacat.run/x")
        self.assertEqual(connect.call_count, 1)
