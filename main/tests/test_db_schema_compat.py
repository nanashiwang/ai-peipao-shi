import unittest

from app.db import portable_column_type


class DbSchemaCompatTest(unittest.TestCase):
    def test_postgresql_uses_timestamp_for_datetime_columns(self):
        self.assertEqual(portable_column_type("DATETIME", "postgresql"), "TIMESTAMP")

    def test_sqlite_keeps_existing_datetime_type(self):
        self.assertEqual(portable_column_type("DATETIME", "sqlite"), "DATETIME")

    def test_other_column_types_are_unchanged(self):
        self.assertEqual(portable_column_type("VARCHAR(120)", "postgresql"), "VARCHAR(120)")
        self.assertEqual(portable_column_type("INTEGER", "postgresql"), "INTEGER")


if __name__ == "__main__":
    unittest.main()
