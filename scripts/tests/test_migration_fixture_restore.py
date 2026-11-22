"""Encrypted fixture replacement regressions; no Docker or external calls."""

from __future__ import annotations

import copy
import hashlib
import importlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
restore_module = importlib.import_module("migration_fixture_restore")


class FakeProcess:
    def __init__(self, outcome=0):
        self.stdout = io.BytesIO()
        self.returncode = None
        self.outcome = outcome
        self.wait_timeouts = []
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
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


class FixtureRuntime:
    def __init__(self, root: Path):
        self.root = root
        self.project = "iteam-migration-drill-0123456789"
        self.base = [
            "docker", "compose", "--project-directory", str(root),
            "--project-name", self.project, "--env-file", str(root / ".env"),
            "-f", str(root / "compose.json"), "-f", str(root / "source-images.json"),
        ]
        self.containers = {
            service: {
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": self.project,
                        "com.docker.compose.service": service,
                    },
                    "Env": [
                        "POSTGRES_USER=iteam",
                        "POSTGRES_DB=iteam_migration_drill",
                    ],
                },
                "State": {"Running": service == "postgres"},
            }
            for service in ("postgres", "backend", "frontend")
        }
        self.inspect = Mock(side_effect=self.containers.__getitem__)
        self.compose = Mock(return_value="")


class MigrationFixtureRestoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="iteam-migration-drill-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.runtime = FixtureRuntime(self.root)
        (self.root / "backups").mkdir(mode=0o700)
        (self.root / "keys").mkdir(mode=0o700)
        self.backup = self.root / "backups" / "fixture.dump.enc"
        self.key = self.root / "keys" / "fixture.key"
        self.backup.write_bytes(b"test ciphertext")
        self.key.write_text("test-only-key\n")
        self.backup.chmod(0o600)
        self.key.chmod(0o600)
        self.proof = {
            "status": "PASS",
            "created_at": datetime.now(UTC).isoformat(),
            "backup": str(self.backup),
            "key_path": str(self.key),
            "checksum": hashlib.sha256(self.backup.read_bytes()).hexdigest(),
            "source_release": "1" * 40,
            "alembic_head": "20261009_0028",
            "isolated_network": "none",
            "table_counts_match": True,
            "tables": 3,
        }
        self.save_proof()

    def save_proof(self):
        path = self.root / "backup-proof.json"
        path.write_text(json.dumps(self.proof))
        path.chmod(0o600)

    def test_original_database_is_replaced_before_streaming_restore(self):
        decrypted, restored = FakeProcess(), FakeProcess()

        def start(*args, **kwargs):
            # Restoration must not accidentally overlay the original fixture.
            self.assertEqual(self.runtime.compose.call_count, 2)
            return decrypted if args[0][0] == "openssl" else restored

        with patch.object(restore_module.subprocess, "Popen", side_effect=start) as popen:
            restore_module.restore_fixture(self.runtime, self.proof)

        calls = self.runtime.compose.call_args_list
        self.assertEqual(
            calls[0].args,
            ("exec", "-T", "postgres", "dropdb", "-U", "iteam", restore_module.DATABASE_NAME),
        )
        self.assertEqual(
            calls[1].args,
            ("exec", "-T", "postgres", "createdb", "-U", "iteam", "-O", "iteam", restore_module.DATABASE_NAME),
        )
        self.assertNotIn("--force", calls[0].args)
        decrypt_call, restore_call = popen.call_args_list
        self.assertEqual(decrypt_call.args[0][-4:], ["-pass", "file:" + str(self.key), "-in", str(self.backup)])
        self.assertIs(restore_call.kwargs["stdin"], decrypted.stdout)
        self.assertEqual(restore_call.kwargs["cwd"], self.root)
        self.assertEqual(restore_call.args[0][:len(self.runtime.base)], self.runtime.base)
        self.assertIn("--single-transaction", restore_call.args[0])
        self.assertIn("--exit-on-error", restore_call.args[0])
        for call in popen.call_args_list:
            self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)
            self.assertNotIn("shell", call.kwargs)
        self.assertEqual(restore_call.kwargs["stdout"], subprocess.DEVNULL)
        self.assertTrue(decrypted.stdout.closed)
        self.assertEqual(restored.wait_timeouts, [300, 10])
        self.assertEqual(decrypted.wait_timeouts, [30, 10])
        self.assertFalse(decrypted.killed or restored.killed)
        self.assertEqual(set(self.root.iterdir()), {self.root / "backups", self.root / "keys", self.root / "backup-proof.json"})

    def test_both_pipeline_exit_codes_are_required(self):
        for decrypt_code, restore_code in [(1, 0), (0, 1), (1, 1)]:
            with self.subTest(decrypt=decrypt_code, restore=restore_code):
                processes = [FakeProcess(decrypt_code), FakeProcess(restore_code)]
                with patch.object(restore_module.subprocess, "Popen", side_effect=processes):
                    with self.assertRaisesRegex(restore_module.ReleaseError, "^MIGRATION_FIXTURE_RESTORE_FAILED$"):
                        restore_module.restore_fixture(self.runtime, self.proof)
                self.assertTrue(all(process.returncode is not None for process in processes))
                self.assertTrue(processes[0].stdout.closed)

    def test_restore_timeout_kills_and_reaps_both_processes(self):
        decrypted = FakeProcess()
        restored = FakeProcess(subprocess.TimeoutExpired(["pg_restore", "secret"], 300))
        with patch.object(restore_module.subprocess, "Popen", side_effect=[decrypted, restored]):
            with self.assertRaisesRegex(restore_module.ReleaseError, "^MIGRATION_FIXTURE_RESTORE_FAILED$") as caught:
                restore_module.restore_fixture(self.runtime, self.proof)
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(decrypted.killed and restored.killed)
        self.assertEqual(restored.wait_timeouts, [300, 10])
        self.assertEqual(decrypted.wait_timeouts, [10])
        self.assertTrue(decrypted.stdout.closed)

    def test_decrypt_timeout_kills_and_reaps_decrypt_process(self):
        decrypted = FakeProcess(subprocess.TimeoutExpired(["openssl", "secret"], 30))
        restored = FakeProcess()
        with patch.object(restore_module.subprocess, "Popen", side_effect=[decrypted, restored]):
            with self.assertRaisesRegex(restore_module.ReleaseError, "^MIGRATION_FIXTURE_RESTORE_FAILED$"):
                restore_module.restore_fixture(self.runtime, self.proof)
        self.assertTrue(decrypted.killed)
        self.assertFalse(restored.killed)
        self.assertEqual(decrypted.wait_timeouts, [30, 10])

    def test_restore_spawn_failure_reaps_started_decrypt_process(self):
        decrypted = FakeProcess()
        with patch.object(restore_module.subprocess, "Popen", side_effect=[decrypted, OSError("secret")]):
            with self.assertRaisesRegex(restore_module.ReleaseError, "^MIGRATION_FIXTURE_RESTORE_FAILED$"):
                restore_module.restore_fixture(self.runtime, self.proof)
        self.assertTrue(decrypted.killed)
        self.assertTrue(decrypted.stdout.closed)
        self.assertEqual(decrypted.wait_timeouts, [10])

    def test_drop_failure_never_recreates_or_restores(self):
        self.runtime.compose.side_effect = restore_module.ReleaseError("EXTERNAL_COMMAND_FAILED")
        with patch.object(restore_module.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(restore_module.ReleaseError, "EXTERNAL_COMMAND_FAILED"):
                restore_module.restore_fixture(self.runtime, self.proof)
        self.runtime.compose.assert_called_once()
        popen.assert_not_called()

    def assert_rejected_before_deletion(self, code):
        with patch.object(restore_module.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(restore_module.ReleaseError, code):
                restore_module.restore_fixture(self.runtime, self.proof)
        self.runtime.compose.assert_not_called()
        popen.assert_not_called()

    def test_non_generated_project_is_rejected(self):
        self.runtime.project = "iteam-production"
        self.assert_rejected_before_deletion("UNSAFE_MIGRATION_FIXTURE_PROJECT")

    def test_ambient_or_foreign_compose_paths_are_rejected(self):
        original = self.runtime.base[:]
        for base in [
            ["docker", "compose", "--project-name", self.runtime.project],
            original[:-1] + [str(self.root / "target-images.json")],
            original[:-1] + ["/production/compose.yml"],
            original + ["--profile", "publishing"],
        ]:
            with self.subTest(base=base):
                self.runtime.base = base
                self.assert_rejected_before_deletion("UNSAFE_MIGRATION_FIXTURE_COMPOSE")

    def test_running_app_prevents_database_deletion(self):
        for service in ("backend", "frontend"):
            with self.subTest(service=service):
                self.runtime.containers[service]["State"]["Running"] = True
                self.assert_rejected_before_deletion("MIGRATION_FIXTURE_WRITERS_NOT_STOPPED")
                self.runtime.containers[service]["State"]["Running"] = False

    def test_foreign_container_and_database_are_rejected(self):
        original = copy.deepcopy(self.runtime.containers["postgres"])
        self.runtime.containers["postgres"]["Config"]["Labels"]["com.docker.compose.project"] = "production"
        self.assert_rejected_before_deletion("MIGRATION_FIXTURE_CONTAINER_OUTSIDE_PROJECT")
        self.runtime.containers["postgres"] = original
        self.runtime.containers["postgres"]["Config"]["Env"] = ["POSTGRES_USER=iteam", "POSTGRES_DB=production"]
        self.assert_rejected_before_deletion("UNSAFE_MIGRATION_FIXTURE_DATABASE")

    def test_ciphertext_is_rechecked_before_database_deletion(self):
        self.backup.write_bytes(b"changed ciphertext")
        self.assert_rejected_before_deletion("BACKUP_FILE_MISSING_OR_CHANGED")

    def test_backup_must_be_inside_generated_backup_directory(self):
        moved = self.root / "outside.dump.enc"
        self.backup.rename(moved)
        self.proof["backup"] = str(moved)
        self.save_proof()
        self.assert_rejected_before_deletion("MIGRATION_FIXTURE_BACKUP_OUTSIDE_PROJECT")

    def test_backup_directory_symlink_is_rejected(self):
        (self.root / "backups").rename(self.root / "moved-backups")
        (self.root / "backups").symlink_to(self.root / "moved-backups", target_is_directory=True)
        self.assert_rejected_before_deletion("UNSAFE_MIGRATION_FIXTURE_DIRECTORY")

    def test_changed_proof_is_rejected(self):
        self.proof["tables"] = 99
        self.assert_rejected_before_deletion("MIGRATION_FIXTURE_PROOF_CHANGED")


if __name__ == "__main__":
    unittest.main()
