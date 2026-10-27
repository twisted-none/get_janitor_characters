import ast
import asyncio
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from secrets import token_hex

from download_flow import DownloadFailed, DownloadResult, DownloadSource, DownloadProgress, DownloadPhase, remove_download
from download_status import DownloadStatus, DownloadJob, card_label, active_status, queue_status
from download_queue import DownloadQueue, QueueFull
import download_trace as trace
from download_errors import SourceError
from download_retries import MAX_ATTEMPTS, waiting_text, EXHAUSTED_TEXT


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
        self.queue_msg = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock(), message_id=11)
        self.wait_msg = self.queue_msg
        self.message = SimpleNamespace(
            message_id=0, edit_text=AsyncMock(), delete=AsyncMock(),
            text="https://janitorai.com/characters/abc", from_user=SimpleNamespace(id=7),
            chat=SimpleNamespace(id=10),
            answer=AsyncMock(return_value=self.wait_msg), answer_document=AsyncMock(),
        )
        self.scope = {
            'MAX_ATTEMPTS': MAX_ATTEMPTS, 'waiting_text': waiting_text, 'EXHAUSTED_TEXT': EXHAUSTED_TEXT,
            'download_jobs': {}, 'DownloadJob': DownloadJob, 'card_label': card_label,
            'token_hex': token_hex,
            'InlineKeyboardMarkup': lambda **kwargs: kwargs,
            'InlineKeyboardButton': lambda **kwargs: kwargs,
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
        callback = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == 'handle_cancel_download')
        callback.decorator_list = []
        exec(compile('from __future__ import annotations\n' + ast.unparse(
            ast.Module(body=[callback], type_ignores=[])), 'main.py', 'exec'), self.scope)
        original = self.scope['handle_link']
        self.preferences = SimpleNamespace(get_lorebook_mode=lambda _: 'separate')
        async def with_preferences(message, store, **kwargs):
            return await original(message, store or self.preferences, **kwargs)
        self.scope['handle_link'] = with_preferences

    def missing_error(self, janny='not_found'):
        return DownloadFailed({'datacat': SourceError('not_found').to_payload(),
                               'jannyai': SourceError(janny).to_payload()})

    async def test_both_missing_schedule_without_holding_queue_and_can_be_cancelled(self):
        self.message.from_user.username = 'original_tag'
        self.message.from_user.full_name = 'Имя Фамилия'
        self.scope['parser'] = SimpleNamespace(download_card=AsyncMock(side_effect=self.missing_error()))
        store = SimpleNamespace(schedule=Mock(return_value=True), cancel=Mock(return_value=True), finish=Mock())
        with self.assertLogs(level='INFO') as logs:
            await self.scope['handle_link'](self.message, None, retry_store=store)
        self.assertNotIn('ERROR:', '\n'.join(logs.output))
        self.assertEqual(self.scope['download_queue']._active, 0)
        self.assertFalse(self.scope['download_jobs'])
        request = store.schedule.call_args.args[0]
        self.assertEqual(request['completed_attempts'], 1)
        self.assertEqual((request['username'], request['full_name']), ('original_tag', 'Имя Фамилия'))
        self.assertIn('30 минут', self.wait_msg.edit_text.await_args.args[0])
        self.scope['_notify_admin'].assert_not_awaited()
        callback = SimpleNamespace(data='download:cancel:' + request['token'], from_user=self.message.from_user,
                                   message=SimpleNamespace(chat=self.message.chat, message_id=11,
                                                           edit_text=AsyncMock()), answer=AsyncMock())
        await self.scope['handle_cancel_download'](callback, retry_store=store)
        store.cancel.assert_called_once_with(request['token'], 7, 10, 11)
        self.assertIn('отменена', callback.message.edit_text.await_args.args[0])

    async def test_cloudflare_does_not_claim_card_is_absent(self):
        self.scope['parser'] = SimpleNamespace(download_card=AsyncMock(side_effect=self.missing_error('challenge_required')))
        store = SimpleNamespace(schedule=Mock(), finish=Mock())
        await self.scope['handle_link'](self.message, None, retry_store=store)
        store.schedule.assert_not_called()
        self.assertNotIn('30 минут', self.wait_msg.edit_text.await_args.args[0])

    async def test_admin_redelivery_success_has_no_status_messages(self):
        self.configure_download(DownloadSource.JANNYAI)
        self.scope['deliver'] = AsyncMock(return_value=True)
        result = await self.scope['handle_link'](self.message, None, redelivery=SimpleNamespace(mode='both'))
        self.assertTrue(result)
        self.message.answer.assert_not_awaited()
        self.message.edit_text.assert_not_awaited()
        self.message.delete.assert_not_awaited()
        self.wait_msg.delete.assert_not_awaited()

    async def test_admin_redelivery_timeout_is_silent_and_does_not_alert(self):
        self.scope['parser'] = SimpleNamespace(download_card=AsyncMock(side_effect=TimeoutError()))
        result = await self.scope['handle_link'](self.message, None, redelivery=SimpleNamespace(mode='both'))
        self.assertFalse(result)
        self.message.answer.assert_not_awaited()
        self.message.edit_text.assert_not_awaited()
        self.wait_msg.edit_text.assert_not_awaited()
        self.scope['_notify_admin'].assert_not_awaited()

    async def test_admin_redelivery_failure_does_not_create_new_alert_or_delayed_job(self):
        self.scope['parser'] = SimpleNamespace(download_card=AsyncMock(side_effect=self.missing_error()))
        store = SimpleNamespace(schedule=Mock(), finish=Mock())
        await self.scope['handle_link'](self.message, None, retry_store=store,
                                       redelivery=SimpleNamespace(mode='both'))
        self.scope['_notify_admin'].assert_not_awaited()
        store.schedule.assert_not_called()
        self.message.answer.assert_not_awaited()
        self.message.edit_text.assert_not_awaited()
        self.wait_msg.edit_text.assert_not_awaited()

    async def test_database_failure_does_not_promise_a_retry(self):
        self.scope['parser'] = SimpleNamespace(download_card=AsyncMock(side_effect=self.missing_error()))
        store = SimpleNamespace(schedule=Mock(side_effect=RuntimeError('fixture')), finish=Mock())
        await self.scope['handle_link'](self.message, None, retry_store=store)
        self.assertNotIn('Проверю снова', self.wait_msg.edit_text.await_args.args[0])
        self.scope['_notify_admin'].assert_awaited_once()

    async def test_successful_retry_removes_schedule_before_delivery(self):
        self.configure_download(DownloadSource.JANNYAI)
        store = SimpleNamespace(exists=Mock(return_value=True), finish=Mock())
        self.message.message_id = 11
        self.message.edit_text = self.wait_msg.edit_text
        self.message.delete = self.wait_msg.delete

        async def deliver(*args):
            store.finish.assert_called_once_with('saved-token')

        self.scope['deliver'] = deliver
        await self.scope['handle_link'](self.message, None, retry_store=store,
                                       retry_request=dict(token='saved-token', title='Saved title', completed_attempts=1))
        self.message.answer.assert_not_awaited()
        self.assertFalse(self.path.exists())

    async def test_shutdown_keeps_in_progress_retry_for_restart(self):
        entered = asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.scope['parser'] = SimpleNamespace(download_card=download)
        store = SimpleNamespace(exists=Mock(return_value=True), finish=Mock())
        self.message.message_id = 11
        self.message.edit_text = self.wait_msg.edit_text
        task = asyncio.create_task(self.scope['handle_link'](
            self.message, None, retry_store=store,
            retry_request=dict(token='saved-token', title='Saved title', completed_attempts=1)))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        store.finish.assert_not_called()
        self.assertEqual(self.scope['download_queue']._active, 0)

    async def test_only_two_additional_attempts_and_terminal_missing_message(self):
        for completed in (1, 2):
            with self.subTest(completed=completed):
                self.scope['parser'] = SimpleNamespace(download_card=AsyncMock(side_effect=self.missing_error()))
                store = SimpleNamespace(schedule=Mock(return_value=True), exists=Mock(return_value=True), finish=Mock())
                self.message.message_id = 11
                self.message.edit_text = self.wait_msg.edit_text
                self.message.delete = self.wait_msg.delete
                request = dict(token='saved-token', title='Saved title', completed_attempts=completed)
                await self.scope['handle_link'](self.message, None, retry_store=store, retry_request=request)
                if completed == 1:
                    self.assertEqual(store.schedule.call_args.args[0]['completed_attempts'], 2)
                    self.assertTrue(store.schedule.call_args.kwargs['resumed'])
                    self.assertIn('Повтор 2 из 2', self.wait_msg.edit_text.await_args.args[0])
                else:
                    store.schedule.assert_not_called()
                    store.finish.assert_called_once_with('saved-token')
                    self.assertIn('автоматические попытки завершены', self.wait_msg.edit_text.await_args.args[0])
                self.assertIn('Saved title', self.wait_msg.edit_text.await_args.args[0])

    async def test_cancel_active_download_cleans_worker_before_releasing_slot(self):
        started, stopping, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def download(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopping.set()
                await finish.wait()

        self.scope['parser'] = SimpleNamespace(download_card=download)
        task = asyncio.create_task(self.scope['handle_link'](self.message, None))
        await asyncio.wait_for(started.wait(), 5)
        token = next(iter(self.scope['download_jobs']))
        callback = SimpleNamespace(data='download:cancel:' + token, from_user=SimpleNamespace(id=8),
                                   message=SimpleNamespace(chat=self.message.chat,
                                                           message_id=self.wait_msg.message_id), answer=AsyncMock())
        await self.scope['handle_cancel_download'](callback)
        self.assertFalse(stopping.is_set())
        callback.from_user.id = 7
        await self.scope['handle_cancel_download'](callback)
        await asyncio.wait_for(stopping.wait(), 5)
        self.assertEqual(self.scope['download_queue']._active, 1)
        await self.scope['handle_cancel_download'](callback)  # A second click must not interrupt cleanup.
        finish.set()
        await asyncio.wait_for(task, 5)
        self.assertFalse(self.scope['download_jobs'])
        self.assertEqual(self.scope['download_queue']._active, 0)
        self.message.answer_document.assert_not_awaited()
        self.assertIn('отменена', self.wait_msg.edit_text.await_args.args[0])
        self.scope['_notify_admin'].assert_not_awaited()

    async def test_cancel_queued_download_does_not_cancel_active_job(self):
        self.configure_download(DownloadSource.DATACAT)
        async with self.scope['download_queue'].slot(99):
            task = asyncio.create_task(self.scope['handle_link'](self.message, None))
            for _ in range(100):
                if self.scope['download_jobs']:
                    break
                await asyncio.sleep(.01)
            token = next(iter(self.scope['download_jobs']))
            callback = SimpleNamespace(data='download:cancel:' + token, from_user=self.message.from_user,
                                       message=SimpleNamespace(chat=self.message.chat,
                                                               message_id=self.wait_msg.message_id), answer=AsyncMock())
            await self.scope['handle_cancel_download'](callback)
            await asyncio.wait_for(task, 5)
            self.assertEqual(self.scope['download_queue']._active, 1)
            self.scope['parser'].download_card.assert_not_awaited()
        self.message.answer_document.assert_not_awaited()

    def configure_download(self, source: DownloadSource, complete: bool = True) -> None:
        async def download(_character_id: str, *, source: str, on_progress: DownloadStatus) -> DownloadResult:
            for stage in DownloadSource:
                await on_progress(stage)
                if stage == selected:
                    await on_progress(DownloadProgress(stage, DownloadPhase.DOWNLOADING))
                    break
            return DownloadResult(str(self.path), selected, complete, llm_available=False)

        selected = source
        self.scope["parser"] = SimpleNamespace(download_card=AsyncMock(side_effect=download))

    async def test_datacat_sends_no_caption_and_clears_status(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        await self.scope["handle_link"](self.message, SimpleNamespace(get_lorebook_mode=lambda _: 'separate'))
        self.assertIsNone(self.message.answer_document.await_args.kwargs["caption"])
        self.assertEqual(self.message.answer.await_count, 1)
        self.assertEqual(self.wait_msg.edit_text.await_count, 3)
        self.assertIn("DataCat", self.wait_msg.edit_text.await_args_list[0].args[0])
        self.assertIn('Проверяю', self.wait_msg.edit_text.await_args_list[0].args[0])
        self.assertIn('Скачиваю с DataCat', self.wait_msg.edit_text.await_args_list[1].args[0])
        self.assertIn("100%", self.wait_msg.edit_text.await_args_list[-1].args[0])
        self.wait_msg.delete.assert_awaited_once()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.scope["pending_downloads"])

    async def test_proxy_uses_one_status_message_and_keeps_source_notice(self) -> None:
        self.configure_download(DownloadSource.PROXY)
        await self.scope["handle_link"](self.message, None)
        self.assertEqual(self.message.answer.await_count, 1)
        texts = [call.args[0] for call in self.wait_msg.edit_text.await_args_list]
        self.assertEqual(len(texts), 5)
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

    async def test_queue_message_shows_position_and_is_reused_when_slot_opens(self) -> None:
        self.configure_download(DownloadSource.DATACAT)
        slot = self.scope["download_queue"].slot(99)
        await slot.__aenter__()
        task = asyncio.create_task(self.scope["handle_link"](self.message, None))
        for _ in range(20):
            if self.queue_msg.edit_text.await_count:
                break
            await asyncio.sleep(.01)
        self.assertIn("0%", self.message.answer.await_args.args[0])
        self.assertIn('Место в очереди: 2', self.queue_msg.edit_text.await_args.args[0])
        self.assertIn('Примерное ожидание:', self.queue_msg.edit_text.await_args.args[0])
        await slot.__aexit__(None, None, None)
        await task
        self.assertEqual(self.message.answer.await_count, 1)
        self.wait_msg.delete.assert_awaited_once()

    async def test_queue_slot_is_held_until_card_delivery_finishes(self):
        self.configure_download(DownloadSource.DATACAT)
        sending = asyncio.Event()
        release = asyncio.Event()

        async def deliver(*args):
            sending.set()
            await release.wait()

        self.scope['deliver'] = deliver
        task = asyncio.create_task(self.scope['handle_link'](self.message, None))
        try:
            await asyncio.wait_for(sending.wait(), 5)
            self.assertEqual(self.scope['download_queue']._active, 1)
            token = next(iter(self.scope['download_jobs']))
            callback = SimpleNamespace(data='download:cancel:' + token, from_user=self.message.from_user,
                                       message=SimpleNamespace(chat=self.message.chat,
                                                               message_id=self.wait_msg.message_id), answer=AsyncMock())
            await self.scope['handle_cancel_download'](callback)
            self.assertFalse(task.cancelling())
            self.assertIsNone(self.wait_msg.edit_text.await_args.kwargs['reply_markup'])
        finally:
            release.set()
            await task
        self.assertEqual(self.scope['download_queue']._active, 0)
        await self.scope['handle_cancel_download'](callback)
        self.assertIn('завершена', callback.answer.await_args.args[0])

    async def test_final_error_does_not_mention_llm(self) -> None:
        self.scope["parser"] = SimpleNamespace(download_card=AsyncMock(side_effect=DownloadFailed()))
        await self.scope["handle_link"](self.message, None)
        self.assertIn("Не удалось получить карточку", self.wait_msg.edit_text.await_args.args[0])
        self.assertNotIn("LLM", self.wait_msg.edit_text.await_args.args[0])
        self.message.answer_document.assert_not_awaited()
