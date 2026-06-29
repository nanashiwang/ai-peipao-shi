import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import seed_templates
from app.models import Template
from app.services.scenario import detect_scene


class SopTemplateTest(unittest.TestCase):
    REQUIRED_SCENES = {
        "\u9996\u8054\u6b22\u8fce",
        "\u73ed\u4f1a\u901a\u77e5",
        "\u6253\u5361\u63d0\u9192",
        "PBL\u63d0\u4ea4",
        "PBL\u70b9\u8bc4",
        "\u8bf7\u5047/\u5b69\u5b50\u6709\u4e8b",
        "\u8bf7\u5047/\u8865\u8bfe",
        "\u6548\u679c\u8d28\u7591",
        "\u7eed\u62a5",
        "\u7ed3\u8bfe",
    }

    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def test_detect_scene_covers_sop_scenarios(self):
        cases = {
            "\u521a\u8fdb\u7fa4\uff0c\u7b2c\u4e00\u6b21\u4e0a\u8bfe\u9700\u8981\u51c6\u5907\u4ec0\u4e48": "\u9996\u8054\u6b22\u8fce",
            "\u4eca\u665a\u73ed\u4f1a\u51e0\u70b9\u5f00\u59cb": "\u73ed\u4f1a\u901a\u77e5",
            "\u8bf7\u63d0\u9192\u5b69\u5b50\u4eca\u5929\u6253\u5361": "\u6253\u5361\u63d0\u9192",
            "\u5b69\u5b50PBL\u5c0f\u4f5c\u54c1\u5df2\u7ecf\u53d1\u7fa4": "PBL\u63d0\u4ea4",
            "\u8bf7\u5e2e\u5fd9\u770b\u770b\u8fd9\u4e2aPBL\u4f5c\u54c1\u600e\u4e48\u6539": "PBL\u70b9\u8bc4",
            "\u4eca\u5929\u8bf7\u5047\u4e0a\u4e0d\u4e86": "\u8bf7\u5047/\u5b69\u5b50\u6709\u4e8b",
            "\u60f3\u95ee\u4e00\u4e0b\u8bf7\u5047\u540e\u600e\u4e48\u8865\u8bfe": "\u8bf7\u5047/\u8865\u8bfe",
            "\u611f\u89c9\u6ca1\u6548\u679c\uff0c\u5b69\u5b50\u6ca1\u53d8\u5316": "\u6548\u679c\u8d28\u7591",
            "\u4e0b\u4e00\u9636\u6bb5\u7eed\u62a5\u600e\u4e48\u5b89\u6392": "\u7eed\u62a5",
            "\u8fd9\u6b21\u8bfe\u7a0b\u7ed3\u8bfe\u540e\u600e\u4e48\u590d\u76d8": "\u7ed3\u8bfe",
        }

        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(detect_scene(text), expected)

    def test_seed_templates_backfills_required_sop_scenes_without_duplicates(self):
        self.db.add(Template(name="\u9996\u8054\u6b22\u8fce", scene="\u9996\u8054\u6b22\u8fce", content="\u7528\u6237\u81ea\u5b9a\u4e49"))
        self.db.commit()

        seed_templates(self.db)
        seed_templates(self.db)

        scenes = {item.scene for item in self.db.query(Template).all()}
        self.assertTrue(self.REQUIRED_SCENES.issubset(scenes))
        self.assertEqual(self.db.query(Template).filter(Template.name == "\u9996\u8054\u6b22\u8fce").count(), 1)
        self.assertEqual(self.db.query(Template).filter(Template.name == "\u9996\u8054\u6b22\u8fce").one().content, "\u7528\u6237\u81ea\u5b9a\u4e49")


if __name__ == "__main__":
    unittest.main()
