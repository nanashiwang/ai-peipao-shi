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

    def test_device_package_includes_send_guard_and_server_controlled_real_send(self):
        self.db.add(Device(device_id="rpa-01", token="token-1", conversations='["一合学社"]', allow_real_send=True))
        self.db.commit()

        response = download_device_package("rpa-01", server_url="https://server.test", db=self.db)
        body = asyncio.run(self.collect_body(response))

        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = set(zf.namelist())
            config = json.loads(zf.read("rpa/config.json").decode("utf-8"))
            manifest = json.loads(zf.read("package_manifest.json").decode("utf-8"))
            readme = zf.read("使用说明.txt").decode("utf-8")

        self.assertIn("rpa/wecom_sender.py", names)
        self.assertIn("rpa/send_guard.py", names)
        self.assertIn("rpa/result_outbox.py", names)
        self.assertIn("watchdog.ps1", names)
        self.assertIn("install_autostart.bat", names)
        self.assertIn("uninstall_autostart.bat", names)
        self.assertIn("校验接入包.ps1", names)
        self.assertIn("package_manifest.json", names)
        self.assertIn("启动.bat", names)
        self.assertEqual(config["api_base_url"], "https://server.test")
        self.assertEqual(config["device_id"], "rpa-01")
        self.assertFalse(config["allow_real_send"])
        self.assertTrue(config["dry_run"])
        self.assertEqual(config["post_send_verify_attempts"], 3)
        self.assertEqual(config["post_send_verify_retry_interval_seconds"], 1.2)
        self.assertTrue(config["post_send_verify_reopen_conversation"])
        self.assertTrue(config["post_send_verify_compare_before_after"])
        self.assertEqual(config["post_send_verify_recent_count"], 12)
        self.assertTrue(config["post_send_verify_clipboard_fallback"])
        self.assertTrue(config["post_send_verify_restore_clipboard"])
        self.assertTrue(config["result_outbox_enabled"])
        self.assertEqual(config["result_outbox_dir"], "result_outbox")
        self.assertEqual(config["result_outbox_flush_limit"], 20)
        self.assertTrue(config["result_outbox_block_new_tasks"])
        self.assertIn("设备监控", readme)
        self.assertIn("真实发送开关", readme)
        self.assertEqual(manifest["package_type"], "rpa-client-script")
        self.assertEqual(manifest["device_id"], "rpa-01")
        manifest_paths = {item["path"] for item in manifest["files"]}
        self.assertIn("rpa/config.json", manifest_paths)
        self.assertIn("rpa/result_outbox.py", manifest_paths)
        self.assertIn("校验接入包.ps1", manifest_paths)


if __name__ == "__main__":
    unittest.main()
