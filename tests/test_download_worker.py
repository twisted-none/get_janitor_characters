import asyncio
import os
from pathlib import Path
import tempfile
import json
import sys
from time import monotonic
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import download_worker

from download_errors import SourceError


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_are_relayed_even_when_worker_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, 'worker.py')
            script.write_text("import sys,json\nfrom pathlib import Path\n"
                "root=Path(sys.argv[-1]).parent\n"
                "(root/'events.jsonl').write_text(json.dumps({'event':'failed','operation':'login_password',"
                "'code':'timeout','message':'fixture-secret','http_status':403})+'\\n')\n"
                "raise SystemExit(1)\n", encoding='utf-8')
            with patch.object(download_worker, '__file__', str(script)), self.assertLogs(level='INFO') as logs:
                with self.assertRaises(SourceError):
                    await download_worker.run_isolated('fixture', 'proxy', timeout=5)
            output = '\n'.join(logs.output)
            self.assertIn('login_password', output)
            self.assertIn('403', output)
            self.assertNotIn('fixture-secret', output)

    def test_worker_main_serializes_only_safe_error_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parser = Mock()
            parser._download_candidate_sync.side_effect = PermissionError("fixture-secret")
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(sys.modules, {"parser": parser}), patch.object(
                    sys, "argv", ["worker", "fixture", "datacat", str(root / "result.json")]
                ):
                    self.assertEqual(download_worker.main(), 1)
            finally:
                os.chdir(previous)
            result = (root / "result.json").read_text()
            self.assertEqual(json.loads(result)["error"]["code"], "permission_denied")
            self.assertNotIn("fixture-secret", result)
    async def test_http_failure_crosses_process_boundary_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "worker.py")
            script.write_text(
                "import sys,json\nfrom pathlib import Path\n"
                "Path(sys.argv[-1]).write_text(json.dumps({'error':{'code':'http_error',"
                "'http_status':403,'error_type':'ValueError','message':'fixture-secret'}}))\n"
                "raise SystemExit(1)\n", encoding="utf-8")
            with patch.object(download_worker, "__file__", str(script)):
                with self.assertRaises(SourceError) as caught:
                    await download_worker.run_isolated("fixture", "datacat", timeout=5)
        self.assertEqual(caught.exception.code, "http_error")
        self.assertEqual(caught.exception.http_status, 403)
        self.assertNotIn("fixture-secret", str(caught.exception))

    async def test_worker_crash_without_json_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "worker.py")
            script.write_text("raise SystemExit(7)\n", encoding="utf-8")
            with patch.object(download_worker, "__file__", str(script)):
                with self.assertRaises(SourceError) as caught:
                    await download_worker.run_isolated("fixture", "proxy", timeout=5)
        self.assertEqual(caught.exception.code, "worker_failed")
        self.assertEqual(caught.exception.exit_code, 7)
    async def test_hung_process_is_terminated_at_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "worker.py")
            script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
            started = monotonic()
            with patch.object(download_worker, "__file__", str(script)):
                with self.assertRaises(asyncio.TimeoutError):
                    await download_worker.run_isolated("fixture", "datacat", timeout=0.2)
            self.assertLess(monotonic() - started, 3)

    async def test_bot_and_database_credentials_do_not_reach_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, "worker.py")
            script.write_text(
                "import os,sys,json\nfrom pathlib import Path\n"
                "assert 'BOT_TOKEN' not in os.environ and 'DATABASE_URL' not in os.environ\n"
                "result=Path(sys.argv[-1]); card=result.parent/'test.png'\ncard.write_bytes(b'fixture')\n"
                "result.write_text(json.dumps({'file_path':str(card),'definition_available':True}))\n", encoding="utf-8")
            with patch.object(download_worker, "__file__", str(script)), patch.dict(os.environ, {"BOT_TOKEN": "fake", "DATABASE_URL": "fake"}):
                result = await download_worker.run_isolated("fixture", "datacat", timeout=5)
            try:
                self.assertTrue(Path(result.file_path).is_file())
            finally:
                Path(result.file_path).unlink(missing_ok=True)
