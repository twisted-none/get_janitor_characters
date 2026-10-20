"""Create and verify a local PostgreSQL custom-format dump without exposing a DSN."""

import argparse
import hashlib
import os
from pathlib import Path
import subprocess

from psycopg.conninfo import conninfo_to_dict


def create_backup(output: Path, environment):
    parameters = conninfo_to_dict(environment.get("DATABASE_URL", ""))
    if not parameters.get("dbname"):
        raise ValueError("DATABASE_URL is required")
    child_environment = {key: value for key, value in environment.items()
                         if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP"}}
    mapping = {"dbname": "PGDATABASE", "user": "PGUSER", "password": "PGPASSWORD", "host": "PGHOST",
               "hostaddr": "PGHOSTADDR", "port": "PGPORT", "sslmode": "PGSSLMODE",
               "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY",
               "options": "PGOPTIONS", "connect_timeout": "PGCONNECT_TIMEOUT",
               "channel_binding": "PGCHANNELBINDING"}
    if set(parameters) - mapping.keys():
        raise ValueError("Unsupported database connection setting")
    child_environment.update({mapping[key]: value for key, value in parameters.items()})
    child_environment.setdefault("PGCONNECT_TIMEOUT", "10")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation is also the ownership evidence used for failure cleanup.
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            subprocess.run(["pg_dump", "--format=custom", "--no-owner", "--no-acl"],
                           env=child_environment, stdout=handle, stderr=subprocess.DEVNULL,
                           check=True, timeout=600)
        subprocess.run(["pg_restore", "--list", str(output)], env=child_environment,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60)
        with output.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        with output.with_suffix(output.suffix + ".sha256").open("x", encoding="ascii") as handle:
            handle.write(digest + "\n")
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def main():
    arguments = argparse.ArgumentParser(description=__doc__)
    arguments.add_argument("output", type=Path)
    args = arguments.parse_args()
    try:
        create_backup(args.output, os.environ)
    except Exception:
        print("Backup failed; check settings, client/server versions and available disk space. No credentials printed.")
        return 1
    print("Backup created; archive listing and SHA256 check completed. Test restoration separately.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
