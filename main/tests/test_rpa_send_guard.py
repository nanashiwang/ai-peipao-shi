import unittest

from rpa.send_guard import (
    SendGuardError,
    active_conversation_verified,
    config_for_send_mode,
    conversation_title_mismatch_detail,
    real_send_block_detail,
    real_send_enabled,
    real_send_requested,
    target_in_allowed_conversations,
    target_not_allowed_detail,
    validate_active_conversation_title,
    validate_foreground_wecom,
)


class RpaSendGuardTest(unittest.TestCase):
    def test_real_send_requires_device_hard_switch(self):
        with self.assertRaises(SendGuardError) as ctx:
            config_for_send_mode({"dry_run": True, "allow_real_send": False}, "real_send")

        self.assertEqual(str(ctx.exception), real_send_block_detail())

    def test_real_send_can_disable_dry_run_only_after_hard_switch(self):
        config = config_for_send_mode({"dry_run": True, "allow_real_send": True}, "real_send")

        self.assertFalse(config["dry_run"])

    def test_dry_run_mode_overrides_even_when_hard_switch_is_enabled(self):
        config = config_for_send_mode({"dry_run": False, "allow_real_send": True}, "dry_run")

        self.assertTrue(config["dry_run"])

    def test_real_send_enabled_requires_boolean_true(self):
        self.assertTrue(real_send_enabled({"allow_real_send": True}))
        self.assertFalse(real_send_enabled({"allow_real_send": "true"}))
        self.assertFalse(real_send_enabled({}))

    def test_legacy_dry_run_false_is_also_blocked_without_hard_switch(self):
        self.assertTrue(real_send_requested({"dry_run": False}, ""))
        with self.assertRaises(SendGuardError):
            config_for_send_mode({"dry_run": False, "allow_real_send": False}, "")

    def test_target_must_be_in_allowed_conversations(self):
        allowed = ["一合学社", "测试2群", ""]

        self.assertTrue(target_in_allowed_conversations(" 一合学社 ", allowed))
        self.assertFalse(target_in_allowed_conversations("许宝月", allowed))
        self.assertFalse(target_in_allowed_conversations("", allowed))
        self.assertEqual(target_not_allowed_detail("许宝月"), "目标「许宝月」不在白名单，已跳过。")

    def test_foreground_guard_accepts_wecom_process_or_target_window(self):
        self.assertIsNone(validate_foreground_wecom(101, target_handle=202, foreground_is_wecom=True))
        self.assertIsNone(validate_foreground_wecom(202, target_handle=202, foreground_is_wecom=False))

    def test_foreground_guard_rejects_unknown_or_non_wecom_window(self):
        with self.assertRaises(SendGuardError) as unknown:
            validate_foreground_wecom(0, target_handle=202, foreground_is_wecom=False)
        self.assertIn("无法确认当前前台窗口", str(unknown.exception))

        with self.assertRaises(SendGuardError) as mismatch:
            validate_foreground_wecom(303, target_handle=202, foreground_is_wecom=False, foreground_title="浏览器")
        self.assertIn("当前前台窗口不是企业微信", str(mismatch.exception))
        self.assertIn("浏览器", str(mismatch.exception))

    def test_active_conversation_title_accepts_visible_ocr_or_ark_match(self):
        self.assertTrue(active_conversation_verified(" 一合学社 ", visible_text="当前聊天：一合学社"))
        self.assertTrue(active_conversation_verified("一合学社", ocr_items=[{"text": "一合学社", "score": 0.9}]))
        self.assertTrue(active_conversation_verified("一合学社", ocr_items=[{"text": "一合学舍", "score": 0.9}], min_ratio=0.7))
        self.assertTrue(active_conversation_verified("一合学社", ark_hit=True))

    def test_active_conversation_title_rejects_ocr_wrong_conversation(self):
        self.assertFalse(active_conversation_verified("一合学社", ocr_items=[{"text": "测试2群", "score": 0.99}], min_ratio=0.9))

        with self.assertRaises(SendGuardError) as ctx:
            validate_active_conversation_title("一合学社", ocr_items=[{"text": "测试2群", "score": 0.99}], min_ratio=0.9)
        self.assertEqual(str(ctx.exception), conversation_title_mismatch_detail("一合学社"))


if __name__ == "__main__":
    unittest.main()
