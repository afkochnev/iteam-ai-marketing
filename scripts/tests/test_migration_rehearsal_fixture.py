"""Keep the pinned live CI request consistent with reviewed migration scripts."""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import migration_release_drill as drill


class MigrationRehearsalFixtureTests(unittest.TestCase):
    def test_pinned_request_matches_migration_bytes(self):
        request = json.loads(
            (ROOT / "scripts/fixtures/migration-0028-to-0030.json").read_text()
        )
        drill.validate_rehearsal_request(request)
        versions = ROOT / "backend/alembic/versions"
        for step in request["migration_path"]:
            matches = list(versions.glob(step["revision"] + "_*.py"))
            self.assertEqual(len(matches), 1)
            self.assertEqual(
                "sha256:" + hashlib.sha256(matches[0].read_bytes()).hexdigest(),
                step["script_sha256"],
                "Migration changed: review and pin a matching signed target build.",
            )


if __name__ == "__main__":
    unittest.main()
