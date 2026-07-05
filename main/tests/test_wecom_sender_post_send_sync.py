import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, patch

RPA_DEPS = ["pyperclip", "win32api", "win32con", "win32gui", "win32process", "pywinauto"]
MISSING_RPA_DEPS = [name for name in RPA_DEPS if importlib.util.find_spec(name) is None]
if MISSING_RPA_DEPS:
    wecom_sender = None
else:
    from rpa import wecom_sender


@unittest.skipIf(wecom_sender is None, f"RPA dependencies missing: {', '.join(MISSING_RPA_DEPS)}")
class WecomSenderPostSendSyncTest(unittest.TestCase):
    def test_search_box_click_point_uses_actual_input_center(self):
        box = (0.0, 0.0, 0.30, 0.07)

        self.assertEqual(wecom_sender.search_box_click_point({}, box), (0.13, 0.052))
        self.assertEqual(
            wecom_sender.search_box_click_point({"search_box_click_ratio_x": 0.2, "search_box_click_ratio_y": 0.08}, box),
            (0.2, 0.08),
        )

    def test_capture_send_screenshot_removes_local_result_image_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "debug_wecom_result_sent_1.png"
            path.write_bytes(b"png-bytes")

            with (
                patch.object(wecom_sender, "find_wecom_window", return_value=object()),
                patch.object(wecom_sender, "activate"),
                patch.object(wecom_sender, "capture_fullscreen_image", return_value=path),
                patch.object(wecom_sender, "screenshot_upload_payload", return_value="encoded"),
            ):
                payload = wecom_sender.capture_send_screenshot({"upload_send_screenshot": True}, 1, "sent")

            self.assertEqual(payload, "encoded")
            self.assertFalse(path.exists())

    def test_prune_local_screenshot_artifacts_deletes_only_expired_controlled_files(self):
        now = datetime(2026, 7, 3, 10, 0, 0)
        with tempfile.TemporaryDirectory() as rpa_tmp, tempfile.TemporaryDirectory() as project_tmp:
            rpa_root = Path(rpa_tmp)
            project_root = Path(project_tmp)
            old_debug = rpa_root / "debug_wecom_locate_20260601_100000_000000.png"
            new_debug = rpa_root / "debug_wecom_locate_20260703_100000_000000.png"
            ignored = rpa_root / "manual-note.png"
            old_tmp = project_root / ".tmp_rpa_messages.json"
            for path, age_days in [(old_debug, 10), (new_debug, 1), (ignored, 10), (old_tmp, 10)]:
                path.write_bytes(b"x")
                ts = (now - timedelta(days=age_days)).timestamp()
                os.utime(path, (ts, ts))

            result = wecom_sender.prune_local_screenshot_artifacts(
                {"local_screenshot_retention_days": 7},
                root=rpa_root,
                project_root=project_root,
                now=now,
            )

            self.assertEqual(result["deleted_count"], 2)
            self.assertFalse(old_debug.exists())
            self.assertFalse(old_tmp.exists())
            self.assertTrue(new_debug.exists())
            self.assertTrue(ignored.exists())

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

    def test_process_task_retries_transient_pre_send_error_before_hotkey(self):
        task = {
            "id": 46,
            "target_name": "parent",
            "family_id": "WECOM_parent",
            "content": "hello",
            "send_mode": "real_send",
            "server_allowed_target": True,
            "device_allow_real_send": True,
        }
        config = {"pre_send_retry_attempts": 2}
        window = object()

        with (
            patch.object(wecom_sender.time, "sleep"),
            patch.object(wecom_sender, "find_wecom_window", return_value=window),
            patch.object(
                wecom_sender,
                "search_conversation",
                side_effect=[RuntimeError("(-2147220991, 'event cannot invoke any subscribers')"), None],
            ) as search,
            patch.object(wecom_sender, "verify_active_conversation") as verify_title,
            patch.object(wecom_sender, "send_message", return_value=("sent", "OK")) as send_message,
        ):
            status, detail, verification = wecom_sender.process_task(task, config)

        self.assertEqual(status, "sent")
        self.assertEqual(verification, {})
        self.assertEqual(search.call_count, 2)
        verify_title.assert_called_once_with(window, "parent", ANY)
        send_message.assert_called_once()
        self.assertIn("发送前瞬时异常重试:2/2", detail)

    def test_process_task_does_not_retry_after_send_hotkey_trace(self):
        task = {
            "id": 47,
            "target_name": "parent",
            "family_id": "WECOM_parent",
            "content": "hello",
            "send_mode": "real_send",
            "server_allowed_target": True,
            "device_allow_real_send": True,
        }
        config = {"pre_send_retry_attempts": 2}

        def fail_after_hotkey(_window, _content, task_config):
            wecom_sender.add_send_trace(task_config, "真实发送热键已触发")
            raise RuntimeError("(-2147220991, 'event cannot invoke any subscribers')")

        with (
            patch.object(wecom_sender.time, "sleep"),
            patch.object(wecom_sender, "find_wecom_window", return_value=object()) as find_window,
            patch.object(wecom_sender, "search_conversation"),
            patch.object(wecom_sender, "verify_active_conversation"),
            patch.object(wecom_sender, "send_message", side_effect=fail_after_hotkey),
        ):
            with self.assertRaises(wecom_sender.RpaError):
                wecom_sender.process_task(task, config)

        find_window.assert_called_once()

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

    def test_post_send_screenshot_visible_text_accepts_short_target_from_found_map(self):
        data = {"found": {"测试1111": True, "最终测试": True}}

        self.assertEqual(wecom_sender.post_send_screenshot_visible_text(data, "最终测试"), "最终测试")

    def test_post_send_screenshot_visible_text_accepts_found_bool(self):
        data = {"found": True}

        self.assertEqual(wecom_sender.post_send_screenshot_visible_text(data, "你好"), "你好")



if __name__ == "__main__":
    unittest.main()
