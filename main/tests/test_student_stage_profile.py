import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Family, RawMessage
from app.services.importer import import_rows


class StudentStageProfileTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def test_course_stage_profile_import_updates_family_without_message(self):
        result = import_rows(self.db, [{
            "家庭编号": "FAM_STAGE",
            "家长昵称": "林妈妈",
            "孩子年级": "初一",
            "课程阶段": "S级陪跑第1阶段",
            "Unit进度": "Unit 3",
            "PBL次数": "2",
            "打卡完成率": "86%",
            "下一里程碑": "完成第3次PBL展示",
            "陪跑师": "怡彤老师",
        }])

        family = self.db.query(Family).filter(Family.family_id == "FAM_STAGE").one()

        self.assertEqual(result["families"], 1)
        self.assertEqual(result["messages"], 0)
        self.assertEqual(result["profile_rows"], 1)
        self.assertEqual(self.db.query(RawMessage).count(), 0)
        self.assertEqual(family.child_grade, "初一")
        self.assertEqual(family.course_stage, "S级陪跑第1阶段")
        self.assertEqual(family.unit_progress, "Unit 3")
        self.assertEqual(family.pbl_count, 2)
        self.assertEqual(family.checkin_rate, "86%")
        self.assertEqual(family.next_milestone, "完成第3次PBL展示")

    def test_invalid_pbl_count_warns_and_does_not_overwrite_existing_value(self):
        self.db.add(Family(family_id="FAM_STAGE", parent_nickname="林妈妈", pbl_count=3))
        self.db.commit()

        result = import_rows(self.db, [{
            "family_id": "FAM_STAGE",
            "parent_nickname": "林妈妈",
            "course_stage": "S级陪跑第2阶段",
            "pbl_count": "两次",
            "checkin_rate": "90%",
        }])

        family = self.db.query(Family).filter(Family.family_id == "FAM_STAGE").one()
        issue_codes = {issue["code"] for issue in result["issues"]}

        self.assertEqual(result["profile_rows"], 1)
        self.assertIn("invalid_pbl_count", issue_codes)
        self.assertEqual(family.pbl_count, 3)
        self.assertEqual(family.course_stage, "S级陪跑第2阶段")
        self.assertEqual(family.checkin_rate, "90%")


if __name__ == "__main__":
    unittest.main()
