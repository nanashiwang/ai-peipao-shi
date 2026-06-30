import unittest
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import FollowupIn, build_family_timeline, create_family_followup, list_followups
from app.models import Family, FollowupRecord


class FollowupRecordTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="f1", parent_nickname="林妈妈", coach_name="怡彤老师"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_create_followup_record_and_show_in_timeline(self):
        payload = FollowupIn(
            followup_type="电话",
            content="电话沟通孩子补课安排",
            result="家长认可周五补课",
            next_action="周五课后同步补课结果",
            owner="怡彤老师",
            status="待跟进",
            occurred_at=datetime(2026, 6, 30, 10, 0, 0),
        )

        row = create_family_followup("f1", payload, db=self.db)
        timeline = build_family_timeline(self.db, "f1")

        self.assertEqual(row["followup_type"], "电话")
        self.assertEqual(self.db.query(FollowupRecord).count(), 1)
        self.assertEqual(timeline[0]["kind"], "followup")
        self.assertEqual(timeline[0]["title"], "跟进：电话")
        self.assertIn("结果：家长认可周五补课", timeline[0]["content"])
        self.assertEqual(timeline[0]["owner"], "怡彤老师")

    def test_followup_type_and_status_are_guarded(self):
        with self.assertRaises(HTTPException):
            create_family_followup(
                "f1",
                FollowupIn(followup_type="未知", content="测试", status="待跟进"),
                db=self.db,
            )

        with self.assertRaises(HTTPException):
            create_family_followup(
                "f1",
                FollowupIn(followup_type="私信", content="测试", status="随便"),
                db=self.db,
            )

        self.assertEqual(self.db.query(FollowupRecord).count(), 0)

    def test_list_followups_filters_by_family_and_status(self):
        create_family_followup("f1", FollowupIn(followup_type="私信", content="提醒打卡", status="已完成"), db=self.db)
        self.db.add(Family(family_id="f2", parent_nickname="周爸爸"))
        self.db.add(FollowupRecord(family_id="f2", followup_type="投诉", content="主管介入", status="需升级"))
        self.db.commit()

        rows = list_followups(family_id="f1", status="已完成", db=self.db)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["family_id"], "f1")
        self.assertEqual(rows[0]["status"], "已完成")


if __name__ == "__main__":
    unittest.main()
