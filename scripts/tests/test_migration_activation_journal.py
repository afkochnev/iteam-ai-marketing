"""Durable activation boundary tests with the real pre-activation engine."""

from __future__ import annotations

import copy
import io
import json
import multiprocessing
import os
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_activation_apply as controller
import migration_activation_journal as activation
import migration_release_apply as preapply
import release_ops as ops
from migration_activation_contract import activation_policy, hash_json
from migration_apply_state import Journal, canonical_bytes, manifest_hash
from test_migration_release_apply import (
    BARRIER_HASH, DATABASE_IDENTITY, OPERATION, FakeRuntime, approval_metadata,
)
from test_release_manifest import manifest

SECRET = "postgresql://private:secret-value@private-host/database"
PROJECT = activation.PROJECT_PREFIX + "b" * 10


class RecordedSource(FakeRuntime):
    def __init__(self, value, journal):
        super().__init__(value, journal)
        self.operation_id = journal.data["operation_id"]
        self.baseline = {
            "version": 1, "operation_id": self.operation_id,
            "manifest_sha256": manifest_hash(value), "database_identity_sha256": DATABASE_IDENTITY,
        }
        self.runtime_identity = hash_json(self.baseline)
        self.inventory_override = lambda value: value.update(runtime_identity_sha256=self.runtime_identity)

    def backup(self):
        proof = super().backup()
        for name, value in (("runtime", self.baseline), ("backup", proof)):
            activation._exclusive_write(self.journal.state_dir / f"migration-{name}-{self.operation_id}.json", value)
        return proof


class FakeActivationRuntime:
    """Small physical adapter double; authority and both journals are real."""

    fixture = True
    project = PROJECT

    def __init__(self, source, state):
        self.source, self.state = source, state
        self.events = source.events
        self.failures = {}
        self.hooks = {}
        self.close_count = 0
        self.started = False
        self.contained = False
        self.plan = {}

    def point(self, phase):
        self.events.append(phase)
        if phase in self.hooks:
            self.hooks[phase]()
        if phase in self.failures:
            raise self.failures[phase]

    def prepare_target_plan(self):
        self.point("plan")
        binding = self.state.data["binding"]
        self.plan = {key: binding[key] for key in activation._PLAN_BINDINGS}
        self.plan["database_identity_sha256"] = DATABASE_IDENTITY
        # These auxiliary records must not be mistaken for prior operations.
        activation._exclusive_write(self.source.journal.state_dir / f"activation-plan-{OPERATION}.json", self.plan)
        return copy.deepcopy(self.plan)

    def passive_postflight(self):
        self.point("passive")
        assert self.source.journal.data["status"] == "PRE_ACTIVATION_READY"
        assert self.source.writers_stopped and self.source.fenced
        return {
            "admission_protocol": 1, "admission_mode": "passive", "business_ready": False,
            "business_writes_blocked": True, "claims_blocked": True, "helpers_removed": True,
            "target_release_sha": self.source.manifest["target_release_sha"],
            "target_schema_head": self.source.manifest["target_schema_head"],
            "target_plan_sha256": hash_json(self.plan), "source_barrier_sha256": BARRIER_HASH,
        }

    def handoff_boundary(self, state):
        self.point("handoff")
        state.begin_activation()

    def active_action(self, name, state):
        state.assert_activation_permitted()
        assert json.loads(state.path.read_text())["phase"] == "ACTIVATION_STARTED"
        assert state.marker_path.is_file()
        self.point(name)

    def recreate_target_stopped(self, state):
        self.active_action("recreate", state)

    def reconnect_data_services(self, state):
        self.active_action("reconnect", state)

    def start_target(self, state):
        self.active_action("start", state)
        self.started = True

    def postflight(self):
        self.point("postflight")
        return {
            "runtime_ready": True, "exact_queues_verified": True,
            "target_release_sha": self.source.manifest["target_release_sha"],
            "target_schema_head": self.source.manifest["target_schema_head"],
            "target_plan_sha256": hash_json(self.plan),
            "target_container_ids_sha256": "sha256:" + "1" * 64,
        }

    def finalize_configuration(self, state):
        self.active_action("finalize", state)
        return {"configuration_persisted": True}

    def contain_target(self):
        self.point("contain_target")
        self.contained = True
        self.started = False
        return containment(self.source.manifest)

    def close(self):
        self.close_count += 1
        self.point("activation_close")


