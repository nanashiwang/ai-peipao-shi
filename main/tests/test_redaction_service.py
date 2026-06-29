import unittest
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import maybe_redact_for_request, ops_redacted_export
from app.models import Family, RawMessage, SendLog, SendTask
from app.services.admin_auth import admin_auth_secret, sign_admin_token
from app.services.redaction_service import mask_name, mask_phone, redact_record


class RedactionServiceTest(unittest.TestCase):
    def test_masks_phone_name_child_and_chat_content(self):
        record = {
            "parent_nickname": "张妈妈",
            "child_grade": "初一",
            "parent_phone": "13800138000",
            "content": "孩子今天说想请假，电话 13800138000。",
            "edited_output": "回复张妈妈：孩子初一压力大，电话 13800138000。",
            "child_summary": "孩子初一，近期情绪波动。",
        }

        redacted = redact_record(record)

        self.assertEqual(mask_phone("13800138000"), "138****8000")
        self.assertEqual(mask_name("张妈妈"), "张*妈")
        self.assertEqual(redacted["parent_nickname"], "张*妈")
        self.assertEqual(redacted["child_grade"], "[孩子信息已脱敏]")
        self.assertEqual(redacted["parent_phone"], "138****8000")
        self.assertIn("聊天内容已脱敏", redacted["content"])
        self.assertIn("聊天内容已脱敏", redacted["edited_output"])
        self.assertIn("聊天内容已脱敏", redacted["child_summary"])
        self.assertNotIn("13800138000", redacted["content"])
        self.assertNotIn("初一", str(redacted))
        self.assertEqual(redacted["privacy_level"], "redacted")

    def test_readonly_request_gets_redacted_view(self):
        token = sign_admin_token("viewer", "readonly", "只读", admin_auth_secret())
        request = SimpleNamespace(headers={"authorization": f"Bearer {token}"}, state=SimpleNamespace())
        data = {"parent_nickname": "王妈妈", "content": "电话 13900139000，孩子最近不想打卡。"}

        redacted = maybe_redact_for_request(data, request)

        self.assertEqual(redacted["parent_nickname"], "王*妈")
        self.assertIn("聊天内容已脱敏", redacted["content"])
        self.assertNotIn("13900139000", str(redacted))

    def test_admin_request_keeps_raw_view(self):
        token = sign_admin_token("admin", "admin", "管理员", admin_auth_secret())
        request = SimpleNamespace(headers={"authorization": f"Bearer {token}"}, state=SimpleNamespace())
        data = {"parent_nickname": "王妈妈", "content": "电话 13900139000"}

        raw = maybe_redact_for_request(data, request)

        self.assertEqual(raw, data)

    def test_redacted_export_contains_no_raw_family_or_message_text(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        db = sessionmaker(bind=engine, future=True)()
        try:
            db.add(Family(family_id="f1", parent_nickname="林妈妈", child_grade="初一"))
            db.add(RawMessage(family_id="f1", speaker="林妈妈", content="孩子手机号 13800138000，今天情绪低落", message_time=datetime(2026, 6, 30, 9, 0, 0)))
            db.add(SendTask(family_id="f1", target_name="林妈妈", scene="回复", content="安抚家长原文"))
            db.add(SendLog(task_id=1, family_id="f1", target_name="林妈妈", status="failed", detail="失败，手机号 13800138000"))
            db.commit()

            snapshot = ops_redacted_export(db=db)
        finally:
            db.close()

        raw = str(snapshot)
        self.assertEqual(snapshot["sensitivity"], "redacted")
        self.assertNotIn("林妈妈", raw)
        self.assertNotIn("初一", raw)
        self.assertNotIn("13800138000", raw)
        self.assertIn("聊天内容已脱敏", raw)


if __name__ == "__main__":
    unittest.main()
