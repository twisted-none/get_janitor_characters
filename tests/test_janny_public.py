import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from download_errors import SourceError
from janny_public import extract_public_character, download_public_card


IDENTIFIER = '12345678-1234-1234-1234-123456789abc'


def encoded(value):
    if isinstance(value, dict):
        return [0, {key: encoded(item) for key, item in value.items()}]
    if isinstance(value, list):
        return [1, [encoded(item) for item in value]]
    return [0, value]


class PublicJannyTests(unittest.TestCase):
    def state(self, **changes):
        character = dict(id=IDENTIFIER, name='Ada', description='Public profile',
                         personality='Actual definition\nwith lines', scenario='A room',
                         firstMessage='Hello, {{user}}!', exampleDialogs='Example',
                         creatorName='Author', tags=[{'name': 'Science fiction'}])
        character.update(changes)
        return {'props': json.dumps({'character': encoded(character),
                                    'imageUrl': encoded('https://image.jannyai.com/avatar.webp')}),
                'url': f'https://jannyai.com/characters/{IDENTIFIER}'}

    def test_extracts_full_public_astro_fields_without_treating_profile_as_definition(self):
        raw, image = extract_public_character(self.state(), IDENTIFIER)
        self.assertEqual(raw['personality'], 'Actual definition\nwith lines')
        self.assertEqual(raw['description'], 'Actual definition\nwith lines')
        self.assertEqual(raw['creator_notes'], 'Public profile')
        self.assertEqual(raw['first_message'], 'Hello, {{user}}!')
        self.assertEqual(raw['example_dialogs'], 'Example')
        self.assertEqual(raw['tags'], ['Science fiction'])
        self.assertEqual(image, 'https://image.jannyai.com/avatar.webp')

    def test_rejects_wrong_character_incomplete_definition_and_private_card(self):
        for changes in ({'id': 'other'}, {'personality': ''}, {'firstMessage': ''},
                        {'isPrivate': True}, {'isPublic': False}):
            with self.subTest(changes=changes), self.assertRaises(SourceError):
                extract_public_character(self.state(**changes), IDENTIFIER)

    def test_rejects_redirects_and_non_avatar_hosts(self):
        state = self.state()
        state['url'] = f'https://evil.test/characters/{IDENTIFIER}'
        with self.assertRaises(SourceError):
            extract_public_character(state, IDENTIFIER)

    def test_preserves_real_lorebooks_but_does_not_invent_them(self):
        raw, _ = extract_public_character(self.state(), IDENTIFIER)
        self.assertNotIn('character_book', raw)
        book = {'entries': [{'keys': ['Ada'], 'content': 'World detail'}]}
        raw, _ = extract_public_character(self.state(character_book=book), IDENTIFIER)
        self.assertEqual(raw['character_book'], book)

    def test_invalid_identifier_never_launches_a_browser(self):
        with patch('janny_public.subprocess.run') as run:
            with self.assertRaises(SourceError):
                download_public_card('../secret', 'unused.png')
            run.assert_not_called()

    def test_parser_selects_docker_public_reader_and_cleans_failed_output(self):
        import parser
        with (patch('janny_public.BROWSER_PYTHON', Mock(is_file=lambda: True)),
              patch('janny_public.download_public_card', return_value='fixture.png') as download,
              patch.object(parser, '_download_janny_api_sync') as legacy,
              patch.object(parser, '_character_file_path', return_value='fixture.png')):
            self.assertEqual(parser._download_janny_character_sync(IDENTIFIER), 'fixture.png')
            download.assert_called_once_with(IDENTIFIER, 'fixture.png')
            legacy.assert_not_called()
            download.side_effect = SourceError('challenge_required')
            with patch.object(parser, 'remove_download') as remove:
                with self.assertRaises(SourceError):
                    parser._download_janny_character_sync(IDENTIFIER)
                remove.assert_called_once_with('fixture.png')

    def test_public_export_creates_valid_png_and_drops_all_account_credentials(self):
        import parser
        from PIL import Image
        from io import BytesIO

        image = BytesIO()
        Image.new('RGB', (2, 2)).save(image, format='PNG')
        state = self.state()

        def run(arguments, **options):
            for key in ('BOT_TOKEN', 'DATABASE_URL', 'MY_COOKIES', 'JANNYAI_PASSWORD', 'HTTP_PROXY'):
                self.assertNotIn(key, options['env'])
            self.assertIn('-I', arguments)
            Path(arguments[-1]).write_text(json.dumps(state), encoding='utf-8')
            return Mock(returncode=0)

        with tempfile.TemporaryDirectory() as directory:
            output = str(Path(directory) / 'card.png')
            with (patch('janny_public.subprocess.run', side_effect=run),
                  patch.object(parser, '_download_url_image_body', return_value=image.getvalue()),
                  patch.dict(os.environ, {key: 'fake-secret' for key in (
                      'BOT_TOKEN', 'DATABASE_URL', 'MY_COOKIES', 'JANNYAI_PASSWORD', 'HTTP_PROXY')})):
                download_public_card(IDENTIFIER, output)
            card = parser._read_card(output)
        self.assertEqual(card['spec'], 'chara_card_v2')
        self.assertEqual(card['data']['description'], 'Actual definition\nwith lines')
        self.assertEqual(card['data']['scenario'], 'A room')
        self.assertEqual(card['data']['first_mes'], 'Hello, {{user}}!')
        self.assertEqual(card['data']['mes_example'], 'Example')
        self.assertEqual(card['data']['creator'], 'Author')

    def test_native_timeout_is_reported_without_raw_process_error(self):
        with patch('janny_public.subprocess.run', side_effect=subprocess.TimeoutExpired('fake-secret', 180)):
            with self.assertRaises(SourceError) as caught:
                download_public_card(IDENTIFIER, 'unused.png')
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertNotIn('fake-secret', str(caught.exception))

