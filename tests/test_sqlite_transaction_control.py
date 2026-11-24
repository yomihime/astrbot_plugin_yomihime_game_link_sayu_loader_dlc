from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from sqlite_transaction_contract import driver_control


@unittest.skipUnless(
    hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"),
    "requires Python sqlite3 transaction controls",
)
class TransactionControlTests(unittest.TestCase):
    def test_first_savepoint_durability_and_read_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = driver_control(Path(tmp))
        legacy, explicit = report["driver_default"], report["explicit_begin"]
        self.assertEqual(legacy["autocommit"], sqlite3.LEGACY_TRANSACTION_CONTROL)
        for field in (
            "first_savepoint_survived",
            "ddl_survived_rollback",
            "repeat_read_saw_intervening_commit",
        ):
            self.assertTrue(legacy[field], field)
            self.assertFalse(explicit[field], field)
        self.assertFalse(legacy["outer_transaction_after_release"])
        self.assertTrue(explicit["outer_transaction_after_release"])
