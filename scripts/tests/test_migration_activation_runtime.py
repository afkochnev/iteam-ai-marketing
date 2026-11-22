"""Activation boundary, exact target policy and independent containment checks.

No Docker daemon, production credentials or approval authority is used here.
The signed-image integration drill exercises the actual engine separately.
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_activation_runtime as activation
import migration_apply_backup as backup
import migration_apply_runtime as preapply
import release_ops as ops
from test_release_manifest import manifest

OPERATION = "a" * 32
PROJECT = "iteam-ai-marketing"
POSTGRES, REDIS = "b" * 64, "c" * 64
NETWORK, OTHER_NETWORK = "d" * 64, "e" * 64
TARGET = "f" * 64
IDENTITY = {"system_identifier": "123456789", "database_oid": 16384, "database_name": "iteam"}


def config() -> dict:
    services = {name: {"image": "source", "restart": "unless-stopped"} for name in ops.ALL}
    for name in ops.APP:
        service = services[name]
        service["environment"] = {"BUILD_SHA": "1" * 40, "APP_ENV": "production"}
        if name in activation.WORKERS:
            role = name.removesuffix("_worker")
            service["environment"].update(WORKER_ROLE=role, SCHEDULER_ROLE="")
            service["command"] = ["celery", "-A", "app.workers.celery_app:celery_app", "worker", "--queues=" + role]
        elif name in ops.SCHEDULERS:
            service["environment"].update(WORKER_ROLE="", SCHEDULER_ROLE=name.removesuffix("_scheduler"))
            service["command"] = ["celery", "-A", "app.workers.celery_app:celery_app", "beat"]
    return {"name": PROJECT, "services": services, "networks": {"default": {"name": PROJECT + "_default"}}}


def target_config(source: dict) -> dict:
    target = copy.deepcopy(source)
    for name in ops.APP:
        target["services"][name]["image"] = "target"
        target["services"][name]["environment"].update(
            BUILD_SHA="2" * 40, RUNTIME_ADMISSION_MODE="active", COMPOSE_FILE="/private/frozen.json")
        target["services"][name]["labels"] = {activation.LABEL: OPERATION}
    return target


def event(kind: str, action: str, identifier: str, **attributes) -> dict:
    return {"Type": kind, "Action": action, "Actor": {"ID": identifier, "Attributes": attributes}}


class TargetConfigurationTests(unittest.TestCase):
    def test_only_release_and_admission_metadata_may_change(self):
        source = config()
        activation.validate_target_configuration(source, target_config(source), PROJECT, "2" * 40)
        changes = (
            lambda value: value["services"]["backend"]["environment"].update(OPENAI_API_KEY="changed"),
            lambda value: value["services"]["backend"]["environment"].update(APP_ENV="maintenance"),
            lambda value: value["services"]["backend"].update(ports=[{"target": 8000, "published": "9000"}]),
            lambda value: value["services"]["postgres"].update(image="other-database"),
            lambda value: value["networks"]["default"].update(internal=True),
        )
        for change in changes:
            with self.subTest(change=change):
                target = target_config(source)
                change(target)
                with self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
                    activation.validate_target_configuration(source, target, PROJECT, "2" * 40)

    def test_a_bound_but_unsafe_role_or_extra_queue_is_not_accepted(self):
        changes = (
            lambda value: value["services"]["ai_worker"]["command"].append("--queues=publication"),
            lambda value: value["services"]["ai_worker"]["command"].__setitem__(-1, "--queues=ai,publication"),
            lambda value: value["services"]["ai_scheduler"]["command"].append("worker"),
            lambda value: value["services"]["backend"]["environment"].update(WORKER_ROLE="ai"),
            lambda value: value["services"]["metrics_worker"]["environment"].update(METRICS_SYNC_ENABLED="true"),
        )
        for change in changes:
            with self.subTest(change=change):
                source = config()
                change(source)
                with self.assertRaises(ops.ReleaseError):
                    activation.validate_target_configuration(source, target_config(source), PROJECT, "2" * 40)

    def test_hooks_jobs_scales_and_namespace_overrides_fail_before_mutation(self):
        changes = (
            {"post_start": [{"command": "touch /side-effect"}]},
            {"pre_stop": [{"command": "touch /side-effect"}]},
            {"provider": {"type": "external"}}, {"use_api_socket": True},
            {"deploy": {"mode": "replicated-job"}}, {"deploy": {"replicas": 2}},
            {"scale": 2}, {"network_mode": "host"}, {"privileged": True},
        )
        for change in changes:
            with self.subTest(change=change):
                source = config()
                source["services"]["backend"].update(change)
                with self.assertRaisesRegex(ops.ReleaseError, "TARGET_CONFIG_INVALID"):
                    activation.validate_target_configuration(source, target_config(source), PROJECT, "2" * 40)

    def test_effective_image_environment_honors_overrides_and_unsets(self):
        image = {"Env": ["PATH=/bin", "FLAG=baked", "REMOVE=baked"]}
        configured = {"environment": {"FLAG": "false", "REMOVE": None}}
        self.assertEqual(activation._effective_environment(image, configured), {"PATH": "/bin", "FLAG": "false"})
        with self.assertRaises(ops.ReleaseError):
            activation._effective_environment({"Env": ["FLAG=a", "FLAG=b"]}, configured)

    def test_worker_passive_probe_uses_real_role_validators_not_api_openai_requirement(self):
        calls = []
        settings = types.SimpleNamespace(
            validate_worker_capabilities=lambda: calls.append("worker"),
            validate_scheduler_capabilities=lambda: calls.append("scheduler"),
            validate_redis_isolation=lambda: calls.append("redis"),
            validate_production=lambda: self.fail("API-only production validation on a role with blank OpenAI"),
        )
        class PassiveError(RuntimeError):
            pass
        admission = types.ModuleType("app.core.runtime_admission")
        admission.RuntimeAdmissionError = PassiveError
        admission.admission_status = lambda: {"admission_protocol": 1, "admission_mode": "passive", "business_ready": False}
        settings_module = types.ModuleType("app.core.config")
        settings_module.settings = settings
        output = io.StringIO()
        with patch.dict(sys.modules, {"app.core.config": settings_module, "app.core.runtime_admission": admission}), \
                patch("importlib.import_module", side_effect=PassiveError("RUNTIME_PASSIVE")), redirect_stdout(output):
            exec(activation.PASSIVE_CLI, {})
        self.assertEqual(calls, ["worker", "scheduler", "redis"])
        self.assertEqual(json.loads(output.getvalue())["admission_mode"], "passive")


class TargetAuditTests(unittest.TestCase):
    def setUp(self):
        self.old = "1" * 64
        self.final_name = PROJECT + "-backend-1"
        self.temporary_name = self.old[:12] + "_" + self.final_name
        self.audit = activation.TargetAudit(OPERATION, PROJECT, {self.old}, {POSTGRES, REDIS}, set(), {"backend": {NETWORK}},
                                            {"backend": {"source_id": self.old, "final_name": self.final_name}})
        self.audit.process = types.SimpleNamespace(poll=lambda: None)

    def create_target(self):
        self.audit.creating = {"backend"}
        self.audit.removing_old = True
        self.audit.observe(event("container", "create", TARGET, **{
            "com.docker.compose.project": PROJECT, "com.docker.compose.service": "backend", activation.LABEL: OPERATION,
            "name": self.temporary_name}))

    def rename_target(self, **overrides):
        attributes = {"com.docker.compose.project": PROJECT, "com.docker.compose.service": "backend",
                      activation.LABEL: OPERATION, "oldName": "/" + self.temporary_name, "name": self.final_name}
        attributes.update(overrides)
        self.audit.observe(event("container", "rename", TARGET, **attributes))

    def test_compose_recreate_allows_exactly_one_registered_temporary_to_final_rename(self):
        self.create_target()
        self.audit.observe(event("container", "destroy", self.old))
        self.rename_target()
        self.audit.check()
        self.assertFalse(self.audit.rename_permits)
        self.rename_target()
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()

    def test_recreate_rename_cannot_change_destination_source_service_or_operation(self):
        changes = (
            {"name": "unapproved-name"}, {"oldName": "/unapproved-source"},
            {"com.docker.compose.service": "ai_worker"},
            {"com.docker.compose.project": "another-project"}, {activation.LABEL: "0" * 32},
        )
        for change in changes:
            with self.subTest(change=change):
                self.setUp()
                self.create_target()
                self.rename_target(**change)
                with self.assertRaises(ops.ReleaseError):
                    self.audit.check()

    def test_target_rename_is_rejected_after_recreate_window_closes(self):
        self.create_target()
        self.audit.removing_old = False
        self.rename_target()
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()

    def test_create_under_an_unapproved_name_cannot_obtain_a_rename_permit(self):
        self.audit.creating = {"backend"}
        self.audit.removing_old = True
        self.audit.observe(event("container", "create", TARGET, **{
            "com.docker.compose.project": PROJECT, "com.docker.compose.service": "backend", activation.LABEL: OPERATION,
            "name": "arbitrary-temporary-name"}))
        self.assertFalse(self.audit.rename_permits)
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()

    def test_target_requires_registered_role_operation_and_explicit_start(self):
        self.create_target()
        self.audit.check()
        self.assertEqual(self.audit.target_ids, {TARGET})
        self.audit.observe(event("container", "start", TARGET))
        with self.assertRaisesRegex(ops.ReleaseError, "OBSERVER_LOST"):
            self.audit.check()

    def test_known_target_and_data_transitions_are_single_use(self):
        self.create_target()
        self.rename_target()
        self.audit.allow("container", "start", TARGET)
        self.audit.observe(event("network", "connect", NETWORK, container=TARGET))
        self.audit.observe(event("container", "start", TARGET))
        self.audit.allow("network", "connect", NETWORK + ":" + POSTGRES)
        self.audit.observe(event("network", "connect", NETWORK, container=POSTGRES))
        self.audit.check()
        self.audit.observe(event("network", "connect", NETWORK, container=POSTGRES))
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()

    def test_transient_target_network_change_and_unrelated_start_are_sticky(self):
        self.create_target()
        self.audit.observe(event("network", "connect", NETWORK, container=TARGET))
        self.audit.observe(event("network", "disconnect", NETWORK, container=TARGET))
        self.audit.allow("network", "connect", NETWORK + ":" + TARGET)
        self.audit.observe(event("network", "connect", NETWORK, container=TARGET))
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()

    def test_source_retirement_never_permits_restarting_source(self):
        self.audit.removing_old = True
        self.audit.observe(event("container", "rename", self.old))
        self.audit.observe(event("container", "destroy", self.old))
        self.audit.check()
        self.audit.observe(event("container", "start", self.old))
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()

    def test_unknown_role_duplicate_create_and_wrong_operation_are_rejected(self):
        for service, operation in (("unknown", OPERATION), ("backend", "0" * 32)):
            with self.subTest(service=service, operation=operation):
                audit = activation.TargetAudit(OPERATION, PROJECT, {self.old}, {POSTGRES, REDIS}, set())
                audit.process = types.SimpleNamespace(poll=lambda: None)
                audit.creating = {"backend"}
                audit.observe(event("container", "create", TARGET, **{
                    "com.docker.compose.project": PROJECT, "com.docker.compose.service": service, activation.LABEL: operation}))
                with self.assertRaises(ops.ReleaseError):
                    audit.check()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env_path = self.root / ".env"
        self.env_path.write_text("APP_ENV=production\nBUILD_SHA=" + "1" * 40 + "\n")
        self.env_path.chmod(0o600)
        state = self.root / "state"
        state.mkdir(mode=0o700)
        self.source = preapply.MigrationRuntime(
            types.SimpleNamespace(root=self.root, env_path=self.env_path, project=PROJECT),
            manifest(), OPERATION, state, self.root / "backup", self.root / "key")
        self.runtime = activation.MigrationActivationRuntime(self.source)
        self.source._database_identity = backup.database_identity_hash(IDENTITY)
        self.source._database_environment = {"POSTGRES_USER": "iteam", "POSTGRES_DB": "iteam", "POSTGRES_PASSWORD": "test-secret"}
        self.source.writer_barrier._ids = {name: f"{index:064x}" for index, name in enumerate(ops.APP, 1)}
        self.source.writer_barrier.postgres_id = POSTGRES
        self.runtime._data = {
            "postgres": {"Id": POSTGRES, "NetworkSettings": {"Networks": {"first": {"NetworkID": NETWORK}, "second": {"NetworkID": OTHER_NETWORK}}}},
            "redis": {"Id": REDIS, "NetworkSettings": {"Networks": {"first": {"NetworkID": NETWORK}}}},
        }

    def test_constructor_exposes_scope_before_inventory(self):
        self.assertEqual(self.runtime.project, PROJECT)
        self.assertEqual(self.runtime.operation_id, OPERATION)
        explicit = preapply.ExplicitRuntime(self.root, PROJECT, [self.root / "compose.json"])
        self.assertEqual(explicit.project, PROJECT)

    def test_target_names_are_fixed_by_compose_and_immutable_container_number(self):
        configured = config()
        for name in ops.APP:
            self.source.writer_barrier._raw[name] = {
                "Name": "/" + PROJECT + "-" + name + "-1",
                "Config": {"Labels": {"com.docker.compose.container-number": "1"}},
            }
        expected = {name: PROJECT + "-" + name + "-1" for name in ops.APP}
        self.assertEqual(self.runtime._target_names(configured), expected)
        self.source.writer_barrier._raw["backend"]["Name"] = "/unapproved-name"
        with self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
            self.runtime._target_names(configured)

    def test_recreate_step_is_closed_and_does_not_expose_command_error_details(self):
        self.assertIsNone(self.runtime.recreate_step)
        with patch.object(self.runtime, "_permit", side_effect=OSError("secret command detail")), self.assertRaises(OSError):
            self.runtime.recreate_target_stopped(None)
        self.assertEqual(self.runtime.recreate_step, "permit")
        self.assertIn(self.runtime.recreate_step, activation.RECREATE_STEPS)

    def source_network_configuration(self):
        configured = config()
        physical = PROJECT + "_default"
        self.source.writer_barrier.safe_baseline = {"postgres_networks": {physical: {"NetworkID": NETWORK}}}
        for name in ops.ALL:
            configured["services"][name]["networks"] = {"default": {}}
            identifier = POSTGRES if name == "postgres" else REDIS if name == "redis" else self.source.writer_barrier._ids[name]
            self.source.writer_barrier._raw[name] = {
                "Id": identifier, "Name": "/" + PROJECT + "-" + name + "-1",
                "HostConfig": {"Memory": 0, "NanoCpus": 0},
                "NetworkSettings": {"Networks": {physical: {
                    "NetworkID": NETWORK, "Aliases": [name, PROJECT + "-" + name + "-1"], "IPAMConfig": None}}},
            }
        return configured, physical

    def test_source_networks_are_anchored_to_signed_postgres_ids_before_consumption(self):
        configured, physical = self.source_network_configuration()
        self.runtime._source_topology(configured)
        self.source.writer_barrier._raw["redis"]["NetworkSettings"]["Networks"][physical]["NetworkID"] = OTHER_NETWORK
        with patch.object(ops, "command") as command, self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
            self.runtime._source_topology(configured)
        command.assert_not_called()  # A fresh lookup cannot rebase the approved ID.

    def test_resolved_compose_null_network_options_preserve_identity_checks(self):
        configured, physical = self.source_network_configuration()
        for service in configured["services"].values():
            service["networks"]["default"] = None
        self.runtime._source_topology(configured)
        self.source.writer_barrier._raw["redis"]["NetworkSettings"]["Networks"][physical]["NetworkID"] = OTHER_NETWORK
        with self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
            self.runtime._source_topology(configured)

    def test_extra_attachment_and_declared_app_only_network_are_both_rejected(self):
        configured, physical = self.source_network_configuration()
        redis = self.source.writer_barrier._raw["redis"]["NetworkSettings"]["Networks"]
        redis["app-only"] = {"NetworkID": OTHER_NETWORK, "Aliases": ["redis"]}
        with self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
            self.runtime._source_topology(configured)
        configured["networks"]["additional"] = {"name": "app-only"}
        configured["services"]["redis"]["networks"]["additional"] = {}
        with self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
            self.runtime._source_topology(configured)

    def test_alias_and_static_ipam_changes_cannot_silently_expand_declared_topology(self):
        configured, physical = self.source_network_configuration()
        endpoint = self.source.writer_barrier._raw["redis"]["NetworkSettings"]["Networks"][physical]
        endpoint["Aliases"].append("another-database")
        with self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
            self.runtime._source_topology(configured)
        endpoint["Aliases"].pop()
        endpoint["IPAMConfig"] = {"IPv4Address": "172.20.0.99"}
        with self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
            self.runtime._source_topology(configured)
        configured["services"]["redis"]["networks"]["default"]["ipv4_address"] = "172.20.0.99"
        self.runtime._source_topology(configured)

    def test_mutated_memory_or_cpu_limits_fail_before_target_creation(self):
        configured, physical = self.source_network_configuration()
        configured["services"]["backend"].update(mem_limit="512m", cpus="0.5")
        host = self.source.writer_barrier._raw["backend"]["HostConfig"]
        host.update(Memory=512 * 1024 ** 2, NanoCpus=500_000_000)
        self.runtime._source_topology(configured)
        for key in ("Memory", "NanoCpus"):
            original = host[key]
            host[key] += 1
            with self.subTest(key=key), self.assertRaisesRegex(ops.ReleaseError, "POLICY_CHANGED"):
                self.runtime._source_topology(configured)
            host[key] = original

    def test_same_byte_plan_replacement_and_environment_replacement_invalidate_live_plan(self):
        self.runtime._plan = {"version": 1, "manifest_sha256": activation.manifest_hash(self.source.manifest)}
        self.runtime._environment, self.runtime._environment_identity = activation._private_read(self.env_path)
        plan_file = self.root / "state" / "plan.json"
        self.runtime._record_file(plan_file, b"{}\n")
        with patch.object(self.source, "_unchanged_configuration"):
            self.runtime._check_plan()
            replacement = self.root / "replacement"
            replacement.write_bytes(b"{}\n")
            replacement.chmod(0o600)
            os.replace(replacement, plan_file)
            with self.assertRaisesRegex(ops.ReleaseError, "PLAN_CHANGED"):
                self.runtime._check_plan()

    def test_write_once_does_not_overwrite_existing_operational_bytes(self):
        path = self.root / "state" / "plan.json"
        first = activation._write_once(path, b"first")
        self.assertEqual(activation._write_once(path, b"first"), first)
        with self.assertRaisesRegex(ops.ReleaseError, "PLAN_CHANGED"):
            activation._write_once(path, b"second")
        self.assertEqual(path.read_bytes(), b"first")

    def test_linked_or_public_plan_files_are_not_valid_authority_inputs(self):
        path = self.root / "unsafe"
        path.write_text("{}")
        path.chmod(0o644)
        with self.assertRaises(ops.ReleaseError):
            activation._private_read(path)
        path.chmod(0o600)
        os.link(path, self.root / "alias")
        with self.assertRaises(ops.ReleaseError):
            activation._private_read(path)

    def test_handoff_keeps_both_observers_live_across_the_durable_marker(self):
        order = []
        old = types.SimpleNamespace(closed=False, process=None, thread=None, volumes=set())
        self.source.writer_barrier.audit = old
        target = types.SimpleNamespace(sync=lambda: order.append("target_sync"))
        def start():
            self.runtime.audit = target
            order.append("target_started_and_synchronized")
        def close_old():
            order.append("source_closed")
            old.closed = True
        state = types.SimpleNamespace(begin_activation=lambda: order.append("durable_marker"),
                                      assert_activation_permitted=lambda: order.append("permit"))
        with patch.object(self.runtime, "_check_plan"), patch.object(self.runtime, "_verify_data"), \
                patch.object(self.runtime, "_start_audit", side_effect=start), \
                patch.object(self.source, "barrier", side_effect=lambda: order.append("source_barrier")), \
                patch.object(self.source.writer_barrier, "close", side_effect=close_old):
            self.runtime.handoff_boundary(state)
        self.assertEqual(order, ["target_started_and_synchronized", "source_barrier", "durable_marker", "permit",
                                 "source_barrier", "target_sync", "source_closed", "target_sync"])
        self.assertTrue(self.runtime._crossed)

    def test_uncertain_marker_failure_still_forbids_return_to_preapply(self):
        state = Mock()
        state.begin_activation.side_effect = OSError("lost durable response")
        with patch.object(self.runtime, "_check_plan"), patch.object(self.runtime, "_verify_data"), \
                patch.object(self.runtime, "_start_audit"), patch.object(self.source, "barrier"), \
                patch.object(self.source.writer_barrier, "close") as close, self.assertRaises(OSError):
            self.runtime.handoff_boundary(state)
        self.assertTrue(self.runtime._crossed)
        close.assert_not_called()

    def test_revoked_or_closed_runtime_cannot_begin_any_active_mutation(self):
        self.runtime._crossed = True
        self.runtime.audit = Mock()
        state = Mock(spec=["assert_activation_permitted"])
        state.assert_activation_permitted.side_effect = ops.ReleaseError("REVOKED")
        with patch.object(self.runtime, "_check_plan"), patch.object(ops, "command") as command:
            with self.assertRaisesRegex(ops.ReleaseError, "REVOKED"):
                self.runtime.recreate_target_stopped(state)
            state.assert_activation_permitted.side_effect = None
            self.runtime._closed = True
            with self.assertRaisesRegex(ops.ReleaseError, "PHASE_INVALID"):
                self.runtime.reconnect_data_services(state)
        command.assert_not_called()

    def test_forked_process_cannot_mutate_or_inspect_live_operation(self):
        with patch.object(activation.os, "getpid", return_value=self.runtime._owner_pid + 1), \
                self.assertRaisesRegex(ops.ReleaseError, "PROCESS_CHANGED"):
            self.runtime.contain_target()

    def namespace(self, extras=()):
        rows = [[POSTGRES, "/postgres", "bridge", "no"], [REDIS, "/redis", "bridge", "no"], *extras]
        def command(args, **kwargs):
            return "\n".join(row[0] for row in rows) if args[:2] == ["docker", "ps"] else "\n".join(json.dumps(row) for row in rows)
        return patch.object(ops, "command", side_effect=command)

    def test_post_recreation_namespace_check_does_not_require_old_container_ids(self):
        with self.namespace():
            self.runtime._namespace_members()

    def test_transitive_namespace_sharer_and_redis_sharer_are_rejected(self):
        for extras in (
            [[TARGET, "/first", "container:postgres", "no"], ["2" * 64, "/second", "container:first", "no"]],
            [[TARGET, "/first", "container:redis", "no"]],
        ):
            with self.subTest(extras=extras), self.namespace(extras), self.assertRaises(ops.ReleaseError):
                self.runtime._namespace_members()

    def test_only_current_registered_no_restart_postgres_helper_is_allowed(self):
        self.source._helpers[TARGET] = None
        with self.namespace([[TARGET, "/helper", "container:postgres", "no"]]):
            self.runtime._namespace_members()
        with self.namespace([[TARGET, "/helper", "container:postgres", "always"]]), self.assertRaises(ops.ReleaseError):
            self.runtime._namespace_members()
        self.source._helpers.clear()
        self.source.writer_barrier.allowed_helpers.add(TARGET)
        with self.namespace([[TARGET, "/helper", "container:postgres", "no"]]), self.assertRaises(ops.ReleaseError):
            self.runtime._namespace_members()

    def test_two_discovery_failures_and_a_stop_failure_still_attempt_data_fencing(self):
        self.runtime.audit = types.SimpleNamespace(allow=lambda *args: None, sync=lambda: None)
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[:2] == ["docker", "ps"]:
                raise ops.ReleaseError("DISCOVERY_FAILED")
            return ""
        stopped = {"HostConfig": {"RestartPolicy": {"Name": "no"}}}
        with patch.object(self.runtime, "_start_audit"), patch.object(self.runtime, "_discover_writers", side_effect=OSError()), \
                patch.object(ops, "command", side_effect=command), patch.object(self.runtime, "_inspect", return_value=stopped), \
                patch.object(self.runtime, "_stop_writers", side_effect=OSError()) as stop, \
                patch.object(self.runtime, "_fence_data") as fence, \
                patch.object(self.runtime, "_verify_data", side_effect=OSError()), self.assertRaisesRegex(ops.ReleaseError, "CONTAINMENT_FAILED"):
            self.runtime.contain_target()
        stop.assert_called_once()
        fence.assert_called_once()
        updated = {args[-1] for args in calls if args[:2] == ["docker", "update"]}
        self.assertEqual(updated, {POSTGRES, REDIS, *self.source.writer_barrier._ids.values()})

    def test_one_network_detach_failure_does_not_skip_other_network_or_redis(self):
        detached = []
        by_id = {value["Id"]: value for value in self.runtime._data.values()}
        def command(args, **kwargs):
            if args[:3] == ["docker", "network", "disconnect"]:
                detached.append((args[-2], args[-1]))
                if args[-2:] == [NETWORK, POSTGRES]:
                    raise ops.ReleaseError("DISCONNECT_FAILED")
            return "lo" if args[-1] == "ls /sys/class/net" else "Iface Destination"
        with patch.object(self.runtime, "_inspect", side_effect=lambda identifier: by_id[identifier]), \
                patch.object(ops, "command", side_effect=command), self.assertRaises(ops.ReleaseError):
            self.runtime._fence_data()
        self.assertEqual(detached, [(NETWORK, POSTGRES), (OTHER_NETWORK, POSTGRES), (NETWORK, REDIS)])

    def test_one_writer_inspection_failure_does_not_skip_another_writer_stop(self):
        stopped = set()
        def inspect(identifier):
            if identifier == "1" * 64:
                raise OSError("inspection unavailable")
            return {"State": {"Running": identifier not in stopped}, "NetworkSettings": {"Networks": {}}}
        def command(args, **kwargs):
            stopped.add(args[-1])
            return ""
        with patch.object(self.runtime, "_inspect", side_effect=inspect), patch.object(ops, "command", side_effect=command), \
                self.assertRaisesRegex(ops.ReleaseError, "CONTAINMENT_FAILED"):
            self.runtime._stop_writers({"1" * 64, "2" * 64})
        self.assertEqual(stopped, {"2" * 64})

    def test_close_is_idempotent_and_readonly_snapshot_does_not_revive_authority(self):
        observer = types.SimpleNamespace(closed=True, process=None, thread=None, volumes=set())
        self.source.writer_barrier.audit = observer
        with patch.object(self.source, "close") as close:
            self.runtime.close()
            self.runtime.close()
            close.assert_called_once()
        snapshot = {"schema": self.source.manifest["target_schema_head"], "database_identity": IDENTITY,
                    "all_tables_sha256": "sha256:" + "9" * 64,
                    "table_counts": {"alembic_version": 1, "workspaces": 2, "campaigns": 4}}
        with patch.object(self.runtime, "_check_plan"), patch.object(self.runtime, "_verify_data"), \
                patch.object(self.runtime, "_namespace_members"), patch.object(self.runtime, "_run_helper", return_value=snapshot) as helper:
            result = self.runtime.full_snapshot()
        self.assertEqual(result["database_identity_sha256"], self.source._database_identity)
        self.assertTrue(self.runtime._closed)
        self.assertEqual(helper.call_args.kwargs["environment"]["OPENAI_API_KEY"], "")
        self.assertEqual(helper.call_args.kwargs["environment"]["APP_ENV"], "maintenance")

    def test_close_cannot_claim_cleanup_while_observer_thread_is_alive(self):
        self.source.writer_barrier.audit = types.SimpleNamespace(
            closed=True, process=None, thread=types.SimpleNamespace(is_alive=lambda: True), volumes=set())
        with patch.object(self.source, "close"), self.assertRaisesRegex(ops.ReleaseError, "CLEANUP_FAILED"):
            self.runtime.close()
        self.assertTrue(self.runtime._closed)
        self.assertFalse(self.runtime._close_verified)


if __name__ == "__main__":
    unittest.main()
