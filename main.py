import asyncio
import logging
import os
import re
import sys
from datetime import UTC, datetime
from secrets import token_hex
from collections.abc import Awaitable, Callable
from typing import Any
from time import monotonic
from collections import OrderedDict

from aiogram import BaseMiddleware, Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

load_dotenv()


def _parse_int_env(*names: str) -> int:
    for name in names:
        raw_value = os.getenv(name, "").strip()
        if not raw_value:
            continue
        try:
            return int(raw_value)
        except ValueError:
            logging.warning("Invalid integer value in %s", name)
    return 0


def _parse_positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        logging.warning("Invalid integer value in %s", name)
        return default
    if value < 1:
        logging.warning("%s must be >= 1, using %s", name, default)
        return default
    return value

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")
DOWNLOAD_TIMEOUT_SECONDS = _parse_positive_int_env("DOWNLOAD_TIMEOUT_SECONDS", 3600)
DOWNLOAD_CONCURRENCY = _parse_positive_int_env("DOWNLOAD_CONCURRENCY", 1)
METRICS_HOST = os.getenv("METRICS_HOST", "0.0.0.0")
METRICS_PORT = int(os.getenv("METRICS_PORT", "8000"))
ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = _parse_int_env("ADMIN_CHAT_ID", "ADMIN_ID")

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
)

import parser
from download_flow import DownloadFailed, remove_download
from download_status import DownloadStatus, DownloadJob, card_label, queue_status
from download_queue import DownloadQueue, QueueFull
from download_retries import RetryStore, run_retries, waiting_text, EXHAUSTED_TEXT, MAX_ATTEMPTS
from alert_retries import AlertRetryStore
from alert_redelivery import AlertRedelivery, PREFIX as ALERT_RETRY_PREFIX, start_alert_server, restore_user
from analytics import AnalyticsStore
from metrics import run_metrics_server
from card_delivery import deliver
import lorebooks
import download_trace as trace

bot = Bot(token=TOKEN)
alert_bot = Bot(token=ALERT_BOT_TOKEN) if ALERT_BOT_TOKEN else None
dp = Dispatcher()
CHARACTER_URL_RE = (r"(?i)^\s*https?://(?:www\.)?(?P<source>janitorai|jannyai)\.com/"
                    r"(?:[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,3}/)?characters/"
                    r"(?P<id>[a-zA-Z0-9-]{1,200})"
                    r"(?:_(?:[^\s%/?#\\\x00-\x1f\x7f]|%[0-9a-f]{2}){1,1800})?"
                    r"(?:/[^\s?#]*)?(?:\?[^\s#]*)?(?:#[^\s]*)?\s*$")
support_waiting: set[tuple[int, int]] = set()
reply_waiting: dict[tuple[int, int], int] = {}
broadcast_waiting: set[tuple[int, int]] = set()
broadcast_pending: dict[tuple[int, int], tuple[float, str, str]] = {}
notification_times: OrderedDict[tuple[str, int], float] = OrderedDict()
pending_downloads: set[int] = set()
download_jobs: dict[str, DownloadJob] = {}
download_queue = DownloadQueue(DOWNLOAD_CONCURRENCY)


class RegisterUserMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.Message, dict[str, Any]], Awaitable[Any]],
        event: types.Message,
        data: dict[str, Any],
    ) -> Any:
        store: AnalyticsStore | None = data.get("analytics_store")
        if store and event.from_user:
            await _record_user(store, event.from_user)
        return await handler(event, data)


@dp.message(CommandStart())
async def handle_start(message: types.Message, analytics_store: AnalyticsStore) -> None:
    _clear_dialog(message.from_user.id if message.from_user else 0, message.chat.id)
    if message.from_user:
        await _record_user(analytics_store, message.from_user, started=True)
    await message.answer(
        "✨ <b>Привет! Я помогу сохранить персонажа из JanitorAI.</b>\n\n"
        "🔗 Пришли ссылку на страницу персонажа в JanitorAI.\n"
        "🖼 Я скачаю его карточку и отправлю её в формате PNG.\n"
        "📦 В картинку встроены все метаданные персонажа.\n\n"
        "🆘 Если что-то пошло не так, напиши в поддержку через /help.\n\n"
        "Пришли ссылку, и начнем.",
        parse_mode="HTML",
        reply_markup=_main_keyboard(message.from_user.id if message.from_user else 0),
    )


