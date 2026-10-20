import asyncio
import unittest
from unittest.mock import patch
from unittest.mock import AsyncMock

from download_flow import DownloadSource
from download_status import DownloadStatus, queue_status


class DownloadStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_stage_edits_the_same_message(self) -> None:
        edit = AsyncMock()
        status = DownloadStatus(edit, min_interval=0)
        for source in DownloadSource:
            await status(source)
        self.assertEqual(edit.await_count, 4)
        texts = [call.args[0] for call in edit.await_args_list]
        for text, name in zip(texts, ("DataCat", "JannyAI", "proxy", "LLM")):
            self.assertIn(name, text)
        self.assertIn("недоступно", texts[-1])
        self.assertTrue(all("%" in text and "⚪️" in text for text in texts))
        self.assertEqual([int(text.split("%", 1)[0].rsplit(" ", 1)[-1]) for text in texts], [10, 30, 55, 80])

    async def test_repeated_status_is_not_sent_twice(self) -> None:
        edit = AsyncMock()
        status = DownloadStatus(edit, min_interval=0)
        await status(DownloadSource.DATACAT)
        await status(DownloadSource.DATACAT)
        edit.assert_awaited_once()

    async def test_telegram_error_does_not_abort_download(self) -> None:
        edit = AsyncMock(side_effect=[RuntimeError("Telegram error"), None])
        status = DownloadStatus(edit, min_interval=0)
        await status(DownloadSource.DATACAT)
        await status(DownloadSource.JANNYAI)
        self.assertEqual(edit.await_count, 2)

    async def test_queue_and_final_sending_status(self) -> None:
        self.assertIn("0%", queue_status())
        self.assertIn("ожидает", queue_status())
        edit = AsyncMock()
        status = DownloadStatus(edit, min_interval=0)
        await status(DownloadSource.DATACAT)
        await status.sending()
        self.assertIn("100%", edit.await_args.args[0])
        self.assertIn("Отправляю карточку", edit.await_args.args[0])

    async def test_timed_progress_never_goes_backwards(self) -> None:
        edit = AsyncMock()
        with patch("download_status.monotonic", return_value=100) as clock:
            status = DownloadStatus(edit, min_interval=0)
            await status(DownloadSource.PROXY)
            clock.return_value = 220
            await status.tick()
            self.assertIn("90%", edit.await_args.args[0])
            await status(DownloadSource.LLM)
            self.assertIn("90%", edit.await_args.args[0])

    async def test_tick_does_not_pass_95_before_sending(self) -> None:
        edit = AsyncMock()
        status = DownloadStatus(edit, min_interval=0)
        with patch("download_status.monotonic", return_value=status._started_at + 10000):
            await status.tick()
        self.assertIn("95%", edit.await_args.args[0])

    async def test_close_stops_late_edits(self) -> None:
        edit = AsyncMock()
        status = DownloadStatus(edit, min_interval=0)
        await status(DownloadSource.DATACAT)
        await status.close()
        await status.tick()
        await status(DownloadSource.JANNYAI)
        edit.assert_awaited_once()

    async def test_retry_after_defers_edit_without_stopping_download(self) -> None:
        class RetryAfter(Exception):
            retry_after = 0.02

        edit = AsyncMock(side_effect=[RetryAfter(), None])
        status = DownloadStatus(edit, min_interval=0)
        await status(DownloadSource.DATACAT)
        await status(DownloadSource.JANNYAI)
        self.assertEqual(edit.await_count, 1)
        await asyncio.sleep(0.03)
        await status.tick()
        self.assertEqual(edit.await_count, 2)
        self.assertIn("JannyAI", edit.await_args.args[0])
