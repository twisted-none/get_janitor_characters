import unittest
from lorebooks import collect, merge, export_card


class LorebookTests(unittest.TestCase):
    def test_all_books_and_all_entries_are_collected_without_execution(self):
        raw = {'scripts': [
            {'id': 'one', 'type': 'lorebook', 'title': 'One',
             'script': "const loreEntries = [{keys:['a'],content:'A'}, {keys:['b'],content:'B'}]; throw Error('never');"},
            {'id': 'two', 'type': 'lorebook', 'entries': {'0': {'content': 'C'}}},
            {'id': 'three', 'type': 'lorebook', 'is_code_public': False, 'script': 'secret'},
        ]}
        books, missing = collect(raw, {})
        self.assertEqual([len(b['entries']) for b in books], [2, 1])
        self.assertEqual(missing, 1)
        self.assertEqual(len(merge(books)['entries']), 3)

    def test_missing_link_and_malformed_code_are_not_silently_lost(self):
        books, missing = collect({'scripts': [
            {'type': 'lorebook', 'id': 'missing'},
            {'type': 'lorebook', 'script': 'loreEntries = malicious();'},
            {'type': 'script', 'script': 'not a lorebook'},
        ]}, {})
        self.assertEqual(books, [])
        self.assertEqual(missing, 2)

    def test_export_modes_keep_books_separate_or_embed_without_duplicate_ids(self):
        books = [{'name': 'A', 'entries': [{'id': 0, 'content': 'a'}]},
                 {'name': 'B', 'entries': [{'id': 0, 'content': 'b'}]}]
        card = {'data': {'character_book': books[0], 'extensions': {'download_bot': {'lorebooks': books}}}}
        separate, files, missing = export_card(card, 'separate')
        self.assertNotIn('character_book', separate['data'])
        self.assertEqual(len(files), 2)
        embedded, files, _ = export_card(card, 'embedded')
        self.assertEqual(files, [])
        self.assertEqual([e['id'] for e in embedded['data']['character_book']['entries']], [0, 1])
        self.assertEqual(len(export_card(card, 'both')[1]), 2)
        self.assertEqual(missing, 0)

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            export_card({'data': {}}, '../bad')
