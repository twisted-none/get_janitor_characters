"""Export an anonymous public Janny page through the isolated Docker browser."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import UUID

from download_errors import SourceError

BROWSER_PYTHON = Path('/opt/janny-browser/bin/python')
STATE_LIMIT = 2_000_000


def _identifier(value):
    try:
        return str(UUID(value.split('_', 1)[0]))
    except (ValueError, AttributeError):
        raise SourceError('card_invalid') from None


def _decode(value, depth=0):
    # Astro's JSON serialization: [0, scalar/object], [1, array]. Never eval JS.
    if depth > 32 or not isinstance(value, list) or len(value) != 2:
        raise SourceError('invalid_response')
    kind, data = value
    if kind == 0:
        if isinstance(data, dict):
            return {key: _decode(item, depth + 1) for key, item in data.items()}
        if data is None or isinstance(data, (str, bool, int, float)):
            return data
    if kind == 1 and isinstance(data, list):
        return [_decode(item, depth + 1) for item in data]
    raise SourceError('invalid_response')


def extract_public_character(state, character_id):
    identifier = _identifier(character_id)
    address = urlsplit(state.get('url', ''))
    if (address.scheme != 'https' or address.netloc != 'jannyai.com'
            or address.path.split('/')[-1].split('_')[0] != identifier):
        raise SourceError('unsafe_url')
    props_text = state.get('props', '')
    if not isinstance(props_text, str) or len(props_text) > STATE_LIMIT:
        raise SourceError('response_too_large')
    props = json.loads(props_text)
    character = _decode(props.get('character'))
    if not isinstance(character, dict) or character.get('id') != identifier:
        raise SourceError('card_invalid')
    if (any(character.get(key) is True for key in ('isPrivate', 'is_private', 'private'))
            or any(character.get(key) is False for key in ('isPublic', 'is_public', 'public'))
            or character.get('visibility', 'public') != 'public'):
        raise SourceError('private_card')
    def text(key):
        value = character.get(key, '')
        return value if isinstance(value, str) else ''
    if not all(text(key).strip() for key in ('name', 'personality', 'firstMessage')):
        raise SourceError('card_invalid')
    raw = dict(name=text('name'), description=text('personality'), personality=text('personality'),
               creator_notes=text('description'), scenario=text('scenario'),
               first_message=text('firstMessage'), example_dialogs=text('exampleDialogs'),
               creator=text('creatorName'))
    tags = character.get('tags', [])
    if isinstance(tags, list):
        raw['tags'] = [tag if isinstance(tag, str) else tag['name']
                       for tag in tags if isinstance(tag, str)
                       or isinstance(tag, dict) and isinstance(tag.get('name'), str)]
    # Keep actual supplied books/scripts for the existing non-executing lorebook parser.
    for key in ('character_book', 'lorebooks', 'scripts'):
        if isinstance(character.get(key), (dict, list)):
            raw[key] = character[key]
    image = _decode(props.get('imageUrl'))
    if not isinstance(image, str) or urlsplit(image).netloc != 'image.jannyai.com' or urlsplit(image).scheme != 'https':
        raise SourceError('missing_image')
    return raw, image


def download_public_card(character_id, file_path):
    identifier = _identifier(character_id)
    with tempfile.TemporaryDirectory(prefix='janny-public-') as directory:
        root = Path(directory)
        state_path = root / 'public.json'
        # No bot/database credentials, account cookies or persistent user profile.
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'LD_LIBRARY_PATH'}}
        environment.update(HOME=directory, XDG_CACHE_HOME=str(root / 'cache'),
                           XDG_CONFIG_HOME=str(root / 'config'), TMPDIR=directory,
                           PYTHONUNBUFFERED='1')
        try:
            result = subprocess.run([
                '/usr/bin/xvfb-run', '-a', '-s', '-screen 0 1440x1100x24 -nolisten tcp',
                str(BROWSER_PYTHON), '-I', str(Path(__file__).with_name('janny_browser.py')),
                identifier, str(state_path),
            ], cwd=directory, env=environment, timeout=180,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            raise SourceError('timeout') from None
        if not state_path.is_file() or state_path.stat().st_size > STATE_LIMIT:
            raise SourceError('worker_failed', exit_code=result.returncode)
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if 'error' in state:
            raise SourceError.from_payload(state['error'])
        if result.returncode:
            raise SourceError('worker_failed', exit_code=result.returncode)
        raw, image_url = extract_public_character(state, identifier)
        # Import only in the existing worker; retain PNG validation and safe HTTPS client.
        import parser
        card = parser._to_chara_card_v2(raw)
        image = parser._download_url_image_body(image_url, state['url'])
        parser._write_chara_png(image, card, file_path)
        return file_path
