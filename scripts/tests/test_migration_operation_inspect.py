"""Inspect real recorded operations without granting authority or doing I/O outside state."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import signal
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_apply_state as state
import migration_operation_inspect as inspection
import migration_release_apply as apply
import release_ops as ops
from test_migration_release_apply import (
    DATABASE_IDENTITY,
    FakeRuntime,
    OPERATION,
    approval_metadata,
)
from test_release_manifest import manifest


FALSE_FIELDS = (
    "live_runtime_checked", "historical_continuity_verified",
    "receipt_signature_verified", "automatic_retry", "activation_authorized",
    "schema_downgrade_allowed",
)
SECRET = "PRIVATE_PAYLOAD_MUST_NOT_APPEAR"


def canonical_hash(value):
    return "sha256:" + hashlib.sha256(state.canonical_bytes(value)).hexdigest()


class RecordedRuntime(FakeRuntime):
    """The existing engine test double, with the files its real adapter publishes."""

    def __init__(self, value, journal):
        super().__init__(value, journal)
        started = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        containers = {}
        for index, name in enumerate(sorted(ops.ALL), 1):
            image_digit = "1" if name == "frontend" else "4" if name == "postgres" else "5" if name == "redis" else "2"
            containers[name] = {
                "id": f"{index:064x}", "image_id": "sha256:" + image_digit * 64,
                "started_at": started, "restart_count": 0,
                "restart_policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            }
        self.baseline = {
            "version": 1, "operation_id": OPERATION,
            "manifest_sha256": state.manifest_hash(value),
            "source_runtime": {
                "version": 1, "operation_id": OPERATION, "project": "iteam-ai-marketing",
                "containers": containers,
                "postgres_networks": {
                    "iteam-ai-marketing_default": {
                        "NetworkID": "6" * 64, "EndpointID": "7" * 64,
                        "Aliases": ["postgres"], "IPAMConfig": None,
                        "IPAddress": "172.19.0.2", "IPPrefixLen": 16,
                        "Gateway": "172.19.0.1", "GlobalIPv6Address": "",
                        "GlobalIPv6PrefixLen": 0, "IPv6Gateway": "",
                        "MacAddress": "02:42:ac:13:00:02",
                    },
                },
                "postgres_host_bindings": [{"HostIp": "127.0.0.1", "HostPort": "5432"}],
            },
            "compose_configuration_sha256": "sha256:" + "8" * 64,
            "environment_sha256": "sha256:" + "9" * 64,
            "database_identity_sha256": DATABASE_IDENTITY,
            "postgres_data_volume": "iteam-ai-marketing_postgres_data",
        }

    def inventory(self):
        value = super().inventory()
        value["runtime_identity_sha256"] = canonical_hash(self.baseline)
        return value

    def backup(self):
        ops.save(self.journal.state_dir / f"migration-runtime-{OPERATION}.json", self.baseline)
        proof = super().backup()
        ops.save(self.journal.state_dir / f"migration-backup-{OPERATION}.json", proof)
        return proof


class MigrationOperationInspectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.directory.chmod(0o700)
        self.value = manifest()
        self.journal = state.Journal(self.directory, OPERATION, self.value, approval_metadata())
        self.runtime = RecordedRuntime(self.value, self.journal)

    @property
    def runtime_path(self):
        return self.directory / f"migration-runtime-{OPERATION}.json"

    @property
    def backup_path(self):
        return self.directory / f"migration-backup-{OPERATION}.json"

    def run_recording(self, expected="PRE_ACTIVATION_READY"):
        with ops.locked_state(self.directory):
            if expected == "PRE_ACTIVATION_READY":
                result = apply.execute(self.runtime, self.journal)
            else:
                with self.assertRaises(apply.ApplyFailure) as caught:
                    apply.execute(self.runtime, self.journal)
                result = caught.exception.result
        self.assertEqual(result["status"], expected)
        return result

    def inspect(self, **kwargs):
        report = inspection.inspect_operation(self.directory, OPERATION, **kwargs)
        self.assertIs(type(report), dict)
        self.assertEqual(report["writer_boundary"], "UNKNOWN")
        for name in FALSE_FIELDS:
            self.assertIs(report[name], False, name)
        self.assertNotIn(SECRET, json.dumps(report))
        return report

    def read(self, path):
        return json.loads(path.read_text())

    def replace_document(self, path, mutate):
        value = self.read(path)
        mutate(value)
        ops.save(path, value)

    def snapshot_files(self):
        return {
            path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns)
            for path in self.directory.iterdir()
        }

    def assert_invalid(self):
        report = self.inspect()
        self.assertEqual(report["record_state"], "INVALID")
        self.assertIsInstance(report["error_code"], str)
        return report

    def test_real_success_is_a_consistent_record_without_live_or_execution_authority(self):
        self.run_recording()
        report = self.inspect()
        self.assertEqual(report["record_state"], "CONSISTENT_TERMINAL")
        self.assertEqual(report["recorded_status"], "PRE_ACTIVATION_READY")
        self.assertEqual(report["recorded_phase"], "MAINTENANCE_READY")
        self.assertIsNone(report["error_code"])
        self.assertIs(report["approval_time_eligible"], True)
        self.assertIs(report["backup_time_eligible"], True)

    def test_real_rollback_records_preserve_the_original_failure_without_authorizing_restart(self):
        self.runtime.failures[("readiness", 1, "after")] = ops.ReleaseError("MIGRATION_READINESS_FAILED")
        self.run_recording("ROLLED_BACK_PRE_ACTIVATION")
        report = self.inspect()
        self.assertEqual(report["record_state"], "CONSISTENT_TERMINAL")
        self.assertEqual(report["recorded_status"], "ROLLED_BACK_PRE_ACTIVATION")
        self.assertEqual(report["recorded_phase"], "SOURCE_CONFIGURATION_RESTORED")
        self.assertEqual(self.read(self.journal.path)["failure_code"], "MIGRATION_READINESS_FAILED")

    def test_real_operator_recovery_record_is_consistent_but_never_a_safe_runtime_claim(self):
        self.runtime.failures[("readiness", 1, "after")] = ops.ReleaseError("MIGRATION_READINESS_FAILED")
        self.runtime.failures[("containment", 1, "before")] = ops.ReleaseError("MIGRATION_CONTAINMENT_FAILED")
        self.run_recording("OPERATOR_RECOVERY_REQUIRED")
        report = self.inspect()
        self.assertEqual(report["record_state"], "CONSISTENT_TERMINAL")
        self.assertEqual(report["recorded_status"], "OPERATOR_RECOVERY_REQUIRED")

    def test_pending_action_remains_incomplete_even_with_valid_backup_records(self):
        captured = []
        self.runtime.hooks[("upgrade", 1, "before")] = lambda _runtime: captured.append(self.journal.path.read_bytes())
        self.run_recording()
        self.journal.path.write_bytes(captured[0])
        report = self.inspect()
        self.assertEqual(report["record_state"], "INCOMPLETE")
        self.assertEqual(report["recorded_status"], "RUNNING")
        self.assertEqual(report["recorded_phase"], "TARGET_PREFLIGHT_VERIFIED")

    def test_backup_files_published_before_journal_completion_are_an_incomplete_record(self):
        captured = []
        self.runtime.hooks[("backup", 1, "before")] = lambda _runtime: captured.append(self.journal.path.read_bytes())
        self.run_recording()
        self.journal.path.write_bytes(captured[0])
        self.assertTrue(self.runtime_path.is_file())
        self.assertTrue(self.backup_path.is_file())
        report = self.inspect()
        self.assertEqual(report["record_state"], "INCOMPLETE")
        self.assertEqual(report["recorded_status"], "RUNNING")
        self.assertEqual(report["recorded_phase"], "APPROVAL_CONSUMED")

    def test_early_backup_failure_can_leave_a_consistent_terminal_record_without_a_proof(self):
        self.runtime.failures[("backup", 1, "before")] = ops.ReleaseError("MIGRATION_BACKUP_BINDING_MISMATCH")
        self.run_recording("OPERATOR_RECOVERY_REQUIRED")
        self.assertTrue(self.runtime_path.is_file())
        self.assertFalse(self.backup_path.exists())
        report = self.inspect()
        self.assertEqual(report["record_state"], "CONSISTENT_TERMINAL")
        self.assertEqual(report["recorded_status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertIsNone(report["backup_time_eligible"])

    def test_terminal_backup_failure_rejects_a_proof_created_after_the_operation_finished(self):
        proofs = []
        self.runtime.backup_override = lambda proof: proofs.append(json.loads(json.dumps(proof)))
        self.runtime.failures[("backup", 1, "after")] = ops.ReleaseError("MIGRATION_BACKUP_BINDING_MISMATCH")
        self.run_recording("OPERATOR_RECOVERY_REQUIRED")
        self.assertFalse(self.backup_path.exists())
        self.assertEqual(self.inspect()["record_state"], "CONSISTENT_TERMINAL")
        finished_at = datetime.fromisoformat(self.read(self.journal.path)["updated_at"])
        proof = proofs[0]
        proof["created_at"] = (finished_at + timedelta(days=1)).isoformat()
        ops.save(self.backup_path, proof)
        self.assert_invalid()

    def test_marker_saved_before_journal_advancement_is_incomplete_not_reusable(self):
        with ops.locked_state(self.directory):
            inventory = self.runtime.inventory()
            self.journal.create({
                "source_release_sha": self.value["source_release_sha"],
                "schema": self.value["source_schema_head"],
                "database_identity_sha256": DATABASE_IDENTITY,
                "runtime_identity_sha256": inventory["runtime_identity_sha256"],
                "source_image_ids": inventory["source_image_ids"],
                "business_sha256": inventory["snapshot"]["business_state"]["sha256"],
                "table_counts": inventory["snapshot"]["business_state"]["table_counts"],
                "checks": {"source_verified": True},
            })
            with patch.object(ops, "save", side_effect=OSError("fixture fsync failure")):
                with self.assertRaises(state.JournalError):
                    self.journal.consume()
        self.assertTrue(self.journal.marker_path.is_file())
        self.assertIsNone(self.read(self.journal.path)["consumed_at"])
        report = self.inspect()
        self.assertEqual(report["record_state"], "INCOMPLETE")
        self.assertEqual(report["recorded_phase"], "VERIFIED")

    def test_historical_proofs_remain_inspectable_after_freshness_expires(self):
        self.run_recording()
        report = self.inspect(now=datetime.now(UTC) + timedelta(hours=2))
        self.assertEqual(report["record_state"], "CONSISTENT_TERMINAL")
        self.assertIs(report["approval_time_eligible"], False)
        self.assertIs(report["backup_time_eligible"], False)

    def test_backup_baseline_may_differ_from_the_initial_live_inventory(self):
        def changed_before_backup(runtime):
            runtime.business = {
                "sha256": "sha256:" + "a" * 64,
                "table_counts": {**runtime.business["table_counts"], "activity_logs": 2},
            }
        self.runtime.hooks[("backup", 1, "before")] = changed_before_backup
        self.run_recording()
        value = self.read(self.journal.path)
        initial = value["history"][0]["evidence"]
        backup = self.read(self.backup_path)["source_business_state"]
        self.assertNotEqual(initial["business_sha256"], backup["sha256"])
        self.assertEqual(self.inspect()["record_state"], "CONSISTENT_TERMINAL")

    def test_sequence_history_and_terminal_claims_are_validated(self):
        self.run_recording()
        original = self.journal.path.read_bytes()
        mutations = {
            "sequence": lambda data: data.__setitem__("sequence", data["sequence"] + 1),
            "event_sequence": lambda data: data["history"][2].__setitem__("sequence", 99),
            "missing_event": lambda data: data["history"].pop(2),
            "unknown_phase": lambda data: data.__setitem__("phase", "UNKNOWN_PHASE"),
            "terminal_pending": lambda data: data.__setitem__("pending_action", "upgrade"),
            "false_terminal": lambda data: data.__setitem__("status", "ROLLED_BACK_PRE_ACTIVATION"),
            "activation": lambda data: data.__setitem__("writer_activation_requested", True),
            "unsafe_error": lambda data: data.__setitem__("failure_code", SECRET),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.journal.path.write_bytes(original)
                self.replace_document(self.journal.path, mutate)
                self.assert_invalid()

    def test_forged_action_order_is_rejected_even_if_phase_names_are_known(self):
        self.run_recording()
        def reverse_upgrade_and_readiness(data):
            begin = next(event for event in data["history"] if event["kind"] == "begin" and event["action"] == "upgrade")
            begin["action"] = "readiness"
        self.replace_document(self.journal.path, reverse_upgrade_and_readiness)
        self.assert_invalid()

    def test_malformed_journal_types_are_invalid_records_not_unavailable_io(self):
        self.run_recording()
        original = self.journal.path.read_bytes()
        replacements = {
            "version": True,
            "sequence": True,
            "status": [],
            "phase": {},
            "history": {},
            "approval": None,
            "failure_code": [],
            "created_at": "2026-01-01T00:00:00",
        }
        for field, replacement in replacements.items():
            with self.subTest(field=field):
                self.journal.path.write_bytes(original)
                self.replace_document(self.journal.path, lambda data: data.__setitem__(field, replacement))
                self.assert_invalid()

    def test_manifest_and_marker_cross_bindings_cannot_be_mixed(self):
        self.run_recording()
        files = self.snapshot_files()
        cases = (
            (self.journal.path, "manifest_sha256", "sha256:" + "0" * 64),
            (self.journal.path, "operation_id", "b" * 32),
            (self.journal.path, "repository", "another/repository"),
            (self.journal.marker_path, "operation_id", "b" * 32),
            (self.journal.marker_path, "approval_run_id", 999),
            (self.journal.marker_path, "artifact_id", 999),
            (self.journal.marker_path, "receipt_sha256", "sha256:" + "0" * 64),
            (self.journal.marker_path, "manifest_sha256", "sha256:" + "0" * 64),
        )
        for path, field, replacement in cases:
            with self.subTest(file=path.name, field=field):
                for name, (data, _mode, _modified) in files.items():
                    (self.directory / name).write_bytes(data)
                self.replace_document(path, lambda data: data.__setitem__(field, replacement))
                self.assert_invalid()

    def test_runtime_and_backup_records_are_bound_to_the_operation_and_completed_evidence(self):
        self.run_recording()
        files = self.snapshot_files()
        cases = (
            (self.runtime_path, lambda data: data.__setitem__("operation_id", "b" * 32)),
            (self.runtime_path, lambda data: data.__setitem__("database_identity_sha256", "sha256:" + "0" * 64)),
            (self.runtime_path, lambda data: data["source_runtime"]["containers"]["backend"].__setitem__("image_id", "sha256:" + "0" * 64)),
            (self.backup_path, lambda data: data.__setitem__("manifest_sha256", "sha256:" + "0" * 64)),
            (self.backup_path, lambda data: data.__setitem__("encrypted_backup_sha256", "sha256:" + "0" * 64)),
            (self.backup_path, lambda data: data["restored_business_state"]["table_counts"].__setitem__("users", 4)),
            (self.backup_path, lambda data: data.__setitem__("source_schema_head", self.value["target_schema_head"])),
        )
        for index, (path, mutate) in enumerate(cases):
            with self.subTest(case=index):
                for name, (data, _mode, _modified) in files.items():
                    (self.directory / name).write_bytes(data)
                self.replace_document(path, mutate)
                self.assert_invalid()

    def test_completed_phase_evidence_cannot_claim_different_data_or_database(self):
        self.run_recording()
        original = self.journal.path.read_bytes()
        for field, replacement in (
            ("business_sha256", "sha256:" + "0" * 64),
            ("database_identity_sha256", "sha256:" + "0" * 64),
            ("schema", self.value["source_schema_head"]),
        ):
            with self.subTest(field=field):
                self.journal.path.write_bytes(original)
                def alter(data):
                    event = next(item for item in data["history"] if item["kind"] == "complete" and item["phase"] == "UPGRADE_COMPLETED")
                    event["evidence"][field] = replacement
                self.replace_document(self.journal.path, alter)
                self.assert_invalid()

    def test_readiness_cannot_switch_to_a_different_valid_barrier_digest(self):
        self.run_recording()
        def alter(data):
            event = next(item for item in data["history"] if item["kind"] == "complete" and item["action"] == "readiness")
            replacement = "sha256:" + "0" * 64
            self.assertNotEqual(event["evidence"]["barrier_sha256"], replacement)
            event["evidence"]["barrier_sha256"] = replacement
        self.replace_document(self.journal.path, alter)
        self.assert_invalid()

    def test_unknown_nested_runtime_payloads_are_invalid_even_with_a_matching_recorded_hash(self):
        self.run_recording()
        journal_original = self.journal.path.read_bytes()
        runtime_original = self.runtime_path.read_bytes()
        mutations = (
            lambda data: data.__setitem__("raw_environment", {"TOKEN": SECRET}),
            lambda data: data["source_runtime"].__setitem__("raw_inspect", SECRET),
            lambda data: data["source_runtime"]["containers"]["backend"].__setitem__("Env", [SECRET]),
            lambda data: data["source_runtime"]["postgres_networks"]["iteam-ai-marketing_default"].__setitem__("raw_network", SECRET),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.journal.path.write_bytes(journal_original)
                self.runtime_path.write_bytes(runtime_original)
                self.replace_document(self.runtime_path, mutate)
                changed_hash = canonical_hash(self.read(self.runtime_path))
                self.replace_document(
                    self.journal.path,
                    lambda data: data["history"][0]["evidence"].__setitem__("runtime_identity_sha256", changed_hash),
                )
                self.assert_invalid()

    def test_unknown_fields_and_ambiguous_json_are_not_echoed(self):
        self.run_recording()
        original = self.journal.path.read_bytes()
        malformed = (
            b"null", b"[]", b"true",
            b'{"version":1,"version":1,"secret":"' + SECRET.encode() + b'"}',
            b'{"version":NaN,"secret":"' + SECRET.encode() + b'"}',
            b"\xff" + SECRET.encode(),
            b"{" + SECRET.encode(),
        )
        for payload in malformed:
            with self.subTest(payload_kind=payload[:12]):
                self.journal.path.write_bytes(payload)
                self.assert_invalid()
        self.journal.path.write_bytes(original)
        self.replace_document(self.journal.path, lambda data: data.__setitem__("raw_environment", {"TOKEN": SECRET}))
        self.assert_invalid()

    def test_json_null_artifacts_are_invalid_rather_than_missing(self):
        self.run_recording()
        for path in (self.journal.marker_path, self.runtime_path, self.backup_path):
            with self.subTest(file=path.name):
                original = path.read_bytes()
                try:
                    path.write_bytes(b"null")
                    self.assert_invalid()
                finally:
                    path.write_bytes(original)

    def test_missing_required_records_never_preserve_a_terminal_integrity_claim(self):
        self.run_recording()
        for path in (self.journal.marker_path, self.runtime_path, self.backup_path):
            with self.subTest(file=path.name):
                contents = path.read_bytes()
                path.unlink()
                try:
                    report = self.inspect()
                    self.assertEqual(report["record_state"], "INCOMPLETE")
                finally:
                    path.write_bytes(contents)
                    path.chmod(0o600)

    def test_missing_state_or_lock_is_not_created(self):
        missing = self.directory / "missing"
        report = inspection.inspect_operation(missing, OPERATION)
        self.assertEqual(report["record_state"], "UNAVAILABLE")
        self.assertFalse(missing.exists())
        report = self.inspect()
        self.assertEqual(report["record_state"], "UNAVAILABLE")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_busy_operation_lock_is_not_bypassed(self):
        self.run_recording()
        with ops.locked_state(self.directory):
            report = self.inspect()
            self.assertEqual(report["record_state"], "UNAVAILABLE")
        self.assertEqual(self.inspect()["record_state"], "CONSISTENT_TERMINAL")

    def test_inspection_accepts_an_existing_shared_reader_lock(self):
        self.run_recording()
        descriptor = os.open(self.directory / "operation.lock", os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertEqual(self.inspect()["record_state"], "CONSISTENT_TERMINAL")
        finally:
            os.close(descriptor)

    def test_replacing_a_record_during_inspection_is_detected_even_with_identical_bytes(self):
        self.run_recording()
        original = self.read(self.journal.path)
        real_read = os.read
        replaced = False
        def replace_after_read(descriptor, length):
            nonlocal replaced
            data = real_read(descriptor, length)
            if data and not replaced:
                replaced = True
                ops.save(self.journal.path, original)
            return data
        with patch.object(os, "read", side_effect=replace_after_read):
            report = self.assert_invalid()
        self.assertTrue(replaced)
        self.assertEqual(report["error_code"], "MIGRATION_INSPECTION_STATE_CHANGED")

    def test_failure_of_the_final_snapshot_check_clears_previously_assembled_claims(self):
        self.run_recording()
        original_check = inspection._Records.check
        checks = 0
        def changed_at_final_check(records):
            nonlocal checks
            checks += 1
            if checks == 3:
                raise inspection._InspectionError("STATE_CHANGED")
            return original_check(records)
        with patch.object(inspection._Records, "check", new=changed_at_final_check):
            report = self.assert_invalid()
        self.assertEqual(checks, 3)
        self.assertEqual(report["error_code"], "MIGRATION_INSPECTION_STATE_CHANGED")
        for field in ("recorded_phase", "recorded_status", "approval_time_eligible", "backup_time_eligible"):
            self.assertIsNone(report[field], field)

    def test_read_only_access_preserves_all_files_and_never_calls_external_tools(self):
        self.run_recording()
        before = self.snapshot_files()
        real_open = os.open
        def read_only_open(path, flags, *args, **kwargs):
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND), 0)
            return real_open(path, flags, *args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch.object(os, "open", side_effect=read_only_open))
            for name in ("mkdir", "unlink", "rename", "replace", "write_bytes", "write_text", "chmod"):
                stack.enter_context(patch.object(Path, name, side_effect=AssertionError("No state mutation")))
            external = [
                stack.enter_context(patch("release_ops.command", side_effect=AssertionError("No command"))),
                stack.enter_context(patch("subprocess.run", side_effect=AssertionError("No subprocess"))),
                stack.enter_context(patch("subprocess.Popen", side_effect=AssertionError("No subprocess"))),
                stack.enter_context(patch("socket.socket", side_effect=AssertionError("No network"))),
            ]
            self.assertEqual(self.inspect()["record_state"], "CONSISTENT_TERMINAL")
            for command in external:
                command.assert_not_called()
        self.assertEqual(self.snapshot_files(), before)

    def test_unsafe_modes_and_owner_are_rejected_without_repairs(self):
        self.run_recording()
        for path, mode in ((self.directory, 0o755), (self.journal.path, 0o644), (self.directory / "operation.lock", 0o644)):
            with self.subTest(path=path.name):
                previous = stat.S_IMODE(path.stat().st_mode)
                path.chmod(mode)
                try:
                    self.assert_invalid()
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                finally:
                    path.chmod(previous)
        with patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assert_invalid()

    def test_symlink_and_hardlink_inputs_are_never_followed(self):
        self.run_recording()
        outside = self.directory / "outside"
        outside.write_text(SECRET)
        outside.chmod(0o600)
        original = self.journal.path.read_bytes()
        self.journal.path.unlink()
        self.journal.path.symlink_to(outside)
        self.assert_invalid()
        self.assertEqual(outside.read_text(), SECRET)
        self.journal.path.unlink()
        self.journal.path.write_bytes(original)
        self.journal.path.chmod(0o600)
        hardlink = self.directory / "second-name"
        os.link(self.journal.path, hardlink)
        self.assert_invalid()
        self.assertEqual(hardlink.read_bytes(), original)

    def test_relative_or_symlinked_state_directory_is_rejected(self):
        self.run_recording()
        alias = self.directory / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        for path in (Path(self.directory.name), alias):
            with self.subTest(path_kind="relative" if not path.is_absolute() else "symlink"):
                report = inspection.inspect_operation(path, OPERATION)
                self.assertEqual(report["record_state"], "INVALID")
                self.assertEqual(report["writer_boundary"], "UNKNOWN")

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(signal, "setitimer"), "POSIX FIFO deadline")
    def test_fifo_input_is_rejected_without_blocking(self):
        self.run_recording()
        self.journal.path.unlink()
        os.mkfifo(self.journal.path, 0o600)
        previous_handler = signal.getsignal(signal.SIGALRM)
        def timeout(_signal, _frame):
            raise TimeoutError("Inspection blocked on FIFO")
        signal.signal(signal.SIGALRM, timeout)
        signal.setitimer(signal.ITIMER_REAL, 2)
        try:
            self.assert_invalid()
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)

    def test_oversized_records_fail_without_echoing_their_content(self):
        self.run_recording()
        self.journal.path.write_bytes(b" " * (4 * 1024 * 1024 + 1) + SECRET.encode())
        self.assert_invalid()

    def test_invalid_operation_identifiers_cannot_select_other_paths_or_leak_input(self):
        self.run_recording()
        before = self.snapshot_files()
        for operation in ("", "../" + SECRET, "a" * 65, "A" * 32, "a/b", True):
            with self.subTest(kind=type(operation).__name__):
                report = inspection.inspect_operation(self.directory, operation)
                self.assertEqual(report["record_state"], "INVALID")
                self.assertEqual(report["writer_boundary"], "UNKNOWN")
                self.assertNotIn(SECRET, json.dumps(report))
                for name in FALSE_FIELDS:
                    self.assertIs(report[name], False)
        self.assertEqual(self.snapshot_files(), before)

    def test_supported_named_operation_identifier_reports_absence_without_creating_it(self):
        self.run_recording()
        before = self.snapshot_files()
        report = inspection.inspect_operation(self.directory, "disposable_run-01")
        self.assertEqual(report["record_state"], "UNAVAILABLE")
        self.assertEqual(report["error_code"], "MIGRATION_INSPECTION_NOT_FOUND")
        self.assertEqual(report["operation_id"], "disposable_run-01")
        self.assertEqual(self.snapshot_files(), before)

    def test_naive_or_non_datetime_observation_is_rejected_without_state_changes(self):
        self.run_recording()
        before = self.snapshot_files()
        for now in (datetime(2026, 1, 1), SECRET, True):
            with self.subTest(kind=type(now).__name__):
                report = self.inspect(now=now)
                self.assertEqual(report["record_state"], "INVALID")
                self.assertEqual(report["error_code"], "MIGRATION_INSPECTION_INVALID_TIME")
        self.assertEqual(self.snapshot_files(), before)

    def test_cli_rejects_execution_and_path_override_arguments_without_echoing_them(self):
        for flag in ("--root", "--state-dir", "--resume", "--activate", "--downgrade"):
            with self.subTest(flag=flag):
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(sys, "argv", ["migration_operation_inspect.py", "--operation-id", OPERATION, flag, SECRET]), patch.object(inspection, "inspect_operation") as inspect_call, redirect_stdout(output), redirect_stderr(errors):
                    try:
                        inspection.main()
                    except SystemExit as result:
                        self.assertNotEqual(result.code, 0)
                inspect_call.assert_not_called()
                self.assertNotIn(SECRET, output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
