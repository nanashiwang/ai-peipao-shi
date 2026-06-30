import json
import unittest

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import AIOutputTaskIn, ai_safety_findings, create_task_from_ai_output, save_ai_output
from app.models import AIOutput, AuditLog, Family, SendTask


class AiSafetyBoundaryTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="f1", parent_nickname="\u5f20\u5988\u5988"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def add_output(self, status="needs_review", text="\u9700\u8981\u5904\u7406\u9000\u6b3e\u548c\u6295\u8bc9"):
        output = AIOutput(
            family_id="f1",
            agent_type="ai_reply",
            source="\u5355\u6d4b",
            raw_json=json.dumps({"\u4f7f\u7528\u4f9d\u636e\u6458\u8981": ["\u5bb6\u957f\u63d0\u5230\u9000\u6b3e"]}, ensure_ascii=False),
            display_text=text,
            edited_output=text,
            status=status,
            risk_level="\u9ad8",
            need_human_review="Y",
        )
        self.db.add(output)
        self.db.commit()
        return output

    def add_safe_output(self, status="needs_review", text="\u5e38\u89c4\u6253\u5361\u63d0\u9192"):
        output = AIOutput(
            family_id="f1",
            agent_type="ai_reply",
            source="\u5355\u6d4b",
            raw_json=json.dumps({"\u4f7f\u7528\u4f9d\u636e\u6458\u8981": ["\u5bb6\u957f\u8be2\u95ee\u6253\u5361"]}, ensure_ascii=False),
            display_text=text,
            edited_output=text,
            status=status,
            risk_level="\u4f4e",
            need_human_review="N",
        )
        self.db.add(output)
        self.db.commit()
        return output

    def test_unapproved_sensitive_output_cannot_create_task(self):
        output = self.add_output(status="needs_review")

        with self.assertRaises(HTTPException):
            create_task_from_ai_output(output.id, AIOutputTaskIn(send_mode="dry_run"), db=self.db)

        self.db.refresh(output)
        self.assertEqual(output.status, "needs_review")
        self.assertEqual(self.db.query(SendTask).count(), 0)
        self.assertEqual(self.db.query(AuditLog).count(), 0)

    def test_approved_sensitive_output_cannot_create_real_send_task(self):
        output = self.add_output(status="approved")

        with self.assertRaises(HTTPException):
            create_task_from_ai_output(
                output.id,
                AIOutputTaskIn(send_mode="real_send", confirm_real_send=True),
                db=self.db,
            )

        self.db.refresh(output)
        self.assertEqual(output.status, "approved")
        self.assertEqual(self.db.query(SendTask).count(), 0)
        self.assertEqual(self.db.query(AuditLog).count(), 0)

    def test_sensitive_override_content_requires_manual_review(self):
        output = self.add_safe_output(status="needs_review")

        with self.assertRaises(HTTPException):
            create_task_from_ai_output(
                output.id,
                AIOutputTaskIn(content="\u8bf7\u5904\u7406\u9000\u6b3e\u6295\u8bc9", send_mode="dry_run"),
                db=self.db,
            )

        self.db.refresh(output)
        self.assertEqual(output.status, "needs_review")
        self.assertEqual(output.edited_output, "\u5e38\u89c4\u6253\u5361\u63d0\u9192")
        self.assertEqual(self.db.query(SendTask).count(), 0)
        self.assertEqual(self.db.query(AuditLog).count(), 0)

    def test_approved_sensitive_output_can_create_dry_run_review_task(self):
        output = self.add_output(status="approved")

        task = create_task_from_ai_output(output.id, AIOutputTaskIn(send_mode="dry_run"), db=self.db)

        self.assertEqual(task["send_mode"], "dry_run")
        self.assertEqual(self.db.query(SendTask).count(), 1)

    def test_safety_scan_ignores_json_field_names(self):
        findings = ai_safety_findings(json.dumps({"是否需要人工介入": False, "推荐回复": "今天继续打卡即可"}, ensure_ascii=False))

        self.assertFalse(findings["requires_manual"])

    def test_safe_output_with_review_field_name_can_create_task(self):
        output = self.add_safe_output(status="needs_review")
        output.raw_json = json.dumps({"是否需要人工介入": False, "推荐回复": "今天继续打卡即可"}, ensure_ascii=False)
        self.db.commit()

        task = create_task_from_ai_output(output.id, AIOutputTaskIn(send_mode="dry_run"), db=self.db)

        self.assertEqual(task["send_mode"], "dry_run")
        self.assertEqual(self.db.query(SendTask).count(), 1)

    def test_save_ai_output_marks_sensitive_result_as_manual_review(self):
        result = {
            "raw": {"\u5efa\u8bae\u8ddf\u8fdb\u52a8\u4f5c": ["\u8f6c\u4eba\u5de5"], "\u4f7f\u7528\u4f9d\u636e\u6458\u8981": ["\u6295\u8bc9"]},
            "display_text": "\u8fd9\u91cc\u6d89\u53ca\u6295\u8bc9\uff0c\u9700\u8981\u4e3b\u7ba1\u786e\u8ba4\u3002",
            "risk_level": "\u4e2d",
            "need_human_review": False,
            "suggested_actions": ["\u4eba\u5de5\u590d\u6838"],
        }

        output = save_ai_output(self.db, "f1", "ai_reply", "\u5355\u6d4b", result)

        self.assertEqual(output.need_human_review, "Y")


if __name__ == "__main__":
    unittest.main()
