import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import SendResultIn, build_ops_health_dashboard, claim_tasks, list_send_tasks, record_send_result, retry_failed_task
from app.models import AuditLog, Device, SendLog, SendTask


class SendTaskRetryTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.dev = Device(device_id="dev-a", token="token", conversations='["一合学社"]')
        self.dev_b = Device(device_id="dev-b", token="token-b", conversations='["一合学社"]')
        self.db.add_all([self.dev, self.dev_b])
        self.db.commit()
        self.dev_request = SimpleNamespace(
            headers={"x-device-id": self.dev.device_id, "x-device-token": self.dev.token},
            state=SimpleNamespace(),
        )

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

        log = record_send_result(task.id, SendResultIn(status="failed", detail="窗口临时丢失", device_id="dev-a"), request=self.dev_request, db=self.db)
        self.db.refresh(task)

        self.assertEqual(log["status"], "failed")
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.device_id, "dev-a")
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

    def test_retryable_failure_stays_bound_to_failed_device(self):
        task = self.add_task(send_mode="dry_run")

        record_send_result(task.id, SendResultIn(status="failed", detail="INPUT_FOCUS: 输入框定位失败：窗口不可点", device_id="dev-a"), request=self.dev_request, db=self.db)
        task.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
        task.scheduled_at = task.next_retry_at
        self.db.commit()

        self.assertEqual(claim_tasks("dev-b", limit=5, dev=self.dev_b, db=self.db), [])
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])
        self.db.refresh(task)
        self.assertEqual(task.device_id, "dev-a")

    def test_stale_assigned_task_requeues_only_for_same_device(self):
        task = self.add_task(send_mode="dry_run")
        task.scheduled_at = datetime.utcnow() - timedelta(minutes=10)
        self.db.commit()

        self.assertEqual(claim_tasks("dev-b", limit=5, dev=self.dev_b, db=self.db), [])
        self.db.refresh(task)
        self.assertEqual(task.status, "assigned")
        self.assertEqual(task.device_id, "dev-a")

        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])
        self.db.refresh(task)
        self.assertEqual(task.status, "assigned")
        self.assertEqual(task.device_id, "dev-a")
        self.assertIn("same_device_requeue", {item.action for item in self.db.query(AuditLog).all()})

    def test_task_list_moves_stale_real_send_assigned_to_manual_review(self):
        task = self.add_task(send_mode="real_send")
        task.scheduled_at = datetime.utcnow() - timedelta(minutes=10)
        self.db.commit()

        rows = list_send_tasks(db=self.db)

        self.db.refresh(task)
        self.assertEqual([row["id"] for row in rows], [task.id])
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.device_id, "dev-a")
        self.assertIsNone(task.next_retry_at)
        self.assertIn("发送结果不确定", task.last_error)
        self.assertIn("real_send_stale_review", {item.action for item in self.db.query(AuditLog).all()})

    def test_claim_does_not_auto_requeue_stale_real_send_to_avoid_duplicate_send(self):
        task = self.add_task(send_mode="real_send")
        task.scheduled_at = datetime.utcnow() - timedelta(minutes=10)
        self.db.commit()

        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.db.refresh(task)
        self.assertEqual(claimed, [])
        self.assertEqual(task.status, "failed")
        self.assertIsNone(task.next_retry_at)
        self.assertIn("real_send_stale_review", {item.action for item in self.db.query(AuditLog).all()})

    def test_real_send_failure_goes_to_manual_alert_without_auto_retry(self):
        task = self.add_task(send_mode="real_send")

        record_send_result(task.id, SendResultIn(status="failed", detail="发送结果未知", device_id="dev-a"), request=self.dev_request, db=self.db)
        self.db.refresh(task)
        health = build_ops_health_dashboard(self.db)
        retry_component = next(item for item in health["components"] if item["label"] == "失败重试与告警")

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.retry_count, 0)
        self.assertIsNone(task.next_retry_at)
        self.assertEqual(retry_component["status"], "critical")
        self.assertEqual(retry_component["metrics"]["retry_alert"], 1)

    def test_real_send_presend_failure_auto_retries(self):
        task = self.add_task(send_mode="real_send")

        record_send_result(task.id, SendResultIn(status="failed", detail="INPUT_FOCUS: 输入框定位失败：窗口不可点", device_id="dev-a"), request=self.dev_request, db=self.db)
        self.db.refresh(task)

        self.assertEqual(task.status, "pending")
        self.assertEqual(task.device_id, "dev-a")
        self.assertEqual(task.retry_count, 1)
        self.assertIsNotNone(task.next_retry_at)
        self.assertIn("auto_retry", {item.action for item in self.db.query(AuditLog).all()})

    def test_real_send_baseline_read_failure_auto_retries_before_hotkey(self):
        task = self.add_task(send_mode="real_send")

        record_send_result(
            task.id,
            SendResultIn(
                status="failed",
                detail="BASELINE_READ_FAILED: baseline read failed before hotkey",
                device_id="dev-a",
            ),
            request=self.dev_request,
            db=self.db,
        )
        self.db.refresh(task)

        self.assertEqual(task.status, "pending")
        self.assertEqual(task.device_id, "dev-a")
        self.assertEqual(task.retry_count, 1)
        self.assertIsNotNone(task.next_retry_at)
        self.assertIn("auto_retry", {item.action for item in self.db.query(AuditLog).all()})

    def test_real_send_after_hotkey_failure_requires_manual_review(self):
        task = self.add_task(send_mode="real_send")

        record_send_result(task.id, SendResultIn(status="failed", detail="RPA_TRACE: 真实发送热键已触发", device_id="dev-a"), request=self.dev_request, db=self.db)
        self.db.refresh(task)

        self.assertEqual(task.status, "failed")
        self.assertEqual(task.retry_count, 0)
        self.assertIsNone(task.next_retry_at)

    def test_real_send_guard_skip_keeps_task_pending(self):
        task = self.add_task(send_mode="real_send")

        record_send_result(task.id, SendResultIn(status="skipped", detail="REAL_SEND_GUARD: 控制端未开启该设备真实发送开关", device_id="dev-a"), request=self.dev_request, db=self.db)
        self.db.refresh(task)

        self.assertEqual(task.status, "pending")
        self.assertEqual(task.device_id, "dev-a")
        self.assertIsNotNone(task.next_retry_at)
        self.assertIn("policy_wait", {item.action for item in self.db.query(AuditLog).all()})

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
