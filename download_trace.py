"""Bounded, allowlisted events; no headers, bodies or arbitrary error messages."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import logging
import re
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

from download_errors import describe_error, ERROR_CODES, ERROR_TYPES

_state = ContextVar('download_trace', default=('', '', None))
_TEXT = {'event', 'operation', 'job', 'source', 'endpoint', 'method', 'code', 'error_type', 'reason', 'content_type', 'character_id'}
_NUMBERS = {'http_status', 'duration_ms', 'bytes', 'timeout_ms', 'books', 'entries', 'missing', 'exit_code'}
_BOOLS = {'definition_available', 'embedded_book', 'partial'}
MAX_BYTES = 131072
_VALUES = {
    'event': {'response', 'request', 'started', 'completed', 'failed', 'body_received', 'card_ready',
              'source_started', 'source_completed', 'fallback', 'partial_selected', 'exports_ready',
              'sent', 'request_variant', 'worker_event', 'received'},
    'operation': {'http', 'identify', 'datacat_api', 'datacat_identify', 'datacat_lookup', 'datacat',
                  'jannyai', 'proxy', 'llm', 'browser_launch', 'navigate', 'login', 'login_email',
                  'login_password', 'login_submit', 'parse_card', 'write_png', 'download_image',
                  'janitor_api', 'proxy_definition', 'export', 'telegram_send', 'lorebooks'},
    'source': {'datacat', 'jannyai', 'proxy', 'llm', ''},
    'method': {'GET', 'POST', 'OPTIONS', 'HEAD', 'DELETE', 'PUT', 'PATCH'},
    'reason': {'definition_missing', 'all_sources_exhausted', 'source_kind_janitor', 'default'},
    'code': ERROR_CODES,
    'error_type': ERROR_TYPES,
    'content_type': {'application/json', 'text/html', 'image/png', 'image/jpeg', 'image/webp', 'other'},
}


def current_job():
    return _state.get()[0]


def job(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with session(current_job() or uuid4().hex):
            return await function(*args, **kwargs)
    return wrapped


def endpoint(url):
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        if not any(host == d or host.endswith('.' + d) for d in ('datacat.run', 'janitorai.com', 'jannyai.com')):
            return '<media>'
        path = parsed.path
        if re.fullmatch(r'/api/liberator/identify|/api/v1/download|/login|/signin|/mb/chats|/generateAlpha', path):
            return host + path
        for prefix in ('/api/characters/', '/characters/', '/mb/characters/', '/mb/chats/'):
            if path.startswith(prefix):
                return host + prefix + '{id}'
        return host + '/<resource>'
    except (ValueError, TypeError):
        return '<invalid>'


def sanitize(record):
    if not isinstance(record, dict):
        return {}
    result = {}
    for key, value in record.items():
        if key == 'content_type' and (not isinstance(value, str) or value not in _VALUES[key]):
            value = 'other'
        if key in _VALUES and (not isinstance(value, str) or value not in _VALUES[key]):
            continue
        if key == 'job' and (not isinstance(value, str) or not re.fullmatch('[a-f0-9]{32}', value)):
            continue
        if key == 'endpoint' and value not in ('<media>', '<invalid>'):
            if not isinstance(value, str) or endpoint('https://' + value) != value:
                continue
        if key in _TEXT and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_./{}<>:-]{1,160}', value):
            result[key] = value
        elif key in _NUMBERS and type(value) is int and -255 <= value <= 10**12:
            result[key] = value
        elif key in _BOOLS and type(value) is bool:
            result[key] = value
    return result


@contextmanager
def session(job, source='', path=None):
    token = _state.set((job, source, path))
    try:
        yield
    finally:
        _state.reset(token)


def emit(event, **fields):
    job, source, path = _state.get()
    record = sanitize(dict(fields, event=event, job=job, source=source))
    if path is not None:
        try:
            if not path.exists() or path.stat().st_size < MAX_BYTES:
                with path.open('a', encoding='utf-8') as output:
                    output.write(json.dumps(record) + '\n')
        except OSError:
            pass  # Diagnostics must not change the download result.
    else:
        logging.info('download_event %s', json.dumps(record, sort_keys=True))


def relay(path, job, source):
    if not path.is_file() or path.stat().st_size > MAX_BYTES + 4096:
        return
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[:512]
    except (OSError, UnicodeError):
        return
    with session(job, source):
        for line in lines:
            try:
                record = sanitize(json.loads(line))
            except (ValueError, TypeError):
                continue
            event = record.pop('event', 'worker_event')
            record.pop('job', None)
            record.pop('source', None)
            emit(event, **record)


@contextmanager
def operation(name, **fields):
    started = monotonic()
    emit('started', operation=name, **fields)
    try:
        yield
    except Exception as exc:
        diagnostic = describe_error(exc).to_payload()
        emit('failed', operation=name, duration_ms=round((monotonic()-started)*1000), **diagnostic)
        raise
    else:
        emit('completed', operation=name, duration_ms=round((monotonic()-started)*1000))


def traced(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with operation(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate
