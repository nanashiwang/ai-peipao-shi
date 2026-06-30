import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import SendResultIn, build_ops_health_dashboard, claim_tasks, record_send_result, retry_failed_task
from app.models import AuditLog, Device, SendLog, SendTask


class SendTaskRetryTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.dev = Device(device_id="dev-a", token="token", conversations='["一合学社"]')
        self.db.add(self.dev)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def add_task(self, send_mode: str = "dry_run", retry_count: int = 0, max_retries: int = 2):
        task = SendTask(
            family_id="f1",
            target_name="一合学社",
            scene="测试",
            content="这是一条测试内容",
            send_mode=send_mode,
            status="assigned",
            device_id="dev-a",
            retry_count=retry_count,
            max_retries=max_retries,
        )
        self.db.add(task)
        self.db.commit()
        return task

    def test_failed_dry_run_auto_retries_and_waits_until_due(self):
        task = self.add_task(send_mode="dry_run")

        log = record_send_result(task.id, SendResultIn(status="failed", detail="窗口临时丢失", device_id="dev-a"), db=self.db)
        self.db.refresh(task)

        self.assertEqual(log["status"], "failed")
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.retry_count, 1)
        self.assertIsNotNone(task.next_retry_at)
        self.assertEqual(self.db.query(SendLog).count(), 1)
        self.assertIn("auto_retry", {item.action for item in self.db.query(AuditLog).all()})
        self.assertEqual(claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db), [])

        task.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
        task.scheduled_at = task.next_retry_at
        self.db.commit()
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])

    def test_real_send_failure_goes_to_manual_alert_without_auto_retry(self):
        task = self.add_task(send_mode="real_send")

        record_send_result(task.id, SendResultIn(status="failed", detail="发送结果未知", device_id="dev-a"), db=self.db)
        self.db.refresh(task)
        health = build_ops_health_dashboard(self.db)
        retry_component = next(item for item in health["components"] if item["label"] == "失败重试与告警")

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.retry_count, 0)
        self.assertIsNone(task.next_retry_at)
        self.assertEqual(retry_component["status"], "critical")
        self.assertEqual(retry_component["metrics"]["retry_alert"], 1)

    def test_manual_retry_requeues_failed_task_after_review(self):
        task = self.add_task(send_mode="dry_run", retry_count=2, max_retries=2)
        task.status = "failed"
        task.last_error = "超过重试上限"
        self.db.commit()

        row = retry_failed_task(task.id, db=self.db)
        self.db.refresh(task)

        self.assertEqual(row["status"], "pending")
        self.assertEqual(task.status, "pending")
        self.assertIsNotNone(task.next_retry_at)
        self.assertIn("manual_retry", {item.action for item in self.db.query(AuditLog).all()})


if __name__ == "__main__":
    unittest.main()
