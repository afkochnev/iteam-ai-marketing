"""Offline coverage of review transfer identity before reusing core containers."""

import json
from pathlib import Path, PureWindowsPath
import sys
import unittest
from unittest.mock import Mock

REVIEW_SOURCES = Path(__file__).resolve().parents[1] / "windows_review"
sys.path.insert(0, str(REVIEW_SOURCES))
from review_origin import validate_core_origin  # noqa: E402
sys.path.remove(str(REVIEW_SOURCES))


class CoreOriginTests(unittest.TestCase):
    root = PureWindowsPath("C:/iTeam Transfers/review")
    project = "iteam_windows_transfer"
    services = {"backend", "frontend", "postgres", "redis"}
    working_dir = "C:/iTeam Transfers/review/windows"
    compose = working_dir + "/compose.windows.yaml"

    def validate(self, *rows):
        read = Mock(return_value=b"\n".join(json.dumps(row).encode() for row in rows))
        validate_core_origin(self.root, read, project=self.project, services=self.services)
        args = read.call_args.args
        self.assertEqual(args[:5], (
            "ps", "--all", "--filter", "label=com.docker.compose.project=iteam_windows_transfer",
            "--format",
        ))
        self.assertIn("com.docker.compose.project.config_files", args[5])

    def test_no_existing_core_or_partial_review_with_matching_labels(self):
        self.validate()
        self.validate(
            ["backend", self.working_dir.upper(), str(PureWindowsPath(self.compose))],
            ["postgres", self.working_dir, self.compose],
        )

    def test_foreign_or_publishing_core_without_any_sibling_projects(self):
        foreign = self.working_dir.replace("review/", "production/")
        for row in (
            ["backend", foreign, foreign + "/compose.windows.yaml"],
            ["frontend", foreign, self.compose],
            ["backend", self.working_dir, self.compose + "," + self.working_dir + "/compose.publication-runtime.json"],
            ["worker", self.working_dir, self.compose],
            ["backend", self.working_dir, "compose.windows.yaml"],
            ["backend", "", self.compose],
            ["backend", self.working_dir, None],
        ):
            with self.subTest(row=row), self.assertRaisesRegex(RuntimeError, "CORE_ORIGIN_MISMATCH"):
                self.validate(["postgres", self.working_dir, self.compose], row)

    def test_duplicate_or_malformed_rows_fail_closed(self):
        row = ["redis", self.working_dir, self.compose]
        for rows in ([row, row], [True], [[]]):
            with self.subTest(rows=rows), self.assertRaisesRegex(RuntimeError, "CORE_ORIGIN_MISMATCH"):
                self.validate(*rows)
        with self.assertRaisesRegex(RuntimeError, "CORE_ORIGIN_MISMATCH"):
            validate_core_origin(
                self.root, Mock(return_value=b"malformed"), project=self.project, services=self.services,
            )


if __name__ == "__main__":
    unittest.main()
