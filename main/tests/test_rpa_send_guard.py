import unittest

from rpa.send_guard import (
    SendGuardError,
    add_send_trace,
    active_conversation_verified,
    config_for_send_mode,
    conversation_title_mismatch_detail,
    detail_with_send_trace,
    dry_run_result_detail,
    real_send_block_detail,
    real_send_enabled,
    real_send_requested,
    search_result_not_found_detail,
    sent_content_confirmed,
    should_press_send_hotkey,
    target_in_allowed_conversations,
    target_not_allowed_detail,
    validate_active_conversation_title,
    validate_foreground_wecom,
    validate_visual_hit,
    visual_hit_has_coordinates,
)


class RpaSendGuardTest(unittest.TestCase):
    def test_real_send_requires_device_control_switch(self):
        with self.assertRaises(SendGuardError) as ctx:
            config_for_send_mode({"dry_run": True, "allow_real_send": False}, "real_send")

        self.assertEqual(str(ctx.exception), real_send_block_detail())

    def test_real_send_can_disable_dry_run_only_after_control_switch(self):
        config = config_for_send_mode({"dry_run": True, "allow_real_send": True}, "real_send")

        self.assertFalse(config["dry_run"])

    def test_server_policy_overrides_local_real_send_switch(self):
        self.assertTrue(real_send_enabled({"allow_real_send": False, "server_allow_real_send": True}))
        self.assertFalse(real_send_enabled({"allow_real_send": True, "server_allow_real_send": False}))

    def test_dry_run_mode_overrides_even_when_control_switch_is_enabled(self):
        config = config_for_send_mode({"dry_run": False, "allow_real_send": True, "clear_after_dry_run": False}, "dry_run")

        self.assertTrue(config["dry_run"])
        self.assertTrue(config["clear_after_dry_run"])
        self.assertFalse(should_press_send_hotkey(config))

    def test_default_dry_run_forces_cleanup_and_never_presses_send_hotkey(self):
        config = config_for_send_mode({"dry_run": True, "allow_real_send": True, "clear_after_dry_run": False}, "")

        self.assertTrue(config["clear_after_dry_run"])
        self.assertFalse(should_press_send_hotkey(config))
        self.assertIn("未按发送键", dry_run_result_detail())
        self.assertIn("已清空输入框", dry_run_result_detail())

    def test_send_trace_is_appended_once(self):
        config = {}
        add_send_trace(config, "会话列表OCR命中")
        add_send_trace(config, "会话列表OCR命中")
        add_send_trace(config, "标题OCR命中")

        detail = detail_with_send_trace("DRY_RUN: 已完成", config)

        self.assertIn("RPA_TRACE: 会话列表OCR命中；标题OCR命中", detail)
        self.assertEqual(detail_with_send_trace(detail, config), detail)

    def test_sent_content_confirmation_ignores_whitespace(self):
        messages = [{"speaker": "我", "content": "测试发送\n第一行 第二行"}]

        self.assertTrue(sent_content_confirmed("测试发送 第一行\n第二行", messages))
        self.assertFalse(sent_content_confirmed("另一条内容", messages))

    def test_sent_content_confirmation_only_checks_recent_messages(self):
        messages = [{"speaker": "我", "content": f"旧消息{i}"} for i in range(9)]
        messages[0]["content"] = "重复内容"

        self.assertFalse(sent_content_confirmed("重复内容", messages, recent_count=8))

    def test_real_send_enabled_requires_boolean_true(self):
        self.assertTrue(real_send_enabled({"allow_real_send": True}))
        self.assertFalse(real_send_enabled({"allow_real_send": "true"}))
        self.assertFalse(real_send_enabled({}))

    def test_legacy_dry_run_false_is_also_blocked_without_control_switch(self):
        self.assertTrue(real_send_requested({"dry_run": False}, ""))
        with self.assertRaises(SendGuardError):
            config_for_send_mode({"dry_run": False, "allow_real_send": False}, "")

    def test_real_send_mode_allows_send_hotkey_only_after_control_switch(self):
        config = config_for_send_mode({"dry_run": True, "allow_real_send": True}, "real_send")

        self.assertTrue(should_press_send_hotkey(config))

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

    def test_visual_hit_guard_accepts_coordinate_hit(self):
        hit = {"rx": 0.2, "ry": 0.3, "text": "一合学社"}

        self.assertTrue(visual_hit_has_coordinates(hit))
        self.assertIsNone(validate_visual_hit("一合学社", hit, stage="搜索结果"))

    def test_visual_hit_guard_rejects_empty_search_result(self):
        with self.assertRaises(SendGuardError) as empty:
            validate_visual_hit("一合学社", None, stage="搜索结果")
        self.assertEqual(str(empty.exception), search_result_not_found_detail("一合学社", "搜索结果"))
        self.assertIn("绝不盲点坐标", str(empty.exception))

        with self.assertRaises(SendGuardError) as no_coordinate:
            validate_visual_hit("一合学社", {"text": "测试2群"}, stage="搜索结果")
        self.assertIn("搜索结果未命中「一合学社」", str(no_coordinate.exception))


if __name__ == "__main__":
    unittest.main()
