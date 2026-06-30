import unittest

from rpa.send_batch_guard import send_until_blocked


class SendBatchGuardTest(unittest.TestCase):
    def test_stops_after_first_task_when_result_outbox_becomes_blocked(self):
        sent = []

        def send_one(task):
            sent.append(task["id"])

        def is_blocked(stage):
            return stage == "after_task" and len(sent) == 1

        count = send_until_blocked(
            [{"id": 1}, {"id": 2}],
            send_one,
            is_blocked,
            sleep_seconds=0,
        )

        self.assertEqual(count, 1)
        self.assertEqual(sent, [1])

    def test_does_not_send_when_already_blocked_before_first_task(self):
        sent = []

        count = send_until_blocked(
            [{"id": 1}],
            lambda task: sent.append(task["id"]),
            lambda stage: stage == "before_task",
        )

        self.assertEqual(count, 0)
        self.assertEqual(sent, [])

    def test_sleeps_between_successful_tasks_only(self):
        sleeps = []

        count = send_until_blocked(
            [{"id": 1}, {"id": 2}],
            lambda task: None,
            lambda stage: False,
            sleep_seconds=1.5,
            sleep_func=sleeps.append,
        )

        self.assertEqual(count, 2)
        self.assertEqual(sleeps, [1.5, 1.5])


if __name__ == "__main__":
    unittest.main()
