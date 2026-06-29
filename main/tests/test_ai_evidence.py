import json
import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import build_ai_evidence, save_ai_output
from app.models import Family, RawMessage


class AiEvidenceTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="f1", parent_nickname="\u5f20\u5988\u5988"))
        for index in range(3):
            self.db.add(
                RawMessage(
                    family_id="f1",
                    message_time=datetime(2026, 6, 30, 9, index, 0),
                    speaker="\u5f20\u5988\u5988",
                    content=f"\u6d88\u606f{index}",
                    source="\u4f01\u5fae",
                )
            )
        self.db.add(
            RawMessage(
                family_id="f2",
                message_time=datetime(2026, 6, 30, 10, 0, 0),
                speaker="\u674e\u5988\u5988",
                content="\u4e0d\u5e94\u8fdb\u5165\u8bc1\u636e",
            )
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_build_ai_evidence_binds_recent_source_messages(self):
        result = {
            "raw": {"\u4f7f\u7528\u4f9d\u636e\u6458\u8981": ["\u5bb6\u957f\u63d0\u5230\u5df2\u6253\u5361"]},
            "display_text": "\u6536\u5230",
        }

        evidence = build_ai_evidence(self.db, "f1", result, limit=2)

        self.assertEqual(evidence["evidence_summary"], ["\u5bb6\u957f\u63d0\u5230\u5df2\u6253\u5361"])
        self.assertEqual([item["content"] for item in evidence["source_messages"]], ["\u6d88\u606f1", "\u6d88\u606f2"])
        self.assertNotIn("\u4e0d\u5e94\u8fdb\u5165\u8bc1\u636e", json.dumps(evidence, ensure_ascii=False))

    def test_save_ai_output_persists_evidence_json(self):
        result = {
            "raw": {"\u4f7f\u7528\u4f9d\u636e\u6458\u8981": "\u6700\u8fd1\u6d88\u606f"},
            "display_text": "\u5efa\u8bae\u56de\u590d",
            "risk_level": "\u4f4e",
            "need_human_review": True,
            "suggested_actions": ["\u5ba1\u6838"],
        }

        output = save_ai_output(self.db, "f1", "ai_reply", "\u5355\u6d4b", result)
        self.db.commit()

        evidence = json.loads(output.evidence_json)
        self.assertEqual(evidence["family_id"], "f1")
        self.assertTrue(evidence["source_messages"])
        self.assertEqual(evidence["evidence_summary"], ["\u6700\u8fd1\u6d88\u606f"])


if __name__ == "__main__":
    unittest.main()
