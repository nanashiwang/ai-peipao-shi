import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import WecomArchiveSyncIn, sync_wecom_archive
from app.models import AIOutput, RawMessage, WecomArchiveState
from app.services.wecom_archive import WecomArchiveConfig, config_status, normalize_archive_message, ArchiveEnvelope


def archive_config() -> WecomArchiveConfig:
    return WecomArchiveConfig(
        enabled=True,
        corp_id="corp-test",
        secret="secret",
        private_key="private-key",
        private_key_path="",
        sdk_path="",
        self_userids={"coach-a"},
        conversation_map={
            "parent-a|coach-a": {"target_name": "许宝月", "family_id": "WECOM_许宝月"},
            "room-1": {"target_name": "一合学社", "family_id": "WECOM_一合学社"},
        },
        user_map={"parent-a": "许宝月", "coach-a": "我"},
    )


def agent_result(text: str = "收到，我来跟进。") -> dict:
    return {
        "raw": {
            "agent": "ai_reply_agent",
            "推荐回复": text,
            "风险等级": "低",
            "是否建议人工介入": True,
            "推荐下一步动作": ["人工审核"],
            "使用依据摘要": ["企业微信存档消息"],
        },
        "display_text": text,
        "risk_level": "低",
        "need_human_review": True,
        "suggested_actions": ["人工审核"],
    }


class WecomArchiveTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def test_config_status_reports_missing_credentials(self):
        status = config_status(WecomArchiveConfig(False, "", "", "", "", ""))
        self.assertFalse(status["enabled"])

        status = config_status(WecomArchiveConfig(True, "", "", "", "", ""))
        self.assertFalse(status["configured"])
        self.assertIn("WECOM_ARCHIVE_CORP_ID", status["missing"])

    def test_normalizes_private_archive_text_message(self):
        cfg = archive_config()
        item = normalize_archive_message(
            ArchiveEnvelope(
                seq=7,
                msgid="msg-1",
                raw={},
                decrypted={
                    "msgid": "msg-1",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850000000,
                    "msgtype": "text",
                    "text": {"content": "老师在吗"},
                },
            ),
            cfg,
        )

        self.assertIsNotNone(item)
        self.assertEqual(item.target_name, "许宝月")
        self.assertEqual(item.family_id, "WECOM_许宝月")
        self.assertEqual(item.speaker, "许宝月")
        self.assertEqual(item.external_id, "wecom_archive:msg-1")
        self.assertTrue(item.latest_inbound)

    def test_sync_imports_archive_message_and_creates_reply_once(self):
        payload = WecomArchiveSyncIn(
            auto_generate_reply=True,
            messages=[
                {
                    "seq": 101,
                    "msgid": "msg-101",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850000000,
                    "msgtype": "text",
                    "text": {"content": "今天孩子作业怎么安排？"},
                }
            ],
        )
        request = SimpleNamespace(headers={}, state=SimpleNamespace())

        with (
            patch("app.main.read_wecom_archive_config", return_value=archive_config()),
            patch("app.main.read_reply_agent_config", return_value={"auto_reply_enabled": True, "auto_create_send_task": False, "send_mode": "dry_run", "tone": "standard", "reply_agent": "ai_reply_agent", "enabled_agents": ["reply_agent"], "high_risk_policy": "manual", "skip_recent_hours": 24, "max_batch": 200}),
            patch("app.main.run_reply_agent_service", return_value=agent_result()) as agent,
        ):
            result = sync_wecom_archive(payload, request=request, db=self.db)
            duplicate = sync_wecom_archive(payload, request=request, db=self.db)

        self.assertEqual(result["normalized"], 1)
        self.assertEqual(result["results"][0]["messages_inserted"], 1)
        self.assertEqual(duplicate["results"][0]["messages_inserted"], 0)
        self.assertEqual(self.db.query(RawMessage).count(), 1)
        self.assertEqual(self.db.query(AIOutput).count(), 1)
        raw = self.db.query(RawMessage).one()
        self.assertEqual(raw.external_id, "wecom_archive:msg-101")
        self.assertEqual(raw.source, "企业微信存档:text")
        output = self.db.query(AIOutput).one()
        self.assertEqual(output.source, "企业微信存档：许宝月")
        self.assertEqual(self.db.query(WecomArchiveState).one().seq, 101)
        self.assertEqual(agent.call_count, 1)

    def test_self_archive_message_does_not_create_reply(self):
        payload = WecomArchiveSyncIn(
            auto_generate_reply=True,
            messages=[
                {
                    "seq": 102,
                    "msgid": "msg-102",
                    "from": "coach-a",
                    "tolist": ["parent-a"],
                    "msgtime": 1782850000000,
                    "msgtype": "text",
                    "text": {"content": "这条是我发出的消息"},
                }
            ],
        )

        with (
            patch("app.main.read_wecom_archive_config", return_value=archive_config()),
            patch("app.main.run_reply_agent_service", return_value=agent_result()) as agent,
        ):
            result = sync_wecom_archive(payload, request=None, db=self.db)

        self.assertEqual(result["results"][0]["messages_inserted"], 1)
        self.assertEqual(self.db.query(AIOutput).count(), 0)
        self.assertEqual(self.db.query(RawMessage).one().speaker, "我")
        agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
