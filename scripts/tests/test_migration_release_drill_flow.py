"""Exercise the real rehearsal orchestration without Docker or external APIs."""

from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from uuid import uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_migration_release_drill import SOURCE_SCHEMA, TARGET_SCHEMA, request

drill = importlib.import_module("migration_release_drill")
contracts = importlib.import_module("release_manifest")
business_fixture = importlib.import_module("migration_business_fixture")


class StatefulRuntime:
    """Model DB replacement and image selection, not the orchestration itself."""

    def __init__(self, failure=None):
        self.failure = failure
        self.events = []
        self.image = "source"
        self.phase = "empty"
        self.head = None
        self.apps_running = False
        self.restored_checks = set()
        self.target_upgrades = 0
        self.cleanup_calls = 0
        self.baseline = {
            "table_counts": {
                **{name: 1 for name in business_fixture.REQUIRED_BUSINESS_TABLES},
                "users": 3,
            },
            "sha256": "sha256:" + "a" * 64,
        }

    def event(self, name):
        self.events.append(name)
        if self.failure == name:
            raise drill.ReleaseError("INJECTED_" + name.upper())

    def compose(self, *args):
        assert args == (
            "up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis"
        ), args
        self.event("infrastructure")
        return ""

    def run_backend(self, *args):
        if args == ("alembic", "upgrade", SOURCE_SCHEMA):
            assert self.image == "source" and self.phase == "empty"
            self.event("source_upgrade")
            self.phase, self.head = "source", SOURCE_SCHEMA
        elif args == ("alembic", "upgrade", TARGET_SCHEMA):
            assert self.phase == "restored" and not self.apps_running
            assert self.image == "target"
            assert self.restored_checks == {"schema", "objects", "data", "alembic"}
            self.target_upgrades += 1
            self.event("target_upgrade")
            self.phase, self.head = "upgraded", TARGET_SCHEMA
        elif args == ("alembic", "downgrade", SOURCE_SCHEMA):
            assert self.phase == "upgraded" and not self.apps_running
            assert self.image == "target", "Downgrade must use target migration code"
            self.event("downgrade")
            self.phase, self.head = "downgraded", SOURCE_SCHEMA
        elif args == ("alembic", "check"):
            expected_image = "target" if self.phase == "upgraded" else "source"
            assert self.image == expected_image
            self.event(self.phase + "_check")
            if self.phase == "restored":
                assert self.restored_checks == {"schema", "objects", "data"}
                self.restored_checks.add("alembic")
        else:
            raise AssertionError(args)
        return ""

    def schema(self):
        self.event(self.phase + "_schema")
        if self.failure == self.phase + "_schema_mismatch":
            return "unexpected_schema"
        if self.phase == "restored":
            self.restored_checks.add("schema")
        return self.head

    def business_state(self):
        self.event(self.phase + "_data")
        state = copy.deepcopy(self.baseline)
        if self.failure == self.phase + "_count_mismatch":
            state["table_counts"]["campaigns"] += 1
        if self.failure == self.phase + "_hash_mismatch":
            state["sha256"] = "sha256:" + "b" * 64
        if self.phase == "restored" and state == self.baseline:
            self.restored_checks.add("data")
        return state

    def source_schema_restored(self):
        assert self.image == "source" and self.head == SOURCE_SCHEMA
        self.event(self.phase + "_objects")
        if self.phase == "restored":
            self.restored_checks.add("objects")

    def switch_images(self, value, *, target):
        assert not self.apps_running
        self.image = "target" if target else "source"
        self.event("switch_" + self.image)

    def app_ready(self, value, *, target):
        assert self.image == ("target" if target else "source")
        self.event(self.phase + "_ready")
        self.apps_running = True

    def stop_app(self):
        self.event(self.phase + "_stop")
        self.apps_running = False

    def workspace_state(self):
        assert self.phase == "upgraded"
        self.event("workspace_invariants")
        users = [
            {"id": "c" * 8 + "-cccc-4ccc-8ccc-" + "c" * 12,
             "role": "ADMIN", "is_active": True},
            {"id": str(drill.ACTIVE_MANAGER_ID), "role": "MANAGER", "is_active": True},
            {"id": str(drill.INACTIVE_MANAGER_ID), "role": "MANAGER", "is_active": False},
        ]
        workspace = {
            "id": str(drill.DEFAULT_WORKSPACE_ID),
            "name": drill.DEFAULT_WORKSPACE_NAME,
            "slug": drill.DEFAULT_WORKSPACE_SLUG,
            "is_default": True,
            "is_active": True,
        }
        if self.failure == "invalid_workspace":
            workspace["slug"] = "wrong-workspace"
        memberships = [
            {
                "id": str(uuid5(drill.DEFAULT_WORKSPACE_ID, "membership:" + user["id"])),
                "workspace_id": str(drill.DEFAULT_WORKSPACE_ID),
                "user_id": user["id"],
                "role": drill.ROLE_MAP[user["role"]],
                "is_active": True,
            }
            for user in users if user["is_active"]
        ]
        return users, [workspace], memberships


