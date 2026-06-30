import tempfile
import unittest
from pathlib import Path

from rpa.result_outbox import (
    enqueue_result,
    load_result_record,
    mark_result_retry,
    new_client_result_id,
    pending_result_files,
    remove_result_record,
    result_outbox_dir,
)


class ResultOutboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_relative_outbox_dir_is_under_rpa_root(self):
        config = {"result_outbox_dir": "queued_results"}

        self.assertEqual(result_outbox_dir(config, self.root), self.root / "queued_results")

    def test_enqueue_load_mark_retry_and_remove_result(self):
        config = {"device_id": "dev-a", "result_outbox_dir": "outbox"}
        payload = {
            "status": "sent",
            "detail": "REAL_RPA: 已发送",
            "client_result_id": new_client_result_id(config, 7),
        }

        path = enqueue_result(config, self.root, 7, "/api/send-tasks/7/result", payload, "network down")
        files = pending_result_files(config, self.root)
        record = load_result_record(path)

        self.assertEqual(files, [path])
        self.assertEqual(record["task_id"], 7)
        self.assertEqual(record["payload"], payload)
        self.assertEqual(record["last_error"], "network down")

        mark_result_retry(path, record, "still down")
        retried = load_result_record(path)

        self.assertEqual(retried["attempts"], 1)
        self.assertEqual(retried["last_error"], "still down")

        remove_result_record(path)

        self.assertEqual(pending_result_files(config, self.root), [])


if __name__ == "__main__":
    unittest.main()
