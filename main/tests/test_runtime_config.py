import tempfile
import unittest
from pathlib import Path

from app.services.runtime_config import assert_runtime_config_safe, runtime_config_report


class RuntimeConfigTest(unittest.TestCase):
    def test_local_sqlite_is_allowed_for_development(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = runtime_config_report(
                "local",
                "sqlite:///./coach_mvp.db",
                Path(tmp) / "ark.json",
                database_url_explicit=False,
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["metrics"]["database_kind"], "sqlite")

    def test_production_rejects_implicit_sqlite_and_missing_ark(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = runtime_config_report(
                "production",
                "sqlite:///./coach_mvp.db",
                Path(tmp) / "ark.json",
                database_url_explicit=False,
            )

        self.assertEqual(report["status"], "critical")
        self.assertIn("正式环境必须显式设置 DATABASE_URL", report["detail"])
        self.assertIn("正式环境禁止使用 SQLite", report["detail"])
        with self.assertRaises(RuntimeError):
            assert_runtime_config_safe(report)

    def test_production_accepts_postgres_and_separate_ark_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            ark = Path(tmp) / "ark.json"
            ark.write_text('{"api_key":"sk-production-valid-key","endpoint_id":"qwen-vl-plus"}', encoding="utf-8")
            report = runtime_config_report(
                "prod",
                "postgresql+psycopg://coach:secret@postgres:5432/coach_mvp",
                ark,
                database_url_explicit=True,
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["metrics"]["database_kind"], "postgresql")
        self.assertNotIn("secret", report["metrics"]["database_url_masked"])
        self.assertTrue(report["metrics"]["ark_configured"])

    def test_pilot_sqlite_is_warned_before_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = runtime_config_report(
                "pilot",
                "sqlite:///./coach_mvp.db",
                Path(tmp) / "ark.json",
                database_url_explicit=True,
            )

        self.assertEqual(report["status"], "warn")
        self.assertIn("扩容前应切换 PostgreSQL", report["detail"])


if __name__ == "__main__":
    unittest.main()