@dp.message(Command("help"))
async def handle_help(message: types.Message) -> None:
    if not message.from_user:
        return
    await _begin_support(message, message.from_user.id)


def _settings_keyboard(mode):
    labels = {'separate': 'Отдельными JSON', 'embedded': 'Внутри PNG', 'both': 'PNG и JSON'}
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=('✓ ' if value == mode else '') + label,
                             callback_data='settings:lorebooks:' + value)
    ] for value, label in labels.items()])


@dp.message(Command('settings'))
async def handle_settings(message: types.Message, analytics_store: AnalyticsStore) -> None:
    if not message.from_user or message.chat.type != 'private':
        await message.answer('Открой настройки в личном чате с ботом.')
        return
    _clear_dialog(message.from_user.id, message.chat.id)
    try:
        mode = await asyncio.to_thread(analytics_store.get_lorebook_mode, message.from_user.id)
    except Exception:
        await message.answer('Не удалось загрузить настройки. Попробуй позже.')
        return
    await message.answer('Лорбуки', reply_markup=_settings_keyboard(mode))


@dp.callback_query(F.data.startswith('settings:lorebooks:'))
async def handle_lorebook_setting(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    if not callback.message or callback.message.chat.type != 'private':
        await callback.answer('Настройки доступны в личном чате.', show_alert=True)
        return
    mode = (callback.data or '').removeprefix('settings:lorebooks:')
    if mode not in lorebooks.MODES:
        await callback.answer('Неизвестный формат.', show_alert=True)
        return
    try:
        await asyncio.to_thread(analytics_store.set_lorebook_mode, callback.from_user.id, mode)
    except Exception:
        await callback.answer('Не удалось сохранить настройку.', show_alert=True)
        return
    await callback.answer('Сохранено')
    keyboard = _settings_keyboard(mode)
    if getattr(callback.message, 'reply_markup', None) != keyboard:
        await callback.message.edit_reply_markup(reply_markup=keyboard)


@dp.message(Command("cancel"))
async def handle_cancel(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    cancelled = _clear_dialog(user_id, message.chat.id)
    await message.answer("Текущее действие отменено." if cancelled else "Сейчас нечего отменять.",
                         reply_markup=_main_keyboard(user_id))


@dp.message(Command("broadcast", "queue"))
async def handle_admin_command(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    if not _is_admin(user_id):
        await message.answer("Команда доступна только администратору.")
        return
    if message.chat.type != "private":
        await message.answer("Открой личный чат с ботом для этой команды.")
        return
    if (message.text or "").split()[0].split("@")[0] == "/queue":
        await message.answer(f"Персонажей в обработке и очереди: {len(pending_downloads)}.")
        return
    _clear_dialog(user_id, message.chat.id)
    broadcast_waiting.add((message.chat.id, user_id))
    await message.answer("Пришли следующим сообщением текст рассылки. /cancel - отменить.",
                         reply_markup=_cancel_keyboard())


@dp.callback_query(F.data == "support:write")
async def handle_support_click(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.from_user or not callback.message:
        return
    await _begin_support(callback.message, callback.from_user.id)


@dp.callback_query(F.data.startswith("support:reply:"))
async def handle_reply_click(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id if callback.from_user else 0):
        await callback.answer("Недоступно", show_alert=True)
        return
    if not callback.message:
        return
    if callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом.", show_alert=True)
        return
    raw_id = (callback.data or "").rsplit(":", 1)[-1]
    if not raw_id.isdigit():
        await callback.answer("Некорректный получатель.", show_alert=True)
        return
    user_id = int(raw_id)
    _clear_dialog(callback.from_user.id, callback.message.chat.id)
    reply_waiting[(callback.message.chat.id, callback.from_user.id)] = user_id
    await callback.answer()
    await callback.message.answer("Напиши ответ пользователю следующим сообщением. /cancel - отменить.",
                                  reply_markup=_cancel_keyboard())


@dp.message(lambda message: message.from_user and (message.chat.id, message.from_user.id) in reply_waiting)
async def handle_admin_reply(message: types.Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    if message.chat.type != "private":
        return
    key = (message.chat.id, message.from_user.id)
    user_id = reply_waiting.get(key)
    if user_id is None:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст ответа.")
        return
    try:
        await message.bot.send_message(user_id, f"Ответ поддержки:\n\n{text}")
    except TelegramAPIError:
        await message.answer("Не удалось доставить ответ. Попробуй снова или нажми /cancel.")
        return
    reply_waiting.pop(key, None)
    await message.answer("Ответ отправлен.", reply_markup=_main_keyboard(message.from_user.id))


@dp.message(lambda message: message.from_user and (message.chat.id, message.from_user.id) in support_waiting)
async def handle_support_text(message: types.Message) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст сообщения.")
        return
    await _send_support_message(message, text)


@dp.message(lambda message: message.from_user and (message.chat.id, message.from_user.id) in broadcast_waiting)
async def handle_broadcast(message: types.Message, analytics_store: AnalyticsStore) -> None:
    if not _is_admin(message.from_user.id):
        return
    key = (message.chat.id, message.from_user.id)
    if message.chat.type != "private" or key not in broadcast_waiting:
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст для рассылки. /cancel - отменить.")
        return
    if len(text) > 3500:
        await message.answer("Текст рассылки должен быть не длиннее 3500 символов.")
        return
    broadcast_waiting.discard(key)
    nonce = token_hex(8)
    broadcast_pending[key] = (monotonic(), text, nonce)
    await message.answer("Предпросмотр рассылки:\n\n" + text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отправить", callback_data=f"broadcast:send:{nonce}"),
        InlineKeyboardButton(text="Отменить", callback_data=f"broadcast:cancel:{nonce}"),
    ]]))


@dp.callback_query(F.data.startswith("broadcast:"))
async def handle_broadcast_confirm(callback: CallbackQuery, analytics_store: AnalyticsStore) -> None:
    if not _is_admin(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Недоступно", show_alert=True)
        return
    message = callback.message
    key = (message.chat.id, callback.from_user.id)
    pending = broadcast_pending.get(key)
    action = (callback.data or "").split(":")
    if (pending is None or len(action) != 3 or action[1] not in {"send", "cancel"}
            or action[2] != pending[2]):
        await callback.answer("Это подтверждение больше не действует.", show_alert=True)
        return
    broadcast_pending.pop(key)
    await callback.answer()
    if action[1] == "cancel":
        await message.answer("Рассылка отменена.")
        return
    if pending is None or monotonic() - pending[0] > 300:
        await message.answer("Подтверждение рассылки истекло. Начни заново через /broadcast.")
        return
    text = pending[1]
    try:
        recipients = await asyncio.to_thread(analytics_store.broadcast_recipients)
    except Exception:
        logging.exception("Failed to load broadcast recipients")
        await message.answer("Не удалось получить список пользователей. Попробуй позже.")
        return
    sent = failed = 0
    for user_id in recipients:
        try:
            try:
                await message.bot.send_message(user_id, text)
            except TelegramRetryAfter as error:
                await asyncio.sleep(error.retry_after)
                await message.bot.send_message(user_id, text)
            sent += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"Рассылка завершена. Отправлено: {sent}. Ошибок: {failed}.",
                         reply_markup=_main_keyboard(callback.from_user.id))


@dp.callback_query(F.data.startswith(ALERT_RETRY_PREFIX))
async def handle_alert_retry(callback: CallbackQuery, alert_service: AlertRedelivery) -> None:
    if callback.message is None:
        await callback.answer('Сообщение недоступно.', show_alert=True)
        return
    text = await alert_service.submit(dict(token=(callback.data or '').removeprefix(ALERT_RETRY_PREFIX),
        user_id=callback.from_user.id, chat_id=callback.message.chat.id,
        message_id=callback.message.message_id, bot_id=callback.bot.id))
    await callback.answer(text, show_alert=True)


@dp.callback_query(F.data.startswith('download:cancel:'))
async def handle_cancel_download(callback: CallbackQuery, retry_store: RetryStore | None = None) -> None:
    token = (callback.data or '').removeprefix('download:cancel:')
    job = download_jobs.get(token)
    message = callback.message
    if job is None and message is not None and retry_store is not None:
        cancelled = await asyncio.to_thread(retry_store.cancel, token, callback.from_user.id,
                                            message.chat.id, message.message_id)
        if cancelled:
            await callback.answer('Отложенные проверки отменены.')
            try:
                await message.edit_text('Загрузка отменена.', reply_markup=None, parse_mode=None)
            except TelegramAPIError:
                logging.warning('Не удалось показать отмену отложенной проверки')
            return
    if (job is None or message is None or job.user_id != callback.from_user.id
            or job.chat_id != message.chat.id or job.message_id != message.message_id
            or job.task.done()):
        await callback.answer('Задача уже завершена или недоступна.', show_alert=True)
        return
    if job.sending:
        await callback.answer('Карточка уже отправляется.')
        return
    if not job.cancel_requested:
        job.cancel_requested = True
        job.task.cancel()
    await callback.answer('Отменяю загрузку…')


@dp.message(F.text.regexp(CHARACTER_URL_RE))
@trace.job
async def handle_link(message: types.Message, analytics_store: AnalyticsStore,
                      retry_store: RetryStore | None = None, retry_request: dict | None = None,
                      alert_service: AlertRedelivery | None = None, redelivery=None) -> bool | None:
    match = re.search(CHARACTER_URL_RE, message.text or "")
    if not match:
        return

    source = match.group("source").lower()
    character_id = match.group("id")
    trace.emit('received', character_id=character_id)
    character_url = match.group(0).strip()
    token = retry_request['token'] if retry_request else token_hex(12)
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Отменить загрузку', callback_data=f'download:cancel:{token}'),
    ]])
    label = retry_request['title'] if retry_request else card_label(character_url, character_id)
    initial_text = f'Карточка: {label}\n{queue_status()}'
    wait_msg = message if retry_request or redelivery else await message.answer(initial_text, reply_markup=markup, parse_mode=None)
    status_message = wait_msg

    async def edit_status(text):
        if redelivery is not None:
            return
        await wait_msg.edit_text(text, reply_markup=None if job.sending else markup, parse_mode=None)

    status = DownloadStatus(edit_status, initial_text=initial_text, title=label)
    file_path: str | None = None
    user_id = message.from_user.id if message.from_user else 0
    download_id = None
    job = DownloadJob(user_id, message.chat.id, wait_msg.message_id, asyncio.current_task())
    download_jobs[token] = job
    attempt = retry_request['completed_attempts'] + 1 if retry_request else 1
    deferred = False
    job_id = id(wait_msg)
    pending_downloads.add(job_id)
    logging.info("Получена ссылка от user_id=%s, source=%s, character_id=%s", user_id, source, character_id)

    try:
        if retry_request:
            if not await asyncio.to_thread(retry_store.exists, token):
                return  # A pending cancellation won the race with scheduler dispatch.
            await edit_status(initial_text)
        download_id = await _start_download(analytics_store, user_id, character_id, character_url)
        async with download_queue.slot(user_id, on_queued=status.queued):
            logging.info(
                "Старт download slot: source=%s, character_id=%s, concurrency=%s",
                source,
                character_id,
                DOWNLOAD_CONCURRENCY,
            )
            ticker = asyncio.create_task(status.run_ticker())
            try:
                result = await asyncio.wait_for(
                    parser.download_card(character_id, source=source, on_progress=status),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            finally:
                ticker.cancel()
                await asyncio.gather(ticker, return_exceptions=True)
            file_path = result.file_path
            logging.info("Файл готов: %s, размер=%s байт", file_path, os.path.getsize(file_path))
            job.sending = True
            if retry_request:
                # Telegram cannot provide exactly-once sends across a process crash.
                # Remove the durable retry before sending to avoid duplicate delivery on restart.
                await asyncio.to_thread(retry_store.finish, token)
            await status.sending()
            mode = redelivery.mode if redelivery else await asyncio.to_thread(analytics_store.get_lorebook_mode, user_id)
            if redelivery:
                delivered = await deliver(message, result, character_id, mode, redelivery=redelivery)
            else:
                delivered = await deliver(message, result, character_id, mode)
        await _finish_download(analytics_store, download_id, success=True)
        try:
            if redelivery is None:
                await status_message.delete()
        except TelegramAPIError:
            logging.warning("Не удалось удалить сообщение статуса после отправки карточки")
        return delivered is not False if redelivery else True
    except asyncio.CancelledError:
        if not job.cancel_requested:
            raise
        await _finish_download(analytics_store, download_id, success=False, error='cancelled by user')
    except asyncio.TimeoutError:
        logging.exception("Таймаут скачивания персонажа %s", character_id)
        await _finish_download(analytics_store, download_id, success=False, error="download timeout")
        if redelivery is None:
            await _notify_admin(message, character_url, TimeoutError("download timeout"), alert_service=alert_service)
        else:
            return False
        await status_message.edit_text(
            "Скачивание заняло слишком много времени. Попробуй позже или напиши в поддержку через /help.",
            reply_markup=_support_keyboard(),
        )
    except Exception as e:
        missing = isinstance(e, DownloadFailed) and e.missing_from_mirrors
        if missing:
            logging.info('Карточка отсутствует на обоих зеркалах: character_id=%s attempt=%s', character_id, attempt)
        else:
            logging.exception("Ошибка скачивания персонажа %s", character_id)
        await _finish_download(analytics_store, download_id, success=False, error=str(e))
        if missing and redelivery is None:
            if attempt >= MAX_ATTEMPTS:
                await status.close()
                await status_message.edit_text(f'Карточка: {status.title}\n{EXHAUSTED_TEXT}',
                                               reply_markup=None, parse_mode=None)
                return
            if retry_store is not None:
                request = dict(token=token, user_id=user_id, chat_id=message.chat.id,
                               message_id=wait_msg.message_id, chat_type=getattr(message.chat, 'type', 'private'),
                               character_id=character_id, url=character_url, title=status.title,
                               completed_attempts=attempt)
                request['username'] = getattr(message.from_user, 'username', None) or ''
                request['full_name'] = getattr(message.from_user, 'full_name', '') or ''
                try:
                    deferred = await asyncio.to_thread(retry_store.schedule, request, resumed=bool(retry_request))
                except Exception as error:
                    logging.warning('Cannot save card retry: %s', type(error).__name__)
                if deferred:
                    await status.close()
                    notice = f'Карточка: {status.title}\n{waiting_text(attempt)}'
                    try:
                        await edit_status(notice)
                    except TelegramAPIError:
                        # The persisted request must remain valid even if Telegram refuses an edit.
                        try:
                            await message.answer(notice, parse_mode=None)
                        except TelegramAPIError:
                            logging.warning('Cannot show deferred retry notice')
                    return
        if redelivery is None:
            await _notify_admin(message, character_url, e, alert_service=alert_service)
        else:
            return False
        error_text = (str(e) if isinstance(e, (DownloadFailed, QueueFull)) else
                      "Не удалось скачать персонажа. Проверь, что страница доступна, и попробуй снова.")
        await status_message.edit_text(error_text + " Если ошибка повторяется, напиши в поддержку через /help.",
                                       reply_markup=_support_keyboard())
    finally:
        if status is not None:
            await status.close()
        # Keep interrupted retries for recovery; explicit cancellation is final.
        interrupted = asyncio.current_task().cancelling() and not job.cancel_requested
        if retry_store is not None and (job.cancel_requested or (retry_request and not deferred and not interrupted)):
            try:
                await asyncio.to_thread(retry_store.finish, token)
            except Exception as error:
                logging.warning('Cannot finish card retry: %s', type(error).__name__)
        if job.cancel_requested and redelivery is None:
            try:
                await status_message.edit_text('Загрузка отменена.', reply_markup=None, parse_mode=None)
            except TelegramAPIError:
                logging.warning('Не удалось показать отмену скачивания')
        if file_path:
            remove_download(file_path)
        pending_downloads.discard(job_id)
        download_jobs.pop(token, None)


@dp.message()
async def handle_invalid_input(message: types.Message) -> None:
    text = (message.text or "").strip()
    response = ("Неизвестная команда. Открой меню или нажми /help." if text.startswith("/") else
                "Нужна ссылка на страницу персонажа JanitorAI.\n\n"
                "Открой персонажа на janitorai.com и скопируй адрес "
                "из браузера. Ссылка должна содержать /characters/ и идентификатор персонажа.")
    await message.answer(response, reply_markup=_main_keyboard(message.from_user.id if message.from_user else 0))


async def main() -> None:
    analytics_store = AnalyticsStore(DATABASE_URL)
    retry_store = await asyncio.to_thread(RetryStore, DATABASE_URL)
    alert_store = await asyncio.to_thread(AlertRetryStore, DATABASE_URL)
    dp["analytics_store"] = analytics_store
    dp['retry_store'] = retry_store
    dp.message.middleware(RegisterUserMiddleware())
    metrics_task = asyncio.create_task(run_metrics_server(analytics_store, METRICS_HOST, METRICS_PORT))
    retry_task = None
    alert_runner = None

    async def redownload(message, context):
        return await handle_link(message, analytics_store, retry_store=retry_store,
                                 alert_service=alert_service, redelivery=context)

    alert_service = AlertRedelivery(bot, alert_bot or bot, ADMIN_CHAT_ID, alert_store,
                                    redownload, analytics_store)
    dp['alert_service'] = alert_service

    async def resume(request):
        message = types.Message(message_id=request['message_id'], date=datetime.now(UTC), text=request['url'],
                                chat=types.Chat(id=request['chat_id'], type=request['chat_type']),
                                from_user=await restore_user(request, analytics_store)).as_(bot)
        await handle_link(message, analytics_store, retry_store=retry_store, retry_request=request,
                          alert_service=alert_service)

    try:
        await _setup_commands()
        alert_runner = await start_alert_server(alert_service, ALERT_BOT_TOKEN or TOKEN)
        retry_task = asyncio.create_task(run_retries(retry_store, resume))
        await dp.start_polling(bot, tasks_concurrency_limit=1024)
    finally:
        metrics_task.cancel()
        if retry_task is not None:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)
        if alert_runner is not None:
            await alert_runner.cleanup()
        await alert_service.close()
        if alert_bot:
            await alert_bot.session.close()


