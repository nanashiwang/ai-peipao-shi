import unittest

from app.services.send_task_operations import (
    role_allows_task_operation,
    send_task_operation_state,
    send_task_workflow_stage,
)


class SendTaskOperationsTest(unittest.TestCase):
    def test_readonly_can_only_view(self):
        state = send_task_operation_state("pending", "dry_run", "readonly")

        self.assertEqual(state["allowed_operations"], ["view"])
        self.assertIn("只读角色", state["operation_warnings"][0])

    def test_coach_can_review_and_dry_run_but_not_confirm_real_send(self):
        state = send_task_operation_state("pending", "dry_run", "coach")

        self.assertIn("edit", state["allowed_operations"])
        self.assertIn("review", state["allowed_operations"])
        self.assertIn("dry_run", state["allowed_operations"])
        self.assertNotIn("confirm_real_send", state["allowed_operations"])

    def test_admin_can_confirm_real_send_after_dry_run(self):
        state = send_task_operation_state("dry_run", "dry_run", "admin")

        self.assertIn("confirm_real_send", state["allowed_operations"])
        self.assertFalse(role_allows_task_operation("dry_run", "dry_run", "coach", "confirm_real_send"))

    def test_real_send_task_is_admin_only_for_editing(self):
        self.assertFalse(role_allows_task_operation("pending", "real_send", "coach", "edit"))
        self.assertFalse(role_allows_task_operation("pending", "real_send", "coach", "web_send"))
        self.assertTrue(role_allows_task_operation("pending", "real_send", "admin", "edit"))
        self.assertTrue(role_allows_task_operation("pending", "real_send", "admin", "cancel"))

    def test_terminal_task_only_allows_view(self):
        state = send_task_operation_state("sent", "real_send", "admin")

        self.assertEqual(state["workflow_stage"], "已发送归档")
        self.assertEqual(state["allowed_operations"], ["view"])

    def test_workflow_stage_labels(self):
        self.assertEqual(send_task_workflow_stage("pending", "real_send"), "待企微真实发送")
        self.assertEqual(send_task_workflow_stage("failed", "dry_run"), "发送失败待复核")


if __name__ == "__main__":
    unittest.main()
