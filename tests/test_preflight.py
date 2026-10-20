import unittest

from tools.preflight import check_environment


class PreflightTests(unittest.TestCase):
    def test_required_settings_and_pairs(self):
        findings = check_environment({})
        self.assertIn(("BOT_TOKEN", False), findings)
        self.assertIn(("DATABASE_URL", False), findings)
        findings = dict(check_environment({"BOT_TOKEN": "fake", "DATABASE_URL": "fake",
                                          "JANITORAI_EMAIL": "fixture@example.invalid"}))
        self.assertFalse(findings["JANITORAI_CREDENTIAL_PAIR"])

    def test_does_not_return_setting_values(self):
        findings = check_environment({"BOT_TOKEN": "fixture-secret", "DATABASE_URL": "fixture-dsn",
                                      "DOWNLOAD_CONCURRENCY": "0"})
        self.assertNotIn("fixture-secret", repr(findings))
        self.assertNotIn("fixture-dsn", repr(findings))
        self.assertFalse(dict(findings)["DOWNLOAD_CONCURRENCY"])

    def test_positive_limits_and_transport_proxy(self):
        findings = dict(check_environment({"BOT_TOKEN": "fake", "DATABASE_URL": "fake",
                                          "DOWNLOAD_CONCURRENCY": "2", "CLOAKBROWSER_PROXY": "fake"}))
        self.assertTrue(findings["DOWNLOAD_CONCURRENCY"])
        self.assertFalse(findings["NO_UNSUPPORTED_TRANSPORT_PROXY"])
