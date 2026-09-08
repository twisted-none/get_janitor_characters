import asyncio
import base64
import json
import os
import socket
import tempfile
import time
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

import parser


def _sample_png() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, status: int, body: bytes | str, url: str = "https://cdn.example.test/ada.png") -> None:
        self.status = status
        self._body = body
        self.url = url
        self.headers = {"content-type": "image/png"}

    def body(self) -> bytes:
        return self._body if isinstance(self._body, bytes) else self._body.encode()

    def text(self) -> str:
        return self._body.decode() if isinstance(self._body, bytes) else self._body


class _FakeRequest:
    def post(self, *args: object, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(403, "<!DOCTYPE html><title>Just a moment...</title>")

    def get(self, *args: object, **kwargs: object) -> _FakeResponse:
        return _FakeResponse(200, _sample_png())


class _FakeContext:
    request = _FakeRequest()

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        pass

    def cookies(self) -> list[dict[str, object]]:
        return []

    def new_page(self) -> "_FakePage":
        return _FakePage()


class _FakePage:
    def on(self, event: str, callback: object) -> None:
        pass

    def goto(self, *args: object, **kwargs: object) -> None:
        pass

    def wait_for_load_state(self, *args: object, **kwargs: object) -> None:
        pass

    def evaluate(self, script: object, arg: object = None) -> object:
        if script == parser.API_FETCH_SCRIPT:
            return {"ok": True, "status": 403, "body": "<!DOCTYPE html><title>Just a moment...</title>"}
        if script == "navigator.userAgent":
            return "FakeUA"
        return {
            "bodyText": "Harry Potter character page",
            "meta": {},
            "scripts": [],
            "title": "Harry Potter",
        }


class _FakeBrowser:
    def new_context(self) -> _FakeContext:
        return _FakeContext()

    def close(self) -> None:
        pass


class ParserTests(unittest.TestCase):
    def test_write_chara_png_embeds_decodable_metadata(self) -> None:
        card = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {"name": "Ada"}}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "card.png")
            parser._write_chara_png(_sample_png(), card, path)

            with Image.open(path) as image:
                encoded = image.info["chara"]

        decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
        self.assertEqual(decoded, card)

    def test_to_chara_card_v2_maps_required_janitor_fields(self) -> None:
        raw = {
            "name": "Ada",
            "description": "Analytical assistant",
            "firstMessage": "Hello.",
            "avatarUrl": "https://example.test/ada.png",
            "tags": ["assistant"],
        }

        card = parser._to_chara_card_v2(raw)

        self.assertEqual(card["spec"], "chara_card_v2")
        self.assertEqual(card["data"]["name"], "Ada")
        self.assertEqual(card["data"]["description"], "Analytical assistant")
        self.assertEqual(card["data"]["first_mes"], "Hello.")
        self.assertEqual(card["data"]["extensions"]["janitorai"], raw)

    def test_datacat_character_maps_to_card_and_avatar(self) -> None:
        character = {
            "character_id": "abc",
            "creator_id": "creator-1",
            "creator_name": "AdaCreator",
            "primary_content_source_kind": "janitor",
            "personality": "Recovered definition",
            "description": "Public notes",
            "avatar": "creator_avatar.png",
            "chara_card_v2_json": {
                "spec": "chara_card_v2",
                "spec_version": "2.0",
                "data": {
                    "name": "Ada",
                    "description": "",
                    "first_mes": "Hello.",
                    "avatar": "https://cdn.example.test/ada.webp",
                    "extensions": {},
                },
            },
        }

        card = parser._datacat_card_from_character(character)

        self.assertEqual(card["data"]["name"], "Ada")
        self.assertEqual(card["data"]["description"], "Recovered definition")
        self.assertEqual(card["data"]["creator_notes"], "Public notes")
        self.assertEqual(card["data"]["extensions"]["datacat"]["id"], "abc")
        self.assertEqual(parser._datacat_image_url(character, card), "https://cdn.example.test/ada.webp")

    def test_datacat_character_falls_back_to_raw_fields(self) -> None:
        character = {
            "name": "Ada",
            "personality": "Analytical assistant",
            "description": "Creator notes",
            "scenario": "Lab",
            "first_message": "Hello.",
            "avatar": "ada.png",
        }

        card = parser._datacat_card_from_character(character)

        self.assertEqual(card["data"]["name"], "Ada")
        self.assertEqual(card["data"]["description"], "Analytical assistant")
        self.assertEqual(card["data"]["creator_notes"], "Creator notes")
        self.assertEqual(
            parser._datacat_image_url(character, card),
            f"{parser.DATACAT_JANITOR_IMAGE_BASE}ada.png",
        )
    def test_extract_character_rejects_janitor_login_ui_payload(self) -> None:
        payloads = [
            {
                "title": "moderation tools",
                "personality": "Profile option",
                "scenario": "Report workflow",
                "firstMessage": "Complete security verification",
                "completeSecurityVerification": "Complete Security Verification",
                "reportTitle": "Report content",
            }
        ]
        state = {"title": "janitor - Login", "bodyText": "Email address Sign in", "scripts": []}

        with self.assertRaisesRegex(ValueError, "cookies"):
            parser._extract_character_from_state(state, payloads, "Janitor AI")

    def test_extract_character_rejects_translation_payload_without_name(self) -> None:
        payloads = [
            {
                "characterDefinitionHeader": "Character Definition",
                "firstMessage": "First message",
                "personality": "Personality",
                "scenario": "Scenario",
                "exampleDialogs": "Example dialogs",
            }
        ]
        state = {"title": "janitor - Login", "bodyText": "Welcome back Login", "scripts": []}

        with self.assertRaisesRegex(ValueError, "cookies"):
            parser._extract_character_from_state(state, payloads, "Janitor AI")

    def test_extract_character_rejects_generic_ui_payload_with_name_label(self) -> None:
        payloads = [
            {
                "name": "Name",
                "description": "Description",
                "login": "Login",
                "logout": "Logout",
                "register": "Register",
                "selectLanguage": "Select language",
                "somethingWentWrong": "Oops! Something went wrong.",
            }
        ]
        state = {"title": "janitor - Login", "bodyText": "Welcome back Login", "scripts": []}

        with self.assertRaisesRegex(ValueError, "cookies"):
            parser._extract_character_from_state(state, payloads, "Janitor AI")

    def test_download_character_routes_by_source(self) -> None:
        with (
            patch.object(parser, "_download_datacat_character_sync", return_value="datacat.png") as datacat,
            patch.object(parser, "_download_janny_character_sync") as janny,
            patch.object(parser, "_download_janitor_character_sync") as janitor,
        ):
            self.assertEqual(asyncio.run(parser.download_character("abc", source="janitorai")), "datacat.png")
            datacat.assert_called_once_with("abc")
            janny.assert_not_called()
            janitor.assert_not_called()

        with (
            patch.object(parser, "_download_datacat_character_sync", side_effect=ValueError("not found")) as datacat,
            patch.object(parser, "_download_janny_character_sync", side_effect=ValueError("waf")) as janny,
            patch.object(parser, "_download_janitor_character_sync", return_value="janitor.png") as janitor,
        ):
            self.assertEqual(asyncio.run(parser.download_character("abc", source="janitorai")), "janitor.png")
            datacat.assert_called_once_with("abc")
            janny.assert_called_once_with("abc")
            janitor.assert_called_once_with("abc")

        with patch.object(parser, "_download_janny_character_sync", return_value="janny.png") as janny:
            self.assertEqual(asyncio.run(parser.download_character("abc", source="jannyai")), "janny.png")
            janny.assert_called_once_with("abc")

    def test_janny_api_403_falls_back_to_browser_page_state(self) -> None:
        payload = {
            "character": {
                "id": "abc",
                "name": "Harry Potter",
                "description": "Wizard",
                "firstMessage": "Hello",
                "avatarUrl": "https://cdn.example.test/harry.png",
            }
        }
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with (
            patch.object(parser, "_launch", return_value=_FakeBrowser()),
            patch.object(parser, "_login_janny"),
            patch("parser.socket.getaddrinfo", return_value=public),
            patch.object(parser, "_extract_character_from_state", return_value=payload["character"]),
        ):
            path = parser._download_janny_character_sync("abc_character-harry-potter")

        with Image.open(path) as image:
            card = json.loads(base64.b64decode(image.info["chara"]).decode("utf-8"))
        self.assertEqual(card["data"]["name"], "Harry Potter")

    def test_find_janitor_image_uses_nested_payload_and_meta(self) -> None:
        payloads = [{"character": {"assets": {"avatarUrl": "https://cdn.test/ada.png"}}}]
        self.assertEqual(
            parser._find_janitor_image({}, {"scripts": [], "meta": {}}, payloads),
            "https://cdn.test/ada.png",
        )
        self.assertEqual(
            parser._find_janitor_image({}, {"scripts": [], "meta": {"og:image": "/ada.png"}}, []),
            "/ada.png",
        )

    def test_find_janitor_image_uses_nested_image_url_container(self) -> None:
        raw = {"media": {"images": [{"url": "https://cdn.test/ada.png"}]}}

        self.assertEqual(
            parser._find_janitor_image(raw, {"scripts": [], "meta": {}}, []),
            "https://cdn.test/ada.png",
        )

    def test_find_janitor_image_uses_dom_image_fallback(self) -> None:
        raw = {"name": "Ada"}
        state = {
            "scripts": [],
            "meta": {},
            "images": [
                {"src": "https://cdn.test/logo.png", "alt": "Logo", "className": "logo", "width": 32, "height": 32},
                {
                    "src": "https://cdn.test/ada.png",
                    "alt": "Ada portrait",
                    "className": "character-image",
                    "width": 512,
                    "height": 512,
                },
            ],
        }

        self.assertEqual(parser._find_janitor_image(raw, state, []), "https://cdn.test/ada.png")

    def test_find_janitor_image_allows_protocol_relative_dom_image(self) -> None:
        raw = {"name": "Ada"}
        state = {
            "scripts": [],
            "meta": {},
            "images": [
                {"src": "/placeholder.png", "alt": "placeholder", "className": "", "width": 512, "height": 512},
                {"src": "//cdn.test/ada.webp", "alt": "Ada", "className": "avatar", "width": 512, "height": 512},
            ],
        }

        self.assertEqual(parser._find_janitor_image(raw, state, []), "//cdn.test/ada.webp")

    def test_validate_image_url_allows_https_and_blocks_private_ip(self) -> None:
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
        with patch("parser.socket.getaddrinfo", return_value=public):
            self.assertEqual(
                parser._validate_image_url("/ada.png", "https://janitorai.com/character/ada"),
                "https://janitorai.com/ada.png",
            )
        with patch("parser.socket.getaddrinfo", return_value=private):
            with self.assertRaisesRegex(ValueError, "небезопасный"):
                parser._validate_image_url("https://localhost/ada.png", "https://janitorai.com")

    def test_login_janitor_requires_complete_credentials(self) -> None:
        with patch.dict(os.environ, {"JANITORAI_EMAIL": "user@example.test", "JANITORAI_PASSWORD": ""}):
            with self.assertRaisesRegex(ValueError, "JANITORAI_EMAIL"):
                parser._login_janitor(object())

    def test_janitor_login_error_only_reports_explicit_failures(self) -> None:
        self.assertEqual(parser._janitor_login_error("Welcome back"), "")
        self.assertEqual(parser._janitor_login_error("Password"), "")
        self.assertIn("логин или пароль", parser._janitor_login_error("Invalid login credentials"))
        self.assertIn("CAPTCHA", parser._janitor_login_error("Verify you are human"))

    def test_expired_access_token_is_not_used(self) -> None:
        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": int(time.time()) - 60}).encode()
        ).decode().rstrip("=")
        self.assertFalse(parser._cookie_is_usable("sb-access-token", f"x.{payload}.x"))


if __name__ == "__main__":
    unittest.main()
