import unittest

from app.services.ark_client import extract_json_object


class ArkClientJsonParsingTest(unittest.TestCase):
    def test_extract_json_object_from_fenced_json_with_extra_text(self):
        text = """模型说明
```json
{"found": {"最终测试": true}}
```
后续解释文字
"""

        self.assertEqual(extract_json_object(text), {"found": {"最终测试": True}})

    def test_extract_json_object_from_first_balanced_object(self):
        text = '前置说明 {"found": true, "text": "最终测试"} 后续说明'

        self.assertEqual(extract_json_object(text), {"found": True, "text": "最终测试"})


if __name__ == "__main__":
    unittest.main()
