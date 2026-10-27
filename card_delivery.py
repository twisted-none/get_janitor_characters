"""Prepare per-user exports without modifying the shared source artifact."""

import asyncio
import json
import hashlib
from pathlib import Path
import tempfile

from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

import download_trace as trace
import lorebooks


def prepare(source, directory, identifier, mode):
    import parser
    card, books, missing = lorebooks.export_card(parser._read_card(source), mode)
    card_path = Path(directory) / (identifier + '.png')
    parser._write_chara_png(Path(source).read_bytes(), card, str(card_path))
    documents = []
    for index, book in enumerate(books, 1):
        path = Path(directory) / f'{identifier}-lorebook-{index}.json'
        path.write_text(json.dumps(lorebooks.standalone(book), ensure_ascii=False, allow_nan=False), encoding='utf-8')
        documents.append(path)
    trace.emit('exports_ready', operation='export', books=len(books), missing=missing,
               entries=sum(len(b['entries']) for b in books))
    return card_path, documents, missing


async def deliver(message, result, identifier, mode, *, redelivery=None):
    with tempfile.TemporaryDirectory(prefix='card-export-') as directory:
        card, books, missing = await asyncio.to_thread(prepare, result.file_path, directory, identifier, mode)
        caption = result.caption
        if missing:
            notice = f'Не удалось получить лорбуки: {missing}. Отправлены доступные данные.'
            caption = (caption + '\n' if caption else '') + notice
        sent_keys = set(redelivery.sent_keys) if redelivery else set()
        if redelivery:
            caption = redelivery.notice
        if 'card' not in sent_keys:
            await message.answer_document(FSInputFile(card), caption=caption)
            if redelivery:
                await redelivery.record_sent('card')
                sent_keys.add('card')
        failed = 0
        for book in books:
            canonical = json.dumps(json.loads(book.read_text(encoding='utf-8')), sort_keys=True, ensure_ascii=False)
            key = 'book:' + hashlib.sha256(canonical.encode()).hexdigest()
            if key in sent_keys:
                continue
            try:
                for attempt in range(3):
                    try:
                        await message.answer_document(FSInputFile(book))
                        if redelivery:
                            await redelivery.record_sent(key)
                            sent_keys.add(key)
                        break
                    except TelegramRetryAfter as exc:
                        if attempt == 2 or exc.retry_after > 120:
                            raise
                        await asyncio.sleep(exc.retry_after)
            except TelegramAPIError as error:
                failed += 1
                if redelivery is not None:
                    redelivery.failure = 'Telegram не доставил часть файлов лорбуков.'
                    redelivery.error = error
        if failed and redelivery is None:
            await message.answer(f'Карточка отправлена, но не удалось отправить JSON-файлы лорбуков: {failed}.')
        trace.emit('sent', operation='telegram_send', books=len(books)-failed, missing=missing+failed)
        return failed == 0