class RehearsalHarness:
    def __init__(self, output, failure=None):
        self.output = output
        self.runtime = StatefulRuntime(failure)
        self.backup_proof = {"checksum": "d" * 64}
        self.saved = None

    def create(self, root, project, value):
        self.runtime.root, self.runtime.project = root, project
        self.runtime.event("create")
        return self.runtime

    def backup(self, runtime, *paths):
        assert runtime.phase == "source" and runtime.apps_running
        runtime.event("backup")

    def check_backup(self, *args):
        self.runtime.event("check_backup")
        return self.backup_proof

    def restore(self, runtime, proof):
        assert proof is self.backup_proof
        assert not runtime.apps_running and runtime.image == "source"
        runtime.event("restore")
        runtime.phase, runtime.head = "restored", SOURCE_SCHEMA

    def cleanup(self, runtime):
        runtime.cleanup_calls += 1
        runtime.event("cleanup")

    def run(self, value):
        real_save = drill.save

        def save_output(path, proof):
            assert self.runtime.events[-1] == "cleanup"
            self.runtime.event("save")
            real_save(path, proof)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(drill.os.environ, {}, clear=True))
            patches = {
                "command": lambda args: str(8 * 1024**3),
                "verify_images": lambda *args: self.runtime.event("verify_images"),
                "create_drill": self.create,
                "seed_fixture": lambda runtime: runtime.event("seed"),
                "backup_restore": self.backup,
                "check_proof": self.check_backup,
                "restore_fixture": self.restore,
                "verify_disposable_topology": lambda runtime, project: runtime.event(runtime.phase + "_topology"),
                "remove_disposable_project": self.cleanup,
            }
            for name, implementation in patches.items():
                stack.enter_context(patch.object(drill, name, side_effect=implementation))
            self.saved = stack.enter_context(patch.object(drill, "save", side_effect=save_output))
            return drill.rehearse(value, self.output, "cosign")


class MigrationReleaseDrillFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name) / "proof.json"

    def assert_failed_cleanly(self, harness, value):
        with self.assertRaises(drill.ReleaseError):
            harness.run(value)
        self.assertEqual(harness.runtime.cleanup_calls, 1)
        harness.saved.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_restored_database_is_checked_before_target_upgrade_and_downgrade(self):
        value = request()
        harness = RehearsalHarness(self.output)
        proof = harness.run(value)
        events = harness.runtime.events
        ordered_gates = [
            "source_stop", "restore", "restored_schema", "restored_objects",
            "restored_data", "restored_check", "switch_target", "target_upgrade",
            "upgraded_ready", "upgraded_stop", "downgrade", "switch_source",
            "downgraded_check", "downgraded_ready", "downgraded_data", "cleanup", "save",
        ]
        positions = [events.index(event) for event in ordered_gates]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(proof, json.loads(self.output.read_text()))
        self.assertEqual(proof["status"], "PASS")
        self.assertTrue(proof["migration_executed_on_restored_database"])
        self.assertTrue(proof["pre_activation_downgrade_tested"])
        self.assertTrue(proof["disposable_project_removed"])
        self.assertFalse(proof["writers_activated"])
        self.assertEqual(set(proof["business_data_sha256"].values()), {"sha256:" + "a" * 64})
        self.assertEqual(proof["encrypted_backup_sha256"], "sha256:" + "d" * 64)

        scope = {key: proof[key] for key in drill.rehearsal_scope(value)}
        self.assertEqual(scope, drill.rehearsal_scope(value))
        manifest = {key: val for key, val in value.items() if key != "request_type"}
        manifest.update(
            version=contracts.MIGRATION_MANIFEST_VERSION,
            release_type=contracts.MIGRATION_RELEASE_TYPE,
            rehearsal_proof={
                **scope,
                "workflow_run_id": 202,
                "artifact_id": 303,
                "proof_sha256": "sha256:" + "e" * 64,
            },
        )
        contracts.validate_migration_manifest(manifest)

    def test_fail_forward_policy_does_not_claim_or_attempt_downgrade(self):
        value = request()
        value["pre_activation_rollback_policy"] = "FAIL_FORWARD_ONLY"
        value["migration_plan_hash"] = drill.migration_plan_hash(**{
            key: value[key] for key in (
                "source_schema_head", "target_schema_head", "migration_path",
                "compatibility_class", "pre_activation_rollback_policy",
            )
        })
        harness = RehearsalHarness(self.output)
        proof = harness.run(value)
        self.assertNotIn("downgrade", harness.runtime.events)
        self.assertEqual(harness.runtime.image, "target")
        self.assertFalse(proof["pre_activation_downgrade_tested"])
        self.assertIsNone(proof["business_data_sha256"]["downgraded"])
        self.assertEqual(harness.runtime.cleanup_calls, 1)

    def test_proof_scope_does_not_share_nested_image_dicts_with_request(self):
        value = request()
        original = copy.deepcopy(value)
        proof = RehearsalHarness(self.output).run(value)
        proof["source_images"]["backend"] = "changed proof"
        self.assertEqual(value, original)
        value["target_images"]["frontend"] = "changed request"
        self.assertEqual(proof["target_images"], original["target_images"])

    def test_restore_or_source_validation_failure_blocks_target_upgrade(self):
        for failure in (
            "restore", "restored_schema_mismatch", "restored_objects",
            "restored_count_mismatch", "restored_hash_mismatch", "restored_check",
        ):
            with self.subTest(failure=failure):
                harness = RehearsalHarness(self.output, failure)
                self.assert_failed_cleanly(harness, request())
                self.assertEqual(harness.runtime.target_upgrades, 0)
                self.assertNotIn("switch_target", harness.runtime.events)

    def test_backup_failure_cleans_up_without_restore_or_target_upgrade(self):
        for failure in ("backup", "check_backup"):
            with self.subTest(failure=failure):
                harness = RehearsalHarness(self.output, failure)
                self.assert_failed_cleanly(harness, request())
                self.assertNotIn("restore", harness.runtime.events)
                self.assertEqual(harness.runtime.target_upgrades, 0)

    def test_migration_readiness_invariants_and_downgrade_failures_never_save_pass(self):
        for failure in (
            "target_upgrade", "upgraded_schema_mismatch", "upgraded_check",
            "invalid_workspace", "upgraded_ready", "upgraded_topology",
            "upgraded_count_mismatch", "upgraded_hash_mismatch", "downgrade",
            "downgraded_schema_mismatch", "downgraded_check", "downgraded_objects",
            "downgraded_ready", "downgraded_topology", "downgraded_hash_mismatch",
        ):
            with self.subTest(failure=failure):
                self.assert_failed_cleanly(RehearsalHarness(self.output, failure), request())

    def test_cleanup_failure_prevents_proof_even_after_all_checks_pass(self):
        harness = RehearsalHarness(self.output, "cleanup")
        self.assert_failed_cleanly(harness, request())
        self.assertIn("downgraded_data", harness.runtime.events)
        self.assertEqual(harness.runtime.events[-1], "cleanup")

    def test_temporary_backup_and_key_cleanup_failure_prevents_proof(self):
        real_temporary_directory = tempfile.TemporaryDirectory

        class FailedTemporaryCleanup:
            def __init__(self, *args, **kwargs):
                self.directory = real_temporary_directory(*args, **kwargs)

            def __enter__(self):
                return self.directory.__enter__()

            def __exit__(self, *args):
                self.directory.__exit__(*args)
                raise OSError("injected temporary backup/key cleanup failure")

        harness = RehearsalHarness(self.output)
        with patch.object(drill.tempfile, "TemporaryDirectory", FailedTemporaryCleanup):
            with self.assertRaises(OSError):
                harness.run(request())
        self.assertEqual(harness.runtime.cleanup_calls, 1)
        self.assertEqual(harness.runtime.events[-1], "cleanup")
        harness.saved.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
