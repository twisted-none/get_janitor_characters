from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any

import psycopg
from psycopg.rows import dict_row


@dataclass(frozen=True, slots=True)
class UserStats:
    user_id: int
    username: str
    full_name: str
    downloads: int
    last_character_url: str


@dataclass(frozen=True, slots=True)
class CharacterStats:
    character_id: str
    url: str
    downloads: int


@dataclass(frozen=True, slots=True)
class AnalyticsSnapshot:
    started_users: int
    active_users_30d: int
    download_attempts: int
    successful_downloads: int
    success_ratio: float
    users: list[UserStats]
    top_characters: list[CharacterStats]


class AnalyticsStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.lock = Lock()
        self._initialize()

    def record_user(self, user: Any, *, started: bool = False) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO bot_users (user_id, username, full_name, first_seen_at, last_seen_at, started_at)
                VALUES (%s, %s, %s, now(), now(), CASE WHEN %s THEN now() ELSE NULL END)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    full_name = EXCLUDED.full_name,
                    last_seen_at = now(),
                    started_at = CASE WHEN %s THEN COALESCE(bot_users.started_at, now()) ELSE bot_users.started_at END
                """,
                (user.id, user.username or "", user.full_name or "", started, started),
            )

    def start_download(self, user_id: int, character_id: str, url: str) -> int:
        with self.lock, self._connect() as db:
            row = db.execute(
                """
                INSERT INTO downloads (user_id, character_id, url, status, attempted_at)
                VALUES (%s, %s, %s, 'attempt', now())
                RETURNING id
                """,
                (user_id, character_id, url),
            ).fetchone()
            return int(row["id"])

    def finish_download(self, download_id: int, *, success: bool, error: str = "") -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """
                UPDATE downloads
                SET status = %s, error = %s, finished_at = now()
                WHERE id = %s
                """,
                ("success" if success else "error", error[:500], download_id),
            )

    def broadcast_recipients(self) -> list[int]:
        with self.lock, self._connect() as db:
            return [int(row["user_id"]) for row in db.execute(
                "SELECT user_id FROM bot_users WHERE started_at IS NOT NULL ORDER BY user_id"
            )]

    def snapshot(self) -> AnalyticsSnapshot:
        with self.lock, self._connect() as db:
            started = _scalar(db, "SELECT COUNT(*) FROM bot_users WHERE started_at IS NOT NULL")
            active = _scalar(db, "SELECT COUNT(DISTINCT user_id) FROM downloads WHERE attempted_at >= now() - interval '30 days'")
            attempts = _scalar(db, "SELECT COUNT(*) FROM downloads")
            successes = _scalar(db, "SELECT COUNT(*) FROM downloads WHERE status = 'success'")
            users = [
                UserStats(int(row["user_id"]), row["username"] or "", row["full_name"] or "", int(row["downloads"] or 0), row["last_character_url"] or "")
                for row in db.execute(
                    """
                    SELECT u.user_id, u.username, u.full_name, COUNT(d.id) AS downloads,
                        (SELECT d2.url FROM downloads d2 WHERE d2.user_id = u.user_id AND d2.status = 'success'
                         ORDER BY d2.finished_at DESC NULLS LAST, d2.id DESC LIMIT 1) AS last_character_url
                    FROM bot_users u
                    LEFT JOIN downloads d ON d.user_id = u.user_id AND d.status = 'success'
                    GROUP BY u.user_id
                    ORDER BY downloads DESC, u.last_seen_at DESC
                    """
                )
            ]
            top = [
                CharacterStats(row["character_id"], row["url"], int(row["downloads"]))
                for row in db.execute(
                    """
                    SELECT character_id, COALESCE(NULLIF(MAX(url), ''), character_id) AS url, COUNT(*) AS downloads
                    FROM downloads
                    WHERE status = 'success'
                    GROUP BY character_id
                    ORDER BY downloads DESC, MAX(finished_at) DESC NULLS LAST
                    LIMIT 5
                    """
                )
            ]
        return AnalyticsSnapshot(started, active, attempts, successes, successes / attempts if attempts else 0.0, users, top)

    def _initialize(self) -> None:
        with self.lock, self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    started_at TIMESTAMPTZ
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES bot_users(user_id) ON DELETE CASCADE,
                    character_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('attempt', 'success', 'error')),
                    error TEXT NOT NULL DEFAULT '',
                    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    finished_at TIMESTAMPTZ
                )
                """
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_janny_downloads_user_status ON downloads(user_id, status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_janny_downloads_attempted_at ON downloads(attempted_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_janny_downloads_character_status ON downloads(character_id, status)")
            db.execute(
                """
                CREATE OR REPLACE VIEW analytics_summary AS
                SELECT
                    (SELECT COUNT(*) FROM bot_users WHERE started_at IS NOT NULL) AS started_users,
                    (SELECT COUNT(DISTINCT user_id) FROM downloads WHERE attempted_at >= now() - interval '30 days') AS active_users_30d,
                    (SELECT COUNT(*) FROM downloads) AS download_attempts,
                    (SELECT COUNT(*) FROM downloads WHERE status = 'success') AS successful_downloads,
                    COALESCE((SELECT COUNT(*)::double precision FROM downloads WHERE status = 'success') /
                             NULLIF((SELECT COUNT(*)::double precision FROM downloads), 0), 0) AS success_ratio
                """
            )
            db.execute(
                """
                CREATE OR REPLACE VIEW analytics_users AS
                SELECT u.user_id, u.username, u.full_name, COUNT(d.id) AS downloaded_characters,
                    (SELECT d2.url FROM downloads d2 WHERE d2.user_id = u.user_id AND d2.status = 'success'
                     ORDER BY d2.finished_at DESC NULLS LAST, d2.id DESC LIMIT 1) AS last_character_url,
                    u.first_seen_at, u.last_seen_at, u.started_at
                FROM bot_users u
                LEFT JOIN downloads d ON d.user_id = u.user_id AND d.status = 'success'
                GROUP BY u.user_id
                """
            )
            db.execute(
                """
                CREATE OR REPLACE VIEW analytics_top_characters AS
                SELECT character_id, COALESCE(NULLIF(MAX(url), ''), character_id) AS url, COUNT(*) AS downloads
                FROM downloads
                WHERE status = 'success'
                GROUP BY character_id
                ORDER BY downloads DESC, MAX(finished_at) DESC NULLS LAST
                LIMIT 5
                """
            )

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, autocommit=True, row_factory=dict_row)


def render_prometheus(snapshot: AnalyticsSnapshot) -> str:
    lines = [
        "# HELP janny_bot_started_users_total Users who sent /start.",
        "# TYPE janny_bot_started_users_total gauge",
        f"janny_bot_started_users_total {snapshot.started_users}",
        "# HELP janny_bot_active_users_30d Users who sent a character link in the last 30 days.",
        "# TYPE janny_bot_active_users_30d gauge",
        f"janny_bot_active_users_30d {snapshot.active_users_30d}",
        "# HELP janny_bot_download_attempts_total Total character download attempts.",
        "# TYPE janny_bot_download_attempts_total gauge",
        f"janny_bot_download_attempts_total {snapshot.download_attempts}",
        "# HELP janny_bot_successful_downloads_total Successful character downloads.",
        "# TYPE janny_bot_successful_downloads_total gauge",
        f"janny_bot_successful_downloads_total {snapshot.successful_downloads}",
        "# HELP janny_bot_download_success_ratio Successful downloads divided by all attempts.",
        "# TYPE janny_bot_download_success_ratio gauge",
        f"janny_bot_download_success_ratio {snapshot.success_ratio:.6f}",
        "# HELP janny_bot_user_downloads_total Successful downloads by user.",
        "# TYPE janny_bot_user_downloads_total gauge",
    ]
    for user in snapshot.users:
        lines.append(f"janny_bot_user_downloads_total{{{_labels(user_id=str(user.user_id), username=user.username, full_name=user.full_name, last_character_url=user.last_character_url)}}} {user.downloads}")
    lines.extend(["# HELP janny_bot_top_character_downloads Top 5 characters by downloads.", "# TYPE janny_bot_top_character_downloads gauge"])
    for rank, character in enumerate(snapshot.top_characters, 1):
        lines.append(f"janny_bot_top_character_downloads{{{_labels(rank=str(rank), character_id=character.character_id, url=character.url)}}} {character.downloads}")
    return "\n".join(lines) + "\n"


def _labels(**values: str) -> str:
    return ",".join(f'{key}="{_escape_label(value)}"' for key, value in values.items())


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _scalar(db: psycopg.Connection, query: str) -> int:
    return int(db.execute(query).fetchone()["count"] or 0)
