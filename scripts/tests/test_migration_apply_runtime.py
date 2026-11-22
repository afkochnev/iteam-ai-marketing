"""Failure-path checks at the runtime/physical-fence boundary; no Docker I/O."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_apply_backup as backup
import migration_apply_runtime as runtime
import migration_writer_barrier as writers
import release_ops as ops
from test_release_manifest import manifest


OPERATION = "a" * 32
POSTGRES = "b" * 64
IDENTITY = {"system_identifier": "123456789", "database_oid": 16384, "database_name": "iteam"}


class ObservedFence:
    """Use the actual sticky event audit while stubbing physical Docker probes."""

    def __init__(self):
        self.postgres_id = POSTGRES
        self.allowed_helpers = set()
        self.audit = writers.EventAudit(OPERATION, {POSTGRES}, POSTGRES, self.allowed_helpers)
        self.audit.process = types.SimpleNamespace(poll=lambda: None)
        self.audit.sealed = True
        self.contain_attempts = 0

    def barrier(self):
        self.audit.check()
        return {"writers_stopped": True, "database_fenced": True,
                "barrier_continuous": True, "barrier_sha256": "sha256:" + "c" * 64}

    def contain(self):
        self.contain_attempts += 1
        return self.barrier()

    def close(self):
        pass

    def reconnect(self):
        for action in ("connect", "disconnect"):
            self.audit.observe({"Type": "network", "Action": action,
                                "Actor": {"ID": "d" * 64, "Attributes": {"container": POSTGRES}}})


class HelperDocker:
    """Model daemon-side create succeeding independently of its CLI response."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.containers = {}
        self.calls = []
        self.inputs = []
        self.next_id = 1
        self.lose_create_reply = False
        self.fail_start = False

    def command(self, args, **kwargs):
        self.calls.append(list(args))
        if args[:2] == ["docker", "create"]:
            identifier = f"{self.next_id:064x}"
            self.next_id += 1
            name = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1].split("=", 1)
            self.containers[identifier] = {
                "Id": identifier, "Name": "/" + name,
                "Config": {"Labels": {label[0]: label[1]}},
                "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
            }
            if self.lose_create_reply:
                raise ops.ReleaseError("EXTERNAL_COMMAND_FAILED")
            return identifier
        if args[:2] == ["docker", "ps"]:
            selected = list(self.containers.values())
            for index, argument in enumerate(args[:-1]):
                if argument != "--filter":
                    continue
                key, value = args[index + 1].split("=", 1)
                if key == "id":
                    selected = [item for item in selected if item["Id"].startswith(value)]
                elif key == "name":
                    selected = [item for item in selected if item["Name"] == value.removeprefix("^").removesuffix("$")]
                elif key == "label":
                    label, expected = value.split("=", 1)
                    selected = [item for item in selected if item["Config"]["Labels"].get(label) == expected]
            return "\n".join(item["Id"] for item in selected)
        if args[:2] == ["docker", "inspect"]:
            reference = args[-1]
            selected = [item for item in self.containers.values()
                        if item["Id"] == reference or item["Name"].lstrip("/") == reference.lstrip("/")]
            if not selected:
                raise ops.ReleaseError("EXTERNAL_COMMAND_FAILED")
            return json.dumps(selected)
        if args[:2] == ["docker", "start"]:
            identifier = args[-1]
            self.adapter.writer_barrier.audit.observe({
                "Type": "container", "Action": "start", "Actor": {"ID": identifier},
            })
            self.adapter.writer_barrier.audit.check()
            self.inputs.append(kwargs.get("input"))
            if self.fail_start:
                raise ops.ReleaseError("EXTERNAL_COMMAND_FAILED")
            return "helper-result"
        if args[:2] == ["docker", "rm"]:
            self.containers.pop(args[-1])
            return args[-1]
        raise AssertionError(args)


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.value = runtime.MigrationRuntime(
            types.SimpleNamespace(root=root, env_path=root / ".env"),
            manifest(), OPERATION, root / "state", root / "backup", root / "key",
        )
        self.value.writer_barrier = ObservedFence()
        self.value._fenced = True
        self.value._database_environment = {
            "POSTGRES_USER": "iteam", "POSTGRES_DB": "iteam",
            "POSTGRES_PASSWORD": "private-password:/?@",
        }
        self.value._database_identity = backup.database_identity_hash(IDENTITY)

    def test_one_shot_is_registered_before_start_and_credentials_only_cross_stdin(self):
        docker = HelperDocker(self.value)
        with patch.object(ops, "command", side_effect=docker.command):
            self.assertEqual(self.value._run("target", ["alembic", "heads"]), "helper-result")
        self.assertFalse(docker.containers)
        self.assertFalse(self.value._helpers)
        self.assertEqual(len(self.value.writer_barrier.allowed_helpers), 1)
        self.value.writer_barrier.audit.check()
        self.assertNotIn("private-password", json.dumps(docker.calls))
        self.assertIn("private-password", docker.inputs[0])
        received = json.loads(docker.inputs[0])["environment"]
        self.assertEqual(received["OPENAI_API_KEY"], "")
        self.assertEqual(received["VK_PUBLISHING_ENABLED"], "false")
        self.assertEqual(received["METRICS_SYNC_ENABLED"], "false")
        self.assertIn("@127.0.0.1:5432/iteam", received["DATABASE_URL"])

    def test_one_shot_failure_still_removes_the_started_helper(self):
        docker = HelperDocker(self.value)
        docker.fail_start = True
        with patch.object(ops, "command", side_effect=docker.command), self.assertRaises(ops.ReleaseError):
            self.value._run("target", ["alembic", "upgrade", "head"])
        self.assertFalse(docker.containers)
        self.assertFalse(self.value._helpers)
        self.assertEqual(len(self.value.writer_barrier.allowed_helpers), 1)

    def test_helper_cannot_run_before_the_physical_fence(self):
        self.value._fenced = False
        with patch.object(ops, "command") as command, self.assertRaises(ops.ReleaseError):
            self.value._run("target", ["alembic", "heads"])
        command.assert_not_called()

    def test_lost_create_reply_is_cleaned_up_without_starting_the_container(self):
        docker = HelperDocker(self.value)
        docker.lose_create_reply = True
        with patch.object(ops, "command", side_effect=docker.command):
            with self.assertRaises(ops.ReleaseError):
                self.value._create(self.value.manifest["target_images"]["backend"], [], bootstrap=True)
            self.value.close()
        self.assertFalse(docker.containers)
        self.assertFalse(any(args[:2] == ["docker", "start"] for args in docker.calls))

    def test_cleanup_failure_does_not_skip_other_helpers_or_physical_containment(self):
        identifiers = ["e" * 64, "f" * 64]
        self.value._helpers = dict.fromkeys(identifiers)
        removed = []

        def remove(identifier):
            removed.append(identifier)
            if identifier == identifiers[0]:
                raise ops.ReleaseError("MIGRATION_HELPER_CLEANUP_FAILED")

        with patch.object(self.value, "_remove", side_effect=remove), patch.object(self.value, "barrier", return_value={}), self.assertRaises(ops.ReleaseError):
            self.value.contain()
        self.assertEqual(removed, identifiers)
        self.assertEqual(self.value.writer_barrier.contain_attempts, 1)

    def test_transient_reconnect_during_zero_client_probe_invalidates_returned_evidence(self):
        def clients(sql):
            self.assertEqual(sql, runtime.CLIENTS_SQL)
            self.value.writer_barrier.reconnect()
            return "0"

        with patch.object(self.value, "_unchanged_configuration"), patch.object(self.value, "_json_query", return_value=copy.deepcopy(IDENTITY)), patch.object(self.value, "_sql", side_effect=clients), self.assertRaisesRegex(ops.ReleaseError, "CONTINUITY_LOST"):
            self.value.barrier()

    def test_containment_cannot_reset_a_previously_lost_observation_interval(self):
        original = self.value.writer_barrier.audit
        self.value.writer_barrier.reconnect()
        with self.assertRaises(ops.ReleaseError):
            self.value.contain()
        self.assertIs(self.value.writer_barrier.audit, original)
        with self.assertRaisesRegex(ops.ReleaseError, "CONTINUITY_LOST"):
            original.check()

    def test_an_existing_database_client_blocks_barrier_after_bounded_retries(self):
        with patch.object(self.value, "_unchanged_configuration"), patch.object(self.value, "_json_query", return_value=copy.deepcopy(IDENTITY)), patch.object(self.value, "_sql", return_value="1") as query, patch.object(runtime.time, "sleep"), self.assertRaisesRegex(ops.ReleaseError, "DATABASE_CLIENTS_PRESENT"):
            self.value.barrier()
        self.assertGreater(query.call_count, 1)
        self.assertLessEqual(query.call_count, 20)

    def test_changed_database_identity_is_rejected_before_claiming_exclusive_clients(self):
        changed = {**IDENTITY, "database_oid": IDENTITY["database_oid"] + 1}
        with patch.object(self.value, "_unchanged_configuration"), patch.object(self.value, "_json_query", return_value=changed), patch.object(self.value, "_sql") as query, self.assertRaisesRegex(ops.ReleaseError, "DATABASE_IDENTITY_CHANGED"):
            self.value.barrier()
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
