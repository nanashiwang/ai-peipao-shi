import base64
import struct
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import dispatch_wecom_customer_tasks, sync_wecom_customer_bindings
from app.models import CustomerChannelBinding, Family, SendLog, SendTask
from app.services.wecom_customer import (
    WecomCustomerApiError,
    WecomCustomerConfig,
    callback_config_status,
    config_status,
    decrypt_callback_request,
    parse_customer_event,
    verify_callback_echo,
)
from app.services.wecom_kf import callback_signature


def customer_config(**overrides) -> WecomCustomerConfig:
    values = {
        "enabled": True,
        "corp_id": "ww-test-corp",
        "secret": "customer-secret",
        "token": "callback-token",
        "encoding_aes_key": "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
    }
    values.update(overrides)
    return WecomCustomerConfig(**values)


def encrypt_callback_value(message: str, config: WecomCustomerConfig) -> str:
    key = base64.b64decode(f"{config.encoding_aes_key}=")
    content = message.encode("utf-8")
    plain = b"0123456789abcdef" + struct.pack(">I", len(content)) + content + config.corp_id.encode("utf-8")
    padder = padding.PKCS7(256).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


class WecomCustomerServiceTest(unittest.TestCase):
    def test_config_distinguishes_callback_and_full_api(self):
        cfg = customer_config(secret="")
        self.assertTrue(callback_config_status(cfg)["callback_configured"])
        status = config_status(cfg)
        self.assertFalse(status["configured"])
        self.assertIn("WECOM_CUSTOMER_SECRET", status["missing"])

    def test_callback_signature_aes_and_event_parse(self):
        cfg = customer_config()
        timestamp = "1783820000"
        nonce = "nonce-1"
        event_xml = (
            "<xml><ToUserName><![CDATA[ww-test-corp]]></ToUserName>"
            "<MsgType><![CDATA[event]]></MsgType>"
            "<Event><![CDATA[change_external_contact]]></Event>"
            "<ChangeType><![CDATA[add_external_contact]]></ChangeType>"
            "<UserID><![CDATA[coach-a]]></UserID>"
            "<ExternalUserID><![CDATA[wm-parent]]></ExternalUserID>"
            "<WelcomeCode><![CDATA[welcome-once]]></WelcomeCode></xml>"
        )
        encrypted = encrypt_callback_value(event_xml, cfg)
        signature = callback_signature(cfg.token, timestamp, nonce, encrypted)
        body = f"<xml><Encrypt><![CDATA[{encrypted}]]></Encrypt></xml>"

        self.assertEqual(
            decrypt_callback_request(body, signature=signature, timestamp=timestamp, nonce=nonce, config=cfg),
            event_xml,
        )
        self.assertEqual(
            verify_callback_echo(encrypted, signature=signature, timestamp=timestamp, nonce=nonce, config=cfg),
            event_xml,
        )
        event = parse_customer_event(event_xml)
        self.assertEqual(event["change_type"], "add_external_contact")
        self.assertEqual(event["userid"], "coach-a")
        self.assertEqual(event["external_userid"], "wm-parent")
        self.assertEqual(event["welcome_code"], "welcome-once")


class WecomCustomerIntegrationTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def add_binding(self) -> CustomerChannelBinding:
        family = Family(family_id="customer-family", parent_nickname="林妈妈")
        binding = CustomerChannelBinding(
            family_id=family.family_id,
            channel="wecom_customer",
            account_id="coach-a",
            external_userid="wm-parent",
            display_name="林妈妈",
        )
        self.db.add_all([family, binding])
        self.db.commit()
        return binding

    def add_task(self, scheduled_at: datetime | None = None, max_retries: int = 2) -> SendTask:
        self.add_binding()
        task = SendTask(
            family_id="customer-family",
            target_name="林妈妈",
            scene="客户联系定时提醒",
            content="今晚八点完成复盘任务。",
            channel="wecom_customer",
            channel_target_id="wm-parent",
            channel_account_id="coach-a",
            send_mode="real_send",
            status="pending",
            scheduled_at=scheduled_at or datetime.utcnow() - timedelta(minutes=1),
            max_retries=max_retries,
        )
        self.db.add(task)
        self.db.commit()
        return task

    def test_customer_sync_creates_stable_family_and_binding(self):
        payload = {
            "members": ["coach-a"],
            "contacts": [
                {
                    "external_contact": {"external_userid": "wm-parent", "name": "林妈妈"},
                    "follow_user": [{"userid": "coach-a", "remark": "林妈妈", "state": "course-a", "add_way": 1}],
                }
            ],
        }
        with (
            patch("app.main.read_wecom_customer_config", return_value=customer_config()),
            patch("app.main.wecom_customer_config_status", return_value={"configured": True, "missing": []}),
            patch("app.main.sync_customer_contacts", return_value=payload),
        ):
            result = sync_wecom_customer_bindings(self.db)

        self.assertEqual(result["customers"], 1)
        binding = self.db.query(CustomerChannelBinding).one()
        self.assertEqual(binding.account_id, "coach-a")
        self.assertEqual(binding.external_userid, "wm-parent")
        self.assertEqual(self.db.query(Family).one().family_id, binding.family_id)

    def test_due_task_creates_group_message_and_waits_for_member_confirmation(self):
        task = self.add_task()
        with (
            patch("app.main.read_wecom_customer_config", return_value=customer_config()),
            patch("app.main.wecom_customer_config_status", return_value={"configured": True, "missing": []}),
            patch("app.main.create_wecom_customer_group_message", return_value={"msgid": "group-msg-1", "fail_list": []}),
        ):
            result = dispatch_wecom_customer_tasks(self.db)

        self.db.refresh(task)
        self.assertEqual(result["created"], 1)
        self.assertEqual(task.status, "pending_confirmation")
        self.assertIn("等待对应成员", task.last_error)
        log = self.db.query(SendLog).one()
        self.assertEqual(log.status, "pending_confirmation")
        self.assertIn("需成员确认", log.verify_detail)

    def test_future_task_is_not_dispatched(self):
        task = self.add_task(datetime.utcnow() + timedelta(hours=1))
        with (
            patch("app.main.read_wecom_customer_config", return_value=customer_config()),
            patch("app.main.wecom_customer_config_status", return_value={"configured": True, "missing": []}),
            patch("app.main.create_wecom_customer_group_message") as sender,
        ):
            result = dispatch_wecom_customer_tasks(self.db)

        self.db.refresh(task)
        self.assertEqual(result["created"], 0)
        self.assertEqual(task.status, "pending")
        sender.assert_not_called()

    def test_api_failure_is_retried_without_duplicate_success_log(self):
        task = self.add_task()
        with (
            patch("app.main.read_wecom_customer_config", return_value=customer_config()),
            patch("app.main.wecom_customer_config_status", return_value={"configured": True, "missing": []}),
            patch("app.main.create_wecom_customer_group_message", side_effect=WecomCustomerApiError("temporary")),
        ):
            result = dispatch_wecom_customer_tasks(self.db)

        self.db.refresh(task)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.retry_count, 1)
        self.assertIsNotNone(task.next_retry_at)
        self.assertEqual(self.db.query(SendLog).count(), 0)


if __name__ == "__main__":
    unittest.main()