async def _record_user(store: AnalyticsStore, user: types.User, *, started: bool = False) -> None:
    try:
        await asyncio.to_thread(store.record_user, user, started=started)
    except Exception:
        logging.exception("Failed to record user analytics")


async def _start_download(store: AnalyticsStore, user_id: int, character_id: str, url: str) -> int | None:
    if not user_id:
        return None
    try:
        return await asyncio.to_thread(store.start_download, user_id, character_id, url)
    except Exception:
        logging.exception("Failed to record download attempt")
        return None


async def _finish_download(store: AnalyticsStore, download_id: int | None, *, success: bool, error: str = "") -> None:
    if download_id is None:
        return
    try:
        await asyncio.to_thread(store.finish_download, download_id, success=success, error=error)
    except Exception:
        logging.exception("Failed to finish download analytics")


def _is_admin(user_id: int) -> bool:
    return bool(ADMIN_CHAT_ID and user_id == ADMIN_CHAT_ID)


def _main_keyboard(user_id: int = 0) -> types.ReplyKeyboardMarkup:
    rows = [[types.KeyboardButton(text="/start"), types.KeyboardButton(text="/help")],
            [types.KeyboardButton(text='/settings')]]
    if _is_admin(user_id):
        rows.append([types.KeyboardButton(text="/broadcast"), types.KeyboardButton(text="/queue")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _cancel_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="/cancel")]], resize_keyboard=True)


