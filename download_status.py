"""Show the queue and one editable progress message for an active card."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from download_flow import DownloadSource


STATUS_TEXT = {
    DownloadSource.DATACAT: "Скачиваю карточку: проверяю DataCat…",
    DownloadSource.JANNYAI: "Пробую скачать карточку с JannyAI…",
    DownloadSource.PROXY: "Проверяю JanitorAI и возможность получить определение через proxy…",
    DownloadSource.LLM: "Через proxy определение не получено. Восстановление через LLM пока недоступно.",
}

STAGE_PERCENT = {
    DownloadSource.DATACAT: 10,
    DownloadSource.JANNYAI: 30,
    DownloadSource.PROXY: 55,
    DownloadSource.LLM: 80,
}


def progress_text(text: str, percent: int) -> str:
    percent = max(0, min(100, percent))
    bar = "🟢" * (percent // 20) + "⚪️" * (5 - percent // 20)
    return f"{bar} {percent}%\n{text}"


def queue_status() -> str:
    return progress_text("Карточка ожидает загрузки...", 0)


def active_status() -> str:
    return progress_text("Ваша карточка скачивается...", 0)


class DownloadStatus:
    def __init__(
        self, edit: Callable[[str], Awaitable[Any]], *, initial_text: str = "", min_interval: float = 1.1,
    ) -> None:
        self._edit = edit
        self._last_text = initial_text
        self._last_edited_at = 0.0
        self._retry_after_until = 0.0
        self._min_interval = min_interval
        self._started_at = monotonic()
        self._actual_percent = 0
        self._shown_percent = 0
        self._stage_text = "Ваша карточка скачивается..."
        self._sending = False
        self._closed = False
        self._lock = asyncio.Lock()

    async def __call__(self, source: DownloadSource) -> None:
        self._actual_percent = max(self._actual_percent, STAGE_PERCENT[source])
        self._stage_text = STATUS_TEXT[source]
        await self._render(force=True)

    async def tick(self) -> None:
        await self._render(force=False)

    async def run_ticker(self, interval: float = 5.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.tick()

    async def sending(self) -> None:
        self._sending = True
        self._stage_text = "Отправляю карточку..."
        await self._render(force=True)

    async def close(self) -> None:
        async with self._lock:
            self._closed = True

    async def _render(self, *, force: bool) -> None:
        if self._sending:
            self._shown_percent = 100
        else:
            elapsed = max(0.0, monotonic() - self._started_at)
            timed = min(95, int(elapsed / 120.0 * 90))
            self._shown_percent = max(self._shown_percent, self._actual_percent, timed)
        await self.update(progress_text(self._stage_text, self._shown_percent), force=force)

    async def update(self, text: str, *, force: bool = True) -> None:
        async with self._lock:
            if self._closed or text == self._last_text:
                return
            now = monotonic()
            if now < self._retry_after_until:
                return
            delay = self._min_interval - max(0.0, now - self._last_edited_at)
            if delay > 0:
                if not force:
                    return
                await asyncio.sleep(delay)
            try:
                await self._edit(text)
            except Exception as exc:
                retry_after = getattr(exc, "retry_after", None)
                if isinstance(retry_after, (int, float)) and retry_after > 0:
                    self._retry_after_until = monotonic() + retry_after
                elif "message is not modified" in str(exc).lower():
                    self._last_text = text
                    self._last_edited_at = monotonic()
                    return
                logging.warning("Не удалось обновить сообщение скачивания: %s", type(exc).__name__)
            else:
                self._last_text = text
                self._last_edited_at = monotonic()
