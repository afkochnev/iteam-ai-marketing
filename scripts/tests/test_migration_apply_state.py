"""Durability and replay boundaries; no Docker, GitHub or production access."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_apply_state as state
import release_ops as ops
from test_release_manifest import manifest


def approval() -> dict:
    return {
        "approval_run_id": 501, "artifact_id": 502,
        "receipt_sha256": "sha256:" + "1" * 64,
        "approved_at": datetime.now(UTC).isoformat(),
    }


FORWARD = (
    "backup", "stop_schedulers", "drain_workers", "stop_writers",
    "fence_database", "source_checks", "target_preflight", "upgrade",
    "target_checks", "readiness",
)


class MigrationApplyStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.binding = approval()

    def journal(self, operation="test-operation", *, binding=None):
        return state.Journal(self.root, operation, manifest(), binding or self.binding)

    def created(self):
        journal = self.journal()
        journal.create({
            "source_release_sha": manifest()["source_release_sha"],
            "database_identity_sha256": "sha256:" + "2" * 64,
            "runtime_identity_sha256": "sha256:" + "3" * 64,
            "source_image_ids": {"backend": "sha256:" + "4" * 64},
        })
        return journal

    def consumed(self):
        journal = self.created()
        journal.consume()
        return journal

    def advance(self, journal, until):
        for action in FORWARD:
            journal.begin(action)
            journal.complete(state.ACTION_PHASES[action])
            if action == until:
                return
        self.fail("Unknown phase helper argument")

    def test_constructor_has_no_writes_and_inputs_and_snapshots_are_copied(self):
        source, binding = manifest(), approval()
        journal = state.Journal(self.root, "one", source, binding)
        self.assertEqual(list(self.root.iterdir()), [])
        source["source_images"]["backend"] = "SECRET"
        binding["receipt_sha256"] = "SECRET"
        journal.create()
        data = journal.data
        data["manifest"]["source_images"]["backend"] = "SECRET"
        self.assertNotIn("SECRET", journal.path.read_text())
        self.assertEqual(journal.data["phase"], "VERIFIED")
        self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)

    def test_operation_manifest_and_approval_contracts_fail_closed(self):
        for name in ("../outside", "a/b", "", "UPPER", "a" * 65):
            with self.subTest(name=name), self.assertRaises(state.JournalError):
                self.journal(name)
        bad = manifest()
        bad["secret"] = "never write"
        with self.assertRaisesRegex(state.JournalError, "INVALID_MIGRATION_STATE_MANIFEST"):
            state.Journal(self.root, "one", bad, approval())
        for field, value in (
            ("approval_run_id", True), ("artifact_id", 0),
            ("receipt_sha256", "token"), ("approved_at", "2026-01-01"),
            ("approved_at", "invalid"), ("secret", "token"),
        ):
            bad = approval()
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(state.JournalError):
                self.journal(binding=bad)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unknown_or_payload_evidence_never_changes_the_journal(self):
        journal = self.consumed()
        journal.begin("backup")
        before = journal.path.read_bytes()
        for evidence in (
            {"credentials": "SECRET"}, {"table_counts": {"users": True}},
            {"table_counts": {"users": -1}}, {"table_counts": {"rows;DROP": 1}},
            {"table_counts": {}}, {"source_image_ids": {"unknown": "sha256:" + "a" * 64}},
            {"source_image_ids": {"backend": "latest"}},
            {"checks": {"secret": True}}, {"checks": {"writers_stopped": 1}},
            {"runtime_identity_sha256": "SECRET"}, {"schema": "secret/value"},
            {"business_sha256": ["SECRET"]}, {"source_release_sha": "SECRET"},
        ):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(
                state.JournalError, "INVALID_MIGRATION_STATE_EVIDENCE"
            ):
                journal.complete("BACKUP_VERIFIED", evidence)
            self.assertEqual(journal.path.read_bytes(), before)
            self.assertNotIn("SECRET", journal.path.read_text())
        journal.complete("BACKUP_VERIFIED", {
            "business_sha256": "sha256:" + "a" * 64,
            "source_business_sha256": "sha256:" + "a" * 64,
            "restored_business_sha256": "sha256:" + "a" * 64,
            "encrypted_backup_sha256": "sha256:" + "b" * 64,
            "table_counts": {"users": 3, "empty_table": 0},
            "checks": {"backup_verified": True, "restored_data_verified": True},
        })

    def test_forward_success_is_durable_and_cannot_activate_writers(self):
        journal = self.consumed()
        for action in FORWARD:
            journal.begin(action)
            disk = json.loads(journal.path.read_text())
            self.assertEqual(disk["pending_action"], action)
            self.assertTrue(journal.has_pending)
            journal.complete(state.ACTION_PHASES[action])
            self.assertIsNone(json.loads(journal.path.read_text())["pending_action"])
        journal.finish("PRE_ACTIVATION_READY")
        data = json.loads(journal.path.read_text())
        self.assertEqual(data["status"], "PRE_ACTIVATION_READY")
        self.assertEqual(data["phase"], "MAINTENANCE_READY")
        self.assertIs(data["writer_activation_requested"], False)
        self.assertEqual([event["sequence"] for event in data["history"]], list(range(1, data["sequence"] + 1)))
        with self.assertRaises(state.JournalError):
            journal.begin("activate_writers")
        with self.assertRaises(state.JournalError):
            journal.consume()

    def test_no_action_before_consumption_out_of_order_or_while_pending(self):
        journal = self.created()
        with self.assertRaisesRegex(state.JournalError, "NOT_CONSUMED"):
            journal.begin("backup")
        journal.consume()
        with self.assertRaises(state.JournalError):
            journal.begin("upgrade")
        journal.begin("backup")
        with self.assertRaisesRegex(state.JournalError, "ACTION_PENDING"):
            journal.begin("stop_schedulers")
        with self.assertRaises(state.JournalError):
            journal.complete("UPGRADE_COMPLETED")

    def test_marker_binds_receipt_manifest_and_shared_approval_namespace(self):
        journal = self.consumed()
        marker = json.loads(journal.marker_path.read_text())
        self.assertEqual(journal.marker_path.name, "approval-501.consumed")
        self.assertEqual(marker["receipt_sha256"], self.binding["receipt_sha256"])
        self.assertEqual(marker["manifest_sha256"], state.manifest_hash(manifest()))
        self.assertEqual(marker["operation_id"], "test-operation")
        self.assertEqual(journal.marker_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(state.JournalError, "APPROVAL_ALREADY_USED"):
            self.journal("another-operation").create()

    def test_existing_static_marker_and_symlink_are_never_replaced(self):
        marker = self.root / "approval-501.consumed"
        marker.write_text("existing static approval consumption")
        with self.assertRaisesRegex(state.JournalError, "APPROVAL_ALREADY_USED"):
            self.journal().create()
        self.assertEqual(marker.read_text(), "existing static approval consumption")
        marker.unlink()
        target = self.root / "outside"
        target.write_text("untouched")
        marker.symlink_to(target)
        with self.assertRaisesRegex(state.JournalError, "APPROVAL_ALREADY_USED"):
            self.journal().create()
        self.assertEqual(target.read_text(), "untouched")

    def test_existing_or_symlink_journal_blocks_restart_even_with_new_approval(self):
        journal = self.consumed()
        journal.begin("backup")
        other = approval()
        other["approval_run_id"] += 1
        before = journal.path.read_bytes()
        with self.assertRaisesRegex(state.JournalError, "OPERATION_ALREADY_EXISTS"):
            self.journal(binding=other).create()
        self.assertEqual(journal.path.read_bytes(), before)
        other_path = self.root / "operation-other.json"
        other_path.symlink_to(journal.path)
        with self.assertRaisesRegex(state.JournalError, "OPERATION_ALREADY_EXISTS"):
            self.journal("other", binding=other).create()
        self.assertEqual(journal.path.read_bytes(), before)

    def test_shared_static_lock_prevents_a_concurrent_migration_operation(self):
        with ops.locked_state(self.root):
            with self.assertRaisesRegex(ops.ReleaseError, "ANOTHER_RELEASE_OPERATION_RUNNING"):
                with ops.locked_state(self.root):
                    self.fail("second operation must not enter")

    def test_unsafe_or_replaced_directory_is_rejected(self):
        journal = self.journal()
        self.root.chmod(0o755)
        with self.assertRaisesRegex(state.JournalError, "UNSAFE_MIGRATION_STATE_DIRECTORY"):
            journal.create()
        self.root.chmod(0o700)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(state.JournalError, "UNSAFE_MIGRATION_STATE_DIRECTORY"):
            state.Journal(link, "one", manifest(), approval())
        with self.assertRaisesRegex(state.JournalError, "UNSAFE_MIGRATION_STATE_DIRECTORY"):
            state.Journal(Path("relative"), "one", manifest(), approval())

    def test_marker_file_and_directory_are_synced_before_consumed_journal(self):
        journal = self.created()
        calls = []
        real_fsync, real_save = os.fsync, ops.save

        def synced(descriptor):
            calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
            real_fsync(descriptor)

        def saved(path, data):
            self.assertEqual(calls, ["file", "directory"])
            self.assertTrue(journal.marker_path.is_file())
            self.assertEqual(data["phase"], "APPROVAL_CONSUMED")
            real_save(path, data)

        with patch.object(state.os, "fsync", side_effect=synced), patch.object(state.ops, "save", side_effect=saved):
            journal.consume()
        self.assertEqual(calls, ["file", "directory", "file", "directory"])

    def test_consumption_fsync_failure_prevents_any_action_and_marker_survives(self):
        journal = self.created()
        def fail_parent(_path):
            raise OSError("SECRET fsync failure")

        with patch.object(state, "_sync_directory", side_effect=fail_parent), self.assertRaisesRegex(
            state.JournalError, "^MIGRATION_STATE_WRITE_FAILED$"
        ):
            journal.consume()
        self.assertTrue(journal.marker_path.exists())
        with self.assertRaisesRegex(state.JournalError, "STATE_UNUSABLE"):
            journal.begin("backup")
        with self.assertRaisesRegex(state.JournalError, "APPROVAL_ALREADY_USED"):
            self.journal("another").create()
        self.assertNotIn("SECRET", journal.path.read_text())

    def test_consumed_marker_survives_failure_to_advance_journal(self):
        journal = self.created()
        with patch.object(state.ops, "save", side_effect=OSError("SECRET")), self.assertRaisesRegex(
            state.JournalError, "STATE_WRITE_FAILED"
        ):
            journal.consume()
        self.assertTrue(journal.marker_path.exists())
        self.assertEqual(json.loads(journal.path.read_text())["phase"], "VERIFIED")
        with self.assertRaisesRegex(state.JournalError, "STATE_UNUSABLE"):
            journal.begin("backup")

    def test_after_rename_fsync_failure_prohibits_unjournaled_recovery(self):
        journal = self.consumed()
        real_fsync = os.fsync

        def fail_directory(descriptor):
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("SECRET")
            return real_fsync(descriptor)

        with patch.object(state.os, "fsync", side_effect=fail_directory), self.assertRaisesRegex(
            state.JournalError, "STATE_WRITE_FAILED"
        ):
            journal.begin("backup")
        self.assertEqual(json.loads(journal.path.read_text())["pending_action"], "backup")
        for action in (lambda: journal.fail_action("MIGRATION_APPLY_FAILED"), lambda: journal.begin("containment")):
            with self.assertRaisesRegex(state.JournalError, "STATE_UNUSABLE"):
                action()

    def test_changed_journal_or_marker_permanently_blocks_the_object(self):
        for target in ("journal", "marker"):
            with self.subTest(target=target):
                directory = self.root / target
                directory.mkdir(mode=0o700)
                journal = state.Journal(directory, "one", manifest(), approval())
                journal.create()
                journal.consume()
                path = journal.path if target == "journal" else journal.marker_path
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                with self.assertRaises(state.JournalError):
                    journal.begin("backup")
                path.write_bytes(original)
                with self.assertRaisesRegex(state.JournalError, "STATE_UNUSABLE"):
                    journal.begin("backup")

    def test_consumption_requires_aware_fresh_time_but_recovery_does_not_expire(self):
        now = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
        for offset, accepted in ((3600, True), (3600.1, False), (-1, False)):
            directory = self.root / str(offset)
            directory.mkdir(mode=0o700)
            binding = approval()
            binding["approved_at"] = (now - timedelta(seconds=offset)).isoformat()
            with patch.object(state, "datetime") as clock:
                clock.now.return_value = now
                clock.fromisoformat.side_effect = datetime.fromisoformat
                journal = state.Journal(directory, "one", manifest(), binding)
                journal.create()
                if accepted:
                    journal.consume()
                    clock.now.return_value = now + timedelta(days=1)
                    journal.begin("backup")
                    journal.fail_action("MIGRATION_APPLY_FAILED")
                    journal.begin("containment")
                    journal.complete("CONTAINED")
                else:
                    with self.assertRaisesRegex(state.JournalError, "MIGRATION_APPROVAL_EXPIRED"):
                        journal.consume()
                    self.assertFalse(journal.marker_path.exists())

    def test_failed_action_can_only_enter_guarded_recovery_not_forward_retry(self):
        journal = self.consumed()
        self.advance(journal, "upgrade")
        journal.begin("target_checks")
        journal.fail_action("MIGRATION_TARGET_CHECKS_FAILED")
        for action in ("target_checks", "readiness", "downgrade"):
            with self.assertRaises(state.JournalError):
                journal.begin(action)
        for action in ("containment", "downgrade", "source_checks", "restore_source_configuration"):
            journal.begin(action)
            journal.complete(state.ACTION_PHASES[action])
        journal.finish("ROLLED_BACK_PRE_ACTIVATION")
        self.assertEqual(journal.data["failure_code"], "MIGRATION_TARGET_CHECKS_FAILED")
        self.assertIsNone(journal.data["recovery_error_code"])

    def test_source_schema_failure_can_recover_without_database_downgrade(self):
        journal = self.consumed()
        journal.begin("backup")
        journal.fail_action("MIGRATION_BACKUP_BINDING_MISMATCH")
        for action in ("containment", "source_checks", "restore_source_configuration"):
            journal.begin(action)
            journal.complete(state.ACTION_PHASES[action])
        journal.finish("ROLLED_BACK_PRE_ACTIVATION")
        self.assertNotIn("downgrade", [event["action"] for event in journal.data["history"]])

    def test_recovery_failure_retains_original_and_does_not_retry_same_action(self):
        journal = self.consumed()
        journal.begin("backup")
        journal.fail_action("MIGRATION_APPLY_FAILED")
        journal.begin("containment")
        journal.fail_action("MIGRATION_CONTAINMENT_FAILED")
        with self.assertRaisesRegex(state.JournalError, "ACTION_ALREADY_ATTEMPTED"):
            journal.begin("containment")
        journal.finish("OPERATOR_RECOVERY_REQUIRED")
        self.assertEqual(journal.data["failure_code"], "MIGRATION_APPLY_FAILED")
        self.assertEqual(journal.data["recovery_error_code"], "MIGRATION_CONTAINMENT_FAILED")

    def test_terminal_guards_and_unknown_error_codes_preserve_pending_intent(self):
        journal = self.consumed()
        with self.assertRaises(state.JournalError):
            journal.finish("PRE_ACTIVATION_READY")
        journal.begin("backup")
        before = journal.path.read_bytes()
        for action in (
            lambda: journal.fail_action("SECRET_PASSWORD_123"),
            lambda: journal.finish("OPERATOR_RECOVERY_REQUIRED", "SECRET_PASSWORD_123"),
        ):
            with self.assertRaisesRegex(state.JournalError, "UNSAFE_MIGRATION_STATE_ERROR_CODE"):
                action()
            self.assertEqual(journal.path.read_bytes(), before)
        journal.finish("OPERATOR_RECOVERY_REQUIRED", "MIGRATION_APPLY_FAILED")
        self.assertEqual(journal.data["pending_action"], "backup")
        self.assertTrue(journal.has_pending)
        with self.assertRaises(state.JournalError):
            journal.complete("BACKUP_VERIFIED")


if __name__ == "__main__":
    unittest.main()
