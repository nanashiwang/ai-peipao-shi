import base64
import struct
import unittest
from datetime import datetime
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import (
    RpaConversationIn,
    RpaMessageIn,
    WecomKfSyncIn,
    dispatch_wecom_kf_tasks,
    sync_conversation_payload,
    sync_wecom_kf_channel,
    verify_wecom_kf_callback,
)
from app.models import CustomerChannelBinding, Family, RawMessage, SendLog, SendTask
from app.services.agent_config_service import list_agent_configs, update_agent_config
from app.services.agent_service import build_agent_context, run_reply_agent_service
from app.services.wecom_kf import (
    WecomKfConfig,
    callback_signature,
    callback_config_status,
    config_status,
    decrypt_callback_request,
    normalized_inbound_message,
    verify_callback_echo,
)


def kf_config() -> WecomKfConfig:
    return WecomKfConfig(
        enabled=True,
        corp_id="ww-test-corp",
        secret="kf-secret",
        token="callback-token",
        encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        default_open_kfid="wk-test",
    )


def encrypt_callback_value(message: str, config: WecomKfConfig) -> str:
    key = base64.b64decode(f"{config.encoding_aes_key}=")
    plain = b"0123456789abcdef" + struct.pack(">I", len(message.encode("utf-8"))) + message.encode("utf-8") + config.corp_id.encode("utf-8")
    padder = padding.PKCS7(256).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def agent_result(text: str = "收到，我来帮您跟进。") -> dict:
    return {
        "raw": {
            "agent": "ai_reply_agent",
            "推荐回复": text,
            "风险等级": "低",
            "是否建议人工介入": False,
            "是否可加入发送任务": True,
        },
        "display_text": text,
        "risk_level": "低",
        "need_human_review": False,
        "suggested_actions": ["发送"],
    }


