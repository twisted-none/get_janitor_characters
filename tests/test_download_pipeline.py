import asyncio
import base64
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

import parser
from download_flow import Candidate, DownloadSource


class DownloadPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def card_file(self, name: str, data: dict) -> str:
        path = str(Path(self.temp.name, name + ".png"))
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
        parser._write_chara_png(buffer.getvalue(), {"spec": "chara_card_v2", "data": data}, path)
        return path

    def datacat_data(self, hidden: bool, definition: str = "") -> dict:
        return parser._datacat_card_from_character({
            "name": "Ada", "description": "Public profile", "first_message": "Hello",
            "showdefinition": not hidden, "personality": definition,
        })["data"]

    async def test_open_datacat_definition_stops_chain_for_both_url_hosts(self) -> None:
        for source in ("janitorai", "jannyai"):
            with self.subTest(source=source):
                path = self.card_file(source, self.datacat_data(False, "Open definition"))
                progress = AsyncMock()
                with (
                    patch.object(parser, "_download_datacat_character_sync", return_value=path),
                    patch.object(parser, "_download_janny_character_sync") as janny,
                    patch.object(parser, "_download_janitor_character_sync") as janitor,
                ):
                    result = await parser.download_card("abc", source=source, on_progress=progress, isolated=False)
                self.assertEqual(result.source, DownloadSource.DATACAT)
                self.assertIsNone(result.caption)
                janny.assert_not_called()
                janitor.assert_not_called()

    async def test_hidden_definition_present_on_datacat_stops_chain(self) -> None:
        path = self.card_file("hidden", self.datacat_data(True, "Hidden definition"))
        with (
            patch.object(parser, "_download_datacat_character_sync", return_value=path),
            patch.object(parser, "_download_janny_character_sync") as janny,
        ):
            result = await parser.download_card("abc", source="janitorai", isolated=False)
        self.assertTrue(result.definition_available)
        janny.assert_not_called()

    async def test_missing_hidden_definition_falls_through_to_janny(self) -> None:
        datacat = self.card_file("public_only", self.datacat_data(True))
        janny = self.card_file("janny", {
            "name": "Ada", "description": "Actual character definition", "first_mes": "Hello",
        })
        with (
            patch.object(parser, "_download_datacat_character_sync", return_value=datacat),
            patch.object(parser, "_download_janny_character_sync", return_value=janny),
            patch.object(parser, "_download_janitor_character_sync") as janitor,
        ):
            result = await parser.download_card("abc", source="janitorai", isolated=False)
        self.assertEqual(result.source, DownloadSource.JANNYAI)
        self.assertFalse(os.path.exists(datacat))
        janitor.assert_not_called()

    def test_datacat_public_description_is_not_definition(self) -> None:
        data = self.datacat_data(True)
        self.assertFalse(parser._card_definition_available(data))

    def test_datacat_tavern_description_can_hold_actual_definition(self) -> None:
        character = {
            "name": "Ada", "description": "Public profile", "first_message": "Hello",
            "showdefinition": False,
            "chara_card_v2_json": {"data": {
                "name": "Ada", "description": "Actual definition", "first_mes": "Hello",
            }},
        }
        card = parser._datacat_card_from_character(character)
        self.assertTrue(parser._card_definition_available(card["data"]))
        self.assertNotIn("datacat", character["chara_card_v2_json"]["data"].get("extensions", {}))

    def test_missing_datacat_does_not_trigger_cloud_extraction(self) -> None:
        with patch.object(parser, "_datacat_json") as request:
            request.side_effect = [(200, {"sessionToken": "fake-session"}), (404, {}), (404, {})]
            with self.assertRaises(ValueError):
                parser._download_datacat_character_sync("abc")
        self.assertEqual([call.args[1] for call in request.call_args_list], [
            "/api/liberator/identify", "/api/characters/abc?sourceKind=janitor", "/api/characters/abc",
        ])

    def test_janitor_credentials_are_not_used_to_log_in_to_janny(self) -> None:
        with patch.dict(os.environ, {
            "JANITORAI_EMAIL": "fake@janitor.test", "JANITORAI_PASSWORD": "fake-password",
            "JANNYAI_EMAIL": "", "JANNYAI_PASSWORD": "",
        }):
            page = Mock()
            parser._login_janny(page)
            page.goto.assert_not_called()

    def test_janny_headers_do_not_contain_janitor_tokens(self) -> None:
        context = Mock()
        context.cookies.return_value = [
            {"domain": ".janitorai.com", "name": "sb-access-token", "value": "fake-janitor"},
            {"domain": ".jannyai.com", "name": "janny-session", "value": "fake-janny"},
            {"domain": ".eviljannyai.com", "name": "bad", "value": "fake-other"},
        ]
        headers = parser._janny_headers(context, "https://jannyai.com", "FakeUA")
        self.assertEqual(headers["Cookie"], "janny-session=fake-janny")
        self.assertNotIn("Authorization", headers)

    async def test_saved_metadata_identifies_download_source(self) -> None:
        path = self.card_file("metadata", self.datacat_data(True, "Hidden definition"))
        with patch.object(parser, "_download_datacat_character_sync", return_value=path):
            await parser.download_card("abc", source="janitorai", isolated=False)
        with Image.open(path) as image:
            data = json.loads(base64.b64decode(image.info["chara"]))["data"]
        self.assertEqual(data["extensions"]["download_bot"], {
            "source": "datacat", "definition_status": "available",
        })

    def test_definition_alias_is_preserved_in_datacat_png(self) -> None:
        card = parser._datacat_card_from_character({
            "name": "Ada", "description": "Public profile", "first_message": "Hello",
            "hidden_definition": "Actual definition",
        })
        self.assertEqual(card["data"]["personality"], "Actual definition")
        self.assertTrue(parser._card_definition_available(card["data"]))

    async def test_invalid_png_from_datacat_does_not_stop_chain(self) -> None:
        invalid = Path(self.temp.name, "invalid.png")
        invalid.write_bytes(b"not a character card")
        janny = self.card_file("valid_janny", {
            "name": "Ada", "description": "Definition", "first_mes": "Hello",
        })
        with (
            patch.object(parser, "_download_datacat_character_sync", return_value=str(invalid)),
            patch.object(parser, "_download_janny_character_sync", return_value=janny),
        ):
            result = await parser.download_card("abc", isolated=False)
        self.assertEqual(result.source, DownloadSource.JANNYAI)
        self.assertFalse(invalid.exists())

    async def test_unsupported_source_is_rejected_before_network(self) -> None:
        with patch.object(parser, "_download_datacat_character_sync") as download:
            with self.assertRaisesRegex(ValueError, "Неподдерживаемый"):
                await parser.download_card("abc", source="other")
        download.assert_not_called()

    def test_datacat_response_size_is_bounded(self) -> None:
        with patch.object(parser, "fetch", side_effect=ValueError("Ответ слишком большой")) as request:
            with self.assertRaisesRegex(ValueError, "слишком большой"):
                parser._datacat_json("GET", "/api/characters/abc")
        self.assertEqual(request.call_args.kwargs["limit"], parser.JSON_RESPONSE_LIMIT)

    async def test_cancelled_worker_is_cleaned_after_it_finishes(self) -> None:
        path = self.card_file("cancelled", self.datacat_data(False, "Open"))
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker(*args: object) -> Candidate:
            started.set()
            await release.wait()
            return Candidate(path, True)

        with patch.object(parser.asyncio, "to_thread", side_effect=worker):
            task = asyncio.create_task(parser._download_candidate(lambda _: path, "abc", DownloadSource.DATACAT))
            await started.wait()
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(os.path.exists(path))
