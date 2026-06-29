import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import build_admin_service_quality_dashboard
from app.models import AIOutput, Family, ParentProfile, RawMessage, SendLog, SendTask, WeeklyReport


class AdminServiceQualityDashboardTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.now = datetime(2026, 6, 30, 10, 0, 0)

    def tearDown(self):
        self.db.close()

    def add_family(self, family_id: str, name: str, coach: str):
        family = Family(family_id=family_id, parent_nickname=name, coach_name=coach, service_status="服务中")
        self.db.add(family)
        self.db.add(RawMessage(family_id=family_id, message_time=self.now, speaker=name, content="今天正常沟通"))
        return family

    def test_dashboard_groups_quality_metrics_by_coach(self):
        self.add_family("risk", "风险家庭", "怡彤老师")
        self.db.add(ParentProfile(family_id="risk", service_risks="家长提出退费和投诉"))
        self.db.add(SendLog(task_id=1, family_id="risk", target_name="风险家庭", status="sent", sent_at=self.now))

        self.add_family("follow", "待跟进家庭", "怡彤老师")
        self.db.add(SendTask(family_id="follow", target_name="待跟进家庭", scene="回复", content="待发送", status="pending"))
        self.db.add(AIOutput(family_id="follow", agent_type="ai_reply", status="needs_review", display_text="待审核"))
        self.db.add(WeeklyReport(family_id="follow", status="needs_review", final_text="周报待审核"))
        self.db.add(SendLog(task_id=2, family_id="follow", target_name="待跟进家庭", status="failed", detail="发送失败", sent_at=self.now))

        self.add_family("normal", "正常家庭", "其他老师")
        self.db.add(SendLog(task_id=3, family_id="normal", target_name="正常家庭", status="dry_run", sent_at=self.now))
        self.db.commit()

        dashboard = build_admin_service_quality_dashboard(self.db, now=self.now)
        totals = dashboard["totals"]
        yitong = next(row for row in dashboard["coaches"] if row["coach_name"] == "怡彤老师")

        self.assertEqual(totals["coach_count"], 2)
        self.assertEqual(totals["family_count"], 3)
        self.assertEqual(totals["risk_family_count"], 1)
        self.assertEqual(totals["pending_task_count"], 1)
        self.assertEqual(totals["review_output_count"], 1)
        self.assertEqual(totals["review_report_count"], 1)
        self.assertEqual(totals["send_log_count"], 3)
        self.assertAlmostEqual(totals["send_completion_rate"], 0.6667)
        self.assertEqual(yitong["risk_family_count"], 1)
        self.assertEqual(yitong["followup_family_count"], 1)
        self.assertEqual(yitong["pending_task_count"], 1)
        self.assertAlmostEqual(yitong["send_failure_rate"], 0.5)
        self.assertEqual(yitong["risk_families"][0]["family_id"], "risk")

    def test_dashboard_supports_coach_filter(self):
        self.add_family("a", "A家庭", "怡彤老师")
        self.add_family("b", "B家庭", "其他老师")
        self.db.commit()

        dashboard = build_admin_service_quality_dashboard(self.db, coach_name="其他老师", now=self.now)

        self.assertEqual(dashboard["totals"]["coach_count"], 1)
        self.assertEqual(dashboard["totals"]["family_count"], 1)
        self.assertEqual(dashboard["coaches"][0]["coach_name"], "其他老师")


if __name__ == "__main__":
    unittest.main()
