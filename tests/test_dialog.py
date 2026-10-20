import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


class DialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath("main.py").read_text(encoding="utf-8"))
        names = {"_clear_dialog", "handle_support_text", "handle_admin_reply", "handle_invalid_input"}
        nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
                 and node.name in names]
        for node in nodes:
            node.decorator_list = []
        self.scope = dict(support_waiting={(1, 7)}, reply_waiting={}, broadcast_waiting=set(), broadcast_pending={},
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
        self.scope["reply_waiting"][(1, 7)] = 8
        self.scope["broadcast_waiting"].add((1, 7))
        self.assertTrue(self.scope["_clear_dialog"](7, 1))
        self.assertFalse(self.scope["_clear_dialog"](7, 1))

    def test_localized_character_urls_keep_source_and_identifier(self) -> None:
        for source in ("janitorai", "jannyai"):
            for locale in ("ru", "en", "pt-BR", "zh-Hant-TW"):
                with self.subTest(source=source, locale=locale):
                    url = f"https://{source}.com/{locale}/characters/abc_character-eri?x=1#details"
                    match = re.search(self.pattern, url)
                    self.assertIsNotNone(match)
                    self.assertEqual(match.group("source"), source)
                    self.assertEqual(match.group("id"), "abc")

    def test_character_slug_does_not_change_identifier(self) -> None:
        identifier = "6460e31a-aa31-418f-8571-841afe8921e9"
        slugs = ("character-frederick-%E1%81%84%E1%81%83-twisted-disney",
                 "character-\u0444\u0440\u0435\u0434\u0435\u0440\u0438\u043a-\U0001f5a4",
                 "character-frederick-%F0%9F%96%A4", "character-" + "a" * 250)
        for source in ("janitorai", "jannyai"):
            for prefix in ("", "ru/", "pt-BR/"):
                for slug in slugs:
                    with self.subTest(source=source, prefix=prefix, slug=slug):
                        url = f"https://{source}.com/{prefix}characters/{identifier}_{slug}?x=1#details"
                        match = re.search(self.pattern, url)
                        self.assertIsNotNone(match)
                        self.assertEqual(match.group("id"), identifier)

    def test_malformed_slug_does_not_bypass_url_checks(self) -> None:
        for url in ("https://janitorai.com.evil.test/characters/abc_title-%E1%81%84",
                    "https://janitorai.com/characters/abc_title-%ZZ",
                    "https://janitorai.com/characters/abc_title-%",
                    "https://janitorai.com/characters/abc_title with spaces",
                    "https://janitorai.com/characters/abc_title\\evil",
                    "https://janitorai.com/characters/" + "a" * 201 + "_title"):
            with self.subTest(url=url):
                self.assertIsNone(re.search(self.pattern, url))

    def test_locale_support_does_not_accept_other_paths_or_hosts(self) -> None:
        for url in ("https://janitorai.com.evil.test/ru/characters/abc",
                    "https://janitorai.com@evil.test/ru/characters/abc",
                    "https://janitorai.com/settings/characters/abc",
                    "https://janitorai.com/ru/en/characters/abc",
                    "https://janitorai.com/123/characters/abc",
                    "https://janitorai.com/ru//characters/abc",
                    "https://janitorai.com/ru/characters/",
                    "https://janitorai.com/ru/characters/" + "a" * 201):
            with self.subTest(url=url):
                self.assertIsNone(re.search(self.pattern, url))

    async def test_invalid_input_guidance_mentions_only_janitor(self) -> None:
        self.scope["_main_keyboard"] = lambda user_id: None
        message = SimpleNamespace(text="not a character URL", from_user=SimpleNamespace(id=7), answer=AsyncMock())
        await self.scope["handle_invalid_input"](message)
        message.answer.assert_awaited_once_with(
            "Нужна ссылка на страницу персонажа JanitorAI.\n\n"
            "Открой персонажа на janitorai.com и скопируй адрес из браузера. "
            "Ссылка должна содержать /characters/ и идентификатор персонажа.",
            reply_markup=None,
        )

    async def test_nontext_keeps_support_state(self) -> None:
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), text=None, answer=AsyncMock())
        await self.scope["handle_support_text"](message)
        self.assertIn((1, 7), self.scope["support_waiting"])
        self.scope["_send_support_message"].assert_not_awaited()

    async def test_nontext_keeps_reply_state(self) -> None:
        self.scope["reply_waiting"][(1, 7)] = 8
        message = SimpleNamespace(from_user=SimpleNamespace(id=7), chat=SimpleNamespace(id=1, type="private"), text=None, answer=AsyncMock())
        await self.scope["handle_admin_reply"](message)
        self.assertEqual(self.scope["reply_waiting"][(1, 7)], 8)
