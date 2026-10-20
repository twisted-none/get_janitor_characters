"""Bounded per-user queue with a five-job round-robin quantum."""

import asyncio
from collections import OrderedDict, deque
from contextlib import asynccontextmanager


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

    @property
    def waiting(self) -> int:
        return sum(map(len, self._users.values()))

    def _dispatch(self) -> None:
        while self._active < self.concurrency and self._users:
            users = list(self._users)
            if self._last_user in self._users and self._streak < self.quantum:
                selected = self._last_user
            else:
                selected = next((user for user in users if user != self._last_user), users[0])
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
            future.set_result(None)

    @asynccontextmanager
    async def slot(self, user_id: int):
        if self.waiting >= self.capacity:
            raise QueueFull("Очередь заполнена. Попробуй немного позже.")
        future = asyncio.get_running_loop().create_future()
        self._users.setdefault(user_id, deque()).append(future)
        self._dispatch()
        try:
            await asyncio.wait_for(asyncio.shield(future), self.wait_timeout)
            yield
        finally:
            if future.done() and not future.cancelled():
                self._active -= 1
            else:
                future.cancel()
                pending = self._users.get(user_id)
                if pending is not None:
                    pending.remove(future)
                    if not pending:
                        del self._users[user_id]
            self._dispatch()
