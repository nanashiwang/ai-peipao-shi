import asyncio
import io
import json
import unittest
import zipfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import download_device_package
from app.models import Device


class DevicePackageTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    async def collect_body(self, response):
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)

    def test_device_package_includes_send_guard_and_safe_default_config(self):
        self.db.add(Device(device_id="rpa-01", token="token-1", conversations='["一合学社"]'))
        self.db.commit()

        response = download_device_package("rpa-01", server_url="https://server.test", db=self.db)
        body = asyncio.run(self.collect_body(response))

        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = set(zf.namelist())
            config = json.loads(zf.read("rpa/config.json").decode("utf-8"))

        self.assertIn("rpa/wecom_sender.py", names)
        self.assertIn("rpa/send_guard.py", names)
        self.assertEqual(config["api_base_url"], "https://server.test")
        self.assertEqual(config["device_id"], "rpa-01")
        self.assertFalse(config["allow_real_send"])
        self.assertTrue(config["dry_run"])


if __name__ == "__main__":
    unittest.main()
