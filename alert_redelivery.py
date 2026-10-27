"""Receive signed callbacks from the one shared alert-bot poller."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
import re
import time

from aiohttp import web
from aiogram.types import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

PREFIX = 'janny:retry:'
NOTICE = 'Проблема со скачиванием исправлена. Вот ваш персонаж: '


async def restore_user(request, preferences):
    identity = request
    if not request.get('full_name'):
        try:
            identity = await asyncio.to_thread(preferences.get_user_identity, request['user_id']) or request
        except Exception:
            logging.warning('Cannot restore original user identity')
    return User(id=request['user_id'], is_bot=False,
                username=identity.get('username') or None,
                first_name=identity.get('full_name') or str(request['user_id']))


def signature(secret, body):
    return hmac.new(secret.encode(), b'janny-alert-v1\n' + body, hashlib.sha256).hexdigest()


def keyboard(token, *, busy=False):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text='Отправляется…' if busy else 'Отправить пользователю еще раз',
        callback_data=PREFIX + token)]])


@dataclass(frozen=True)
class Redelivery:
    mode: str
    sent_keys: tuple[str, ...]
    record_sent: Callable[[str], Awaitable[None]]
    notice: str = NOTICE


class AlertRedelivery:
    def __init__(self, main_bot, alert_bot, admin_id, store, download, preferences):
        self.main_bot, self.alert_bot, self.admin_id = main_bot, alert_bot, admin_id
        self.store, self.download, self.preferences = store, download, preferences
        self.active = set()
        self.tasks = set()

    async def notify(self, message, url, text):
        token = None
        try:
            mode = await asyncio.to_thread(self.preferences.get_lorebook_mode, message.from_user.id)
            token = await asyncio.to_thread(self.store.create, user_id=message.from_user.id,
                chat_id=message.chat.id, chat_type=message.chat.type, url=url, lorebook_mode=mode,
                bot_id=self.alert_bot.id, admin_chat_id=self.admin_id,
                username=getattr(message.from_user, 'username', None) or '',
                full_name=getattr(message.from_user, 'full_name', '') or '', alert_text=text)
        except Exception as error:
            logging.warning('Cannot save alert retry: %s', type(error).__name__)
        # A storage failure must not suppress the original error alert.
        sent = await self.alert_bot.send_message(self.admin_id, text)
        if token:
            try:
                await asyncio.to_thread(self.store.bind_message, token, sent.message_id)
                await self._markup({'message_id': sent.message_id}, keyboard(token))
            except Exception as error:
                logging.warning('Cannot bind alert retry button: %s', type(error).__name__)

    async def submit(self, payload):
        if any(type(payload.get(key)) is not int for key in ('user_id', 'chat_id', 'message_id', 'bot_id')):
            return 'Некорректная кнопка.'
        if (not self.admin_id or payload.get('user_id') != self.admin_id
                or payload.get('chat_id') != self.admin_id or payload.get('bot_id') != self.alert_bot.id):
            return 'Нет доступа.'
        token = payload.get('token', '')
        if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{24}', token) is None:
            return 'Некорректная кнопка.'
        if type(payload.get('message_id')) is not int or payload['message_id'] <= 0:
            return 'Некорректная кнопка.'
        if token in self.active or len(self.active) >= 2:
            return 'Отправка уже выполняется. Дождись завершения.'
        self.active.add(token)
        claim = None
        handed_off = False
        acquisition = asyncio.create_task(asyncio.to_thread(self.store.claim, token, payload['bot_id'],
                                                            payload['chat_id'], payload['message_id']))
        try:
            try:
                claim = await asyncio.shield(acquisition)
            except asyncio.CancelledError:
                claim = await acquisition
                raise
            if claim is None:
                return 'Алерт истёк, недоступен или уже обрабатывается.'
            task = asyncio.create_task(self._run(token, claim))
            self.tasks.add(task)
            task.add_done_callback(self._done)
            handed_off = True
            claim = None  # The task owns the database session from here.
            return 'Ставлю повторную загрузку в очередь.'
        finally:
            if claim is not None:
                await asyncio.to_thread(claim.close)
            if not handed_off:
                self.active.discard(token)

    async def _run(self, token, claim):
        request = claim.request
        try:
            if not request['delivered']:
                await self._markup(request, keyboard(token, busy=True))
                message = Message(message_id=0, date=datetime.now(UTC), text=request['url'],
                    from_user=await restore_user(request, self.preferences),
                    chat=Chat(id=request['chat_id'], type=request['chat_type'])).as_(self.main_bot)

                async def record_sent(key):
                    write = asyncio.create_task(asyncio.to_thread(claim.record_sent, key))
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        await write
                        raise

                context = Redelivery(request['lorebook_mode'], tuple(request['sent_keys']), record_sent)
                complete = await self.download(message, context)
                if complete:
                    write = asyncio.create_task(asyncio.to_thread(claim.mark_delivered))
                    try:
                        await asyncio.shield(write)
                    except asyncio.CancelledError:
                        await write
                        raise
            if request['delivered']:
                try:
                    await self.alert_bot.delete_message(self.admin_id, request['message_id'])
                except Exception:
                    await self._markup(request, keyboard(token))
                    await self._notice(request, token, 'Карточка отправлена. Повторное нажатие повторит только удаление алерта.')
            else:
                await self._markup(request, keyboard(token))
                await self._notice(request, token, 'Не удалось отправить все файлы. Алерт сохранён, можно повторить попытку.')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.warning('Card alert redelivery failed: %s', type(error).__name__)
            await self._markup(request, keyboard(token))
            await self._notice(request, token, 'Повторная отправка не удалась. Алерт сохранён.')
        finally:
            try:
                await asyncio.to_thread(claim.close)
            finally:
                self.active.discard(token)

    def _done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.warning('Card redelivery cleanup failed: %s', type(task.exception()).__name__)

    async def _markup(self, request, markup):
        try:
            await self.alert_bot.edit_message_reply_markup(chat_id=self.admin_id,
                message_id=request['message_id'], reply_markup=markup)
        except Exception:
            logging.warning('Cannot update card alert button')

    async def _notice(self, request, token, text):
        try:
            base = request.get('alert_text') or (
                f"Janny Bot error\nUser ID: {request['user_id']}\nURL: {request['url']}")
            # Leave room for the result even when the original alert is long.
            base = base.encode('utf-16-le')[:7000].decode('utf-16-le', errors='ignore')
            await self.alert_bot.edit_message_text(chat_id=self.admin_id, message_id=request['message_id'],
                text=base + '\n\n' + text, reply_markup=keyboard(token), parse_mode=None)
        except Exception:
            logging.warning('Cannot report card redelivery result')

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def start_alert_server(service, secret, *, host='0.0.0.0', port=8001):
    async def retry(request):
        body = await request.read()
        if not secret or len(body) > 2048 or not hmac.compare_digest(
                request.headers.get('X-Alert-Signature', ''), signature(secret, body)):
            raise web.HTTPForbidden()
        try:
            payload = json.loads(body)
            if (not isinstance(payload, dict) or type(payload.get('timestamp')) is not int
                    or abs(time.time() - payload['timestamp']) > 60):
                raise ValueError('Invalid callback')
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None
        return web.json_response({'text': await service.submit(payload)})

    app = web.Application(client_max_size=2048)
    app.router.add_post('/retry', retry)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
