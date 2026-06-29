import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import (
    SendResultIn,
    create_task_from_report,
    create_tasks_from_reports,
    record_send_result,
    send_task_to_web_chat,
)
from app.models import Family, SendTask, WeeklyReport


class WeeklyReportSendClosureTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()
        self.db.add(Family(family_id="f1", parent_nickname="张妈妈", coach_name="陪跑师"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def add_report(self, final_text="本周状态稳定，继续保持。"):
        report = WeeklyReport(
            family_id="f1",
            week_label="2026-W26",
            status="approved",
            final_text=final_text,
        )
        self.db.add(report)
        self.db.commit()
        return report

    def test_batch_create_binds_report_and_prevents_duplicate_tasks(self):
        report = self.add_report()

        first = create_tasks_from_reports(db=self.db)
        second = create_tasks_from_reports(db=self.db)

        self.db.refresh(report)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["created"], 0)
        self.assertEqual(self.db.query(SendTask).count(), 1)
        self.assertGreater(report.send_task_id, 0)
        self.assertEqual(report.send_status, "task_created")

    def test_single_report_endpoint_reuses_existing_bound_task(self):
        report = self.add_report()

        first = create_task_from_report(report.id, db=self.db)
        second = create_task_from_report(report.id, db=self.db)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        self.assertEqual(self.db.query(SendTask).count(), 1)

    def test_pending_dry_run_task_syncs_latest_report_text(self):
        report = self.add_report()
        create_task_from_report(report.id, db=self.db)
        report.final_text = "更新后的周报内容，请以这一版为准。"
        self.db.commit()

        create_task_from_report(report.id, db=self.db)

        self.db.refresh(report)
        task = self.db.get(SendTask, report.send_task_id)
        self.assertEqual(task.content, "更新后的周报内容，请以这一版为准。")

    def test_rpa_result_updates_report_send_status_and_sent_time(self):
        report = self.add_report()
        create_task_from_report(report.id, db=self.db)
        self.db.refresh(report)

        log = record_send_result(report.send_task_id, SendResultIn(status="sent", detail="REAL_RPA: 已发送"), db=self.db)

        self.db.refresh(report)
        self.assertEqual(log["status"], "sent")
        self.assertEqual(report.send_status, "sent")
        self.assertIsNotNone(report.sent_at)

    def test_web_chat_send_updates_report_closure_status(self):
        report = self.add_report()
        create_task_from_report(report.id, db=self.db)
        self.db.refresh(report)
        task = self.db.get(SendTask, report.send_task_id)

        send_task_to_web_chat(self.db, task)
        self.db.commit()

        self.db.refresh(report)
        self.assertEqual(report.send_status, "sent")
        self.assertIsNotNone(report.sent_at)


if __name__ == "__main__":
    unittest.main()
