import asyncio
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientSession
from aiogram import Bot
from alert_redelivery import AlertRedelivery, signature, start_alert_server, restore_user
from alert_retries import AlertRetryStore


class RedeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.main_bot = Bot('111111:fake_test_token')
        self.addAsyncCleanup(self.main_bot.session.close)
        self.alert_bot = SimpleNamespace(id=222222, send_message=AsyncMock(),
            delete_message=AsyncMock(), edit_message_reply_markup=AsyncMock(), edit_message_text=AsyncMock())
        self.request = dict(token='a' * 24, user_id=71, chat_id=72, chat_type='private',
            url='https://janitorai.com/characters/fixture', lorebook_mode='both', message_id=13,
            sent_keys=[], delivered=False, username='original_tag', full_name='Настоящее имя', alert_text='Original error')
        self.claim = SimpleNamespace(request=self.request, record_sent=Mock(), close=Mock(),
                                     mark_delivered=Mock(side_effect=lambda: self.request.update(delivered=True)))
        self.store = SimpleNamespace(claim=Mock(return_value=self.claim))
        self.download = AsyncMock(return_value=True)
        self.service = AlertRedelivery(self.main_bot, self.alert_bot, 9, self.store, self.download, None)
        self.addAsyncCleanup(self.service.close)
        self.payload = dict(token='a' * 24, user_id=9, chat_id=9, message_id=13, bot_id=222222)

    async def drain(self):
        await asyncio.gather(*tuple(self.service.tasks))

    async def test_original_recipient_main_bot_and_alert_deleted_after_delivery(self):
        await self.service.submit(self.payload)
        await self.drain()
        message, context = self.download.await_args.args
        self.assertIs(message.bot, self.main_bot)
        self.assertEqual((message.chat.id, message.from_user.id), (72, 71))
        self.assertEqual(context.mode, 'both')
        self.assertEqual(message.from_user.username, 'original_tag')
        self.assertEqual(message.from_user.full_name, 'Настоящее имя')
        self.alert_bot.delete_message.assert_awaited_once_with(9, 13)
        self.claim.close.assert_called_once()
        self.assertFalse(self.service.active)

    async def test_foreign_actor_chat_bot_and_invalid_token_cannot_claim(self):
        for change in ({'user_id': 8}, {'chat_id': 8}, {'bot_id': 123}, {'token': 'bad'}):
            await self.service.submit(self.payload | change)
        self.store.claim.assert_not_called()

    async def test_double_click_during_download_does_not_create_second_task(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def download(*args):
            entered.set()
            await release.wait()
            return True

        self.download.side_effect = download
        await self.service.submit(self.payload)
        await asyncio.wait_for(entered.wait(), 2)
        text = await self.service.submit(self.payload)
        self.assertIn('уже выполняется', text)
        self.store.claim.assert_called_once()
        release.set()
        await self.drain()

    async def test_partial_failure_retains_alert_and_delete_failure_does_not_redownload(self):
        self.download.return_value = False
        await self.service.submit(self.payload)
        await self.drain()
        self.alert_bot.delete_message.assert_not_awaited()
        self.alert_bot.send_message.assert_not_awaited()
        update = self.alert_bot.edit_message_text.await_args.kwargs
        self.assertEqual((update['chat_id'], update['message_id']), (9, 13))
        self.assertTrue(update['text'].startswith('Original error\n\n'))
        self.assertEqual(update['reply_markup'].inline_keyboard[0][0].text, 'Отправить пользователю еще раз')
        self.download.return_value = True
        self.alert_bot.delete_message.side_effect = [RuntimeError('fixture'), None]
        await self.service.submit(self.payload)
        await self.drain()
        await self.service.submit(self.payload)
        await self.drain()
        self.assertEqual(self.download.await_count, 2)
        self.assertEqual(self.alert_bot.delete_message.await_count, 2)
        self.alert_bot.send_message.assert_not_awaited()

    async def test_exception_updates_same_alert_without_new_notification(self):
        self.download.side_effect = RuntimeError('fixture')
        await self.service.submit(self.payload)
        await self.drain()
        self.alert_bot.send_message.assert_not_awaited()
        self.assertIn('Повторная отправка не удалась', self.alert_bot.edit_message_text.await_args.kwargs['text'])

    async def test_legacy_identity_uses_saved_profile_and_missing_profile_uses_id(self):
        profile = SimpleNamespace(get_user_identity=Mock(return_value=dict(username='saved_tag', full_name='Имя Фамилия')))
        user = await restore_user(dict(user_id=71), profile)
        self.assertEqual((user.username, user.full_name), ('saved_tag', 'Имя Фамилия'))
        profile.get_user_identity.return_value = None
        user = await restore_user(dict(user_id=71), profile)
        self.assertIsNone(user.username)
        self.assertEqual(user.full_name, '71')

    async def test_long_original_alert_stays_within_telegram_limit(self):
        self.request['alert_text'] = '😀' * 4000
        self.download.return_value = False
        await self.service.submit(self.payload)
        await self.drain()
        text = self.alert_bot.edit_message_text.await_args.kwargs['text']
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 4096)

    async def test_unavailable_claim_does_not_leave_active_token(self):
        self.store.claim.return_value = None
        await self.service.submit(self.payload)
        self.assertFalse(self.service.active)

    async def test_alert_button_is_attached_only_after_database_binding(self):
        self.store.create = Mock(return_value='a' * 24)
        self.store.bind_message = Mock()
        self.service.preferences = SimpleNamespace(get_lorebook_mode=Mock(return_value='both'))
        self.alert_bot.send_message.return_value = SimpleNamespace(message_id=13)

        async def edit(**kwargs):
            self.store.bind_message.assert_called_once_with('a' * 24, 13)

        self.alert_bot.edit_message_reply_markup.side_effect = edit
        message = SimpleNamespace(from_user=SimpleNamespace(id=71, username='original_tag', full_name='Имя'),
                                  chat=SimpleNamespace(id=72, type='private'))
        await self.service.notify(message, self.request['url'], 'Error')
        self.assertNotIn('reply_markup', self.alert_bot.send_message.await_args.kwargs)
        self.alert_bot.edit_message_reply_markup.assert_awaited_once()
        self.assertEqual(self.store.create.call_args.kwargs['alert_text'], 'Error')
        self.assertEqual(self.store.create.call_args.kwargs['username'], 'original_tag')
        self.assertEqual(self.store.create.call_args.kwargs['full_name'], 'Имя')


class SignedGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_signature_expired_payload_and_valid_request(self):
        service = SimpleNamespace(submit=AsyncMock(return_value='queued'))
        runner = await start_alert_server(service, 'fixture-secret', host='127.0.0.1', port=0)
        self.addAsyncCleanup(runner.cleanup)
        url = f'http://127.0.0.1:{runner.addresses[0][1]}/retry'
        body = json.dumps(dict(timestamp=int(time.time()), token='a' * 24)).encode()
        async with ClientSession() as client:
            async with client.post(url, data=body, headers={'X-Alert-Signature': 'wrong'}) as response:
                self.assertEqual(response.status, 403)
            expired = json.dumps(dict(timestamp=1)).encode()
            async with client.post(url, data=expired,
                    headers={'X-Alert-Signature': signature('fixture-secret', expired)}) as response:
                self.assertEqual(response.status, 400)
            async with client.post(url, data=body,
                    headers={'X-Alert-Signature': signature('fixture-secret', body)}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())['text'], 'queued')
        service.submit.assert_awaited_once()


@unittest.skipUnless(os.getenv('RETRY_FIXTURE_URL'), 'Requires disposable PostgreSQL fixture')
class AlertStoreTests(unittest.TestCase):
    def test_binding_lock_receipts_survive_restart_and_expiry(self):
        url = os.environ['RETRY_FIXTURE_URL']
        if 'janitor-retry-db-fixture-' not in url:
            raise ValueError('Refusing non-fixture database')
        store = AlertRetryStore(url)
        token = store.create(user_id=71, chat_id=72, chat_type='private',
            url='https://janitorai.com/characters/fixture', lorebook_mode='both', bot_id=22, admin_chat_id=9,
            username='saved_tag', full_name='Имя Фамилия', alert_text='Original error')
        store.bind_message(token, 13)
        self.assertIsNone(store.claim(token, 23, 9, 13))
        self.assertIsNone(store.claim(token, 22, 9, 14))
        claim = store.claim(token, 22, 9, 13)
        try:
            self.assertIsNone(store.claim(token, 22, 9, 13))
            claim.record_sent('card')
            claim.record_sent('card')
            claim.mark_delivered()
        finally:
            claim.close()
        restarted = AlertRetryStore(url).claim(token, 22, 9, 13)
        try:
            self.assertEqual(restarted.request['sent_keys'], ['card'])
            self.assertTrue(restarted.request['delivered'])
            self.assertEqual(restarted.request['username'], 'saved_tag')
            self.assertEqual(restarted.request['full_name'], 'Имя Фамилия')
            self.assertEqual(restarted.request['alert_text'], 'Original error')
        finally:
            restarted.close()
        with store._connect() as db:
            db.execute("UPDATE card_alert_retries SET expires_at = now() - interval '1 second' WHERE token = %s", (token,))
        self.assertIsNone(store.claim(token, 22, 9, 13))

    def test_upgrade_existing_table_preserves_request_and_recovers_user_profile(self):
        from analytics import AnalyticsStore
        from aiogram.types import User
        store = AlertRetryStore(os.environ['RETRY_FIXTURE_URL'])
        token = store.create(user_id=81, chat_id=82, chat_type='private', url='https://example.invalid/card',
                             lorebook_mode='both', bot_id=22, admin_chat_id=9)
        store.bind_message(token, 19)
        with store._connect() as db:
            db.execute('ALTER TABLE card_alert_retries DROP COLUMN username, DROP COLUMN full_name, DROP COLUMN alert_text')
        upgraded = AlertRetryStore(os.environ['RETRY_FIXTURE_URL'])
        profile = AnalyticsStore(os.environ['RETRY_FIXTURE_URL'])
        profile.record_user(User(id=81, is_bot=False, username='old_tag', first_name='Имя'))
        claim = upgraded.claim(token, 22, 9, 19)
        try:
            self.assertEqual(claim.request['alert_text'], '')
            user = asyncio.run(restore_user(claim.request, profile))
            self.assertEqual(user.username, 'old_tag')
        finally:
            claim.close()
