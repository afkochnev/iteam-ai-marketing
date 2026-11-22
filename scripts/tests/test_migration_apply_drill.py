"""Disposable pre-activation drill contract; no Docker or provider access."""

from __future__ import annotations

import copy
import fcntl
import importlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
drill = importlib.import_module("migration_apply_drill")


def request() -> dict:
    return drill.ops.load(Path(__file__).resolve().parents[1] / "fixtures" / "migration-0028-to-0030.json")


def snapshot() -> dict:
    counts = {name: 1 for name in (
        "campaigns", "tasks", "content_items", "content_versions", "approvals",
        "publications", "activity_logs", "users",
    )}
    return {
        "build_sha": request()["source_release_sha"],
        "schema": request()["source_schema_head"],
        "database_identity_sha256": "sha256:" + "1" * 64,
        "business_state": {"sha256": "sha256:" + "2" * 64, "table_counts": counts},
        "table_counts": {**counts, "alembic_version": 1},
    }


class FakeAdapter:
    instances: list = []
    events: list = []

    def __init__(self, runtime, manifest, operation, state, backups, keys):
        self.runtime, self.manifest, self.operation = runtime, manifest, operation
        self.state, self.backups, self.keys = state, backups, keys
        self.schema = manifest["source_schema_head"]
        self.readiness_checked = False
        self.actions = []
        self.__class__.instances.append(self)

    def inventory(self):
        return {
            "snapshot": snapshot(), "runtime_identity_sha256": "sha256:" + "3" * 64,
            "source_image_ids": {name: "sha256:" + "4" * 64 for name in drill.ops.APP},
        }

    def quiet(self):
        pass

    def backup(self):
        self.backups.mkdir(mode=0o700)
        self.keys.mkdir(mode=0o700)
        (self.backups / "encrypted.dump").write_bytes(b"encrypted fixture placeholder")
        (self.keys / "fixture.key").write_text("private fixture placeholder")
        source = snapshot()
        return {
            "proof_version": 1, "status": "PASS", "operation_id": self.operation,
            "manifest_sha256": drill.manifest_hash(self.manifest),
            "database_identity_sha256": source["database_identity_sha256"],
            "source_release_sha": self.manifest["source_release_sha"],
            "source_schema_head": self.manifest["source_schema_head"],
            "source_backend_image": self.manifest["source_images"]["backend"],
            "encrypted_backup_sha256": "sha256:" + "5" * 64,
            "created_at": datetime.now(UTC).isoformat(),
            "source_business_state": source["business_state"],
            "restored_business_state": source["business_state"],
            "table_counts_match": True, "isolated_restore_network": "none",
            "restore_container_removed": True,
        }

    def stop_schedulers(self):
        self.actions.append("stop_schedulers")

    def drain_workers(self):
        self.actions.append("drain_workers")

    def stop_writers(self):
        self.actions.append("stop_writers")

    def fence_database(self):
        self.actions.append("fence_database")

    def barrier(self):
        return {
            "writers_stopped": True, "database_fenced": True, "barrier_continuous": True,
            "barrier_sha256": "sha256:" + "6" * 64,
        }

    def snapshot(self, tables=None):
        result = snapshot()
        result["schema"] = self.schema
        if tables is not None:
            assert tables == sorted(result["business_state"]["table_counts"])
        return result

    def target_preflight(self):
        self.actions.append("target_preflight")

    def upgrade_exact(self):
        self.actions.append("upgrade")
        self.schema = self.manifest["target_schema_head"]

    def target_checks(self):
        self.actions.append("target_checks")

    def maintenance_readiness(self):
        self.actions.append("maintenance_readiness")
        self.readiness_checked = True

    def downgrade_exact(self):
        self.actions.append("downgrade")
        self.schema = self.manifest["source_schema_head"]

    def source_checks(self):
        self.actions.append("source_checks")

    def restore_source_configuration(self):
        self.actions.append("restore_source_configuration")

    def contain(self):
        self.actions.append("contain")

    def close(self):
        self.events.append(("adapter_closed", self.runtime.root))
        self.journal = drill.ops.load(self.state / ("operation-" + self.operation + ".json"))
        self.context = drill.ops.load(self.state / "disposable-fixture-authorization.json")


