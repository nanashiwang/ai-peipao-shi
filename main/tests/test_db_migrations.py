import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

from app.db import migrations_enabled, run_schema_migrations


BASELINE_REVISION = "20260703_0001"


class DbMigrationsTest(unittest.TestCase):
    def test_migrations_disabled_for_local_by_default(self):
        with patch.dict(os.environ, {"APP_ENV": "local"}, clear=True):
            self.assertFalse(migrations_enabled())

    def test_migrations_enabled_for_deployed_env_by_default(self):
        with patch.dict(os.environ, {"APP_ENV": "pilot"}, clear=True):
            self.assertTrue(migrations_enabled())

    def test_migrations_env_flag_overrides_deployed_default(self):
        with patch.dict(os.environ, {"APP_ENV": "pilot", "DB_MIGRATIONS_ENABLED": "false"}, clear=True):
            self.assertFalse(migrations_enabled())
        with patch.dict(os.environ, {"APP_ENV": "local", "DB_MIGRATIONS_ENABLED": "true"}, clear=True):
            self.assertTrue(migrations_enabled())

    def test_baseline_migration_creates_schema_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "coach_mvp.db"
            database_url = f"sqlite:///{db_path.as_posix()}"

            run_schema_migrations(database_url)
            run_schema_migrations(database_url)

            engine = create_engine(database_url, future=True)
            try:
                inspector = inspect(engine)
                tables = set(inspector.get_table_names())

                self.assertIn("families", tables)
                self.assertIn("raw_messages", tables)
                self.assertIn("send_logs", tables)
                self.assertIn("alembic_version", tables)

                family_columns = {column["name"] for column in inspector.get_columns("families")}
                device_columns = {column["name"] for column in inspector.get_columns("devices")}
                self.assertIn("campus_name", family_columns)
                self.assertIn("allow_real_send", device_columns)

                with engine.connect() as conn:
                    version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                self.assertEqual(version, BASELINE_REVISION)
            finally:
                engine.dispose()

    def test_baseline_migration_patches_existing_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            database_url = f"sqlite:///{db_path.as_posix()}"
            engine = create_engine(database_url, future=True)

            try:
                with engine.begin() as conn:
                    conn.execute(text("CREATE TABLE families (id INTEGER PRIMARY KEY, family_id VARCHAR(64))"))
                    conn.execute(
                        text(
                            "CREATE TABLE send_logs ("
                            "id INTEGER PRIMARY KEY, task_id INTEGER, family_id VARCHAR(64), "
                            "target_name VARCHAR(120), status VARCHAR(30), detail TEXT, sent_at DATETIME)"
                        )
                    )

                run_schema_migrations(database_url)

                inspector = inspect(engine)
                family_columns = {column["name"] for column in inspector.get_columns("families")}
                send_log_columns = {column["name"] for column in inspector.get_columns("send_logs")}

                self.assertIn("course_stage", family_columns)
                self.assertIn("campus_name", family_columns)
                self.assertIn("screenshot_path", send_log_columns)
                self.assertIn("verify_status", send_log_columns)
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
