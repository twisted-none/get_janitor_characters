from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.db_backup import create_backup


class BackupTests(unittest.TestCase):
    def test_secret_stays_out_of_command_line_and_backup_is_checked(self):
        commands = []
        def run(command, **kwargs):
            commands.append((command, kwargs))
            if command[0] == "pg_dump":
                kwargs["stdout"].write(b"PGDMP-fixture")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backup.dump"
            with patch("tools.db_backup.subprocess.run", side_effect=run):
                create_backup(output, {"DATABASE_URL": "postgresql://fixture:fake-secret@localhost/test"})
            self.assertEqual(output.read_bytes(), b"PGDMP-fixture")
            self.assertTrue(output.with_suffix(".dump.sha256").is_file())
        self.assertEqual([item[0][0] for item in commands], ["pg_dump", "pg_restore"])
        self.assertNotIn("fake-secret", repr([item[0] for item in commands]))
        self.assertEqual(commands[0][1]["env"]["PGPASSWORD"], "fake-secret")

    def test_existing_backup_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backup.dump"
            output.write_bytes(b"original")
            with self.assertRaises(FileExistsError):
                create_backup(output, {"DATABASE_URL": "postgresql://fixture@localhost/test"})
            self.assertEqual(output.read_bytes(), b"original")

    def test_failed_dump_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "backup.dump"
            with patch("tools.db_backup.subprocess.run", side_effect=RuntimeError("fixture")):
                with self.assertRaises(RuntimeError):
                    create_backup(output, {"DATABASE_URL": "postgresql://fixture@localhost/test"})
            self.assertFalse(output.exists())