class FakeRecoveryAdapter(FakeAdapter):
    def maintenance_readiness(self):
        super().maintenance_readiness()
        raise drill.ops.ReleaseError("INJECTED_FAILURE")


class MigrationApplyDrillTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix=drill.TEMP_PREFIX))
        self.root = Path(self.directory).resolve()
        self.outputs = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        FakeAdapter.instances = []
        FakeAdapter.events = []

    def fixture(self):
        return drill.create_fixture(self.root, "iteam-migration-drill-0123456789", request())

    def test_generated_fixture_has_all_source_workers_inert_providers_and_only_loopback_port(self):
        runtime = self.fixture()
        config = drill.ops.load(self.root / "compose.json")
        self.assertEqual(set(config["services"]), drill.ops.ALL)
        self.assertEqual(config["services"]["postgres"]["ports"], ["127.0.0.1::5432"])
        self.assertEqual(config["networks"], {
            "default": {"internal": True}, "host_publish": {"driver": "bridge", "internal": False},
        })
        self.assertEqual(config["services"]["postgres"]["networks"], ["default", "host_publish"])
        self.assertTrue(all("networks" not in config["services"][name]
                            for name in drill.ops.ALL - {"postgres"}))
        env = dict(line.split("=", 1) for line in runtime.env_path.read_text().splitlines())
        self.assertEqual(env["APP_ENV"], "development")
        self.assertEqual(env["TASK_DISPATCH_INTERVAL_SECONDS"], "3600")
        self.assertEqual(env["OPTIMIZATION_ANALYSIS_SCAN_INTERVAL_SECONDS"], "3600")
        self.assertTrue(all(env[key] == "false" for key in drill.PROVIDER_FLAGS))
        self.assertTrue(all(env[key] == "" for key in drill.PROVIDER_CREDENTIALS))
        for name in drill.ops.WORKERS:
            service = config["services"][name]
            self.assertEqual(service["image"], request()["source_images"]["backend"])
            if name.endswith("_worker"):
                self.assertIn("--queues=" + name.removesuffix("_worker"), service["command"])
                self.assertIn("--concurrency=1", service["command"])
                self.assertIn("--pool=solo", service["command"])
            else:
                self.assertIn("beat", service["command"])
        for name in (".env", "compose.json", "source-images.json", "target-images.json"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600)

    def test_cannot_reuse_a_checkout_project_or_existing_directory(self):
        with self.assertRaisesRegex(drill.ops.ReleaseError, "UNSAFE_APPLY_DRILL_DIRECTORY"):
            drill.create_fixture(self.outputs, "iteam-migration-drill-0123456789", request())
        with self.assertRaisesRegex(drill.ops.ReleaseError, "UNSAFE_APPLY_DRILL_PROJECT"):
            drill.create_fixture(self.root, "iteam-ai-marketing", request())
        (self.root / "existing").write_text("keep")
        with self.assertRaisesRegex(drill.ops.ReleaseError, "UNSAFE_APPLY_DRILL_DIRECTORY"):
            self.fixture()
        self.assertEqual((self.root / "existing").read_text(), "keep")

    def test_static_policy_denies_production_credentials_external_mounts_and_namespaces(self):
        runtime = self.fixture()
        original = drill.ops.load(self.root / "compose.json")
        changes = [
            lambda value: value["networks"].update(default={"external": True}),
            lambda value: value["volumes"].update(postgres_data={"external": True}),
            lambda value: value["services"]["backend"].update(pid="host"),
            lambda value: value["services"]["backend"].update(network_mode="host"),
            lambda value: value["services"]["backend"].update(networks=["default", "host_publish"]),
            lambda value: value["services"]["redis"].update(networks=["host_publish"]),
            lambda value: value["services"]["postgres"].update(networks=["default", "foreign"]),
            lambda value: value["networks"].update(host_publish={"external": True}),
            lambda value: value["services"]["backend"].update(ipc="container:production"),
            lambda value: value["services"]["backend"].update(privileged=True),
            lambda value: value["services"]["postgres"].update(volumes=["/production:/var/lib/postgresql/data"]),
            lambda value: value["services"].update(extra={"image": "postgres:17-alpine"}),
            lambda value: value["services"]["metrics_worker"].update(image="backend:latest"),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                changed = copy.deepcopy(original)
                change(changed)
                drill.ops.save(self.root / "compose.json", changed)
                with self.assertRaisesRegex(drill.ops.ReleaseError, "UNSAFE_APPLY_DRILL_COMPOSE"):
                    drill.validate_fixture_files(runtime, request())
        drill.ops.save(self.root / "compose.json", original)
        original_env = runtime.env_path.read_text()
        for values in (
            {"APP_ENV": "production"}, {"DATABASE_URL": "postgresql://production/database"},
            {"VK_ACCESS_TOKEN": "secret"}, {"TELEGRAM_METRICS_SESSION": "secret"},
            {"METRICS_SYNC_ENABLED": "true"}, {"COMPOSE_FILE": "production.yml"},
        ):
            with self.subTest(keys=sorted(values)):
                runtime.env_path.write_text(drill.ops.set_env(original_env, values))
                with self.assertRaisesRegex(drill.ops.ReleaseError, "UNSAFE_APPLY_DRILL_ENVIRONMENT"):
                    drill.validate_fixture_files(runtime, request())

    def test_static_port_policy_rejects_non_loopback_fixed_or_extra_ports(self):
        runtime = self.fixture()
        original = drill.ops.load(self.root / "compose.json")
        for ports in (["5432:5432"], ["0.0.0.0::5432"], ["127.0.0.1:5432:5432"],
                      ["127.0.0.1::5432", "127.0.0.1::5433"]):
            changed = copy.deepcopy(original)
            changed["services"]["postgres"]["ports"] = ports
            drill.ops.save(self.root / "compose.json", changed)
            with self.assertRaisesRegex(drill.ops.ReleaseError, "UNSAFE_APPLY_DRILL_PORT"):
                drill.validate_fixture_files(runtime, request())

    def live(self, runtime):
        network_id = "a" * 64
        publish_network_id = "b" * 64
        network_name = runtime.project + "_default"
        publish_network_name = runtime.project + "_host_publish"
        containers = {}
        for index, name in enumerate(sorted(drill.ops.ALL), 1):
            volume = {"postgres": "postgres_data", "redis": "redis_data",
                      "publication_worker": "vk_publishing_credentials"}.get(name)
            ports = {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "45678"}]} if name == "postgres" else {}
            containers[name] = {
                "Id": f"{index:064x}",
                "Config": {
                    "Labels": {"com.docker.compose.project": runtime.project,
                               "com.docker.compose.service": name},
                    "Image": request()["source_images"]["frontend" if name == "frontend" else "backend"],
                    "Env": runtime.env_path.read_text().splitlines(),
                },
                "State": {"Running": True},
                "HostConfig": {"NetworkMode": network_name, "PidMode": "", "UTSMode": "",
                               "UsernsMode": "", "IpcMode": "private", "Privileged": False,
                               "PortBindings": copy.deepcopy(ports), "RestartPolicy": {"Name": "unless-stopped"},
                               "Binds": [runtime.project + "_" + volume + ":/data:rw"] if volume else None},
                "NetworkSettings": {
                    "Networks": {network_name: {"NetworkID": network_id}, **(
                        {publish_network_name: {"NetworkID": publish_network_id}} if name == "postgres" else {}
                    )}, "Ports": ports,
                },
                "Mounts": [{"Type": "volume", "Name": runtime.project + "_" + volume}] if volume else [],
            }
        network = {"Name": network_name, "Internal": True, "Driver": "bridge", "Scope": "local",
                   "Labels": {"com.docker.compose.project": runtime.project,
                              "com.docker.compose.network": "default"},
                   "Containers": {data["Id"]: {} for data in containers.values()}}
        publish_network = {"Name": publish_network_name, "Internal": False, "Driver": "bridge", "Scope": "local",
                           "Labels": {"com.docker.compose.project": runtime.project,
                                      "com.docker.compose.network": "host_publish"},
                           "Containers": {containers["postgres"]["Id"]: {}}}
        networks = {network_id: network, publish_network_id: publish_network}
        def command(argv, **kwargs):
            if argv[:3] == ["docker", "network", "inspect"]:
                return json.dumps([networks[argv[3]]])
            if argv[:2] == ["docker", "ps"]:
                return "\n".join(data["Id"] for data in containers.values())
            raise AssertionError("unexpected Docker command")
        return containers, networks, command

    def test_live_fixture_rejects_namespace_sharing_extra_network_members_and_remote_port(self):
        runtime = self.fixture()
        cases = ("pid", "network", "bind", "network_member", "remote_port", "extra_port", "provider_secret",
                 "app_on_publish_network", "redis_on_publish_network", "publish_foreign_member",
                 "foreign_network_label", "unexpected_network", "publish_internal",
                 "unallocated_port", "empty_port")
        for case in ("valid", "postgres_publish_mode", *cases):
            containers, networks, command = self.live(runtime)
            network = networks["a" * 64]
            publish_network = networks["b" * 64]
            publish_name = runtime.project + "_host_publish"
            if case == "pid":
                containers["backend"]["HostConfig"]["PidMode"] = "host"
            elif case == "network":
                containers["backend"]["HostConfig"]["NetworkMode"] = "container:production"
            elif case == "bind":
                containers["postgres"]["Mounts"][0]["Type"] = "bind"
            elif case == "network_member":
                network["Containers"]["f" * 64] = {}
            elif case == "remote_port":
                containers["postgres"]["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostIp"] = "0.0.0.0"
            elif case == "extra_port":
                containers["frontend"]["HostConfig"]["PortBindings"] = {"3000/tcp": [{}]}
            elif case == "provider_secret":
                containers["metrics_worker"]["Config"]["Env"].append("TELEGRAM_METRICS_SESSION=secret")
            elif case == "postgres_publish_mode":
                containers["postgres"]["HostConfig"]["NetworkMode"] = publish_name
            elif case in {"app_on_publish_network", "redis_on_publish_network"}:
                service = "ai_worker" if case == "app_on_publish_network" else "redis"
                containers[service]["NetworkSettings"]["Networks"][publish_name] = {"NetworkID": "b" * 64}
            elif case == "publish_foreign_member":
                publish_network["Containers"]["f" * 64] = {}
            elif case == "foreign_network_label":
                publish_network["Labels"]["com.docker.compose.project"] = "production"
            elif case == "unexpected_network":
                containers["postgres"]["NetworkSettings"]["Networks"]["foreign"] = {"NetworkID": "f" * 64}
            elif case == "publish_internal":
                publish_network["Internal"] = True
            elif case in {"unallocated_port", "empty_port"}:
                containers["postgres"]["NetworkSettings"]["Ports"]["5432/tcp"] = None if case == "unallocated_port" else []
            with self.subTest(case=case), patch.object(runtime, "inspect", side_effect=lambda service: containers[service]), \
                    patch.object(drill.ops, "command", side_effect=command), patch.object(drill, "_connects", return_value=True):
                if case in {"valid", "postgres_publish_mode"}:
                    ids, port = drill.verify_live_fixture(runtime, request())
                    self.assertEqual(len(ids), 11)
                    self.assertEqual(port, 45678)
                else:
                    with self.assertRaises(drill.ops.ReleaseError):
                        drill.verify_live_fixture(runtime, request())

    def test_boundary_requires_host_port_to_close_and_every_original_writer_stopped(self):
        runtime = self.fixture()
        containers, _, _ = self.live(runtime)
        ids = {name: data["Id"] for name, data in containers.items()}
        for name in (*drill.ops.APP, "postgres"):
            containers[name]["State"]["Running"] = name == "postgres"
            containers[name]["HostConfig"]["RestartPolicy"]["Name"] = "no"
        containers["postgres"]["NetworkSettings"]["Networks"] = {}
        adapter = Mock(barrier=Mock(return_value={
            "writers_stopped": True, "database_fenced": True, "barrier_continuous": True,
        }))
        with patch.object(runtime, "inspect", side_effect=lambda service: containers[service]), \
                patch.object(drill, "_connects", return_value=False) as connects:
            drill.verify_held_boundary(runtime, adapter, ids, 45678)
            connects.assert_called_once_with(45678)
            containers["ai_worker"]["State"]["Running"] = True
            with self.assertRaisesRegex(drill.ops.ReleaseError, "APPLY_DRILL_WRITER_BOUNDARY_OPEN"):
                drill.verify_held_boundary(runtime, adapter, ids, 45678)
            containers["ai_worker"]["State"]["Running"] = False
            connects.return_value = True
            with self.assertRaisesRegex(drill.ops.ReleaseError, "APPLY_DRILL_HOST_PORT_NOT_FENCED"):
                drill.verify_held_boundary(runtime, adapter, ids, 45678)

    def test_synthetic_context_is_bound_to_request_and_never_claims_production_approval(self):
        value = request()
        manifest, approval, context = drill.fixture_inputs(value, "success", "a" * 32)
        self.assertEqual(context["authorization"], "DISPOSABLE_FIXTURE_ONLY")
        self.assertIs(context["production_approval_verified"], False)
        self.assertEqual(context["request_scope"], drill.rehearsal.rehearsal_scope(value))
        self.assertEqual(context["manifest_sha256"], drill.manifest_hash(manifest))
        self.assertEqual(approval["receipt_sha256"], drill._hash(context))
        manifest["source_images"]["backend"] = "changed"
        self.assertEqual(value, request())
        with self.assertRaisesRegex(drill.ops.ReleaseError, "INVALID_APPLY_DRILL_AUTHORIZATION"):
            drill.fixture_inputs(value, "production", "a" * 32)

    def test_failure_is_injected_only_after_actual_maintenance_readiness(self):
        adapter = object.__new__(drill.ReadinessFailureRuntime)
        with patch.object(drill.MigrationRuntime, "maintenance_readiness") as readiness:
            with self.assertRaisesRegex(drill.ops.ReleaseError, "INJECTED_FAILURE"):
                adapter.maintenance_readiness()
            readiness.assert_called_once_with()
            self.assertTrue(adapter.readiness_checked)
        other = object.__new__(drill.ReadinessFailureRuntime)
        with patch.object(drill.MigrationRuntime, "maintenance_readiness", side_effect=drill.ops.ReleaseError("REAL_READINESS_FAILURE")):
            with self.assertRaisesRegex(drill.ops.ReleaseError, "REAL_READINESS_FAILURE"):
                other.maintenance_readiness()
            self.assertFalse(other.readiness_checked)

    def mocked_live_run(self, *, cleanup_failure=False):
        create = drill.create_fixture
        def fixture(root, project, value):
            runtime = create(root, project, value)
            runtime.compose = Mock(side_effect=lambda *args: json.dumps(snapshot()) if backup_code(args) else "")
            runtime.run_backend = Mock()
            runtime.schema = Mock(return_value=value["source_schema_head"])
            runtime.ready = Mock()
            runtime.frontend_ready = Mock()
            runtime.workers_ready = Mock()
            return runtime
        def backup_code(args):
            return drill.backup.SNAPSHOT_CODE in args
        def command(argv, **kwargs):
            if argv[:2] == ["docker", "info"]:
                return str(8 * 1024**3)
            if len(argv) >= 3 and argv[0] == "docker" and argv[2] == "ls":
                return ""
            raise AssertionError("unexpected Docker command")
        def cleanup(runtime):
            self.assertIn(("adapter_closed", runtime.root), FakeAdapter.events)
            FakeAdapter.events.append(("docker_removed", runtime.root))
            if cleanup_failure:
                raise drill.ops.ReleaseError("MIGRATION_DRILL_CLEANUP_INCOMPLETE")
        def inspect_records(state, operation):
            self.assertIn(("adapter_closed", state.parent), FakeAdapter.events)
            self.assertNotIn(("docker_removed", state.parent), FakeAdapter.events)
            # The production inspector takes a shared lock. Acquiring exclusive
            # here also proves that the apply's exclusive lock has been released.
            with (state / "operation.lock").open("rb") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            journal = drill.ops.load(state / ("operation-" + operation + ".json"))
            FakeAdapter.events.append(("records_inspected", state.parent))
            return {
                "record_state": "CONSISTENT_TERMINAL", "error_code": None,
                "recorded_status": journal["status"], "recorded_phase": journal["phase"],
                "writer_boundary": "UNKNOWN", "live_runtime_checked": False,
                "historical_continuity_verified": False, "receipt_signature_verified": False,
                "automatic_retry": False, "activation_authorized": False,
                "schema_downgrade_allowed": False,
            }
        self.stack.enter_context(patch.object(drill, "create_fixture", side_effect=fixture))
        self.stack.enter_context(patch.object(drill, "MigrationRuntime", FakeAdapter))
        self.stack.enter_context(patch.object(drill, "ReadinessFailureRuntime", FakeRecoveryAdapter))
        self.stack.enter_context(patch.object(drill, "verify_live_fixture", return_value=({}, 45678)))
        self.stack.enter_context(patch.object(drill, "verify_held_boundary"))
        self.stack.enter_context(patch.object(drill.rehearsal, "seed_fixture"))
        self.stack.enter_context(patch.object(drill.rehearsal, "remove_disposable_project", side_effect=cleanup))
        self.stack.enter_context(patch.object(drill.inspection, "inspect_operation", side_effect=inspect_records))
        verification = self.stack.enter_context(patch.object(drill.rehearsal, "verify_images"))
        self.stack.enter_context(patch.object(drill.ops, "command", side_effect=command))
        self.stack.enter_context(patch.object(drill.engine.approval, "verify_approved", side_effect=AssertionError("production approval must not run")))
        self.stack.enter_context(patch.object(drill.engine, "apply", side_effect=AssertionError("production apply must not run")))
        return verification

    def test_real_engine_and_journal_exercise_both_outcomes_then_publish_after_cleanup(self):
        verification = self.mocked_live_run()
        phases = []
        output = self.outputs / "proof.json"
        original_close = FakeAdapter.close
        def close_under_apply_lock(adapter):
            with (adapter.state / "operation.lock").open("rb") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            original_close(adapter)
        with patch.object(FakeAdapter, "close", close_under_apply_lock):
            proof = drill.run_drill(request(), output, "cosign", progress=phases.append)
        verification.assert_called_once()
        self.assertEqual(proof["proof_type"], "MIGRATION_PREACTIVATION_DRILL")
        self.assertEqual(proof["proof_contract_version"], 2)
        self.assertEqual(proof["authorization"], "DISPOSABLE_FIXTURE_ONLY")
        self.assertFalse(proof["production_approval_verified"])
        self.assertFalse(proof["application_external_network"])
        self.assertTrue(proof["postgres_has_host_publish_network"])
        self.assertFalse(proof["target_maintenance_external_network"])
        self.assertNotIn("external_network", proof)
        self.assertEqual(proof["success"]["status"], "PRE_ACTIVATION_READY")
        self.assertEqual(proof["recovery"]["status"], "ROLLED_BACK_PRE_ACTIVATION")
        self.assertEqual(proof["success"]["schema"], request()["target_schema_head"])
        self.assertEqual(proof["recovery"]["schema"], request()["source_schema_head"])
        self.assertEqual(proof["scenario_count"], 2)
        self.assertEqual(proof["disposable_services_per_scenario"], 11)
        self.assertEqual(drill.ops.load(output), proof)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(len(FakeAdapter.instances), 2)
        success, recovery = FakeAdapter.instances
        self.assertNotEqual(success.runtime.root, recovery.runtime.root)
        self.assertNotEqual(success.operation, recovery.operation)
        self.assertNotIn("downgrade", success.actions)
        self.assertIn("downgrade", recovery.actions)
        self.assertEqual(recovery.journal["failure_code"], "INJECTED_FAILURE")
        for adapter in FakeAdapter.instances:
            self.assertFalse(adapter.runtime.root.exists())
            self.assertFalse(adapter.journal["writer_activation_requested"])
            self.assertEqual(adapter.context["authorization"], drill.AUTHORIZATION)
            events = [event for event, root in FakeAdapter.events if root == adapter.runtime.root]
            self.assertEqual(events, ["adapter_closed", "records_inspected", "docker_removed"])
        for name in ("success", "recovery"):
            report = proof[name]["post_close_record_inspection"]
            self.assertEqual(report["record_state"], "CONSISTENT_TERMINAL")
            self.assertEqual(report["recorded_status"], proof[name]["status"])
            self.assertEqual(report["writer_boundary"], "UNKNOWN")
            self.assertFalse(report["activation_authorized"])
            self.assertIn(name + "_record_inspection", phases)
        self.assertIn("success_readiness", phases)
        self.assertIn("recovery_containment", phases)
        self.assertIn("recovery_downgrade", phases)
        self.assertEqual(phases[-1], "publish_proof")
        serialized = json.dumps(proof)
        for secret in ("private fixture placeholder", "drill-only", str(success.runtime.root), "DATABASE_URL"):
            self.assertNotIn(secret, serialized)

    def test_inconsistent_inspection_cleans_up_and_never_publishes_pass(self):
        self.mocked_live_run()
        output = self.outputs / "proof.json"
        with patch.object(drill.inspection, "inspect_operation", return_value={"record_state": "INVALID"}):
            with self.assertRaisesRegex(drill.ops.ReleaseError, "APPLY_DRILL_RECORD_INSPECTION_FAILED") as caught:
                drill.run_drill(request(), output, "cosign")
        self.assertEqual(caught.exception._apply_drill_failure_phase, "success_record_inspection")
        self.assertFalse(output.exists())
        self.assertFalse(FakeAdapter.instances[0].runtime.root.exists())
        self.assertEqual([event for event, _ in FakeAdapter.events], ["adapter_closed", "docker_removed"])

    def test_inspection_cannot_promote_saved_records_to_live_authority(self):
        self.mocked_live_run()
        valid = {
            "record_state": "CONSISTENT_TERMINAL", "recorded_status": "PRE_ACTIVATION_READY",
            "error_code": None, "writer_boundary": "UNKNOWN",
            **dict.fromkeys(("live_runtime_checked", "historical_continuity_verified",
                            "receipt_signature_verified", "automatic_retry",
                            "activation_authorized", "schema_downgrade_allowed"), False),
        }
        for key in ("writer_boundary", "live_runtime_checked", "historical_continuity_verified",
                    "receipt_signature_verified", "automatic_retry", "activation_authorized",
                    "schema_downgrade_allowed"):
            with self.subTest(key=key):
                report = {**valid, key: "PROVEN_CLOSED" if key == "writer_boundary" else True}
                output = self.outputs / (key + ".json")
                with patch.object(drill.inspection, "inspect_operation", return_value=report):
                    with self.assertRaisesRegex(drill.ops.ReleaseError, "APPLY_DRILL_RECORD_INSPECTION_FAILED"):
                        drill.run_drill(request(), output, "cosign")
                self.assertFalse(output.exists())
                self.assertFalse(FakeAdapter.instances[-1].runtime.root.exists())

    def test_cleanup_failure_destroys_temporary_files_and_never_publishes_pass(self):
        self.mocked_live_run(cleanup_failure=True)
        output = self.outputs / "proof.json"
        with self.assertRaisesRegex(drill.ops.ReleaseError, "MIGRATION_DRILL_CLEANUP_INCOMPLETE"):
            drill.run_drill(request(), output, "cosign")
        self.assertFalse(output.exists())
        self.assertEqual(len(FakeAdapter.instances), 1)
        self.assertFalse(FakeAdapter.instances[0].runtime.root.exists())

    def test_unexpected_apply_failure_keeps_primary_error_and_phase_through_cleanup(self):
        self.mocked_live_run()
        output = self.outputs / "proof.json"
        with patch.object(FakeAdapter, "target_checks", side_effect=drill.ops.ReleaseError("MIGRATION_TARGET_CHECKS_FAILED")):
            with self.assertRaises(drill.engine.ApplyFailure) as caught:
                drill.run_drill(request(), output, "cosign")
        self.assertEqual(caught.exception.result["error_code"], "MIGRATION_TARGET_CHECKS_FAILED")
        self.assertEqual(caught.exception.result["status"], "ROLLED_BACK_PRE_ACTIVATION")
        self.assertEqual(caught.exception._apply_drill_failure_phase, "success_target_checks")
        self.assertFalse(output.exists())
        self.assertFalse(FakeAdapter.instances[0].runtime.root.exists())

    def test_existing_output_and_symlink_are_rejected_before_docker_or_verification(self):
        target = self.outputs / "existing.json"
        target.write_text("keep")
        link = self.outputs / "link.json"
        link.symlink_to(self.outputs / "missing.json")
        with patch.object(drill.ops, "command") as command, patch.object(drill.rehearsal, "verify_images") as verify:
            for output in (target, link):
                with self.assertRaisesRegex(drill.ops.ReleaseError, "APPLY_DRILL_OUTPUT_EXISTS"):
                    drill.run_drill(request(), output, "cosign")
            command.assert_not_called()
            verify.assert_not_called()
        self.assertEqual(target.read_text(), "keep")
        with self.assertRaisesRegex(drill.ops.ReleaseError, "APPLY_DRILL_OUTPUT_EXISTS"):
            drill._publish(target, {"status": "PASS"})
        self.assertEqual(target.read_text(), "keep")

    def test_cli_has_no_production_namespace_override_and_diagnostics_drop_exception_text(self):
        parser = drill.build_parser()
        self.assertEqual({action.dest for action in parser._actions}, {"help", "request", "output", "cosign"})
        phases = []
        with self.assertRaisesRegex(drill.ops.ReleaseError, "INVALID_APPLY_DRILL_PHASE"):
            drill.report_phase(phases.append, "secret=not-a-phase")
        self.assertEqual(phases, [])
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["drill", "--request", "request.json", "--output", "proof.json"]), \
                patch.object(drill.ops, "load", side_effect=ValueError("private-token-and-database-url")), \
                redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit):
            drill.main()
        value = json.loads(out.getvalue())
        self.assertEqual(value["status"], "FAIL")
        self.assertEqual(value["phase"], "request_load")
        self.assertNotIn("private-token", out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
