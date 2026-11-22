"""The preservation gate must not silently accept empty business tables."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
fixture = importlib.import_module("migration_business_fixture")
ReleaseError = importlib.import_module("release_ops").ReleaseError


class BusinessFixtureCoverageTests(unittest.TestCase):
    def setUp(self):
        self.counts = {table: 1 for table in fixture.REQUIRED_BUSINESS_TABLES}

    def test_accepts_nonempty_fixture_with_other_schema_tables(self):
        self.counts.update(users=3, agents=5, agent_runs=0)
        fixture.require_business_fixture(self.counts)

    def test_deleting_all_rows_from_any_required_table_blocks_proof(self):
        for table in fixture.REQUIRED_BUSINESS_TABLES:
            with self.subTest(table=table):
                changed = self.counts | {table: 0}
                with self.assertRaisesRegex(
                    ReleaseError, "BUSINESS_FIXTURE_NOT_REPRESENTATIVE"
                ):
                    fixture.require_business_fixture(changed)

    def test_missing_table_blocks_proof(self):
        for table in fixture.REQUIRED_BUSINESS_TABLES:
            with self.subTest(table=table):
                changed = self.counts.copy()
                del changed[table]
                with self.assertRaises(ReleaseError):
                    fixture.require_business_fixture(changed)

    def test_malformed_count_or_payload_blocks_proof(self):
        for value in (True, None, -1, "1", 1.0):
            with self.subTest(value=value):
                with self.assertRaises(ReleaseError):
                    fixture.require_business_fixture(self.counts | {"publications": value})
        for value in ([], None, "not-counts"):
            with self.subTest(payload=value):
                with self.assertRaises(ReleaseError):
                    fixture.require_business_fixture(value)


if __name__ == "__main__":
    unittest.main()
