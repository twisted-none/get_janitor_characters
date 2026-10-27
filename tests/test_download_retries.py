import asyncio
import os
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, Mock

from download_errors import SourceError
from download_flow import DownloadFailed
from download_retries import RetryStore, RETRY_DELAY_SECONDS, run_retries
from janny_browser import document_status


class RetryPolicyTests(unittest.TestCase):
    def test_only_both_confirmed_missing_enable_delayed_checks(self):
        def failure(first, second):
            return DownloadFailed({'datacat': SourceError(first).to_payload(),
                                   'jannyai': SourceError(second).to_payload()})
        self.assertTrue(failure('not_found', 'not_found').missing_from_mirrors)
        for code in ('challenge_required', 'access_denied', 'timeout', 'card_invalid', 'network_error'):
            self.assertFalse(failure('not_found', code).missing_from_mirrors)
            self.assertFalse(failure(code, 'not_found').missing_from_mirrors)
        self.assertFalse(DownloadFailed().missing_from_mirrors)
        self.assertEqual(RETRY_DELAY_SECONDS, 1800)

    def test_only_exact_top_level_response_can_confirm_janny_missing(self):
        page = SimpleNamespace(main_frame=object())
        url = 'https://jannyai.com/characters/123'
        response = SimpleNamespace(url=url, status=404, frame=page.main_frame,
                                   request=SimpleNamespace(is_navigation_request=lambda: True))
        self.assertEqual(document_status(response, page, url), 404)
        response.status = 403
        self.assertEqual(document_status(response, page, url), 403)
        response.url = 'https://challenges.cloudflare.com/404'
        self.assertIsNone(document_status(response, page, url))
        response.url = url
        response.frame = object()
        self.assertIsNone(document_status(response, page, url))


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_due_work_resumes_and_idle_scheduler_does_not_busy_poll(self):
        entered = asyncio.Event()
        store = SimpleNamespace(recover_running=Mock(), claim_due=Mock(side_effect=[{'token': 'a'}, None]))

        async def resume(request):
            self.assertEqual(request['token'], 'a')
            entered.set()

        task = asyncio.create_task(run_retries(store, resume, poll_interval=10))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.sleep(.03)
            self.assertEqual(store.claim_due.call_count, 2)
            store.recover_running.assert_called_once()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


@unittest.skipUnless(os.getenv('RETRY_FIXTURE_URL'), 'Requires disposable PostgreSQL fixture')
class RetryStorageTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ['RETRY_FIXTURE_URL']
        if 'janitor-retry-db-fixture-' not in self.url:
            raise ValueError('Refusing a database outside the disposable fixture')
        self.store = RetryStore(self.url)
        with self.store._connect() as db:
            db.execute('TRUNCATE pending_card_retries')
        self.request = dict(token='a' * 24, user_id=7, chat_id=10, message_id=11, chat_type='private',
                            character_id='fixture', url='https://janitorai.com/characters/fixture',
                            title='Тест', completed_attempts=1)

    def make_due(self):
        with self.store._connect() as db:
            db.execute("UPDATE pending_card_retries SET due_at = now() - interval '1 second'")

    def test_persistence_delay_claim_and_recovery_after_restart(self):
        self.assertTrue(self.store.schedule(self.request))
        self.assertIsNone(self.store.claim_due())
        with self.store._connect() as db:
            seconds = db.execute('SELECT extract(epoch FROM due_at - now()) AS seconds FROM pending_card_retries').fetchone()['seconds']
        self.assertGreater(seconds, 1795)
        self.assertLessEqual(seconds, 1800)
        self.make_due()
        request = self.store.claim_due()
        self.assertEqual(request['completed_attempts'], 1)
        self.assertIsNone(self.store.claim_due())
        restarted = RetryStore(self.url)
        restarted.recover_running()
        self.assertEqual(restarted.claim_due()['token'], self.request['token'])
        self.request['completed_attempts'] = 2
        self.assertTrue(restarted.schedule(self.request, resumed=True))
        self.assertIsNone(restarted.claim_due())
        self.make_due()
        self.assertEqual(restarted.claim_due()['completed_attempts'], 2)
        restarted.finish(self.request['token'])
        self.assertIsNone(restarted.claim_due())

    def test_owner_checks_and_cancelled_request_cannot_be_rescheduled(self):
        self.store.schedule(self.request)
        self.assertFalse(self.store.cancel(self.request['token'], 8, 10, 11))
        self.assertFalse(self.store.cancel(self.request['token'], 7, 20, 11))
        self.assertFalse(self.store.cancel(self.request['token'], 7, 10, 21))
        self.make_due()
        self.assertIsNotNone(self.store.claim_due())
        self.assertTrue(self.store.cancel(self.request['token'], 7, 10, 11))
        self.assertFalse(self.store.schedule(self.request, resumed=True))
        self.assertFalse(self.store.exists(self.request['token']))

    def test_third_completed_attempt_cannot_be_scheduled(self):
        self.request['completed_attempts'] = 3
        with self.assertRaises(ValueError):
            self.store.schedule(self.request)

    def test_concurrent_claims_only_return_a_due_request_once(self):
        self.store.schedule(self.request)
        self.make_due()
        second = RetryStore(self.url)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda store: store.claim_due(), (self.store, second)))
        self.assertEqual(sum(claim is not None for claim in claims), 1)
