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

    def test_real_send_blocks_hotkey_when_baseline_read_fails(self):
        config = {
            "dry_run": False,
            "server_allow_real_send": True,
            "verify_sent_message_enabled": True,
            "post_send_verify_compare_before_after": True,
            "_current_target": "\u4e00\u5408\u5b66\u793e",
        }

        with (
            patch.object(wecom_sender, "ensure_foreground_wecom") as foreground,
            patch.object(wecom_sender, "extract_visible_chat_messages", side_effect=RuntimeError("read failed")),
            patch.object(wecom_sender, "hotkey") as hotkey,
            patch.object(wecom_sender, "focus_message_input") as focus_input,
        ):
            status, detail = wecom_sender.send_message(object(), "\u6d4b\u8bd5\u53d1\u9001", config)

        self.assertEqual(status, "failed")
        self.assertIn("BASELINE_READ_FAILED", detail)
        self.assertEqual(config["_send_verification"]["verify_status"], "failed")
        foreground.assert_called()
        hotkey.assert_not_called()
        focus_input.assert_not_called()

    def test_confirm_sent_message_reopens_target_and_persists_before_confirming(self):
        window = object()
        before = [{"speaker": "家长", "content": "旧消息"}]
        after = before + [{"speaker": "我", "content": "测试发送", "source": "企业微信RPA"}]
        config = {
            "post_send_verify_wait_seconds": 0,
            "post_send_verify_attempts": 1,
            "post_send_verify_clipboard_fallback": False,
            "post_send_verify_ark_fallback": False,
            "_current_family_id": "WECOM_yihe",
            "api_base_url": "http://example.invalid",
        }

        with (
            patch.object(wecom_sender.time, "sleep"),
            patch.object(wecom_sender, "ensure_foreground_wecom"),
            patch.object(wecom_sender, "search_conversation") as search,
            patch.object(wecom_sender, "verify_active_conversation") as verify_title,
            patch.object(wecom_sender, "extract_visible_chat_messages", return_value=after),
            patch.object(
                wecom_sender,
                "sync_conversation_to_api",
                return_value={"messages_inserted": 1, "conversation_check": {"status": "ok", "message_count": 2}},
            ) as sync,
        ):
            confirmed, verification = wecom_sender.confirm_sent_message(window, "一合学社", "测试发送", config, before)

        self.assertTrue(confirmed)
        self.assertEqual(verification["verify_status"], "confirmed")
        self.assertIn("VERIFY_CONFIRMED", verification["verify_detail"])
        self.assertIn("回读已落库", verification["verify_detail"])
        search.assert_called_once_with(window, "一合学社", config)
        verify_title.assert_called_once_with(window, "一合学社", config)
        sync.assert_called_once()

    def test_confirm_sent_message_fails_when_readback_is_not_landed(self):
        window = object()
        before = [{"speaker": "家长", "content": "旧消息"}]
        after = before + [{"speaker": "我", "content": "测试发送", "source": "企业微信RPA"}]
        config = {
            "post_send_verify_wait_seconds": 0,
            "post_send_verify_attempts": 1,
            "post_send_verify_clipboard_fallback": False,
            "post_send_verify_ark_fallback": False,
            "_current_family_id": "WECOM_yihe",
            "api_base_url": "http://example.invalid",
        }

        with (
            patch.object(wecom_sender.time, "sleep"),
            patch.object(wecom_sender, "ensure_foreground_wecom"),
            patch.object(wecom_sender, "search_conversation"),
            patch.object(wecom_sender, "verify_active_conversation"),
            patch.object(wecom_sender, "extract_visible_chat_messages", return_value=after),
            patch.object(wecom_sender, "sync_conversation_to_api", side_effect=RuntimeError("api down")),
        ):
            confirmed, verification = wecom_sender.confirm_sent_message(window, "一合学社", "测试发送", config, before)

        self.assertFalse(confirmed)
        self.assertEqual(verification["verify_status"], "failed")
        self.assertIn("VERIFY_PERSIST_FAILED", verification["verify_detail"])
        self.assertIn("未成功落库", verification["verify_detail"])


    def test_confirm_sent_message_uses_post_send_screenshot_fallback(self):
        window = object()
        before = [{"speaker": "parent", "content": "old message"}]
        screenshot_messages = [{"speaker": "?", "content": "RPA_TOKEN_23", "source": "????RPA-???????"}]
        config = {
            "post_send_verify_wait_seconds": 0,
            "post_send_verify_attempts": 1,
            "post_send_verify_clipboard_fallback": False,
            "post_send_verify_ark_fallback": False,
            "post_send_verify_screenshot_fallback": True,
            "_current_family_id": "WECOM_yihe",
            "api_base_url": "http://example.invalid",
        }

        with (
            patch.object(wecom_sender.time, "sleep"),
            patch.object(wecom_sender, "ensure_foreground_wecom"),
            patch.object(wecom_sender, "search_conversation"),
            patch.object(wecom_sender, "verify_active_conversation"),
            patch.object(wecom_sender, "extract_visible_chat_messages", return_value=before),
            patch.object(wecom_sender, "extract_post_send_screenshot_messages", return_value=screenshot_messages),
            patch.object(
                wecom_sender,
                "sync_conversation_to_api",
                return_value={"messages_inserted": 1, "conversation_check": {"status": "ok", "message_count": 1}},
            ) as sync,
        ):
            confirmed, verification = wecom_sender.confirm_sent_message(window, "??2?", "RPA_TOKEN_23", config, before)

        self.assertTrue(confirmed)
        self.assertEqual(verification["verify_status"], "confirmed")
        self.assertIn("VERIFY_CONFIRMED", verification["verify_detail"])
        self.assertIn("message_count=1", verification["verify_detail"])
        sync.assert_called_once()



if __name__ == "__main__":
    unittest.main()
