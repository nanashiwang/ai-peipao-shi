import json
import unittest
from unittest.mock import patch

from deploy import health_probe


class FakeResponse:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or {}

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class HealthProbeTest(unittest.TestCase):
    def test_run_once_returns_success_for_ok_health(self):
        with patch("deploy.health_probe.urllib.request.urlopen", return_value=FakeResponse({"ok": True})):
            self.assertEqual(health_probe.run_once({"HEALTH_PROBE_URL": "http://api/health"}), 0)

    def test_run_once_posts_alert_for_failed_health(self):
        calls = []

        def fake_urlopen(request, timeout=5):
            calls.append(request)
            if len(calls) == 1:
                return FakeResponse({"ok": False, "config_status": "critical"})
            return FakeResponse({})

        with patch("deploy.health_probe.urllib.request.urlopen", side_effect=fake_urlopen):
            result = health_probe.run_once({"HEALTH_PROBE_URL": "http://api/health", "ALERT_WEBHOOK_URL": "http://alert"})

        self.assertEqual(result, 1)
        self.assertEqual(calls[1].full_url, "http://alert")
        self.assertIn("AI陪跑师健康检查异常", calls[1].data.decode("utf-8"))

    def test_load_health_handles_invalid_response(self):
        with patch("deploy.health_probe.urllib.request.urlopen", return_value=FakeResponse({"status": "missing-ok"})):
            ok, payload = health_probe.load_health("http://api/health", 5)

        self.assertFalse(ok)
        self.assertEqual(payload["status"], "missing-ok")


if __name__ == "__main__":
    unittest.main()
