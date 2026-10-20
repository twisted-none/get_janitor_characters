import asyncio
import unittest

from download_queue import DownloadQueue, QueueFull


class DownloadQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_jobs_then_other_user(self):
        queue = DownloadQueue()
        order = []
        gate = asyncio.Event()

        async def job(user, first=False):
            async with queue.slot(user):
                order.append(user)
                if first:
                    await gate.wait()

        tasks = [asyncio.create_task(job(1, True))]
        await asyncio.sleep(0)
        tasks += [asyncio.create_task(job(1)) for _ in range(9)]
        tasks += [asyncio.create_task(job(2)) for _ in range(6)]
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(order[:10], [1] * 5 + [2] * 5)
        self.assertEqual(order.count(1), 10)
        self.assertEqual(order.count(2), 6)

    async def test_single_user_has_no_five_job_limit(self):
        queue = DownloadQueue()
        for _ in range(17):
            async with queue.slot(1):
                pass
        self.assertEqual(queue.waiting, 0)

    async def test_three_users_rotate_without_starvation(self):
        queue = DownloadQueue()
        order = []
        gate = asyncio.Event()
        async def job(user, first=False):
            async with queue.slot(user):
                order.append(user)
                if first:
                    await gate.wait()
        tasks = [asyncio.create_task(job(1, True))]
        await asyncio.sleep(0)
        for user in (1, 2, 3):
            tasks += [asyncio.create_task(job(user)) for _ in range(10)]
        await asyncio.sleep(0.01)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(order[:15], [1] * 5 + [2] * 5 + [3] * 5)

    async def test_cancelled_waiter_does_not_leak_capacity(self):
        queue = DownloadQueue(capacity=1)
        async with queue.slot(1):
            async def waiting():
                async with queue.slot(2):
                    pass
            task = asyncio.create_task(waiting())
            await asyncio.sleep(0.01)
            with self.assertRaises(QueueFull):
                async with queue.slot(3):
                    pass
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with queue.slot(3):
            self.assertEqual(queue.waiting, 0)
