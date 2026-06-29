import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Family, RawMessage
from app.services.importer import import_rows


class ImportQualityTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def test_import_reports_quality_issues_and_skips_bad_rows(self):
        rows = [
            {
                "family_id": "F001",
                "parent_nickname": "张妈妈",
                "message_time": "2026-06-30 09:00",
                "speaker": "张妈妈",
                "content": "今天已完成打卡",
                "手机号": "13800138000",
            },
            {
                "family_id": "",
                "parent_nickname": "李妈妈",
                "message_time": "2026-06-30 09:10",
                "speaker": "李妈妈",
                "content": "缺少家庭编号",
            },
            {
                "family_id": "F002",
                "parent_nickname": "王妈妈",
                "message_time": "bad-date",
                "speaker": "王妈妈",
                "content": "时间格式错误",
            },
            {
                "family_id": "F003",
                "parent_nickname": "赵妈妈",
                "message_time": "2026-06-30 09:20",
                "speaker": "赵妈妈",
                "content": "RPA?????????????????????",
            },
        ]

        result = import_rows(self.db, rows)

        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["skipped"], 3)
        self.assertEqual(self.db.query(Family).count(), 1)
        self.assertEqual(self.db.query(RawMessage).count(), 1)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertTrue({"missing_family_id", "invalid_time", "mojibake_content"}.issubset(codes))

    def test_import_skips_duplicate_rows_in_file_and_database(self):
        row = {
            "family_id": "F001",
            "parent_nickname": "张妈妈",
            "message_time": "2026-06-30 09:00",
            "speaker": "张妈妈",
            "content": "今天已完成打卡",
        }

        first = import_rows(self.db, [row, dict(row)])
        second = import_rows(self.db, [dict(row)])

        self.assertEqual(first["messages"], 1)
        self.assertEqual(first["skipped"], 1)
        self.assertEqual(second["messages"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(self.db.query(RawMessage).count(), 1)
        self.assertEqual(first["issues"][0]["code"], "duplicate_in_file")
        self.assertEqual(second["issues"][0]["code"], "duplicate_existing")

    def test_import_allows_warnings_but_surfaces_them(self):
        rows = [
            {
                "family_id": "F001",
                "message_time": "",
                "speaker": "",
                "content": "正常内容",
                "手机号": "123",
            }
        ]

        result = import_rows(self.db, rows)

        self.assertEqual(result["messages"], 1)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertTrue({"missing_conversation", "missing_speaker", "missing_time", "invalid_phone"}.issubset(codes))


if __name__ == "__main__":
    unittest.main()
