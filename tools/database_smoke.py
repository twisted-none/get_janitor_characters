"""Verify analytics, health and backup/restore against a disposable local PostgreSQL."""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from aiohttp import ClientSession
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analytics import AnalyticsStore
from metrics import start_metrics_server
from tools.db_backup import create_backup


async def main():
    if os.environ.get("LOCAL_DATABASE_FIXTURE") != "1":
        raise ValueError("Only disposable local database fixtures are allowed")
    database_url = os.environ["DATABASE_URL"]
    fixture = psycopg.conninfo.conninfo_to_dict(database_url)
    if (not fixture.get("host", "").startswith("janitor-db-fixture-")
            or fixture.get("dbname") != "fixture" or fixture.get("user") != "fixture"):
        raise ValueError("Refusing a database outside the disposable fixture")
    store = AnalyticsStore(database_url)
    store.record_user(SimpleNamespace(id=42, username="fixture", full_name="Local fixture"), started=True)
    download = store.start_download(42, "fixture", "https://example.invalid/fixture")
    store.finish_download(download, success=True)
    assert store.snapshot().successful_downloads == 1
    runner = await start_metrics_server(store, "127.0.0.1", 0)
    try:
        port = runner.addresses[0][1]
        async with ClientSession() as session:
            for endpoint in ("health", "metrics"):
                async with session.get(f"http://127.0.0.1:{port}/{endpoint}") as response:
                    assert response.status == 200
                    assert await response.text()
        print("ANALYTICS_HEALTH_METRICS: OK (local fixture)")
    finally:
        await runner.cleanup()
    with tempfile.TemporaryDirectory(prefix="db-fixture-") as directory:
        dump = Path(directory) / "fixture.dump"
        create_backup(dump, os.environ)
        assert dump.with_suffix(".dump.sha256").is_file()
        with psycopg.connect(database_url, autocommit=True) as db:
            db.execute("CREATE DATABASE restore_fixture")
        parameters = psycopg.conninfo.conninfo_to_dict(database_url)
        child_env = {"PATH": os.environ["PATH"], "PGHOST": parameters["host"],
                     "PGUSER": parameters["user"], "PGPASSWORD": parameters["password"]}
        subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
                        "--dbname=restore_fixture", str(dump)], env=child_env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60)
        parameters["dbname"] = "restore_fixture"
        with psycopg.connect(**parameters) as db:
            assert db.execute("SELECT count(*) FROM bot_users").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM downloads WHERE status = %s", ("success",)).fetchone()[0] == 1
        print("BACKUP_RESTORE_SHA256: OK (local fixture)")


if __name__ == "__main__":
    asyncio.run(main())
