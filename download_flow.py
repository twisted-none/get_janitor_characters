"""Ordered, short-circuiting downloads and source-aware result notices."""

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum


class DownloadSource(str, Enum):
    DATACAT = "datacat"
    JANNYAI = "jannyai"
    PROXY = "proxy"
    LLM = "llm"


ProgressCallback = Callable[[DownloadSource], Awaitable[None]]


class DownloadFailed(ValueError):
    def __init__(self, llm_available: bool) -> None:
        message = "Не удалось получить карточку через DataCat, JannyAI и JanitorAI."
        if not llm_available:
            message += " Восстановление через LLM пока недоступно."
        super().__init__(message)


@dataclass(frozen=True)
class Candidate:
    file_path: str
    definition_available: bool
    notice: str | None = None


@dataclass(frozen=True)
class DownloadResult:
    file_path: str
    source: DownloadSource
    definition_available: bool
    notice: str | None = None
    llm_available: bool = True

    @property
    def reconstructed(self) -> bool:
        return self.source == DownloadSource.LLM and self.definition_available

    @property
    def caption(self) -> str | None:
        if not self.definition_available:
            return "Определение получить не удалось; PNG содержит только доступные поля."
        if self.reconstructed:
            return "Определение восстановлено через LLM JanitorAI и может отличаться от оригинала."
        if self.notice:
            return self.notice
        if self.source == DownloadSource.PROXY:
            return "Определение получено через Janitor proxy."
        return None


def remove_download(file_path: str) -> None:
    try:
        os.remove(file_path)
    except FileNotFoundError:
        pass
    except OSError:
        logging.warning("Не удалось удалить временную карточку")


async def run_download_flow(
    downloaders: Mapping[DownloadSource, Callable[[], Awaitable[Candidate]]],
    on_progress: ProgressCallback | None = None,
) -> DownloadResult:
    partials: list[tuple[DownloadSource, Candidate]] = []
    chosen: Candidate | None = None
    llm_available = DownloadSource.LLM in downloaders
    try:
        for source in DownloadSource:
            if on_progress:
                try:
                    await on_progress(source)
                except Exception as exc:
                    logging.warning("Не удалось обновить статус скачивания: %s", type(exc).__name__)
            downloader = downloaders.get(source)
            if downloader is None:
                logging.info("Источник %s не подключён", source.value)
                continue
            try:
                candidate = await downloader()
            except Exception as exc:
                logging.warning("Источник %s недоступен: %s", source.value, type(exc).__name__)
                continue
            if not candidate.definition_available:
                partials.append((source, candidate))
                continue
            chosen = candidate
            return DownloadResult(candidate.file_path, source, True, candidate.notice, llm_available)
        if partials:
            source, chosen = partials[0]
            return DownloadResult(chosen.file_path, source, False, llm_available=llm_available)
        raise DownloadFailed(llm_available)
    finally:
        for _, candidate in partials:
            if chosen is None or candidate.file_path != chosen.file_path:
                remove_download(candidate.file_path)
