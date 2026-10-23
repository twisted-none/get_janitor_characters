"""Ordered, short-circuiting downloads and source-aware result notices."""

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from time import monotonic

from download_errors import SourceError, describe_error
import download_trace as trace


class DownloadSource(str, Enum):
    DATACAT = "datacat"
    JANNYAI = "jannyai"
    PROXY = "proxy"
    LLM = "llm"


ProgressCallback = Callable[[DownloadSource], Awaitable[None]]


class DownloadFailed(ValueError):
    def __init__(self, diagnostics=None) -> None:
        self.diagnostics = {source: SourceError.from_payload(payload).to_payload()
                            for source, payload in (diagnostics or {}).items()
                            if source in {stage.value for stage in DownloadSource}}
        super().__init__("Не удалось получить карточку через DataCat, JannyAI и JanitorAI.")


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


@trace.job
async def run_download_flow(
    downloaders: Mapping[DownloadSource, Callable[[], Awaitable[Candidate]]],
    on_progress: ProgressCallback | None = None,
) -> DownloadResult:
    partials: list[tuple[DownloadSource, Candidate]] = []
    chosen: Candidate | None = None
    llm_available = DownloadSource.LLM in downloaders
    diagnostics = {}
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
            started = monotonic()
            trace.emit('source_started', operation=source.value)
            try:
                candidate = await downloader()
            except Exception as exc:
                diagnostic = describe_error(exc).to_payload()
                diagnostics[source.value] = diagnostic
                logging.warning("download_source_failed source=%s code=%s http_status=%s error_type=%s exit_code=%s duration_ms=%s",
                                source.value, diagnostic["code"], diagnostic["http_status"], diagnostic["error_type"],
                                diagnostic["exit_code"], round((monotonic() - started) * 1000))
                continue
            trace.emit('source_completed', operation=source.value, definition_available=candidate.definition_available,
                       duration_ms=round((monotonic()-started)*1000))
            if not candidate.definition_available:
                trace.emit('fallback', operation=source.value, reason='definition_missing', partial=True)
                partials.append((source, candidate))
                continue
            chosen = candidate
            return DownloadResult(candidate.file_path, source, True, candidate.notice, llm_available)
        if partials:
            trace.emit('partial_selected', reason='all_sources_exhausted', partial=True)
            source, chosen = partials[0]
            return DownloadResult(chosen.file_path, source, False, llm_available=llm_available)
        raise DownloadFailed(diagnostics)
    finally:
        for _, candidate in partials:
            if chosen is None or candidate.file_path != chosen.file_path:
                remove_download(candidate.file_path)
