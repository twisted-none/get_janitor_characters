import json
import tempfile
import unittest
from pathlib import Path

import download_trace as trace


class TraceTests(unittest.TestCase):
    def test_title_uses_private_bounded_channel_without_diagnostic_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = []
            with trace.session('a' * 32, 'jannyai', root / 'events.jsonl'), trace.observe(events.append):
                trace.publish_title('  Название\n <b>тест</b>\u202e ' + 'x' * 500)
            title = json.loads(root.joinpath('title.json').read_text())
            self.assertEqual(len(title), 200)
            self.assertNotIn('\u202e', title)
            self.assertFalse(root.joinpath('events.jsonl').exists())
            self.assertEqual(events, [{'event': 'card_title', 'title': title}])

    def test_observer_receives_sanitized_copy_without_changing_saved_events(self):
        observed = []

        def observe(event):
            observed.append(dict(event))
            event['message'] = 'fixture-secret'

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            with trace.session('a' * 32, 'datacat', path), trace.observe(observe):
                trace.emit('started', operation='download_image', token='fixture-secret')
            self.assertNotIn('fixture-secret', path.read_text())
        self.assertEqual(observed[0]['operation'], 'download_image')
        self.assertNotIn('token', observed[0])

    def test_events_are_bounded_and_never_contain_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            with trace.session('a' * 32, 'datacat', path):
                trace.emit('response', operation='identify', http_status=403,
                           token='fixture-secret', message='fixture-secret', bytes=25)
            payload = json.loads(path.read_text())
            self.assertEqual(payload['http_status'], 403)
            self.assertNotIn('fixture-secret', path.read_text())

    def test_operation_records_failure_without_exception_text(self):
        with self.assertLogs(level='INFO') as logs:
            with self.assertRaises(ValueError):
                with trace.operation('parse_card'):
                    raise ValueError('fixture-secret')
        self.assertIn('invalid_response', '\n'.join(logs.output))
        self.assertNotIn('fixture-secret', '\n'.join(logs.output))

    def test_urls_never_expose_userinfo_query_or_unknown_paths(self):
        self.assertEqual(trace.endpoint('https://datacat.run/api/characters/abc?token=secret'),
                         'datacat.run/api/characters/{id}')
        self.assertNotIn('secret', trace.endpoint('https://user:secret@datacat.run/secret'))

    def test_untrusted_worker_cannot_put_arbitrary_text_into_fields(self):
        record = trace.sanitize({'code': 'fixture-secret', 'error_type': 'fixture-secret',
                                 'endpoint': 'fixture-secret', 'message': 'fixture-secret'})
        self.assertNotIn('fixture-secret', json.dumps(record))
