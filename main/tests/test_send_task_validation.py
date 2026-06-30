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
    DeviceUpdateIn,
    DeviceConversationBatchCheckRequestIn,
    DeviceConversationCheckRequestIn,
    HeartbeatIn,
    RpaConversationIn,
    RpaMessageIn,
    SendResultIn,
    SendTaskIn,
    SendTaskPreflightIn,
    SendTaskRealSendIn,
    SendTaskUpdate,
    actor_from_request,
    build_send_task_preflight,
    cancel_send_task,
    claim_tasks,
    create_send_task,
    device_heartbeat,
    list_audit_logs,
    list_send_tasks,
    queue_task_dry_run,
    queue_device_conversation_checks_batch,
    queue_device_conversation_check,
    queue_task_real_send,
    record_send_result,
    resolve_send_screenshot,
    sync_rpa_conversation,
    update_device,
    update_send_task,
    validate_send_task_execution_guard,
    validate_device_conversation_scope,
    validate_real_send_risk,
    validate_send_mode,
    validate_send_mode_submit,
    validate_send_task_content,
)
from app.models import AuditLog, Device, DeviceConversationCheck, Family, SendLog, SendTask
from app.services.admin_auth import admin_auth_secret, sign_admin_token


def admin_request(role: str = "admin"):
    token = sign_admin_token(role, role, role, admin_auth_secret())
    return SimpleNamespace(headers={"authorization": f"Bearer {token}"}, state=SimpleNamespace())


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

    def test_actor_header_accepts_url_encoded_chinese(self):
        request = SimpleNamespace(headers={"x-actor": "%E6%8E%A7%E5%88%B6%E7%AB%AF"}, state=SimpleNamespace())

        self.assertEqual(actor_from_request(request), "\u63a7\u5236\u7aef")

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
        self.db.add_all([
            Family(family_id="f1", parent_nickname="一合学社", coach_name="coach"),
            Family(family_id="f2", parent_nickname="一合学社", coach_name="coach"),
            Device(device_id="rpa-01", token="token", conversations='["一合学社"]', allow_real_send=True, wecom_ok="Y", last_heartbeat=datetime.utcnow()),
            DeviceConversationCheck(
                device_id="rpa-01",
                target_name="一合学社",
                status="ok",
                message_count=1,
                source="企业微信RPA-视觉回读",
                verified_at=datetime.utcnow(),
            ),
        ])
        self.db.commit()
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
        self.db.add_all([
            Family(family_id="f1", parent_nickname="一合学社", coach_name="coach"),
            Family(family_id="f2", parent_nickname="一合学社", coach_name="coach"),
            Device(device_id="rpa-01", token="token", conversations='["一合学社"]', allow_real_send=True, wecom_ok="Y", last_heartbeat=datetime.utcnow()),
            DeviceConversationCheck(
                device_id="rpa-01",
                target_name="一合学社",
                status="ok",
                message_count=1,
                source="企业微信RPA-视觉回读",
                verified_at=datetime.utcnow(),
            ),
        ])
        self.db.commit()

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

    def test_create_real_send_rejects_duplicate_active_task(self):
        create_send_task(
            SendTaskIn(
                family_id="f1",
                target_name="\u4e00\u5408\u5b66\u793e",
                scene="test",
                content="\u91cd\u590d\u5185\u5bb9",
                device_id="rpa-01",
                send_mode="real_send",
                confirm_real_send=True,
            ),
            db=self.db,
        )

        with self.assertRaises(HTTPException):
            create_send_task(
                SendTaskIn(
                    family_id="f2",
                    target_name="\u4e00\u5408\u5b66\u793e",
                    scene="test",
                    content="\u91cd\u590d\u5185\u5bb9",
                    device_id="rpa-01",
                    send_mode="real_send",
                    confirm_real_send=True,
                ),
                db=self.db,
            )

        self.assertEqual(self.db.query(SendTask).count(), 1)
        self.assertEqual(self.db.query(AuditLog).count(), 1)

    def test_coach_role_cannot_create_real_send_task(self):
        with self.assertRaises(HTTPException) as ctx:
            create_send_task(
                SendTaskIn(
                    family_id="f1",
                    target_name="\u4e00\u5408\u5b66\u793e",
                    scene="test",
                    content="\u771f\u5b9e\u53d1\u9001\u5185\u5bb9",
                    device_id="rpa-01",
                    send_mode="real_send",
                    confirm_real_send=True,
                ),
                request=admin_request("coach"),
                db=self.db,
            )

        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self.db.query(SendTask).count(), 0)

    def test_coach_role_cannot_bind_device_when_creating_task(self):
        with self.assertRaises(HTTPException) as ctx:
            create_send_task(
                SendTaskIn(
                    family_id="f1",
                    target_name="\u4e00\u5408\u5b66\u793e",
                    scene="test",
                    content="\u5f85\u5ba1\u6838\u5185\u5bb9",
                    device_id="rpa-01",
                ),
                request=admin_request("coach"),
                db=self.db,
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_create_real_send_rejects_recent_sent_same_content(self):
        sent_task = SendTask(
            family_id="f1",
            target_name="\u4e00\u5408\u5b66\u793e",
            scene="sent",
            content="\u8fd1\u671f\u5df2\u53d1\u5185\u5bb9",
            send_mode="real_send",
            status="sent",
        )
        self.db.add(sent_task)
        self.db.flush()
        self.db.add(
            SendLog(
                task_id=sent_task.id,
                family_id=sent_task.family_id,
                target_name=sent_task.target_name,
                status="sent",
                sent_at=datetime.utcnow() - timedelta(minutes=10),
            )
        )
        self.db.commit()

        with self.assertRaises(HTTPException):
            create_send_task(
                SendTaskIn(
                    family_id="f2",
                    target_name="\u4e00\u5408\u5b66\u793e",
                    scene="test",
                    content="\u8fd1\u671f\u5df2\u53d1\u5185\u5bb9",
                    device_id="rpa-01",
                    send_mode="real_send",
                    confirm_real_send=True,
                ),
                db=self.db,
            )

        self.assertEqual(self.db.query(SendTask).count(), 1)

    def test_update_real_send_rejects_duplicate_without_mutating_task(self):
        create_send_task(
            SendTaskIn(
                family_id="f1",
                target_name="\u4e00\u5408\u5b66\u793e",
                scene="test",
                content="\u5df2\u6392\u961f\u5185\u5bb9",
                device_id="rpa-01",
                send_mode="real_send",
                confirm_real_send=True,
            ),
            db=self.db,
        )
        candidate = create_send_task(
            SendTaskIn(
                family_id="f2",
                target_name="\u4e00\u5408\u5b66\u793e",
                scene="test",
                content="\u5019\u9009\u5185\u5bb9",
            ),
            db=self.db,
        )

        with self.assertRaises(HTTPException):
            update_send_task(
                candidate["id"],
                SendTaskUpdate(
                    content="\u5df2\u6392\u961f\u5185\u5bb9",
                    device_id="rpa-01",
                    send_mode="real_send",
                    confirm_real_send=True,
                    status="pending",
                ),
                db=self.db,
            )

        saved = self.db.get(SendTask, candidate["id"])
        self.assertEqual(saved.content, "\u5019\u9009\u5185\u5bb9")
        self.assertEqual(saved.send_mode, "dry_run")
        self.assertEqual(saved.status, "pending")
        self.assertEqual(self.db.query(AuditLog).filter(AuditLog.entity_id == candidate["id"]).count(), 1)

    def test_coach_role_cannot_confirm_existing_real_send_task(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u5f85\u786e\u8ba4\u5185\u5bb9"),
            db=self.db,
        )

        with self.assertRaises(HTTPException) as ctx:
            update_send_task(
                task["id"],
                SendTaskUpdate(send_mode="real_send", confirm_real_send=True, content="\u5f85\u786e\u8ba4\u5185\u5bb9", status="pending", device_id="rpa-01"),
                request=admin_request("coach"),
                db=self.db,
            )

        saved = self.db.get(SendTask, task["id"])
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(saved.send_mode, "dry_run")

    def test_confirm_real_send_is_audited(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u6d4b\u8bd5\u5185\u5bb9"),
            db=self.db,
        )

        update_send_task(
            task["id"],
            SendTaskUpdate(send_mode="real_send", confirm_real_send=True, content="\u6d4b\u8bd5\u5185\u5bb9", status="pending", device_id="rpa-01"),
            db=self.db,
        )

        actions = [log.action for log in self.db.query(AuditLog).filter(AuditLog.entity_id == task["id"]).order_by(AuditLog.id).all()]
        self.assertIn("confirm_real_send", actions)

    def test_update_real_send_from_dry_run_requeues_pending(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u8bd5\u8fd0\u884c\u540e\u4fdd\u5b58\u771f\u53d1"),
            db=self.db,
        )
        saved = self.db.get(SendTask, task["id"])
        saved.status = "dry_run"
        self.db.commit()

        result = update_send_task(
            task["id"],
            SendTaskUpdate(send_mode="real_send", confirm_real_send=True, content="\u8bd5\u8fd0\u884c\u540e\u4fdd\u5b58\u771f\u53d1", status="dry_run", device_id="rpa-01"),
            request=admin_request("admin"),
            db=self.db,
        )

        self.assertEqual(result["send_mode"], "real_send")
        self.assertEqual(result["status"], "pending")

    def test_queue_real_send_after_dry_run_completion(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u8bd5\u8fd0\u884c\u540e\u771f\u53d1"),
            db=self.db,
        )
        saved = self.db.get(SendTask, task["id"])
        saved.status = "dry_run"
        self.db.commit()

        result = queue_task_real_send(
            task["id"],
            SendTaskRealSendIn(content="\u8bd5\u8fd0\u884c\u540e\u771f\u53d1", device_id="rpa-01"),
            request=admin_request("admin"),
            db=self.db,
        )

        self.assertEqual(result["send_mode"], "real_send")
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["scheduled_at"])
        actions = [log.action for log in self.db.query(AuditLog).filter(AuditLog.entity_id == task["id"]).order_by(AuditLog.id).all()]
        self.assertIn("confirm_real_send", actions)

    def test_task_list_returns_operation_layer_for_role(self):
        create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u5f85\u5ba1\u6838\u5185\u5bb9"),
            db=self.db,
        )

        row = list_send_tasks(request=admin_request("coach"), db=self.db)[0]

        self.assertEqual(row["workflow_stage"], "\u5f85\u5ba1\u6838/\u8bd5\u8fd0\u884c")
        self.assertIn("dry_run", row["allowed_operations"])
        self.assertNotIn("confirm_real_send", row["allowed_operations"])

    def test_cancel_task_is_audited_and_list_is_limited(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u6d4b\u8bd5\u5185\u5bb9"),
            db=self.db,
        )

        cancel_send_task(task["id"], db=self.db)
        logs = list_audit_logs(entity_type="send_task", entity_id=task["id"], limit=1, db=self.db)

        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["action"], "cancel")

    def test_queue_dry_run_forces_safe_mode_and_audits(self):
        task = create_send_task(
            SendTaskIn(
                family_id="f1",
                target_name="\u4e00\u5408\u5b66\u793e",
                scene="test",
                content="\u8bd5\u8fd0\u884c\u5185\u5bb9",
                device_id="rpa-01",
                send_mode="real_send",
                confirm_real_send=True,
            ),
            db=self.db,
        )

        result = queue_task_dry_run(task["id"], db=self.db)

        self.assertEqual(result["send_mode"], "dry_run")
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["scheduled_at"])
        actions = [log.action for log in self.db.query(AuditLog).filter(AuditLog.entity_id == task["id"]).order_by(AuditLog.id).all()]
        self.assertIn("queue_dry_run", actions)

    def test_coach_can_queue_real_send_task_dry_run_without_edit_permission(self):
        task = create_send_task(
            SendTaskIn(
                family_id="f1",
                target_name="\u4e00\u5408\u5b66\u793e",
                scene="test",
                content="\u5148\u8bd5\u8fd0\u884c\u5185\u5bb9",
                device_id="rpa-01",
                send_mode="real_send",
                confirm_real_send=True,
            ),
            db=self.db,
        )

        with self.assertRaises(HTTPException):
            update_send_task(
                task["id"],
                SendTaskUpdate(
                    family_id="f1",
                    target_name="\u4e00\u5408\u5b66\u793e",
                    scene="test",
                    content="\u5148\u8bd5\u8fd0\u884c\u5185\u5bb9",
                    send_mode="dry_run",
                    status="pending",
                ),
                request=admin_request("coach"),
                db=self.db,
            )

        result = queue_task_dry_run(task["id"], request=admin_request("coach"), db=self.db)

        self.assertEqual(result["send_mode"], "dry_run")
        self.assertEqual(result["status"], "pending")

    def test_queue_dry_run_rejects_finished_task(self):
        task = create_send_task(
            SendTaskIn(family_id="f1", target_name="\u4e00\u5408\u5b66\u793e", scene="test", content="\u8bd5\u8fd0\u884c\u5185\u5bb9"),
            db=self.db,
        )
        self.db.get(SendTask, task["id"]).status = "sent"
        self.db.commit()

        with self.assertRaises(HTTPException):
            queue_task_dry_run(task["id"], db=self.db)


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
        self.dev_b = Device(
            device_id="dev-b",
            token="token-b",
            conversations='["\u4e00\u5408\u5b66\u793e"]',
        )
        self.db.add_all([self.dev, self.dev_b])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def add_conversation_proof(self, device_id: str = "dev-a", target_name: str = "一合学社", verified_at: datetime | None = None):
        self.db.add(
            DeviceConversationCheck(
                device_id=device_id,
                target_name=target_name,
                status="ok",
                message_count=1,
                source="企业微信RPA-视觉回读",
                verified_at=verified_at or datetime.utcnow(),
            )
        )
        self.db.commit()

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

    def test_real_send_claim_requires_device_control_switch(self):
        task = SendTask(
            family_id="f-real",
            target_name="一合学社",
            scene="real",
            content="真实发送由控制端设备开关放行",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.db.add(task)
        self.db.commit()

        self.assertEqual(claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db), [])
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")

        updated = update_device("dev-a", DeviceUpdateIn(allow_real_send=True), db=self.db)
        self.assertTrue(updated["allow_real_send"])
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()
        self.add_conversation_proof()
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])
        self.assertTrue(claimed[0]["device_allow_real_send"])
        self.db.refresh(task)
        self.assertEqual(task.status, "assigned")

    def test_real_send_without_device_binding_is_not_auto_claimed(self):
        task = SendTask(
            family_id="f-unbound-real",
            target_name="一合学社",
            scene="real",
            content="真实发送必须指定发送设备",
            send_mode="real_send",
            status="pending",
            device_id="",
        )
        self.db.add(task)
        self.db.commit()
        update_device("dev-a", DeviceUpdateIn(allow_real_send=True), db=self.db)

        self.assertEqual(claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db), [])
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.device_id, "")

    def test_real_send_claim_waits_until_device_wecom_is_ready(self):
        task = SendTask(
            family_id="f-real-wecom",
            target_name="一合学社",
            scene="real",
            content="企微健康后才领取",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.db.add(task)
        self.db.commit()
        update_device("dev-a", DeviceUpdateIn(allow_real_send=True), db=self.db)
        self.dev.wecom_ok = "N"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()

        self.assertEqual(claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db), [])
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")

        self.dev.wecom_ok = "Y"
        self.db.commit()
        self.add_conversation_proof()
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])

    def test_real_send_claim_waits_until_device_outbox_is_flushed(self):
        task = SendTask(
            family_id="f-real-outbox",
            target_name="一合学社",
            scene="real",
            content="补传队列清空后才可领取",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.db.add(task)
        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.dev.outbox_pending_count = 1
        self.dev.outbox_last_error = "network down"
        self.db.commit()

        self.assertEqual(claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db), [])
        row = list_send_tasks(db=self.db)[0]
        self.assertEqual(row["send_readiness"]["status"], "blocked")
        self.assertIn("发送结果待补传", "；".join(row["send_readiness"]["reasons"]))

        self.dev.outbox_pending_count = 0
        self.db.commit()
        self.add_conversation_proof()
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])

    def test_claim_returns_only_one_real_send_task_per_poll(self):
        dry_task = SendTask(
            family_id="f-dry-batch",
            target_name="一合学社",
            scene="dry",
            content="试运行不应与真实发送混批预占",
            send_mode="dry_run",
            status="pending",
        )
        real_task_1 = SendTask(
            family_id="f-real-batch-1",
            target_name="一合学社",
            scene="real",
            content="真实发送批次第一条",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        real_task_2 = SendTask(
            family_id="f-real-batch-2",
            target_name="一合学社",
            scene="real",
            content="真实发送批次第二条",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.db.add_all([dry_task, real_task_1, real_task_2])
        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()
        self.add_conversation_proof()

        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.db.refresh(dry_task)
        self.db.refresh(real_task_1)
        self.db.refresh(real_task_2)
        self.assertEqual([item["id"] for item in claimed], [real_task_1.id])
        self.assertEqual(real_task_1.status, "assigned")
        self.assertEqual(dry_task.status, "pending")
        self.assertEqual(real_task_2.status, "pending")

    def test_claim_waits_for_inflight_real_send_before_next_real_send(self):
        active_real = SendTask(
            family_id="f-real-active",
            target_name="一合学社",
            scene="real",
            content="上一条真实发送执行中",
            send_mode="real_send",
            status="assigned",
            device_id="dev-a",
            scheduled_at=datetime.utcnow(),
        )
        next_real = SendTask(
            family_id="f-real-next",
            target_name="一合学社",
            scene="real",
            content="下一条真实发送必须等待",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        dry_task = SendTask(
            family_id="f-dry-while-real",
            target_name="一合学社",
            scene="dry",
            content="试运行仍可领取",
            send_mode="dry_run",
            status="pending",
        )
        self.db.add_all([active_real, next_real, dry_task])
        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()

        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.db.refresh(next_real)
        self.db.refresh(dry_task)
        row = next(item for item in list_send_tasks(db=self.db) if item["id"] == next_real.id)
        self.assertEqual([item["id"] for item in claimed], [dry_task.id])
        self.assertEqual(next_real.status, "pending")
        self.assertEqual(dry_task.status, "assigned")
        self.assertEqual(row["send_readiness"]["status"], "blocked")
        self.assertIn("已有真实发送任务执行中", "；".join(row["send_readiness"]["reasons"]))

    def test_heartbeat_persists_outbox_state_for_device_monitoring(self):
        result = device_heartbeat(
            "dev-a",
            HeartbeatIn(
                wecom_ok="Y",
                conversations=["一合学社"],
                outbox_pending_count=2,
                outbox_last_error="API connection failed",
            ),
            dev=self.dev,
            db=self.db,
        )

        self.db.refresh(self.dev)
        self.assertEqual(self.dev.outbox_pending_count, 2)
        self.assertEqual(self.dev.outbox_last_error, "API connection failed")
        self.assertTrue(result["outbox_blocked"])
        self.assertIn("2", result["outbox_status_label"])

    def test_rpa_sync_records_device_conversation_read_proof(self):
        request = SimpleNamespace(headers={"x-device-id": "dev-a", "x-device-token": "token"}, state=SimpleNamespace())

        result = sync_rpa_conversation(
            RpaConversationIn(
                target_name="一合学社",
                family_id="WECOM_一合学社",
                messages=[
                    RpaMessageIn(speaker="我", content="群内可见消息", source="企业微信RPA-视觉回读"),
                ],
                auto_generate_reply=False,
                auto_create_reply_task=False,
                auto_generate_all_agents=False,
            ),
            request=request,
            db=self.db,
        )

        proof = self.db.query(DeviceConversationCheck).filter_by(device_id="dev-a", target_name="一合学社").one()
        self.assertEqual(result["conversation_check"]["status"], "ok")
        self.assertEqual(proof.message_count, 1)
        self.assertEqual(proof.source, "企业微信RPA-视觉回读")
        view = update_device("dev-a", DeviceUpdateIn(), db=self.db)
        self.assertEqual(view["conversation_proof_count"], 1)
        self.assertEqual(view["conversation_proof_total"], 1)
        self.assertTrue(view["conversation_proof_ready"])

    def test_device_view_reports_missing_and_expired_conversation_proofs(self):
        self.dev.conversations = '["一合学社", "测试2群", "许宝月"]'
        self.db.add(
            DeviceConversationCheck(
                device_id="dev-a",
                target_name="一合学社",
                status="ok",
                message_count=1,
                source="企业微信RPA-视觉回读",
                verified_at=datetime.utcnow(),
            )
        )
        self.db.add(
            DeviceConversationCheck(
                device_id="dev-a",
                target_name="测试2群",
                status="ok",
                message_count=1,
                source="企业微信RPA-视觉回读",
                verified_at=datetime.utcnow() - timedelta(hours=25),
            )
        )
        self.db.commit()

        view = update_device("dev-a", DeviceUpdateIn(), db=self.db)

        self.assertEqual(view["conversation_proof_count"], 1)
        self.assertEqual(view["conversation_proof_total"], 3)
        self.assertFalse(view["conversation_proof_ready"])
        self.assertEqual(set(view["conversation_proof_missing_targets"]), {"测试2群", "许宝月"})
        self.assertIn("1/3", view["conversation_proof_label"])

    def test_device_view_reports_real_send_closure_metrics(self):
        self.db.add_all([
            SendLog(
                task_id=101,
                family_id="f-real-ok",
                target_name="一合学社",
                status="sent",
                send_mode="real_send",
                device_id="dev-a",
                verify_status="confirmed",
                verify_detail="VERIFY_CONFIRMED: 目标「一合学社」回读命中",
                sent_at=datetime.utcnow(),
            ),
            SendLog(
                task_id=102,
                family_id="f-real-failed",
                target_name="一合学社",
                status="failed",
                send_mode="real_send",
                device_id="dev-a",
                verify_status="failed",
                detail="SEND_CONFIRM_FAILED: 未回读命中",
                sent_at=datetime.utcnow(),
            ),
            SendLog(
                task_id=103,
                family_id="f-other-device",
                target_name="一合学社",
                status="sent",
                send_mode="real_send",
                device_id="dev-b",
                verify_status="confirmed",
                sent_at=datetime.utcnow(),
            ),
        ])
        self.db.commit()

        view = update_device("dev-a", DeviceUpdateIn(), db=self.db)

        self.assertEqual(view["real_send_attempted_24h"], 2)
        self.assertEqual(view["real_send_confirmed_24h"], 1)
        self.assertEqual(view["real_send_confirm_failed_24h"], 1)
        self.assertEqual(view["real_send_confirm_rate_24h"], 50.0)
        self.assertIn("确认率 50.0%", view["real_send_success_label"])

    def test_real_send_claim_auto_prepares_missing_device_conversation_proof(self):
        task = SendTask(
            family_id="f-real-proof",
            target_name="一合学社",
            scene="real",
            content="有可读证明后才真发",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.db.add(task)
        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()

        first_claimed = claim_tasks("dev-a", limit=1, dev=self.dev, db=self.db)
        self.db.refresh(task)
        self.assertEqual(task.status, "pending")
        self.assertEqual(len(first_claimed), 1)
        self.assertEqual(first_claimed[0]["scene"], main_module.CONVERSATION_CHECK_SCENE)
        self.assertEqual(first_claimed[0]["target_name"], "一合学社")
        row = [item for item in list_send_tasks(db=self.db) if item["id"] == task.id][0]
        self.assertIn("没有成功读取目标", "；".join(row["send_readiness"]["reasons"]))
        self.assertEqual(claim_tasks("dev-a", limit=1, dev=self.dev, db=self.db), [])
        check_count = self.db.query(SendTask).filter(
            SendTask.scene == main_module.CONVERSATION_CHECK_SCENE,
            SendTask.target_name == "一合学社",
        ).count()
        self.assertEqual(check_count, 1)

        check_task = self.db.get(SendTask, first_claimed[0]["id"])
        check_task.status = "dry_run"
        self.add_conversation_proof()
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])

    def test_real_send_auto_prepare_respects_failed_check_cooldown(self):
        task = SendTask(
            family_id="f-real-proof-cooldown",
            target_name="一合学社",
            scene="real",
            content="证明失败后不要无限切群",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.db.add(task)
        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()

        first_claimed = claim_tasks("dev-a", limit=1, dev=self.dev, db=self.db)
        self.assertEqual(first_claimed[0]["scene"], main_module.CONVERSATION_CHECK_SCENE)
        check_task = self.db.get(SendTask, first_claimed[0]["id"])
        check_task.status = "failed"
        check_task.last_error = "搜索结果未命中"
        check_task.scheduled_at = datetime.utcnow()
        self.db.commit()

        self.assertEqual(claim_tasks("dev-a", limit=1, dev=self.dev, db=self.db), [])
        check_count = self.db.query(SendTask).filter(
            SendTask.scene == main_module.CONVERSATION_CHECK_SCENE,
            SendTask.target_name == "一合学社",
        ).count()
        self.assertEqual(check_count, 1)
        row = [item for item in list_send_tasks(db=self.db) if item["id"] == task.id][0]
        self.assertIn("自动补证明冷却", "；".join(row["send_readiness"]["reasons"]))

        check_task.scheduled_at = datetime.utcnow() - timedelta(seconds=main_module.CONVERSATION_CHECK_FAILURE_COOLDOWN_SECONDS + 1)
        self.db.commit()
        next_claimed = claim_tasks("dev-a", limit=1, dev=self.dev, db=self.db)

        self.assertEqual(next_claimed[0]["scene"], main_module.CONVERSATION_CHECK_SCENE)
        self.assertNotEqual(next_claimed[0]["id"], first_claimed[0]["id"])

    def test_task_readiness_explains_real_send_blocks_and_ready_state(self):
        task = SendTask(
            family_id="f-ready",
            target_name="一合学社",
            scene="real",
            content="准备度检查",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.db.add(task)
        self.db.commit()

        row = list_send_tasks(db=self.db)[0]
        self.assertEqual(row["send_readiness"]["status"], "blocked")
        self.assertIn("真实发送开关未开启", "；".join(row["send_readiness"]["reasons"]))

        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()
        row = list_send_tasks(db=self.db)[0]
        self.assertEqual(row["send_readiness"]["status"], "blocked")
        self.assertIn("没有成功读取目标", "；".join(row["send_readiness"]["reasons"]))
        self.assertEqual(row["send_readiness"]["actions"][0]["action"], "queue_conversation_check")
        self.assertEqual(row["send_readiness"]["actions"][0]["target_name"], "一合学社")
        self.assertTrue(row["send_readiness"]["actions"][0]["available"])

        self.add_conversation_proof()
        row = list_send_tasks(db=self.db)[0]

        self.assertEqual(row["send_readiness"]["status"], "ready")
        self.assertEqual(row["send_readiness"]["label"], "真实发送条件就绪")
        self.assertEqual(row["send_readiness"]["actions"], [])

    def test_task_readiness_check_action_points_to_existing_check_task(self):
        task = SendTask(
            family_id="f-ready",
            target_name="一合学社",
            scene="real",
            content="准备度检查",
            send_mode="real_send",
            status="pending",
            device_id="dev-a",
        )
        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.add(task)
        self.db.commit()
        existing = queue_device_conversation_check(
            "dev-a",
            DeviceConversationCheckRequestIn(target_name="一合学社", family_id="WECOM_一合学社"),
            db=self.db,
        )

        row = [item for item in list_send_tasks(db=self.db) if item["id"] == task.id][0]

        self.assertEqual(row["send_readiness"]["status"], "blocked")
        self.assertFalse(row["send_readiness"]["actions"][0]["available"])
        self.assertEqual(row["send_readiness"]["actions"][0]["existing_task_id"], existing["id"])

    def test_preflight_blocks_real_send_before_task_creation_until_ready(self):
        blocked = build_send_task_preflight(
            self.db,
            SendTaskPreflightIn(
                family_id="f-preflight",
                target_name="一合学社",
                scene="real",
                content="预检内容",
                send_mode="real_send",
                confirm_real_send=True,
                device_id="dev-a",
            ),
        )

        self.assertFalse(blocked["ok"])
        self.assertIn("真实发送开关未开启", "；".join(blocked["reasons"]))

        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()
        ready = build_send_task_preflight(
            self.db,
            SendTaskPreflightIn(
                family_id="f-preflight",
                target_name="一合学社",
                scene="real",
                content="预检内容",
                send_mode="real_send",
                confirm_real_send=True,
                device_id="dev-a",
            ),
        )

        self.assertFalse(ready["ok"])
        self.assertIn("没有成功读取目标", "；".join(ready["reasons"]))
        self.assertEqual(ready["conversation_check_hint"]["action"], "queue_conversation_check")
        self.assertEqual(ready["conversation_check_hint"]["device_id"], "dev-a")
        self.assertEqual(ready["conversation_check_hint"]["target_name"], "一合学社")
        self.assertTrue(ready["conversation_check_hint"]["available"])

        self.add_conversation_proof()
        ready = build_send_task_preflight(
            self.db,
            SendTaskPreflightIn(
                family_id="f-preflight",
                target_name="一合学社",
                scene="real",
                content="预检内容",
                send_mode="real_send",
                confirm_real_send=True,
                device_id="dev-a",
            ),
        )

        self.assertTrue(ready["ok"])
        self.assertEqual(ready["label"], "发送预检通过")
        self.assertIsNone(ready["conversation_check_hint"])

    def test_preflight_conversation_check_hint_points_to_existing_check_task(self):
        self.dev.allow_real_send = True
        self.dev.wecom_ok = "Y"
        self.dev.last_heartbeat = datetime.utcnow()
        self.db.commit()
        existing = queue_device_conversation_check(
            "dev-a",
            DeviceConversationCheckRequestIn(target_name="一合学社", family_id="WECOM_一合学社"),
            db=self.db,
        )

        blocked = build_send_task_preflight(
            self.db,
            SendTaskPreflightIn(
                family_id="f-preflight",
                target_name="一合学社",
                scene="real",
                content="预检内容",
                send_mode="real_send",
                confirm_real_send=True,
                device_id="dev-a",
            ),
        )

        self.assertFalse(blocked["ok"])
        self.assertFalse(blocked["conversation_check_hint"]["available"])
        self.assertEqual(blocked["conversation_check_hint"]["existing_task_id"], existing["id"])

    def test_allow_any_conversation_claims_group_or_private_chat_outside_whitelist(self):
        task = SendTask(
            family_id="f-private",
            target_name="许宝月",
            scene="private",
            content="私聊也由控制端统一派发",
            send_mode="dry_run",
            status="pending",
        )
        self.db.add(task)
        self.db.commit()

        self.assertEqual(claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db), [])

        updated = update_device("dev-a", DeviceUpdateIn(allow_any_conversation=True), db=self.db)
        self.assertTrue(updated["allow_any_conversation"])
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task.id])
        self.assertTrue(claimed[0]["server_allowed_target"])
        self.assertTrue(claimed[0]["device_allow_any_conversation"])

    def test_control_panel_can_queue_readonly_conversation_check_for_device(self):
        task = queue_device_conversation_check(
            "dev-a",
            DeviceConversationCheckRequestIn(target_name="一合学社", family_id="WECOM_一合学社"),
            db=self.db,
        )

        self.assertEqual(task["scene"], main_module.CONVERSATION_CHECK_SCENE)
        self.assertEqual(task["send_mode"], "dry_run")
        self.assertEqual(task["device_id"], "dev-a")
        self.assertEqual(claim_tasks("dev-b", limit=5, dev=self.dev_b, db=self.db), [])
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual([item["id"] for item in claimed], [task["id"]])
        self.assertEqual(claimed[0]["scene"], main_module.CONVERSATION_CHECK_SCENE)
        self.assertEqual(claimed[0]["target_name"], "一合学社")

    def test_control_panel_can_queue_all_conversation_checks_for_device(self):
        self.dev.conversations = '["一合学社", "测试2群", "一合学社"]'
        self.db.commit()

        result = queue_device_conversation_checks_batch(
            "dev-a",
            DeviceConversationBatchCheckRequestIn(),
            db=self.db,
        )

        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual({task["target_name"] for task in result["queued"]}, {"一合学社", "测试2群"})
        claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)

        self.assertEqual({item["target_name"] for item in claimed}, {"一合学社", "测试2群"})
        self.assertTrue(all(item["scene"] == main_module.CONVERSATION_CHECK_SCENE for item in claimed))

    def test_batch_conversation_check_skips_existing_pending_check(self):
        self.dev.conversations = '["一合学社", "测试2群"]'
        self.db.commit()
        existing = queue_device_conversation_check(
            "dev-a",
            DeviceConversationCheckRequestIn(target_name="一合学社", family_id="WECOM_一合学社"),
            db=self.db,
        )

        result = queue_device_conversation_checks_batch(
            "dev-a",
            DeviceConversationBatchCheckRequestIn(),
            db=self.db,
        )

        self.assertEqual(result["queued_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped"][0]["task_id"], existing["id"])
        self.assertEqual(result["queued"][0]["target_name"], "测试2群")

    def test_batch_conversation_check_can_only_queue_missing_or_expired_proofs(self):
        self.dev.conversations = '["一合学社", "测试2群", "许宝月"]'
        self.add_conversation_proof(target_name="一合学社")
        self.add_conversation_proof(target_name="测试2群", verified_at=datetime.utcnow() - timedelta(hours=25))

        result = queue_device_conversation_checks_batch(
            "dev-a",
            DeviceConversationBatchCheckRequestIn(missing_only=True),
            db=self.db,
        )

        self.assertEqual(result["queued_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual({task["target_name"] for task in result["queued"]}, {"测试2群", "许宝月"})

    def test_claim_does_not_assign_same_task_twice_across_devices(self):
        task = SendTask(
            family_id="f-shared",
            target_name="一合学社",
            scene="shared",
            content="只允许一个被控端领取",
            send_mode="dry_run",
            status="pending",
        )
        self.db.add(task)
        self.db.commit()

        first_claimed = claim_tasks("dev-a", limit=5, dev=self.dev, db=self.db)
        second_claimed = claim_tasks("dev-b", limit=5, dev=self.dev_b, db=self.db)

        self.db.refresh(task)
        self.assertEqual([item["id"] for item in first_claimed], [task.id])
        self.assertEqual(second_claimed, [])
        self.assertEqual(task.status, "assigned")
        self.assertEqual(task.device_id, "dev-a")


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
        self.assertEqual(log["send_reason"], "failed_unknown")
        self.assertEqual(log["send_reason_level"], "danger")
        self.assertTrue(log["screenshot_path"].startswith("/api/send-artifacts/task_"))
        filename = log["screenshot_path"].rsplit("/", 1)[1]
        self.assertEqual(resolve_send_screenshot(filename).read_bytes(), self.PNG_BYTES)

    def test_record_send_result_persists_group_verification(self):
        task = self.add_task()
        verified_at = datetime.utcnow()

        log = record_send_result(
            task.id,
            SendResultIn(
                status="sent",
                detail="REAL_RPA: 已通过企业微信 PC 端发送。",
                device_id="rpa-01",
                verify_status="confirmed",
                verify_detail="VERIFY_CONFIRMED: 目标「一合学社」可见聊天记录回读命中本次内容",
                verified_at=verified_at,
            ),
            db=self.db,
        )

        self.db.refresh(task)
        saved_log = self.db.query(SendLog).filter(SendLog.task_id == task.id).one()
        self.assertEqual(task.status, "sent")
        self.assertEqual(log["verify_status"], "confirmed")
        self.assertIn("回读命中", log["verify_detail"])
        self.assertEqual(saved_log.verify_status, "confirmed")
        self.assertEqual(saved_log.verified_at.replace(microsecond=0), verified_at.replace(microsecond=0))

    def test_record_send_result_is_idempotent_by_client_result_id(self):
        task = self.add_task()
        payload = SendResultIn(
            status="sent",
            detail="REAL_RPA: 已发送",
            device_id="rpa-01",
            client_result_id="rpa-01-task-1-result",
            verify_status="confirmed",
            verify_detail="VERIFY_CONFIRMED: 目标「一合学社」可见聊天记录回读命中本次内容",
        )

        first = record_send_result(task.id, payload, db=self.db)
        second = record_send_result(task.id, payload, db=self.db)

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.db.query(SendLog).filter(SendLog.task_id == task.id).count(), 1)

    def test_real_send_sent_without_group_verification_is_landed_as_failed(self):
        task = self.add_task()

        log = record_send_result(
            task.id,
            SendResultIn(status="sent", detail="REAL_RPA: 已通过企业微信 PC 端发送。", device_id="rpa-01"),
            db=self.db,
        )

        self.db.refresh(task)
        self.assertEqual(task.status, "failed")
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["verify_status"], "unknown")
        self.assertIn("SEND_CONFIRM_FAILED", log["detail"])

    def test_real_send_confirmed_without_evidence_is_landed_as_failed(self):
        task = self.add_task()

        log = record_send_result(
            task.id,
            SendResultIn(
                status="sent",
                detail="REAL_RPA: 已通过企业微信 PC 端发送。",
                device_id="rpa-01",
                verify_status="confirmed",
            ),
            db=self.db,
        )

        self.db.refresh(task)
        self.assertEqual(task.status, "failed")
        self.assertEqual(log["status"], "failed")
        self.assertEqual(log["verify_status"], "unknown")
        self.assertIn("缺少目标会话回读命中证据", log["verify_detail"])

    def test_record_send_result_keeps_dry_run_mode(self):
        task = self.add_task()
        task.send_mode = "dry_run"
        self.db.commit()

        log = record_send_result(task.id, SendResultIn(status="dry_run"), db=self.db)

        self.assertEqual(log["status"], "dry_run")
        self.assertEqual(log["send_mode"], "dry_run")
        self.assertEqual(log["send_reason"], "dry_run_done")

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
