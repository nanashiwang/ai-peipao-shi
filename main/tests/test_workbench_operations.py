import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import build_service_funnel, build_workbench_todos
from app.models import AIOutput, Family, ParentProfile, RawMessage, SendLog, WeeklyReport


class WorkbenchOperationsTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.now = datetime(2026, 6, 30, 10, 0, 0)

    def tearDown(self):
        self.db.close()

    def add_family(self, family_id, name, coach="怡彤老师", status="服务中"):
        family = Family(family_id=family_id, parent_nickname=name, coach_name=coach, service_status=status)
        self.db.add(family)
        self.db.flush()
        return family

    def add_message(self, family_id, content, days_ago=0):
        msg = RawMessage(
            family_id=family_id,
            message_time=self.now - timedelta(days=days_ago),
            speaker="家长",
            content=content,
            source="单测",
        )
        self.db.add(msg)
        self.db.flush()
        return msg

    def test_service_funnel_infers_core_statuses_and_coach_filter(self):
        self.add_family("risk", "风险家庭")
        self.db.add(ParentProfile(family_id="risk", service_risks="家长投诉并提出退费"))
        self.add_family("renew", "续报家庭", status="续报沟通")
        self.add_family("closed", "结课家庭", status="已结课")
        self.add_family("follow", "沉默家庭")
        self.add_message("follow", "上周沟通", days_ago=5)
        self.add_family("normal", "正常家庭")
        self.add_message("normal", "今天已完成打卡")
        self.add_family("other", "其他老师家庭", coach="其他老师")
        self.add_message("other", "今天正常沟通")
        self.db.commit()

        funnel = build_service_funnel(self.db, now=self.now)
        counts = {stage["stage"]: stage["family_count"] for stage in funnel["stages"]}
        filtered = build_service_funnel(self.db, coach_name="怡彤老师", now=self.now)

        self.assertEqual(counts["风险"], 1)
        self.assertEqual(counts["续报"], 1)
        self.assertEqual(counts["已结课"], 1)
        self.assertEqual(counts["需跟进"], 1)
        self.assertEqual(counts["正常"], 2)
        self.assertEqual(filtered["total_families"], 5)

    def test_todo_aggregation_covers_business_backlog_and_coach_filter(self):
        family = self.add_family("f1", "张妈妈")
        self.add_family("f2", "李妈妈", coach="其他老师")
        self.add_message("f1", "孩子的PBL作品还没提交，今晚可能完成不了。")
        self.add_message("f1", "今天请假上不了课，想问怎么补课。")
        self.add_message("f1", "最近感觉没效果，我有点不满意想投诉。")
        self.add_message("f2", "PBL还没交。")
        self.db.add_all([
            WeeklyReport(family_id=family.family_id, status="approved", final_text="本周周报"),
            AIOutput(family_id=family.family_id, agent_type="ai_reply", status="needs_review", display_text="待审核回复"),
            SendLog(task_id=7, family_id=family.family_id, target_name="张妈妈", status="failed", detail="窗口未找到"),
        ])
        self.db.commit()

        todos = build_workbench_todos(self.db, coach_name="怡彤老师", now=self.now)
        counts = {category["key"]: category["count"] for category in todos["categories"]}

        self.assertEqual(counts["pbl_incomplete"], 1)
        self.assertEqual(counts["leave_makeup"], 1)
        self.assertEqual(counts["weekly_pending_send"], 1)
        self.assertEqual(counts["negative_feedback"], 1)
        self.assertEqual(counts["ai_review"], 1)
        self.assertEqual(counts["send_failed"], 1)
        pbl_items = next(category["items"] for category in todos["categories"] if category["key"] == "pbl_incomplete")
        self.assertEqual(pbl_items[0]["family_id"], "f1")


if __name__ == "__main__":
    unittest.main()
