import unittest
from unittest.mock import patch

from app.services.admin_auth import (
    admin_auth_required,
    admin_auth_secret,
    admin_token_ttl_seconds,
    normalize_campus_names,
    parent_token_ttl_seconds,
    path_requires_admin_auth,
    role_allowed_for_request,
    sign_admin_token,
    verify_admin_token,
)


class AdminAuthTest(unittest.TestCase):
    def test_auth_required_for_pilot_and_production_unless_explicitly_disabled(self):
        self.assertFalse(admin_auth_required({"APP_ENV": "local"}))
        self.assertTrue(admin_auth_required({"APP_ENV": "pilot"}))
        self.assertTrue(admin_auth_required({"APP_ENV": "production"}))
        self.assertTrue(admin_auth_required({"ADMIN_AUTH_REQUIRED": "true", "APP_ENV": "local"}))
        self.assertFalse(admin_auth_required({"ADMIN_AUTH_REQUIRED": "false", "APP_ENV": "local"}))
        self.assertTrue(admin_auth_required({"ADMIN_AUTH_REQUIRED": "false", "APP_ENV": "production"}))

    def test_production_requires_explicit_secret(self):
        with self.assertRaises(RuntimeError):
            admin_auth_secret({"APP_ENV": "production"})

        with self.assertRaises(RuntimeError):
            admin_auth_secret({"APP_ENV": "production", "ADMIN_AUTH_SECRET": "change-me-before-production"})

        secret = "0123456789abcdef0123456789abcdef"
        self.assertEqual(admin_auth_secret({"APP_ENV": "production", "ADMIN_AUTH_SECRET": secret}), secret)

    def test_token_sign_verify_and_expiry(self):
        token = sign_admin_token("admin", "admin", "系统管理员", "secret", ttl_seconds=60, now=100)
        identity = verify_admin_token(token, "secret", now=120)

        self.assertEqual(identity.username, "admin")
        self.assertEqual(identity.role, "admin")
        self.assertEqual(identity.campus_names, ())
        with self.assertRaises(ValueError):
            verify_admin_token(token, "wrong", now=120)
        with self.assertRaises(ValueError):
            verify_admin_token(token, "secret", now=200)

    def test_token_ttl_defaults_are_long_lived_and_configurable(self):
        self.assertEqual(admin_token_ttl_seconds({}), 30 * 24 * 3600)
        self.assertEqual(parent_token_ttl_seconds({}), 30 * 24 * 3600)
        self.assertEqual(admin_token_ttl_seconds({"ADMIN_TOKEN_TTL_SECONDS": "7200"}), 7200)
        self.assertEqual(parent_token_ttl_seconds({"PARENT_TOKEN_TTL_SECONDS": "7200"}), 7200)
        self.assertEqual(admin_token_ttl_seconds({"ADMIN_TOKEN_TTL_SECONDS": "bad"}), 30 * 24 * 3600)
        self.assertEqual(admin_token_ttl_seconds({"ADMIN_TOKEN_TTL_SECONDS": "1"}), 3600)

        with patch.dict("os.environ", {"ADMIN_TOKEN_TTL_SECONDS": "7200"}):
            token = sign_admin_token("ops", "admin", "系统管理员", "secret", now=100)
        verify_admin_token(token, "secret", now=100 + 7199)
        with self.assertRaises(ValueError):
            verify_admin_token(token, "secret", now=100 + 7201)

    def test_token_keeps_account_campus_scope(self):
        token = sign_admin_token("ops", "readonly", "校区主管", "secret", campus_names="南坪校区，观音桥校区", now=100)
        identity = verify_admin_token(token, "secret", now=120)

        self.assertEqual(identity.campus_names, ("南坪校区", "观音桥校区"))
        self.assertEqual(normalize_campus_names(["南坪校区", "南坪校区", " "]), ("南坪校区",))

    def test_protected_paths_and_role_permissions(self):
        self.assertFalse(path_requires_admin_auth("/health"))
        self.assertFalse(path_requires_admin_auth("/api/admin/auth/status"))
        self.assertFalse(path_requires_admin_auth("/api/admin/auth/login"))
        self.assertFalse(path_requires_admin_auth("/api/admin/auth/register"))
        self.assertFalse(path_requires_admin_auth("/api/parent/dashboard"))
        self.assertFalse(path_requires_admin_auth("/api/devices/rpa-01/heartbeat"))
        self.assertTrue(path_requires_admin_auth("/api/test-chat/login"))
        self.assertTrue(path_requires_admin_auth("/api/send-artifacts/shot_abcdefghijklmnopqrstuvwxyz012345.png"))
        self.assertTrue(path_requires_admin_auth("/api/send-tasks"))

        self.assertTrue(role_allowed_for_request("readonly", "GET", "/api/send-tasks"))
        self.assertFalse(role_allowed_for_request("readonly", "POST", "/api/send-tasks"))
        self.assertTrue(role_allowed_for_request("coach", "POST", "/api/send-tasks"))
        self.assertFalse(role_allowed_for_request("coach", "GET", "/api/ops/health"))
        self.assertTrue(role_allowed_for_request("admin", "GET", "/api/ops/health"))


if __name__ == "__main__":
    unittest.main()
