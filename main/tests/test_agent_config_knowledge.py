import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.services.agent_config_service import (
    LOCAL_EMBEDDING_MODEL,
    create_knowledge_chunks,
    list_agent_configs,
    search_knowledge,
    update_agent_config,
)


class AgentConfigKnowledgeTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def test_default_agent_configs_are_seeded_and_editable(self):
        configs = list_agent_configs(self.db)
        keys = {item["agent_key"] for item in configs}

        self.assertIn("ai_reply_agent", keys)
        self.assertIn("daily_workbench_agent", keys)
        with patch("app.services.agent_config_service.call_ark_embedding", side_effect=RuntimeError("no remote")):
            results = search_knowledge(self.db, "家长说没效果想退费怎么办", "ai_reply_agent", 3)
        self.assertTrue(any("高风险回复兜底规则" in item["title"] for item in results))

        saved = update_agent_config(
            self.db,
            "ai_reply_agent",
            name="回复专家",
            system_prompt="只输出JSON，优先引用知识库。",
            enabled=True,
            retrieval_enabled=True,
            retrieval_top_k=3,
        )

        self.assertEqual(saved["name"], "回复专家")
        self.assertEqual(saved["retrieval_top_k"], 3)

    def test_knowledge_chunks_use_vector_search_with_local_fallback(self):
        with patch("app.services.agent_config_service.call_ark_embedding", side_effect=RuntimeError("no remote")):
            created = create_knowledge_chunks(
                self.db,
                title="负面反馈处理SOP",
                content="家长表达不满意或投诉时，先共情确认，再给出明确跟进时间，不要承诺退费。",
                tags="投诉,负面反馈",
                agent_scope="ai_reply_agent",
            )
            results = search_knowledge(self.db, "家长投诉不满意应该怎么回复", "ai_reply_agent", 3)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["embedding_model"], LOCAL_EMBEDDING_MODEL)
        self.assertGreaterEqual(len(results), 1)
        self.assertIn("负面反馈", results[0]["title"])


if __name__ == "__main__":
    unittest.main()
