import unittest

from app.services.send_log_classifier import classify_send_log, parse_send_trace


class SendLogClassifierTest(unittest.TestCase):
    def test_parses_rpa_trace_items(self):
        detail = "DRY_RUN: 已完成\nRPA_TRACE: 会话列表OCR命中；标题OCR命中；dry-run已清空输入框"

        self.assertEqual(parse_send_trace(detail), ["会话列表OCR命中", "标题OCR命中", "dry-run已清空输入框"])

    def test_classifies_title_mismatch_as_title_guard(self):
        result = classify_send_log("failed", "发送前校验失败：当前聊天标题不是「一合学社」，已阻止发送（防发错群）。")

        self.assertEqual(result["send_stage"], "标题校验")
        self.assertEqual(result["send_reason"], "title_mismatch")
        self.assertEqual(result["send_reason_level"], "danger")

    def test_classifies_search_and_input_failures(self):
        search = classify_send_log("failed", "搜索结果未命中「一合学社」，已中止，绝不盲点坐标。")
        input_focus = classify_send_log("failed", "INPUT_FOCUS: 输入框定位失败：窗口不可点")

        self.assertEqual(search["send_reason"], "search_not_found")
        self.assertEqual(input_focus["send_stage"], "输入框")
        self.assertEqual(input_focus["send_reason"], "input_focus_failed")

    def test_classifies_success_modes(self):
        dry_run = classify_send_log("dry_run", "DRY_RUN: 已定位会话并粘贴内容，未按发送键，已清空输入框。")
        sent = classify_send_log("sent", "REAL_RPA: 已通过企业微信 PC 端发送。")

        self.assertEqual(dry_run["send_reason_label"], "试运行完成")
        self.assertEqual(sent["send_reason_label"], "真实发送完成")


if __name__ == "__main__":
    unittest.main()
