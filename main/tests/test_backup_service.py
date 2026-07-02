import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.services.backup_service import (
    backup_path,
    create_backup,
    create_postgres_backup,
    create_sqlite_backup,
    list_backups,
    run_restore_drill,
    sqlite_path_from_url,
)


class BackupServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.db_path = self.base / "coach_mvp.db"
        self.backup_dir = self.base / "backups"

    def tearDown(self):
        self.tmp.cleanup()

    def create_db(self, tables=None):
        tables = tables or ["families", "raw_messages", "send_tasks", "send_logs"]
        with closing(sqlite3.connect(self.db_path)) as conn:
            for table in tables:
                conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO families (name) VALUES ('张妈妈')")
            conn.commit()

    def test_create_backup_list_and_restore_drill_pass(self):
        self.create_db()
        (self.backup_dir / "ignore.txt").parent.mkdir(parents=True, exist_ok=True)
        (self.backup_dir / "ignore.txt").write_text("not a backup", encoding="utf-8")

        backup = create_sqlite_backup(
            "sqlite:///coach_mvp.db",
            self.backup_dir,
            self.base,
            now=datetime(2026, 6, 30, 10, 0, 0),
        )
        backups = list_backups(self.backup_dir)
        drill = run_restore_drill(self.backup_dir / backup["filename"])

        self.assertEqual(backup["filename"], "coach_mvp_20260630_100000.sqlite3")
        self.assertEqual(backup["backup_type"], "sqlite")
        self.assertEqual(backup["sensitivity"], "raw_sensitive")
        self.assertTrue(backup["contains_sensitive_data"])
        self.assertEqual([item["filename"] for item in backups], [backup["filename"]])
        self.assertTrue(backups[0]["contains_sensitive_data"])
        self.assertTrue(drill["passed"])
        self.assertEqual(drill["backup_type"], "sqlite")
        self.assertEqual(drill["integrity"], "ok")
        self.assertEqual(drill["missing_tables"], [])

    def test_restore_drill_reports_missing_core_tables(self):
        self.create_db(tables=["families", "send_tasks"])
        backup = create_sqlite_backup(
            "sqlite:///coach_mvp.db",
            self.backup_dir,
            self.base,
            now=datetime(2026, 6, 30, 10, 1, 0),
        )

        drill = run_restore_drill(self.backup_dir / backup["filename"])

        self.assertFalse(drill["passed"])
        self.assertEqual(drill["missing_tables"], ["raw_messages", "send_logs"])

    def test_backup_path_rejects_traversal_and_unknown_names(self):
        with self.assertRaises(ValueError):
            backup_path(self.backup_dir, "../coach_mvp_20260630_100000.sqlite3")
        with self.assertRaises(ValueError):
            backup_path(self.backup_dir, "coach_mvp_latest.sqlite3")

    def test_sqlite_path_from_url_rejects_unsupported_databases(self):
        with self.assertRaises(ValueError):
            sqlite_path_from_url("sqlite:///:memory:", self.base)
        with self.assertRaises(ValueError):
            sqlite_path_from_url("postgresql+psycopg://coach:coach@postgres/db", self.base)

    def test_create_backup_dispatches_postgres_to_pg_dump(self):
        database_url = "postgresql+psycopg://coach:secret@postgres:5432/coach_mvp"

        def fake_run(cmd, check, env, stdout, stderr, text):
            self.assertTrue(check)
            self.assertTrue(text)
            self.assertIn("--host", cmd)
            self.assertIn("postgres", cmd)
            self.assertIn("--username", cmd)
            self.assertIn("coach", cmd)
            self.assertEqual(env["PGPASSWORD"], "secret")
            target = Path(cmd[cmd.index("--file") + 1])
            target.write_text(
                """
                CREATE TABLE public.families (id integer);
                CREATE TABLE public.raw_messages (id integer);
                CREATE TABLE public.send_tasks (id integer);
                CREATE TABLE public.send_logs (id integer);
                """,
                encoding="utf-8",
            )

        with patch("app.services.backup_service.shutil.which", return_value="/usr/bin/pg_dump"), patch(
            "app.services.backup_service.subprocess.run", side_effect=fake_run
        ):
            backup = create_backup(database_url, self.backup_dir, self.base, now=datetime(2026, 6, 30, 10, 2, 0))

        self.assertEqual(backup["filename"], "coach_mvp_20260630_100200.postgres.sql")
        self.assertEqual(backup["backup_type"], "postgresql")
        self.assertEqual([item["filename"] for item in list_backups(self.backup_dir)], [backup["filename"]])

        drill = run_restore_drill(self.backup_dir / backup["filename"])
        self.assertTrue(drill["passed"])
        self.assertEqual(drill["backup_type"], "postgresql")
        self.assertEqual(drill["missing_tables"], [])

    def test_postgres_backup_requires_pg_dump(self):
        with patch("app.services.backup_service.shutil.which", return_value=None):
            with self.assertRaises(FileNotFoundError):
                create_postgres_backup(
                    "postgresql+psycopg://coach:secret@postgres:5432/coach_mvp",
                    self.backup_dir,
                    now=datetime(2026, 6, 30, 10, 3, 0),
                )


if __name__ == "__main__":
    unittest.main()
