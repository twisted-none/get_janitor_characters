import ast
import asyncio
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from download_flow import DownloadFailed, DownloadResult, DownloadSource, remove_download
from download_status import DownloadStatus, active_status, queue_status
from download_queue import DownloadQueue, QueueFull
import download_trace as trace


class FakeTelegramError(Exception):
    pass


class DownloadHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8"))
        handler = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                       and node.name == "handle_link")
        handler.decorator_list = []
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "CHARACTER_URL_RE" for t in node.targets))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, "character.png")
        self.path.write_bytes(b"PNG test fixture")
        self.queue_msg = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        self.wait_msg = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())
        self.message = SimpleNamespace(
            text="https://janitorai.com/characters/abc", from_user=SimpleNamespace(id=7),
            answer=AsyncMock(side_effect=[self.queue_msg, self.wait_msg]), answer_document=AsyncMock(),
        )
        self.scope = {
            "re": re, "asyncio": asyncio, "logging": logging, "os": os, 'trace': trace,
            "CHARACTER_URL_RE": ast.literal_eval(assignment.value),
            "DownloadStatus": DownloadStatus, "active_status": active_status,
            "queue_status": queue_status, "remove_download": remove_download,
            "download_queue": DownloadQueue(), "QueueFull": QueueFull, "pending_downloads": set(),
            "DOWNLOAD_CONCURRENCY": 1, "DOWNLOAD_TIMEOUT_SECONDS": 5,
            "_start_download": AsyncMock(return_value=1), "_finish_download": AsyncMock(),
            "_notify_admin": AsyncMock(), "_support_keyboard": lambda: None,
            "FSInputFile": lambda path, filename: (path, filename),
            "TelegramAPIError": FakeTelegramError,
            "DownloadFailed": DownloadFailed,
        }
        async def deliver_fixture(message, result, identifier, mode):
            await message.answer_document(document=(result.file_path, identifier + '.png'), caption=result.caption)
        self.scope['deliver'] = deliver_fixture
        code = "from __future__ import annotations\n" + ast.unparse(ast.Module(body=[handler], type_ignores=[]))
        exec(compile(code, "main.py", "exec"), self.scope)
        original = self.scope['handle_link']
        self.preferences = SimpleNamespace(get_lorebook_mode=lambda _: 'separate')
        async def with_preferences(message, store):
            await original(message, store or self.preferences)
        self.scope['handle_link'] = with_preferences

    def configure_download(self, source: DownloadSource, complete: bool = True) -> None:
        async def download(_character_id: str, *, source: str, on_progress: DownloadStatus) -> DownloadResult:
            for stage in DownloadSource:
                await on_progress(stage)
                if stage == selected:
                    break
            return DownloadResult(str(self.path), selected, complete, llm_available=False)

        selected = source
        self.scope["parser"] = SimpleNamespace(download_card=AsyncMock(side_effect=download))

    async def test_datacat_sends_no_caption_and_clears_status(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        await self.scope["handle_link"](self.message, SimpleNamespace(get_lorebook_mode=lambda _: 'separate'))
        self.assertIsNone(self.message.answer_document.await_args.kwargs["caption"])
        self.assertEqual(self.message.answer.await_count, 2)
        self.queue_msg.delete.assert_awaited_once()
        self.assertEqual(self.wait_msg.edit_text.await_count, 2)
        self.assertIn("DataCat", self.wait_msg.edit_text.await_args_list[0].args[0])
        self.assertIn("100%", self.wait_msg.edit_text.await_args_list[-1].args[0])
        self.wait_msg.delete.assert_awaited_once()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.scope["pending_downloads"])

    async def test_proxy_uses_one_status_message_and_keeps_source_notice(self) -> None:
        self.configure_download(DownloadSource.PROXY)
        await self.scope["handle_link"](self.message, None)
        self.assertEqual(self.message.answer.await_count, 2)
        texts = [call.args[0] for call in self.wait_msg.edit_text.await_args_list]
        self.assertEqual(len(texts), 4)
        self.assertIn("proxy", self.message.answer_document.await_args.kwargs["caption"])

    async def test_localized_link_downloads_the_character_not_the_locale(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        self.message.text = "https://janitorai.com/ru/characters/abc_character-eri"
        await self.scope["handle_link"](self.message, None)
        download = self.scope["parser"].download_card
        download.assert_awaited_once()
        self.assertEqual(download.await_args.args, ("abc",))
        self.assertEqual(download.await_args.kwargs["source"], "janitorai")
        self.message.answer_document.assert_awaited_once()

    async def test_encoded_character_slug_downloads_using_only_uuid(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        identifier = "6460e31a-aa31-418f-8571-841afe8921e9"
        self.message.text = (f"https://janitorai.com/characters/{identifier}"
                             "_character-frederick-%E1%81%84%E1%81%83-twisted-disney")
        await self.scope["handle_link"](self.message, None)
        download = self.scope["parser"].download_card
        download.assert_awaited_once()
        self.assertEqual(download.await_args.args, (identifier,))
        self.assertEqual(download.await_args.kwargs["source"], "janitorai")
        self.message.answer_document.assert_awaited_once()

    async def test_send_failure_cleans_file_and_records_failure(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        self.message.answer_document.side_effect = FakeTelegramError("send failed")
        await self.scope["handle_link"](self.message, None)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.scope["_finish_download"].await_args.kwargs["success"])
        self.scope["_notify_admin"].assert_awaited_once()
        self.wait_msg.delete.assert_not_awaited()

    async def test_failure_to_delete_status_does_not_mark_sent_card_failed(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        self.wait_msg.delete.side_effect = FakeTelegramError("already deleted")
        await self.scope["handle_link"](self.message, None)
        self.scope["_finish_download"].assert_awaited_once_with(self.preferences, 1, success=True)
        self.scope["_notify_admin"].assert_not_awaited()

    async def test_queue_message_is_replaced_when_slot_opens(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        slot = self.scope["download_queue"].slot(99)
        await slot.__aenter__()
        task = asyncio.create_task(self.scope["handle_link"](self.message, None))
        await asyncio.sleep(0)
        self.assertIn("0%", self.message.answer.await_args.args[0])
        self.assertIn("ожидает", self.message.answer.await_args.args[0])
        await slot.__aexit__(None, None, None)
        await task
        self.assertEqual(self.message.answer.await_count, 2)
        self.queue_msg.delete.assert_awaited_once()
        self.assertIn("скачивается", self.message.answer.await_args.args[0])
        self.wait_msg.delete.assert_awaited_once()

    async def test_final_error_does_not_mention_llm(self) -> None:
        self.scope["parser"] = SimpleNamespace(download_card=AsyncMock(side_effect=DownloadFailed()))
        await self.scope["handle_link"](self.message, None)
        self.assertIn("Не удалось получить карточку", self.wait_msg.edit_text.await_args.args[0])
        self.assertNotIn("LLM", self.wait_msg.edit_text.await_args.args[0])
        self.message.answer_document.assert_not_awaited()
