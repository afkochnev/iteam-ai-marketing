"""Live drill diagnostics never expose exception payloads or command details."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import migration_release_drill as drill
from test_migration_release_drill import request
from test_migration_release_drill_flow import RehearsalHarness


class MigrationReleaseDrillDiagnosticsTests(unittest.TestCase):
    def run_cli(self, implementation, *, fails=True):
        with (
            patch.object(sys, "argv", [
                "migration_release_drill", "--request", "request.json",
                "--output", "proof.json",
            ]),
            patch.object(drill, "load", return_value=request()),
            patch.object(drill, "rehearse", side_effect=implementation),
            redirect_stdout(io.StringIO()) as stdout,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            if fails:
                with self.assertRaises(SystemExit) as caught:
                    drill.main()
                self.assertEqual(caught.exception.code, 1)
            else:
                drill.main()
        # stdout remains exactly one machine-readable result, even with progress.
        return json.loads(stdout.getvalue()), stderr.getvalue()

    def test_unknown_exception_and_release_error_text_is_redacted_on_both_streams(self):
        for error in (
            KeyError("access_token=secret"),
            ValueError("postgresql://secret@host/database"),
            drill.ReleaseError("EXTERNAL_COMMAND_FAILED access_token=secret"),
            subprocess.TimeoutExpired(["pg_restore", "access_token=secret"], 300),
        ):
            with self.subTest(error_type=type(error).__name__):
                def fail(*args, progress):
                    progress("restore")
                    raise error

                result, stderr = self.run_cli(fail)
                self.assertEqual(result, {
                    "status": "FAIL", "automatic_retry": False,
                    "phase": "restore", "error_code": "UNEXPECTED_MIGRATION_ERROR",
                })
                self.assertEqual(json.loads(stderr), {"status": "RUNNING", "phase": "restore"})
                self.assertNotIn("secret", json.dumps(result) + stderr)

    def test_known_codes_are_preserved_without_relaxing_exact_allowlist(self):
        for code in ("EXTERNAL_COMMAND_FAILED", "MIGRATION_FIXTURE_RESTORE_FAILED"):
            with self.subTest(code=code):
                def fail(*args, progress):
                    progress("restore")
                    raise drill.ReleaseError(code)

                result, _ = self.run_cli(fail)
                self.assertEqual(result["error_code"], code)

    def test_untrusted_phase_names_and_exception_phase_payloads_are_not_logged(self):
        def invalid_callback(*args, progress):
            progress("restore access_token=secret")

        result, stderr = self.run_cli(invalid_callback)
        self.assertEqual(result["phase"], "request_load")
        self.assertEqual(result["error_code"], "INVALID_REHEARSAL_DIAGNOSTIC_PHASE")
        self.assertEqual(stderr, "")
        for invalid in ("access_token=secret", ["access_token=secret"]):
            with self.subTest(phase_type=type(invalid).__name__):
                def fail(*args, progress):
                    progress("seed")
                    error = KeyError("access_token=secret")
                    error._migration_failure_phase = invalid
                    raise error

                result, stderr = self.run_cli(fail)
                self.assertEqual(result["phase"], "seed")
                self.assertNotIn("secret", json.dumps(result) + stderr)

    def test_success_progress_uses_stderr_and_final_result_uses_stdout(self):
        def succeed(*args, progress):
            progress("target_ready")
            progress("cleanup")
            progress("publish_proof")
            return {"status": "PASS"}

        result, stderr = self.run_cli(succeed, fails=False)
        self.assertEqual(result, {"status": "PASS"})
        self.assertEqual([json.loads(line)["phase"] for line in stderr.splitlines()], [
            "target_ready", "cleanup", "publish_proof",
        ])

    def test_real_orchestration_is_silent_without_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                RehearsalHarness(Path(directory) / "proof.json").run(request())
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")

    def test_successful_cleanup_preserves_original_failed_phase(self):
        phases = []
        original_rehearse = drill.rehearse
        with tempfile.TemporaryDirectory() as directory:
            harness = RehearsalHarness(Path(directory) / "proof.json", "restored_hash_mismatch")
            with patch.object(
                drill, "rehearse",
                side_effect=lambda *args: original_rehearse(*args, progress=phases.append),
            ):
                with self.assertRaises(drill.ReleaseError) as caught:
                    harness.run(request())
        self.assertEqual(caught.exception._migration_failure_phase, "restore_checks")
        self.assertEqual(phases[-2:], ["cleanup", "temporary_cleanup"])

        def fail_after_cleanup(*args, progress):
            for phase in phases:
                progress(phase)
            raise caught.exception

        result, _ = self.run_cli(fail_after_cleanup)
        self.assertEqual(result["phase"], "restore_checks")
        self.assertEqual(result["error_code"], "BUSINESS_DATA_CHANGED_AFTER_RESTORE")

    def test_image_verification_emits_only_fixed_phase_names(self):
        value = request()
        phases = []
        with ExitStack() as stack:
            replacements = {
                "accepted": None,
                "run_info": {"head_sha": value["target_release_sha"], "run_attempt": 1},
                "target_build_record_images": value["target_images"],
                "verify_image": None, "command": "", "require_image_schema": None,
                "image_migration_inventory": value["migration_path"],
            }
            for name, result in replacements.items():
                stack.enter_context(patch.object(drill, name, return_value=result))
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
                drill.verify_images(value, "cosign")
                drill.verify_images(value, "cosign", phases.append)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertEqual(phases, [
            "source_accepted", "target_accepted", "target_build", "target_build_records",
            "source_backend_verify", "source_backend_pull",
            "source_frontend_verify", "source_frontend_pull",
            "target_backend_verify", "target_backend_pull",
            "target_frontend_verify", "target_frontend_pull",
            "source_schema_head", "target_schema_head", "migration_inventory",
        ])


if __name__ == "__main__":
    unittest.main()