def containment(value):
    return {
        "runtime_stopped": True, "new_claims_blocked": True, "data_services_fenced": True,
        "target_schema_preserved": True, "database_identity_preserved": True,
        "target_schema_head": value["target_schema_head"], "database_identity_sha256": DATABASE_IDENTITY,
        "all_tables_sha256": "sha256:" + "2" * 64,
    }


def inherited_child(state):
    try:
        state.assert_activation_permitted()
    except ops.ReleaseError as error:
        if str(error) != "ACTIVATION_PROCESS_CHANGED":
            os._exit(71)
    else:
        os._exit(72)
    if state.request_containment("ACTIVATION_FAILED"):
        os._exit(73)
    if state.finish_containment(containment(manifest()))["state_durability"] != "UNKNOWN":
        os._exit(74)
    os._exit(0)


def crash_child(directory, binding, previous, plan, passive):
    state = activation.ActivationJournal(Path(directory), binding)
    state.create(previous, plan, passive)
    original = activation._exclusive_write

    def crash(path, value):
        original(path, value)
        if path.suffix == ".started":
            os._exit(73)

    with patch.object(activation, "_exclusive_write", side_effect=crash):
        state.begin_activation()
    os._exit(74)


class ActivationCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix=activation.TEMP_PREFIX)))
        self.directory = root / "state"
        self.directory.mkdir(mode=0o700)
        self.stack.enter_context(ops.locked_state(self.directory))
        self.manifest = manifest()
        self.previous = Journal(self.directory, OPERATION, self.manifest, approval_metadata())
        self.source = RecordedSource(self.manifest, self.previous)
        self.binding = {
            "authorization": activation.FIXTURE_AUTHORIZATION, "operation_id": OPERATION,
            "project": PROJECT, "manifest_sha256": manifest_hash(self.manifest),
            "receipt_sha256": self.previous.data["approval"]["receipt_sha256"], "approval_run_id": 501,
            "runtime_identity_sha256": self.source.runtime_identity,
            "target_release_sha": self.manifest["target_release_sha"],
            "target_schema_head": self.manifest["target_schema_head"],
            "activation_policy_sha256": hash_json(activation_policy()),
            "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        }
        self.state = activation.ActivationJournal(self.directory, self.binding)
        self.runtime = FakeActivationRuntime(self.source, self.state)

    def preactivated(self):
        self.runtime.prepare_target_plan()
        result = preapply.execute(self.source, self.previous)
        self.assertEqual(result["status"], "PRE_ACTIVATION_READY")
        return self.runtime.passive_postflight()

    def prepared(self):
        passive = self.preactivated()
        self.state.create(self.previous, self.runtime.plan, passive)
        return self.state

    def started(self):
        state = self.prepared()
        state.begin_activation()
        return state

    def execute(self, **kwargs):
        return controller.execute(self.runtime, self.previous, self.state,
                                  expected_runtime_identity_sha256=self.source.runtime_identity, **kwargs)

    def assert_no_authority(self, value):
        for key in ("automatic_retry", "resume_authorized", "schema_downgrade_allowed",
                    "backup_restore_allowed", "production_approval_verified"):
            self.assertIs(value[key], False, key)
        self.assertNotIn(SECRET, json.dumps(value))

    def run_child(self, target, *args):
        child = multiprocessing.get_context("fork").Process(target=target, args=args)
        child.start()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join(5)
            self.fail("disposable child did not terminate")
        return child.exitcode


class ActivationJournalTests(ActivationCase):
    def test_terminal_preapply_is_pinned_without_reopening_old_journal(self):
        passive = self.preactivated()
        old = self.previous.path.read_bytes()
        with patch.object(self.previous, "_live", side_effect=AssertionError("old mutation permit")):
            self.state.create(self.previous, self.runtime.plan, passive)
        self.state.begin_activation()
        self.assertEqual(self.previous.path.read_bytes(), old)
        self.assertEqual(self.state.data["phase"], "ACTIVATION_STARTED")
        self.assertEqual(len(self.state._dependencies), 4)
        self.assert_no_authority(self.state.summary())

    def test_binding_types_namespaces_and_unknown_fields_are_rejected(self):
        for key, value in (("authorization", []), ("approval_run_id", True), ("operation_id", "../private"),
                           ("project", "iteam-ai-marketing"), ("activation_policy_sha256", "sha256:" + "0" * 64),
                           ("expires_at", "2026-01-01"), ("secret", SECRET)):
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                activation.ActivationJournal(self.directory, {**self.binding, key: value})
        self.assertFalse(self.state.path.exists())

    def test_no_preactivation_or_mismatched_runtime_cannot_create_activation(self):
        with self.assertRaises(ops.ReleaseError):
            self.state.create(self.previous, {}, {})
        self.assertFalse(self.state.path.exists())
        self.assertFalse(self.state.marker_path.exists())

    def test_database_plan_binding_and_typed_passive_proof_are_required(self):
        passive = self.preactivated()
        for key, value in (("business_ready", 0), ("claims_blocked", 1), ("target_release_sha", "0" * 40)):
            state = activation.ActivationJournal(self.directory, self.binding)
            with self.subTest(key=key), self.assertRaisesRegex(ops.ReleaseError, "EVIDENCE_INVALID"):
                state.create(self.previous, self.runtime.plan, {**passive, key: value})
        state = activation.ActivationJournal(self.directory, self.binding)
        with self.assertRaisesRegex(ops.ReleaseError, "EVIDENCE_INVALID"):
            state.create(self.previous, {**self.runtime.plan, "database_identity_sha256": "sha256:" + "0" * 64}, passive)

    def test_terminal_marker_or_baseline_changed_before_handoff_is_rejected(self):
        passive = self.preactivated()
        self.previous.marker_path.write_text('{}\n')
        with self.assertRaisesRegex(ops.ReleaseError, "DEPENDENCY_CHANGED"):
            self.state.create(self.previous, self.runtime.plan, passive)
        self.assertFalse(self.state.path.exists())

    def test_baseline_hash_and_backup_scope_are_revalidated(self):
        passive = self.preactivated()
        baseline = self.directory / f"migration-runtime-{OPERATION}.json"
        baseline.write_text('{}\n')
        with self.assertRaisesRegex(ops.ReleaseError, "DEPENDENCY_CHANGED"):
            self.state.create(self.previous, self.runtime.plan, passive)

    def test_changed_shape_valid_backup_is_bound_to_completed_backup_event(self):
        passive = self.preactivated()
        path = self.directory / f"migration-backup-{OPERATION}.json"
        proof = json.loads(path.read_bytes())
        proof["encrypted_backup_sha256"] = "sha256:" + "9" * 64
        path.write_bytes(canonical_bytes(proof))
        with self.assertRaisesRegex(ops.ReleaseError, "DEPENDENCY_CHANGED"):
            self.state.create(self.previous, self.runtime.plan, passive)

    def test_passive_barrier_must_match_completed_preactivation_barrier(self):
        passive = self.preactivated()
        passive["source_barrier_sha256"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(ops.ReleaseError, "EVIDENCE_INVALID"):
            self.state.create(self.previous, self.runtime.plan, passive)

    def test_marker_file_and_directory_are_fsynced_before_permit(self):
        state = self.prepared()
        synced = []
        original = activation.os.fsync

        def sync(fd):
            item = os.fstat(fd)
            synced.append((stat.S_ISDIR(item.st_mode), item.st_ino))
            original(fd)

        with patch.object(activation.os, "fsync", side_effect=sync):
            state.begin_activation()
        state.assert_activation_permitted()
        index = synced.index((False, state.marker_path.stat().st_ino))
        self.assertIn((True, self.directory.stat().st_ino), synced[index + 1:])
        self.assertGreaterEqual(sum(directory for directory, _ in synced), 3)
        self.assertEqual(stat.S_IMODE(state.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(state.marker_path.stat().st_mode), 0o600)

    def test_marker_directory_fsync_failure_latches_without_permit(self):
        state = self.prepared()
        original = activation.os.fsync

        def fail(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and state.marker_path.exists():
                raise OSError(SECRET)
            original(fd)

        with patch.object(activation.os, "fsync", side_effect=fail):
            with self.assertRaisesRegex(ops.ReleaseError, "STATE_WRITE_FAILED"):
                state.begin_activation()
        self.assertTrue(state.marker_path.exists())
        with self.assertRaises(ops.ReleaseError):
            state.assert_activation_permitted()
        self.assertEqual(state.summary()["state_durability"], "UNKNOWN")
        self.assert_no_authority(state.summary())

    def test_readback_and_after_write_failures_do_not_grant_permit(self):
        state = self.prepared()
        original = activation.ops.save

        def fail(path, data):
            original(path, data)
            if data["phase"] == "ACTIVATION_STARTED":
                raise OSError(SECRET)

        with patch.object(activation.ops, "save", side_effect=fail):
            with self.assertRaisesRegex(ops.ReleaseError, "STATE_WRITE_FAILED"):
                state.begin_activation()
        self.assertTrue(state.marker_path.exists())
        self.assertEqual(state.summary()["phase"], "OPERATOR_RECOVERY_REQUIRED")

    def test_identical_byte_dependency_replacement_revokes_live_permit(self):
        state = self.started()
        dependency = self.previous.marker_path
        replacement = self.directory / "replacement"
        replacement.write_bytes(dependency.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, dependency)
        with self.assertRaisesRegex(ops.ReleaseError, "DEPENDENCY_CHANGED"):
            state.assert_activation_permitted()
        self.assertEqual(state.summary()["state_durability"], "UNKNOWN")

    def test_identical_byte_marker_replacement_revokes_live_permit(self):
        state = self.started()
        replacement = self.directory / "replacement"
        replacement.write_bytes(state.marker_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, state.marker_path)
        with self.assertRaisesRegex(ops.ReleaseError, "MARKER_CHANGED"):
            state.assert_activation_permitted()

    def test_unsafe_file_kinds_modes_and_links_fail_closed(self):
        state = self.started()
        os.link(state.marker_path, self.directory / "linked-marker")
        with self.assertRaisesRegex(ops.ReleaseError, "STATE_CHANGED"):
            state.assert_activation_permitted()
        fifo = self.directory / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(ops.ReleaseError, "STATE_CHANGED"):
            activation._read_private(fifo)

    def test_forked_child_cannot_use_parent_permit_or_modify_records(self):
        state = self.started()
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.assertEqual(self.run_child(inherited_child, state), 0)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})
        state.assert_activation_permitted()

    def test_crash_after_marker_fsync_cannot_resume(self):
        passive = self.preactivated()
        self.assertEqual(self.run_child(crash_child, str(self.directory), self.binding,
                                        self.previous, self.runtime.plan, passive), 73)
        self.assertEqual(json.loads(self.state.path.read_text())["phase"], "ACTIVATION_REQUESTED")
        self.assertTrue(self.state.marker_path.exists())
        replacement = activation.ActivationJournal(self.directory, self.binding)
        with self.assertRaisesRegex(ops.ReleaseError, "STATE_EXISTS"):
            replacement.create(self.previous, self.runtime.plan, passive)
        with self.assertRaises(ops.ReleaseError):
            replacement.assert_activation_permitted()

    def test_wall_clock_and_monotonic_deadlines_revoke_start_but_allow_stop(self):
        state = self.started()
        with patch.object(activation.time, "monotonic", return_value=state._deadline + 1):
            with self.assertRaisesRegex(ops.ReleaseError, "EXPIRED"):
                state.assert_activation_permitted()
        self.assertTrue(state.request_containment("ACTIVATION_AUTHORITY_EXPIRED"))
        report = state.finish_containment(containment(self.manifest))
        self.assertEqual(report["phase"], "CONTAINED")
        self.assertTrue(report["containment_verified"])
        self.assert_no_authority(report)

    def test_containment_evidence_is_bound_to_original_database(self):
        state = self.started()
        state.request_containment("ACTIVATION_FAILED")
        evidence = {**containment(self.manifest), "database_identity_sha256": "sha256:" + "9" * 64}
        result = state.finish_containment(evidence)
        self.assertEqual(result["phase"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertFalse(result["containment_verified"])

    def test_unsafe_error_cannot_persist_secrets_or_leave_authority(self):
        state = self.started()
        before = state.path.read_bytes()
        self.assertFalse(state.request_containment(SECRET))
        self.assertEqual(state.path.read_bytes(), before)
        self.assert_no_authority(state.summary())
        with self.assertRaises(ops.ReleaseError):
            state.assert_activation_permitted()

    def test_changed_final_record_revokes_positive_summary(self):
        state = self.started()
        state.request_containment("ACTIVATION_FAILED")
        self.assertTrue(state.finish_containment(containment(self.manifest))["containment_verified"])
        state.path.write_text(SECRET)
        report = state.summary()
        self.assertFalse(report["containment_verified"])
        self.assertEqual(report["state_durability"], "UNKNOWN")
        self.assert_no_authority(report)


class ActivationControllerTests(ActivationCase):
    def test_continuous_success_finishes_both_journals_and_closes_observers(self):
        result = self.execute()
        self.assertEqual(result["status"], "ACTIVE")
        self.assertEqual(self.previous.data["status"], "PRE_ACTIVATION_READY")
        self.assertEqual(self.state.data["phase"], "ACTIVE")
        self.assertEqual(self.runtime.close_count, 1)
        self.assertTrue(result["observer_cleanup_verified"])
        self.assertLess(self.source.events.index("readiness"), self.source.events.index("handoff"))
        self.assertLess(self.source.events.index("handoff"), self.source.events.index("reconnect"))
        self.assertLess(self.source.events.index("finalize"), self.source.events.index("activation_close"))
        self.assertNotIn("downgrade", self.source.events)
        self.assert_no_authority(result)
        with self.assertRaises(ops.ReleaseError):
            self.state.assert_activation_permitted()

    def test_mismatched_inventory_rejects_before_consume_or_mutation(self):
        self.source.inventory_override = lambda value: value.update(runtime_identity_sha256="sha256:" + "0" * 64)
        with self.assertRaisesRegex(ops.ReleaseError, "RUNTIME_BINDING_MISMATCH"):
            self.execute()
        self.assertFalse(self.previous.marker_path.exists())
        self.assertFalse(self.previous.path.exists())
        self.assertNotIn("backup", self.source.events)
        self.assertEqual(self.runtime.close_count, 1)

    def test_plan_failure_precedes_consumption_and_mutation(self):
        self.runtime.failures["plan"] = ops.ReleaseError("ACTIVATION_PLAN_UNSAFE")
        with self.assertRaises(ops.ReleaseError):
            self.execute()
        self.assertFalse(self.previous.marker_path.exists())
        self.assertNotIn("backup", self.source.events)

    def test_postwrite_failure_preserves_target_without_old_downgrade_path(self):
        def written(runtime, ready):
            self.assertTrue(runtime.started)
            self.source.business["sha256"] = "sha256:" + "8" * 64
            raise ops.ReleaseError("INJECTED_POST_ACTIVATION_FAILURE")

        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute(after_active=written)
        result = caught.exception.result
        self.assertEqual(result["status"], "CONTAINED")
        self.assertEqual(result["error_code"], "INJECTED_POST_ACTIVATION_FAILURE")
        self.assertEqual(self.source.schema_head, self.manifest["target_schema_head"])
        self.assertEqual(self.source.business["sha256"], "sha256:" + "8" * 64)
        self.assertNotIn("downgrade", self.source.events)
        self.assertNotIn("restore_source_configuration", self.source.events)
        self.assertTrue(self.runtime.contained)
        self.assert_no_authority(result)

    def test_marker_persistence_failure_still_attempts_physical_containment(self):
        original = activation._exclusive_write

        def fail(path, value):
            original(path, value)
            if path.suffix == ".started":
                raise OSError(SECRET)

        with patch.object(activation, "_exclusive_write", side_effect=fail):
            with self.assertRaises(controller.ActivationFailure) as caught:
                self.execute()
        self.assertTrue(self.runtime.contained)
        self.assertNotIn("recreate", self.source.events)
        self.assertEqual(caught.exception.result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assert_no_authority(caught.exception.result)

    def test_passive_failure_after_preapply_never_enters_old_downgrade_handler(self):
        self.runtime.failures["passive"] = RuntimeError(SECRET)
        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute()
        self.assertTrue(self.runtime.contained)
        self.assertNotIn("downgrade", self.source.events)
        self.assertFalse(self.state.marker_path.exists())
        self.assertEqual(caught.exception.result["error_code"], "ACTIVATION_PASSIVE_POSTFLIGHT_FAILED")
        self.assert_no_authority(caught.exception.result)

    def test_expiry_at_boundary_blocks_all_active_commands(self):
        self.runtime.hooks["handoff"] = lambda: setattr(self.state, "_deadline", 0)
        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.result["error_code"], "ACTIVATION_AUTHORITY_EXPIRED")
        self.assertEqual(caught.exception.result["status"], "CONTAINED")
        self.assertFalse(self.state.marker_path.exists())
        self.assertNotIn("start", self.source.events)

    def test_close_failure_after_active_runs_containment_before_return(self):
        def fail_first():
            if self.runtime.close_count == 1:
                raise RuntimeError(SECRET)

        self.runtime.hooks["activation_close"] = fail_first
        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.result["status"], "CONTAINED")
        self.assertEqual(caught.exception.result["error_code"], "ACTIVATION_OBSERVER_CLOSE_FAILED")
        self.assertTrue(self.runtime.contained)
        self.assertEqual(self.runtime.close_count, 2)

    def test_failed_containment_reports_recovery_with_original_safe_error(self):
        self.runtime.failures.update(start=RuntimeError(SECRET), contain_target=RuntimeError(SECRET))
        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute()
        result = caught.exception.result
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertEqual(result["error_code"], "ACTIVATION_START_FAILED")
        self.assertEqual(result["recovery_error_code"], "ACTIVATION_CONTAINMENT_FAILED")
        self.assert_no_authority(result)

    def test_state_loss_during_final_cleanup_never_reports_active(self):
        self.runtime.hooks["activation_close"] = lambda: self.state.path.write_text(SECRET)
        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute()
        self.assertEqual(caught.exception.result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertTrue(self.runtime.contained)
        self.assert_no_authority(caught.exception.result)

    def test_containment_state_loss_during_cleanup_never_reports_contained(self):
        self.runtime.failures["start"] = RuntimeError(SECRET)
        self.runtime.hooks["activation_close"] = lambda: self.state.path.write_text(SECRET)
        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute()
        result = caught.exception.result
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertFalse(result["activation_state"]["containment_verified"])

    def test_persistent_close_failure_preserves_structured_recovery_evidence(self):
        self.runtime.failures["activation_close"] = RuntimeError(SECRET)
        with self.assertRaises(controller.ActivationFailure) as caught:
            self.execute()
        result = caught.exception.result
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertTrue(self.runtime.contained)
        self.assertEqual(result["error_code"], "ACTIVATION_OBSERVER_CLOSE_FAILED")
        self.assertEqual(result["recovery_error_code"], "ACTIVATION_OBSERVER_CLOSE_FAILED")
        self.assertFalse(result["observer_cleanup_verified"])
        self.assert_no_authority(result)

    def test_existing_auxiliary_plan_is_allowed_but_saved_operation_is_not(self):
        self.runtime.prepare_target_plan()
        controller._new_operation(self.directory)
        activation._exclusive_write(self.state.marker_path, {"version": 1})
        with self.assertRaisesRegex(ops.ReleaseError, "OPERATION_EXISTS"):
            controller._new_operation(self.directory)

    def test_cli_rejects_resume_and_override_with_fixed_diagnostic(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = controller.main(["apply", "--resume", SECRET])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["error_code"], "ACTIVATION_ARGUMENTS_INVALID")
        self.assertNotIn(SECRET, output.getvalue())

    def test_plan_output_is_private_exclusive_and_cannot_overwrite(self):
        destination = self.directory / "reviewable-plan.json"
        controller.publish_plan(destination, {"activation_authorized": False})
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        before = destination.read_bytes()
        with self.assertRaisesRegex(ops.ReleaseError, "OUTPUT_UNSAFE"):
            controller.publish_plan(destination, {"activation_authorized": True})
        self.assertEqual(destination.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
