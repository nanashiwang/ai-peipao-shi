import base64
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.db import Base
from app.main import (
    REAL_SEND_MIN_INTERVAL_SECONDS,
    SendResultIn,
    SendTaskIn,
    SendTaskUpdate,
    cancel_send_task,
    claim_tasks,
    create_send_task,
    list_audit_logs,
    record_send_result,
    resolve_send_screenshot,
    update_send_task,
    validate_send_task_execution_guard,
    validate_device_conversation_scope,
    validate_real_send_risk,
    validate_send_mode,
    validate_send_mode_submit,
    validate_send_task_content,
)
from app.models import AuditLog, Device, SendLog, SendTask


class SendTaskValidationTest(unittest.TestCase):
    def assert_invalid(self, content: str):
        with self.assertRaises(HTTPException):
            validate_send_task_content(content)

    def test_accepts_normal_chinese_content(self):
        content = "RPA\u6d4b\u8bd5\u6d88\u606f\uff0c\u8bf7\u5ffd\u7565\u3002"

        self.assertEqual(validate_send_task_content(content), content)

    def test_rejects_empty_content(self):
        self.assert_invalid("   ")

    def test_rejects_question_mark_mojibake(self):
        self.assert_invalid("RPA?????????????????????")

    def test_rejects_replacement_character(self):
        self.assert_invalid("RPA\u6d4b\u8bd5\ufffd\u6d88\u606f")

    def test_rejects_common_mojibake_tokens(self):
        self.assert_invalid("\u93b4\u621d\u6ed1\u6d93\u7487\u5cf0\u5bf0\u7039")

    def test_accepts_normal_question(self):
        content = "\u8bf7\u95ee\u4eca\u5929\u9700\u8981\u6253\u5361\u5417?"

        self.assertEqual(validate_send_task_content(content), content)

class SendModeValidationTest(unittest.TestCase):
    def test_defaults_to_dry_run(self):
        self.assertEqual(validate_send_mode(""), "dry_run")

    def test_accepts_supported_modes(self):
        self.assertEqual(validate_send_mode("dry_run"), "dry_run")
        self.assertEqual(validate_send_mode("real_send"), "real_send")

    def test_rejects_unknown_mode(self):
        with self.assertRaises(HTTPException):
            validate_send_mode("auto_send")

    def test_real_send_requires_explicit_confirmation(self):
        with self.assertRaises(HTTPException):
            validate_send_mode_submit("real_send", False)

    def test_real_send_accepts_explicit_confirmation(self):
        self.assertEqual(validate_send_mode_submit("real_send", True), "real_send")

    def test_existing_real_send_can_be_saved_without_reconfirm(self):
        self.assertEqual(validate_send_mode_submit("real_send", False, "real_send"), "real_send")


class SendTaskExecutionGuardTest(unittest.TestCase):
    def test_rejects_stale_pending_task(self):
        task = SimpleNamespace(
            send_mode="dry_run",
            scheduled_at=datetime(2026, 6, 1, 10, 0, 0),
            created_at=datetime(2026, 6, 1, 10, 0, 0),
        )

        with self.assertRaises(HTTPException):
            validate_send_task_execution_guard(task, now=datetime(2026, 6, 29, 10, 0, 0))

    def test_rejects_task_without_execution_time(self):
        task = SimpleNamespace(send_mode="dry_run", scheduled_at=None, created_at=None)

        with self.assertRaises(HTTPException):
            validate_send_task_execution_guard(task, now=datetime(2026, 6, 29, 10, 0, 0))

    def test_accepts_recent_task_and_returns_mode(self):
        task = SimpleNamespace(
            send_mode="real_send",
            scheduled_at=datetime(2026, 6, 28, 10, 0, 0),
            created_at=datetime(2026, 6, 20, 10, 0, 0),
        )

        self.assertEqual(validate_send_task_execution_guard(task, now=datetime(2026, 6, 29, 10, 0, 0)), "real_send")


class DeviceBindingValidationTest(unittest.TestCase):
    def test_rejects_missing_device(self):
        with self.assertRaises(HTTPException):
            validate_device_conversation_scope(None, "\u4e00\u5408\u5b66\u793e")

    def test_rejects_device_without_conversations(self):
        dev = SimpleNamespace(device_id="rpa-01", conversations="[]")

        with self.assertRaises(HTTPException):
            validate_device_conversation_scope(dev, "\u4e00\u5408\u5b66\u793e")

    def test_rejects_target_outside_device_scope(self):
        dev = SimpleNamespace(device_id="rpa-01", conversations='["\u4e00\u5408\u5b66\u793e"]')

        with self.assertRaises(HTTPException):
            validate_device_conversation_scope(dev, "\u6d4b\u8bd52\u7fa4")

    def test_accepts_target_inside_device_scope(self):
        dev = SimpleNamespace(device_id="rpa-01", conversations='["\u4e00\u5408\u5b66\u793e"]')

        self.assertIsNone(validate_device_conversation_scope(dev, "\u4e00\u5408\u5b66\u793e"))


class RealSendRiskValidationTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.now = datetime(2026, 6, 29, 10, 0, 0)

    def tearDown(self):
        self.db.close()

    def test_rejects_duplicate_active_real_send_task(self):
        self.db.add(
            SendTask(
                family_id="f1",
                target_name="\u4e00\u5408\u5b66\u793e",
                scene="test",
                content="\u6d4b\u8bd5\u5185\u5bb9",
                send_mode="real_send",
                status="pending",
            )
        )
        self.db.commit()

        with self.assertRaises(HTTPException):
            validate_real_send_risk(self.db, "\u4e00\u5408\u5b66\u793e", "\u6d4b\u8bd5\u5185\u5bb9", now=self.now)

    def test_rejects_recent_same_content_after_interval(self):
        task = SendTask(
            family_id="f1",
            target_name="\u4e00\u5408\u5b66\u793e",
            scene="sent",
            content="\u76f8\u540c\u5185\u5bb9",
            send_mode="real_send",
            status="sent",
        )
        self.db.add(task)
        self.db.flush()
        self.db.add(
            SendLog(
                task_id=task.id,
                family_id=task.family_id,
                target_name=task.target_name,
                status="sent",
                sent_at=self.now - timedelta(minutes=10),
            )
        )
        self.db.commit()

        with self.assertRaises(HTTPException):
            validate_real_send_risk(self.db, "\u4e00\u5408\u5b66\u793e", "\u76f8\u540c\u5185\u5bb9", now=self.now)

    def test_rejects_min_interval_even_for_different_content(self):
        self.db.add(
            SendLog(
                task_id=1,
                family_id="f1",
                target_name="\u4e00\u5408\u5b66\u793e",
                status="sent",
                sent_at=self.now - timedelta(seconds=REAL_SEND_MIN_INTERVAL_SECONDS - 1),
            )
        )
        self.db.commit()

        with self.assertRaises(HTTPException):
            validate_real_send_risk(self.db, "\u4e00\u5408\u5b66\u793e", "\u4e0d\u540c\u5185\u5bb9", now=self.now)

    def test_allows_real_send_when_no_duplicate_or_recent_send(self):
        self.assertIsNone(validate_real_send_risk(self.db, "\u4e00\u5408\u5b66\u793e", "\u65b0\u5185\u5bb9", now=self.now))


class SendTaskAuditLogTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def test_create_task_writes_audit_log(self):
        task = create_send_task(
            SendTaskIn(
                family_id="f1",
                target_name="\u4e00\u5408\u5b66\u793e",
                scene="test",
                content="\u5f85\u5ba1\u6838\u5185\u5bb9",
            ),
            db=self.db,
        )

        logs = self.db.query(AuditLog).filter(AuditLog.entity_id == task["id"]).all()

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].action, "create")
        self.assertIn("\u5f85\u5ba1\u6838\u5185\u5bb9", logs[0].after_json)

    def test_create_task_rejects_mojibake_content(self):
        with self.assertRaises(HTTPException):
            create_send_task(
                SendTaskIn(
                    family_id="f1",
                    target_name="\u4e00\u5408\u5b66\u793e",
                    scene="test",
                    content="RPA?????????????????????",
                ),
                db=self.db,
            )

        self.assertEqual(self.db.query(SendTask).count(), 0)
        self.assertEqual(self.db.query(AuditLog).count(), 0)

    def test_update_task_rejects_mojibake_content_without_mutating_task(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u539f\u59cb\u5185\u5bb9"),
            db=self.db,
        )

        with self.assertRaises(HTTPException):
            update_send_task(
                task["id"],
                SendTaskUpdate(content="RPA?????????????????????", status="pending"),
                db=self.db,
            )

        saved = self.db.get(SendTask, task["id"])
        self.assertEqual(saved.content, "\u539f\u59cb\u5185\u5bb9")
        self.assertEqual(saved.status, "pending")
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.entity_id == task["id"]).count(), 1)

    def test_confirm_real_send_is_audited(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u6d4b\u8bd5\u5185\u5bb9"),
            db=self.db,
        )

        update_send_task(
            task["id"],
            SendTaskUpdate(send_mode="real_send", confirm_real_send=True, content="\u6d4b\u8bd5\u5185\u5bb9", status="pending"),
            db=self.db,
        )

        actions = [log.action for log in self.db.query(AuditLog).filter(AuditLog.entity_id == task["id"]).order_by(AuditLog.id).all()]
        self.assertIn("confirm_real_send", actions)

    def test_cancel_task_is_audited_and_list_is_limited(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u6d4b\u8bd5\u5185\u5bb9"),
            db=self.db,
        )

        cancel_send_task(task["id"], db=self.db)
        logs = list_audit_logs(entity_type="send_task", entity_id=task["id"], limit=1, db=self.db)

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "cancel")


class ClaimTaskGuardTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.dev = Device(
            device_id="dev-a",
            token="token",
            conversations='["\u4e00\u5408\u5b66\u793e"]',
        )
        self.db.add(self.dev)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_claim_marks_stale_task_failed_and_only_returns_recent_task(self):
        stale_time = datetime.utcnow() - timedelta(days=8)
        old_task = SendTask(
            family_id="f-old",
            target_name="\u4e00\u5408\u5b66\u793e",
            scene="old",
            content="\u65e7\u4efb\u52a1",
            send_mode="dry_run",
            status="pending",
            scheduled_at=stale_time,
            created_at=stale_time,
        )
        recent_task = SendTask(
            family_id="f-new",
            target_name="\u4e00\u5408\u5b66\u793e",
            scene="new",
            content="\u65b0\u4efb\u52a1",
            send_mode="dry_run",
            status="pending",
        )
        self.db.add_all([old_task, recent_task])
        self.db.commit()

        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.db.refresh(old_task)
        self.db.refresh(recent_task)
        self.assertEqual([item["id"] for item in claimed], [recent_task.id])
        self.assertEqual(old_task.status, "failed")
        self.assertEqual(recent_task.status, "assigned")
        guard_log = self.db.query(SendLog).filter(SendLog.task_id == old_task.id).one()
        self.assertIn("SEND_GUARD", guard_log.detail)
        self.assertEqual(guard_log.send_mode, "dry_run")

    def test_claim_only_returns_tasks_in_device_conversation_scope(self):
        allowed_task = SendTask(
            family_id="f-allowed",
            target_name="一合学社",
            scene="allowed",
            content="允许领取",
            send_mode="dry_run",
            status="pending",
        )
        outside_task = SendTask(
            family_id="f-outside",
            target_name="测试2群",
            scene="outside",
            content="不应领取",
            send_mode="dry_run",
            status="pending",
        )
        self.db.add_all([allowed_task, outside_task])
        self.db.commit()

        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.db.refresh(allowed_task)
        self.db.refresh(outside_task)
        self.assertEqual([item["id"] for item in claimed], [allowed_task.id])
        self.assertEqual(allowed_task.status, "assigned")
        self.assertEqual(allowed_task.device_id, "dev-a")
        self.assertEqual(outside_task.status, "pending")
        self.assertEqual(outside_task.device_id, "")


class SendResultEvidenceTest(unittest.TestCase):
    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24

    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_screenshot_dir = main_module.SEND_SCREENSHOT_DIR
        main_module.SEND_SCREENSHOT_DIR = Path(self.tmp.name)

    def tearDown(self):
        main_module.SEND_SCREENSHOT_DIR = self.old_screenshot_dir
        self.db.close()
        self.tmp.cleanup()

    def add_task(self):
        task = SendTask(
            family_id="f1",
            target_name="\u4e00\u5408\u5b66\u793e",
            scene="test",
            content="\u6d4b\u8bd5\u5185\u5bb9",
            send_mode="real_send",
            status="assigned",
            device_id="rpa-01",
        )
        self.db.add(task)
        self.db.commit()
        return task

    def test_record_send_result_stores_server_screenshot(self):
        task = self.add_task()
        payload = SendResultIn(
            status="failed",
            detail="\u7a97\u53e3\u4e22\u5931",
            device_id="rpa-01",
            screenshot_base64=base64.b64encode(self.PNG_BYTES).decode("ascii"),
        )

        log = record_send_result(task.id, payload, db=self.db)

        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["device_id"], "rpa-01")
        self.assertEqual(log["send_mode"], "real_send")
        self.assertTrue(log["screenshot_path"].startswith("/api/send-artifacts/task_"))
        filename = log["screenshot_path"].rsplit("/", 1)[1]
        self.assertEqual(resolve_send_screenshot(filename).read_bytes(), self.PNG_BYTES)

    def test_record_send_result_keeps_dry_run_mode(self):
        task = self.add_task()
        task.send_mode = "dry_run"
        self.db.commit()

        log = record_send_result(task.id, SendResultIn(status="dry_run"), db=self.db)

        self.assertEqual(log["status"], "dry_run")
        self.assertEqual(log["send_mode"], "dry_run")

    def test_rejects_non_image_screenshot_payload(self):
        task = self.add_task()
        payload = SendResultIn(
            status="sent",
            screenshot_base64=base64.b64encode(b"not-image").decode("ascii"),
        )

        with self.assertRaises(HTTPException):
            record_send_result(task.id, payload, db=self.db)

    def test_rejects_artifact_path_traversal(self):
        with self.assertRaises(HTTPException):
            resolve_send_screenshot("../coach_mvp.db")


if __name__ == "__main__":
    unittest.main()
