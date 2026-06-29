import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import SendLog
from app.services.retention_service import prune_retention, retention_report


class RetentionServiceTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.screenshot_dir = self.root / "data" / "send_screenshots"
        self.screenshot_dir.mkdir(parents=True)
        self.now = datetime(2026, 6, 30, 10, 0, 0)
        self.policy = {"send_log_days": 30, "screenshot_days": 30, "runtime_log_days": 30}

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def touch(self, path: Path, age_days: int, content: bytes = b"x") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        ts = (self.now - timedelta(days=age_days)).timestamp()
        os.utime(path, (ts, ts))
        return path

    def add_send_log(self, age_days: int) -> None:
        self.db.add(
            SendLog(
                task_id=age_days,
                family_id=f"f{age_days}",
                target_name="张妈妈",
                status="failed",
                sent_at=self.now - timedelta(days=age_days),
            )
        )
        self.db.commit()

    def test_report_counts_expired_items_without_deleting(self):
        self.add_send_log(45)
        self.add_send_log(5)
        old_shot = self.touch(self.screenshot_dir / "task_1_20260501_100000_000000.png", 45)
        new_shot = self.touch(self.screenshot_dir / "task_2_20260629_100000_000000.png", 1)
        old_runtime_log = self.touch(self.root / "server.log.1", 45)

        report = retention_report(self.db, self.screenshot_dir, self.root, self.policy, self.now)

        self.assertEqual(report["send_logs"]["expired_count"], 1)
        self.assertEqual(report["screenshots"]["expired_count"], 1)
        self.assertEqual(report["runtime_logs"]["expired_count"], 1)
        self.assertTrue(old_shot.exists())
        self.assertTrue(new_shot.exists())
        self.assertTrue(old_runtime_log.exists())
        self.assertEqual(self.db.query(SendLog).count(), 2)

    def test_prune_deletes_only_expired_controlled_files_and_rows(self):
        self.add_send_log(45)
        self.add_send_log(5)
        old_shot = self.touch(self.screenshot_dir / "task_1_20260501_100000_000000.jpg", 45)
        new_shot = self.touch(self.screenshot_dir / "task_2_20260629_100000_000000.jpg", 1)
        ignored = self.touch(self.screenshot_dir / "manual-note.jpg", 45)
        old_runtime_log = self.touch(self.root / "server.err.log.1", 45)
        current_log = self.touch(self.root / "server.err.log", 45)

        result = prune_retention(self.db, self.screenshot_dir, self.root, self.policy, self.now, execute=True)

        self.assertTrue(result["executed"])
        self.assertEqual(result["deleted"]["send_logs"], 1)
        self.assertEqual(result["deleted"]["screenshots"], 1)
        self.assertEqual(result["deleted"]["runtime_logs"], 1)
        self.assertFalse(old_shot.exists())
        self.assertFalse(old_runtime_log.exists())
        self.assertTrue(new_shot.exists())
        self.assertTrue(ignored.exists())
        self.assertTrue(current_log.exists())
        self.assertEqual(self.db.query(SendLog).count(), 1)


if __name__ == "__main__":
    unittest.main()
