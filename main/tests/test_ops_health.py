import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.db import Base
from app.main import CLAIM_TIMEOUT_SECONDS, build_ops_health_dashboard
from app.models import Device, SendLog, SendTask


class OpsHealthDashboardTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.now = datetime(2026, 6, 30, 10, 0, 0)
        self.tmp = tempfile.TemporaryDirectory()
        self.old_screenshot_dir = main_module.SEND_SCREENSHOT_DIR
        self.old_ark_path = main_module.ARK_CONFIG_PATH
        self.old_backup_dir = main_module.BACKUP_DIR
        main_module.SEND_SCREENSHOT_DIR = Path(self.tmp.name) / "shots"
        main_module.ARK_CONFIG_PATH = Path(self.tmp.name) / "ark.json"
        main_module.BACKUP_DIR = Path(self.tmp.name) / "backups"

    def tearDown(self):
        main_module.SEND_SCREENSHOT_DIR = self.old_screenshot_dir
        main_module.ARK_CONFIG_PATH = self.old_ark_path
        main_module.BACKUP_DIR = self.old_backup_dir
        self.db.close()
        self.tmp.cleanup()

    def component(self, dashboard, label):
        return next(item for item in dashboard["components"] if item["label"] == label)

    def test_health_reports_warning_without_devices_or_ark(self):
        dashboard = build_ops_health_dashboard(self.db, now=self.now)

        self.assertEqual(dashboard["overall_status"], "warn")
        self.assertEqual(self.component(dashboard, "运行环境配置")["status"], "ok")
        self.assertEqual(self.component(dashboard, "管理端鉴权")["status"], "ok")
        self.assertEqual(self.component(dashboard, "任务领取锁")["status"], "ok")
        self.assertEqual(self.component(dashboard, "被控端设备")["status"], "warn")
        self.assertEqual(self.component(dashboard, "云端视觉定位")["status"], "warn")
        self.assertEqual(self.component(dashboard, "数据备份")["status"], "warn")
        self.assertEqual(self.component(dashboard, "日志保留策略")["status"], "ok")

    def test_health_reports_devices_queue_failures_and_artifacts(self):
        self.db.add_all([
            Device(device_id="dev-online", token="t1", wecom_ok="Y", last_heartbeat=self.now - timedelta(seconds=10)),
            Device(device_id="dev-offline", token="t2", wecom_ok="N", last_heartbeat=self.now - timedelta(minutes=10)),
            SendTask(
                family_id="f1",
                target_name="张妈妈",
                scene="回复",
                content="待发送",
                status="assigned",
                scheduled_at=self.now - timedelta(seconds=CLAIM_TIMEOUT_SECONDS + 1),
            ),
            SendLog(task_id=1, family_id="f1", target_name="张妈妈", status="failed", sent_at=self.now - timedelta(hours=1)),
        ])
        main_module.SEND_SCREENSHOT_DIR.mkdir(parents=True)
        (main_module.SEND_SCREENSHOT_DIR / "task_1_20260630_100000_000000.png").write_bytes(b"1234")
        self.db.commit()

        dashboard = build_ops_health_dashboard(self.db, now=self.now)

        self.assertEqual(dashboard["overall_status"], "critical")
        self.assertEqual(self.component(dashboard, "被控端设备")["metrics"]["online"], 1)
        self.assertEqual(self.component(dashboard, "发送队列")["status"], "critical")
        self.assertEqual(self.component(dashboard, "发送队列")["metrics"]["stale_assigned"], 1)
        self.assertEqual(self.component(dashboard, "近24小时发送失败")["status"], "warn")
        self.assertEqual(self.component(dashboard, "截图证据目录")["metrics"]["file_count"], 1)
        self.assertEqual(self.component(dashboard, "截图证据目录")["metrics"]["total_bytes"], 4)

    def test_health_ok_when_core_dependencies_are_ready(self):
        self.db.add(Device(device_id="dev-online", token="t1", wecom_ok="Y", last_heartbeat=self.now))
        main_module.ARK_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        main_module.ARK_CONFIG_PATH.write_text('{"api_key":"sk-test123456","endpoint_id":"qwen-vl-plus"}', encoding="utf-8")
        main_module.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (main_module.BACKUP_DIR / "coach_mvp_20260630_100000.sqlite3").write_bytes(b"backup")
        self.db.commit()

        dashboard = build_ops_health_dashboard(self.db, now=self.now)

        self.assertEqual(dashboard["overall_status"], "ok")
        self.assertTrue(self.component(dashboard, "云端视觉定位")["metrics"]["configured"])
        self.assertEqual(self.component(dashboard, "数据备份")["metrics"]["file_count"], 1)


if __name__ == "__main__":
    unittest.main()
