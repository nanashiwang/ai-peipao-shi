import importlib.util
import unittest
from unittest.mock import patch

RPA_DEPS = ["pyperclip", "win32api", "win32con", "win32gui", "win32process", "pywinauto"]
MISSING_RPA_DEPS = [name for name in RPA_DEPS if importlib.util.find_spec(name) is None]
if MISSING_RPA_DEPS:
    wecom_sender = None
else:
    from rpa import wecom_sender


@unittest.skipIf(wecom_sender is None, f"RPA dependencies missing: {', '.join(MISSING_RPA_DEPS)}")
class WecomSenderPostSendSyncTest(unittest.TestCase):
    def test_post_send_verification_syncs_readback_without_creating_reply(self):
        calls = []

        def fake_sync(target_name, family_id, messages, config, latest_message=""):
            calls.append(
                {
                    "target_name": target_name,
                    "family_id": family_id,
                    "messages": messages,
                    "config": config,
                    "latest_message": latest_message,
                }
            )
            return {"conversation_check": {"status": "ok"}}

        config = {
            "api_base_url": "http://example.invalid",
            "_current_family_id": "WECOM_yihe",
            "auto_generate_ai_reply": True,
            "auto_create_reply_task": True,
            "auto_generate_all_agents": True,
        }
        messages = [
            {"speaker": "\u6211", "content": "\u6d4b\u8bd5\u53d1\u9001", "source": "\u4f01\u4e1a\u5fae\u4fe1RPA"},
            {"speaker": "\u5bb6\u957f", "content": "\u5df2\u6536\u5230", "source": "\u4f01\u4e1a\u5fae\u4fe1RPA"},
        ]

        with patch.object(wecom_sender, "sync_conversation_to_api", side_effect=fake_sync):
            result = wecom_sender.sync_post_send_verification_messages("\u4e00\u5408\u5b66\u793e", messages, config)

        self.assertEqual(result, {"conversation_check": {"status": "ok"}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target_name"], "\u4e00\u5408\u5b66\u793e")
        self.assertEqual(calls[0]["family_id"], "WECOM_yihe")
        self.assertEqual(calls[0]["latest_message"], "\u5df2\u6536\u5230")
        self.assertFalse(calls[0]["config"]["auto_generate_ai_reply"])
        self.assertFalse(calls[0]["config"]["auto_create_reply_task"])
        self.assertFalse(calls[0]["config"]["auto_generate_all_agents"])
        self.assertIn("proof_status=ok", " / ".join(config["_send_trace"]))

    def test_post_send_verification_sync_can_be_disabled(self):
        config = {"post_send_verify_sync_conversation": False}

        with patch.object(wecom_sender, "sync_conversation_to_api") as sync:
            result = wecom_sender.sync_post_send_verification_messages("\u4e00\u5408\u5b66\u793e", [{"content": "x"}], config)

        self.assertIsNone(result)
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()
