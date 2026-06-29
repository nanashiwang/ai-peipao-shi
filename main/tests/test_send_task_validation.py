import unittest

from fastapi import HTTPException

from app.main import validate_send_task_content


class SendTaskValidationTest(unittest.TestCase):
    def assert_invalid(self, content: str):
        with self.assertRaises(HTTPException):
            validate_send_task_content(content)

    def test_accepts_normal_chinese_content(self):
        content = "RPA\u6d4b\u8bd5\u6d88\u606f\uff0c\u8bf7\u5ffd\u7565\u3002"

        self.assertEqual(validate_send_task_content(content), content)

    def test_rejects_empty_content(self):
        self.assert_invalid("   ")

    def test_rejects_question_mark_mojibake(self):
        self.assert_invalid("RPA?????????????????????")

    def test_rejects_replacement_character(self):
        self.assert_invalid("RPA\u6d4b\u8bd5\ufffd\u6d88\u606f")

    def test_rejects_common_mojibake_tokens(self):
        self.assert_invalid("\u93b4\u621d\u6ed1\u6d93\u7487\u5cf0\u5bf0\u7039")

    def test_accepts_normal_question(self):
        content = "\u8bf7\u95ee\u4eca\u5929\u9700\u8981\u6253\u5361\u5417?"

        self.assertEqual(validate_send_task_content(content), content)


if __name__ == "__main__":
    unittest.main()
