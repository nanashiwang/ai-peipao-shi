import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import AutoReplyDraftIn, admin_auth_secret, auto_draft_replies, sign_admin_token
from app.models import AIOutput, Family, RawMessage, SendTask


def scoped_request(role: str = "coach", username: str = "coach_yitong", display_name: str = "怡彤老师"):
    token = sign_admin_token(username, role, display_name, admin_auth_secret())
    return SimpleNamespace(headers={"authorization": f"Bearer {token}"}, state=SimpleNamespace())


def agent_result(text: str = "收到，我会记录并跟进。") -> dict:
    return {
        "raw": {
            "agent": "ai_reply_agent",
            "推荐回复": text,
            "风险等级": "低",
            "是否建议人工介入": True,
            "推荐下一步动作": ["人工审核"],
            "使用依据摘要": ["单测消息"],
        },
        "display_text": text,
        "risk_level": "低",
        "need_human_review": True,
        "suggested_actions": ["人工审核"],
    }


class AutoReplyDraftTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def add_family(self, family_id: str, parent_name: str, coach_name: str = "怡彤老师") -> None:
        self.db.add(Family(family_id=family_id, parent_nickname=parent_name, coach_name=coach_name))

    def add_message(self, family_id: str, speaker: str, content: str) -> None:
        self.db.add(
            RawMessage(
                family_id=family_id,
                speaker=speaker,
                content=content,
                message_time=datetime.utcnow(),
                is_effective="Y",
            )
        )

    def test_creates_review_draft_without_send_task(self):
        self.add_family("f1", "林妈妈")
        self.add_family("f2", "周爸爸")
        self.add_message("f1", "林妈妈", "孩子今天请假，明天怎么补？")
        self.db.commit()

        with patch("app.main.run_reply_agent_service", return_value=agent_result()) as agent:
            result = auto_draft_replies(AutoReplyDraftIn(), request=None, db=self.db)

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["skipped_items"][0]["family_id"], "f2")
        agent.assert_called_once()
        output = self.db.query(AIOutput).one()
        self.assertEqual(output.family_id, "f1")
        self.assertEqual(output.agent_type, "ai_reply")
        self.assertEqual(output.source, "自动回复草稿")
        self.assertEqual(output.status, "needs_review")
        self.assertEqual(self.db.query(SendTask).count(), 0)

    def test_skips_recent_pending_reply_to_avoid_duplicate_drafts(self):
        self.add_family("f1", "林妈妈")
        self.add_message("f1", "林妈妈", "今天作业完成了")
        self.db.add(
            AIOutput(
                family_id="f1",
                agent_type="ai_reply",
                status="needs_review",
                source="手动生成",
                display_text="已有待审核回复",
            )
        )
        self.db.commit()

        with patch("app.main.run_reply_agent_service", return_value=agent_result()) as agent:
            result = auto_draft_replies(AutoReplyDraftIn(), request=None, db=self.db)

        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["skipped_items"][0]["reason"], "已有近期待审核回复")
        agent.assert_not_called()
        self.assertEqual(self.db.query(AIOutput).count(), 1)

    def test_respects_coach_scope(self):
        self.add_family("own", "林妈妈", "怡彤老师")
        self.add_family("other", "周爸爸", "其他老师")
        self.add_message("own", "林妈妈", "孩子今天打卡完成")
        self.add_message("other", "周爸爸", "请帮忙看一下课程")
        self.db.commit()

        with patch("app.main.run_reply_agent_service", return_value=agent_result()):
            result = auto_draft_replies(AutoReplyDraftIn(), request=scoped_request(), db=self.db)

        self.assertEqual(result["created"], 1)
        self.assertEqual(result["outputs"][0]["family_id"], "own")
        self.assertEqual([item.family_id for item in self.db.query(AIOutput).all()], ["own"])


if __name__ == "__main__":
    unittest.main()