def _clear_dialog(user_id: int, chat_id: int) -> bool:
    key = (chat_id, user_id)
    active = key in support_waiting or key in reply_waiting or key in broadcast_waiting or key in broadcast_pending
    support_waiting.discard(key)
    reply_waiting.pop(key, None)
    broadcast_waiting.discard(key)
    broadcast_pending.pop(key, None)
    return active


async def _begin_support(message: types.Message, user_id: int) -> None:
    if message.chat.type != "private":
        await message.answer("Напиши в поддержку в личном чате с ботом.")
        return
    if not ADMIN_CHAT_ID:
        await message.answer("Поддержка сейчас недоступна.", reply_markup=_main_keyboard(user_id))
        return
    _clear_dialog(user_id, message.chat.id)
    support_waiting.add((message.chat.id, user_id))
    await message.answer("Напиши сообщение для поддержки следующим сообщением. /cancel - отменить.",
                         reply_markup=_cancel_keyboard())


async def _setup_commands() -> None:
    commands = [types.BotCommand(command=name, description=description) for name, description in (
        ("start", "Начать работу"), ("help", "Написать в поддержку"),
        ("cancel", "Отменить текущее действие"),
        ('settings', 'Настройки лорбуков'),
    )]
    await bot.set_my_commands(commands)
    await bot.set_chat_menu_button(menu_button=types.MenuButtonCommands())
    if ADMIN_CHAT_ID:
        await bot.set_my_commands(commands + [
            types.BotCommand(command="broadcast", description="Рассылка пользователям"),
            types.BotCommand(command="queue", description="Персонажи в обработке и очереди"),
        ], scope=types.BotCommandScopeChat(chat_id=ADMIN_CHAT_ID))


