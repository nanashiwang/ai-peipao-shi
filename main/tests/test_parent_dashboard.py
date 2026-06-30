import unittest
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import ParentReportAckIn, ParentReportFeedbackIn, ReportUpdate, admin_auth_secret, parent_ack_report, parent_dashboard, parent_feedback_report, update_report
from app.models import Family, FollowupRecord, ParentProfile, RawMessage, UserAccount, WeeklyReport
from app.services.admin_auth import bearer_token, sign_parent_token, verify_parent_token


class ParentDashboardTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add_all(
            [
                Family(
                    family_id="f1",
                    parent_nickname="林妈妈",
                    child_grade="初一",
                    campus_name="南坪校区",
                    coach_name="怡彤老师",
                    course_stage="S1",
                    unit_progress="U3",
                    pbl_count=2,
                    checkin_rate="80%",
                    next_milestone="完成 U3 复盘",
                ),
                Family(family_id="f2", parent_nickname="周爸爸"),
                UserAccount(username="lin", password="123456", display_name="林妈妈", role="parent", family_id="f1"),
                ParentProfile(family_id="f1", child_summary="启动慢但能完成拆小任务", suggested_actions="每天先做一个核心动作"),
                WeeklyReport(
                    family_id="f1",
                    week_label="第1周",
                    status="approved",
                    overall_state="节奏恢复中",
                    main_changes="能按计划完成核心任务",
                    parent_focus="降低一次性要求",
                    teacher_suggestion="继续拆小任务",
                    final_text="本周整体稳定。",
                    send_status="sent",
                    sent_at=datetime(2026, 1, 1, 9, 0),
                ),
                WeeklyReport(family_id="f1", week_label="草稿周", status="draft", final_text="不应展示"),
                RawMessage(family_id="f1", speaker="林妈妈", content="孩子今天完成打卡", checkin_status="已打卡"),
                RawMessage(family_id="f2", speaker="周爸爸", content="其他家庭消息"),
            ]
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def token(self, family_id: str = "f1", username: str = "lin") -> str:
        return sign_parent_token(username, "林妈妈", family_id, admin_auth_secret())

    def test_parent_token_roundtrip(self):
        token = self.token()
        identity = verify_parent_token(token, admin_auth_secret())

        self.assertEqual(bearer_token(f"Bearer {token}"), token)
        self.assertEqual(identity.family_id, "f1")
        self.assertEqual(identity.username, "lin")

    def test_parent_dashboard_returns_only_bound_family(self):
        data = parent_dashboard(authorization=f"Bearer {self.token()}", db=self.db)

        self.assertEqual(data["family"]["family_id"], "f1")
        self.assertEqual(data["family"]["course_stage"], "S1")
        self.assertEqual(data["progress"]["checkin_count"], 1)
        self.assertEqual(data["weekly_report"]["week_label"], "第1周")
        self.assertNotIn("不应展示", str(data))
        self.assertNotIn("其他家庭消息", str(data))

    def test_parent_can_ack_approved_report(self):
        report_id = self.db.query(WeeklyReport).filter(WeeklyReport.status == "approved").one().id

        result = parent_ack_report(report_id, ParentReportAckIn(note="已阅读"), authorization=f"Bearer {self.token()}", db=self.db)
        dashboard = parent_dashboard(authorization=f"Bearer {self.token()}", db=self.db)

        self.assertEqual(result["ack_by"], "林妈妈")
        self.assertIsNotNone(result["report"]["parent_ack_at"])
        self.assertEqual(result["report"]["parent_ack_note"], "已阅读")
        self.assertIsNotNone(dashboard["weekly_report"]["parent_ack_at"])

    def test_report_edit_resets_parent_ack(self):
        report = self.db.query(WeeklyReport).filter(WeeklyReport.status == "approved").one()
        parent_ack_report(report.id, ParentReportAckIn(note="已阅读"), authorization=f"Bearer {self.token()}", db=self.db)
        parent_feedback_report(report.id, ParentReportFeedbackIn(score=5, note="不错"), authorization=f"Bearer {self.token()}", db=self.db)

        updated = update_report(report.id, ReportUpdate(final_text="更新后的正式周报", status="approved"), db=self.db)

        self.assertIsNone(updated["parent_ack_at"])
        self.assertEqual(updated["parent_ack_note"], "")
        self.assertEqual(updated["parent_feedback_score"], 0)
        self.assertEqual(updated["parent_feedback_note"], "")
        self.assertIsNone(updated["parent_feedback_at"])

    def test_parent_feedback_updates_report_and_escalates_low_score(self):
        report_id = self.db.query(WeeklyReport).filter(WeeklyReport.status == "approved").one().id

        result = parent_feedback_report(report_id, ParentReportFeedbackIn(score=2, note="建议更具体"), authorization=f"Bearer {self.token()}", db=self.db)
        dashboard = parent_dashboard(authorization=f"Bearer {self.token()}", db=self.db)
        profile = self.db.query(ParentProfile).filter(ParentProfile.family_id == "f1").one()
        followup = self.db.query(FollowupRecord).filter(FollowupRecord.family_id == "f1").one()

        self.assertTrue(result["followup_created"])
        self.assertEqual(result["report"]["parent_feedback_score"], 2)
        self.assertEqual(dashboard["weekly_report"]["parent_feedback_note"], "建议更具体")
        self.assertEqual(profile.satisfaction_level, "低")
        self.assertEqual(followup.status, "需升级")
        self.assertIn(f"周报#{report_id}", followup.content)

    def test_parent_feedback_rejects_invalid_score_and_unowned_report(self):
        report_id = self.db.query(WeeklyReport).filter(WeeklyReport.status == "approved").one().id
        with self.assertRaises(HTTPException) as bad_score:
            parent_feedback_report(report_id, ParentReportFeedbackIn(score=6, note="越界"), authorization=f"Bearer {self.token()}", db=self.db)
        self.assertEqual(bad_score.exception.status_code, 400)

        other = WeeklyReport(family_id="f2", week_label="其他家庭", status="approved", final_text="其他家庭")
        self.db.add(other)
        self.db.commit()
        with self.assertRaises(HTTPException) as other_blocked:
            parent_feedback_report(other.id, ParentReportFeedbackIn(score=4), authorization=f"Bearer {self.token()}", db=self.db)
        self.assertEqual(other_blocked.exception.status_code, 404)

    def test_parent_dashboard_rejects_mismatched_account(self):
        with self.assertRaises(HTTPException) as blocked:
            parent_dashboard(authorization=f"Bearer {self.token('f2')}", db=self.db)

        self.assertEqual(blocked.exception.status_code, 401)

    def test_parent_cannot_ack_other_family_or_draft_report(self):
        draft_id = self.db.query(WeeklyReport).filter(WeeklyReport.status == "draft").one().id
        with self.assertRaises(HTTPException) as draft_blocked:
            parent_ack_report(draft_id, ParentReportAckIn(), authorization=f"Bearer {self.token()}", db=self.db)
        self.assertEqual(draft_blocked.exception.status_code, 404)

        other = WeeklyReport(family_id="f2", week_label="其他家庭", status="approved", final_text="其他家庭")
        self.db.add(other)
        self.db.commit()
        with self.assertRaises(HTTPException) as other_blocked:
            parent_ack_report(other.id, ParentReportAckIn(), authorization=f"Bearer {self.token()}", db=self.db)
        self.assertEqual(other_blocked.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
