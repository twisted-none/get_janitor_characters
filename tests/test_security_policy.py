import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from security_policy import clean_card, protect_context, require_origin, require_public


class SecurityPolicyTests(unittest.TestCase):
    def test_hidden_definition_does_not_make_public_card_private(self):
        require_public({"is_public": True, "showdefinition": False})
        for card in ({"is_public": False}, {}, {"is_public": True, "is_private": True}):
            with self.subTest(card=card), self.assertRaises(ValueError):
                require_public(card)

    def test_origin_is_checked_exactly(self):
        require_origin(SimpleNamespace(url="https://janitorai.com/characters/id"))
        for url in ("https://janitorai.com.evil.invalid", "http://janitorai.com", "https://janitorai.com:8443"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                require_origin(SimpleNamespace(url=url))

    def test_metadata_is_allowlisted(self):
        card = clean_card({"data": {"name": "Ada", "first_mes": "Hi", "access_token": "fake",
                           "extensions": {"janitorai": {"personality": "Definition", "account_email": "fake", "access_token": "fake"},
                                          "session": {"token": "fake"}}}})
        self.assertNotIn("access_token", card["data"])
        self.assertEqual(card["data"]["extensions"], {"janitorai": {"personality": "Definition"}})

    def test_browser_request_is_aborted_on_private_destination(self):
        context = Mock()
        protect_context(context)
        callback = context.route.call_args.args[1]
        route = Mock()
        with patch("security_policy.fetch", side_effect=ValueError("private")):
            callback(route)
        route.abort.assert_called_once()
        route.fulfill.assert_not_called()

    def test_login_does_not_fill_off_origin_form(self):
        import parser
        page = Mock(url="https://other.invalid/login")
        page.goto.return_value.status = 200
        with patch.dict("os.environ", {"JANNYAI_EMAIL": "fake@example.invalid", "JANNYAI_PASSWORD": "fake"}):
            with self.assertRaises(ValueError):
                parser._login_janny(page)
        page.locator.assert_not_called()