class WecomKfServiceTest(unittest.TestCase):
    def test_config_status_requires_callback_credentials(self):
        status = config_status(WecomKfConfig(True, "corp", "", "", ""))
        self.assertFalse(status["configured"])
        self.assertIn("WECOM_KF_SECRET", status["missing"])

    def test_callback_can_be_verified_before_secret_is_available(self):
        config = WecomKfConfig(
            enabled=True,
            corp_id="ww-test-corp",
            secret="",
            token="callback-token",
            encoding_aes_key="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        )
        callback_status = callback_config_status(config)
        full_status = config_status(config)
        timestamp = "1783820000"
        nonce = "nonce-1"
        encrypted = encrypt_callback_value("callback-ok", config)
        signature = callback_signature(config.token, timestamp, nonce, encrypted)

        self.assertTrue(callback_status["callback_configured"])
        self.assertFalse(full_status["configured"])
        self.assertIn("WECOM_KF_SECRET", full_status["missing"])
        with patch("app.main.read_wecom_kf_config", return_value=config):
            response = verify_wecom_kf_callback(signature, timestamp, nonce, encrypted)
        self.assertEqual(response.body.decode("utf-8"), "callback-ok")

    def test_callback_signature_and_aes_round_trip(self):
        config = kf_config()
        timestamp = "1783820000"
        nonce = "nonce-1"
        event_xml = (
            "<xml><ToUserName><![CDATA[ww-test-corp]]></ToUserName>"
            "<MsgType><![CDATA[event]]></MsgType><Event><![CDATA[kf_msg_or_event]]></Event>"
            "<Token><![CDATA[event-token]]></Token><OpenKfId><![CDATA[wk-test]]></OpenKfId></xml>"
        )
        encrypted = encrypt_callback_value(event_xml, config)
        signature = callback_signature(config.token, timestamp, nonce, encrypted)
        body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"

        self.assertEqual(
            decrypt_callback_request(
                body,
                signature=signature,
                timestamp=timestamp,
                nonce=nonce,
                config=config,
            ),
            event_xml,
        )
        self.assertEqual(
            verify_callback_echo(
                encrypted,
                signature=signature,
                timestamp=timestamp,
                nonce=nonce,
                config=config,
            ),
            event_xml,
        )

    def test_normalizes_text_and_does_not_auto_reply_to_media(self):
        base = {
            "msgid": "msg-1",
            "open_kfid": "wk-test",
            "external_userid": "wm-parent",
            "send_time": 1783820000,
            "origin": 3,
        }
        text = normalized_inbound_message({**base, "msgtype": "text", "text": {"content": "老师在吗"}}, {"nickname": "林妈妈"})
        image = normalized_inbound_message({**base, "msgid": "msg-2", "msgtype": "image", "image": {"media_id": "m1"}})

        self.assertEqual(text["target_name"], "林妈妈")
        self.assertTrue(text["auto_reply"])
        self.assertEqual(image["content"], "[图片]")
        self.assertFalse(image["auto_reply"])


class WecomKfIntegrationTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="WECOM_KF_family", parent_nickname="林妈妈"))
        self.db.add(
            CustomerChannelBinding(
                family_id="WECOM_KF_family",
                channel="wecom_kf",
                account_id="wk-test",
                external_userid="wm-parent",
                display_name="林妈妈",
                last_inbound_msgid="msg-1",
                last_inbound_at=datetime.utcnow(),
                reply_window_started_at=datetime.utcnow(),
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_wecom_kf_uses_shared_prompt_and_knowledge(self):
        self.db.add(
            RawMessage(
                family_id="WECOM_KF_family",
                speaker="林妈妈",
                content="孩子今天想请假",
                source="微信客服:text",
            )
        )
        self.db.commit()
        list_agent_configs(self.db)
        self.db.commit()
        update_agent_config(
            self.db,
            "ai_reply_agent",
            name="统一回复 Agent",
            system_prompt="这是管理后台保存的统一回复 Prompt。只输出JSON。",
            enabled=True,
            retrieval_enabled=True,
            retrieval_top_k=4,
        )
        captured = {}

        def fake_call(system_prompt, payload):
            captured["prompt"] = system_prompt
            captured["payload"] = payload
            return {
                "推荐回复": "收到，今天先休息，我们明天再把节奏接上。",
                "风险等级": "低",
                "是否建议人工介入": False,
                "是否可加入发送任务": True,
            }

        context = build_agent_context(
            self.db,
            "WECOM_KF_family",
            {"type": "wecom_kf", "label": "微信客服", "account_id": "wk-test", "customer_id": "wm-parent"},
        )
        with (
            patch("app.services.agent_config_service.call_ark_embedding", side_effect=RuntimeError("local")),
            patch("app.services.agent_service.call_ark_json", side_effect=fake_call),
        ):
            result = run_reply_agent_service(context, "孩子今天想请假")

        self.assertIn("管理后台保存的统一回复 Prompt", captured["prompt"])
        self.assertIn("当前渠道约束", captured["prompt"])
        self.assertIn("微信客服", captured["prompt"])
        self.assertEqual(captured["payload"]["channel"]["type"], "wecom_kf")
        self.assertTrue(captured["payload"]["retrieved_knowledge"])
        self.assertTrue(any("请假补打卡" in item["title"] for item in captured["payload"]["retrieved_knowledge"]))
        self.assertIn("今天先休息", result["display_text"])

    def test_auto_reply_creates_wecom_kf_task_without_device(self):
        payload = RpaConversationIn(
            target_name="林妈妈",
            family_id="WECOM_KF_family",
            messages=[
                RpaMessageIn(
                    speaker="林妈妈",
                    content="老师在吗",
                    message_time=datetime.utcnow().isoformat(),
                    source="微信客服:text",
                    external_id="wecom_kf:msg-1",
                )
            ],
            auto_generate_reply=True,
            channel="wecom_kf",
            channel_target_id="wm-parent",
            channel_account_id="wk-test",
            source_message_id="msg-1",
        )
        reply_config = {
            "auto_reply_enabled": True,
            "auto_create_send_task": True,
            "send_mode": "real_send",
            "tone": "standard",
            "reply_agent": "ai_reply_agent",
            "enabled_agents": ["reply_agent", "safety_agent"],
            "high_risk_policy": "manual",
            "skip_recent_hours": 0,
            "max_batch": 200,
        }
        with (
            patch("app.main.read_reply_agent_config", return_value=reply_config),
            patch("app.main.run_reply_agent_service", return_value=agent_result()),
            patch("app.main.read_wecom_kf_config", return_value=kf_config()),
            patch("app.main.wecom_kf_config_status", return_value={"configured": True, "missing": []}),
        ):
            result = sync_conversation_payload(self.db, payload, actor="微信客服API", source_prefix="微信客服")

        task = self.db.query(SendTask).one()
        self.assertEqual(result["send_task"]["id"], task.id)
        self.assertEqual(task.channel, "wecom_kf")
        self.assertEqual(task.channel_target_id, "wm-parent")
        self.assertEqual(task.channel_account_id, "wk-test")
        self.assertEqual(task.device_id, "")

    def test_dispatch_sends_and_updates_reply_window(self):
        task = SendTask(
            family_id="WECOM_KF_family",
            target_name="林妈妈",
            scene="微信客服回复",
            content="收到，我来跟进。",
            channel="wecom_kf",
            channel_target_id="wm-parent",
            channel_account_id="wk-test",
            source_message_id="msg-1",
            send_mode="real_send",
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        with (
            patch("app.main.read_wecom_kf_config", return_value=kf_config()),
            patch("app.main.wecom_kf_config_status", return_value={"configured": True, "missing": []}),
            patch("app.main.send_wecom_kf_text", return_value={"errcode": 0, "msgid": "kf-api-msg-1"}) as sender,
        ):
            result = dispatch_wecom_kf_tasks(self.db)

        self.db.refresh(task)
        binding = self.db.query(CustomerChannelBinding).one()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(task.status, "sent")
        self.assertEqual(binding.reply_count, 1)
        self.assertEqual(self.db.query(SendLog).one().channel, "wecom_kf")
        self.assertEqual(self.db.query(RawMessage).filter(RawMessage.speaker == "我").one().content, task.content)
        sender.assert_called_once()

    def test_dispatch_blocks_sixth_reply_until_customer_speaks_again(self):
        binding = self.db.query(CustomerChannelBinding).one()
        binding.reply_count = 5
        task = SendTask(
            family_id="WECOM_KF_family",
            target_name="林妈妈",
            scene="微信客服回复",
            content="第六条回复",
            channel="wecom_kf",
            channel_target_id="wm-parent",
            channel_account_id="wk-test",
            send_mode="real_send",
            status="pending",
        )
        self.db.add(task)
        self.db.commit()
        with (
            patch("app.main.read_wecom_kf_config", return_value=kf_config()),
            patch("app.main.wecom_kf_config_status", return_value={"configured": True, "missing": []}),
            patch("app.main.send_wecom_kf_text") as sender,
        ):
            result = dispatch_wecom_kf_tasks(self.db)

        self.db.refresh(task)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(task.status, "failed")
        self.assertIn("已回复5条", task.last_error)
        sender.assert_not_called()

    def test_duplicate_inbound_message_does_not_reset_reply_quota(self):
        binding = self.db.query(CustomerChannelBinding).one()
        binding.reply_count = 4
        self.db.add(
            RawMessage(
                family_id="WECOM_KF_family",
                speaker="林妈妈",
                content="老师在吗",
                source="微信客服:text",
                external_id="wecom_kf:msg-duplicate",
            )
        )
        self.db.commit()
        raw = {
            "msgid": "msg-duplicate",
            "open_kfid": "wk-test",
            "external_userid": "wm-parent",
            "send_time": 1783820000,
            "origin": 3,
            "msgtype": "text",
            "text": {"content": "老师在吗"},
        }
        with (
            patch("app.main.read_wecom_kf_config", return_value=kf_config()),
            patch("app.main.wecom_kf_config_status", return_value={"configured": True, "missing": []}),
            patch("app.main.sync_wecom_kf_messages", return_value={"msg_list": [raw], "next_cursor": "cursor-2", "has_more": 0}),
            patch("app.main.batch_get_customers", return_value={"wm-parent": {"nickname": "林妈妈"}}),
        ):
            result = sync_wecom_kf_channel(WecomKfSyncIn(open_kfid="wk-test", auto_generate_reply=False), self.db)

        self.db.refresh(binding)
        self.assertEqual(result["normalized"], 0)
        self.assertEqual(binding.reply_count, 4)


if __name__ == "__main__":
    unittest.main()
