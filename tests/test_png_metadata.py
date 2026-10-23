"""Janny PNGs can place chara after IDAT, which Image.open alone does not read."""

import base64
from io import BytesIO
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

from PIL import Image

import parser


def trailing_card(card):
    image = BytesIO()
    Image.new('RGB', (2, 2), 'red').save(image, format='PNG')
    png = image.getvalue()
    text = b'chara\x00' + base64.b64encode(json.dumps(card).encode())
    chunk = struct.pack('>I', len(text)) + b'tEXt' + text + struct.pack('>I', zlib.crc32(b'tEXt' + text))
    return png[:-12] + chunk + png[-12:]


class PngMetadataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'card.png'
        self.card = {'data': {'name': 'Fixture', 'description': 'Public', 'personality': 'Definition',
            'first_mes': 'Hello', 'extensions': {'janitorai': {'definition_source': 'janitor_proxy_prompt'}}}}
        self.path.write_bytes(trailing_card(self.card))

    def test_reader_accepts_metadata_after_pixels(self):
        with Image.open(self.path) as image:
            self.assertNotIn('chara', image.info)
        self.assertEqual(parser._read_card(str(self.path)), self.card)

    def test_verified_definition_uses_trailing_metadata_too(self):
        self.assertTrue(parser.has_verified_hidden_definition(str(self.path)))

    def test_normalization_preserves_actual_definition(self):
        from download_flow import DownloadResult, DownloadSource
        parser._mark_download(DownloadResult(str(self.path), DownloadSource.JANNYAI, True))
        result = parser._read_card(str(self.path))
        self.assertEqual(result['data']['personality'], 'Definition')
        self.assertEqual(result['data']['first_mes'], 'Hello')

    def test_truncated_pixels_are_rejected_even_when_metadata_exists(self):
        self.path.write_bytes(self.path.read_bytes()[:-20])
        with self.assertRaises((OSError, ValueError, SyntaxError)):
            parser._read_card(str(self.path))
