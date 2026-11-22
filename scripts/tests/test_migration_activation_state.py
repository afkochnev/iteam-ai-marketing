"""Candidate-only activation durability; no Docker, database or external access."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migration_activation_state as state
import release_ops as ops

SECRET = "UNTRUSTED_PRIVATE_PAYLOAD"


def binding() -> dict:
    return {
        "authorization": state.AUTHORIZATION, "operation_id": "a" * 32,
        "project": state.PROJECT_PREFIX + "b" * 10, "candidate_sha": "c" * 40,
        "candidate_image_id": "sha256:" + "d" * 64,
        "target_schema_head": "20261121_0030",
    }


def passive() -> dict:
    value = binding()
    return {
        **{key: value[key] for key in ("candidate_sha", "candidate_image_id", "project")},
        "passive_checks_sha256": "sha256:" + "e" * 64,
        "business_writes_blocked": True, "claims_blocked": True,
    }


def containment() -> dict:
    return {
        "target_schema_head": binding()["target_schema_head"],
        "committed_write_preserved": True, "new_claims_blocked": True,
        "runtime_stopped": True, "evidence_sha256": "sha256:" + "f" * 64,
    }


def _crash_after_durable_marker(directory: str) -> None:
    original = state._exclusive_write

    def crash(path, value):
        original(path, value)
        if path.suffix == ".started":
            os._exit(73)

    with ops.locked_state(Path(directory)), patch.object(state, "_exclusive_write", side_effect=crash):
        journal = state.CandidateActivationState(Path(directory), binding())
        journal.create()
        journal.mark_passive_verified(passive())
        journal.begin_activation()
    os._exit(74)


def _try_inherited_activation(journal) -> None:
    for method in (journal.assert_activation_permitted, journal.mark_active):
        try:
            method()
        except state.ActivationStateError as error:
            if str(error) != "CANDIDATE_ACTIVATION_PROCESS_CHANGED":
                os._exit(75)
        else:
            os._exit(76)
    if journal.request_containment("RUNTIME_ADMISSION_FAILED") is not False:
        os._exit(77)
    report = journal.finish_containment(True, evidence=containment())
    if report["state_durability"] != "UNKNOWN" or report["containment_verified"]:
        os._exit(78)
    os._exit(0)


class CandidateActivationStateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory(prefix=state.TEMP_PREFIX))
        self.root = Path(temporary)
        self.directory = self.root / "state"
        self.directory.mkdir(mode=0o700)
        self.journal = state.CandidateActivationState(self.directory, binding())

    def prepared(self):
        self.journal.create()
        self.journal.mark_passive_verified(passive())
        return self.journal

    def started(self):
        journal = self.prepared()
        journal.begin_activation()
        return journal

    def assert_no_authority(self, result):
        for key in (
            "automatic_retry", "schema_downgrade_allowed", "production_approval_verified",
            "production_activation_authorized", "external_side_effects_authorized",
        ):
            self.assertIs(result[key], False, key)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_constructor_has_no_writes_and_copies_candidate_identity(self):
        self.assertEqual(list(self.directory.iterdir()), [])
        source = binding()
        journal = state.CandidateActivationState(self.directory, source)
        source["candidate_sha"] = SECRET
        journal.create()
        changed = journal.data
        changed["binding"]["candidate_image_id"] = SECRET
        self.assertNotIn(SECRET, journal.path.read_text())
        self.assertEqual(stat.S_IMODE(journal.path.stat().st_mode), 0o600)
        self.assert_no_authority(journal.summary())

    def test_production_namespace_authority_unknown_fields_and_invalid_identity_are_rejected(self):
        cases = (
            ("authorization", "PRODUCTION"), ("authorization", True),
            ("operation_id", "../" + SECRET), ("project", "iteam-ai-marketing"),
            ("candidate_sha", "main"), ("candidate_image_id", "candidate:latest"),
            ("target_schema_head", "head/" + SECRET), ("token", SECRET),
        )
        for key, item in cases:
            value = binding()
            value[key] = item
            with self.subTest(key=key), self.assertRaisesRegex(
                state.ActivationStateError, "INVALID_CANDIDATE_ACTIVATION_BINDING"
            ) as caught:
                state.CandidateActivationState(self.directory, value)
            self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(list(self.directory.iterdir()), [])
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / "state"
            other.mkdir(mode=0o700)
            with self.assertRaisesRegex(state.ActivationStateError, "UNSAFE_CANDIDATE_ACTIVATION_DIRECTORY"):
                state.CandidateActivationState(other, binding())

    def test_directory_permissions_symlinks_and_owner_are_not_repaired(self):
        self.directory.chmod(0o755)
        with self.assertRaises(state.ActivationStateError):
            state.CandidateActivationState(self.directory, binding())
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o755)
        self.directory.chmod(0o700)
        with patch.object(state.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(state.ActivationStateError):
                state.CandidateActivationState(self.directory, binding())
        actual = self.root / "actual"
        self.directory.rename(actual)
        self.directory.symlink_to(actual, target_is_directory=True)
        with self.assertRaises(state.ActivationStateError):
            state.CandidateActivationState(self.directory, binding())
        self.assertEqual(list(actual.iterdir()), [])

    def test_passive_evidence_is_required_closed_typed_and_bound(self):
        self.journal.create()
        with self.assertRaisesRegex(state.ActivationStateError, "INVALID_TRANSITION"):
            self.journal.begin_activation()
        before = self.journal.path.read_bytes()
        cases = (
            ("candidate_sha", "1" * 40), ("candidate_image_id", "sha256:" + "2" * 64),
            ("project", state.PROJECT_PREFIX + "3" * 10),
            ("claims_blocked", 1), ("business_writes_blocked", False),
            ("passive_checks_sha256", SECRET), ("payload", SECRET),
        )
        for key, item in cases:
            evidence = passive()
            evidence[key] = item
            with self.subTest(key=key), self.assertRaisesRegex(state.ActivationStateError, "INVALID_EVIDENCE"):
                self.journal.mark_passive_verified(evidence)
            self.assertEqual(self.journal.path.read_bytes(), before)
        self.journal.mark_passive_verified(passive())
        self.assertEqual(self.journal.data["phase"], "PASSIVE_VERIFIED")

    def test_marker_and_journal_are_fsynced_before_any_activation_permit(self):
        journal = self.prepared()
        synced = []
        original = state.os.fsync

        def fsync(fd):
            info = os.fstat(fd)
            synced.append((stat.S_ISDIR(info.st_mode), info.st_ino))
            original(fd)

        with patch.object(state.os, "fsync", side_effect=fsync):
            journal.begin_activation()
        journal.assert_activation_permitted()
        marker_inode = journal.marker_path.stat().st_ino
        directory_inode = self.directory.stat().st_ino
        self.assertIn((False, marker_inode), synced)
        marker_index = synced.index((False, marker_inode))
        self.assertIn((True, directory_inode), synced[marker_index + 1:])
        self.assertGreaterEqual(sum(is_directory for is_directory, _ in synced), 3)
        persisted = json.loads(journal.path.read_text())
        marker = json.loads(journal.marker_path.read_text())
        self.assertEqual(persisted["phase"], "ACTIVATION_STARTED")
        self.assertTrue(persisted["activation_boundary_recorded"])
        self.assertEqual(marker["binding"], binding())
        self.assertEqual(marker["binding_sha256"], persisted["binding_sha256"])
        self.assertFalse(marker["schema_downgrade_allowed"])
        self.assertEqual(stat.S_IMODE(journal.marker_path.stat().st_mode), 0o600)
        with self.assertRaisesRegex(state.ActivationStateError, "INVALID_TRANSITION"):
            journal.begin_activation()

    def test_fsync_failure_after_marker_bytes_never_grants_activation_or_downgrade(self):
        journal = self.prepared()
        original = state.os.fsync

        def fsync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and journal.marker_path.exists():
                raise OSError(SECRET)
            original(fd)

        with patch.object(state.os, "fsync", side_effect=fsync):
            with self.assertRaisesRegex(state.ActivationStateError, "STATE_WRITE_FAILED") as caught:
                journal.begin_activation()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(journal.marker_path.exists())
        with self.assertRaisesRegex(state.ActivationStateError, "STATE_UNUSABLE"):
            journal.assert_activation_permitted()
        report = journal.summary()
        self.assertEqual(report["state_durability"], "UNKNOWN")
        self.assertTrue(report["activation_may_have_started"])
        self.assert_no_authority(report)
        self.assertFalse(journal.request_containment("RUNTIME_ADMISSION_FAILED"))
        self.assertEqual(journal.finish_containment(True, evidence=containment())["phase"], "OPERATOR_RECOVERY_REQUIRED")

    def test_journal_write_failure_after_durable_marker_is_irreversible(self):
        journal = self.prepared()
        original = state.ops.save

        def save(path, value):
            if value["phase"] == "ACTIVATION_STARTED":
                original(path, value)
                raise OSError(SECRET)
            original(path, value)

        with patch.object(state.ops, "save", side_effect=save):
            with self.assertRaisesRegex(state.ActivationStateError, "STATE_WRITE_FAILED"):
                journal.begin_activation()
        self.assertTrue(journal.marker_path.exists())
        self.assertEqual(journal.summary()["state_durability"], "UNKNOWN")
        self.assert_no_authority(journal.summary())
        replacement = state.CandidateActivationState(self.directory, binding())
        with self.assertRaisesRegex(state.ActivationStateError, "STATE_EXISTS"):
            replacement.create()

    def test_existing_marker_or_different_operation_cannot_be_replayed(self):
        journal = self.started()
        marker = journal.marker_path.read_bytes()
        for operation in ("a" * 32, "1" * 32):
            other = {**binding(), "operation_id": operation}
            replacement = state.CandidateActivationState(self.directory, other)
            with self.assertRaisesRegex(state.ActivationStateError, "STATE_EXISTS"):
                replacement.create()
            with self.assertRaises(state.ActivationStateError):
                replacement.assert_activation_permitted()
        self.assertEqual(journal.marker_path.read_bytes(), marker)

    def test_changed_or_unsafe_marker_revokes_the_live_object(self):
        journal = self.started()
        journal.marker_path.write_text(SECRET)
        with self.assertRaisesRegex(state.ActivationStateError, "MARKER_CHANGED"):
            journal.assert_activation_permitted()
        self.assertEqual(journal.summary()["phase"], "OPERATOR_RECOVERY_REQUIRED")
        self.assert_no_authority(journal.summary())

    def test_identical_byte_marker_replacement_revokes_the_live_permit(self):
        journal = self.started()
        old_inode = journal.marker_path.stat().st_ino
        replacement = self.directory / "replacement"
        replacement.write_bytes(journal.marker_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, journal.marker_path)
        self.assertNotEqual(journal.marker_path.stat().st_ino, old_inode)
        self.assertEqual(journal.marker_path.stat().st_nlink, 1)
        with self.assertRaisesRegex(state.ActivationStateError, "MARKER_CHANGED"):
            journal.assert_activation_permitted()
        self.assertEqual(journal.summary()["state_durability"], "UNKNOWN")
        self.assert_no_authority(journal.summary())

    def test_identical_byte_journal_replacement_is_not_an_own_durable_write(self):
        journal = self.started()
        old_inode = journal.path.stat().st_ino
        replacement = self.directory / "replacement"
        replacement.write_bytes(journal.path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, journal.path)
        self.assertNotEqual(journal.path.stat().st_ino, old_inode)
        self.assertEqual(journal.path.stat().st_nlink, 1)
        with self.assertRaisesRegex(state.ActivationStateError, "STATE_CHANGED"):
            journal.assert_activation_permitted()
        self.assertEqual(journal.summary()["state_durability"], "UNKNOWN")
        self.assert_no_authority(journal.summary())

    def test_changed_process_cannot_create_records_from_an_inherited_uncreated_object(self):
        with patch.object(state.os, "getpid", return_value=os.getpid() + 1):
            with self.assertRaisesRegex(state.ActivationStateError, "PROCESS_CHANGED"):
                self.journal.create()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_forked_child_cannot_inherit_activation_or_state_write_authority(self):
        journal = self.started()
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        child = multiprocessing.get_context("fork").Process(
            target=_try_inherited_activation, args=(journal,),
        )
        child.start()
        child.join(timeout=5)
        if child.is_alive():
            child.kill()
            child.join(timeout=5)
            self.fail("inherited candidate object fixture did not exit")
        self.assertEqual(child.exitcode, 0)
        self.assertEqual({path.name: path.read_bytes() for path in self.directory.iterdir()}, before)
        journal.assert_activation_permitted()
        self.assertEqual(journal.summary()["phase"], "ACTIVATION_STARTED")

    def test_readback_mismatch_blocks_the_first_activation(self):
        journal = self.prepared()
        original = state._exclusive_write

        def changed(path, value):
            original(path, value)
            if path == journal.marker_path:
                path.write_text(SECRET)

        with patch.object(state, "_exclusive_write", side_effect=changed):
            with self.assertRaisesRegex(state.ActivationStateError, "STATE_WRITE_FAILED"):
                journal.begin_activation()
        with self.assertRaises(state.ActivationStateError):
            journal.assert_activation_permitted()
        self.assertEqual(journal.summary()["state_durability"], "UNKNOWN")

    def test_post_write_containment_records_preservation_and_never_reopens_activation(self):
        journal = self.started()
        journal.mark_active()
        marker = journal.marker_path.read_bytes()
        self.assertTrue(journal.request_containment("INJECTED_POST_ACTIVATION_FAILURE"))
        with self.assertRaises(state.ActivationStateError):
            journal.assert_activation_permitted()
        result = journal.finish_containment(True, evidence=containment())
        self.assertEqual(result["phase"], "CONTAINED")
        self.assertEqual(result["state_durability"], "CONFIRMED")
        self.assertTrue(result["containment_verified"])
        self.assertTrue(result["committed_write_preserved"])
        self.assertTrue(result["target_schema_preserved"])
        self.assertEqual(result["error_code"], "INJECTED_POST_ACTIVATION_FAILURE")
        self.assertEqual(journal.marker_path.read_bytes(), marker)
        self.assert_no_authority(result)
        for forbidden in ("resume", "downgrade", "restore", "restore_source_configuration"):
            self.assertFalse(hasattr(journal, forbidden))

    def test_containment_failure_never_claims_preservation_or_reenables_activation(self):
        journal = self.started()
        journal.request_containment("RUNTIME_ADMISSION_FAILED")
        report = journal.finish_containment(False)
        self.assertEqual(report["phase"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertFalse(report["containment_verified"])
        self.assertFalse(report["committed_write_preserved"])
        self.assert_no_authority(report)
        with self.assertRaises(state.ActivationStateError):
            journal.assert_activation_permitted()

    def test_invalid_containment_evidence_cannot_claim_a_verified_outcome(self):
        journal = self.started()
        journal.request_containment("RUNTIME_ADMISSION_FAILED")
        evidence = {**containment(), "target_schema_head": "20261009_0028", "payload": SECRET}
        report = journal.finish_containment(True, evidence=evidence)
        self.assertEqual(report["phase"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertFalse(report["containment_verified"])
        self.assertEqual(report["error_code"], "CANDIDATE_ACTIVATION_INVALID_EVIDENCE")
        self.assertNotIn(SECRET, journal.path.read_text())
        self.assert_no_authority(report)

    def test_containment_intent_write_failure_still_revokes_permission_without_raising(self):
        journal = self.started()
        with patch.object(state.ops, "save", side_effect=OSError(SECRET)):
            self.assertFalse(journal.request_containment("INJECTED_POST_ACTIVATION_FAILURE"))
        with self.assertRaises(state.ActivationStateError):
            journal.assert_activation_permitted()
        report = journal.finish_containment(True, evidence=containment())
        self.assertEqual(report["state_durability"], "UNKNOWN")
        self.assertFalse(report["containment_verified"])
        self.assert_no_authority(report)

    def test_untrusted_failure_text_is_not_recorded_and_cannot_prevent_revocation(self):
        journal = self.started()
        before = journal.path.read_bytes()
        self.assertFalse(journal.request_containment(SECRET))
        self.assertEqual(journal.path.read_bytes(), before)
        with self.assertRaises(state.ActivationStateError):
            journal.assert_activation_permitted()
        self.assert_no_authority(journal.summary())

    def test_summary_drops_positive_claims_if_final_record_changes(self):
        journal = self.started()
        journal.request_containment("INJECTED_POST_ACTIVATION_FAILURE")
        journal.finish_containment(True, evidence=containment())
        journal.path.write_text(SECRET)
        result = journal.summary()
        self.assertEqual(result["state_durability"], "UNKNOWN")
        self.assertFalse(result["containment_verified"])
        self.assertFalse(result["committed_write_preserved"])
        self.assert_no_authority(result)

    def test_process_death_after_marker_fsync_cannot_resume_or_erase_boundary(self):
        child = multiprocessing.get_context("fork").Process(
            target=_crash_after_durable_marker, args=(str(self.directory),),
        )
        child.start()
        child.join(timeout=5)
        if child.is_alive():
            child.kill()
            child.join(timeout=5)
            self.fail("disposable crash fixture did not exit")
        self.assertEqual(child.exitcode, 73)
        persisted = json.loads(self.journal.path.read_text())
        marker = json.loads(self.journal.marker_path.read_text())
        self.assertEqual(persisted["phase"], "ACTIVATION_REQUESTED")
        self.assertTrue(persisted["activation_may_have_started"])
        self.assertEqual(marker["binding"], binding())
        with ops.locked_state(self.directory):
            replacement = state.CandidateActivationState(self.directory, binding())
            with self.assertRaisesRegex(state.ActivationStateError, "STATE_EXISTS"):
                replacement.create()
            with self.assertRaises(state.ActivationStateError):
                replacement.assert_activation_permitted()
        self.assertFalse(marker["schema_downgrade_allowed"])


if __name__ == "__main__":
    unittest.main()
