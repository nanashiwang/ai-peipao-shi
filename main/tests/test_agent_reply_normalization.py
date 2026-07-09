import unittest
from unittest.mock import patch

from app.main import ai_reply_scene
from app.models import Family
from app.services.agent_service import run_quick_reply_agent_service, run_reply_agent_service


def context() -> dict:
    return {
        "db": None,
        "family": Family(family_id="family-1", parent_nickname="许宝月", child_grade="三年级", coach_name="王坤"),
        "messages": [],
        "profile": None,
        "reports": [],
        "logs": [],
        "templates": [],
    }


class AgentReplyNormalizationTest(unittest.TestCase):
    def test_reply_agent_accepts_new_prompt_fields(self):
        ark_result = {
            "agent": "ai_reply_agent",
            "回复正文": "收到，我先看一下今天的安排，稍后同步给您。",
            "意图": "咨询",
            "风险等级": "低",
            "是否可加入发送任务": True,
            "needs_human_review": False,
            "下一步动作": "记录跟进",
        }

        with patch("app.services.agent_service.call_ark_json", return_value=ark_result):
            result = run_reply_agent_service(context(), "老师，今天怎么安排？")

        self.assertEqual(result["display_text"], "收到，我先看一下今天的安排，稍后同步给您。")
        self.assertEqual(result["risk_level"], "低")
        self.assertFalse(result["need_human_review"])
        self.assertEqual(result["suggested_actions"], ["记录跟进"])
        self.assertEqual(ai_reply_scene(result["raw"], "普通咨询"), "咨询")

    def test_reply_agent_blocks_when_model_says_not_queueable(self):
        ark_result = {
            "agent": "ai_reply_agent",
            "回复正文": "我理解您的担心，这个问题我先记录并同步负责老师核对。",
            "意图": "退费",
            "风险等级": "高",
            "是否可加入发送任务": False,
            "needs_human_review": False,
        }

        with patch("app.services.agent_service.call_ark_json", return_value=ark_result):
            result = run_reply_agent_service(context(), "感觉没效果，想退费")

        self.assertTrue(result["need_human_review"])
        self.assertEqual(result["display_text"], "我理解您的担心，这个问题我先记录并同步负责老师核对。")

    def test_quick_reply_accepts_new_prompt_fields(self):
        ark_result = {
            "agent": "quick_reply_agent",
            "回复正文": "收到，今天先不加压，我们优先完成最关键的一项即可。",
            "风险等级": "低",
            "needs_human_review": False,
        }

        with patch("app.services.agent_service.call_ark_json", return_value=ark_result):
            result = run_quick_reply_agent_service(context(), "今天请假")

        self.assertEqual(result["display_text"], "收到，今天先不加压，我们优先完成最关键的一项即可。")
        self.assertFalse(result["need_human_review"])


if __name__ == "__main__":
    unittest.main()
