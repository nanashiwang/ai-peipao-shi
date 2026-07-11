import hashlib
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import RpaConversationIn, RpaMessageIn, WecomArchiveSyncIn, build_wecom_archive_poll_payload, sync_conversation_payload, sync_wecom_archive
from app.models import AIOutput, CustomerChannelBinding, Device, Family, RawMessage, SendLog, SendTask, WecomArchiveState
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
            "coach-a|parent-a": {"target_name": "许宝月", "family_id": "WECOM_许宝月"},
            "room-1": {"target_name": "一合学社", "family_id": "WECOM_一合学社"},
        },
        user_map={"parent-a": "许宝月", "coach-a": "我"},
        auto_resolve_names=False,
    )


def agent_result(text: str = "收到，我来跟进。", need_human_review: bool = True) -> dict:
    return {
        "raw": {
            "agent": "ai_reply_agent",
            "推荐回复": text,
            "风险等级": "低",
            "是否建议人工介入": need_human_review,
            "推荐下一步动作": ["人工审核"] if need_human_review else ["加入发送任务"],
            "使用依据摘要": ["企业微信存档消息"],
        },
        "display_text": text,
        "risk_level": "低",
        "need_human_review": need_human_review,
        "suggested_actions": ["人工审核"] if need_human_review else ["加入发送任务"],
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

    def test_archive_poller_uses_frontend_reply_config(self):
        with patch(
            "app.main.read_reply_agent_config",
            return_value={
                "auto_reply_enabled": True,
                "auto_create_send_task": True,
                "send_mode": "real_send",
            },
        ):
            payload = build_wecom_archive_poll_payload()

        self.assertTrue(payload.auto_generate_reply)
        self.assertTrue(payload.auto_create_reply_task)

    def test_sync_normalizes_stale_private_chat_family_and_self_speaker(self):
        self.db.add(Family(family_id="WECOM_DM_old", parent_nickname="许宝月 / nanashi"))
        self.db.add(
            RawMessage(
                family_id="WECOM_DM_old",
                speaker="nanashi",
                content="之前我发的消息",
                source="企业微信存档:text",
            )
        )
        self.db.commit()
        payload = RpaConversationIn(
            target_name="许宝月",
            family_id="WECOM_DM_old",
            messages=[
                RpaMessageIn(
                    speaker="许宝月",
                    content="你好",
                    message_time="2026-07-06T03:24:46",
                    source="企业微信存档:text",
                    external_id="wecom_archive:new",
                )
            ],
            auto_generate_reply=False,
        )

        cfg = archive_config()
        cfg.self_userids = {"nanashi"}
        cfg.user_map = {"nanashi": "我", "XuBaoYue": "许宝月"}
        with patch("app.main.read_wecom_archive_config", return_value=cfg):
            sync_conversation_payload(self.db, payload, source_prefix="企业微信存档")

        family = self.db.query(Family).filter_by(family_id="WECOM_DM_old").one()
        self.assertEqual(family.parent_nickname, "许宝月")
        speakers = [item.speaker for item in self.db.query(RawMessage).order_by(RawMessage.id).all()]
        self.assertEqual(speakers, ["我", "许宝月"])

    def test_sync_uses_stable_family_id_when_target_name_is_duplicated(self):
        self.db.add(Family(family_id="WECOM_许宝月", parent_nickname="许宝月"))
        self.db.add(Family(family_id="WECOM_DM_old_xu", parent_nickname="许宝月"))
        self.db.commit()
        payload = RpaConversationIn(
            target_name="许宝月",
            family_id="WECOM_DM_new_xu",
            messages=[
                RpaMessageIn(
                    speaker="许宝月",
                    content="我报了个课",
                    message_time="2026-07-11T23:14:03",
                    source="企业微信存档:text",
                    external_id="wecom_archive:new-xu",
                )
            ],
            auto_generate_reply=False,
        )

        result = sync_conversation_payload(self.db, payload, source_prefix="企业微信存档")

        self.assertEqual(result["family_id"], "WECOM_DM_new_xu")
        self.assertEqual(result["messages_inserted"], 1)
        family = self.db.query(Family).filter(Family.family_id == "WECOM_DM_new_xu").one()
        self.assertEqual(family.parent_nickname, "许宝月")
        message = self.db.query(RawMessage).filter(RawMessage.external_id == "wecom_archive:new-xu").one()
        self.assertEqual(message.family_id, "WECOM_DM_new_xu")

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
        self.assertEqual(item.external_userid, "parent-a")

    def test_archive_private_chat_reuses_customer_contact_family(self):
        self.db.add(Family(family_id="customer-family", parent_nickname="林妈妈"))
        self.db.add(
            CustomerChannelBinding(
                family_id="customer-family",
                channel="wecom_customer",
                account_id="coach-a",
                external_userid="parent-a",
                display_name="林妈妈",
            )
        )
        self.db.commit()
        payload = WecomArchiveSyncIn(
            messages=[
                {
                    "seq": 1,
                    "msgid": "customer-msg-1",
                    "decrypted": {
                        "msgid": "customer-msg-1",
                        "from": "parent-a",
                        "tolist": ["coach-a"],
                        "msgtime": 1782850000000,
                        "msgtype": "text",
                        "text": {"content": "老师，今天的任务是什么？"},
                    },
                }
            ],
            auto_generate_reply=False,
        )

        with patch("app.main.read_wecom_archive_config", return_value=archive_config()):
            result = sync_wecom_archive(payload, db=self.db)

        self.assertEqual(result["results"][0]["family_id"], "customer-family")
        message = self.db.query(RawMessage).filter_by(external_id="wecom_archive:customer-msg-1").one()
        self.assertEqual(message.family_id, "customer-family")

    def test_auto_resolves_group_archive_name_without_manual_mapping(self):
        cfg = WecomArchiveConfig(
            enabled=True,
            corp_id="corp-test",
            secret="secret",
            private_key="private-key",
            private_key_path="",
            sdk_path="",
            self_userids={"coach-a"},
            conversation_map={},
            user_map={"parent-a": "许宝月", "coach-a": "我"},
        )

        with patch("app.services.wecom_archive._auto_resolve_room_name", return_value="一合学社") as resolver:
            item = normalize_archive_message(
                ArchiveEnvelope(
                    seq=8,
                    msgid="msg-room-1",
                    raw={},
                    decrypted={
                        "msgid": "msg-room-1",
                        "from": "parent-a",
                        "tolist": ["coach-a"],
                        "roomid": "room-1",
                        "msgtime": 1782850000000,
                        "msgtype": "text",
                        "text": {"content": "群里测试"},
                    },
                ),
                cfg,
            )

        self.assertIsNotNone(item)
        self.assertEqual(item.target_name, "一合学社")
        self.assertEqual(item.family_id, "WECOM_room-1")
        resolver.assert_called_once_with("room-1", cfg)

    def test_private_archive_uses_one_thread_for_both_directions(self):
        cfg = WecomArchiveConfig(
            enabled=True,
            corp_id="corp-test",
            secret="secret",
            private_key="private-key",
            private_key_path="",
            sdk_path="",
            self_userids={"coach-a"},
            conversation_map={},
            user_map={"parent-a": "许宝月", "coach-a": "我"},
            auto_resolve_names=False,
        )
        expected_family_id = f"WECOM_DM_{hashlib.sha1('coach-a|parent-a'.encode('utf-8')).hexdigest()[:16]}"

        inbound = normalize_archive_message(
            ArchiveEnvelope(
                seq=9,
                msgid="msg-private-in",
                raw={},
                decrypted={
                    "msgid": "msg-private-in",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850000000,
                    "msgtype": "text",
                    "text": {"content": "老师在吗"},
                },
            ),
            cfg,
        )
        outbound = normalize_archive_message(
            ArchiveEnvelope(
                seq=10,
                msgid="msg-private-out",
                raw={},
                decrypted={
                    "msgid": "msg-private-out",
                    "from": "coach-a",
                    "tolist": ["parent-a"],
                    "msgtime": 1782850001000,
                    "msgtype": "text",
                    "text": {"content": "我在，马上看。"},
                },
            ),
            cfg,
        )

        self.assertEqual(inbound.target_name, "许宝月")
        self.assertEqual(outbound.target_name, "许宝月")
        self.assertEqual(inbound.family_id, expected_family_id)
        self.assertEqual(outbound.family_id, expected_family_id)
        self.assertEqual(inbound.speaker, "许宝月")
        self.assertEqual(outbound.speaker, "我")
        self.assertTrue(inbound.latest_inbound)
        self.assertFalse(outbound.latest_inbound)

    def test_private_archive_without_self_userid_keeps_stable_participant_thread(self):
        cfg = WecomArchiveConfig(
            enabled=True,
            corp_id="corp-test",
            secret="secret",
            private_key="private-key",
            private_key_path="",
            sdk_path="",
            self_userids=set(),
            conversation_map={},
            user_map={"parent-a": "许宝月", "coach-a": "我"},
            auto_resolve_names=False,
        )

        inbound = normalize_archive_message(
            ArchiveEnvelope(
                seq=11,
                msgid="msg-private-no-self-in",
                raw={},
                decrypted={
                    "msgid": "msg-private-no-self-in",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850000000,
                    "msgtype": "text",
                    "text": {"content": "单聊测试"},
                },
            ),
            cfg,
        )
        outbound = normalize_archive_message(
            ArchiveEnvelope(
                seq=12,
                msgid="msg-private-no-self-out",
                raw={},
                decrypted={
                    "msgid": "msg-private-no-self-out",
                    "from": "coach-a",
                    "tolist": ["parent-a"],
                    "msgtime": 1782850001000,
                    "msgtype": "text",
                    "text": {"content": "收到。"},
                },
            ),
            cfg,
        )

        self.assertEqual(inbound.target_name, "我 / 许宝月")
        self.assertEqual(outbound.target_name, "我 / 许宝月")
        self.assertEqual(inbound.family_id, outbound.family_id)

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

    def test_empty_recent_reply_output_does_not_block_next_auto_reply(self):
        self.db.add(
            AIOutput(
                family_id="WECOM_许宝月",
                agent_type="ai_reply",
                status="approved",
                display_text="",
                edited_output="",
                created_at=datetime.utcnow(),
            )
        )
        self.db.commit()
        payload = WecomArchiveSyncIn(
            auto_generate_reply=True,
            messages=[
                {
                    "seq": 102,
                    "msgid": "msg-102",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850030000,
                    "msgtype": "text",
                    "text": {"content": "老师，刚才那条看到吗？"},
                }
            ],
        )

        with (
            patch("app.main.read_wecom_archive_config", return_value=archive_config()),
            patch(
                "app.main.read_reply_agent_config",
                return_value={
                    "auto_reply_enabled": True,
                    "auto_create_send_task": False,
                    "send_mode": "dry_run",
                    "tone": "standard",
                    "reply_agent": "ai_reply_agent",
                    "enabled_agents": ["reply_agent"],
                    "high_risk_policy": "manual",
                    "skip_recent_hours": 24,
                    "max_batch": 200,
                },
            ),
            patch("app.main.run_reply_agent_service", return_value=agent_result()) as agent,
        ):
            result = sync_wecom_archive(payload, request=None, db=self.db)

        agent.assert_called_once()
        self.assertEqual(result["results"][0]["auto_reply_note"], "")
        self.assertEqual(self.db.query(AIOutput).count(), 2)

    def test_auto_reply_real_send_allows_same_content_from_prior_sent_log(self):
        now = datetime.utcnow()
        self.db.add(
            Device(
                device_id="rpa-01",
                name="测试设备",
                token="token",
                conversations="[]",
                status="online",
                wecom_ok="Y",
                allow_real_send=True,
                allow_any_conversation=True,
                wecom_userid="coach-a",
                wecom_account_name="我",
                last_heartbeat=now,
            )
        )
        previous = SendTask(
            family_id="WECOM_许宝月",
            target_name="许宝月",
            scene="sent",
            content="收到，我来跟进。",
            send_mode="real_send",
            status="sent",
            device_id="rpa-01",
        )
        self.db.add(previous)
        self.db.flush()
        self.db.add(
            SendLog(
                task_id=previous.id,
                family_id=previous.family_id,
                target_name=previous.target_name,
                status="sent",
                send_mode="real_send",
                sent_at=now - timedelta(minutes=10),
            )
        )
        self.db.commit()
        payload = WecomArchiveSyncIn(
            auto_generate_reply=True,
            messages=[
                {
                    "seq": 103,
                    "msgid": "msg-103",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850060000,
                    "msgtype": "text",
                    "text": {"content": "老师在吗"},
                }
            ],
        )

        with (
            patch("app.main.AUTO_REPLY_DUPLICATE_WINDOW_SECONDS", 0),
            patch("app.main.read_wecom_archive_config", return_value=archive_config()),
            patch(
                "app.main.read_reply_agent_config",
                return_value={
                    "auto_reply_enabled": True,
                    "auto_create_send_task": True,
                    "send_mode": "real_send",
                    "tone": "standard",
                    "reply_agent": "ai_reply_agent",
                    "enabled_agents": ["reply_agent"],
                    "high_risk_policy": "manual",
                    "skip_recent_hours": 0,
                    "max_batch": 200,
                },
            ),
            patch("app.main.run_reply_agent_service", return_value=agent_result(need_human_review=False)),
        ):
            result = sync_wecom_archive(payload, request=None, db=self.db)

        self.assertIsNotNone(result["results"][0]["send_task"])
        task = self.db.query(SendTask).filter(SendTask.status == "pending").one()
        self.assertEqual(task.target_name, "许宝月")
        self.assertEqual(task.send_mode, "real_send")
        self.assertTrue(task.scene.startswith("企微自动回复/"))

    def test_auto_reply_real_send_queues_when_device_has_inflight_task(self):
        now = datetime.utcnow()
        self.db.add(
            Device(
                device_id="rpa-01",
                name="测试设备",
                token="token",
                conversations="[]",
                status="online",
                wecom_ok="Y",
                allow_real_send=True,
                allow_any_conversation=True,
                wecom_userid="coach-a",
                wecom_account_name="我",
                last_heartbeat=now,
            )
        )
        self.db.add(
            SendTask(
                family_id="WECOM_许宝月",
                target_name="许宝月",
                scene="上一条自动回复",
                content="上一条正在发送。",
                send_mode="real_send",
                status="assigned",
                device_id="rpa-01",
                scheduled_at=now,
            )
        )
        self.db.commit()
        payload = WecomArchiveSyncIn(
            auto_generate_reply=True,
            messages=[
                {
                    "seq": 105,
                    "msgid": "msg-105",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850180000,
                    "msgtype": "text",
                    "text": {"content": "老师，后面怎么沟通？"},
                }
            ],
        )

        with (
            patch("app.main.AUTO_REPLY_DUPLICATE_WINDOW_SECONDS", 0),
            patch("app.main.read_wecom_archive_config", return_value=archive_config()),
            patch(
                "app.main.read_reply_agent_config",
                return_value={
                    "auto_reply_enabled": True,
                    "auto_create_send_task": True,
                    "send_mode": "real_send",
                    "tone": "standard",
                    "reply_agent": "ai_reply_agent",
                    "enabled_agents": ["reply_agent"],
                    "high_risk_policy": "manual",
                    "skip_recent_hours": 0,
                    "max_batch": 200,
                },
            ),
            patch("app.main.run_reply_agent_service", return_value=agent_result("收到，我来跟进。", need_human_review=False)),
        ):
            result = sync_wecom_archive(payload, request=None, db=self.db)

        self.assertEqual(result["results"][0]["auto_reply_note"], "")
        self.assertIsNotNone(result["results"][0]["send_task"])
        pending = self.db.query(SendTask).filter(SendTask.status == "pending", SendTask.content == "收到，我来跟进。").one()
        self.assertEqual(pending.target_name, "许宝月")
        self.assertEqual(pending.device_id, "rpa-01")

    def test_auto_reply_real_send_skips_when_archive_account_unbound(self):
        now = datetime.utcnow()
        self.db.add(
            Device(
                device_id="rpa-other",
                name="其他设备",
                token="token",
                conversations="[]",
                status="online",
                wecom_ok="Y",
                allow_real_send=True,
                allow_any_conversation=True,
                wecom_userid="coach-b",
                last_heartbeat=now,
            )
        )
        self.db.commit()
        payload = WecomArchiveSyncIn(
            auto_generate_reply=True,
            messages=[
                {
                    "seq": 104,
                    "msgid": "msg-104",
                    "from": "parent-a",
                    "tolist": ["coach-a"],
                    "msgtime": 1782850120000,
                    "msgtype": "text",
                    "text": {"content": "老师在吗"},
                }
            ],
        )

        with (
            patch("app.main.read_wecom_archive_config", return_value=archive_config()),
            patch(
                "app.main.read_reply_agent_config",
                return_value={
                    "auto_reply_enabled": True,
                    "auto_create_send_task": True,
                    "send_mode": "real_send",
                    "tone": "standard",
                    "reply_agent": "ai_reply_agent",
                    "enabled_agents": ["reply_agent"],
                    "high_risk_policy": "manual",
                    "skip_recent_hours": 0,
                    "max_batch": 200,
                },
            ),
            patch("app.main.run_reply_agent_service", return_value=agent_result(need_human_review=False)) as agent,
        ):
            result = sync_wecom_archive(payload, request=None, db=self.db)

        note = result["results"][0]["auto_reply_note"]
        self.assertIn("没有绑定被控端", note)
        self.assertIsNone(result["results"][0]["send_task"])
        self.assertEqual(self.db.query(SendTask).count(), 0)
        self.assertEqual(self.db.query(AIOutput).count(), 0)
        agent.assert_not_called()

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
            patch("app.main.read_reply_agent_config", return_value={"auto_reply_enabled": True, "auto_create_send_task": False, "send_mode": "dry_run", "tone": "standard", "reply_agent": "ai_reply_agent", "enabled_agents": ["reply_agent"], "high_risk_policy": "manual", "skip_recent_hours": 0, "max_batch": 200}),
            patch("app.main.run_reply_agent_service", return_value=agent_result()) as agent,
        ):
            result = sync_wecom_archive(payload, request=None, db=self.db)

        self.assertEqual(result["results"][0]["messages_inserted"], 1)
        self.assertEqual(self.db.query(AIOutput).count(), 0)
        self.assertEqual(self.db.query(RawMessage).one().speaker, "我")
        agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
