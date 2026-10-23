import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from analytics import AnalyticsStore
import lorebooks


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(Path(__file__).resolve().parents[1].joinpath('main.py').read_text(encoding='utf-8'))
        names = {'handle_lorebook_setting', 'handle_settings'}
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
        for node in nodes:
            node.decorator_list = []
        self.scope = {'asyncio': asyncio, 'lorebooks': lorebooks, '_settings_keyboard': lambda mode: mode,
                      '_clear_dialog': MagicMock()}
        exec('from __future__ import annotations\n' + ast.unparse(ast.Module(body=nodes, type_ignores=[])), self.scope)
        self.store = SimpleNamespace(set_lorebook_mode=MagicMock(), get_lorebook_mode=MagicMock(return_value='both'))
        self.callback = SimpleNamespace(data='settings:lorebooks:both', from_user=SimpleNamespace(id=7),
            message=SimpleNamespace(chat=SimpleNamespace(type='private'), edit_reply_markup=AsyncMock()), answer=AsyncMock())

    async def test_update_uses_authenticated_user_not_callback_id(self):
        await self.scope['handle_lorebook_setting'](self.callback, self.store)
        self.store.set_lorebook_mode.assert_called_once_with(7, 'both')

    async def test_selecting_current_mode_does_not_edit_unchanged_markup(self):
        self.callback.message.reply_markup = 'both'
        await self.scope['handle_lorebook_setting'](self.callback, self.store)
        self.callback.message.edit_reply_markup.assert_not_awaited()

    async def test_invalid_modes_and_group_callbacks_are_refused(self):
        for mode in ('../etc', 'both:999'):
            self.callback.data = 'settings:lorebooks:' + mode
            await self.scope['handle_lorebook_setting'](self.callback, self.store)
        self.callback.data = 'settings:lorebooks:both'
        self.callback.message.chat.type = 'group'
        await self.scope['handle_lorebook_setting'](self.callback, self.store)
        self.store.set_lorebook_mode.assert_not_called()

    async def test_setting_storage_failure_does_not_claim_success(self):
        self.store.set_lorebook_mode.side_effect = OSError()
        await self.scope['handle_lorebook_setting'](self.callback, self.store)
        self.callback.message.edit_reply_markup.assert_not_awaited()
        self.assertTrue(self.callback.answer.await_args.kwargs['show_alert'])

    def test_sql_is_parameterized_and_default_is_separate(self):
        store = AnalyticsStore.__new__(AnalyticsStore)
        from threading import Lock
        store.lock = Lock()
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None
        with patch.object(store, '_connect') as connection:
            connection.return_value.__enter__.return_value = db
            self.assertEqual(store.get_lorebook_mode(7), 'separate')
            store.set_lorebook_mode(7, 'both')
            self.assertEqual(db.execute.call_args.args[1], (7, 'both'))
            with self.assertRaises(ValueError):
                store.set_lorebook_mode(7, 'bad')
