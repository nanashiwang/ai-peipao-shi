import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.claim_lock import (
    MAX_CLAIM_LIMIT,
    claim_lock_report,
    database_dialect_name,
    normalize_claim_limit,
    supports_skip_locked,
)


class ClaimLockStrategyTest(unittest.TestCase):
    def test_normalize_claim_limit_clamps_invalid_or_large_values(self):
        self.assertEqual(normalize_claim_limit(0), 1)
        self.assertEqual(normalize_claim_limit("bad"), 5)
        self.assertEqual(normalize_claim_limit(MAX_CLAIM_LIMIT + 10), MAX_CLAIM_LIMIT)

    def test_sqlite_uses_conditional_update_fallback(self):
        engine = create_engine("sqlite:///:memory:", future=True)
        db = sessionmaker(bind=engine, future=True)()
        try:
            report = claim_lock_report(db)

            self.assertEqual(database_dialect_name(db), "sqlite")
            self.assertFalse(supports_skip_locked(db))
            self.assertEqual(report["label"], "任务领取锁")
            self.assertEqual(report["metrics"]["mode"], "conditional_update_fallback")
        finally:
            db.close()

    def test_postgresql_like_dialect_supports_skip_locked(self):
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        self.assertTrue(supports_skip_locked(bind))
        self.assertEqual(claim_lock_report(bind)["metrics"]["mode"], "row_lock_skip_locked")


if __name__ == "__main__":
    unittest.main()
