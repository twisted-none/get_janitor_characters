import asyncio
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from typing import Any

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
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://janny:janny@postgres:5432/janny_bot")
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
from analytics import AnalyticsStore
from metrics import run_metrics_server

bot = Bot(token=TOKEN)
alert_bot = Bot(token=ALERT_BOT_TOKEN) if ALERT_BOT_TOKEN else None
dp = Dispatcher()
CHARACTER_URL_RE = (r"(?i)^\s*https?://(?:www\.)?(?P<source>janitorai|jannyai)\.com/characters/"
                    r"(?P<id>[a-zA-Z0-9_-]+)(?:/[^\s?#]*)?(?:\?[^\s#]*)?(?:#[^\s]*)?\s*$")
support_waiting: set[int] = set()
reply_waiting: dict[int, int] = {}
broadcast_waiting: set[int] = set()
pending_downloads: set[int] = set()
download_semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)


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
    _clear_dialog(message.from_user.id if message.from_user else 0)
    if message.from_user:
        await _record_user(analytics_store, message.from_user, started=True)
    await message.answer(
        "Привет! Я помогу сохранить персонажа из JannyAI или JanitorAI.\n\n"
        "Пришли ссылку на страницу персонажа, и я отправлю его карточку в PNG.\n"
        "Если что-то пошло не так, напиши в поддержку через /help.\n\n"
        "Пришли ссылку, и начнем.",
        reply_markup=_main_keyboard(message.from_user.id if message.from_user else 0),
    )


@dp.message(Command("help"))
async def handle_help(message: types.Message) -> None:
    if not message.from_user:
        return
    await _begin_support(message, message.from_user.id)


