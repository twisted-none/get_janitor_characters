import base64
import json
import time
import unittest
from unittest.mock import Mock

import janitor_proxy


def _token(expires_in: int = 3600) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + expires_in}).encode())
    return f"header.{payload.decode().rstrip('=')}.signature"


class JanitorProxyTests(unittest.TestCase):
    def test_created_response_is_accepted(self) -> None:
        page = Mock(url="https://janitorai.com")
        page.evaluate.return_value = {"status": 201, "text": '{"id":12}'}
        self.assertEqual(janitor_proxy._api_json(page, "fake-token", "/mb/chats", "POST", {}), {"id": 12})

    def test_expired_token_is_rejected(self) -> None:
        self.assertFalse(janitor_proxy._token_is_live(_token(-60)))

    def test_browser_session_token_replaces_expired_cookie(self) -> None:
        fresh = _token()
        encoded = base64.urlsafe_b64encode(json.dumps({"access_token": fresh}).encode()).decode()
        page = Mock(url="https://janitorai.com")
        page.evaluate.return_value = _token(-60)
        context = Mock()
        context.cookies.return_value = [
            {"name": "sb-access-token", "value": _token(-60)},
            {"name": "sb-project-auth-token", "value": f"base64-{encoded}"},
        ]
        self.assertEqual(janitor_proxy.get_access_token(page, context), fresh)

    def test_parse_definition_uses_only_character_sections(self) -> None:
        system = (
            "<system>Rules</system>"
            "<Ada's Persona>Exact definition</Ada's Persona>"
            "<Scenario>At school</Scenario>"
            "<UserPersona>Private user data</UserPersona>"
            "<example_dialogs>Ada: Hello</example_dialogs>"
        )
        self.assertEqual(janitor_proxy.parse_definition(system, "Ada"), {
            "personality": "Exact definition",
            "scenario": "At school",
            "example_dialogs": "Ada: Hello",
        })

    def test_extract_definition_reads_proxy_prompt(self) -> None:
        responses = {
            "/mb/characters/abc": {"id": "abc", "allow_proxy": True, "name": "Ada", "is_public": True},
            "/mb/chats": {"id": 12, "user_id": "user-1"},
            "/mb/chats/12": {"chatMessages": [{
                "id": 5, "created_at": "2026-01-01T00:00:00Z", "message": "Hello",
            }]},
            "/generateAlpha": {"messages": [{
                "role": "system", "content": "<Ada's Persona>Secret</Ada's Persona><Scenario>Lab</Scenario>",
            }]},
        }
        requests = []

        def evaluate(_script, args=None):
            if args is None:
                return _token()
            requests.append(args)
            return {"status": 200, "text": json.dumps(responses[args["path"]])}

        page = Mock(evaluate=evaluate, url="https://janitorai.com")
        context = Mock()
        context.cookies.return_value = []
        definition = janitor_proxy.extract_definition(page, context, "abc")

        self.assertEqual(definition, {
            "personality": "Secret", "scenario": "Lab", "first_message": "Hello",
        })
        self.assertEqual([request["path"] for request in requests], list(responses))
        self.assertEqual(requests[-1]["body"]["userConfig"]["open_ai_mode"], "proxy")
        self.assertEqual(requests[-1]["body"]["userConfig"]["open_ai_reverse_proxy"], "")
        self.assertEqual(requests[-1]["body"]["userConfig"]["reverseProxyKey"], "")

    def test_disabled_proxy_does_not_create_chat(self) -> None:
        requests = []

        def evaluate(_script, args=None):
            if args is None:
                return _token()
            requests.append(args["path"])
            return {"status": 200, "text": '{"allow_proxy":false,"is_public":true}'}

        page = Mock(evaluate=evaluate, url="https://janitorai.com")
        context = Mock()
        context.cookies.return_value = []
        with self.assertRaisesRegex(ValueError, "отключил"):
            janitor_proxy.extract_definition(page, context, "abc")
        self.assertEqual(requests, ["/mb/characters/abc"])
