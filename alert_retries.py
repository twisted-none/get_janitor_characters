"""Persistent admin redelivery requests and per-file delivery receipts."""

import re
from secrets import token_urlsafe

import psycopg
from psycopg.rows import dict_row


class AlertClaim:
    def __init__(self, db, request):
        self.db, self.request = db, request

    def record_sent(self, key):
        if not re.fullmatch(r'card|book:[a-f0-9]{64}', key):
            raise ValueError('Invalid delivery receipt')
        self.db.execute('''UPDATE card_alert_retries SET sent_keys = array_append(sent_keys, %s)
            WHERE token = %s AND NOT (%s = ANY(sent_keys))''', (key, self.request['token'], key))
        if key not in self.request['sent_keys']:
            self.request['sent_keys'].append(key)

    def mark_delivered(self):
        if 'card' not in self.request['sent_keys']:
            raise ValueError('Card has not been delivered')
        self.db.execute('UPDATE card_alert_retries SET delivered = TRUE WHERE token = %s',
                        (self.request['token'],))
        self.request['delivered'] = True

    def close(self):
        # Closing the session releases its advisory lock, including on process failure.
        self.db.close()


class AlertRetryStore:
    def __init__(self, database_url):
        self._url = database_url
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS card_alert_retries (
                token TEXT PRIMARY KEY, user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL,
                chat_type TEXT NOT NULL, url TEXT NOT NULL, lorebook_mode TEXT NOT NULL,
                bot_id BIGINT NOT NULL, admin_chat_id BIGINT NOT NULL, message_id BIGINT,
                sent_keys TEXT[] NOT NULL DEFAULT '{}', delivered BOOLEAN NOT NULL DEFAULT FALSE,
                expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days'
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_card_alert_expiry ON card_alert_retries (expires_at)')
            db.execute("ALTER TABLE card_alert_retries ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE card_alert_retries ADD COLUMN IF NOT EXISTS full_name TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE card_alert_retries ADD COLUMN IF NOT EXISTS alert_text TEXT NOT NULL DEFAULT ''")

    def _connect(self):
        return psycopg.connect(self._url, autocommit=True, row_factory=dict_row, connect_timeout=10,
                               options='-c statement_timeout=15000 -c lock_timeout=5000')

    def create(self, *, user_id, chat_id, chat_type, url, lorebook_mode, bot_id, admin_chat_id,
               username='', full_name='', alert_text=''):
        if user_id <= 0 or len(url) > 4096 or lorebook_mode not in ('separate', 'embedded', 'both'):
            raise ValueError('Invalid alert retry')
        token = token_urlsafe(18)
        with self._connect() as db:
            db.execute('DELETE FROM card_alert_retries WHERE expires_at <= now()')
            db.execute('''INSERT INTO card_alert_retries
                (token, user_id, chat_id, chat_type, url, lorebook_mode, bot_id, admin_chat_id,
                 username, full_name, alert_text)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                (token, user_id, chat_id, chat_type, url, lorebook_mode, bot_id, admin_chat_id,
                 username, full_name, alert_text))
        return token

    def bind_message(self, token, message_id):
        with self._connect() as db:
            db.execute('UPDATE card_alert_retries SET message_id = %s WHERE token = %s AND message_id IS NULL',
                        (message_id, token))

    def claim(self, token, bot_id, chat_id, message_id):
        db = self._connect()
        try:
            locked = db.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 19384)) AS locked',
                                 (token,)).fetchone()['locked']
            if locked:
                request = db.execute('''SELECT * FROM card_alert_retries WHERE token = %s AND bot_id = %s
                    AND admin_chat_id = %s AND message_id = %s AND expires_at > now()''',
                    (token, bot_id, chat_id, message_id)).fetchone()
                if request is not None:
                    return AlertClaim(db, request)
        except BaseException:
            db.close()
            raise
        db.close()
        return None
