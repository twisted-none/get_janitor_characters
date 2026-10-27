"""Durable, bounded retries for cards confirmed absent from both mirrors."""

import asyncio
import logging
from threading import Lock

import psycopg
from psycopg.rows import dict_row

RETRY_DELAY_SECONDS = 30 * 60
MAX_ATTEMPTS = 3


def waiting_text(completed_attempts):
    return ('Карточка пока не найдена на DataCat и JannyAI. Возможно, она ещё не появилась. '
            f'Проверю снова через 30 минут. Повтор {completed_attempts} из 2. '
            'После этого запрос встанет в общую очередь.')


EXHAUSTED_TEXT = ('Карточка пока не найдена на DataCat и JannyAI. '
                  'После двух повторных проверок автоматические попытки завершены. '
                  'Попробуй отправить ссылку позже.')


class RetryStore:
    def __init__(self, database_url):
        self._url = database_url
        self._lock = Lock()
        with self._connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS pending_card_retries (
                token TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
                chat_type TEXT NOT NULL, character_id TEXT NOT NULL, url TEXT NOT NULL,
                title TEXT NOT NULL, completed_attempts INTEGER NOT NULL CHECK (completed_attempts BETWEEN 1 AND 2),
                state TEXT NOT NULL CHECK (state IN ('waiting', 'running')),
                due_at TIMESTAMPTZ NOT NULL
            )''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_card_retries_due ON pending_card_retries (state, due_at)')
            db.execute("ALTER TABLE pending_card_retries ADD COLUMN IF NOT EXISTS username TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE pending_card_retries ADD COLUMN IF NOT EXISTS full_name TEXT NOT NULL DEFAULT ''")

    def _connect(self):
        return psycopg.connect(self._url, autocommit=True, row_factory=dict_row, connect_timeout=10,
                               options='-c statement_timeout=15000 -c lock_timeout=5000')

    def schedule(self, request, *, resumed=False):
        """Rescheduling never recreates a cancelled/deleted persistent request."""
        if request['completed_attempts'] not in (1, 2):
            raise ValueError('retry limit exceeded')
        with self._lock, self._connect() as db:
            if resumed:
                return db.execute('''UPDATE pending_card_retries SET state = 'waiting',
                    completed_attempts = %s, title = %s, due_at = now() + %s * interval '1 second'
                    WHERE token = %s AND state = 'running' RETURNING token''',
                    (request['completed_attempts'], request['title'], RETRY_DELAY_SECONDS,
                     request['token'])).fetchone() is not None
            return db.execute('''INSERT INTO pending_card_retries
                (token, user_id, chat_id, message_id, chat_type, character_id, url, title,
                 completed_attempts, username, full_name, state, due_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'waiting', now() + %s * interval '1 second')
                ON CONFLICT (token) DO NOTHING RETURNING token''',
                tuple(request[key] for key in ('token', 'user_id', 'chat_id', 'message_id', 'chat_type',
                      'character_id', 'url', 'title', 'completed_attempts')) +
                (request.get('username', ''), request.get('full_name', ''), RETRY_DELAY_SECONDS,)).fetchone() is not None

    def exists(self, token):
        with self._lock, self._connect() as db:
            return db.execute('SELECT token FROM pending_card_retries WHERE token = %s', (token,)).fetchone() is not None

    def finish(self, token):
        with self._lock, self._connect() as db:
            db.execute('DELETE FROM pending_card_retries WHERE token = %s', (token,))

    def cancel(self, token, user_id, chat_id, message_id):
        with self._lock, self._connect() as db:
            return db.execute('''DELETE FROM pending_card_retries WHERE token = %s AND user_id = %s
                AND chat_id = %s AND message_id = %s RETURNING token''',
                (token, user_id, chat_id, message_id)).fetchone() is not None

    def recover_running(self):
        # The bot has one polling process. Interrupted retries resume after its restart.
        with self._lock, self._connect() as db:
            db.execute("UPDATE pending_card_retries SET state = 'waiting' WHERE state = 'running'")

    def claim_due(self):
        with self._lock, self._connect() as db:
            return db.execute('''WITH due AS (
                SELECT token FROM pending_card_retries WHERE state = 'waiting' AND due_at <= now()
                ORDER BY due_at, token FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE pending_card_retries AS r SET state = 'running' FROM due
              WHERE r.token = due.token RETURNING r.*''').fetchone()


async def run_retries(store, resume, *, poll_interval=15):
    recovered = False
    while True:
        try:
            if not recovered:
                await asyncio.to_thread(store.recover_running)
                recovered = True
            request = await asyncio.to_thread(store.claim_due)
            if request is None:
                await asyncio.sleep(poll_interval)
                continue
            # A separate task lets a user cancel this retry without stopping the scheduler.
            await asyncio.create_task(resume(request))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.warning('Retry scheduler failed: %s', type(error).__name__)
            await asyncio.sleep(poll_interval)
