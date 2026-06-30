import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import (
    AccountIn,
    LoginIn,
    admin_auth_secret,
    admin_auth_status,
    admin_login,
    admin_register,
    hash_password,
    operation_role_from_request,
    sign_admin_token,
    verify_password,
)
from app.models import UserAccount


def request_with_role(role: str, username: str = "admin"):
    token = sign_admin_token(username, role, username, admin_auth_secret())
    return f"Bearer {token}"


class AdminRegistrationTest(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        Base.metadata.create_all(bind=engine)
        self.db = sessionmaker(bind=engine, future=True)()

    def tearDown(self):
        self.db.close()

    def test_first_admin_registration_ignores_existing_non_admin_accounts(self):
        self.db.add(UserAccount(username="coach", password=hash_password("123456"), display_name="陪跑师", role="coach"))
        self.db.commit()

        status = admin_auth_status(db=self.db)
        self.assertTrue(status["bootstrap_required"])

        created = admin_register(
            AccountIn(username="root", password="secret", display_name="超管", role="coach"),
            db=self.db,
        )

        self.assertEqual(created["role"], "admin")
        self.assertIn("admin_token", created)
        saved = self.db.query(UserAccount).filter(UserAccount.username == "root").one()
        self.assertNotEqual(saved.password, "secret")
        self.assertTrue(verify_password(saved.password, "secret"))

    def test_after_bootstrap_only_admin_can_create_control_accounts(self):
        admin = admin_register(AccountIn(username="root", password="secret", display_name="超管"), db=self.db)

        with self.assertRaises(HTTPException) as unauth:
            admin_register(AccountIn(username="coach", password="123456", display_name="陪跑师", role="coach"), db=self.db)
        self.assertEqual(unauth.exception.status_code, 401)

        with self.assertRaises(HTTPException) as coach_blocked:
            admin_register(
                AccountIn(username="readonly", password="123456", display_name="只读", role="readonly"),
                authorization=request_with_role("coach", "coach"),
                db=self.db,
            )
        self.assertEqual(coach_blocked.exception.status_code, 403)

        created = admin_register(
            AccountIn(username="coach", password="123456", display_name="陪跑师", role="coach"),
            authorization=f"Bearer {admin['admin_token']}",
            db=self.db,
        )
        self.assertEqual(created["role"], "coach")
        self.assertIn("admin_token", created)

    def test_admin_login_accepts_and_upgrades_legacy_plaintext_password(self):
        self.db.add(UserAccount(username="legacy", password="123456", display_name="旧账号", role="admin"))
        self.db.commit()

        logged_in = admin_login(LoginIn(username="legacy", password="123456"), db=self.db)

        self.assertEqual(logged_in["role"], "admin")
        saved = self.db.query(UserAccount).filter(UserAccount.username == "legacy").one()
        self.assertNotEqual(saved.password, "123456")
        self.assertTrue(verify_password(saved.password, "123456"))

    def test_http_request_without_token_is_readonly_for_operations(self):
        request = SimpleNamespace(headers={}, state=SimpleNamespace())

        self.assertEqual(operation_role_from_request(request), "readonly")
        self.assertEqual(operation_role_from_request(None), "admin")


if __name__ == "__main__":
    unittest.main()
