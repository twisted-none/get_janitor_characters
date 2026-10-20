import asyncio
import os
from pathlib import Path
import tempfile
from time import monotonic
import unittest
from unittest.mock import patch

import download_worker


class WorkerTests(unittest.IsolatedAsyncioTestCase):
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
