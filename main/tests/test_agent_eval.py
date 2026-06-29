import unittest

from app.services.agent_eval import AGENT_EVAL_CASES, evaluate_case, run_agent_evaluation


class AgentEvaluationTest(unittest.TestCase):
    def test_eval_suite_covers_core_sop_and_safety_cases(self):
        scenes = {case["expected_scene"] for case in AGENT_EVAL_CASES}

        self.assertTrue({
            "首联欢迎",
            "打卡提醒",
            "PBL点评",
            "请假/补课",
            "续报",
            "结课",
            "转人工",
        }.issubset(scenes))

    def test_all_baseline_eval_cases_pass(self):
        result = run_agent_evaluation()

        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["passed"], result["total"])
        self.assertGreaterEqual(result["pass_rate"], 1.0)

    def test_risk_case_requires_manual_safe_reply_shape(self):
        case = next(item for item in AGENT_EVAL_CASES if item["id"] == "risk_refund_complaint")

        result = evaluate_case(case)

        self.assertTrue(result["passed"])
        self.assertEqual(result["actual_scene"], "转人工")
        self.assertEqual(result["actual_risk_level"], "高")
        self.assertIn("主管", result["reply"])
        self.assertFalse(result["forbidden_hits"])


if __name__ == "__main__":
    unittest.main()
