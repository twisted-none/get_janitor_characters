"""Show the queue and one editable progress message for an active card."""

import asyncio
import logging
import math
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any

from download_flow import DownloadSource, DownloadProgress, DownloadPhase
from download_queue import QueueEstimate
from download_trace import clean_title


@dataclass
class DownloadJob:
    user_id: int
    chat_id: int
    message_id: int
    task: asyncio.Task
    cancel_requested: bool = False
    sending: bool = False


def card_label(url: str, identifier: str) -> str:
    slug = urlsplit(url).path.split('/characters/', 1)[-1].split('/', 1)[0].partition('_')[2]
    return clean_title(unquote(slug).removeprefix('character-').replace('-', ' ')) or identifier


STATUS_TEXT = {
    DownloadSource.DATACAT: "Проверяю на DataCat…",
    DownloadSource.JANNYAI: "Проверяю на JannyAI…",
    DownloadSource.PROXY: "Проверяю JanitorAI и возможность получить определение через proxy…",
    DownloadSource.LLM: "Через proxy определение не получено. Восстановление через LLM пока недоступно.",
}

DOWNLOAD_TEXT = {
    DownloadSource.DATACAT: 'Скачиваю с DataCat…',
    DownloadSource.JANNYAI: 'Скачиваю с JannyAI…',
    DownloadSource.PROXY: 'Скачиваю через proxy…',
    DownloadSource.LLM: 'Скачиваю восстановленную карточку…',
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


def queue_status(estimate: QueueEstimate | None = None) -> str:
    if estimate is None:
        return progress_text('Карточка ожидает загрузки…', 0)
    seconds = max(0, math.ceil(estimate.estimated_wait_seconds))
    if seconds < 60:
        wait = 'менее минуты'
    elif seconds < 3600:
        wait = f'около {math.ceil(seconds / 60)} мин'
    else:
        wait = f'около {math.ceil(seconds / 3600)} ч'
    return progress_text(f'Место в очереди: {estimate.position}.\nПримерное ожидание: {wait}.', 0)


def active_status() -> str:
    return progress_text("Ваша карточка скачивается...", 0)


class DownloadStatus:
    def __init__(
        self, edit: Callable[[str], Awaitable[Any]], *, initial_text: str = "", min_interval: float = 1.1,
        title: str = '',
    ) -> None:
        self._edit = edit
        self._title = clean_title(title)
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
        self._waiting = False
        self._lock = asyncio.Lock()

    @property
    def title(self) -> str:
        return self._title

    async def __call__(self, event: DownloadSource | DownloadProgress) -> None:
        if isinstance(event, DownloadProgress) and clean_title(event.title):
            self._title = clean_title(event.title)
        source = event.source if isinstance(event, DownloadProgress) else event
        downloading = isinstance(event, DownloadProgress) and event.phase == DownloadPhase.DOWNLOADING
        if self._waiting:
            self._started_at = monotonic()
            self._waiting = False
        self._actual_percent = max(self._actual_percent, STAGE_PERCENT[source])
        if downloading:
            self._actual_percent = max(self._actual_percent, STAGE_PERCENT[source] + 10)
        self._stage_text = DOWNLOAD_TEXT[source] if downloading else STATUS_TEXT[source]
        await self._render(force=True)

    async def queued(self, estimate: QueueEstimate) -> None:
        self._waiting = True
        await self.update(queue_status(estimate), force=True)

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
        if self._title:
            text = f'Карточка: {self._title}\n{text}'
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
