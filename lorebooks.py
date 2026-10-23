"""Extract captured lorebooks as data, never execute upstream scripts."""

from copy import deepcopy
import json
import re

import json5

MODES = ('separate', 'embedded', 'both')
MAX_SOURCE = 2_000_000
ENTRY_FIELDS = {'keys', 'secondary_keys', 'content', 'enabled', 'insertion_order', 'case_sensitive',
                'constant', 'selective', 'position', 'priority', 'comment', 'extensions', 'id',
                'probability', 'useProbability', 'selectiveLogic', 'order', 'disable', 'key', 'keysecondary'}


def _keys(value):
    if isinstance(value, str):
        return [v.strip() for v in value.split(',') if v.strip()]
    return [str(v) for v in value] if isinstance(value, list) else []


def normalize(value, name='Lorebook'):
    if isinstance(value, list):
        value = {'entries': value}
    if not isinstance(value, dict):
        return None
    entries = value.get('entries', value.get('loreEntries', value.get('lore_entries')))
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        return None
    result = []
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise ValueError('invalid_lore_entry')
        entry = {k: deepcopy(v) for k, v in raw.items() if k in ENTRY_FIELDS}
        entry['id'] = index
        entry['keys'] = _keys(raw.get('keys', raw.get('key', raw.get('keywords', []))))
        entry['secondary_keys'] = _keys(raw.get('secondary_keys', raw.get('keysecondary', [])))
        entry['content'] = str(raw.get('content') or '\n'.join(str(raw[k]) for k in ('personality', 'scenario') if raw.get(k)))
        entry.setdefault('enabled', not bool(raw.get('disable', False)))
        entry.setdefault('insertion_order', raw.get('order', index))
        extra = {k: deepcopy(v) for k, v in raw.items() if k not in ENTRY_FIELDS and k not in {'personality', 'scenario', 'keywords'}}
        if extra:
            extensions = entry.get('extensions')
            entry['extensions'] = dict(extensions) if isinstance(extensions, dict) else {}
            entry['extensions']['source_entry_options'] = extra
        result.append(entry)
    book = {k: deepcopy(value[k]) for k in ('description', 'scan_depth', 'token_budget', 'recursive_scanning', 'extensions') if k in value}
    book.update(name=str(value.get('name') or value.get('title') or name), entries=result)
    return book


def _source(text):
    if not isinstance(text, str) or len(text) > MAX_SOURCE:
        return None
    try:
        return json.loads(text)
    except ValueError:
        if text.lstrip().startswith(('[', '{')):
            return json5.loads(text, allow_duplicate_keys=False)
        match = re.search(r'\bloreEntries\s*=\s*(?=\[)', text)
        if not match:
            return None
        # The parser consumes one JSON5 value, not the surrounding program.
        return json5.loads(text, start=match.end(), consume_trailing=False, allow_duplicate_keys=False)


def collect(raw, card):
    books, missing, fingerprints, ids = [], 0, set(), set()

    def add(value, name='Lorebook'):
        book = normalize(value, name)
        if book is None:
            return False
        fingerprint = json.dumps(book, sort_keys=True, ensure_ascii=True, allow_nan=False)
        if fingerprint not in fingerprints:
            fingerprints.add(fingerprint)
            books.append(book)
        return True

    embedded = card.get('data', card).get('character_book')
    if embedded is not None:
        try:
            if not add(embedded):
                missing += 1
        except (ValueError, TypeError, RecursionError):
            missing += 1
    scripts = raw.get('scripts') or raw.get('lorebooks') or []
    if isinstance(scripts, dict):
        scripts = list(scripts.values())
    if not isinstance(scripts, list):
        return books, missing + 1
    for item in scripts:
        if not isinstance(item, dict):
            missing += 1
            continue
        if str(item.get('type', 'lorebook')).lower() != 'lorebook':
            continue
        identifier = str(item.get('id') or item.get('script_id') or '')
        if identifier and identifier in ids:
            continue
        if identifier:
            ids.add(identifier)
        if any(item.get(key) is False for key in ('is_public', 'is_code_public', 'script_detail_available',
                                                 'isPublic', 'isCodePublic', 'scriptDetailAvailable')):
            missing += 1
            continue
        try:
            value = item if any(k in item for k in ('entries', 'loreEntries', 'lore_entries')) else _source(
                item.get('script') or item.get('original_script') or '')
            if not add(value, str(item.get('title') or item.get('name') or 'Lorebook')):
                missing += 1
        except (ValueError, TypeError, RecursionError):
            missing += 1
    return books, missing


def attach(raw, card):
    books, missing = collect(raw, card)
    if not books and not missing:
        return card
    data = card.setdefault('data', {})
    if not isinstance(data.get('extensions'), dict):
        data['extensions'] = {}
    extensions = data.setdefault('extensions', {})
    if not isinstance(extensions.get('download_bot'), dict):
        extensions['download_bot'] = {}
    metadata = extensions['download_bot']
    metadata.update(lorebooks=books, lorebooks_missing=missing)
    return card


def merge(books):
    entries = []
    for book in books:
        for original in book['entries']:
            entry = deepcopy(original)
            entry['id'] = len(entries)
            entries.append(entry)
    return {'name': 'Character lorebooks', 'entries': entries, 'extensions': {
        'source_books': [{k: deepcopy(v) for k, v in book.items() if k != 'entries'} for book in books]}}


def export_card(card, mode):
    if mode not in MODES:
        raise ValueError('invalid_lorebook_mode')
    result = deepcopy(card)
    data = result.setdefault('data', {})
    extensions = data.get('extensions', {})
    metadata = extensions.get('download_bot', {}) if isinstance(extensions, dict) else {}
    if not isinstance(metadata, dict):
        metadata = {}
    captured = metadata.pop('lorebooks', None)
    missing = metadata.pop('lorebooks_missing', 0)
    missing = missing if type(missing) is int and 0 <= missing <= 1000000 else 0
    if captured is None:
        books, count = collect({}, card)
    else:
        books, count = collect({'lorebooks': captured}, {})
    missing += count
    data.pop('character_book', None)
    if mode in ('embedded', 'both') and books:
        data['character_book'] = merge(books)
    return result, books if mode in ('separate', 'both') else [], missing


def standalone(book):
    """SillyTavern world-info JSON, with sequential stable entry IDs."""
    entries = {}
    for index, original in enumerate(book['entries']):
        entry = deepcopy(original)
        entry.update(uid=index, key=entry['keys'], keysecondary=entry.get('secondary_keys', []),
                     disable=not entry.get('enabled', True), order=entry.get('insertion_order', index))
        entries[str(index)] = entry
    return dict(book, entries=entries)
