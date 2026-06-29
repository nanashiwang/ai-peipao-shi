import unittest
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import build_family_timeline
from app.models import AIOutput, CheckinRecord, Family, RawMessage, SendLog, WeeklyReport


class FamilyTimelineTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="f1", parent_nickname="\u5f20\u5988\u5988"))
        self.db.add(Family(family_id="f2", parent_nickname="\u674e\u5988\u5988"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_builds_unified_timeline_sorted_desc(self):
        message = RawMessage(
            family_id="f1",
            message_time=datetime(2026, 6, 28, 9, 0, 0),
            speaker="\u5f20\u5988\u5988",
            content="\u4eca\u5929\u5df2\u6253\u5361",
            source="\u4f01\u5fae",
            checkin_status="\u5df2\u5b8c\u6210",
        )
        self.db.add(message)
        self.db.flush()
        self.db.add_all([
            CheckinRecord(
                family_id="f1",
                message_id=message.id,
                checkin_type="\u5b8c\u6210\u6253\u5361",
                evidence="\u4eca\u5929\u5df2\u6253\u5361",
                created_at=datetime(2026, 6, 28, 9, 1, 0),
            ),
            AIOutput(
                family_id="f1",
                agent_type="ai_reply",
                source="\u4f01\u5faeRPA",
                display_text="\u6536\u5230\uff0c\u7ee7\u7eed\u4fdd\u6301",
                edited_output="\u6536\u5230\uff0c\u7ee7\u7eed\u4fdd\u6301",
                status="needs_review",
                risk_level="\u4f4e",
                updated_at=datetime(2026, 6, 28, 9, 2, 0),
            ),
            WeeklyReport(
                family_id="f1",
                week_label="2026-W26",
                status="approved",
                final_text="\u672c\u5468\u8868\u73b0\u7a33\u5b9a",
                updated_at=datetime(2026, 6, 28, 9, 3, 0),
            ),
            SendLog(
                task_id=1,
                family_id="f1",
                target_name="\u5f20\u5988\u5988",
                status="sent",
                send_mode="real_send",
                detail="\u5df2\u53d1\u9001",
                sent_at=datetime(2026, 6, 28, 9, 4, 0),
            ),
            RawMessage(
                family_id="f2",
                message_time=datetime(2026, 6, 28, 10, 0, 0),
                speaker="\u674e\u5988\u5988",
                content="\u5176\u4ed6\u5bb6\u5ead\u6d88\u606f",
            ),
        ])
        self.db.commit()

        timeline = build_family_timeline(self.db, "f1")

        self.assertEqual([item["kind"] for item in timeline], ["send_log", "weekly_report", "ai_output", "checkin", "message"])
        self.assertEqual(timeline[0]["send_mode"], "real_send")
        self.assertNotIn("\u5176\u4ed6\u5bb6\u5ead\u6d88\u606f", [item["content"] for item in timeline])

    def test_limits_timeline_size(self):
        for index in range(3):
            self.db.add(
                RawMessage(
                    family_id="f1",
                    message_time=datetime(2026, 6, 28, 9, index, 0),
                    speaker="\u5f20\u5988\u5988",
                    content=f"\u6d88\u606f{index}",
                )
            )
        self.db.commit()

        timeline = build_family_timeline(self.db, "f1", limit=2)

        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0]["content"], "\u6d88\u606f2")


if __name__ == "__main__":
    unittest.main()
