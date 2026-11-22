"""Exercise the real pre-activation engine and journal without production I/O."""

from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_apply_backup as backup
import migration_apply_state as state
import migration_release_apply as apply
import migration_writer_barrier as barrier
import release_ops as ops
from test_release_manifest import manifest

OPERATION = "a" * 32
DATABASE_IDENTITY = "sha256:" + "b" * 64
BUSINESS_HASH = "sha256:" + "c" * 64
RUNTIME_IDENTITY = "sha256:" + "d" * 64
BARRIER_HASH = "sha256:" + "e" * 64
LEGACY_COUNTS = {
    "users": 3, "campaigns": 1, "tasks": 1, "content_items": 1,
    "content_versions": 1, "approvals": 1, "publications": 1,
    "activity_logs": 1, "agent_runs": 0,
}


def approval_metadata():
    return {
        "approval_run_id": 501, "artifact_id": 502,
        "receipt_sha256": "sha256:" + "f" * 64,
        "approved_at": datetime.now(UTC).isoformat(),
    }


class FakeRuntime:
    """Track schema, data, writer fence and observable action side effects."""

    def __init__(self, value, journal):
        self.manifest = value
        self.journal = journal
        self.events = []
        self.calls = {}
        self.failures = {}
        self.hooks = {}
        self.schema_head = value["source_schema_head"]
        self.current_build = value["source_release_sha"]
        self.database_identity = DATABASE_IDENTITY
        self.business = {"sha256": BUSINESS_HASH, "table_counts": copy.deepcopy(LEGACY_COUNTS)}
        self.schedulers_stopped = False
        self.workers_drained = False
        self.writers_stopped = False
        self.fenced = False
        self.continuous = True
        self.source_config_restored = False
        self.backup_override = None
        self.inventory_override = None
        self.barrier_override = None

    def _point(self, name, occurrence, when):
        key = (name, occurrence, when)
        if key in self.hooks:
            self.hooks[key](self)
        if key in self.failures:
            raise self.failures[key]

    def _start(self, name, *, mutating=False):
        self.events.append(name)
        occurrence = self.calls.get(name, 0) + 1
        self.calls[name] = occurrence
        if mutating:
            # Every actual mutation, including backup-container creation, must
            # see durable one-use consumption. Containment may be unjournaled
            # only after the journal itself became unsafe.
            assert self.journal.marker_path.is_file()
            if name != "containment":
                persisted = json.loads(self.journal.path.read_text())
                assert persisted["pending_action"] == name
        self._point(name, occurrence, "before")
        return occurrence

    def _end(self, name, occurrence):
        self._point(name, occurrence, "after")

    def snapshot_value(self):
        counts = {**self.business["table_counts"], "alembic_version": 1}
        if self.schema_head != self.manifest["source_schema_head"]:
            counts.update(workspaces=1, workspace_memberships=2)
        return {
            "build_sha": self.current_build, "schema": self.schema_head,
            "database_identity_sha256": self.database_identity,
            "business_state": copy.deepcopy(self.business), "table_counts": counts,
        }

    def inventory(self):
        count = self._start("inventory")
        value = {
            "snapshot": self.snapshot_value(),
            "source_image_ids": {
                name: "sha256:" + ("1" if name == "frontend" else "2") * 64
                for name in ops.APP
            },
            "runtime_identity_sha256": RUNTIME_IDENTITY,
        }
        if self.inventory_override:
            self.inventory_override(value)
        self._end("inventory", count)
        return value

    def quiet(self):
        count = self._start("quiet")
        self._end("quiet", count)

    def backup(self):
        count = self._start("backup", mutating=True)
        value = {
            "operation_id": self.journal.data["operation_id"],
            "manifest_sha256": self.journal.data["manifest_sha256"],
            "database_identity_sha256": self.database_identity,
            "source_release_sha": self.manifest["source_release_sha"],
            "source_schema_head": self.manifest["source_schema_head"],
            "source_backend_image": self.manifest["source_images"]["backend"],
            "proof_version": 1, "status": "PASS",
            "created_at": datetime.now(UTC).isoformat(),
            "encrypted_backup_sha256": "sha256:" + "3" * 64,
            "source_business_state": copy.deepcopy(self.business),
            "restored_business_state": copy.deepcopy(self.business),
            "table_counts_match": True, "isolated_restore_network": "none",
            "restore_container_removed": True,
        }
        if self.backup_override:
            self.backup_override(value)
        self._end("backup", count)
        return value

    def stop_schedulers(self):
        count = self._start("stop_schedulers", mutating=True)
        self.schedulers_stopped = True
        self._end("stop_schedulers", count)

    def drain_workers(self):
        count = self._start("drain_workers", mutating=True)
        assert self.schedulers_stopped
        self.workers_drained = True
        self._end("drain_workers", count)

    def stop_writers(self):
        count = self._start("stop_writers", mutating=True)
        assert self.workers_drained
        self.writers_stopped = True
        self._end("stop_writers", count)

    def fence_database(self):
        count = self._start("fence_database", mutating=True)
        assert self.writers_stopped
        self.fenced = True
        self._end("fence_database", count)

    def barrier(self):
        count = self._start("barrier")
        value = {
            "writers_stopped": self.writers_stopped, "database_fenced": self.fenced,
            "barrier_continuous": self.continuous, "barrier_sha256": BARRIER_HASH,
        }
        if self.barrier_override:
            self.barrier_override(value)
        self._end("barrier", count)
        return value

    def snapshot(self, tables=None):
        count = self._start("snapshot")
        assert tables == sorted(self.business["table_counts"])
        result = self.snapshot_value()
        self._end("snapshot", count)
        return result

    def target_preflight(self):
        count = self._start("target_preflight")
        assert self.fenced and self.writers_stopped
        self._end("target_preflight", count)

    def upgrade_exact(self):
        count = self._start("upgrade", mutating=True)
        assert self.fenced and self.writers_stopped and self.continuous
        assert self.schema_head == self.manifest["source_schema_head"]
        self.schema_head = self.manifest["target_schema_head"]
        self.current_build = self.manifest["target_release_sha"]
        self._end("upgrade", count)

    def target_checks(self):
        count = self._start("target_checks")
        assert self.schema_head == self.manifest["target_schema_head"]
        assert self.writers_stopped and self.fenced
        self._end("target_checks", count)

    def maintenance_readiness(self):
        count = self._start("readiness", mutating=True)
        assert self.schema_head == self.manifest["target_schema_head"]
        assert self.writers_stopped and self.fenced and self.continuous
        self._end("readiness", count)

    def downgrade_exact(self):
        count = self._start("downgrade", mutating=True)
        assert self.schema_head == self.manifest["target_schema_head"]
        assert self.manifest["pre_activation_rollback_policy"] == "PRE_ACTIVATION_DOWNGRADE"
        assert self.writers_stopped and self.fenced and self.continuous
        self.schema_head = self.manifest["source_schema_head"]
        self.current_build = self.manifest["source_release_sha"]
        self._end("downgrade", count)

    def source_checks(self):
        count = self._start("source_checks")
        assert self.schema_head == self.manifest["source_schema_head"]
        assert self.writers_stopped and self.fenced
        self._end("source_checks", count)

    def restore_source_configuration(self):
        count = self._start("restore_source_configuration", mutating=True)
        assert self.schema_head == self.manifest["source_schema_head"]
        assert self.writers_stopped and self.fenced
        self.source_config_restored = True
        self._end("restore_source_configuration", count)

    def contain(self):
        count = self._start("containment", mutating=True)
        self.schedulers_stopped = self.writers_stopped = self.fenced = True
        # Re-closing a lost physical barrier cannot restore historical continuity.
        self._end("containment", count)

    def close(self):
        count = self._start("close")
        self._end("close", count)


class MigrationReleaseApplyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case_count = 0

    def case(self, *, value=None, binding=None):
        self.case_count += 1
        directory = self.root / str(self.case_count)
        directory.mkdir(mode=0o700)
        value = value or manifest()
        journal = state.Journal(directory, OPERATION, value, binding or approval_metadata())
        return FakeRuntime(value, journal), journal

    def run_engine(self, runtime, journal, **kwargs):
        with ops.locked_state(journal.state_dir):
            return apply.execute(runtime, journal, **kwargs)

    def failure(self, runtime, journal):
        with self.assertRaises(apply.ApplyFailure) as raised:
            self.run_engine(runtime, journal)
        self.assertIs(raised.exception.result["automatic_retry"], False)
        self.assertNotIn("SECRET", json.dumps(raised.exception.result))
        self.assertNotIn("SECRET", journal.path.read_text())
        return raised.exception.result

    def test_success_runs_all_real_phases_and_finishes_with_writers_disabled(self):
        runtime, journal = self.case()
        phases = []
        result = self.run_engine(runtime, journal, progress=phases.append)
        self.assertEqual(result["status"], "PRE_ACTIVATION_READY")
        self.assertEqual(result["schema"], runtime.manifest["target_schema_head"])
        self.assertEqual(result["business_sha256"], BUSINESS_HASH)
        self.assertEqual(result["writer_boundary"], "PROVEN_CLOSED")
        self.assertEqual(phases, list(state.FORWARD_PREDECESSORS))
        self.assertEqual(journal.data["status"], "PRE_ACTIVATION_READY")
        self.assertTrue(runtime.writers_stopped and runtime.fenced)
        self.assertNotIn("downgrade", runtime.events)
        self.assertNotIn("restore_source_configuration", runtime.events)
        self.assertEqual(runtime.calls["quiet"], 2)
        self.assertEqual(runtime.calls["upgrade"], 1)
        self.assertFalse(hasattr(runtime, "activate_writers"))

    def test_wrong_source_is_rejected_before_journal_consumption_or_runtime_mutation(self):
        for field, value in (
            ("schema", "20261121_0029"), ("build_sha", "9" * 40),
            ("business_state", {"sha256": BUSINESS_HASH, "table_counts": {"users": True}}),
        ):
            runtime, journal = self.case()
            runtime.inventory_override = lambda data, f=field, v=value: data["snapshot"].__setitem__(f, v)
            with self.subTest(field=field), self.assertRaises(ops.ReleaseError):
                self.run_engine(runtime, journal)
            self.assertFalse(journal.path.exists())
            self.assertFalse(journal.marker_path.exists())
            self.assertEqual(runtime.events, ["inventory", "quiet"])

    def test_initial_quiet_failure_does_not_consume_or_change_runtime(self):
        runtime, journal = self.case()
        runtime.failures[("quiet", 1, "before")] = ops.ReleaseError("MIGRATION_DRAIN_TIMEOUT")
        with self.assertRaisesRegex(ops.ReleaseError, "DRAIN_TIMEOUT"):
            self.run_engine(runtime, journal)
        self.assertFalse(journal.path.exists())
        self.assertFalse(journal.marker_path.exists())
        self.assertNotIn("containment", runtime.events)

    def test_expired_or_future_approval_is_rejected_at_consumption_before_runtime_mutations(self):
        now = datetime.now(UTC)
        for approved_at in (now - timedelta(hours=2), now + timedelta(hours=1)):
            binding = approval_metadata()
            binding["approved_at"] = approved_at.isoformat()
            runtime, journal = self.case(binding=binding)
            with self.subTest(time=approved_at), self.assertRaisesRegex(ops.ReleaseError, "APPROVAL_EXPIRED"):
                self.run_engine(runtime, journal)
            self.assertEqual(journal.data["phase"], "VERIFIED")
            self.assertFalse(journal.marker_path.exists())
            self.assertEqual(runtime.events, ["inventory", "quiet"])

    def test_backup_contract_rejects_identity_cleanup_time_types_and_raw_payloads(self):
        mutations = (
            ("operation_id", "b" * 32), ("manifest_sha256", "sha256:" + "0" * 64),
            ("database_identity_sha256", "sha256:" + "0" * 64),
            ("source_release_sha", "0" * 40), ("source_schema_head", "20261121_0029"),
            ("source_backend_image", "backend:latest"), ("proof_version", True),
            ("status", "FAIL"), ("table_counts_match", 1),
            ("isolated_restore_network", "host"), ("restore_container_removed", False),
            ("created_at", (datetime.now(UTC) - timedelta(hours=2)).isoformat()),
            ("created_at", (datetime.now(UTC) + timedelta(hours=2)).isoformat()),
            ("created_at", "2026-11-22T12:00:00"), ("created_at", 1),
            ("encrypted_backup_sha256", "SECRET"), ("credentials", "SECRET"),
        )
        for field, value in mutations:
            runtime, journal = self.case()
            runtime.backup_override = lambda proof, f=field, v=value: proof.__setitem__(f, v)
            result = self.failure(runtime, journal)
            with self.subTest(field=field, value=value):
                self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
                self.assertEqual(result["error_code"], "MIGRATION_BACKUP_BINDING_MISMATCH")
                self.assertNotIn("upgrade", runtime.events)
                self.assertNotIn("downgrade", runtime.events)
                self.assertEqual(runtime.calls["backup"], 1)

    def test_backup_and_restored_hash_or_counts_must_match(self):
        for key, replacement in (("sha256", "sha256:" + "0" * 64), ("table_counts", {"users": 2})):
            runtime, journal = self.case()
            runtime.backup_override = lambda proof, k=key, v=replacement: proof["restored_business_state"].__setitem__(k, v)
            result = self.failure(runtime, journal)
            self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
            self.assertNotIn("upgrade", runtime.events)

    def test_backup_can_capture_a_new_consistent_state_before_writer_quiescence(self):
        runtime, journal = self.case()
        runtime.hooks[("backup", 1, "before")] = lambda value: value.business.__setitem__("sha256", "sha256:" + "4" * 64)
        result = self.run_engine(runtime, journal)
        self.assertEqual(result["status"], "PRE_ACTIVATION_READY")
        self.assertEqual(result["business_sha256"], "sha256:" + "4" * 64)

    def test_data_changed_after_backup_blocks_upgrade_without_replacing_baseline(self):
        for change in ("hash", "count", "identity"):
            runtime, journal = self.case()
            def changed(value):
                if change == "hash":
                    value.business["sha256"] = "sha256:" + "5" * 64
                elif change == "count":
                    value.business["table_counts"]["users"] += 1
                else:
                    value.database_identity = "sha256:" + "6" * 64
            runtime.hooks[("stop_writers", 1, "after")] = changed
            result = self.failure(runtime, journal)
            with self.subTest(change=change):
                self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
                self.assertNotIn("upgrade", runtime.events)
                self.assertNotIn("downgrade", runtime.events)
                self.assertEqual(runtime.calls["backup"], 1)

    def test_every_action_failure_is_contained_and_cannot_replay_or_activate(self):
        actions = list(state.FORWARD_PREDECESSORS)
        for action in actions:
            for when in ("before", "after"):
                runtime, journal = self.case()
                runtime.failures[(action, 1, when)] = RuntimeError("SECRET details")
                result = self.failure(runtime, journal)
                with self.subTest(action=action, when=when):
                    self.assertEqual(runtime.calls["containment"], 1)
                    self.assertTrue(runtime.writers_stopped and runtime.fenced)
                    self.assertLessEqual(runtime.calls.get("upgrade", 0), 1)
                    self.assertLessEqual(runtime.calls.get("downgrade", 0), 1)
                    self.assertTrue(journal.marker_path.is_file())
                    if action in {"backup", "stop_schedulers", "drain_workers", "stop_writers", "fence_database"}:
                        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
                        self.assertNotIn("downgrade", runtime.events)
                    else:
                        self.assertEqual(result["status"], "ROLLED_BACK_PRE_ACTIVATION")
                        self.assertEqual(runtime.schema_head, runtime.manifest["source_schema_head"])
                        self.assertTrue(runtime.source_config_restored)

    def test_writer_barrier_diagnostics_remain_specific_and_allowlisted(self):
        for code, expected in (
            ("WRITER_BARRIER_EVENT_SYNC_FAILED", "WRITER_BARRIER_EVENT_SYNC_FAILED"),
            ("WRITER_BARRIER_EVENT_MONITOR_FAILED", "WRITER_BARRIER_EVENT_MONITOR_FAILED"),
            ("WRITER_BARRIER_CONTINUITY_LOST", "WRITER_BARRIER_CONTINUITY_LOST"),
            ("WRITER_BARRIER_SECRET_PASSWORD", "UNEXPECTED_MIGRATION_APPLY_ERROR"),
        ):
            runtime, journal = self.case()
            runtime.failures[("stop_schedulers", 1, "before")] = ops.ReleaseError(code)
            result = self.failure(runtime, journal)
            with self.subTest(code=code):
                self.assertEqual(result["error_code"], expected)
                self.assertEqual(journal.data["failure_code"], expected)
                self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
                self.assertEqual(runtime.calls["containment"], 1)
                self.assertTrue(runtime.writers_stopped and runtime.fenced)
                self.assertNotIn("upgrade", runtime.events)
                self.assertNotIn("downgrade", runtime.events)
                self.assertTrue(journal.marker_path.is_file())

    def test_containment_keeps_original_and_specific_barrier_error(self):
        runtime, journal = self.case()
        runtime.failures[("readiness", 1, "before")] = RuntimeError("SECRET initial")
        runtime.failures[("containment", 1, "before")] = ops.ReleaseError("WRITER_BARRIER_CONTINUITY_LOST")
        result = self.failure(runtime, journal)
        self.assertEqual(result["error_code"], "MIGRATION_READINESS_FAILED")
        self.assertEqual(result["recovery_error_code"], "WRITER_BARRIER_CONTINUITY_LOST")
        self.assertEqual(journal.data["recovery_error_code"], "WRITER_BARRIER_CONTINUITY_LOST")
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertNotIn("downgrade", runtime.events)

    def test_target_and_readiness_failures_recheck_signed_target_before_downgrade(self):
        for action in ("target_checks", "readiness"):
            runtime, journal = self.case()
            runtime.failures[(action, 1, "after")] = RuntimeError("SECRET")
            result = self.failure(runtime, journal)
            self.assertEqual(result["status"], "ROLLED_BACK_PRE_ACTIVATION")
            self.assertEqual(runtime.calls["target_preflight"], 2)
            self.assertLess(runtime.events.index("containment"), runtime.events.index("downgrade"))
            self.assertLess(runtime.events.index("downgrade"), runtime.events.index("restore_source_configuration"))
            self.assertEqual(runtime.business["sha256"], BUSINESS_HASH)

    def test_fail_forward_policy_never_downgrades_target_schema(self):
        value = manifest()
        value["pre_activation_rollback_policy"] = "FAIL_FORWARD_ONLY"
        from release_manifest import migration_plan_hash
        value["migration_plan_hash"] = migration_plan_hash(**{
            key: value[key] for key in (
                "source_schema_head", "target_schema_head", "migration_path",
                "compatibility_class", "pre_activation_rollback_policy",
            )
        })
        value["rehearsal_proof"]["migration_plan_hash"] = value["migration_plan_hash"]
        runtime, journal = self.case(value=value)
        runtime.failures[("readiness", 1, "after")] = RuntimeError("SECRET")
        result = self.failure(runtime, journal)
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertEqual(runtime.schema_head, value["target_schema_head"])
        self.assertNotIn("downgrade", runtime.events)
        self.assertNotIn("restore_source_configuration", runtime.events)

    def test_intermediate_unknown_or_changed_data_state_prevents_downgrade(self):
        for change in ("intermediate", "unknown", "data", "identity"):
            runtime, journal = self.case()
            def changed(value):
                if change == "intermediate":
                    value.schema_head = "20261121_0029"
                elif change == "unknown":
                    value.schema_head = "unexpected_schema"
                elif change == "data":
                    value.business["sha256"] = "sha256:" + "9" * 64
                else:
                    value.database_identity = "sha256:" + "9" * 64
            runtime.hooks[("upgrade", 1, "after")] = changed
            result = self.failure(runtime, journal)
            with self.subTest(change=change):
                self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
                self.assertNotIn("downgrade", runtime.events)
                self.assertNotIn("restore_source_configuration", runtime.events)

    def test_missing_or_lost_continuity_cannot_be_repaired_by_containment(self):
        for mode in ("missing", "wrong_type", "lost_after_upgrade"):
            runtime, journal = self.case()
            if mode == "missing":
                runtime.barrier_override = lambda value: value.pop("barrier_continuous")
            elif mode == "wrong_type":
                runtime.barrier_override = lambda value: value.__setitem__("barrier_continuous", 1)
            else:
                runtime.hooks[("upgrade", 1, "after")] = lambda value: setattr(value, "continuous", False)
            result = self.failure(runtime, journal)
            with self.subTest(mode=mode):
                self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
                self.assertEqual(result["writer_boundary"], "UNKNOWN")
                self.assertNotIn("downgrade", runtime.events)
                self.assertNotIn("restore_source_configuration", runtime.events)

    def test_event_sync_timeout_remains_ineligible_for_downgrade_after_stream_recovers(self):
        runtime, journal = self.case()
        audit = barrier.EventAudit(OPERATION, set(), "a" * 64, set())
        audit.process = types.SimpleNamespace(poll=lambda: None)
        delivery = True
        pending = []

        def command(args):
            event = {
                "Type": "volume", "Action": "create" if args[2] == "create" else "destroy",
                "Actor": {"ID": args[3]},
            }
            if delivery:
                audit.observe(event)
            else:
                pending.append(event)
            return args[3]

        def timeout(value):
            nonlocal delivery
            delivery = False
            audit.sync(timeout=0)

        def recover_stream(value):
            nonlocal delivery
            delivery = True
            for event in pending:
                audit.observe(event)
            self.assertIsNone(audit.process.poll())

        runtime.barrier_override = lambda value: audit.sync()
        runtime.hooks[("readiness", 1, "before")] = timeout
        runtime.hooks[("containment", 1, "before")] = recover_stream
        with patch.object(ops, "command", side_effect=command):
            audit.arm()
            result = self.failure(runtime, journal)
        self.assertEqual(result["error_code"], "WRITER_BARRIER_EVENT_SYNC_FAILED")
        self.assertEqual(result["recovery_error_code"], "WRITER_BARRIER_CONTINUITY_LOST")
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertEqual(result["writer_boundary"], "UNKNOWN")
        self.assertEqual(runtime.calls["upgrade"], 1)
        self.assertEqual(runtime.calls["containment"], 1)
        self.assertNotIn("downgrade", runtime.events)
        self.assertNotIn("restore_source_configuration", runtime.events)

    def test_recovery_preflight_downgrade_or_source_failure_retains_original_error(self):
        failures = (
            ("target_preflight", 2, "before"), ("downgrade", 1, "before"),
            ("downgrade", 1, "after"), ("source_checks", 2, "before"),
            ("restore_source_configuration", 1, "before"),
        )
        for failure in failures:
            runtime, journal = self.case()
            runtime.failures[("readiness", 1, "before")] = RuntimeError("SECRET initial")
            runtime.failures[failure] = RuntimeError("SECRET recovery")
            result = self.failure(runtime, journal)
            with self.subTest(failure=failure):
                self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
                self.assertEqual(result["error_code"], "MIGRATION_READINESS_FAILED")
                self.assertIsNotNone(result["recovery_error_code"])
                self.assertTrue(runtime.writers_stopped and runtime.fenced)

    def test_containment_failure_cannot_trigger_schema_recovery(self):
        runtime, journal = self.case()
        runtime.failures[("readiness", 1, "before")] = RuntimeError("SECRET initial")
        runtime.failures[("containment", 1, "before")] = RuntimeError("SECRET containment")
        result = self.failure(runtime, journal)
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertEqual(result["recovery_error_code"], "MIGRATION_CONTAINMENT_FAILED")
        self.assertNotIn("downgrade", runtime.events)

    def test_journal_write_failure_after_upgrade_allows_containment_only(self):
        runtime, journal = self.case()
        original = ops.save

        def fail_after_upgrade(path, data):
            if data["phase"] == "UPGRADE_COMPLETED" and data["pending_action"] is None:
                raise OSError("SECRET persistence failure")
            return original(path, data)

        with patch.object(state.ops, "save", side_effect=fail_after_upgrade):
            result = self.failure(runtime, journal)
        self.assertEqual(result["status"], "OPERATOR_RECOVERY_REQUIRED")
        self.assertEqual(result["error_code"], "MIGRATION_STATE_WRITE_FAILED")
        self.assertEqual(runtime.schema_head, runtime.manifest["target_schema_head"])
        self.assertNotIn("downgrade", runtime.events)
        self.assertEqual(json.loads(journal.path.read_text())["pending_action"], "upgrade")

    def test_keyboard_interrupt_is_sanitized_and_recovery_stays_within_boundary(self):
        for action in ("backup", "readiness"):
            runtime, journal = self.case()
            runtime.failures[(action, 1, "before")] = KeyboardInterrupt("SECRET interruption")
            result = self.failure(runtime, journal)
            self.assertTrue(runtime.writers_stopped and runtime.fenced)
            expected = "OPERATOR_RECOVERY_REQUIRED" if action == "backup" else "ROLLED_BACK_PRE_ACTIVATION"
            self.assertEqual(result["status"], expected)

    def test_unknown_exception_details_are_absent_from_cli_output(self):
        for error in (RuntimeError("SECRET exception"), KeyboardInterrupt("SECRET interruption")):
            output = io.StringIO()
            argv = [
                "migration_release_apply.py", "--root", str(self.root),
                "--approval-run", "501", "--approval-artifact", "502",
                "--backup-dir", str(self.root / "backup"), "--key-dir", str(self.root / "keys"),
            ]
            with patch.object(sys, "argv", argv), patch.object(apply, "apply", side_effect=error), redirect_stdout(output), self.assertRaises(SystemExit) as exit:
                apply.main()
            self.assertEqual(exit.exception.code, 1)
            report = json.loads(output.getvalue())
            self.assertEqual(report["error_code"], "UNEXPECTED_MIGRATION_APPLY_ERROR")
            self.assertNotIn("SECRET", output.getvalue())

    def test_existing_operation_blocks_even_a_new_approval_before_authentication(self):
        directory = self.root / "shared-state"
        directory.mkdir(mode=0o700)
        (directory / "operation-existing.json").write_text('{"pending_action":"upgrade"}')
        module = types.ModuleType("migration_apply_runtime")
        module.production_runtime = lambda *_args: self.fail("must not construct runtime")
        with patch.dict(sys.modules, {"migration_apply_runtime": module}), patch.object(apply, "state_directory", return_value=directory), patch.object(apply.approval, "verify_approved") as verify, self.assertRaisesRegex(ops.ReleaseError, "OPERATION_ALREADY_EXISTS"):
            apply.apply(self.root, 999, 1000, self.root / "backup", self.root / "keys", "cosign")
        verify.assert_not_called()

    def test_production_entry_uses_shared_lock_verified_receipt_and_closes_adapter(self):
        directory = self.root / "shared-state"
        value = manifest()
        receipt = {"review": {"migration_manifest": value}}
        runtime_holder = []
        module = types.ModuleType("migration_apply_runtime")

        def factory(root, actual_manifest, operation, actual_state, backup_dir, key_dir):
            self.assertEqual(root, self.root)
            self.assertEqual(actual_manifest, value)
            self.assertEqual(actual_state, directory)
            self.assertEqual(operation, OPERATION)
            with self.assertRaisesRegex(ops.ReleaseError, "ANOTHER_RELEASE_OPERATION_RUNNING"):
                with ops.locked_state(actual_state):
                    self.fail("the entry point must already own the shared lock")
            # This second object is only a view of the future durable paths; the
            # engine receives the authentic Journal constructed inside apply().
            journal = state.Journal(directory, operation, value, approval_metadata())
            runtime = FakeRuntime(actual_manifest, journal)
            runtime_holder.append(runtime)
            return runtime

        module.production_runtime = factory
        output = io.StringIO()
        with patch.dict(sys.modules, {"migration_apply_runtime": module}), patch.object(apply, "state_directory", return_value=directory), patch.object(apply.approval, "verify_approved", return_value=receipt) as verify, patch.object(apply.approval, "trusted_run", return_value={"updated_at": datetime.now(UTC).isoformat()}), patch.object(apply.secrets, "token_hex", return_value=OPERATION), redirect_stdout(output):
            result = apply.apply(self.root, 501, 502, self.root / "backup", self.root / "keys", "cosign")
        verify.assert_called_once_with(501, 502, "cosign")
        self.assertEqual(result["status"], "PRE_ACTIVATION_READY")
        self.assertEqual(runtime_holder[0].calls["close"], 1)
        self.assertNotIn("SECRET", output.getvalue())


if __name__ == "__main__":
    unittest.main()
