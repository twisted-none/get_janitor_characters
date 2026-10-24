import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from download_flow import DownloadProgress, DownloadPhase, DownloadSource
from download_status import DownloadStatus, queue_status
from download_queue import QueueEstimate
from download_worker import run_isolated
import download_worker


class LiveProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_title_reaches_status_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, 'worker.py')
            script.write_text(
                "import sys,json,time\nfrom pathlib import Path\nroot=Path(sys.argv[-1]).parent\n"
                "(root/'title.json').write_text(json.dumps('Тест <b>карточки</b>'))\n"
                "(root/'events.jsonl').write_text(json.dumps({'event':'started','operation':'download_image'})+'\\n')\n"
                "time.sleep(.6)\ncard=root/'card.png';card.write_bytes(b'fixture')\n"
                "(root/'result.json').write_text(json.dumps({'file_path':str(card),'definition_available':True}))\n")
            callback = AsyncMock()
            with patch.object(download_worker, '__file__', str(script)):
                result = await run_isolated('fixture', 'jannyai', on_progress=callback)
            try:
                self.assertEqual(callback.await_args.args[0].title, 'Тест <b>карточки</b>')
            finally:
                Path(result.file_path).unlink()

    async def test_title_is_kept_when_sources_change_and_while_sending(self):
        edit = AsyncMock()
        status = DownloadStatus(edit, title='slug fallback', min_interval=0)
        await status(DownloadProgress(DownloadSource.DATACAT, DownloadPhase.DOWNLOADING, 'Имя <>&'))
        await status(DownloadSource.JANNYAI)
        await status.sending()
        self.assertTrue(all('Карточка: Имя <>&' in c.args[0] for c in edit.await_args_list))

    async def test_nonisolated_datacat_progress_uses_the_same_source_contract(self):
        from io import BytesIO
        from PIL import Image
        import parser
        import download_trace as trace

        image = BytesIO()
        Image.new('RGB', (2, 2)).save(image, format='PNG')
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'card.png')

            def download(identifier):
                with trace.operation('download_image'):
                    pass
                parser._write_chara_png(image.getvalue(), {'spec': 'chara_card_v2', 'data': {
                    'name': 'Ada', 'description': 'Definition', 'first_mes': 'Hello',
                }}, path)
                return path

            progress = AsyncMock()
            with patch.object(parser, '_download_datacat_character_sync', side_effect=download), \
                    patch.object(parser, '_download_janny_character_sync') as janny:
                result = await parser.download_card('fixture', isolated=False, on_progress=progress)
            self.assertEqual(result.source, DownloadSource.DATACAT)
            self.assertEqual([call.args[0] for call in progress.await_args_list], [
                DownloadSource.DATACAT, DownloadProgress(DownloadSource.DATACAT, DownloadPhase.DOWNLOADING),
            ])
            janny.assert_not_called()

    def test_worker_detects_download_after_many_network_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            events = [{'event': 'response', 'operation': 'http'}] * 600
            events.append({'event': 'started', 'operation': 'download_image'})
            path.write_text(''.join(json.dumps(event) + '\n' for event in events))
            self.assertTrue(download_worker._download_has_started(path))

    async def test_checking_and_downloading_have_distinct_source_texts(self):
        edit = AsyncMock()
        status = DownloadStatus(edit, min_interval=0)
        for source, name in ((DownloadSource.DATACAT, 'DataCat'), (DownloadSource.JANNYAI, 'JannyAI'),
                             (DownloadSource.PROXY, 'proxy')):
            await status(source)
            self.assertIn('Проверяю', edit.await_args.args[0])
            self.assertIn(name, edit.await_args.args[0])
            await status(DownloadProgress(source, DownloadPhase.DOWNLOADING))
            self.assertIn('Скачиваю', edit.await_args.args[0])
            self.assertIn(name, edit.await_args.args[0])

    async def test_queue_status_shows_position_and_approximate_wait(self):
        text = queue_status(QueueEstimate(3, 5, 121))
        self.assertIn('Место в очереди: 3', text)
        self.assertIn('Примерное ожидание:', text)
        self.assertIn('3 мин', text)
        self.assertIn('0%', text)

    async def test_worker_reports_downloading_before_result_is_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, 'worker.py')
            script.write_text(
                "import sys,json,time\nfrom pathlib import Path\n"
                "root=Path(sys.argv[-1]).parent\n"
                "(root/'events.jsonl').write_text(json.dumps({'event':'started',"
                "'operation':'download_image','message':'fixture-secret'})+'\\n')\n"
                "time.sleep(.6)\nPath(__file__).with_name('ready').write_text('done')\n"
                "card=root/'card.png';card.write_bytes(b'fixture')\n"
                "(root/'result.json').write_text(json.dumps({'file_path':str(card),'definition_available':True}))\n",
                encoding='utf-8')
            events = []

            async def progress(event):
                self.assertFalse(Path(directory, 'ready').exists())
                events.append(event)

            with patch.object(download_worker, '__file__', str(script)):
                result = await run_isolated('fixture', 'jannyai', timeout=5, on_progress=progress)
            try:
                self.assertEqual(events, [DownloadProgress(DownloadSource.JANNYAI, DownloadPhase.DOWNLOADING)])
                self.assertNotIn('fixture-secret', repr(events))
            finally:
                Path(result.file_path).unlink(missing_ok=True)

    async def test_fast_worker_event_is_not_lost_and_notification_error_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, 'worker.py')
            script.write_text(
                "import sys,json\nfrom pathlib import Path\nroot=Path(sys.argv[-1]).parent\n"
                "(root/'events.jsonl').write_text(json.dumps({'event':'started','operation':'download_image'})+'\\n')\n"
                "card=root/'card.png';card.write_bytes(b'fixture')\n"
                "(root/'result.json').write_text(json.dumps({'file_path':str(card),'definition_available':True}))\n",
                encoding='utf-8')
            callback = AsyncMock(side_effect=RuntimeError('fixture-secret'))
            with patch.object(download_worker, '__file__', str(script)):
                result = await run_isolated('fixture', 'datacat', on_progress=callback)
            try:
                callback.assert_awaited_once_with(DownloadProgress(DownloadSource.DATACAT, DownloadPhase.DOWNLOADING))
            finally:
                Path(result.file_path).unlink(missing_ok=True)
