import asyncio
import threading
from types import SimpleNamespace
import unittest

from metrics import _metrics


class MetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_runs_off_loop_and_is_cached(self):
        threads = []
        snapshot = SimpleNamespace(started_users=0, active_users_30d=0, download_attempts=0,
                                   successful_downloads=0, success_ratio=0, users=[], top_characters=[])
        def get_snapshot():
            threads.append(threading.get_ident())
            return snapshot
        request = SimpleNamespace(app={"analytics_store": SimpleNamespace(snapshot=get_snapshot)})
        first, second = await asyncio.gather(_metrics(request), _metrics(request))
        self.assertEqual(first.text, second.text)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())
