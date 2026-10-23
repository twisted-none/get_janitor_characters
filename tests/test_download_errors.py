import json
import socket
import ssl
import unittest

from download_errors import SourceError, describe_error


class DownloadErrorTests(unittest.TestCase):
    def test_playwright_timeout_has_timeout_code(self):
        from playwright.sync_api import TimeoutError as BrowserTimeout
        self.assertEqual(describe_error(BrowserTimeout("fixture-secret")).code, "timeout")

    def test_safe_fields_only_survive_serialization(self):
        error = SourceError("http_error", "fixture-password", http_status=403)
        payload = error.to_payload()
        self.assertEqual(payload["code"], "http_error")
        self.assertEqual(payload["http_status"], 403)
        self.assertNotIn("fixture-password", json.dumps(payload))
        restored = SourceError.from_payload({**payload, "message": "fixture-token"})
        self.assertEqual(restored.http_status, 403)
        self.assertNotIn("fixture-token", str(restored))

    def test_untrusted_fields_cannot_enter_logs(self):
        error = SourceError.from_payload({"code": "fixture-secret", "error_type": "fixture-secret",
                                          "http_status": "fixture-secret", "exit_code": "fixture-secret"})
        self.assertEqual(error.code, "internal_error")
        self.assertNotIn("fixture-secret", str(error.to_payload()))

    def test_common_failure_types_have_distinct_codes(self):
        for error, code in ((socket.gaierror("fixture-token"), "dns_error"),
                            (ssl.SSLError("fixture-token"), "tls_error"),
                            (PermissionError("fixture-token"), "permission_denied"),
                            (TimeoutError("fixture-token"), "timeout"),
                            (ModuleNotFoundError("fixture-token"), "dependency_missing")):
            with self.subTest(code=code):
                result = describe_error(error)
                self.assertEqual(result.code, code)
                self.assertNotIn("fixture-token", str(result.to_payload()))
