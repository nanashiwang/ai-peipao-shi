import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import create_family_ai_bundle
from app.models import AIOutput, CheckinRecord, Family, ParentProfile, RawMessage, WeeklyReport


def agent_result(agent: str, raw: dict | None = None) -> dict:
    return {
        "raw": {
            "agent": agent,
            "风险等级": "低",
            "使用依据摘要": ["单测消息"],
            **(raw or {}),
        },
        "display_text": f"{agent} 输出",
        "risk_level": "低",
        "need_human_review": False,
        "suggested_actions": ["人工复核"],
    }


class FamilyAiBundleTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="f1", parent_nickname="林妈妈", coach_name="怡彤老师"))
        self.db.add(
            RawMessage(
                family_id="f1",
                message_time=datetime(2026, 6, 30, 9, 0, 0),
                speaker="林妈妈",
                content="今天完成打卡，想了解下一阶段安排。",
                checkin_status="完成打卡",
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_create_bundle_generates_four_outputs_and_review_artifacts(self):
        profile = agent_result(
            "family_profile_agent",
            {
                "家长关注点": ["阶段安排"],
                "沟通风格": "结果导向型",
                "满意度评级": "中高",
                "风险信号": ["暂无明显高风险"],
                "续报意向": "明确关注",
                "学生状态": "打卡稳定",
                "建议跟进动作": ["同步阶段规划"],
            },
        )
        weekly = agent_result(
            "weekly_report_agent",
            {
                "本周学习总结": "本周稳定",
                "学习亮点": ["完成打卡"],
                "需要关注": ["阶段规划"],
                "下周建议": ["安排复盘"],
            },
        )

        with patch("app.main.run_family_profile_agent_service", return_value=profile), \
             patch("app.main.run_weekly_report_agent_service", return_value=weekly), \
             patch("app.main.run_reply_agent_service", return_value=agent_result("ai_reply_agent")), \
             patch("app.main.run_checkin_pbl_agent_service", return_value=agent_result("checkin_pbl_agent")):
            result = create_family_ai_bundle(self.db, "f1", "单测一键生成")
            self.db.commit()

        output_types = {item.agent_type for item in result["outputs"]}

        self.assertEqual(output_types, {"family_profile", "weekly_report", "ai_reply", "checkin_pbl"})
        self.assertEqual(self.db.query(AIOutput).count(), 4)
        self.assertEqual(self.db.query(WeeklyReport).count(), 1)
        self.assertEqual(self.db.query(CheckinRecord).count(), 1)
        saved_profile = self.db.query(ParentProfile).filter(ParentProfile.family_id == "f1").one()
        self.assertEqual(saved_profile.satisfaction_level, "中高")
        self.assertEqual(saved_profile.renewal_intent, "明确关注")

    def test_bundle_requires_messages(self):
        self.db.add(Family(family_id="empty", parent_nickname="空家庭"))
        self.db.commit()

        with self.assertRaises(HTTPException):
            create_family_ai_bundle(self.db, "empty")


if __name__ == "__main__":
    unittest.main()
