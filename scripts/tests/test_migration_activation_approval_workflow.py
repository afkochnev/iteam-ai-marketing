"""The activation signer stays separate, human-gated and unable to apply."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_migration_approval_workflow import run_blocks

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/approve-migration-activation.yml"


class ActivationApprovalWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text()
        cls.prepare, cls.approve = cls.source.split("  approve:\n", 1)

    def test_manual_main_first_attempt_and_human_gate_precede_signing(self):
        triggers = self.source.split("\non:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertEqual(re.findall(r"^  ([a-z_]+):", triggers, re.MULTILINE), ["workflow_dispatch"])
        guard = "if: github.ref == 'refs/heads/main' && github.run_attempt == 1"
        self.assertIn(guard, self.prepare)
        self.assertIn(guard, self.approve)
        self.assertIn("    needs: prepare\n", self.approve)
        self.assertIn("    environment: production\n", self.approve)
        self.assertIn("  group: production-release-approval\n", self.source)
        self.assertIn("  cancel-in-progress: false\n", self.source)
        self.assertNotIn("id-token: write", self.prepare)
        self.assertIn("      id-token: write\n", self.approve)
        self.assertLess(self.approve.index("migration_activation_approval.py seal"), self.approve.index("cosign sign-blob"))

    def test_all_eight_bound_inputs_use_required_strings_and_quoted_environment(self):
        fields = {
            "rehearsal_run_id": ("REHEARSAL_RUN", "--rehearsal-run"),
            "rehearsal_artifact_id": ("REHEARSAL_ARTIFACT", "--rehearsal-artifact"),
            "proof_sha256": ("PROOF_SHA256", "--proof-sha256"),
            "expected_current_release_sha": ("EXPECTED_SOURCE", "--expected-source-release"),
            "expected_current_schema_head": ("EXPECTED_SCHEMA", "--expected-source-schema"),
            "target_build_run_id": ("TARGET_BUILD", "--target-build-run"),
            "operation_id": ("OPERATION_ID", "--operation-id"),
            "expected_runtime_identity_sha256": ("EXPECTED_RUNTIME", "--expected-runtime-identity-sha256"),
        }
        inputs = self.source.split("    inputs:\n", 1)[1].split("\npermissions:\n", 1)[0]
        self.assertEqual(set(re.findall(r"^      ([a-z0-9_]+):", inputs, re.MULTILINE)), set(fields))
        self.assertEqual(inputs.count("        required: true\n"), len(fields))
        self.assertEqual(inputs.count("        type: string"), len(fields))
        for name, (variable, flag) in fields.items():
            with self.subTest(input=name):
                self.assertIn(variable + ": ${{ inputs." + name + " }}", self.prepare)
                self.assertIn(flag + ' "$' + variable + '"', self.prepare)
        for block in run_blocks(self.source):
            self.assertNotIn("${{", block)

    def test_signed_receipt_binds_the_specific_review_artifact_and_exact_bytes(self):
        self.assertIn("review_artifact_id: ${{ steps.review_artifact.outputs.artifact-id }}", self.prepare)
        self.assertIn("review_sha256: ${{ steps.review_hash.outputs.sha256 }}", self.prepare)
        self.assertIn("hashlib.sha256(Path('migration-activation-review.json').read_bytes())", self.prepare)
        self.assertIn("name: migration-activation-for-review\n", self.prepare)
        self.assertIn("artifact-ids: ${{ needs.prepare.outputs.review_artifact_id }}", self.approve)
        self.assertIn("path: ${{ runner.temp }}/migration-activation-review\n", self.approve)
        self.assertIn("merge-multiple: true\n", self.approve)
        self.assertIn("REVIEW_SHA256: ${{ needs.prepare.outputs.review_sha256 }}", self.approve)
        self.assertIn('--review-sha256 "$REVIEW_SHA256"', self.approve)
        self.assertIn('--review "$RUNNER_TEMP/migration-activation-review/migration-activation-review.json"', self.approve)
        self.assertIn("name: approved-migration-activation\n", self.approve)
        self.assertIn("cosign sign-blob --yes --bundle migration-activation.sigstore.json migration-activation.json", self.approve)
        self.assertIn("            migration-activation.json\n            migration-activation.sigstore.json\n", self.approve)
        self.assertEqual(self.source.count("          if-no-files-found: error\n"), 2)
        self.assertNotIn("approved-migration-release", self.source)
        self.assertNotIn("migration_release_approval.py", self.source)

    def test_pinned_workflow_has_no_production_transport_or_apply_path(self):
        uses = re.findall(r"^      - uses: (\S+)", self.source, re.MULTILINE)
        self.assertTrue(uses)
        for action in uses:
            self.assertRegex(action, r"^[A-Za-z0-9_./-]+@[0-9a-f]{40}$")
        self.assertEqual(self.source.count("          persist-credentials: false\n"), 2)
        commands = "\n".join(run_blocks(self.source))
        self.assertNotRegex(commands, r"\b(?:ssh|scp|sftp|alembic|pg_restore|psql)\b")
        self.assertNotIn("migration_activation_apply.py", commands)
        self.assertNotIn("migration_release_apply.py", commands)
        self.assertNotRegex(commands, r"\brelease_ops\.py\s+apply\b")
        self.assertNotRegex(commands, r"\bdocker\s+(?:compose|stack)\b")
        self.assertEqual(set(re.findall(r"secrets\.([A-Za-z0-9_]+)", self.source)), {"GITHUB_TOKEN"})


if __name__ == "__main__":
    unittest.main()
