"""Guard the manual approval and artifact boundaries of migration signing."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/approve-migration-release.yml"


def run_blocks(source: str) -> list[str]:
    """Read this workflow's literal run blocks without a YAML dependency."""
    result: list[str] = []
    current: list[str] | None = None
    for line in source.splitlines():
        if line == "        run: |":
            current = []
            result.append("")
        elif current is not None:
            if line.startswith("          ") or not line:
                current.append(line)
                result[-1] = "\n".join(current)
            else:
                current = None
    return result


class MigrationApprovalWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text()
        cls.prepare, cls.approve = cls.source.split("  approve:\n", 1)

    def test_only_explicit_main_first_attempt_can_reach_manual_signing(self):
        triggers = self.source.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertEqual(
            re.findall(r"^  ([a-z_]+):", triggers, re.MULTILINE),
            ["workflow_dispatch"],
        )
        guard = "if: github.ref == 'refs/heads/main' && github.run_attempt == 1"
        self.assertIn(guard, self.prepare)
        self.assertIn(guard, self.approve)
        self.assertIn("    needs: prepare\n", self.approve)
        self.assertIn("    environment: production\n", self.approve)
        self.assertIn("  group: production-release-approval\n", self.source)
        self.assertIn("  cancel-in-progress: false\n", self.source)
        self.assertNotIn("id-token: write", self.prepare)
        self.assertIn("      id-token: write\n", self.approve)
        self.assertLess(self.approve.index(".py seal"), self.approve.index("cosign sign-blob"))

    def test_inputs_are_required_strings_passed_as_quoted_environment_values(self):
        fields = {
            "rehearsal_run_id": ("REHEARSAL_RUN", "--rehearsal-run"),
            "rehearsal_artifact_id": ("REHEARSAL_ARTIFACT", "--rehearsal-artifact"),
            "proof_sha256": ("PROOF_SHA256", "--proof-sha256"),
            "expected_current_release_sha": ("EXPECTED_SOURCE", "--expected-source-release"),
            "expected_current_schema_head": ("EXPECTED_SCHEMA", "--expected-source-schema"),
            "target_build_run_id": ("TARGET_BUILD", "--target-build-run"),
        }
        input_section = self.source.split("    inputs:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertEqual(
            set(re.findall(r"^      ([a-z0-9_]+):", input_section, re.MULTILINE)),
            set(fields),
        )
        self.assertEqual(input_section.count("        required: true\n"), len(fields))
        self.assertEqual(input_section.count("        type: string"), len(fields))
        for name, (variable, flag) in fields.items():
            with self.subTest(input=name):
                self.assertIn(variable + ": ${{ inputs." + name + " }}", self.prepare)
                self.assertIn(flag + ' "$' + variable + '"', self.prepare)
        for block in run_blocks(self.source):
            self.assertNotIn("${{", block)

    def test_review_is_selected_by_id_and_hashed_before_receipt_signing(self):
        self.assertIn("review_artifact_id: ${{ steps.review_artifact.outputs.artifact-id }}", self.prepare)
        self.assertIn("review_sha256: ${{ steps.review_hash.outputs.sha256 }}", self.prepare)
        self.assertIn("hashlib.sha256(Path('migration-review.json').read_bytes())", self.prepare)
        self.assertIn("name: migration-release-for-review\n", self.prepare)
        self.assertIn("artifact-ids: ${{ needs.prepare.outputs.review_artifact_id }}", self.approve)
        self.assertIn("path: ${{ runner.temp }}/migration-release-review\n", self.approve)
        self.assertIn("merge-multiple: true\n", self.approve)
        self.assertIn("REVIEW_SHA256: ${{ needs.prepare.outputs.review_sha256 }}", self.approve)
        self.assertIn('--review-sha256 "$REVIEW_SHA256"', self.approve)
        self.assertIn('--review "$RUNNER_TEMP/migration-release-review/migration-review.json"', self.approve)
        self.assertIn("name: approved-migration-release\n", self.approve)
        self.assertIn("            migration-release.json\n            migration-release.sigstore.json\n", self.approve)
        self.assertEqual(self.source.count("          if-no-files-found: error\n"), 2)

    def test_actions_are_pinned_and_commands_cannot_apply_a_release(self):
        uses = re.findall(r"^      - uses: (\S+)", self.source, re.MULTILINE)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
        self.assertEqual(self.source.count("          persist-credentials: false\n"), 2)
        commands = "\n".join(run_blocks(self.source))
        self.assertNotRegex(commands, r"\b(?:ssh|scp|sftp)\b")
        self.assertNotRegex(commands, r"\b(?:alembic|pg_restore|psql)\b")
        self.assertNotRegex(commands, r"\brelease_ops\.py\s+apply\b")
        self.assertNotRegex(commands, r"\bdocker\s+(?:compose|stack)\b")
        secret_names = re.findall(r"secrets\.([A-Za-z0-9_]+)", self.source)
        self.assertEqual(set(secret_names), {"GITHUB_TOKEN"})


if __name__ == "__main__":
    unittest.main()
