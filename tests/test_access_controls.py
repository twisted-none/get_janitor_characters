import ast
import asyncio
from collections import OrderedDict
from pathlib import Path
from secrets import token_hex
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from download_flow import DownloadFailed


class AccessControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8"))
        names = {"_is_admin", "_clear_dialog", "handle_admin_command", "handle_broadcast", "handle_broadcast_confirm", "handle_admin_reply", "_allow_notification", "_notify_admin"}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        self.scope = {"ADMIN_CHAT_ID": 7, "support_waiting": set(), "reply_waiting": {}, "broadcast_waiting": set(),
                      "broadcast_pending": {}, "pending_downloads": set(), "notification_times": OrderedDict(),
                      "monotonic": monotonic, "token_hex": token_hex, "asyncio": asyncio, "_cancel_keyboard": lambda: None,
                      "_main_keyboard": lambda user: None, "InlineKeyboardMarkup": lambda **kw: kw,
                      "InlineKeyboardButton": lambda **kw: kw, "TelegramAPIError": RuntimeError,
                      "TelegramRetryAfter": type("Retry", (Exception,), {})}
        self.scope.update(DownloadFailed=DownloadFailed, alert_bot=None)
        exec(compile("from __future__ import annotations\n" + ast.unparse(ast.Module(body=nodes, type_ignores=[])), "access-tests", "exec"), self.scope)

    def message(self, user=7, chat=1, text="/broadcast", kind="private"):
        return SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(id=chat, type=kind),
                               text=text, answer=AsyncMock(), bot=SimpleNamespace(send_message=AsyncMock()))

    async def test_admin_notification_contains_safe_source_diagnostics(self):
        message = self.message()
        message.from_user.username = "fixture"
        error = DownloadFailed({"datacat": {"code": "http_error", "http_status": 410, "message": "fixture-secret"}})
        await self.scope["_notify_admin"](message, "https://example.invalid/fixture", error)
        text = message.bot.send_message.await_args.args[1]
        self.assertIn("datacat: http_error (HTTP 410)", text)
        self.assertNotIn("fixture-secret", text)

    async def test_non_admin_and_group_cannot_start_broadcast(self):
        for message in (self.message(user=8), self.message(kind="group")):
            await self.scope["handle_admin_command"](message)
        self.assertFalse(self.scope["broadcast_waiting"])

    async def test_other_chat_is_not_broadcast_and_preview_requires_confirmation(self):
        first = self.message()
        await self.scope["handle_admin_command"](first)
        store = SimpleNamespace(broadcast_recipients=lambda: [9])
        other = self.message(chat=2, text="private fixture")
        await self.scope["handle_broadcast"](other, store)
        other.bot.send_message.assert_not_awaited()
        self.assertFalse(self.scope["broadcast_pending"])
        first.text = "approved preview"
        await self.scope["handle_broadcast"](first, store)
        first.bot.send_message.assert_not_awaited()
        nonce = self.scope["broadcast_pending"][(1, 7)][2]
        callback = SimpleNamespace(message=first, from_user=first.from_user, data=f"broadcast:send:{nonce}", answer=AsyncMock())
        await self.scope["handle_broadcast_confirm"](callback, store)
        first.bot.send_message.assert_awaited_once_with(9, "approved preview")
        await self.scope["handle_broadcast_confirm"](callback, store)
        self.assertEqual(first.bot.send_message.await_count, 1)

    async def test_old_button_cannot_confirm_new_preview(self):
        message = self.message(text="new preview")
        self.scope["broadcast_pending"][(1, 7)] = (monotonic(), "new preview", "current")
        callback = SimpleNamespace(message=message, from_user=message.from_user,
                                   data="broadcast:send:obsolete", answer=AsyncMock())
        store = SimpleNamespace(broadcast_recipients=lambda: [9])
        await self.scope["handle_broadcast_confirm"](callback, store)
        message.bot.send_message.assert_not_awaited()
        self.assertIn((1, 7), self.scope["broadcast_pending"])

    async def test_reply_does_not_cross_chats(self):
        self.scope["reply_waiting"][(1, 7)] = 9
        message = self.message(chat=2, text="fixture")
        await self.scope["handle_admin_reply"](message)
        message.bot.send_message.assert_not_awaited()

    def test_admin_id_is_not_taken_from_message_text(self):
        self.assertFalse(self.scope["_is_admin"](8))
        self.assertTrue(self.scope["_is_admin"](7))

    def test_admin_notifications_are_bounded(self):
        allow = self.scope["_allow_notification"]
        self.assertTrue(allow("error", 1, 60))
        self.assertFalse(allow("error", 1, 60))
        for user in range(3000):
            allow("error", user, 60)
        self.assertLessEqual(len(self.scope["notification_times"]), 2048)
