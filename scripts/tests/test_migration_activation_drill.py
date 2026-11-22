"""Signed fixture provenance, isolation, commit observation and proof publication."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_activation_contract as contract
import migration_activation_drill as drill
import migration_release_drill as rehearsal
import release_ops as ops
from test_migration_activation_contract import proof as combined_fixture


def request() -> dict:
    return json.loads((ROOT / "scripts/fixtures/migration-activation-0028-to-0030.json").read_text())


class RequestTests(unittest.TestCase):
    def test_new_request_pins_signed_gate_capable_build_and_retains_source(self):
        value = request()
        contract.validate_activation_request(value)
        old = json.loads((ROOT / "scripts/fixtures/migration-0028-to-0030.json").read_text())
        migration = value["migration_request"]
        for key in ("source_release_sha", "source_schema_head", "source_images", "migration_path"):
            self.assertEqual(migration[key], old[key])
        self.assertEqual(migration["target_release_sha"], "5285d47580c84476165bf6dd4ad1df3809db7884")
        self.assertEqual(migration["target_build_run_id"], 45047752884)
        self.assertEqual(migration["target_build_attempt"], 1)
        self.assertEqual(migration["target_images"]["backend"],
                         "ghcr.io/afkochnev/iteam-ai-marketing-backend@sha256:3b346a47255f1d743a52dbe2f1e403765b42431741c908df35f3f65e9819e341")
        self.assertEqual(migration["target_images"]["frontend"],
                         "ghcr.io/afkochnev/iteam-ai-marketing-frontend@sha256:d05cc79a22955238c38f4e61550e961360d725efb58965c936f7a348e3262b63")

    def test_fixture_receipt_is_explicit_and_not_a_production_approval(self):
        value = request()
        original = copy.deepcopy(value)
        manifest, receipt, context = drill.fixture_inputs(value, "failure", "f" * 32)
        self.assertEqual(value, original)
        self.assertEqual(context["authorization"], "DISPOSABLE_SIGNED_ACTIVATION_ONLY")
        self.assertFalse(context["production_approval_verified"])
        self.assertTrue(context["synthetic_receipt"])
        self.assertEqual(receipt["approval_run_id"], 2)
        self.assertEqual(receipt["receipt_sha256"], contract.hash_json(context))
        self.assertEqual(manifest["release_type"], "MIGRATION")
        self.assertEqual(manifest["rehearsal_proof"]["workflow_run_id"], 2)

    def test_no_production_inputs_in_cli(self):
        parser = drill.build_parser()
        options = {value for action in parser._actions for value in action.option_strings}
        self.assertEqual(options, {"-h", "--help", "--request", "--output", "--cosign"})


class IsolationTests(unittest.TestCase):
    def test_fixture_is_private_has_no_application_external_network_or_credentials(self):
        with tempfile.TemporaryDirectory(prefix=drill.TEMP_PREFIX) as directory:
            root = Path(directory).resolve()
            with mock.patch.dict(os.environ, {}, clear=True):
                runtime = drill.create_fixture(root, drill.PROJECT_PREFIX + "a" * 10,
                                               request()["migration_request"])
            self.assertEqual(runtime.root, root)
            config = ops.load(root / "compose.json")
            self.assertEqual(set(config["services"]), ops.ALL)
            self.assertEqual(config["networks"]["default"], {"internal": True})
            self.assertFalse(config["networks"]["host_publish"]["internal"])
            self.assertEqual(config["services"]["postgres"]["ports"], ["127.0.0.1::5432"])
            for name in ops.APP:
                service = config["services"][name]
                self.assertNotIn("ports", service)
                self.assertNotIn("networks", service)
                self.assertEqual(service["restart"], "unless-stopped")
            environment = dict(line.split("=", 1) for line in (root / ".env").read_text().splitlines())
            self.assertEqual(environment["RUNTIME_ADMISSION_MODE"], "active")
            self.assertTrue(all(environment[key] == "" for key in drill.apply_drill.PROVIDER_CREDENTIALS))
            self.assertTrue(all(environment[key] == "false" for key in drill.apply_drill.PROVIDER_FLAGS))
            self.assertEqual(stat.S_IMODE((root / ".env").stat().st_mode), 0o600)

    def test_constructor_rejects_existing_production_namespace_and_nonempty_root(self):
        with tempfile.TemporaryDirectory(prefix=drill.TEMP_PREFIX) as directory:
            root = Path(directory).resolve()
            with self.assertRaisesRegex(ops.ReleaseError, "UNSAFE_PROJECT"):
                drill.create_fixture(root, "iteam-ai-marketing", request()["migration_request"])
            (root / "existing").write_text("belongs to someone")
            with self.assertRaisesRegex(ops.ReleaseError, "UNSAFE_DIRECTORY"):
                drill.create_fixture(root, drill.PROJECT_PREFIX + "a" * 10, request()["migration_request"])

    def test_old_factory_guard_is_not_relaxed_for_new_prefix(self):
        with tempfile.TemporaryDirectory(prefix=drill.TEMP_PREFIX) as directory:
            with self.assertRaisesRegex(ops.ReleaseError, "INVALID_MIGRATION_DRILL_PROJECT"):
                rehearsal.MigrationDrillRuntime(Path(directory), project=drill.PROJECT_PREFIX + "a" * 10)

    def test_passive_environment_is_selected_before_target_container_creation(self):
        migration = request()["migration_request"]
        health = {"status": "ok", "database": "ok", "redis": "ok", "schema": "ok",
                  "build_sha": migration["target_release_sha"], "admission_protocol": 1,
                  "admission_mode": "passive", "business_ready": False, "database_read_only": True}
        with tempfile.TemporaryDirectory() as directory:
            runtime = object.__new__(drill.PassiveMigrationRuntime)
            runtime.env_path = Path(directory) / ".env"
            runtime.env_path.write_text("RUNTIME_ADMISSION_MODE=active\nBUILD_SHA=unchanged\n")
            observed = []

            def compose(*args):
                observed.append((args, runtime.env_path.read_text()))
                return json.dumps(health) if args[0] == "exec" else ""

            runtime.compose = compose
            with mock.patch.object(rehearsal.MigrationDrillRuntime, "app_ready") as parent:
                runtime.app_ready(migration, target=True)
            self.assertIn("--force-recreate", observed[0][0])
            self.assertTrue(all("RUNTIME_ADMISSION_MODE=passive" in env for _, env in observed))
            self.assertTrue(runtime.target_passive["before_downgrade"])
            self.assertFalse(runtime.target_passive["business_ready"])
            parent.assert_called_once_with(migration, True)

    def test_active_target_health_cannot_be_claimed_as_passive_evidence(self):
        migration = request()["migration_request"]
        with tempfile.TemporaryDirectory() as directory:
            runtime = object.__new__(drill.PassiveMigrationRuntime)
            runtime.env_path = Path(directory) / ".env"
            runtime.env_path.write_text("RUNTIME_ADMISSION_MODE=active\n")
            runtime.compose = mock.Mock(return_value=json.dumps({"status": "ok", "admission_mode": "active"}))
            with mock.patch.object(rehearsal.MigrationDrillRuntime, "app_ready"):
                with self.assertRaises(drill.admission.DrillError):
                    runtime.app_ready(migration, target=True)


class SignedTargetTests(unittest.TestCase):
    def test_signed_refs_resolve_to_actual_local_image_ids(self):
        migration = request()["migration_request"]
        commands = []

        def inspect(argv):
            commands.append(argv)
            return json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {
                "Env": ["BUILD_SHA=" + migration["target_release_sha"]],
                "Labels": {"org.opencontainers.image.revision": migration["target_release_sha"]},
            }}])

        with mock.patch.object(ops, "command", side_effect=inspect):
            result = drill.target_images(migration)
        self.assertEqual(set(result), {"backend", "frontend"})
        self.assertEqual({call[-1] for call in commands}, set(migration["target_images"].values()))

    def test_wrong_target_revision_is_rejected(self):
        with mock.patch.object(ops, "command", return_value=json.dumps([{
            "Id": "sha256:" + "a" * 64, "Config": {"Labels": {"org.opencontainers.image.revision": "f" * 40}},
        }])):
            with self.assertRaisesRegex(ops.ReleaseError, "IMAGE_MISMATCH"):
                drill.target_images(request()["migration_request"])

    def test_adversarial_uses_registry_image_without_candidate_build_and_requires_cleanup(self):
        commands = mock.Mock()
        commands.run.return_value.stdout = json.dumps([{"Id": "sha256:" + "c" * 64}])
        instance = mock.Mock()
        instance.cleanup.return_value = True
        passive = {"status": "PASS"}
        migration = request()["migration_request"]
        images = {"backend": "sha256:" + "b" * 64, "frontend": "sha256:" + "d" * 64}
        with (mock.patch.object(drill.admission, "Commands", return_value=commands),
              mock.patch.object(drill.admission, "Runtime", return_value=instance) as runtime,
              mock.patch.object(drill.admission, "passive_checks", return_value=(passive, {})),
              mock.patch.object(drill.admission.Candidate, "build", side_effect=AssertionError("local build forbidden"))):
            result = drill.adversarial_passive(migration, images)
        metadata = runtime.call_args.args[1]
        self.assertEqual(metadata, {"candidate_sha": migration["target_release_sha"],
                                    "candidate_image_id": images["backend"]})
        self.assertEqual(result["target_image_id"], images["backend"])
        self.assertTrue(result["cleanup_verified"])
        instance.start.assert_called_once()
        instance.cleanup.assert_called_once()

    def test_failed_passive_fixture_is_cleaned_and_cannot_return_pass(self):
        commands = mock.Mock()
        commands.run.return_value.stdout = json.dumps([{"Id": "sha256:" + "c" * 64}])
        instance = mock.Mock()
        instance.cleanup.return_value = True
        with (mock.patch.object(drill.admission, "Commands", return_value=commands),
              mock.patch.object(drill.admission, "Runtime", return_value=instance),
              mock.patch.object(drill.admission, "passive_checks", side_effect=drill.admission.DrillError("ADMISSION_MISMATCH"))):
            with self.assertRaises(drill.admission.DrillError):
                drill.adversarial_passive(request()["migration_request"], {"backend": "sha256:" + "b" * 64})
        instance.cleanup.assert_called_once()


class CommittedWorkTests(unittest.TestCase):
    def snapshot(self, digest="a"):
        return {"schema": drill.SCHEMA, "all_tables_sha256": "sha256:" + digest * 64,
                "database_identity_sha256": "sha256:" + "d" * 64,
                "table_counts": dict.fromkeys(sorted(contract._TARGET_TABLES), 1)}

    def backend(self, adapter, code, value=None):
        if code == drill.HTTP_CODE:
            return {"http_status": 201, "body": {"id": "c", "name": drill.CANARY_NAME}}
        if code == drill.ENQUEUE_CODE:
            return {"enqueued": True}
        if code == drill.CANARY_CODE:
            return {"status": "FAILED", "started": True, "finished": True, "count": 1}
        raise AssertionError(code)

    def test_http_commit_real_claim_and_stable_full_snapshot_are_all_required(self):
        adapter = mock.Mock()
        adapter.full_snapshot.side_effect = [self.snapshot("a"), self.snapshot("b"), self.snapshot("b")]
        with (mock.patch.object(drill, "_backend_json", side_effect=self.backend) as backend,
              mock.patch.object(drill.time, "sleep")):
            result = drill.committed_work(adapter)
        self.assertEqual(result, self.snapshot("b"))
        self.assertEqual([call.args[1] for call in backend.call_args_list],
                         [drill.HTTP_CODE, drill.ENQUEUE_CODE, drill.CANARY_CODE])
        self.assertEqual(adapter.full_snapshot.call_count, 3)

    def test_rejected_http_write_never_enqueues_or_claims_work(self):
        adapter = mock.Mock()
        with mock.patch.object(drill, "_backend_json", return_value={"http_status": 503}) as backend:
            with self.assertRaisesRegex(ops.ReleaseError, "WRITE_NOT_PROVED"):
                drill.committed_work(adapter)
        backend.assert_called_once()
        adapter.full_snapshot.assert_not_called()

    def test_queue_delivery_alone_cannot_prove_real_worker_claim(self):
        adapter = mock.Mock()

        def responses(current, code, value=None):
            if code == drill.CANARY_CODE:
                return {"status": "QUEUED", "started": False, "finished": False, "count": 1}
            return self.backend(current, code, value)

        with (mock.patch.object(drill, "_backend_json", side_effect=responses),
              mock.patch.object(drill.time, "monotonic", side_effect=[0, 1, 91]),
              mock.patch.object(drill.time, "sleep")):
            with self.assertRaisesRegex(ops.ReleaseError, "CLAIM_NOT_PROVED"):
                drill.committed_work(adapter)
        adapter.full_snapshot.assert_not_called()

    def test_container_payloads_are_valid_python(self):
        for name in ("HTTP_CODE", "ENQUEUE_CODE", "CANARY_CODE"):
            with self.subTest(name=name):
                compile(getattr(drill, name), "<" + name + ">", "exec")


class ProofPublicationTests(unittest.TestCase):
    def orchestrate(self, directory: Path, *, fail=None):
        value = request()
        evidence = combined_fixture(value)

        def nested(*args, **kwargs):
            evidence["migration_rehearsal"]["created_at"] = datetime.now(UTC).isoformat()
            return evidence["migration_rehearsal"], evidence["migration_target_passive"]

        def scenario(request, name, images, **kwargs):
            if name == fail:
                raise ops.ReleaseError("ACTIVATION_DRILL_CLEANUP_FAILED")
            return evidence[name]

        with (mock.patch.object(ops, "command", return_value=str(8 * 1024**3)),
              mock.patch.object(rehearsal, "require_clean_compose_environment"),
              mock.patch.object(drill, "checkout_identity", return_value={
                  "controller_sha": evidence["controller_sha"], "controller_tree": evidence["controller_tree"]}),
              mock.patch.object(drill, "migration_rehearsal", side_effect=nested),
              mock.patch.object(drill, "target_images", return_value={"backend": "sha256:" + "8" * 64}),
              mock.patch.object(drill, "adversarial_passive", return_value=evidence["adversarial_passive"]),
              mock.patch.object(drill, "scenario", side_effect=scenario)):
            return drill.run_drill(value, directory / "proof.json", "cosign")

    def test_complete_proof_passes_actual_validator_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "proof.json"
            result = self.orchestrate(Path(directory))
            self.assertEqual(json.loads(output.read_text()), result)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertTrue(result["observers_closed"])
            self.assertFalse(result["production_approval_verified"])
            self.assertEqual(result["failure"]["committed_all_tables_sha256"],
                             result["failure"]["contained_all_tables_sha256"])

    def test_cleanup_failure_prevents_proof_publication(self):
        for name in ("success", "failure"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ops.ReleaseError, "CLEANUP_FAILED"):
                    self.orchestrate(Path(directory), fail=name)
                self.assertFalse((Path(directory) / "proof.json").exists())

    def test_existing_output_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "proof.json"
            output.write_text("owned output")
            with self.assertRaisesRegex(ops.ReleaseError, "OUTPUT_UNSAFE"):
                self.orchestrate(Path(directory))
            self.assertEqual(output.read_text(), "owned output")

    def test_cli_diagnostics_do_not_print_runtime_exception_text(self):
        with (mock.patch.object(sys, "argv", ["drill", "--request", "request.json", "--output", "proof.json"]),
              mock.patch.object(ops, "load", side_effect=ValueError("secret-uri")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            with self.assertRaises(SystemExit):
                drill.main()
        self.assertNotIn("secret-uri", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error_code"], "ACTIVATION_DRILL_FAILED")


if __name__ == "__main__":
    unittest.main()
