"""Migration backup boundaries: consistent data, bound evidence and cleanup."""

from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migration_apply_backup as backup
import migration_release_drill as drill
import release_ops as ops


def binding() -> dict:
    return {
        "operation_id": "a" * 32,
        "manifest_sha256": "sha256:" + "b" * 64,
        "database_identity_sha256": "sha256:" + "c" * 64,
        "source_release_sha": "1" * 40,
        "source_schema_head": backup.SOURCE_SCHEMA,
        "source_backend_image": f"ghcr.io/{ops.REPO}-backend@sha256:" + "d" * 64,
    }


def snapshot(*, exported: bool = False) -> dict:
    state = drill.canonical_business_state({"campaigns": [], "users": ['{"id": 1}']})
    value = {
        "build_sha": "1" * 40,
        "schema": backup.SOURCE_SCHEMA,
        "database_identity_sha256": "sha256:" + "c" * 64,
        "business_state": state,
        "table_counts": {"alembic_version": 1, "campaigns": 0, "users": 1},
    }
    if exported:
        value["snapshot"] = "00000003-0000001B-1"
    return value


def proof() -> dict:
    state = snapshot()["business_state"]
    return {
        **binding(), "proof_version": 1, "status": "PASS",
        "created_at": datetime.now(UTC).isoformat(),
        "encrypted_backup_sha256": "sha256:" + "e" * 64,
        "source_business_state": state,
        "restored_business_state": copy.deepcopy(state),
        "table_counts_match": True, "isolated_restore_network": "none",
        "restore_container_removed": True,
    }


class Process:
    def __init__(self, outcome=0):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.returncode = None
        self.outcome = outcome
        self.killed = False
        self.timeouts = []

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.timeouts.append(timeout)
        if self.returncode is not None:
            return self.returncode
        if isinstance(self.outcome, Exception):
            error, self.outcome = self.outcome, 0
            raise error
        self.returncode = self.outcome
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class Runtime:
    def __init__(self, root):
        self.root = root
        self.env_path = root / ".env"
        self.env_path.write_text("APP_ENV=maintenance\n")
        self.base = ["docker", "compose", "--project-name", "fixture"]
        self.database = {
            "Id": "2" * 64, "Image": "sha256:" + "3" * 64,
            "Config": {"Env": ["POSTGRES_USER=iteam", "POSTGRES_DB=iteam"]},
        }
        self.inspect = Mock(side_effect=lambda _: copy.deepcopy(self.database))


class Daemon:
    def __init__(self):
        self.calls = []
        self.containers = {}
        self.volumes = set()
        self.restored = snapshot()
        self.restored["database_identity_sha256"] = "sha256:" + "f" * 64
        self.fail_cleanup = False

    def command(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:3] == ["docker", "ps", "--all"]:
            value = argv[-1]
            if value.startswith("name=^/"):
                name = value.removeprefix("name=^/").removesuffix("$")
                return self.containers.get(name, {}).get("Id", "")
            if value.startswith("id="):
                return value[3:] if any(x["Id"] == value[3:] for x in self.containers.values()) else ""
        if argv[:2] == ["docker", "run"]:
            name = argv[argv.index("--name") + 1]
            if "backup-inspection" in " ".join(argv):
                return json.dumps(self.restored)
            volume = "5" * 64
            self.volumes.add(volume)
            self.containers[name] = {
                "Id": "4" * 64,
                "Config": {"Labels": {
                    "iteam.migration.operation": "a" * 32,
                    "iteam.migration.purpose": "backup-restore",
                }},
                "Mounts": [{"Type": "volume", "Name": volume}],
            }
            return "4" * 64
        if argv[:2] == ["docker", "exec"]:
            return "ready"
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([next(x for x in self.containers.values() if x["Id"] == argv[2])])
        if argv[:2] == ["docker", "rm"]:
            if self.fail_cleanup:
                raise ops.ReleaseError("EXTERNAL_COMMAND_FAILED")
            for name, value in list(self.containers.items()):
                if value["Id"] == argv[-1]:
                    self.volumes.difference_update(x["Name"] for x in value["Mounts"])
                    del self.containers[name]
            return ""
        if argv[:3] == ["docker", "volume", "ls"]:
            name = argv[-1].removeprefix("name=^").removesuffix("$")
            return name if name in self.volumes else ""
        raise AssertionError(argv)


class MigrationBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.runtime = Runtime(self.root)
        self.backups, self.keys, self.output = self.root / "backups", self.root / "keys", self.root / "proof.json"
        self.daemon = Daemon()
        self.exporter = Process()
        self.pipelines = []

    def pipe(self, source, target, **kwargs):
        self.pipelines.append((list(source), list(target)))
        output = kwargs.get("output")
        if output is not None:
            output.write(b"encrypted-backup-fixture")

    def run_backup(self, *, exported=None, pipeline=None):
        with (
            patch.object(backup.ops, "command", side_effect=self.daemon.command),
            patch.object(backup.subprocess, "Popen", return_value=self.exporter),
            patch.object(backup, "_read_export", return_value=exported or snapshot(exported=True)),
            patch.object(backup, "_pipeline", side_effect=pipeline or self.pipe),
        ):
            return backup.backup_restore(self.runtime, self.backups, self.keys, self.output, binding())

    def test_snapshot_and_binding_contracts_reject_ambiguous_or_incomplete_data(self):
        backup.validate_binding(binding())
        backup.validate_snapshot(snapshot())
        backup.validate_snapshot(snapshot(exported=True), exported=True)
        changes = [
            ("extra", "private payload"), ("build_sha", True), ("schema", "bad; DROP TABLE"),
            ("database_identity_sha256", "secret"), ("table_counts", {"alembic_version": True}),
            ("business_state", {"sha256": "sha256:" + "0" * 64, "table_counts": {"users": True}}),
        ]
        for key, replacement in changes:
            value = snapshot()
            value[key] = replacement
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                backup.validate_snapshot(value)
        for key, replacement in (
            ("operation_id", "../existing"), ("source_schema_head", "20261121_0030"),
            ("source_backend_image", "backend:latest"), ("manifest_sha256", True),
        ):
            value = binding()
            value[key] = replacement
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                backup.validate_binding(value)

    def test_database_identity_is_canonical_and_strict(self):
        identity = {"database_name": "iteam", "database_oid": 123, "system_identifier": "1234567890"}
        self.assertEqual(backup.database_identity_hash(identity), backup._hash(identity))
        for value in (True, 0, "123"):
            with self.subTest(value=value), self.assertRaises(ops.ReleaseError):
                backup.database_identity_hash({**identity, "database_oid": value})

    def test_historical_proof_is_inspectable_without_becoming_eligible_for_apply(self):
        now = datetime.now(UTC)
        value = proof()
        created = now - timedelta(days=2)
        value["created_at"] = created.isoformat()
        before = copy.deepcopy(value)
        self.assertEqual(backup.validate_proof_structure(value, binding()), created)
        self.assertEqual(value, before)
        with self.assertRaisesRegex(ops.ReleaseError, "INVALID_MIGRATION_BACKUP_PROOF"):
            backup.validate_proof(value, binding(), now=now)

    def test_structure_and_execution_share_the_same_proof_binding_checks(self):
        for key, replacement in (
            ("created_at", "2026-01-01T00:00:00"),
            ("operation_id", "b" * 32),
            ("table_counts_match", 1),
            ("restore_container_removed", False),
            ("key_path", "/private/DO_NOT_PRINT"),
        ):
            value = proof()
            value[key] = replacement
            for validator in (backup.validate_proof_structure, backup.validate_proof):
                with self.subTest(key=key, validator=validator.__name__):
                    with self.assertRaises(ops.ReleaseError):
                        validator(value, binding())

    def test_proof_freshness_boundaries_remain_required_for_apply(self):
        now = datetime.now(UTC)
        for age in (-1, 0, 3600, 3601):
            value = proof()
            value["created_at"] = (now - timedelta(seconds=age)).isoformat()
            with self.subTest(age=age):
                backup.validate_proof_structure(value, binding())
                if 0 <= age <= 3600:
                    backup.validate_proof(value, binding(), now=now)
                else:
                    with self.assertRaises(ops.ReleaseError):
                        backup.validate_proof(value, binding(), now=now)

    def test_proof_binds_operation_database_manifest_and_restored_business_data(self):
        backup.validate_proof(proof(), binding())
        for key in binding():
            value = proof()
            value[key] = "changed"
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                backup.validate_proof(value, binding())
        for key, replacement in (
            ("restore_container_removed", False), ("table_counts_match", 1),
            ("isolated_restore_network", "default"), ("created_at", "2026-01-01T00:00:00"),
            ("created_at", (datetime.now(UTC) - timedelta(hours=2)).isoformat()),
            ("created_at", (datetime.now(UTC) + timedelta(minutes=1)).isoformat()),
            ("key_path", "/private/key"),
        ):
            value = proof()
            value[key] = replacement
            with self.subTest(key=key, replacement=replacement), self.assertRaises(ops.ReleaseError):
                backup.validate_proof(value, binding())
        value = proof()
        value["restored_business_state"]["sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(ops.ReleaseError):
            backup.validate_proof(value, binding())

    def test_same_snapshot_dump_private_files_and_cleanup_before_pass(self):
        result = self.run_backup()
        backup.validate_proof(result, binding())
        self.assertEqual(result, json.loads(self.output.read_text()))
        self.assertIn("--snapshot=00000003-0000001B-1", self.pipelines[0][0])
        self.assertIn("--single-transaction", self.pipelines[1][1])
        self.assertIn("--exit-on-error", self.pipelines[1][1])
        self.assertEqual(len(self.pipelines), 2)
        self.assertFalse(self.daemon.containers)
        self.assertFalse(self.daemon.volumes)
        self.assertTrue(self.exporter.stdin.closed)
        for directory in (self.backups, self.keys):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(len(list(directory.iterdir())), 1)
            self.assertEqual(next(directory.iterdir()).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(self.root), json.dumps(result))
        restore = next(x for x in self.daemon.calls if x[:3] == ["docker", "run", "-d"])
        self.assertEqual(restore[restore.index("--network") + 1], "none")
        inspection = next(x for x in self.daemon.calls if "--entrypoint" in x)
        self.assertEqual(inspection[inspection.index("--network") + 1], "container:" + "4" * 64)
        self.assertIn("--read-only", inspection)
        self.assertNotIn("-v", inspection)

    def test_source_scope_mismatch_stops_before_key_dump_or_restore(self):
        for key in ("build_sha", "schema", "database_identity_sha256"):
            with self.subTest(key=key):
                value = snapshot(exported=True)
                value[key] = "wrong"
                with self.assertRaisesRegex(ops.ReleaseError, "SOURCE_MISMATCH"):
                    self.run_backup(exported=value)
                self.assertFalse(self.output.exists())
                self.assertFalse(list(self.keys.iterdir()))
                self.assertFalse(self.pipelines)
                self.exporter = Process()

    def test_changed_restored_rows_fail_without_success_proof_and_remove_clone(self):
        self.daemon.restored["business_state"]["sha256"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ops.ReleaseError, "RESTORE_MISMATCH"):
            self.run_backup()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.daemon.containers)
        self.assertFalse(self.daemon.volumes)
        self.assertTrue(list(self.backups.iterdir()))

    def test_changed_database_container_fails_before_proof(self):
        changed = copy.deepcopy(self.runtime.database)
        changed["Id"] = "9" * 64
        self.runtime.inspect.side_effect = [self.runtime.database, changed]
        with self.assertRaisesRegex(ops.ReleaseError, "DATABASE_CHANGED"):
            self.run_backup()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.daemon.containers)

    def test_changed_ciphertext_is_rejected_before_restore(self):
        with patch.object(backup, "file_hash", side_effect=["a" * 64, "b" * 64]):
            with self.assertRaisesRegex(ops.ReleaseError, "CIPHERTEXT_CHANGED"):
                self.run_backup()
        self.assertEqual(len(self.pipelines), 1)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.daemon.containers)

    def test_cleanup_failure_cannot_publish_a_pass(self):
        self.daemon.fail_cleanup = True
        with self.assertRaisesRegex(ops.ReleaseError, "CLEANUP_FAILED"):
            self.run_backup()
        self.assertFalse(self.output.exists())

    def test_pipeline_error_releases_exporter_and_does_not_publish(self):
        def fail(*args, **kwargs):
            raise ops.ReleaseError("MIGRATION_BACKUP_PIPELINE_FAILED")
        with self.assertRaisesRegex(ops.ReleaseError, "PIPELINE_FAILED"):
            self.run_backup(pipeline=fail)
        self.assertTrue(self.exporter.stdin.closed)
        self.assertIsNotNone(self.exporter.poll())
        self.assertFalse(self.output.exists())

    def test_existing_output_and_unsafe_directories_fail_before_processes(self):
        self.output.write_text("keep")
        with self.assertRaisesRegex(ops.ReleaseError, "OUTPUT_EXISTS"):
            self.run_backup()
        self.assertEqual(self.output.read_text(), "keep")
        self.output.unlink()
        self.keys.chmod(0o755)
        with self.assertRaisesRegex(ops.ReleaseError, "DIRECTORIES_UNSAFE"):
            self.run_backup()
        self.keys.chmod(0o700)
        with self.assertRaisesRegex(ops.ReleaseError, "DIRECTORIES_UNSAFE"):
            backup.backup_restore(self.runtime, self.backups, self.backups / "keys", self.output, binding())
        self.runtime.env_path.write_text('APP_ENV="production"\n')
        with self.assertRaisesRegex(ops.ReleaseError, "DIRECTORIES_UNSAFE"):
            self.run_backup()

    def test_symlink_directory_or_backup_cannot_redirect_writes(self):
        actual = self.root / "actual"
        actual.mkdir(mode=0o700)
        self.backups.symlink_to(actual, target_is_directory=True)
        with self.assertRaisesRegex(ops.ReleaseError, "DIRECTORIES_UNSAFE"):
            self.run_backup()
        self.backups.unlink()
        self.backups.mkdir(mode=0o700)
        target = actual / "keep"
        target.write_text("keep")
        (self.backups / ("migration-" + "a" * 32 + ".dump.enc")).symlink_to(target)
        with self.assertRaisesRegex(ops.ReleaseError, "OUTPUT_EXISTS"):
            self.run_backup()
        self.assertEqual(target.read_text(), "keep")

    def test_proof_publication_cannot_replace_a_concurrent_file(self):
        self.output.write_text("keep")
        with self.assertRaisesRegex(ops.ReleaseError, "OUTPUT_EXISTS"):
            backup._publish_proof(self.output, proof())
        self.assertEqual(self.output.read_text(), "keep")
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), [".env", "proof.json"])

    def test_pipeline_checks_both_exit_codes_and_reaps_timeout(self):
        for producer_result, consumer_result in ((1, 0), (0, 1), (0, subprocess.TimeoutExpired("private command", 600))):
            producer, consumer = Process(producer_result), Process(consumer_result)
            with self.subTest(producer=producer_result, consumer=type(consumer_result)), patch.object(
                backup.subprocess, "Popen", side_effect=[producer, consumer]
            ), self.assertRaisesRegex(ops.ReleaseError, "PIPELINE_FAILED"):
                backup._pipeline(["producer"], ["consumer"])
            self.assertIsNotNone(producer.poll())
            self.assertIsNotNone(consumer.poll())
            self.assertTrue(producer.stdout.closed)
            self.assertTrue(all(value is not None for value in producer.timeouts + consumer.timeouts))

    def test_pipeline_spawn_failure_reaps_already_started_producer(self):
        producer = Process()
        with patch.object(backup.subprocess, "Popen", side_effect=[producer, OSError("private payload")]):
            with self.assertRaisesRegex(ops.ReleaseError, "PIPELINE_FAILED"):
                backup._pipeline(["producer"], ["consumer"])
        self.assertTrue(producer.killed)

    def test_export_read_is_bounded_even_without_a_newline(self):
        for output in ("{", "x" * (1024 * 1024 + 1)):
            code = "import sys; sys.stdout.write(" + repr(output) + "); sys.stdout.flush(); sys.stdin.readline()"
            # Send long code through a temporary script, avoiding argv size limits.
            script = self.root / "exporter.py"
            script.write_text(code)
            child = subprocess.Popen([sys.executable, "-u", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            try:
                with self.subTest(size=len(output)), self.assertRaisesRegex(ops.ReleaseError, "EXPORT_FAILED"):
                    backup._read_export(child, timeout=0.1)
            finally:
                child.stdin.close()
                backup._reap(child)
                child.stdout.close()

    def test_export_read_accepts_only_complete_valid_snapshot(self):
        code = "import sys; print(" + repr(json.dumps(snapshot(exported=True))) + ",flush=True); sys.stdin.readline()"
        child = subprocess.Popen([sys.executable, "-u", "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(backup._read_export(child, timeout=2), snapshot(exported=True))
        finally:
            child.stdin.close()
            backup._reap(child)
            child.stdout.close()

    def test_snapshot_payload_uses_one_read_only_repeatable_read_transaction(self):
        rows = {"campaigns": [], "users": ['{"precise": 123456789.00000001}', '{"id": 2}', '{"id": 2}']}
        trace = []

        class Result:
            def __init__(self, value): self.value = value
            def scalars(self): return self
            def all(self): return self.value

        class Context:
            async def __aenter__(self): trace.append("begin"); return self
            async def __aexit__(self, *args): trace.append("end")

        class Connection(Context):
            async def execution_options(self, **kwargs): trace.append(kwargs); return self
            def begin(self): return Context()
            async def execute(self, sql):
                trace.append(sql)
                if sql.startswith("SELECT tablename"):
                    return Result(["alembic_version", *rows])
                if sql.startswith("SELECT to_jsonb"):
                    return Result(rows[sql.split('"')[1]])
                if sql == "SELECT version_num FROM public.alembic_version":
                    return Result([backup.SOURCE_SCHEMA])
                return Result([])
            async def scalar(self, sql):
                trace.append(sql)
                if sql.startswith("SELECT count(*)"):
                    name = sql.split('"')[1]
                    return 1 if name == "alembic_version" else len(rows[name])
                if "system_identifier" in sql: return "1234567890"
                if "oid::bigint" in sql: return 123
                if "current_database()" in sql: return "iteam"
                raise AssertionError(sql)

        class Engine:
            def connect(self): return Connection()
            async def dispose(self): trace.append("disposed")

        modules = {
            "sqlalchemy": types.SimpleNamespace(text=lambda value: value),
            "app": types.ModuleType("app"), "app.core": types.ModuleType("app.core"),
            "app.core.database": types.SimpleNamespace(engine=Engine()),
            "app.core.config": types.SimpleNamespace(settings=types.SimpleNamespace(build_sha="1" * 40)),
        }
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, "argv", ["-c", json.dumps(sorted(rows))]), redirect_stdout(output):
            exec(backup.SNAPSHOT_CODE, {})
        value = json.loads(output.getvalue())
        backup.validate_snapshot(value)
        self.assertEqual(value["business_state"], drill.canonical_business_state(rows))
        self.assertEqual(value["database_identity_sha256"], backup.database_identity_hash({
            "database_name": "iteam", "database_oid": 123, "system_identifier": "1234567890",
        }))
        self.assertIn({"isolation_level": "REPEATABLE READ"}, trace)
        self.assertLess(trace.index("SET TRANSACTION READ ONLY"), next(i for i, v in enumerate(trace) if isinstance(v, str) and v.startswith("SELECT")))
        self.assertEqual(trace[-1], "disposed")


if __name__ == "__main__":
    unittest.main()
