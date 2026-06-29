import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import (
    REAL_SEND_MIN_INTERVAL_SECONDS,
    validate_device_conversation_scope,
    validate_real_send_risk,
    validate_send_mode,
    validate_send_mode_submit,
    validate_send_task_content,
)
from app.models import SendLog, SendTask


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


if __name__ == "__main__":
    unittest.main()
