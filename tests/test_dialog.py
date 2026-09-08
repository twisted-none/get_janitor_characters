import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


class DialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8"))
        names = {"_clear_dialog", "handle_support_text", "handle_admin_reply"}
        nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
                 and node.name in names]
        for node in nodes:
            node.decorator_list = []
        self.scope = dict(support_waiting={7}, reply_waiting={}, broadcast_waiting=set(),
                          _is_admin=lambda user_id: True, _send_support_message=AsyncMock())
        code = "from __future__ import annotations\n" + ast.unparse(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(code, "main.py", "exec"), self.scope)
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "CHARACTER_URL_RE" for t in node.targets))
        self.pattern = ast.literal_eval(assignment.value)

    def test_character_urls(self) -> None:
        for url in ("https://jannyai.com/characters/abc", "https://janitorai.com/characters/abc/name?x=1",
                    " HTTPS://JANITORAI.COM/characters/abc "):
            self.assertIsNotNone(re.search(self.pattern, url))
        for url in ("https://jannyai.com.evil.test/characters/abc", "https://jannyai.com/characters/",
                    "https://example.com", "https://jannyai.com/characters/abc extra"):
            self.assertIsNone(re.search(self.pattern, url))

    def test_cancel_clears_all_dialogs(self) -> None:
        self.scope["reply_waiting"][7] = 8
        self.scope["broadcast_waiting"].add(7)
        self.assertTrue(self.scope["_clear_dialog"](7))
        self.assertFalse(self.scope["_clear_dialog"](7))

    async def test_nontext_keeps_support_state(self) -> None:
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), text=None, answer=AsyncMock())
        await self.scope["handle_support_text"](message)
        self.assertIn(7, self.scope["support_waiting"])
        self.scope["_send_support_message"].assert_not_awaited()

    async def test_nontext_keeps_reply_state(self) -> None:
        self.scope["reply_waiting"][7] = 8
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), text=None, answer=AsyncMock())
        await self.scope["handle_admin_reply"](message)
        self.assertEqual(self.scope["reply_waiting"][7], 8)
