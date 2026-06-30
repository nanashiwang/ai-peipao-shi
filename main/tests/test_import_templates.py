import csv
import unittest
from io import StringIO

from app.services.importer import get_import_template, import_template_csv_bytes, list_import_templates


class ImportTemplateCatalogTest(unittest.TestCase):
    def test_catalog_covers_required_business_template_types(self):
        templates = list_import_templates()
        business_types = {item["business_type"] for item in templates}

        self.assertTrue({
            "学员信息",
            "聊天记录",
            "打卡记录",
            "请假缺课记录",
            "课程阶段数据",
        }.issubset(business_types))
        self.assertTrue(all(item["version"] for item in templates))
        self.assertTrue(all(item["required_fields"] for item in templates))

    def test_chat_template_keeps_importer_required_headers(self):
        template = get_import_template("chat_messages_v1")

        self.assertTrue({"family_id", "message_time", "speaker", "content"}.issubset(template["headers"]))
        self.assertEqual(template["template_family"], "chat_messages")

    def test_course_stage_template_contains_stage_profile_fields(self):
        template = get_import_template("course_stage_v1")

        self.assertTrue({
            "course_stage",
            "unit_progress",
            "pbl_count",
            "checkin_rate",
            "next_milestone",
        }.issubset(template["headers"]))
        self.assertEqual(template["template_family"], "course_stage")

    def test_template_csv_contains_utf8_bom_header_and_sample_row(self):
        raw = import_template_csv_bytes("chat_messages_v1")

        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        rows = list(csv.DictReader(StringIO(text)))
        self.assertEqual(rows[0]["family_id"], "FAM001")
        self.assertIn("content", rows[0])

    def test_unknown_template_rejected(self):
        with self.assertRaises(KeyError):
            get_import_template("unknown_v1")


if __name__ == "__main__":
    unittest.main()
