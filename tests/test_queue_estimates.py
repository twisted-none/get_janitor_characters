import asyncio
import unittest
from unittest.mock import patch

from download_queue import DownloadQueue


class QueueEstimateTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_status_callback_does_not_extend_queue_timeout(self):
        queue = DownloadQueue(wait_timeout=.03)

        async def slow_callback(estimate):
            await asyncio.sleep(5)

        async with queue.slot(1):
            async def waiting():
                async with queue.slot(2, on_queued=slow_callback):
                    self.fail('A timed out job must not acquire a slot')

            task = asyncio.create_task(waiting())
            done, _ = await asyncio.wait({task}, timeout=.3)
            try:
                self.assertIn(task, done)
                with self.assertRaises(asyncio.TimeoutError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(queue.waiting, 0)
        self.assertEqual(queue._active, 0)

    async def test_waiters_refresh_on_completion_without_waiting_for_periodic_tick(self):
        queue = DownloadQueue()
        gates = [asyncio.Event() for _ in range(3)]
        estimates = [[], []]
        updates = [asyncio.Event(), asyncio.Event()]

        async def notify(index, estimate):
            estimates[index].append(estimate)
            updates[index].set()

        async def job(index):
            callback = None if index == 0 else lambda estimate: notify(index - 1, estimate)
            async with queue.slot(index, on_queued=callback, update_interval=999):
                await gates[index].wait()

        tasks = [asyncio.create_task(job(0))]
        await asyncio.sleep(0)
        tasks += [asyncio.create_task(job(index)) for index in (1, 2)]
        try:
            await asyncio.wait_for(updates[1].wait(), 1)
            self.assertEqual(estimates[1][-1].position, 3)
            self.assertGreater(estimates[1][-1].estimated_wait_seconds, 0)
            updates[1].clear()
            gates[0].set()
            await asyncio.wait_for(updates[1].wait(), 1)
            self.assertEqual(estimates[1][-1].position, 2)
            self.assertLess(estimates[1][-1].estimated_wait_seconds,
                            estimates[1][0].estimated_wait_seconds)
        finally:
            for gate in gates:
                gate.set()
            await asyncio.gather(*tasks)

    async def test_estimates_follow_five_job_rotation_instead_of_enqueue_order(self):
        queue = DownloadQueue()
        gate = asyncio.Event()
        snapshots = {}
        ready = asyncio.Event()

        async def queued(name, estimate):
            snapshots[name] = estimate
            if len(snapshots) == 7:
                ready.set()

        async def job(user, name):
            async with queue.slot(user, on_queued=lambda estimate: queued(name, estimate)):
                await gate.wait()

        tasks = [asyncio.create_task(job(1, 'active'))]
        await asyncio.sleep(0)
        tasks += [asyncio.create_task(job(1, f'a{index}')) for index in range(6)]
        tasks.append(asyncio.create_task(job(2, 'b')))
        try:
            await asyncio.wait_for(ready.wait(), 1)
            self.assertEqual(snapshots['b'].position, 6)
            self.assertGreater(snapshots['a4'].position, snapshots['b'].position)
        finally:
            gate.set()
            await asyncio.gather(*tasks)

    async def test_parallel_slots_use_earliest_available_slot_for_eta(self):
        with patch('download_queue.monotonic', return_value=100):
            queue = DownloadQueue(concurrency=2)
            gate = asyncio.Event()
            snapshot = []
            notified = asyncio.Event()

            async def callback(estimate):
                snapshot.append(estimate)
                notified.set()

            async def job(user, callback=None):
                async with queue.slot(user, on_queued=callback):
                    await gate.wait()

            tasks = [asyncio.create_task(job(user)) for user in (1, 2)]
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(job(3, callback)))
            try:
                await asyncio.wait_for(notified.wait(), 1)
                self.assertEqual(snapshot[-1].position, 3)
                self.assertEqual(snapshot[-1].estimated_wait_seconds, 120)
            finally:
                gate.set()
                await asyncio.gather(*tasks)

    async def test_cancelled_waiter_disappears_from_other_users_positions(self):
        queue = DownloadQueue()
        snapshots = []
        updated = asyncio.Event()
        gate = asyncio.Event()

        async def callback(estimate):
            snapshots.append(estimate)
            updated.set()

        async def job(user, callback=None):
            async with queue.slot(user, on_queued=callback, update_interval=999):
                await gate.wait()

        tasks = [asyncio.create_task(job(1))]
        await asyncio.sleep(0)
        tasks += [asyncio.create_task(job(2)), asyncio.create_task(job(3, callback))]
        try:
            await asyncio.wait_for(updated.wait(), 1)
            self.assertEqual(snapshots[-1].position, 3)
            updated.clear()
            tasks[1].cancel()
            await asyncio.gather(tasks[1], return_exceptions=True)
            await asyncio.wait_for(updated.wait(), 1)
            self.assertEqual(snapshots[-1].position, 2)
        finally:
            gate.set()
            await asyncio.gather(*tasks, return_exceptions=True)
