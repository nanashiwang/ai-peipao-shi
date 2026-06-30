import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import upsert_parent_profile_from_agent
from app.models import Family, ParentProfile, RawMessage
from app.services.agent_service import build_agent_context, run_family_profile_agent


class ParentProfileEnrichmentTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="f1", parent_nickname="林妈妈", child_grade="初一"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def add_message(self, content: str, speaker: str = "林妈妈"):
        self.db.add(
            RawMessage(
                family_id="f1",
                message_time=datetime(2026, 6, 30, 9, 0, 0),
                speaker=speaker,
                content=content,
                checkin_status="完成打卡" if "打卡" in content else "",
            )
        )

    def test_family_profile_agent_persists_satisfaction_and_renewal_intent(self):
        self.add_message("孩子今天完成打卡，下一阶段续报怎么安排？")
        self.add_message("希望老师给一个后续课程节奏。")
        self.db.commit()

        context = build_agent_context(self.db, "f1")
        with patch("app.services.agent_service._call_ark_or_none", return_value={"_ark_error": "test"}):
            result = run_family_profile_agent(context)
        upsert_parent_profile_from_agent(self.db, "f1", result)
        self.db.commit()

        profile = self.db.query(ParentProfile).filter(ParentProfile.family_id == "f1").one()

        self.assertEqual(profile.satisfaction_level, "中高")
        self.assertEqual(profile.renewal_intent, "明确关注")
        self.assertIn("续报意向：明确关注", result["display_text"])
        self.assertTrue(profile.pain_points)
        self.assertTrue(profile.communication_style)
        self.assertTrue(profile.service_risks)

    def test_risk_message_marks_low_satisfaction_without_renewal_signal(self):
        self.add_message("最近感觉没效果，有点不满意，想投诉。")
        self.db.commit()

        with patch("app.services.agent_service._call_ark_or_none", return_value={"_ark_error": "test"}):
            result = run_family_profile_agent(build_agent_context(self.db, "f1"))
        upsert_parent_profile_from_agent(self.db, "f1", result)
        self.db.commit()

        profile = self.db.query(ParentProfile).filter(ParentProfile.family_id == "f1").one()

        self.assertEqual(profile.satisfaction_level, "低")
        self.assertEqual(profile.renewal_intent, "暂不明确")
        self.assertIn("不满意", profile.service_risks)


if __name__ == "__main__":
    unittest.main()
