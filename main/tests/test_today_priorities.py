import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import build_today_priorities
from app.models import AIOutput, Family, ParentProfile, RawMessage, SendTask, WeeklyReport


class TodayPrioritiesTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.now = datetime(2026, 6, 30, 10, 0, 0)
        self.db.add_all([
            Family(family_id="risk", parent_nickname="\u98ce\u9669\u5bb6\u5ead", coach_name="\u6021\u5f64\u8001\u5e08"),
            Family(family_id="stale", parent_nickname="\u6c89\u9ed8\u5bb6\u5ead"),
            Family(family_id="quiet", parent_nickname="\u6b63\u5e38\u5bb6\u5ead"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_prioritizes_risk_review_and_pending_tasks(self):
        self.db.add_all([
            ParentProfile(
                family_id="risk",
                service_risks="\u5bb6\u957f\u660e\u786e\u63d0\u5230\u9000\u8d39\u548c\u6295\u8bc9",
                suggested_actions="\u4e3b\u7ba1\u4ecb\u5165",
            ),
            RawMessage(
                family_id="risk",
                message_time=self.now - timedelta(days=1),
                speaker="\u5bb6\u957f",
                content="\u4e0d\u6ee1\u610f",
            ),
            AIOutput(
                family_id="risk",
                agent_type="ai_reply",
                display_text="\u9700\u8981\u5ba1\u6838",
                edited_output="\u9700\u8981\u5ba1\u6838",
                status="needs_review",
            ),
            SendTask(
                family_id="risk",
                target_name="\u98ce\u9669\u5bb6\u5ead",
                scene="\u56de\u590d",
                content="\u5f85\u53d1\u9001",
                send_mode="real_send",
                status="pending",
            ),
            RawMessage(
                family_id="stale",
                message_time=self.now - timedelta(days=8),
                speaker="\u5bb6\u957f",
                content="\u5f88\u4e45\u524d\u7684\u6d88\u606f",
            ),
            RawMessage(
                family_id="quiet",
                message_time=self.now,
                speaker="\u5bb6\u957f",
                content="\u4eca\u5929\u6b63\u5e38\u6c9f\u901a",
            ),
        ])
        self.db.commit()

        priorities = build_today_priorities(self.db, now=self.now)

        self.assertEqual(priorities[0]["family_id"], "risk")
        self.assertEqual(priorities[0]["level"], "高")
        self.assertIn("\u5b58\u5728\u9ad8\u98ce\u9669", "\uff1b".join(priorities[0]["reasons"]))
        self.assertEqual(priorities[0]["suggested_action"], "\u5148\u5904\u7406\u5f85\u53d1\u9001\u4efb\u52a1")
        self.assertIn("stale", [item["family_id"] for item in priorities])
        self.assertNotIn("quiet", [item["family_id"] for item in priorities])

    def test_includes_report_review_and_honors_limit(self):
        self.db.add_all([
            WeeklyReport(family_id="risk", week_label="W1", status="needs_review", final_text="\u5468\u62a5"),
            WeeklyReport(family_id="stale", week_label="W1", status="needs_review", final_text="\u5468\u62a5"),
        ])
        self.db.commit()

        priorities = build_today_priorities(self.db, limit=1, now=self.now)

        self.assertEqual(len(priorities), 1)
        self.assertEqual(priorities[0]["review_report_count"], 1)


if __name__ == "__main__":
    unittest.main()
