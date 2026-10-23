import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from PIL import Image, PngImagePlugin
from card_delivery import deliver


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_send_failure_keeps_delivered_card_and_cleans_files(self):
        from aiogram.exceptions import TelegramBadRequest
        from aiogram.methods import SendDocument
        from unittest.mock import patch
        paths = []
        def prepare_fixture(source, directory, identifier, mode):
            card = Path(directory) / 'card.png'
            book = Path(directory) / 'book.json'
            card.write_bytes(b'fixture')
            book.write_bytes(b'{}')
            paths.extend([card, book])
            return card, [book], 0
        error = TelegramBadRequest(method=SendDocument(chat_id=1, document='fixture'), message='fixture failure')
        message = SimpleNamespace(answer_document=AsyncMock(side_effect=[None, error]), answer=AsyncMock())
        with patch('card_delivery.prepare', side_effect=prepare_fixture):
            await deliver(message, SimpleNamespace(file_path='fixture', caption=None), 'fixture', 'separate')
        message.answer.assert_awaited_once()
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_all_modes_send_expected_files_and_cleanup(self):
        for mode, expected in [('separate', 3), ('embedded', 1), ('both', 3)]:
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'input.png'
                books = [{'name': 'A', 'entries': [{'content': 'A', 'keys': ['a']}]},
                         {'name': 'B', 'entries': [{'content': 'B', 'keys': ['b']}]}]
                card = {'data': {'name': 'Fixture', 'first_mes': 'Hi', 'extensions': {
                    'download_bot': {'lorebooks': books, 'lorebooks_missing': 1}}}}
                info = PngImagePlugin.PngInfo()
                info.add_text('chara', base64.b64encode(json.dumps(card).encode()).decode())
                Image.new('RGB', (4, 4)).save(source, pnginfo=info)
                paths = []
                async def send(document, caption=None):
                    path = Path(document.path)
                    self.assertTrue(path.is_file())
                    paths.append(path)
                    if path.suffix == '.png':
                        with Image.open(path) as image:
                            data = json.loads(base64.b64decode(image.info['chara']))['data']
                        self.assertEqual('character_book' in data, mode != 'separate')
                        self.assertIn('1', caption)
                message = SimpleNamespace(answer_document=AsyncMock(side_effect=send))
                await deliver(message, SimpleNamespace(file_path=str(source), caption=None), 'fixture', mode)
                self.assertEqual(len(paths), expected)
                self.assertTrue(source.exists())
                self.assertTrue(all(not p.exists() for p in paths))
