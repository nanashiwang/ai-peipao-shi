import os
import unittest
from unittest.mock import patch

from app.services.rate_limit import (
    ADMIN_API_RULE,
    ADMIN_LOGIN_RULE,
    RateLimitRule,
    SlidingWindowRateLimiter,
    admin_rate_limit_rule_for_path,
    rate_limit_key_for_request,
    rate_limit_report,
)


class RateLimitTest(unittest.TestCase):
    def test_sliding_window_denies_over_limit_and_recovers_after_window(self):
        limiter = SlidingWindowRateLimiter()
        rule = RateLimitRule("unit", 2, 10)

        first = limiter.check("127.0.0.1", rule, now=100.0)
        second = limiter.check("127.0.0.1", rule, now=101.0)
        denied = limiter.check("127.0.0.1", rule, now=102.0)
        recovered = limiter.check("127.0.0.1", rule, now=112.0)

        self.assertTrue(first.allowed)
        self.assertEqual(first.remaining, 1)
        self.assertTrue(second.allowed)
        self.assertEqual(second.remaining, 0)
        self.assertFalse(denied.allowed)
        self.assertGreater(denied.retry_after_seconds, 0)
        self.assertTrue(recovered.allowed)

    def test_reset_clears_buckets(self):
        limiter = SlidingWindowRateLimiter()
        limiter.check("127.0.0.1", RateLimitRule("unit", 1, 10), now=100.0)

        self.assertEqual(limiter.bucket_count(), 1)
        limiter.reset()

        self.assertEqual(limiter.bucket_count(), 0)

    def test_admin_path_rule_exempts_device_and_public_paths(self):
        self.assertIs(admin_rate_limit_rule_for_path("/api/admin/auth/login"), ADMIN_LOGIN_RULE)
        self.assertIs(admin_rate_limit_rule_for_path("/api/send-tasks"), ADMIN_API_RULE)
        self.assertIsNone(admin_rate_limit_rule_for_path("/health"))
        self.assertIsNone(admin_rate_limit_rule_for_path("/api/devices/rpa-01/heartbeat"))
        self.assertIsNone(admin_rate_limit_rule_for_path("/api/devices/rpa-01/claim"))
        self.assertIsNone(admin_rate_limit_rule_for_path("/api/send-tasks/1/result"))

    def test_request_key_groups_by_client_and_rule(self):
        self.assertEqual(rate_limit_key_for_request("10.0.0.1", ADMIN_LOGIN_RULE), "10.0.0.1:login")
        self.assertEqual(rate_limit_key_for_request("10.0.0.1", ADMIN_API_RULE), "10.0.0.1:api")
        self.assertEqual(rate_limit_key_for_request("", ADMIN_API_RULE), "unknown:api")

    def test_report_reflects_enabled_flag(self):
        with patch.dict(os.environ, {"ADMIN_RATE_LIMIT_ENABLED": "true"}):
            enabled = rate_limit_report()
        with patch.dict(os.environ, {"ADMIN_RATE_LIMIT_ENABLED": "false"}):
            disabled = rate_limit_report()

        self.assertEqual(enabled["label"], "管理端限流")
        self.assertEqual(enabled["status"], "ok")
        self.assertTrue(enabled["metrics"]["enabled"])
        self.assertEqual(disabled["status"], "warn")
        self.assertFalse(disabled["metrics"]["enabled"])


if __name__ == "__main__":
    unittest.main()