def _support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Написать админу", callback_data="support:write")]]
    )


def _reply_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Ответить", callback_data=f"support:reply:{user_id}")]]
    )


async def _send_support_message(message: types.Message, text: str) -> None:
    if not ADMIN_CHAT_ID or not message.from_user:
        await message.answer("Поддержка сейчас недоступна.")
        return
    user = message.from_user
    if not _allow_notification("support", user.id, 30):
        await message.answer("Сообщение уже отправлено. Подожди немного перед следующим.")
        return
    username = f"@{user.username}" if user.username else user.full_name
    payload = (
        "Сообщение в поддержку: Janny Bot\n"
        f"User: {username}\n"
        f"User ID: {user.id}\n\n"
        f"{text}"
    )
    try:
        await message.bot.send_message(ADMIN_CHAT_ID, payload, reply_markup=_reply_keyboard(user.id))
    except TelegramAPIError:
        await message.answer("Не удалось отправить сообщение. Попробуй снова или нажми /cancel.")
        return
    support_waiting.discard((message.chat.id, user.id))
    await message.answer("Сообщение отправлено. Я пришлю ответ здесь.", reply_markup=_main_keyboard(user.id))


async def _notify_admin(message: types.Message, url: str, error: Exception, *, alert_service=None) -> None:
    if not ADMIN_CHAT_ID:
        return
    user = message.from_user
    if not _allow_notification("error", user.id if user else 0, 60):
        return
    user_name = (f'@{user.username}' if user.username else user.full_name) if user else "unknown"
    text = (
        "Janny Bot error\n"
        f"User: {user_name}\n"
        f"User ID: {user.id if user else 'unknown'}\n"
        f"Chat ID: {message.chat.id}\n"
        f"URL: {url}\n"
        f"Error type: {type(error).__name__}"
    )
    if isinstance(error, DownloadFailed):
        for source, diagnostic in error.diagnostics.items():
            text += f"\n{source}: {diagnostic['code']}"
            if diagnostic["http_status"] is not None:
                text += f" (HTTP {diagnostic['http_status']})"
            if diagnostic["exit_code"] is not None:
                text += f" [exit {diagnostic['exit_code']}]"
    try:
        if alert_service is not None:
            await alert_service.notify(message, url, text)
        else:
            await (alert_bot or message.bot).send_message(ADMIN_CHAT_ID, text)
    except Exception:
        logging.exception("Failed to notify admin about user error")


def _allow_notification(kind: str, user_id: int, interval: float) -> bool:
    key = (kind, user_id)
    now = monotonic()
    if now - notification_times.get(key, float("-inf")) < interval:
        return False
    notification_times[key] = now
    notification_times.move_to_end(key)
    while len(notification_times) > 2048:
        notification_times.popitem(last=False)
    return True


if __name__ == "__main__":
    asyncio.run(main())
