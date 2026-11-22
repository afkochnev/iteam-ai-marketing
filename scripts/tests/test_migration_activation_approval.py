"""Exercise the separate activation authority using real JSON and ZIP evidence."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_activation_approval as approval
import migration_activation_contract as contract
import migration_release_approval as legacy
import release_ops as ops
from test_migration_activation_contract import CONTROLLER_TREE, proof as fixture_proof, request as fixture_request
from test_migration_release_approval import (
    APPROVAL_ARTIFACT, APPROVAL_RUN, REHEARSAL_ARTIFACT, REHEARSAL_RUN, TRUSTED_HEAD,
    Evidence as MigrationEvidence, archive, iso,
)

OPERATION_ID = "d" * 32
RUNTIME_IDENTITY = "sha256:" + "e" * 64


class Evidence(MigrationEvidence):
    """Reuse the v1 transport fixtures without giving v1 evidence new authority."""

    def __init__(self):
        super().__init__()
        self.request = fixture_request()
        self.proof = fixture_proof(self.request)
        self.proof["created_at"] = iso(self.now - timedelta(minutes=11))
        self.proof["migration_rehearsal"]["created_at"] = iso(self.now - timedelta(minutes=12))
        self.controller = {"sha": TRUSTED_HEAD, "tree": {"sha": CONTROLLER_TREE}}
        self.runs[APPROVAL_RUN]["path"] = ".github/workflows/" + approval.WORKFLOW
        self.publish_proof()
        raw = approval.encode(self.request)
        self.request_record = {
            "type": "file", "path": approval.REQUEST_PATH, "encoding": "base64",
            "content": base64.b64encode(raw).decode(), "size": len(raw),
            "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        }

    def publish_proof(self):
        self.proof_data = approval.encode(self.proof)
        self.set_archive(REHEARSAL_ARTIFACT, REHEARSAL_RUN, approval.PROOF_ARTIFACT,
                         archive([(approval.PROOF_FILE, self.proof_data)]))

    def gh(self, endpoint):
        if endpoint == f"contents/{approval.REQUEST_PATH}?ref={TRUSTED_HEAD}":
            value = self.request_record
        elif endpoint == "git/commits/" + TRUSTED_HEAD:
            value = self.controller
        else:
            return super().gh(endpoint)
        self.calls.append(endpoint)
        value = copy.deepcopy(value)
        return self.after_get(endpoint, self.calls.count(endpoint), value) if self.after_get else value

    def parameters(self):
        request = self.request["migration_request"]
        return {
            "rehearsal_run_id": REHEARSAL_RUN, "rehearsal_artifact_id": REHEARSAL_ARTIFACT,
            "proof_sha256": approval.sha256(self.proof_data),
            "expected_source_release": request["source_release_sha"],
            "expected_source_schema": request["source_schema_head"],
            "target_build_run_id": request["target_build_run_id"], "cosign": "test-cosign",
            "operation_id": OPERATION_ID, "expected_runtime_identity_sha256": RUNTIME_IDENTITY,
        }

    def rehearsal_parameters(self):
        return {key: value for key, value in self.parameters().items()
                if key not in {"operation_id", "expected_runtime_identity_sha256"}}

    def mocks(self, stack):
        stack.enter_context(patch.object(ops, "gh", side_effect=self.gh))
        stack.enter_context(patch.object(legacy, "download_archive", side_effect=self.download))
        accepted = stack.enter_context(patch.object(ops, "accepted"))
        images = stack.enter_context(patch.object(approval.migration, "verify_images"))
        admission = stack.enter_context(patch.object(approval, "verify_admission_image"))
        return accepted, images, admission

    def review(self):
        with ExitStack() as stack:
            self.mocks(stack)
            return approval.prepare(**self.parameters())

    def receipt(self, review=None):
        review = self.review() if review is None else review
        issued = self.now - timedelta(minutes=4)
        return {
            "version": 2, "receipt_type": approval.RECEIPT_TYPE, "repository": approval.REPO,
            "authorization": approval.AUTHORIZATION, "approval_run_id": APPROVAL_RUN,
            "approval_attempt": 1, "approval_workflow_sha": TRUSTED_HEAD,
            "review_sha256": approval.sha256(approval.encode(review)), "review": review,
            "issued_at": iso(issued), "expires_at": iso(issued + timedelta(hours=1)),
        }

    def approved_archive(self, receipt=None):
        value = self.receipt() if receipt is None else receipt
        self.set_archive(APPROVAL_ARTIFACT, APPROVAL_RUN, approval.APPROVED_ARTIFACT, archive([
            (approval.RECEIPT_FILE, approval.encode(value)),
            (approval.BUNDLE_FILE, b'{"verificationMaterial":"test"}'),
        ]))
        return value

    def environment(self):
        return {**super().environment(), "GITHUB_WORKFLOW_REF":
                f"{approval.REPO}/.github/workflows/{approval.WORKFLOW}@refs/heads/main"}


class PrepareTests(unittest.TestCase):
    def test_review_binds_operation_runtime_policy_and_combined_signed_proof(self):
        evidence = Evidence()
        with ExitStack() as stack:
            accepted, images, admission = evidence.mocks(stack)
            review = approval.prepare(**evidence.parameters())
        accepted.assert_called_once_with(TRUSTED_HEAD)
        images.assert_called_once_with(evidence.request["migration_request"], "test-cosign")
        admission.assert_called_once_with(evidence.request)
        self.assertEqual(review["version"], 2)
        self.assertEqual(review["operation_id"], OPERATION_ID)
        self.assertEqual(review["expected_runtime_identity_sha256"], RUNTIME_IDENTITY)
        self.assertEqual(review["activation_policy"], contract.activation_policy())
        self.assertEqual(review["migration_manifest"]["version"], 2)
        self.assertEqual(review["activation_rehearsal"]["request_sha256"], contract.hash_json(evidence.request))
        self.assertEqual(review["migration_manifest"]["rehearsal_proof"]["proof_sha256"], approval.sha256(evidence.proof_data))
        for endpoint in (f"actions/runs/{REHEARSAL_RUN}", f"actions/artifacts/{REHEARSAL_ARTIFACT}"):
            self.assertEqual(evidence.calls.count(endpoint), 2)

    def test_read_only_planner_can_build_the_same_manifest_without_an_approval(self):
        evidence = Evidence()
        expected = evidence.review()["migration_manifest"]
        evidence.calls.clear()
        with ExitStack() as stack:
            evidence.mocks(stack)
            gates = stack.enter_context(patch.object(ops, "gates"))
            request, proof, bindings = approval.load_rehearsal(**evidence.rehearsal_parameters())
            manifest = approval.build_migration_manifest(request, bindings)
        self.assertEqual(manifest, expected)
        self.assertEqual(proof["authorization"], contract.FIXTURE_AUTHORITY)
        gates.assert_not_called()
        self.assertFalse(any(str(APPROVAL_RUN) in endpoint or endpoint.endswith("/approvals") for endpoint in evidence.calls))

    def test_malformed_runtime_binding_fails_before_reading_remote_evidence(self):
        for field, value in (
            ("operation_id", "d" * 31), ("operation_id", "D" * 32), ("operation_id", None),
            ("expected_runtime_identity_sha256", "e" * 64),
            ("expected_runtime_identity_sha256", True),
        ):
            evidence = Evidence()
            with self.subTest(field=field, value=value), patch.object(approval, "load_rehearsal") as load:
                with self.assertRaisesRegex(ops.ReleaseError, "INVALID_ACTIVATION_RUNTIME_BINDING"):
                    approval.prepare(**{**evidence.parameters(), field: value})
                load.assert_not_called()

    def test_only_successful_first_attempt_main_ci_can_supply_activation_evidence(self):
        for key, value in (
            ("path", ".github/workflows/approve-migration-release.yml"), ("event", "pull_request"),
            ("head_branch", "feature"), ("run_attempt", 2), ("run_attempt", True),
            ("status", "in_progress"), ("conclusion", "failure"),
            ("head_repository", {"id": 999, "full_name": approval.REPO}),
        ):
            evidence = Evidence()
            evidence.runs[REHEARSAL_RUN][key] = value
            with self.subTest(field=key), ExitStack() as stack:
                _, images, admission = evidence.mocks(stack)
                with self.assertRaises(ops.ReleaseError):
                    approval.prepare(**evidence.parameters())
                images.assert_not_called()
                admission.assert_not_called()

    def test_old_artifact_proof_and_controller_substitution_cannot_gain_activation_authority(self):
        for changed in ("artifact", "proof", "controller", "tree", "commit"):
            evidence = Evidence()
            if changed == "artifact":
                evidence.artifacts[REHEARSAL_ARTIFACT]["name"] = legacy.PROOF_ARTIFACT
            elif changed == "proof":
                evidence.proof = evidence.proof["migration_rehearsal"]
                evidence.publish_proof()
            elif changed == "controller":
                evidence.proof["controller_sha"] = "c" * 40
                evidence.publish_proof()
            elif changed == "tree":
                evidence.controller["tree"]["sha"] = "c" * 40
            else:
                evidence.controller["sha"] = "c" * 40
            with self.subTest(changed=changed), ExitStack() as stack:
                _, images, admission = evidence.mocks(stack)
                with self.assertRaises(ops.ReleaseError):
                    approval.prepare(**evidence.parameters())
                images.assert_not_called()
                admission.assert_not_called()

    def test_raw_proof_hash_and_content_addressed_request_are_rechecked(self):
        for changed in ("raw", "request-blob", "artifact-after", "run-after", "target-build-after"):
            evidence = Evidence()
            if changed == "raw":
                evidence.set_archive(REHEARSAL_ARTIFACT, REHEARSAL_RUN, approval.PROOF_ARTIFACT,
                                     archive([(approval.PROOF_FILE, json.dumps(evidence.proof).encode())]))
            elif changed == "request-blob":
                evidence.request_record["sha"] = "f" * 40

            def mutate(endpoint, count, value):
                if count == 2 and changed == "artifact-after" and endpoint == f"actions/artifacts/{REHEARSAL_ARTIFACT}":
                    value["digest"] = "sha256:" + "f" * 64
                if count == 2 and changed == "run-after" and endpoint == f"actions/runs/{REHEARSAL_RUN}":
                    value["run_attempt"] = 2
                return value

            evidence.after_get = mutate
            with self.subTest(changed=changed), ExitStack() as stack:
                _, images, _ = evidence.mocks(stack)
                if changed == "target-build-after":
                    images.side_effect = lambda *_args: evidence.runs[evidence.request["migration_request"]["target_build_run_id"]].update(run_attempt=2)
                with self.assertRaises(ops.ReleaseError):
                    approval.prepare(**evidence.parameters())

    def test_policy_and_scope_cannot_be_changed_in_a_well_formed_review(self):
        original = Evidence().review()
        mutations = [
            (("activation_policy", "openai_requests_authorized"), False),
            (("activation_policy", "publication_requests_authorized"), True),
            (("activation_policy", "automatic_retry"), True),
            (("activation_rehearsal", "admission_protocol"), True),
            (("activation_rehearsal", "target_release_sha"), "c" * 40),
            (("migration_manifest", "version"), 1),
            (("version",), 1),
        ]
        for path, value in mutations:
            review = copy.deepcopy(original)
            target = review
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises((ops.ReleaseError, approval.ManifestContractError)):
                approval.validate_review(review)

    def test_review_explains_openai_authority_and_success_restart_policies(self):
        summary = approval.review_summary(Evidence().review())
        for text in ("business writes, queue claims and OpenAI requests", "Publication and metrics requests remain forbidden",
                     "same live operation", "at most one hour", "restores the exact bound service restart policies"):
            self.assertIn(text, summary)


class AdmissionImageTests(unittest.TestCase):
    def test_signed_target_must_independently_report_passive_protocol_one(self):
        request = fixture_request()
        expected = {"admission_protocol": 1, "admission_mode": "passive", "business_ready": False,
                    "build_sha": request["migration_request"]["target_release_sha"]}
        with patch.object(ops, "command", return_value=json.dumps(expected)) as command:
            approval.verify_admission_image(request)
        argv = command.call_args.args[0]
        self.assertEqual(argv[:4], ["docker", "run", "--rm", "--pull=never"])
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertIn("--read-only", argv)
        self.assertIn("no-new-privileges", argv)
        self.assertIn("APP_ENV=maintenance", argv)
        self.assertIn("RUNTIME_ADMISSION_MODE=passive", argv)
        self.assertIn(request["migration_request"]["target_images"]["backend"], argv)
        for field, value in (("admission_protocol", True), ("admission_protocol", 0),
                             ("admission_mode", "active"), ("business_ready", True),
                             ("build_sha", "f" * 40)):
            with self.subTest(field=field, value=value), patch.object(ops, "command", return_value=json.dumps({**expected, field: value})):
                with self.assertRaisesRegex(ops.ReleaseError, "ACTIVATION_ADMISSION_IMAGE_MISMATCH"):
                    approval.verify_admission_image(request)


class ReceiptWindowTests(unittest.TestCase):
    def test_signed_window_is_bounded_and_consumption_must_still_be_fresh(self):
        evidence = Evidence()
        receipt = evidence.receipt()
        approval.validate_receipt(receipt, evidence.runs[APPROVAL_RUN])
        approval.assert_receipt_fresh(receipt, now=evidence.now)
        issued = datetime.fromisoformat(receipt["issued_at"])
        expires = datetime.fromisoformat(receipt["expires_at"])
        for now in (issued - timedelta(microseconds=1), expires + timedelta(microseconds=1)):
            with self.subTest(now=now), self.assertRaisesRegex(ops.ReleaseError, "ACTIVATION_APPROVAL_EXPIRED"):
                approval.assert_receipt_fresh(receipt, now=now)
        for duration in (-1, 0, 3601):
            changed = {**receipt, "expires_at": iso(issued + timedelta(seconds=duration))}
            with self.subTest(duration=duration), self.assertRaisesRegex(ops.ReleaseError, "INVALID_ACTIVATION_APPROVAL_WINDOW"):
                approval.validate_receipt(changed, evidence.runs[APPROVAL_RUN])

    def test_issuance_must_be_inside_the_signing_run_and_times_must_be_aware(self):
        evidence = Evidence()
        receipt = evidence.receipt()
        for issued in (evidence.now - timedelta(minutes=9), evidence.now - timedelta(minutes=2)):
            changed = {**receipt, "issued_at": iso(issued), "expires_at": iso(issued + timedelta(hours=1))}
            with self.subTest(issued=issued), self.assertRaisesRegex(ops.ReleaseError, "INVALID_ACTIVATION_APPROVAL_WINDOW"):
                approval.validate_receipt(changed, evidence.runs[APPROVAL_RUN])
        for field in ("issued_at", "expires_at"):
            changed = {**receipt, field: "2026-11-22T10:00:00"}
            with self.subTest(field=field), self.assertRaisesRegex(ops.ReleaseError, "INVALID_ACTIVATION_APPROVAL_WINDOW"):
                approval.assert_receipt_fresh(changed, now=evidence.now)
        with self.assertRaisesRegex(ops.ReleaseError, "INVALID_ACTIVATION_APPROVAL_WINDOW"):
            approval.assert_receipt_fresh(receipt, now=datetime.now())

    def test_v1_receipts_and_disposable_authority_are_not_production_receipts(self):
        evidence = Evidence()
        for receipt in (MigrationEvidence().receipt(), {**evidence.receipt(), "authorization": contract.FIXTURE_AUTHORITY},
                        {**evidence.receipt(), "version": 1}, {**evidence.receipt(), "approval_attempt": True}):
            with self.subTest(receipt_type=receipt.get("receipt_type")), self.assertRaisesRegex(ops.ReleaseError, "INVALID_ACTIVATION_RECEIPT"):
                approval.validate_receipt(receipt, evidence.runs[APPROVAL_RUN])


class SealTests(unittest.TestCase):
    def seal(self, evidence, data, *, environment=None, expected_hash=None):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "review.json"
            path.write_bytes(data)
            evidence.runs[APPROVAL_RUN].update(status="in_progress", conclusion=None)
            evidence.mocks(stack)
            stack.enter_context(patch.dict(os.environ, environment or evidence.environment(), clear=True))
            return approval.seal(path, expected_hash or approval.sha256(data), "test-cosign")

    def test_seal_preserves_exact_review_and_issues_only_the_bounded_v2_authority(self):
        evidence = Evidence()
        review = evidence.review()
        receipt = self.seal(evidence, approval.encode(review))
        self.assertEqual(receipt["review"], review)
        self.assertEqual(receipt["authorization"], approval.AUTHORIZATION)
        self.assertEqual(receipt["version"], 2)
        self.assertEqual(receipt["approval_run_id"], APPROVAL_RUN)
        self.assertEqual(receipt["approval_workflow_sha"], TRUSTED_HEAD)
        self.assertEqual((datetime.fromisoformat(receipt["expires_at"]) - datetime.fromisoformat(receipt["issued_at"])).total_seconds(), 3600)
        approval.assert_receipt_fresh(receipt)

    def test_old_workflow_context_noncanonical_bytes_and_changed_review_cannot_be_sealed(self):
        for changed in ("old-workflow", "rerun", "wrong-ref", "hash", "noncanonical"):
            evidence = Evidence()
            review = evidence.review()
            raw = approval.encode(review)
            env = evidence.environment()
            expected_hash = None
            if changed == "old-workflow":
                env["GITHUB_WORKFLOW_REF"] = f"{approval.REPO}/.github/workflows/{legacy.WORKFLOW}@refs/heads/main"
            elif changed == "rerun":
                env["GITHUB_RUN_ATTEMPT"] = "2"
            elif changed == "wrong-ref":
                env["GITHUB_REF"] = "refs/heads/feature"
            elif changed == "hash":
                expected_hash = "sha256:" + "f" * 64
            else:
                raw = json.dumps(review).encode()
            with self.subTest(changed=changed), self.assertRaises(ops.ReleaseError):
                self.seal(evidence, raw, environment=env, expected_hash=expected_hash)


class VerifyApprovedTests(unittest.TestCase):
    def verify(self, evidence, *, signature_error=None):
        with ExitStack() as stack:
            accepted, images, admission = evidence.mocks(stack)
            gates = stack.enter_context(patch.object(ops, "gates"))
            command = stack.enter_context(patch.object(ops, "command", return_value="", side_effect=signature_error))
            result = approval.verify_approved(APPROVAL_RUN, APPROVAL_ARTIFACT, "test-cosign")
            return result, gates, command, accepted, images, admission

    def test_signature_identity_human_approval_and_rebuilt_evidence_all_bind_v2(self):
        evidence = Evidence()
        receipt = evidence.approved_archive()
        result, gates, command, accepted, images, admission = self.verify(evidence)
        self.assertEqual(result, receipt)
        gates.assert_called_once_with()
        argv = command.call_args.args[0]
        self.assertEqual(argv[:2], ["test-cosign", "verify-blob"])
        self.assertEqual(argv[argv.index("--certificate-identity") + 1],
                         f"https://github.com/{approval.REPO}/.github/workflows/{approval.WORKFLOW}@refs/heads/main")
        self.assertEqual(argv[argv.index("--certificate-oidc-issuer") + 1], "https://token.actions.githubusercontent.com")
        images.assert_called_once_with(evidence.request["migration_request"], "test-cosign")
        admission.assert_called_once_with(evidence.request)
        self.assertEqual(accepted.call_count, 2)
        self.assertIn(f"actions/runs/{APPROVAL_RUN}/approvals", evidence.calls)

    def test_named_human_production_gate_and_correct_signature_are_required(self):
        for changed in ("missing-human", "bot", "rejection", "wrong-workflow", "signature"):
            evidence = Evidence()
            evidence.approved_archive()
            if changed == "missing-human":
                evidence.history = []
            elif changed == "bot":
                evidence.history[0]["user"]["type"] = "Bot"
            elif changed == "rejection":
                evidence.history[0]["state"] = "rejected"
            elif changed == "wrong-workflow":
                evidence.runs[APPROVAL_RUN]["path"] = ".github/workflows/" + legacy.WORKFLOW
            error = ops.ReleaseError("EXTERNAL_COMMAND_FAILED") if changed == "signature" else None
            with self.subTest(changed=changed), self.assertRaises(ops.ReleaseError):
                self.verify(evidence, signature_error=error)

    def test_signed_review_is_rebuilt_and_cannot_change_request_or_images(self):
        for changed in ("request", "images", "review-hash"):
            evidence = Evidence()
            receipt = evidence.receipt()
            review = receipt["review"]
            if changed == "request":
                review["activation_rehearsal"]["request_sha256"] = "sha256:" + "f" * 64
            elif changed == "images":
                image = f"ghcr.io/{approval.REPO}-backend@sha256:" + "f" * 64
                review["migration_manifest"]["source_images"]["backend"] = image
                review["migration_manifest"]["rehearsal_proof"]["source_images"]["backend"] = image
                review["activation_rehearsal"]["source_images"]["backend"] = image
            receipt["review_sha256"] = approval.sha256(approval.encode(review)) if changed != "review-hash" else "sha256:" + "f" * 64
            evidence.approved_archive(receipt)
            with self.subTest(changed=changed), self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_REVIEW_(?:CHANGED|HASH_MISMATCH)"):
                self.verify(evidence)

    def test_receipt_expiring_during_revalidation_is_rejected_before_returning_authority(self):
        evidence = Evidence()
        evidence.approved_archive()
        with patch.object(approval, "assert_receipt_fresh", side_effect=[None, ops.ReleaseError("ACTIVATION_APPROVAL_EXPIRED")]) as freshness:
            with self.assertRaisesRegex(ops.ReleaseError, "ACTIVATION_APPROVAL_EXPIRED"):
                self.verify(evidence)
        self.assertEqual(freshness.call_count, 2)
        self.assertEqual(evidence.calls.count(f"actions/artifacts/{APPROVAL_ARTIFACT}"), 2)

    def test_changed_approval_and_artifact_after_revalidation_are_rejected(self):
        for changed in ("run", "artifact"):
            evidence = Evidence()
            evidence.approved_archive()

            def mutate(endpoint, count, value):
                if count == 2 and changed == "run" and endpoint == f"actions/runs/{APPROVAL_RUN}":
                    value["run_attempt"] = 2
                if count == 2 and changed == "artifact" and endpoint == f"actions/artifacts/{APPROVAL_ARTIFACT}":
                    value["digest"] = "sha256:" + "f" * 64
                return value

            evidence.after_get = mutate
            with self.subTest(changed=changed), self.assertRaises(ops.ReleaseError):
                self.verify(evidence)

    def test_activation_artifact_is_rejected_by_the_legacy_verifier(self):
        evidence = Evidence()
        evidence.approved_archive()
        with ExitStack() as stack:
            evidence.mocks(stack)
            stack.enter_context(patch.object(ops, "gates"))
            command = stack.enter_context(patch.object(ops, "command"))
            with self.assertRaisesRegex(ops.ReleaseError, "UNTRUSTED_MIGRATION_WORKFLOW"):
                legacy.verify_approved(APPROVAL_RUN, APPROVAL_ARTIFACT, "test-cosign")
            command.assert_not_called()


class ApprovalCliTests(unittest.TestCase):
    def test_existing_output_and_external_failures_publish_no_authority_or_sensitive_details(self):
        for failure in ("exists", ops.ReleaseError("credential=do-not-print"),
                        subprocess.TimeoutExpired(["tool", "credential=do-not-print"], 600, output="private-output")):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "receipt.json"
                if failure == "exists":
                    output.write_text("preserve")
                with (
                    patch.object(sys, "argv", ["migration_activation_approval", "verify-approved", "--approval-run", "301",
                                               "--approval-artifact", "401", "--output", str(output)]),
                    patch.object(approval, "verify_approved", side_effect=None if failure == "exists" else failure) as verify,
                    redirect_stdout(io.StringIO()) as stdout,
                    self.assertRaises(SystemExit),
                ):
                    approval.main()
                if failure == "exists":
                    verify.assert_not_called()
                    self.assertEqual(output.read_text(), "preserve")
                else:
                    self.assertFalse(output.exists())
                self.assertNotIn("do-not-print", stdout.getvalue())
                self.assertNotIn("private-output", stdout.getvalue())
                self.assertEqual(json.loads(stdout.getvalue())["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
