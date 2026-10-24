"""Bounded per-user queue with a five-job round-robin quantum."""

import asyncio
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import heapq
import logging
from time import monotonic


@dataclass(frozen=True)
class QueueEstimate:
    position: int
    total: int
    estimated_wait_seconds: float


QueueCallback = Callable[[QueueEstimate], Awaitable[None]]


class QueueFull(ValueError):
    pass


class DownloadQueue:
    def __init__(self, concurrency: int = 1, capacity: int = 1000, quantum: int = 5,
                 wait_timeout: float = 3600) -> None:
        if min(concurrency, capacity, quantum, wait_timeout) <= 0:
            raise ValueError("Queue limits must be positive")
        self.concurrency = concurrency
        self.capacity = capacity
        self.quantum = quantum
        self.wait_timeout = wait_timeout
        self._users: OrderedDict[int, deque[asyncio.Future]] = OrderedDict()
        self._active = 0
        self._last_user: int | None = None
        self._streak = 0
        self._active_jobs: dict[asyncio.Future, float] = {}
        self._updates: dict[asyncio.Future, asyncio.Event] = {}
        self._durations: deque[float] = deque(maxlen=20)

    @property
    def waiting(self) -> int:
        return sum(map(len, self._users.values()))

    def _select(self, users, last_user, streak):
        if last_user in users and streak < self.quantum:
            return last_user
        return next((user for user in users if user != last_user), next(iter(users)))

    def _ordered(self):
        users = OrderedDict((user, deque(items)) for user, items in self._users.items())
        last, streak = self._last_user, self._streak
        ordered = []
        while users:
            user = self._select(users, last, streak)
            future = users[user].popleft()
            if not users[user]:
                del users[user]
            else:
                users.move_to_end(user)
            if future.cancelled():
                continue
            streak = min(self.quantum, streak + 1) if user == last else 1
            last = user
            ordered.append(future)
        return ordered

    def _estimate(self, future):
        ordered = self._ordered()
        duration = max(5.0, sum(self._durations) / len(self._durations)) if self._durations else 120.0
        now = monotonic()
        slots = [max(5.0, duration - (now - start)) for start in self._active_jobs.values()]
        slots += [0.0] * (self.concurrency - len(slots))
        heapq.heapify(slots)
        for index, pending in enumerate(ordered):
            available = heapq.heappop(slots)
            if pending is future:
                return QueueEstimate(len(self._active_jobs) + index + 1,
                                     len(self._active_jobs) + len(ordered), available)
            heapq.heappush(slots, available + duration)
        raise ValueError('Ticket is no longer waiting')

    def _changed(self):
        for update in self._updates.values():
            update.set()

    def _dispatch(self) -> None:
        while self._active < self.concurrency and self._users:
            selected = self._select(self._users, self._last_user, self._streak)
            future = self._users[selected].popleft()
            if not self._users[selected]:
                del self._users[selected]
            else:
                self._users.move_to_end(selected)
            if future.cancelled():
                continue
            self._streak = min(self.quantum, self._streak + 1) if selected == self._last_user else 1
            self._last_user = selected
            self._active += 1
            self._active_jobs[future] = monotonic()
            future.set_result(None)
        self._changed()

    @asynccontextmanager
    async def slot(self, user_id: int, *, on_queued: QueueCallback | None = None,
                   update_interval: float = 15):
        if update_interval <= 0:
            raise ValueError('Queue update interval must be positive')
        if self.waiting >= self.capacity:
            raise QueueFull("Очередь заполнена. Попробуй немного позже.")
        future = asyncio.get_running_loop().create_future()
        update = self._updates[future] = asyncio.Event()
        self._users.setdefault(user_id, deque()).append(future)
        self._dispatch()
        deadline = monotonic() + self.wait_timeout
        try:
            while not future.done():
                update.clear()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                if on_queued:
                    try:
                        await asyncio.wait_for(on_queued(self._estimate(future)), timeout=min(5, remaining))
                    except Exception as error:
                        logging.warning('Queue status update failed: %s', type(error).__name__)
                if future.done():
                    break
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    await asyncio.wait_for(update.wait(), min(update_interval, remaining))
                except asyncio.TimeoutError:
                    if monotonic() >= deadline:
                        raise
            yield
        finally:
            self._updates.pop(future, None)
            if future.done() and not future.cancelled():
                self._active -= 1
                self._durations.append(max(0.0, monotonic() - self._active_jobs.pop(future)))
            else:
                future.cancel()
                pending = self._users.get(user_id)
                if pending is not None:
                    pending.remove(future)
                    if not pending:
                        del self._users[user_id]
            self._dispatch()
