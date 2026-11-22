"""Exercise the migration approval trust boundary without network or Docker."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from contextlib import ExitStack, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_release_approval as approval
import release_ops as ops
from release_manifest import ManifestContractError
from test_migration_rehearsal_proof import proof as fixture_proof
from test_migration_rehearsal_proof import request as fixture_request

REHEARSAL_RUN = 101
REHEARSAL_ARTIFACT = 201
APPROVAL_RUN = 301
APPROVAL_ARTIFACT = 401
TRUSTED_HEAD = "a" * 40
REPOSITORY_ID = 123


def iso(value: datetime) -> str:
    return value.isoformat()


def archive(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> bytes:
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for name, data in entries:
                target.writestr(name, data)
    return output.getvalue()


class Evidence:
    """Real JSON/ZIP evidence; only GitHub and image inspection are substituted."""

    def __init__(self):
        self.now = datetime.now(UTC)
        self.request = fixture_request()
        self.proof = fixture_proof(self.request)
        self.proof["created_at"] = iso(self.now - timedelta(minutes=12))
        self.proof_data = approval.encode(self.proof)
        self.runs = {
            REHEARSAL_RUN: self.run(REHEARSAL_RUN, "ci.yml", "push", minutes=15),
            APPROVAL_RUN: self.run(APPROVAL_RUN, approval.WORKFLOW, "workflow_dispatch", minutes=8),
        }
        build = self.run(self.request["target_build_run_id"], "release-images.yml", "workflow_run", minutes=30)
        build["head_sha"] = self.request["target_release_sha"]
        build["run_attempt"] = self.request["target_build_attempt"]
        self.runs[build["id"]] = build
        self.artifacts = {}
        self.archives = {}
        self.set_archive(REHEARSAL_ARTIFACT, REHEARSAL_RUN, approval.PROOF_ARTIFACT,
                         archive([(approval.PROOF_FILE, self.proof_data)]))
        raw = approval.encode(self.request)
        self.request_record = {
            "type": "file", "path": approval.REQUEST_PATH, "encoding": "base64",
            "content": base64.b64encode(raw).decode(), "size": len(raw),
            "sha": hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest(),
        }
        self.history = [{
            "state": "approved", "environments": [{"name": "production"}],
            "user": {"login": ops.REVIEWER, "type": "User"},
        }]
        self.calls = []
        self.after_get = None

    def run(self, run_id, workflow, event, *, minutes):
        repo = {"id": REPOSITORY_ID, "full_name": approval.REPO}
        return {
            "id": run_id, "path": ".github/workflows/" + workflow,
            "event": event, "head_branch": "main", "head_sha": TRUSTED_HEAD,
            "repository": copy.deepcopy(repo), "head_repository": copy.deepcopy(repo),
            "run_attempt": 1, "status": "completed", "conclusion": "success",
            "run_started_at": iso(self.now - timedelta(minutes=minutes)),
            "updated_at": iso(self.now - timedelta(minutes=minutes - 5)),
        }

    def set_archive(self, artifact_id, run_id, name, data):
        run = self.runs[run_id]
        finish = datetime.fromisoformat(run["updated_at"])
        self.archives[artifact_id] = data
        self.artifacts[artifact_id] = {
            "id": artifact_id, "name": name, "digest": approval.sha256(data),
            "size_in_bytes": len(data), "expired": False,
            "created_at": iso(finish - timedelta(minutes=1)),
            "updated_at": iso(finish - timedelta(seconds=30)),
            "expires_at": iso(self.now + timedelta(days=89)),
            "workflow_run": {
                "id": run_id, "head_sha": TRUSTED_HEAD, "head_branch": "main",
                "repository_id": REPOSITORY_ID, "head_repository_id": REPOSITORY_ID,
            },
        }

    def gh(self, endpoint):
        self.calls.append(endpoint)
        if endpoint.endswith("/approvals"):
            result = self.history
        elif endpoint.startswith("actions/runs/"):
            result = self.runs[int(endpoint.rsplit("/", 1)[1])]
        elif endpoint.startswith("actions/artifacts/"):
            result = self.artifacts[int(endpoint.rsplit("/", 1)[1])]
        elif endpoint == f"contents/{approval.REQUEST_PATH}?ref={TRUSTED_HEAD}":
            result = self.request_record
        else:
            raise AssertionError("Unexpected API endpoint: " + endpoint)
        result = copy.deepcopy(result)
        if self.after_get:
            result = self.after_get(endpoint, self.calls.count(endpoint), result)
        return result

    def download(self, artifact_id, destination):
        destination.write_bytes(self.archives[artifact_id])

    def parameters(self):
        return {
            "rehearsal_run_id": REHEARSAL_RUN, "rehearsal_artifact_id": REHEARSAL_ARTIFACT,
            "proof_sha256": approval.sha256(self.proof_data),
            "expected_source_release": self.request["source_release_sha"],
            "expected_source_schema": self.request["source_schema_head"],
            "target_build_run_id": self.request["target_build_run_id"],
            "cosign": "test-cosign",
        }

    def mocks(self, stack):
        stack.enter_context(patch.object(ops, "gh", side_effect=self.gh))
        stack.enter_context(patch.object(approval, "download_archive", side_effect=self.download))
        accepted = stack.enter_context(patch.object(ops, "accepted"))
        images = stack.enter_context(patch.object(approval.drill, "verify_images"))
        return accepted, images

    def review(self):
        with ExitStack() as stack:
            self.mocks(stack)
            return approval.prepare(**self.parameters())

    def receipt(self, review=None):
        review = self.review() if review is None else review
        return {
            "version": 1, "receipt_type": approval.RECEIPT_TYPE, "repository": approval.REPO,
            "approval_run_id": APPROVAL_RUN, "approval_attempt": 1,
            "approval_workflow_sha": TRUSTED_HEAD,
            "review_sha256": approval.sha256(approval.encode(review)), "review": review,
        }

    def approved_archive(self, receipt=None):
        value = self.receipt() if receipt is None else receipt
        self.set_archive(APPROVAL_ARTIFACT, APPROVAL_RUN, approval.APPROVED_ARTIFACT, archive([
            (approval.RECEIPT_FILE, approval.encode(value)),
            (approval.BUNDLE_FILE, b'{"verificationMaterial":"test"}'),
        ]))
        return value

    def environment(self):
        return {
            "GITHUB_REPOSITORY": approval.REPO, "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_WORKFLOW_REF": f"{approval.REPO}/.github/workflows/{approval.WORKFLOW}@refs/heads/main",
            "GITHUB_RUN_ID": str(APPROVAL_RUN), "GITHUB_SHA": TRUSTED_HEAD,
        }


class PrepareTests(unittest.TestCase):
    def test_prepare_uses_exact_evidence_and_rechecks_it_after_image_inspection(self):
        evidence = Evidence()
        with ExitStack() as stack:
            accepted, images = evidence.mocks(stack)
            result = approval.prepare(**evidence.parameters())
        accepted.assert_called_once_with(TRUSTED_HEAD)
        images.assert_called_once_with(evidence.request, "test-cosign")
        manifest = result["migration_manifest"]
        self.assertEqual(manifest["version"], 2)
        self.assertEqual(manifest["release_type"], "MIGRATION")
        self.assertEqual(manifest["source_images"], evidence.request["source_images"])
        self.assertEqual(manifest["target_images"], evidence.request["target_images"])
        self.assertEqual(manifest["rehearsal_proof"]["artifact_id"], REHEARSAL_ARTIFACT)
        self.assertEqual(manifest["rehearsal_proof"]["proof_sha256"], approval.sha256(evidence.proof_data))
        self.assertEqual(evidence.calls.count(f"actions/runs/{REHEARSAL_RUN}"), 2)
        self.assertEqual(evidence.calls.count(f"actions/artifacts/{REHEARSAL_ARTIFACT}"), 2)

    def test_untrusted_or_unsuccessful_workflow_never_reaches_image_inspection(self):
        mutations = [
            ("id", True), ("id", REHEARSAL_RUN + 1), ("path", ".github/workflows/fake.yml"),
            ("event", "pull_request"), ("head_branch", "feature"), ("head_sha", "not-a-sha"),
            ("repository", {"id": REPOSITORY_ID, "full_name": "other/repo"}),
            ("head_repository", {"id": REPOSITORY_ID + 1, "full_name": approval.REPO}),
            ("head_repository", {"id": REPOSITORY_ID, "full_name": "fork/repo"}),
            ("run_attempt", 2), ("run_attempt", True), ("status", "in_progress"),
            ("conclusion", "failure"),
        ]
        for key, value in mutations:
            with self.subTest(field=key, value=value):
                evidence = Evidence()
                evidence.runs[REHEARSAL_RUN][key] = value
                with ExitStack() as stack:
                    _, images = evidence.mocks(stack)
                    with self.assertRaises(ops.ReleaseError):
                        approval.prepare(**evidence.parameters())
                    images.assert_not_called()

    def test_source_and_target_build_operator_inputs_must_match_the_trusted_request(self):
        for field, changed in (
            ("expected_source_release", "b" * 40),
            ("expected_source_schema", "20261121_0030"),
            ("target_build_run_id", 999),
            ("target_build_run_id", True),
        ):
            with self.subTest(field=field):
                evidence = Evidence()
                parameters = {**evidence.parameters(), field: changed}
                with ExitStack() as stack:
                    _, images = evidence.mocks(stack)
                    with self.assertRaises(ops.ReleaseError):
                        approval.prepare(**parameters)
                    images.assert_not_called()

    def test_proof_cannot_be_reused_for_another_image_or_build(self):
        for field, value in (
            ("target_build_run_id", 999), ("target_build_attempt", 2),
            ("source_release_sha", "b" * 40), ("target_release_sha", "c" * 40),
            ("source_images", {"backend": "fake", "frontend": "fake"}),
            ("target_images", {"backend": "fake", "frontend": "fake"}),
        ):
            with self.subTest(field=field):
                evidence = Evidence()
                evidence.proof[field] = value
                evidence.proof_data = approval.encode(evidence.proof)
                evidence.set_archive(REHEARSAL_ARTIFACT, REHEARSAL_RUN, approval.PROOF_ARTIFACT,
                                     archive([(approval.PROOF_FILE, evidence.proof_data)]))
                with ExitStack() as stack:
                    _, images = evidence.mocks(stack)
                    with self.assertRaisesRegex(ops.ReleaseError, "REHEARSAL_PROOF_SCOPE_MISMATCH"):
                        approval.prepare(**evidence.parameters())
                    images.assert_not_called()

    def test_image_verification_failure_prevents_a_review(self):
        evidence = Evidence()
        with ExitStack() as stack:
            _, images = evidence.mocks(stack)
            images.side_effect = ops.ReleaseError("TARGET_BUILD_RUN_MISMATCH")
            with self.assertRaisesRegex(ops.ReleaseError, "TARGET_BUILD_RUN_MISMATCH"):
                approval.prepare(**evidence.parameters())

    def test_target_build_rerun_or_failure_after_image_verification_blocks_review(self):
        for mutation in (
            {"run_attempt": 2}, {"run_attempt": True},
            {"status": "in_progress", "conclusion": None},
            {"conclusion": "failure"}, {"head_sha": "b" * 40},
            {"id": 999},
        ):
            evidence = Evidence()
            with self.subTest(mutation=mutation), ExitStack() as stack:
                _, images = evidence.mocks(stack)
                images.side_effect = lambda *_args: evidence.runs[evidence.request["target_build_run_id"]].update(mutation)
                with self.assertRaises(ops.ReleaseError):
                    approval.prepare(**evidence.parameters())

    def test_proof_hash_covers_original_bytes_not_reserialized_json(self):
        evidence = Evidence()
        different_bytes = json.dumps(evidence.proof, separators=(",", ":")).encode()
        evidence.set_archive(REHEARSAL_ARTIFACT, REHEARSAL_RUN, approval.PROOF_ARTIFACT,
                             archive([(approval.PROOF_FILE, different_bytes)]))
        with ExitStack() as stack:
            _, images = evidence.mocks(stack)
            with self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_PROOF_HASH_MISMATCH"):
                approval.prepare(**evidence.parameters())
            images.assert_not_called()

    def test_run_or_artifact_change_during_verification_blocks_prepare(self):
        for change in ("rerun", "head", "updated", "artifact"):
            with self.subTest(change=change):
                evidence = Evidence()

                def mutate(endpoint, count, value):
                    if count == 2 and endpoint == f"actions/runs/{REHEARSAL_RUN}":
                        if change == "rerun":
                            value["run_attempt"] = 2
                        elif change == "head":
                            value["head_sha"] = "b" * 40
                        elif change == "updated":
                            value["updated_at"] = iso(evidence.now - timedelta(minutes=9))
                    if count == 2 and change == "artifact" and endpoint == f"actions/artifacts/{REHEARSAL_ARTIFACT}":
                        value["digest"] = "sha256:" + "f" * 64
                    return value

                evidence.after_get = mutate
                with ExitStack() as stack:
                    evidence.mocks(stack)
                    with self.assertRaises(ops.ReleaseError):
                        approval.prepare(**evidence.parameters())

    def test_request_blob_is_content_addressed_and_cannot_be_replaced(self):
        for field, value in (
            ("sha", "f" * 40), ("path", "unreviewed/request.json"),
            ("size", 1), ("encoding", "utf-8"), ("content", "!not-base64!"),
        ):
            with self.subTest(field=field):
                evidence = Evidence()
                evidence.request_record[field] = value
                with ExitStack() as stack:
                    _, images = evidence.mocks(stack)
                    with self.assertRaisesRegex(ops.ReleaseError, "INVALID_TRUSTED_MIGRATION_REQUEST"):
                        approval.prepare(**evidence.parameters())
                    images.assert_not_called()


class ArtifactTests(unittest.TestCase):
    def test_metadata_binds_id_name_run_repository_and_validity_window(self):
        mutations = [
            (("id",), True), (("id",), 202), (("name",), "release-drill-proof"),
            (("workflow_run", "id"), 102), (("workflow_run", "head_sha"), "b" * 40),
            (("workflow_run", "head_branch"), "feature"),
            (("workflow_run", "repository_id"), 999),
            (("workflow_run", "head_repository_id"), 999),
            (("expired",), True), (("expired",), 0), (("size_in_bytes",), True),
            (("size_in_bytes",), approval.MAX_BYTES + 1), (("digest",), "bad"),
        ]
        for path, value in mutations:
            with self.subTest(path=path, value=value):
                evidence = Evidence()
                target = evidence.artifacts[REHEARSAL_ARTIFACT]
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] = value
                with patch.object(ops, "gh", side_effect=evidence.gh):
                    with self.assertRaises(ops.ReleaseError):
                        approval.artifact_metadata(REHEARSAL_ARTIFACT, evidence.runs[REHEARSAL_RUN], approval.PROOF_ARTIFACT)
        for field, offset in (("expires_at", -1), ("created_at", -16), ("updated_at", -9)):
            evidence = Evidence()
            evidence.artifacts[REHEARSAL_ARTIFACT][field] = iso(evidence.now + timedelta(minutes=offset))
            with self.subTest(field=field), patch.object(ops, "gh", side_effect=evidence.gh):
                with self.assertRaises(ops.ReleaseError):
                    approval.artifact_metadata(REHEARSAL_ARTIFACT, evidence.runs[REHEARSAL_RUN], approval.PROOF_ARTIFACT)

    def test_downloaded_zip_must_match_the_server_digest_and_size(self):
        evidence = Evidence()
        data = evidence.archives[REHEARSAL_ARTIFACT]
        for changed in (data + b"extra", data[:-1] + bytes([data[-1] ^ 1])):
            with self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_ARTIFACT_DIGEST_MISMATCH"):
                approval.unpack_archive(changed, evidence.artifacts[REHEARSAL_ARTIFACT], {approval.PROOF_FILE})

    def test_hostile_archives_are_rejected_even_with_matching_digest(self):
        symlink = zipfile.ZipInfo(approval.PROOF_FILE)
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        examples = [
            [("../" + approval.PROOF_FILE, b"{}")],
            [("/" + approval.PROOF_FILE, b"{}")],
            [(approval.PROOF_FILE, b"{}"), (approval.PROOF_FILE, b"{}")],
            [(approval.PROOF_FILE, b"{}"), ("extra.json", b"{}")],
            [(symlink, b"/etc/passwd")],
            [(approval.PROOF_FILE, b"")],
            [(approval.PROOF_FILE, b"a" * (approval.MAX_BYTES + 1))],
        ]
        for entries in examples:
            data = archive(entries)
            metadata = {"digest": approval.sha256(data), "size_in_bytes": len(data)}
            with self.subTest(names=[str(item[0]) for item in entries]):
                with self.assertRaisesRegex(ops.ReleaseError, "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE"):
                    approval.unpack_archive(data, metadata, {approval.PROOF_FILE})
        data = b"not a ZIP"
        with self.assertRaisesRegex(ops.ReleaseError, "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE"):
            approval.unpack_archive(data, {"digest": approval.sha256(data), "size_in_bytes": len(data)}, {approval.PROOF_FILE})

    def test_json_rejects_duplicate_keys_and_nonfinite_constants(self):
        for data in (
            b'{"a": 1, "a": 2}', b'{"outer":{"a":1,"a":2}}',
            b'{"number":NaN}', b'{"number":Infinity}', b'{"number":-Infinity}',
            b'{"number":1e9999}', b'{"number":-1e9999}',
            b"\xff", b"", b"[",
        ):
            with self.subTest(data=data), self.assertRaisesRegex(ops.ReleaseError, "INVALID_MIGRATION_APPROVAL_JSON"):
                approval.parse_json(data)

    def test_corrupt_compressed_payload_fails_closed_with_matching_zip_digest(self):
        raw = archive([(approval.PROOF_FILE, b"a migration proof payload " * 100)])
        with zipfile.ZipFile(io.BytesIO(raw)) as source:
            offset = source.infolist()[0].header_offset
        name_length, extra_length = struct.unpack_from("<HH", raw, offset + 26)
        payload_start = offset + 30 + name_length + extra_length
        changed = raw[:payload_start] + b"\xff\xff\xff" + raw[payload_start + 3:]
        metadata = {"digest": approval.sha256(changed), "size_in_bytes": len(changed)}
        with self.assertRaisesRegex(ops.ReleaseError, "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE"):
            approval.unpack_archive(changed, metadata, {approval.PROOF_FILE})


class SealTests(unittest.TestCase):
    def seal(self, evidence, data, *, environment=None, expected_hash=None, mutate_run=None):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = Path(directory) / "review.json"
            path.write_bytes(data)
            evidence.runs[APPROVAL_RUN].update(status="in_progress", conclusion=None)
            if mutate_run:
                mutate_run(evidence.runs[APPROVAL_RUN])
            evidence.mocks(stack)
            stack.enter_context(patch.dict(os.environ, evidence.environment() if environment is None else environment, clear=True))
            return approval.seal(path, expected_hash or approval.sha256(data), "test-cosign")

    def test_seal_keeps_exact_review_and_binds_workflow_context(self):
        evidence = Evidence()
        review = evidence.review()
        raw = approval.encode(review)
        result = self.seal(evidence, raw)
        self.assertEqual(result["review"], review)
        self.assertEqual(result["review_sha256"], approval.sha256(raw))
        self.assertEqual(result["approval_run_id"], APPROVAL_RUN)
        self.assertEqual(result["approval_workflow_sha"], TRUSTED_HEAD)
        self.assertEqual(result["receipt_type"], approval.RECEIPT_TYPE)

    def test_seal_rejects_changed_or_noncanonical_review_bytes(self):
        evidence = Evidence()
        review = evidence.review()
        canonical = approval.encode(review)
        for data, expected in (
            (canonical, "sha256:" + "f" * 64),
            (json.dumps(review).encode(), None),
        ):
            with self.subTest(canonical=data == canonical):
                with self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_REVIEW_HASH_MISMATCH"):
                    self.seal(evidence, data, expected_hash=expected)

    def test_seal_rejects_wrong_ref_attempt_repository_event_and_workflow(self):
        for key, value in (
            ("GITHUB_REF", "refs/heads/feature"), ("GITHUB_RUN_ATTEMPT", "2"),
            ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_EVENT_NAME", "push"),
            ("GITHUB_WORKFLOW_REF", f"{approval.REPO}/.github/workflows/approve-release.yml@refs/heads/main"),
            ("GITHUB_SHA", "b" * 40),
        ):
            with self.subTest(key=key):
                evidence = Evidence()
                raw = approval.encode(evidence.review())
                env = {**evidence.environment(), key: value}
                with self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_APPROVAL_CONTEXT_MISMATCH"):
                    self.seal(evidence, raw, environment=env)

    def test_completed_or_rerun_approval_cannot_be_resealed(self):
        for change in ({"status": "completed", "conclusion": "success"}, {"run_attempt": 2}):
            evidence = Evidence()
            raw = approval.encode(evidence.review())
            with self.subTest(change=change), self.assertRaises(ops.ReleaseError):
                self.seal(evidence, raw, mutate_run=lambda value: value.update(change))

    def test_signing_run_change_while_the_review_is_rebuilt_blocks_sealing(self):
        evidence = Evidence()
        raw = approval.encode(evidence.review())

        def changed(endpoint, count, value):
            if endpoint == f"actions/runs/{APPROVAL_RUN}" and count == 2:
                value["head_sha"] = "b" * 40
            return value

        evidence.after_get = changed
        with self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_WORKFLOW_CHANGED"):
            self.seal(evidence, raw)


class VerifyApprovedTests(unittest.TestCase):
    def verify(self, evidence, *, command_error=None):
        with ExitStack() as stack:
            accepted, images = evidence.mocks(stack)
            gates = stack.enter_context(patch.object(ops, "gates"))
            command = stack.enter_context(patch.object(ops, "command", return_value="", side_effect=command_error))
            result = approval.verify_approved(APPROVAL_RUN, APPROVAL_ARTIFACT, "test-cosign")
            return result, gates, command, accepted, images

    def test_signed_receipt_rechecks_gates_human_history_images_and_exact_review(self):
        evidence = Evidence()
        receipt = evidence.approved_archive()
        result, gates, command, accepted, images = self.verify(evidence)
        self.assertEqual(result, receipt)
        gates.assert_called_once_with()
        command.assert_called_once()
        argv = command.call_args.args[0]
        self.assertEqual(argv[:2], ["test-cosign", "verify-blob"])
        identity = argv[argv.index("--certificate-identity") + 1]
        self.assertEqual(identity, f"https://github.com/{approval.REPO}/.github/workflows/{approval.WORKFLOW}@refs/heads/main")
        self.assertNotIn("/approve-release.yml@", identity)
        self.assertIn(f"actions/runs/{APPROVAL_RUN}/approvals", evidence.calls)
        images.assert_called_once_with(evidence.request, "test-cosign")
        self.assertEqual(accepted.call_count, 2)

    def test_gate_failure_and_bad_signature_block_verification(self):
        evidence = Evidence()
        evidence.approved_archive()
        with ExitStack() as stack:
            evidence.mocks(stack)
            stack.enter_context(patch.object(ops, "gates", side_effect=ops.ReleaseError("MAIN_ADMIN_BYPASS")))
            command = stack.enter_context(patch.object(ops, "command"))
            with self.assertRaisesRegex(ops.ReleaseError, "MAIN_ADMIN_BYPASS"):
                approval.verify_approved(APPROVAL_RUN, APPROVAL_ARTIFACT, "test-cosign")
            command.assert_not_called()
        with self.assertRaisesRegex(ops.ReleaseError, "EXTERNAL_COMMAND_FAILED"):
            self.verify(evidence, command_error=ops.ReleaseError("EXTERNAL_COMMAND_FAILED"))

    def test_explicit_production_approval_by_the_named_human_is_required(self):
        valid = Evidence().history[0]
        cases = [
            [], None, [{**valid, "state": "rejected"}],
            [{**valid, "environments": [{"name": "staging"}]}],
            [{**valid, "user": {"login": "someone-else", "type": "User"}}],
            [{**valid, "user": {"login": ops.REVIEWER, "type": "Bot"}}],
            [valid, {**valid, "state": "rejected"}],
        ]
        for history in cases:
            evidence = Evidence()
            evidence.approved_archive()
            evidence.history = history
            with self.subTest(history=history), self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_HUMAN_APPROVAL_MISSING"):
                self.verify(evidence)

    def test_approval_expires_after_one_hour_and_future_times_are_invalid(self):
        for offset, error in ((-3601, "MIGRATION_APPROVAL_EXPIRED"), (60, "INVALID_MIGRATION_APPROVAL_TIME")):
            evidence = Evidence()
            evidence.approved_archive()
            evidence.runs[APPROVAL_RUN]["run_started_at"] = iso(evidence.now - timedelta(hours=2))
            evidence.runs[APPROVAL_RUN]["updated_at"] = iso(evidence.now + timedelta(seconds=offset))
            with self.subTest(offset=offset), self.assertRaisesRegex(ops.ReleaseError, error):
                self.verify(evidence)

    def test_receipt_identity_and_review_hash_are_bound_to_the_approval_run(self):
        for field, value in (
            ("receipt_type", "STATIC"), ("approval_run_id", APPROVAL_RUN + 1),
            ("approval_run_id", True), ("approval_attempt", 2),
            ("approval_workflow_sha", "b" * 40), ("review_sha256", "sha256:" + "f" * 64),
        ):
            evidence = Evidence()
            receipt = evidence.receipt()
            receipt[field] = value
            evidence.approved_archive(receipt)
            with self.subTest(field=field), self.assertRaises(ops.ReleaseError):
                self.verify(evidence)

    def test_manifest_is_rebuilt_after_signature_not_trusted_from_the_artifact(self):
        evidence = Evidence()
        receipt = evidence.receipt()
        changed = receipt["review"]["migration_manifest"]
        changed["source_images"]["backend"] = f"ghcr.io/{approval.REPO}-backend@sha256:" + "e" * 64
        changed["rehearsal_proof"]["source_images"] = copy.deepcopy(changed["source_images"])
        receipt["review_sha256"] = approval.sha256(approval.encode(receipt["review"]))
        evidence.approved_archive(receipt)
        with self.assertRaisesRegex(ops.ReleaseError, "MIGRATION_REVIEW_CHANGED"):
            self.verify(evidence)

    def test_changed_or_expired_approval_after_revalidation_is_rejected(self):
        for change in ("rerun", "updated", "expired", "artifact"):
            evidence = Evidence()
            evidence.approved_archive()

            def mutate(endpoint, count, value):
                if count == 2 and endpoint == f"actions/runs/{APPROVAL_RUN}":
                    if change == "rerun":
                        value["run_attempt"] = 2
                    elif change == "updated":
                        value["updated_at"] = iso(evidence.now - timedelta(minutes=2))
                    elif change == "expired":
                        value["run_started_at"] = iso(evidence.now - timedelta(hours=2))
                        value["updated_at"] = iso(evidence.now - timedelta(seconds=3601))
                if count == 2 and change == "artifact" and endpoint == f"actions/artifacts/{APPROVAL_ARTIFACT}":
                    value["digest"] = "sha256:" + "f" * 64
                return value

            evidence.after_get = mutate
            with self.subTest(change=change), self.assertRaises(ops.ReleaseError):
                self.verify(evidence)

    def test_static_receipt_and_static_manifest_cannot_authorize_migration(self):
        evidence = Evidence()
        static = {
            "approval_run_id": APPROVAL_RUN, "approval_attempt": 1,
            "approval_workflow_sha": TRUSTED_HEAD,
            "expected_source_release": evidence.request["source_release_sha"],
            "release": {"version": 1},
        }
        evidence.approved_archive(static)
        with self.assertRaisesRegex(ops.ReleaseError, "INVALID_MIGRATION_RECEIPT"):
            self.verify(evidence)
        review = evidence.review()
        review["migration_manifest"] = {"version": 1, "release_sha": TRUSTED_HEAD}
        with self.assertRaises(ManifestContractError):
            approval.validate_review(review)

    def test_migration_receipt_cannot_enter_the_static_apply_verifier(self):
        evidence = Evidence()
        review = evidence.review()
        with self.assertRaisesRegex(ops.ReleaseError, "UNEXPECTED_MANIFEST_FIELDS"):
            ops.validate_manifest(review["migration_manifest"])
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(ops, "gh", side_effect=evidence.gh))
            stack.enter_context(patch.object(ops, "gates"))
            command = stack.enter_context(patch.object(ops, "command"))
            with self.assertRaisesRegex(ops.ReleaseError, "UNTRUSTED_WORKFLOW"):
                ops.approved(APPROVAL_RUN, Path(directory), "test-cosign")
            command.assert_not_called()


class ApprovalCliTests(unittest.TestCase):
    def test_existing_output_is_never_replaced_or_reverified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.json"
            path.write_text("preserve")
            with (
                patch.object(sys, "argv", ["migration_release_approval", "verify-approved", "--approval-run", "301", "--approval-artifact", "401", "--output", str(path)]),
                patch.object(approval, "verify_approved") as verify,
                redirect_stdout(io.StringIO()) as output,
                self.assertRaises(SystemExit),
            ):
                approval.main()
            verify.assert_not_called()
            self.assertEqual(path.read_text(), "preserve")
            self.assertEqual(json.loads(output.getvalue())["error_code"], "MIGRATION_APPROVAL_OUTPUT_EXISTS")

    def test_failure_does_not_publish_an_output_or_echo_untrusted_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.json"
            with (
                patch.object(sys, "argv", ["migration_release_approval", "verify-approved", "--approval-run", "301", "--approval-artifact", "401", "--output", str(path)]),
                patch.object(approval, "verify_approved", side_effect=ops.ReleaseError("token=do-not-print")),
                redirect_stdout(io.StringIO()) as output,
                self.assertRaises(SystemExit),
            ):
                approval.main()
            self.assertFalse(path.exists())
            self.assertNotIn("do-not-print", output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "FAIL")

    def test_subprocess_timeout_is_sanitized_and_does_not_publish_an_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.json"
            error = subprocess.TimeoutExpired(
                ["external-tool", "credential=do-not-print"], 600,
                output="private-output", stderr="private-stderr",
            )
            with (
                patch.object(sys, "argv", ["migration_release_approval", "verify-approved", "--approval-run", "301", "--approval-artifact", "401", "--output", str(path)]),
                patch.object(approval, "verify_approved", side_effect=error),
                redirect_stdout(io.StringIO()) as output,
                self.assertRaises(SystemExit),
            ):
                approval.main()
            self.assertFalse(path.exists())
            for private in ("do-not-print", "private-output", "private-stderr"):
                self.assertNotIn(private, output.getvalue())
            self.assertEqual(json.loads(output.getvalue())["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
