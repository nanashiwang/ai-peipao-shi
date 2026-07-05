import json
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import ConversationDirectSendIn, direct_send_conversation_message
from app.models import AIOutput, Device, DeviceConversationCheck, Family, RawMessage, SendTask


def reply_result(text: str = "收到，我会按今天的安排继续跟进。") -> dict:
    return {
        "raw": {
            "agent": "quick_reply_agent",
            "场景类型": "普通咨询",
            "风险等级": "低",
            "推荐回复": text,
            "推荐下一步动作": ["记录本次跟进"],
            "是否建议人工介入": False,
        },
        "display_text": text,
        "risk_level": "低",
        "need_human_review": False,
        "suggested_actions": ["记录本次跟进"],
    }


class ConversationDirectSendTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        now = datetime.utcnow()
        self.db.add(Family(family_id="family-1", parent_nickname="许宝月", coach_name="王坤"))
        self.db.add(
            RawMessage(
                family_id="family-1",
                speaker="许宝月",
                content="老师，今天的安排怎么做？",
                message_time=now,
                source="测试",
            )
        )
        self.db.add(
            Device(
                device_id="win1",
                name="王坤电脑",
                token="token",
                conversations=json.dumps(["许宝月"], ensure_ascii=False),
                status="online",
                wecom_ok="Y",
                allow_real_send=True,
                last_heartbeat=now,
            )
        )
        self.db.add(
            DeviceConversationCheck(
                device_id="win1",
                target_name="许宝月",
                status="ok",
                message_count=1,
                source="测试",
                verified_at=now,
                updated_at=now,
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_smart_reply_direct_send_creates_ai_output_and_real_task(self):
        with (
            patch(
                "app.main.read_reply_agent_config",
                return_value={"reply_agent": "quick_reply_agent", "tone": "standard"},
            ),
            patch("app.main.run_quick_reply_agent_service", return_value=reply_result()) as agent,
        ):
            result = direct_send_conversation_message(
                "family-1",
                ConversationDirectSendIn(smart_reply=True),
                request=None,
                db=self.db,
            )

        agent.assert_called_once()
        self.assertEqual(result["device_id"], "win1")
        self.assertEqual(result["target_name"], "许宝月")
        self.assertEqual(result["ai_output"]["status"], "task_created")

        task = self.db.query(SendTask).one()
        self.assertEqual(task.family_id, "family-1")
        self.assertEqual(task.target_name, "许宝月")
        self.assertEqual(task.content, "收到，我会按今天的安排继续跟进。")
        self.assertEqual(task.send_mode, "real_send")
        self.assertEqual(task.status, "pending")

        output = self.db.query(AIOutput).one()
        self.assertEqual(output.source, "会话工作台智能回复")
        self.assertEqual(output.status, "task_created")


if __name__ == "__main__":
    unittest.main()
