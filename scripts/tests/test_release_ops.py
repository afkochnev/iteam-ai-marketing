"""Failure-boundary tests: no Docker, GitHub, provider or production access."""

from __future__ import annotations

import importlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ops = importlib.import_module("release_ops")
backup = importlib.import_module("release_backup")
drill = importlib.import_module("release_drill")
OLD = "1" * 40
NEW = "2" * 40


def manifest(sha=NEW):
    return {
        "version": 1,
        "repository": ops.REPO,
        "release_sha": sha,
        "build_run_id": 12,
        "build_attempt": 1,
        "alembic_head": ops.SCHEMA,
        "frontend_api_url": ops.API_URL,
        "images": {
            name: "ghcr.io/" + ops.REPO + "-" + name + "@sha256:" + "a" * 64
            for name in ["backend", "frontend"]
        },
    }


class FakeRuntime:
    def __init__(self, root):
        self.root = root
        self.env_path = root / ".env"
        self.env_path.write_text(
            "BUILD_SHA="
            + OLD
            + "\nCOMPOSE_FILE=compose.json:concurrency.json\nSECRET=do-not-log\nAPP_ENV=production\nVK_PUBLISHING_ENABLED=true\n"
        )
        self.sha = OLD
        self.calls = []
        self.fail_once = False
        self.drift = False
        self.rows_changed = False

    def snapshot(self):
        return {
            "build_sha": self.sha,
            "schema": ops.SCHEMA,
            "unsafe": 0,
            "agent_runs": 0,
            "vk": True,
            "telegram": True,
            "canary": "",
            "publication_hash": "changed" if self.rows_changed else "persisted",
            "audit_hash": "audit",
        }

    def topology(self):
        return {
            name: {
                "image": self.sha + "0" * 24
                if name in ops.APP
                else "stable-data-service",
                "reference": "old-image",
                "runtime_policy_hash": "stable-runtime-policy",
                "revision": self.sha if name in ops.APP else None,
                "env_hash": "changed"
                if self.drift and self.sha == NEW
                else "same-private-env-hash",
                "command": [name],
                "mounts": [],
            }
            for name in ops.ALL
        }

    def credentials(self):
        return {"vk_checked": True, "file_uid": 10001, "file_mode": "0600"}

    def compose(self, *args):
        self.calls.append(args)
        if args[0] == "config":
            return json.dumps(
                {
                    "name": "iteam-ai-marketing",
                    "services": {name: {} for name in ops.APP},
                }
            )
        return ""

    def restart(self, *, migration_check=True):
        self.calls.append(("restart", migration_check))
        self.sha = ops.env_value(self.env_path.read_text(), "BUILD_SHA")
        if self.fail_once and self.sha == NEW:
            self.fail_once = False
            raise ops.ReleaseError("INJECTED_FAILURE")


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_only_expected_digest_repositories_and_exact_schema(self):
        ops.validate_manifest(manifest())
        for key, value in [
            ("repository", "evil/repo"),
            ("alembic_head", "other"),
            ("frontend_api_url", "https://other.example"),
        ]:
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                item = manifest()
                item[key] = value
                ops.validate_manifest(item)
        for bad in [
            "ghcr.io/" + ops.REPO + "-backend:latest",
            "evil@sha256:" + "a" * 64,
            "ghcr.io/" + ops.REPO + "-backend@sha256:" + "a" * 64 + ";echo bad",
        ]:
            with self.subTest(ref=bad), self.assertRaises(ops.ReleaseError):
                item = manifest()
                item["images"]["backend"] = bad
                ops.validate_manifest(item)
        item = manifest()
        item["payload"] = "unexpected"
        with self.assertRaises(ops.ReleaseError):
            ops.validate_manifest(item)

    def test_duplicate_json_and_symlink_outputs_are_rejected(self):
        path = self.root / "malformed.json"
        path.write_text('{"version":1,"version":2}')
        with self.assertRaises(ops.ReleaseError):
            ops.load(path)
        target = self.root / "target"
        target.write_text("unchanged")
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(ops.ReleaseError):
            ops.atomic(path, "replace")
        self.assertEqual(target.read_text(), "unchanged")

    def test_overlay_never_builds_or_changes_provider_environment(self):
        result = ops.overlay(manifest())
        self.assertEqual(set(result["services"]), {*ops.APP, "vk_credentials_admin"})
        for service in result["services"].values():
            self.assertEqual(set(service), {"image"})
            self.assertIn("@sha256:", service["image"])

    def test_subprocess_error_does_not_expose_secret_output(self):
        result = subprocess.CompletedProcess(
            ["docker"], 1, stdout="access_token=secret", stderr="password=private"
        )
        with patch.object(ops.subprocess, "run", return_value=result):
            with self.assertRaises(ops.ReleaseError) as raised:
                ops.command(["docker", "inspect"])
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_private_state_and_exclusive_operation_lock(self):
        state = self.root / "state"
        with ops.locked_state(state):
            with self.assertRaises(ops.ReleaseError):
                with ops.locked_state(state):
                    pass
        state.chmod(0o755)
        with self.assertRaises(ops.ReleaseError):
            with ops.locked_state(state):
                pass

    def test_environment_edits_preserve_secret_and_gate_bytes(self):
        text = (
            'SECRET="$do not expand"\nVK_PUBLISHING_ENABLED=true\nBUILD_SHA='
            + OLD
            + "\n"
        )
        updated = ops.set_env(
            text, {"BUILD_SHA": NEW, "COMPOSE_FILE": "base.json:images.json"}
        )
        self.assertIn('SECRET="$do not expand"\nVK_PUBLISHING_ENABLED=true\n', updated)
        self.assertEqual(ops.env_value(updated, "BUILD_SHA"), NEW)
        with self.assertRaises(ops.ReleaseError):
            ops.set_env("BUILD_SHA=old\nBUILD_SHA=duplicate\n", {"BUILD_SHA": NEW})

    @patch.object(ops, "pull_and_validate")
    @patch.object(ops, "command", return_value="")
    def test_success_preserves_business_snapshot_and_runtime_policy(self, cmd, pull):
        runtime = FakeRuntime(self.root)
        result = ops.switch(
            runtime, manifest(), self.root, "success", git_checkout=False
        )
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(runtime.snapshot()["publication_hash"], "persisted")
        self.assertIn("SECRET=do-not-log", runtime.env_path.read_text())
        self.assertEqual(
            ops.load(self.root / "success-before.json")["snapshot"]["build_sha"], OLD
        )
        self.assertNotIn("do-not-log", (self.root / "success-before.json").read_text())
        self.assertTrue(all("downgrade" not in call for call in runtime.calls))
        pull.assert_called_once()

    @patch.object(ops, "pull_and_validate")
    @patch.object(ops, "command", return_value="")
    def test_failed_start_restores_exact_previous_images_and_sha(self, cmd, pull):
        runtime = FakeRuntime(self.root)
        runtime.fail_once = True
        with self.assertRaisesRegex(ops.ReleaseError, "ROLLBACK_COMPLETED"):
            ops.switch(runtime, manifest(), self.root, "failure", git_checkout=False)
        self.assertEqual(runtime.sha, OLD)
        self.assertEqual(ops.env_value(runtime.env_path.read_text(), "BUILD_SHA"), OLD)
        rollback = ops.load(self.root / "failure-rollback-images.json")
        self.assertTrue(
            all(
                service["image"] == OLD + "0" * 24
                for service in rollback["services"].values()
            )
        )
        self.assertEqual(
            ops.load(self.root / "failure-result.json")["status"], "ROLLED_BACK"
        )
        self.assertIn(("restart", False), runtime.calls)

    @patch.object(ops, "pull_and_validate")
    @patch.object(ops, "command", return_value="")
    def test_runtime_drift_triggers_rollback_without_rewriting_business_data(
        self, cmd, pull
    ):
        runtime = FakeRuntime(self.root)
        runtime.drift = True
        with self.assertRaisesRegex(ops.ReleaseError, "ROLLBACK_COMPLETED"):
            ops.switch(runtime, manifest(), self.root, "drift", git_checkout=False)
        self.assertEqual(runtime.sha, OLD)
        self.assertFalse(
            any(call[0] in {"restore", "psql", "publish"} for call in runtime.calls)
        )

    def test_backup_receipt_expires_and_binds_source_release_and_file(self):
        encrypted, key = self.root / "dump.enc", self.root / "key"
        encrypted.write_bytes(b"ciphertext")
        encrypted.chmod(0o600)
        key.write_text("fixture")
        key.chmod(0o600)
        proof = {
            "created_at": datetime.now(UTC).isoformat(),
            "status": "PASS",
            "source_release": OLD,
            "alembic_head": ops.SCHEMA,
            "isolated_network": "none",
            "table_counts_match": True,
            "backup": str(encrypted),
            "key_path": str(key),
            "checksum": backup.file_hash(encrypted),
        }
        path = self.root / "proof.json"
        ops.save(path, proof)
        backup.check_proof(path, OLD)
        for field, value in [
            ("source_release", NEW),
            ("status", "FAIL"),
            ("created_at", (datetime.now(UTC) - timedelta(hours=2)).isoformat()),
        ]:
            modified = dict(proof, **{field: value})
            ops.save(path, modified)
            with self.assertRaises(ops.ReleaseError):
                backup.check_proof(path, OLD)
        ops.save(path, proof)
        encrypted.write_bytes(b"tampered")
        with self.assertRaises(ops.ReleaseError):
            backup.check_proof(path, OLD)

    def test_approval_is_consumed_even_when_deployment_fails(self):
        state = self.root / "state"
        runtime = FakeRuntime(self.root)
        with (
            patch.object(ops, "approved", return_value=(manifest(), OLD)),
            patch.object(ops, "accepted"),
            patch.object(ops, "Runtime", return_value=runtime),
            patch.object(backup, "check_proof"),
            patch.object(
                ops, "switch", side_effect=ops.ReleaseError("FAILED")
            ) as switch,
        ):
            with self.assertRaises(ops.ReleaseError):
                ops.apply(self.root, state, 17, self.root / "proof", "cosign")
            with self.assertRaisesRegex(ops.ReleaseError, "APPROVAL_ALREADY_USED"):
                ops.apply(self.root, state, 17, self.root / "proof", "cosign")
            switch.assert_called_once()

    def test_drill_has_no_production_ports_bind_mounts_or_provider_credentials(self):
        runtime = drill.create_drill(
            self.root, "iteam-release-drill-test", manifest(OLD)
        )
        config = ops.load(self.root / "compose.json")
        self.assertTrue(config["networks"]["default"]["internal"])
        for service in config["services"].values():
            self.assertNotIn("ports", service)
            for volume in service.get("volumes", []):
                self.assertFalse(volume.startswith("/"))
        env = runtime.env_path.read_text()
        self.assertIn("APP_ENV=development\n", env)
        self.assertIn("/iteam_release_drill\n", env)
        self.assertIn("REDIS_URL=redis://redis:6379/15\n", env)
        self.assertNotIn("/iteam_release_test", env)
        self.assertIn("VK_PUBLISHING_ENABLED=false", env)
        self.assertIn("TELEGRAM_PUBLISHING_ENABLED=false", env)
        self.assertIn("VK_ACCESS_TOKEN=\n", env)
        self.assertNotIn(
            "iteam-ai-marketing_vk_publishing_credentials", json.dumps(config)
        )

    def test_signed_approval_receipt_cannot_be_replayed_for_another_run(self):
        info = {
            "run_attempt": 1,
            "head_sha": OLD,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        history = [
            {
                "state": "approved",
                "user": {"login": ops.REVIEWER, "type": "User"},
                "environments": [{"name": "production"}],
            }
        ]
        ops.save(
            self.root / "release.json",
            {
                "approval_run_id": 98,
                "approval_attempt": 1,
                "approval_workflow_sha": OLD,
                "expected_source_release": OLD,
                "release": manifest(),
            },
        )
        with (
            patch.object(ops, "gates"),
            patch.object(ops, "run_info", return_value=info),
            patch.object(ops, "accepted"),
            patch.object(ops, "gh", return_value=history),
            patch.object(ops, "command", return_value=""),
        ):
            with self.assertRaisesRegex(ops.ReleaseError, "RECEIPT_MISMATCH"):
                ops.approved(99, self.root, "cosign")

    def test_missing_real_human_review_blocks_approval_before_download(self):
        info = {
            "run_attempt": 1,
            "head_sha": OLD,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with (
            patch.object(ops, "gates"),
            patch.object(ops, "run_info", return_value=info),
            patch.object(ops, "accepted"),
            patch.object(ops, "gh", return_value=[]),
            patch.object(ops, "command") as cmd,
        ):
            with self.assertRaisesRegex(ops.ReleaseError, "HUMAN_APPROVAL_MISSING"):
                ops.approved(99, self.root, "cosign")
            cmd.assert_not_called()

    def test_prepare_failure_cli_does_not_print_underlying_secret(self):
        with (
            patch.object(
                sys,
                "argv",
                [
                    "release_ops",
                    "prepare",
                    "--build-run",
                    "1",
                    "--output",
                    str(self.root / "o"),
                ],
            ),
            patch.object(ops, "prepare", side_effect=KeyError("access_token=secret")),
            redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaises(SystemExit):
                ops.main()
        self.assertNotIn("secret", output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["automatic_retry"])


if __name__ == "__main__":
    unittest.main()