@dp.message(Command("cancel"))
async def handle_cancel(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    cancelled = _clear_dialog(user_id)
    await message.answer("Текущее действие отменено." if cancelled else "Сейчас нечего отменять.",
                         reply_markup=_main_keyboard(user_id))


@dp.message(Command("broadcast", "queue"))
async def handle_admin_command(message: types.Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    if not _is_admin(user_id):
        await message.answer("Команда доступна только администратору.")
        return
    if (message.text or "").split()[0].split("@")[0] == "/queue":
        await message.answer(f"Персонажей в обработке и очереди: {len(pending_downloads)}.")
        return
    _clear_dialog(user_id)
    broadcast_waiting.add(user_id)
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
    raw_id = (callback.data or "").rsplit(":", 1)[-1]
    if not raw_id.isdigit():
        await callback.answer("Некорректный получатель.", show_alert=True)
        return
    user_id = int(raw_id)
    _clear_dialog(callback.from_user.id)
    reply_waiting[callback.from_user.id] = user_id
    await callback.answer()
    await callback.message.answer("Напиши ответ пользователю следующим сообщением. /cancel - отменить.",
                                  reply_markup=_cancel_keyboard())


@dp.message(lambda message: message.from_user and message.from_user.id in reply_waiting)
async def handle_admin_reply(message: types.Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    user_id = reply_waiting[message.from_user.id]
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст ответа.")
        return
    try:
        await message.bot.send_message(user_id, f"Ответ поддержки:\n\n{text}")
    except TelegramAPIError:
        await message.answer("Не удалось доставить ответ. Попробуй снова или нажми /cancel.")
        return
    reply_waiting.pop(message.from_user.id, None)
    await message.answer("Ответ отправлен.", reply_markup=_main_keyboard(message.from_user.id))


@dp.message(lambda message: message.from_user and message.from_user.id in support_waiting)
async def handle_support_text(message: types.Message) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст сообщения.")
        return
    await _send_support_message(message, text)


@dp.message(lambda message: message.from_user and message.from_user.id in broadcast_waiting)
async def handle_broadcast(message: types.Message, analytics_store: AnalyticsStore) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст для рассылки. /cancel - отменить.")
        return
    try:
        recipients = await asyncio.to_thread(analytics_store.broadcast_recipients)
    except Exception:
        logging.exception("Failed to load broadcast recipients")
        await message.answer("Не удалось получить список пользователей. Попробуй позже.")
        return
    broadcast_waiting.discard(message.from_user.id)
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
                         reply_markup=_main_keyboard(message.from_user.id))


@dp.message(F.text.regexp(CHARACTER_URL_RE))
async def handle_link(message: types.Message, analytics_store: AnalyticsStore) -> None:
    match = re.search(CHARACTER_URL_RE, message.text or "")
    if not match:
        return

    source = match.group("source").lower()
    character_id = match.group("id")
    character_url = match.group(0).strip()
    queued = download_semaphore.locked()
    wait_msg = await message.answer("⏳ В очереди на скачивание..." if queued else "⏳ Скачиваю...")
    user_id = message.from_user.id if message.from_user else 0
    download_id = await _start_download(analytics_store, user_id, character_id, character_url)
    job_id = id(wait_msg)
    pending_downloads.add(job_id)
    logging.info("Получена ссылка от user_id=%s, source=%s, character_id=%s", user_id, source, character_id)

    try:
        async with download_semaphore:
            if queued:
                await wait_msg.edit_text("⏳ Скачиваю...")
            logging.info(
                "Старт download slot: source=%s, character_id=%s, concurrency=%s",
                source,
                character_id,
                DOWNLOAD_CONCURRENCY,
            )
            file_path = await asyncio.wait_for(
                parser.download_character(character_id, source=source),
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
        logging.info("Файл готов: %s, размер=%s байт", file_path, os.path.getsize(file_path))
        await message.answer_document(document=FSInputFile(file_path, filename=f"{character_id}.png"))
        try:
            os.remove(file_path)
        except OSError:
            logging.warning("Не удалось удалить временный файл: %s", file_path)
        await _finish_download(analytics_store, download_id, success=True)
        await wait_msg.delete()
    except asyncio.TimeoutError:
        logging.exception("Таймаут скачивания персонажа %s", character_id)
        await _finish_download(analytics_store, download_id, success=False, error="download timeout")
        await _notify_admin(message, character_url, TimeoutError("download timeout"))
        await wait_msg.edit_text(
            "Скачивание заняло слишком много времени. Попробуй позже или напиши в поддержку через /help.",
            reply_markup=_support_keyboard(),
        )
    except Exception as e:
        logging.exception("Ошибка скачивания персонажа %s", character_id)
        await _finish_download(analytics_store, download_id, success=False, error=str(e))
        await _notify_admin(message, character_url, e)
        await wait_msg.edit_text("Не удалось скачать персонажа. Проверь, что страница доступна, "
                                 "и попробуй снова. Если ошибка повторяется, напиши в поддержку через /help.",
                                 reply_markup=_support_keyboard())
    finally:
        pending_downloads.discard(job_id)


@dp.message()
async def handle_invalid_input(message: types.Message) -> None:
    text = (message.text or "").strip()
    response = ("Неизвестная команда. Открой меню или нажми /help." if text.startswith("/") else
                "Нужна ссылка на страницу персонажа JannyAI или JanitorAI.\n\n"
                "Открой персонажа на jannyai.com или janitorai.com и скопируй адрес "
                "из браузера. Ссылка должна содержать /characters/ и идентификатор персонажа.")
    await message.answer(response, reply_markup=_main_keyboard(message.from_user.id if message.from_user else 0))


async def main() -> None:
    analytics_store = AnalyticsStore(DATABASE_URL)
    dp["analytics_store"] = analytics_store
    dp.message.middleware(RegisterUserMiddleware())
    metrics_task = asyncio.create_task(run_metrics_server(analytics_store, METRICS_HOST, METRICS_PORT))
    try:
        await _setup_commands()
        await dp.start_polling(bot)
    finally:
        metrics_task.cancel()
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
    rows = [[types.KeyboardButton(text="/start"), types.KeyboardButton(text="/help")]]
    if _is_admin(user_id):
        rows.append([types.KeyboardButton(text="/broadcast"), types.KeyboardButton(text="/queue")])
    return types.ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _cancel_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(keyboard=[[types.KeyboardButton(text="/cancel")]], resize_keyboard=True)


def _clear_dialog(user_id: int) -> bool:
    active = user_id in support_waiting or user_id in reply_waiting or user_id in broadcast_waiting
    support_waiting.discard(user_id)
    reply_waiting.pop(user_id, None)
    broadcast_waiting.discard(user_id)
    return active


async def _begin_support(message: types.Message, user_id: int) -> None:
    if not ADMIN_CHAT_ID:
        await message.answer("Поддержка сейчас недоступна.", reply_markup=_main_keyboard(user_id))
        return
    _clear_dialog(user_id)
    support_waiting.add(user_id)
    await message.answer("Напиши сообщение для поддержки следующим сообщением. /cancel - отменить.",
                         reply_markup=_cancel_keyboard())


async def _setup_commands() -> None:
    commands = [types.BotCommand(command=name, description=description) for name, description in (
        ("start", "Начать работу"), ("help", "Написать в поддержку"),
        ("cancel", "Отменить текущее действие"),
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
    support_waiting.discard(user.id)
    await message.answer("Сообщение отправлено. Я пришлю ответ здесь.", reply_markup=_main_keyboard(user.id))


async def _notify_admin(message: types.Message, url: str, error: Exception) -> None:
    if not ADMIN_CHAT_ID:
        return
    user = message.from_user
    user_name = (user.username or user.full_name) if user else "unknown"
    text = (
        "Janny Bot error\n"
        f"User: {user_name}\n"
        f"User ID: {user.id if user else 'unknown'}\n"
        f"Chat ID: {message.chat.id}\n"
        f"URL: {url}\n"
        f"Error: {error}"
    )
    try:
        await (alert_bot or message.bot).send_message(ADMIN_CHAT_ID, text)
    except Exception:
        logging.exception("Failed to notify admin about user error")


if __name__ == "__main__":
    asyncio.run(main())
