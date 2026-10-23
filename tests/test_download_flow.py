import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from download_flow import Candidate, DownloadFailed, DownloadSource, run_download_flow
from download_errors import SourceError


class DownloadFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.status = AsyncMock()

    def candidate(self, name: str, complete: bool = True) -> Candidate:
        path = Path(self.temp.name, name + ".png")
        path.write_bytes(b"test fixture")
        return Candidate(str(path), complete)

    async def test_datacat_success_stops_before_other_sources(self) -> None:
        first = self.candidate("datacat")
        stages = {source: AsyncMock(return_value=first) for source in DownloadSource}
        result = await run_download_flow(stages, self.status)
        self.assertEqual(result.source, DownloadSource.DATACAT)
        self.assertIsNone(result.caption)
        self.assertEqual(result.file_path, first.file_path)
        for source in list(DownloadSource)[1:]:
            stages[source].assert_not_awaited()
        self.assertEqual([call.args[0] for call in self.status.await_args_list], [DownloadSource.DATACAT])

    async def test_incomplete_datacat_falls_through_to_janny(self) -> None:
        incomplete = self.candidate("datacat", complete=False)
        stages = {
            DownloadSource.DATACAT: AsyncMock(return_value=incomplete),
            DownloadSource.JANNYAI: AsyncMock(return_value=self.candidate("janny")),
            DownloadSource.PROXY: AsyncMock(),
        }
        result = await run_download_flow(stages, self.status)
        self.assertEqual(result.source, DownloadSource.JANNYAI)
        self.assertFalse(os.path.exists(incomplete.file_path))
        stages[DownloadSource.PROXY].assert_not_awaited()

    async def test_errors_fall_through_in_order_to_proxy(self) -> None:
        stages = {
            DownloadSource.DATACAT: AsyncMock(side_effect=ValueError("not found")),
            DownloadSource.JANNYAI: AsyncMock(side_effect=ValueError("missing definition")),
            DownloadSource.PROXY: AsyncMock(return_value=self.candidate("proxy")),
            DownloadSource.LLM: AsyncMock(),
        }
        result = await run_download_flow(stages, self.status)
        self.assertEqual(result.source, DownloadSource.PROXY)
        self.assertIn("proxy", result.caption)
        stages[DownloadSource.LLM].assert_not_awaited()
        self.assertEqual([call.args[0] for call in self.status.await_args_list], list(DownloadSource)[:3])

    async def test_llm_success_is_explicitly_reconstructed(self) -> None:
        stages = {source: AsyncMock(side_effect=ValueError("unavailable")) for source in DownloadSource}
        stages[DownloadSource.LLM] = AsyncMock(return_value=self.candidate("reconstructed"))
        result = await run_download_flow(stages, self.status)
        self.assertEqual(result.source, DownloadSource.LLM)
        self.assertTrue(result.reconstructed)
        self.assertIn("восстановлено", result.caption)

    async def test_no_definition_returns_first_public_card_with_notice(self) -> None:
        first = self.candidate("public_datacat", complete=False)
        second = self.candidate("public_janny", complete=False)
        stages = {
            DownloadSource.DATACAT: AsyncMock(return_value=first),
            DownloadSource.JANNYAI: AsyncMock(return_value=second),
            DownloadSource.PROXY: AsyncMock(side_effect=ValueError("disabled")),
        }
        result = await run_download_flow(stages, self.status)
        self.assertFalse(result.definition_available)
        self.assertEqual(result.file_path, first.file_path)
        self.assertEqual(result.caption, "Определение получить не удалось; PNG содержит только доступные поля.")
        self.assertTrue(os.path.exists(first.file_path))
        self.assertFalse(os.path.exists(second.file_path))

    async def test_no_downloadable_card_raises(self) -> None:
        stages = {source: AsyncMock(side_effect=ValueError("unavailable")) for source in DownloadSource}
        with self.assertRaisesRegex(ValueError, "получить карточку"):
            await run_download_flow(stages, self.status)

    async def test_source_diagnostics_have_no_secret_messages_or_llm_error(self) -> None:
        stages = {
            DownloadSource.DATACAT: AsyncMock(side_effect=SourceError("http_error", "fixture-secret", http_status=403)),
            DownloadSource.JANNYAI: AsyncMock(side_effect=PermissionError("fixture-secret")),
            DownloadSource.PROXY: AsyncMock(side_effect=TimeoutError("fixture-secret")),
        }
        with self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(DownloadFailed) as caught:
                await run_download_flow(stages, self.status)
        self.assertEqual(caught.exception.diagnostics["datacat"]["http_status"], 403)
        self.assertEqual(caught.exception.diagnostics["jannyai"]["code"], "permission_denied")
        self.assertNotIn("fixture-secret", repr(caught.exception.diagnostics))
        self.assertNotIn("fixture-secret", "\n".join(logs.output))
        self.assertNotIn("LLM", str(caught.exception))
        self.assertIn("code=http_error", logs.output[0])

    async def test_cancel_does_not_start_later_sources_and_cleans_partial(self) -> None:
        first = self.candidate("partial", complete=False)
        stages = {
            DownloadSource.DATACAT: AsyncMock(return_value=first),
            DownloadSource.JANNYAI: AsyncMock(side_effect=asyncio.CancelledError),
            DownloadSource.PROXY: AsyncMock(),
        }
        with self.assertRaises(asyncio.CancelledError):
            await run_download_flow(stages, self.status)
        self.assertFalse(os.path.exists(first.file_path))
        stages[DownloadSource.PROXY].assert_not_awaited()

    async def test_notification_error_does_not_restart_source(self) -> None:
        self.status.side_effect = RuntimeError("Telegram unavailable")
        stages = {DownloadSource.DATACAT: AsyncMock(return_value=self.candidate("datacat"))}
        result = await run_download_flow(stages, self.status)
        self.assertEqual(result.source, DownloadSource.DATACAT)
        stages[DownloadSource.DATACAT].assert_awaited_once()
