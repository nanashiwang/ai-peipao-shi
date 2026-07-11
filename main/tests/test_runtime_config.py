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
                "postgresql+psycopg://coach:strong-db-password-123@postgres:5432/coach_mvp",
                ark,
                database_url_explicit=True,
                env={"ADMIN_AUTH_SECRET": "0123456789abcdef0123456789abcdef"},
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["metrics"]["database_kind"], "postgresql")
        self.assertNotIn("strong-db-password-123", report["metrics"]["database_url_masked"])
        self.assertTrue(report["metrics"]["ark_configured"])

    def test_deployed_env_rejects_sqlite_and_weak_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = runtime_config_report(
                "pilot",
                "sqlite:///./coach_mvp.db",
                Path(tmp) / "ark.json",
                database_url_explicit=True,
                env={"ADMIN_AUTH_REQUIRED": "false", "ADMIN_AUTH_SECRET": "change-me-before-production"},
            )

        self.assertEqual(report["status"], "critical")
        self.assertIn("部署环境禁止设置 ADMIN_AUTH_REQUIRED=false", report["detail"])
        self.assertIn("ADMIN_AUTH_SECRET", report["detail"])
        self.assertIn("部署环境禁止使用 SQLite", report["detail"])

    def test_ark_environment_variables_take_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = runtime_config_report(
                "production",
                "postgresql+psycopg://coach:strong-db-password-123@postgres:5432/coach_mvp",
                Path(tmp) / "ark.json",
                database_url_explicit=True,
                env={
                    "ADMIN_AUTH_SECRET": "0123456789abcdef0123456789abcdef",
                    "ARK_API_KEY": "sk-env-valid-key",
                    "ARK_ENDPOINT_ID": "qwen-vl-plus",
                },
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["metrics"]["ark_source"], "env")
        self.assertTrue(report["metrics"]["ark_configured"])

    def test_enabled_wecom_kf_requires_complete_callback_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = runtime_config_report(
                "local",
                "sqlite:///./coach_mvp.db",
                Path(tmp) / "ark.json",
                database_url_explicit=False,
                env={"WECOM_KF_ENABLED": "true", "WECOM_KF_CORP_ID": "corp"},
            )

        self.assertEqual(report["status"], "critical")
        self.assertIn("WECOM_KF_SECRET", report["detail"])

    def test_enabled_wecom_kf_accepts_complete_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = runtime_config_report(
                "local",
                "sqlite:///./coach_mvp.db",
                Path(tmp) / "ark.json",
                database_url_explicit=False,
                env={
                    "WECOM_KF_ENABLED": "true",
                    "WECOM_KF_CORP_ID": "corp",
                    "WECOM_KF_SECRET": "secret-value",
                    "WECOM_KF_TOKEN": "callback-token",
                    "WECOM_KF_ENCODING_AES_KEY": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
                },
            )

        self.assertEqual(report["status"], "ok")


if __name__ == "__main__":
    unittest.main()
