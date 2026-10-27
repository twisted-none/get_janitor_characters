from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from download_errors import SourceError
import parser


class JanitorSessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = str(Path(self.directory) / 'card.png')
        self.page = Mock(url='about:blank')
        self.context = Mock()
        self.context.new_page.return_value = self.page
        self.context._download_failure = None
        self.context._download_api_status = None
        self.page.context = self.context
        self.browser = Mock()
        self.urls = []
        self.session = True
        self.raw = {'id': 'fixture', 'name': 'Ada', 'description': 'Public profile',
                    'personality': '', 'first_message': 'Hello', 'is_public': True, 'allow_proxy': True}
        image = BytesIO()
        Image.new('RGB', (2, 2)).save(image, format='PNG')

        def navigate(page, url, **options):
            page.url = url
            self.urls.append(url)
            return SimpleNamespace(status=200)

        self.navigate = self.stack.enter_context(patch.object(parser, '_navigate', side_effect=navigate))
        self.login = self.stack.enter_context(patch.object(parser, '_login_janitor'))
        self.proxy = self.stack.enter_context(patch.object(parser, 'extract_definition', return_value={
            'personality': 'Actual definition', 'scenario': 'Lab', 'first_message': 'Hello'}))
        self.stack.enter_context(patch.object(parser, 'get_access_token', side_effect=lambda *args: 'fake-session' if self.session else ''))
        self.stack.enter_context(patch.object(parser, '_launch', return_value=self.browser))
        self.stack.enter_context(patch.object(parser, '_new_janitor_context', return_value=self.context))
        self.stack.enter_context(patch.object(parser, '_character_file_path', return_value=self.path))
        self.stack.enter_context(patch.object(parser, '_save_janitor_state'))
        self.stack.enter_context(patch.object(parser, '_sync_context_cookies'))
        self.stack.enter_context(patch.object(parser, 'load_data'))
        self.stack.enter_context(patch.dict(parser.GLOBAL_COOKIES, {}, clear=True))
        self.stack.enter_context(patch.object(parser, '_extract_janitor_character', side_effect=lambda *args: dict(self.raw)))
        self.stack.enter_context(patch.object(parser, '_find_janitor_image', return_value='https://cdn.example.invalid/card.png'))
        self.stack.enter_context(patch.object(parser, '_download_image_body', return_value=image.getvalue()))
        self.page.evaluate.return_value = {'scripts': [], 'bodyText': 'Public card'}

    def test_live_session_skips_login_and_exports_recovered_definition(self):
        self.login.side_effect = SourceError('access_denied', http_status=403)
        path = parser._download_janitor_character_sync('fixture')
        self.login.assert_not_called()
        self.assertEqual(self.urls, ['https://janitorai.com/characters/fixture'])
        self.proxy.assert_called_once_with(self.page, self.context, 'fixture')
        self.assertEqual(parser._read_card(path)['data']['personality'], 'Actual definition')
        self.browser.close.assert_called_once()

    def test_missing_session_logs_in_only_after_opening_card_and_reloads(self):
        self.session = False

        def login(page):
            self.urls.append('login')
            page.url = 'https://janitorai.com/login'
            self.session = True

        self.login.side_effect = login
        parser._download_janitor_character_sync('fixture')
        self.assertEqual(self.urls, ['https://janitorai.com/characters/fixture', 'login',
                                     'https://janitorai.com/characters/fixture'])
        self.login.assert_called_once()

    def test_public_definition_does_not_require_a_session(self):
        self.session = False
        self.raw['personality'] = 'Public definition'
        parser._download_janitor_character_sync('fixture')
        self.login.assert_not_called()
        self.proxy.assert_not_called()

    def test_card_access_denied_is_not_disguised_as_a_login_or_proxy_failure(self):
        def denied(page, url, **options):
            page.url = url
            return SimpleNamespace(status=403)

        self.navigate.side_effect = denied
        with self.assertRaises(SourceError) as caught:
            parser._download_janitor_character_sync('fixture')
        self.assertEqual((caught.exception.code, caught.exception.http_status), ('access_denied', 403))
        self.login.assert_not_called()
        self.proxy.assert_not_called()
        self.assertFalse(Path(self.path).exists())
        self.browser.close.assert_called_once()

    def test_missing_session_that_login_cannot_restore_is_auth_required(self):
        self.session = False
        with self.assertRaises(SourceError) as caught:
            parser._download_janitor_character_sync('fixture')
        self.assertEqual(caught.exception.code, 'auth_required')
        self.proxy.assert_not_called()

    def test_private_card_is_rejected_before_login_or_proxy(self):
        self.session = False
        self.raw['is_public'] = False
        with self.assertRaises(SourceError) as caught:
            parser._download_janitor_character_sync('fixture')
        self.assertEqual(caught.exception.code, 'private_card')
        self.login.assert_not_called()
        self.proxy.assert_not_called()

    def test_unauthorized_card_is_retried_once_after_restoring_session(self):
        self.session = False

        def navigate(page, url, **options):
            page.url = url
            return SimpleNamespace(status=200 if self.session else 401)

        self.navigate.side_effect = navigate
        self.login.side_effect = lambda page: setattr(self, 'session', True)
        parser._download_janitor_character_sync('fixture')
        self.assertEqual(self.navigate.call_count, 2)
        self.login.assert_called_once()
        self.proxy.assert_called_once()

    def test_rejected_session_does_not_cause_an_infinite_login_loop(self):
        def navigate(page, url, **options):
            page.url = url
            return SimpleNamespace(status=401)

        self.navigate.side_effect = navigate
        with self.assertRaises(SourceError) as caught:
            parser._download_janitor_character_sync('fixture')
        self.assertEqual(caught.exception.code, 'auth_required')
        self.assertEqual(self.navigate.call_count, 2)
        self.login.assert_not_called()
        self.proxy.assert_not_called()

    def test_off_origin_redirect_never_attempts_login(self):
        def navigate(page, url, **options):
            page.url = 'https://other.example.invalid/characters/fixture'
            return SimpleNamespace(status=200)

        self.navigate.side_effect = navigate
        with self.assertRaises(SourceError) as caught:
            parser._download_janitor_character_sync('fixture')
        self.assertEqual(caught.exception.code, 'unsafe_url')
        self.login.assert_not_called()
        self.proxy.assert_not_called()

    def test_unauthorized_card_is_retried_once_after_restoring_session(self):
        self.session = False

        def navigate(page, url, **options):
            page.url = url
            return SimpleNamespace(status=200 if self.session else 401)

        self.navigate.side_effect = navigate
        self.login.side_effect = lambda page: setattr(self, 'session', True)
        parser._download_janitor_character_sync('fixture')
        self.assertEqual(self.navigate.call_count, 2)
        self.login.assert_called_once()
        self.proxy.assert_called_once()

    def test_rejected_session_does_not_cause_an_infinite_login_loop(self):
        def navigate(page, url, **options):
            page.url = url
            return SimpleNamespace(status=401)

        self.navigate.side_effect = navigate
        with self.assertRaises(SourceError) as caught:
            parser._download_janitor_character_sync('fixture')
        self.assertEqual(caught.exception.code, 'auth_required')
        self.assertEqual(self.navigate.call_count, 2)
        self.login.assert_not_called()
        self.proxy.assert_not_called()

    def test_off_origin_redirect_never_attempts_login(self):
        def navigate(page, url, **options):
            page.url = 'https://other.example.invalid/characters/fixture'
            return SimpleNamespace(status=200)

        self.navigate.side_effect = navigate
        with self.assertRaises(SourceError) as caught:
            parser._download_janitor_character_sync('fixture')
        self.assertEqual(caught.exception.code, 'unsafe_url')
        self.login.assert_not_called()
        self.proxy.assert_not_called()

    def test_proxy_auth_error_keeps_partial_card_and_safe_diagnostics(self):
        self.proxy.side_effect = SourceError('auth_required', 'fixture-secret', http_status=401)
        with self.assertLogs(level='WARNING') as logs:
            path = parser._download_janitor_character_sync('fixture')
        self.assertIn('code=auth_required', '\n'.join(logs.output))
        self.assertNotIn('fixture-secret', '\n'.join(logs.output))
        self.assertEqual(parser._read_card(path)['data']['personality'], '')
