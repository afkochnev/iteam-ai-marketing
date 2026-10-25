from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_ops as ops
from test_release_ops import FakeRuntime, OLD, NEW, manifest


class EnvironmentTests(unittest.TestCase):
    def test_signed_image_default_changes_preserve_explicit_operator_policy(self):
        configured = {"JWT_SECRET": "fixture-secret", "VK_PUBLISHING_ENABLED": "true"}
        old = [
            "PATH=/bin",
            "PYTHON_VERSION=3.12.11",
            "PYTHON_SHA256=old",
            "JWT_SECRET=fixture-secret",
            "VK_PUBLISHING_ENABLED=true",
        ]
        new = [
            line.replace("3.12.11", "3.12.12").replace("SHA256=old", "SHA256=new")
            for line in old
        ]
        old_hash, old_operator = ops.environment_hashes(
            old, old[:3], configured, "backend"
        )
        new_hash, new_operator = ops.environment_hashes(
            new, new[:3], configured, "backend"
        )
        self.assertNotEqual(old_hash, new_hash)
        self.assertEqual(old_operator, new_operator)
        with tempfile.TemporaryDirectory() as folder:
            runtime = FakeRuntime(Path(folder))
            before = runtime.topology()
            runtime.sha = NEW
            after = runtime.topology()
            before["backend"].update(env_hash=old_hash, operator_env_hash=old_operator)
            after["backend"].update(env_hash=new_hash, operator_env_hash=new_operator)
            ops.compare(before, after, NEW)

    def test_actual_image_environment_must_match_metadata_without_prefix_exemptions(
        self,
    ):
        baked = ["PYTHON_VERSION=3.12.12", "PYTHON_SHA256=expected", "PATH=/bin"]
        for actual in [
            baked + ["PYTHON_SECRET=do-not-log"],
            baked[:-1],
            ["PYTHON_VERSION=wrong", *baked[1:]],
            [*baked[:-1], "PATH=/untrusted"],
        ]:
            with (
                self.subTest(actual=actual),
                self.assertRaisesRegex(
                    ops.ReleaseError, "^RUNTIME_ENVIRONMENT_MISMATCH:backend$"
                ),
            ):
                ops.environment_hashes(actual, baked, {}, "backend")

    def test_explicit_overrides_beat_image_defaults_and_cannot_drift(self):
        baked = [
            "JWT_SECRET=image-value",
            "VK_PUBLISHING_ENABLED=false",
            "PYTHON_VERSION=new",
        ]
        configured = {
            "JWT_SECRET": "operator-secret",
            "VK_PUBLISHING_ENABLED": "true",
            "PYTHON_VERSION": "pinned",
        }
        actual = [k + "=" + v for k, v in configured.items()]
        ops.environment_hashes(actual, baked, configured, "publication_worker")
        for key in configured:
            wrong = [
                k + "=" + (v if k != key else "wrong") for k, v in configured.items()
            ]
            with self.assertRaises(ops.ReleaseError):
                ops.environment_hashes(wrong, baked, configured, "publication_worker")
        _, before = ops.environment_hashes(actual, baked, configured, "backend")
        changed = {**configured, "VK_PUBLISHING_ENABLED": "false"}
        _, after = ops.environment_hashes(
            [k + "=" + v for k, v in changed.items()], baked, changed, "backend"
        )
        self.assertNotEqual(before, after)

    def test_unset_and_only_release_bookkeeping_exclusions(self):
        actual = ["JWT_SECRET=fixture", "BUILD_SHA=new", "COMPOSE_FILE=new-overlay"]
        configured = {
            "REMOVE": None,
            "JWT_SECRET": "fixture",
            "BUILD_SHA": "old",
            "COMPOSE_FILE": "old-overlay",
        }
        ops.environment_hashes(actual, ["REMOVE=image-default"], configured, "backend")
        for entries in [["A=one", "A=two"], ["missing-equals"]]:
            with self.assertRaisesRegex(
                ops.ReleaseError, "INVALID_ENVIRONMENT_METADATA"
            ):
                ops.environment_hashes(entries, [], {}, "backend")

    def test_operator_changes_and_data_service_env_changes_are_still_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            before = FakeRuntime(Path(folder)).topology()
            for name, key in [
                ("backend", "operator_env_hash"),
                ("postgres", "env_hash"),
                ("frontend", "runtime_policy_hash"),
                ("publication_worker", "mounts"),
            ]:
                after = copy.deepcopy(before)
                after[name][key] = "unintended"
                with self.assertRaisesRegex(ops.ReleaseError, "RUNTIME_POLICY_CHANGED"):
                    ops.compare(before, after, OLD)


class FailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_failure(self, original, rollback=None):
        runtime = FakeRuntime(self.root)
        original_restart = runtime.restart

        def restart(*, migration_check=True):
            if migration_check:
                runtime.sha = NEW
                raise original
            if rollback is not None:
                raise rollback
            original_restart(migration_check=False)

        with (
            patch.object(runtime, "restart", side_effect=restart),
            patch.object(ops, "pull_and_validate"),
            patch.object(ops, "command", return_value=""),
        ):
            with self.assertRaises(ops.ReleaseError) as caught:
                ops.switch(
                    runtime, manifest(), self.root, "attempt", git_checkout=False
                )
        return runtime, caught.exception

    def test_original_failure_survives_successful_rollback(self):
        runtime, error = self.run_failure(
            ops.ReleaseError("RUNTIME_POLICY_CHANGED:backend:operator_env_hash")
        )
        self.assertEqual(str(error), "DEPLOY_FAILED_ROLLBACK_COMPLETED")
        expected = "RUNTIME_POLICY_CHANGED:backend:operator_env_hash"
        self.assertEqual(error.deployment_error_code, expected)
        for name in ["attempt-failure.json", "attempt-result.json"]:
            data = ops.load(self.root / name)
            self.assertEqual(data["deployment_error_code"], expected)
            self.assertEqual(data["rollback_status"], "COMPLETED")
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(runtime.sha, OLD)

    def test_original_failure_is_saved_before_recovery_and_rollback_error_is_separate(
        self,
    ):
        runtime = FakeRuntime(self.root)

        def restart(*, migration_check=True):
            if migration_check:
                raise ops.ReleaseError("FRONTEND_READINESS_TIMEOUT")
            recorded = ops.load(self.root / "attempt-failure.json")
            self.assertEqual(
                recorded["deployment_error_code"], "FRONTEND_READINESS_TIMEOUT"
            )
            self.assertEqual(recorded["rollback_status"], "PENDING")
            raise ops.ReleaseError("WORKER_QUEUES_NOT_READY")

        with (
            patch.object(runtime, "restart", side_effect=restart),
            patch.object(ops, "pull_and_validate"),
            patch.object(ops, "command", return_value=""),
        ):
            with self.assertRaises(ops.ReleaseError) as caught:
                ops.switch(
                    runtime, manifest(), self.root, "attempt", git_checkout=False
                )
        error = caught.exception
        self.assertEqual(str(error), "ROLLBACK_REQUIRES_OPERATOR_ATTENTION")
        self.assertEqual(error.deployment_error_code, "FRONTEND_READINESS_TIMEOUT")
        self.assertEqual(error.rollback_error_code, "WORKER_QUEUES_NOT_READY")
        self.assertEqual(
            ops.load(self.root / "attempt-result.json")["rollback_error_code"],
            "WORKER_QUEUES_NOT_READY",
        )

    def test_arbitrary_exception_and_unknown_codes_never_persist_secrets(self):
        for original in [
            RuntimeError("access_token=DO_NOT_LOG"),
            ops.ReleaseError("TOKEN_DO_NOT_LOG"),
        ]:
            _, error = self.run_failure(original)
            self.assertEqual(error.deployment_error_code, "UNEXPECTED_DEPLOYMENT_ERROR")
            text = "".join(p.read_text() for p in self.root.glob("*failure.json"))
            self.assertNotIn("DO_NOT_LOG", text)

    def test_diagnostic_io_failure_cannot_prevent_rollback(self):
        real_save = ops.save

        def save(path, value):
            if path.name == "attempt-failure.json":
                raise OSError("private-secret")
            return real_save(path, value)

        with patch.object(ops, "save", side_effect=save):
            runtime, error = self.run_failure(
                ops.ReleaseError("FRONTEND_READINESS_TIMEOUT")
            )
        self.assertEqual(runtime.sha, OLD)
        self.assertEqual(error.deployment_error_code, "FRONTEND_READINESS_TIMEOUT")

    def test_cli_returns_original_code_and_separate_recovery_outcome(self):
        error = ops.ReleaseError(
            "DEPLOY_FAILED_ROLLBACK_COMPLETED",
            deployment_error_code="FRONTEND_READINESS_TIMEOUT",
        )
        stream = io.StringIO()
        with (
            patch.object(sys, "argv", ["release_ops.py", "check-gates"]),
            patch.object(ops, "gates", side_effect=error),
            redirect_stdout(stream),
            self.assertRaises(SystemExit),
        ):
            ops.main()
        value = json.loads(stream.getvalue())
        self.assertEqual(value["deployment_error_code"], "FRONTEND_READINESS_TIMEOUT")
        self.assertEqual(value["error_code"], "DEPLOY_FAILED_ROLLBACK_COMPLETED")
        self.assertFalse(value["automatic_retry"])


class TopologyWiringTests(unittest.TestCase):
    def test_runtime_uses_immutable_image_metadata_and_hashes_only_secret_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            runtime = ops.Runtime(Path(folder))
            config = {
                "services": {
                    name: {"environment": {"PRIVATE": "fixture-do-not-leak"}}
                    for name in ops.ALL
                }
            }
            config["services"]["publication_worker"]["volumes"] = [
                {"source": "vk", "type": "volume", "target": "/run/iteam-secrets/vk"}
            ]
            image_id = "sha256:" + "a" * 64

            def inspect(name):
                return {
                    "Image": image_id,
                    "State": {"Running": True},
                    "Config": {
                        "Env": [
                            "PYTHON_VERSION=3.12.12",
                            "PRIVATE=fixture-do-not-leak",
                        ],
                        "Image": "mutable-tag-not-used",
                        "Cmd": [name],
                        "Labels": {"org.opencontainers.image.revision": OLD},
                    },
                    "HostConfig": {},
                    "Mounts": [],
                    "NetworkSettings": {"Networks": {"isolated": {}}},
                }

            def image_command(args):
                self.assertEqual(args, ["docker", "image", "inspect", image_id])
                return json.dumps([{"Config": {"Env": ["PYTHON_VERSION=3.12.12"]}}])

            with (
                patch.object(runtime, "compose", return_value=json.dumps(config)),
                patch.object(runtime, "inspect", side_effect=inspect),
                patch.object(ops, "command", side_effect=image_command) as tool,
            ):
                result = runtime.topology()
                self.assertEqual(tool.call_count, 1)
                self.assertNotIn("fixture-do-not-leak", json.dumps(result))
                self.assertEqual(len(result), 11)
