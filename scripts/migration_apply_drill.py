#!/usr/bin/env python3
"""Exercise pre-activation apply and recovery on two disposable Docker fixtures.

Only a rehearsal request and a new output file are accepted. Synthetic journal
receipts are explicitly scoped to DISPOSABLE_FIXTURE_ONLY; this command never
authenticates an operator approval or calls the production apply entry point.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import migration_apply_backup as backup
import migration_operation_inspect as inspection
import migration_release_apply as engine
import migration_release_drill as rehearsal
import release_ops as ops
from migration_apply_runtime import ERROR_CODES as RUNTIME_ERROR_CODES, MigrationRuntime
from migration_apply_state import ACTION_PHASES, Journal, canonical_bytes, manifest_hash
from migration_business_fixture import require_business_fixture
from migration_writer_barrier import ERROR_CODES as BARRIER_ERROR_CODES
from release_manifest import ManifestContractError, validate_migration_manifest

AUTHORIZATION = "DISPOSABLE_FIXTURE_ONLY"
PROOF_TYPE = "MIGRATION_PREACTIVATION_DRILL"
TEMP_PREFIX = "iteam-migration-apply-drill-"
PROJECT_PATTERN = re.compile(r"iteam-migration-drill-[0-9a-f]{10}\Z")
PROVIDER_FLAGS = (
    "TELEGRAM_PUBLISHING_ENABLED", "VK_PUBLISHING_ENABLED",
    "TELEGRAM_METRICS_ENABLED", "VK_METRICS_ENABLED", "METRICS_SYNC_ENABLED",
)
PROVIDER_CREDENTIALS = (
    "OPENAI_API_KEY", "OPENAI_VECTOR_STORE_ID", "OPENAI_AGENTS_TRACING_API_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_TARGET_CHAT_ID",
    "VK_ACCESS_TOKEN", "VK_CREDENTIALS_FILE", "VK_CLIENT_ID", "VK_EXPECTED_USER_ID",
    "VK_OWNER_ID", "VK_CANARY_PUBLICATION_ID", "TELEGRAM_METRICS_API_ID",
    "TELEGRAM_METRICS_API_HASH", "TELEGRAM_METRICS_SESSION", "TELEGRAM_METRICS_PEER",
    "TELEGRAM_METRICS_CHAT_ID", "VK_METRICS_ACCESS_TOKEN", "VK_METRICS_OWNER_ID",
)
_SCENARIO_PHASES = frozenset({
    "fixture", "infrastructure", "source_migrate", "seed", "source_ready",
    "source_topology", "source_data", "apply", "held_boundary", "record_inspection", "cleanup",
    "temporary_cleanup",
}) | frozenset(ACTION_PHASES)
_PHASES = frozenset({
    "request_load", "preflight", "image_verification", "publish_proof",
}) | rehearsal._DIAGNOSTIC_PHASES | frozenset(
    f"{scenario}_{phase}"
    for scenario in ("success", "recovery") for phase in _SCENARIO_PHASES
)
ERROR_CODES = frozenset({
    "INVALID_APPLY_DRILL_PHASE", "UNSAFE_APPLY_DRILL_DIRECTORY",
    "UNSAFE_APPLY_DRILL_PROJECT", "UNSAFE_APPLY_DRILL_COMPOSE",
    "UNSAFE_APPLY_DRILL_ENVIRONMENT", "UNSAFE_APPLY_DRILL_TOPOLOGY",
    "UNSAFE_APPLY_DRILL_PORT", "APPLY_DRILL_HOST_PORT_UNREACHABLE",
    "APPLY_DRILL_HOST_PORT_NOT_FENCED", "APPLY_DRILL_WRITER_BOUNDARY_OPEN",
    "APPLY_DRILL_SOURCE_STATE_MISMATCH", "APPLY_DRILL_FINAL_STATE_MISMATCH",
    "APPLY_DRILL_OUTCOME_MISMATCH", "APPLY_DRILL_JOURNAL_MISMATCH",
    "APPLY_DRILL_RECORD_INSPECTION_FAILED",
    "APPLY_DRILL_HELPER_CLEANUP_INCOMPLETE", "APPLY_DRILL_TEMPORARY_CLEANUP_INCOMPLETE",
    "APPLY_DRILL_OUTPUT_EXISTS", "APPLY_DRILL_REQUIRES_6_GIB",
    "APPLY_DRILL_DOWNGRADE_POLICY_REQUIRED", "INVALID_APPLY_DRILL_AUTHORIZATION",
})
_SAFE_ERROR_CODES = (
    ERROR_CODES | RUNTIME_ERROR_CODES | BARRIER_ERROR_CODES | backup.ERROR_CODES
    | engine.ERROR_CODES | engine.FAILURE_CODES
)


def report_phase(progress, phase: str) -> None:
    ops.require(type(phase) is str and phase in _PHASES, "INVALID_APPLY_DRILL_PHASE")
    if progress is not None:
        progress(phase)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def fixture_inputs(request: dict, scenario: str, operation_id: str) -> tuple[dict, dict, dict]:
    """Synthetic shape-valid inputs, never evidence of production approval."""
    rehearsal.validate_rehearsal_request(request)
    ops.require(
        scenario in {"success", "recovery"}
        and re.fullmatch(r"[0-9a-f]{32}", operation_id),
        "INVALID_APPLY_DRILL_AUTHORIZATION",
    )
    scope = rehearsal.rehearsal_scope(request)
    fixture = {
        "authorization": AUTHORIZATION, "production_approval_verified": False,
        "scenario": scenario, "operation_id": operation_id, "request_scope": scope,
    }
    # These positive integers satisfy the shared journal's shape contract only.
    # They are not fetched from GitHub and must never be used by production apply.
    synthetic_id = 1 if scenario == "success" else 2
    manifest = {
        key: copy.deepcopy(value) for key, value in request.items()
        if key not in {"version", "request_type"}
    }
    manifest.update(
        version=2, release_type="MIGRATION",
        rehearsal_proof={
            "workflow_run_id": synthetic_id, "artifact_id": synthetic_id,
            "proof_sha256": _hash(fixture), **copy.deepcopy(scope),
        },
    )
    validate_migration_manifest(manifest)
    fixture["manifest_sha256"] = manifest_hash(manifest)
    approval = {
        "approval_run_id": synthetic_id, "artifact_id": synthetic_id,
        "receipt_sha256": _hash(fixture), "approved_at": datetime.now(UTC).isoformat(),
    }
    return manifest, approval, fixture


def _private_fixture_root(root: Path, *, empty: bool = False) -> None:
    ops.require(
        root.is_absolute() and root.resolve() == root and root.is_dir()
        and not root.is_symlink()
        and re.fullmatch(re.escape(TEMP_PREFIX) + r"[a-z0-9_\-]{8}", root.name),
        "UNSAFE_APPLY_DRILL_DIRECTORY",
    )
    metadata = root.stat()
    ops.require(
        metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700
        and (not empty or not any(root.iterdir())),
        "UNSAFE_APPLY_DRILL_DIRECTORY",
    )


def _worker_service(name: str, image: str) -> dict:
    service: dict[str, Any] = {
        "image": image, "env_file": ".env", "stop_grace_period": "210s",
        "restart": "unless-stopped", "mem_limit": "256m", "cpus": 0.5,
        "depends_on": {
            "postgres": {"condition": "service_healthy"},
            "redis": {"condition": "service_healthy"},
        },
    }
    if name.endswith("_scheduler"):
        service["environment"] = {
            "WORKER_ROLE": "",
            "SCHEDULER_ROLE": "publication" if name.startswith("publication") else "ai",
        }
        service["command"] = [
            "celery", "-A", "app.workers.celery_app:celery_app", "beat",
            "--loglevel=WARNING", "--schedule=/tmp/beat",
        ]
    else:
        role = name.removesuffix("_worker")
        service["environment"] = {"WORKER_ROLE": role, "SCHEDULER_ROLE": ""}
        service["command"] = [
            "celery", "-A", "app.workers.celery_app:celery_app", "worker",
            "--loglevel=WARNING", "--queues=" + role, "--concurrency=1", "--pool=solo",
        ]
    if name == "publication_worker":
        service["volumes"] = ["vk_publishing_credentials:/run/iteam-secrets/vk"]
    return service


def create_fixture(root: Path, project: str, request: dict) -> rehearsal.MigrationDrillRuntime:
    """Extend the existing fixture, only inside an empty generated private root."""
    _private_fixture_root(root, empty=True)
    ops.require(PROJECT_PATTERN.fullmatch(project), "UNSAFE_APPLY_DRILL_PROJECT")
    runtime = rehearsal.create_drill(root, project, request)
    updates = {key: "false" for key in PROVIDER_FLAGS}
    updates.update({key: "" for key in PROVIDER_CREDENTIALS})
    updates.update({
        "TASK_DISPATCH_INTERVAL_SECONDS": "3600",
        "OPTIMIZATION_ANALYSIS_SCAN_INTERVAL_SECONDS": "3600",
        "OPENAI_AGENTS_DISABLE_TRACING": "true",
    })
    runtime.env_path.write_text(ops.set_env(runtime.env_path.read_text(), updates))
    runtime.env_path.chmod(0o600)
    config = ops.load(root / "compose.json")
    config["services"]["postgres"]["ports"] = ["127.0.0.1::5432"]
    config["services"]["postgres"]["restart"] = "unless-stopped"
    # Docker publishes ports through a default-gateway network, which an
    # internal-only container lacks. Only this synthetic PostgreSQL joins the
    # generated publishing bridge. Applications/Redis remain internal, and the
    # real writer barrier removes BOTH PostgreSQL attachments before migration.
    config["services"]["postgres"]["networks"] = ["default", "host_publish"]
    config["networks"]["host_publish"] = {"driver": "bridge", "internal": False}
    for name in ("backend", "frontend"):
        config["services"][name].update(restart="unless-stopped", stop_grace_period="210s")
    for name in ops.WORKERS:
        config["services"][name] = _worker_service(name, request["source_images"]["backend"])
    config["volumes"]["vk_publishing_credentials"] = {}
    ops.save(root / "compose.json", config)
    validate_fixture_files(runtime, request)
    return runtime


def validate_fixture_files(runtime: rehearsal.MigrationDrillRuntime, request: dict) -> None:
    """Deny a reused production checkout, external mount, namespace or endpoint."""
    _private_fixture_root(runtime.root)
    rehearsal.require_clean_compose_environment()
    rehearsal.validate_rehearsal_request(request)
    ops.require(PROJECT_PATTERN.fullmatch(runtime.project), "UNSAFE_APPLY_DRILL_PROJECT")
    for name in (".env", "compose.json", "source-images.json", "target-images.json"):
        path = runtime.root / name
        ops.require(
            path.is_file() and not path.is_symlink()
            and stat.S_IMODE(path.stat().st_mode) == 0o600,
            "UNSAFE_APPLY_DRILL_DIRECTORY",
        )
    config = ops.load(runtime.root / "compose.json")
    ops.require(
        set(config) == {"services", "volumes", "networks"}
        and set(config["services"]) == ops.ALL
        and config["volumes"] == {
            "postgres_data": {}, "redis_data": {}, "vk_publishing_credentials": {},
        }
        and config["networks"] == {
            "default": {"internal": True},
            "host_publish": {"driver": "bridge", "internal": False},
        },
        "UNSAFE_APPLY_DRILL_COMPOSE",
    )
    services = config["services"]
    allowed = {
        "postgres": {"image", "environment", "volumes", "healthcheck", "ports", "restart", "networks"},
        "redis": {"image", "volumes", "healthcheck"},
        "backend": {"env_file", "depends_on", "mem_limit", "cpus", "restart", "stop_grace_period"},
        "frontend": {"depends_on", "mem_limit", "cpus", "restart", "stop_grace_period"},
    }
    for name, fields in allowed.items():
        ops.require(type(services[name]) is dict and set(services[name]) == fields,
                    "UNSAFE_APPLY_DRILL_COMPOSE")
    ops.require(
        services["postgres"]["ports"] == ["127.0.0.1::5432"], "UNSAFE_APPLY_DRILL_PORT",
    )
    ops.require(
        services["postgres"]["image"] == "postgres:17-alpine"
        and services["redis"]["image"] == "redis:8-alpine"
        and services["postgres"]["environment"] == {
            "POSTGRES_USER": "iteam", "POSTGRES_PASSWORD": "drill-only",
            "POSTGRES_DB": rehearsal.DATABASE_NAME,
        }
        and services["postgres"]["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
        and services["redis"]["volumes"] == ["redis_data:/data"]
        and services["postgres"]["networks"] == ["default", "host_publish"]
        and services["backend"]["env_file"] == ".env",
        "UNSAFE_APPLY_DRILL_COMPOSE",
    )
    for name in ops.WORKERS:
        ops.require(services[name] == _worker_service(name, request["source_images"]["backend"]),
                    "UNSAFE_APPLY_DRILL_COMPOSE")
    for side in ("source", "target"):
        ops.require(ops.load(runtime.root / (side + "-images.json")) == {
            "services": {name: {"image": request[side + "_images"][name]}
                         for name in ("backend", "frontend")},
        }, "UNSAFE_APPLY_DRILL_COMPOSE")
    lines = runtime.env_path.read_text().splitlines()
    ops.require(all("=" in line for line in lines), "UNSAFE_APPLY_DRILL_ENVIRONMENT")
    env = dict(line.split("=", 1) for line in lines)
    ops.require(len(env) == len(lines), "UNSAFE_APPLY_DRILL_ENVIRONMENT")
    expected = {
        "APP_ENV": "development", "BUILD_SHA": request["source_release_sha"],
        "DATABASE_URL": "postgresql+asyncpg://iteam:drill-only@postgres:5432/" + rehearsal.DATABASE_NAME,
        "REDIS_URL": "redis://redis:6379/15", "ADMIN_EMAIL": "migration-admin@example.com",
        "TASK_DISPATCH_INTERVAL_SECONDS": "3600",
        "OPTIMIZATION_ANALYSIS_SCAN_INTERVAL_SECONDS": "3600",
        "OPENAI_AGENTS_DISABLE_TRACING": "true",
        **{key: "false" for key in PROVIDER_FLAGS},
        **{key: "" for key in PROVIDER_CREDENTIALS},
    }
    ops.require(
        set(env) == set(expected) | {"JWT_SECRET", "APP_SECRET", "ADMIN_PASSWORD"}
        and all(env.get(key) == value for key, value in expected.items())
        and all(re.fullmatch(r"[A-Za-z0-9_\-]{40,}", env[key])
                for key in ("JWT_SECRET", "APP_SECRET", "ADMIN_PASSWORD")),
        "UNSAFE_APPLY_DRILL_ENVIRONMENT",
    )


def _connects(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def verify_live_fixture(runtime: rehearsal.MigrationDrillRuntime, request: dict) -> tuple[dict, int]:
    validate_fixture_files(runtime, request)
    containers = {name: runtime.inspect(name) for name in sorted(ops.ALL)}
    ids = {name: data["Id"] for name, data in containers.items()}
    ops.require(len(set(ids.values())) == len(ops.ALL), "UNSAFE_APPLY_DRILL_TOPOLOGY")
    project_ids = ops.command([
        "docker", "ps", "--all", "--quiet", "--no-trunc", "--filter",
        "label=com.docker.compose.project=" + runtime.project,
    ]).splitlines()
    ops.require(len(project_ids) == len(ids) and set(project_ids) == set(ids.values()),
                "UNSAFE_APPLY_DRILL_TOPOLOGY")
    network_name = runtime.project + "_default"
    publish_network_name = runtime.project + "_host_publish"
    networks: dict[str, str] = {}
    for name, data in containers.items():
        labels = data["Config"].get("Labels") or {}
        host = data["HostConfig"]
        expected_networks = {network_name, publish_network_name} if name == "postgres" else {network_name}
        ops.require(
            labels.get("com.docker.compose.project") == runtime.project
            and labels.get("com.docker.compose.service") == name
            and data["State"].get("Running") is True
            and host.get("NetworkMode") in expected_networks
            and all(host.get(key) in (None, "") for key in ("PidMode", "UTSMode", "UsernsMode"))
            and host.get("IpcMode") in (None, "private")
            and not any(host.get(key) for key in (
                "Privileged", "VolumesFrom", "Links", "ExtraHosts", "Devices", "CapAdd",
            ))
            and set(data["NetworkSettings"]["Networks"]) == expected_networks,
            "UNSAFE_APPLY_DRILL_TOPOLOGY",
        )
        for attached_name, endpoint in data["NetworkSettings"]["Networks"].items():
            identifier = endpoint["NetworkID"]
            ops.require(attached_name not in networks or networks[attached_name] == identifier,
                        "UNSAFE_APPLY_DRILL_TOPOLOGY")
            networks[attached_name] = identifier
        expected_mounts = {
            "postgres": {runtime.project + "_postgres_data"},
            "redis": {runtime.project + "_redis_data"},
            "publication_worker": {runtime.project + "_vk_publishing_credentials"},
        }.get(name, set())
        ops.require(
            # Compose may serialize safe named volumes in HostConfig.Binds.
            # Inspect the actual mount type and exact generated volume name.
            all(mount["Type"] == "volume" for mount in data["Mounts"])
            and {mount.get("Name") for mount in data["Mounts"]} == expected_mounts,
            "UNSAFE_APPLY_DRILL_TOPOLOGY",
        )
        if name in ops.APP:
            component = "frontend" if name == "frontend" else "backend"
            ops.require(data["Config"]["Image"] == request["source_images"][component],
                        "UNSAFE_APPLY_DRILL_TOPOLOGY")
            if name != "frontend":
                raw_env = data["Config"].get("Env") or []
                ops.require(all(type(item) is str and "=" in item for item in raw_env),
                            "UNSAFE_APPLY_DRILL_ENVIRONMENT")
                env = dict(item.split("=", 1) for item in raw_env)
                ops.require(
                    len(env) == len(raw_env) and env.get("APP_ENV") == "development"
                    and env.get("BUILD_SHA") == request["source_release_sha"]
                    and env.get("DATABASE_URL") == "postgresql+asyncpg://iteam:drill-only@postgres:5432/" + rehearsal.DATABASE_NAME
                    and env.get("REDIS_URL") == "redis://redis:6379/15"
                    and all(env.get(key) == "false" for key in PROVIDER_FLAGS)
                    and all(env.get(key) == "" for key in PROVIDER_CREDENTIALS),
                    "UNSAFE_APPLY_DRILL_ENVIRONMENT",
                )
        if name != "postgres":
            ops.require(not host.get("PortBindings") and not any(
                (data["NetworkSettings"].get("Ports") or {}).values()
            ), "UNSAFE_APPLY_DRILL_PORT")
    ops.require(set(networks) == {network_name, publish_network_name}
                and len(set(networks.values())) == 2, "UNSAFE_APPLY_DRILL_TOPOLOGY")
    for key, internal, members in (
        ("default", True, set(ids.values())), ("host_publish", False, {ids["postgres"]}),
    ):
        expected_name = runtime.project + "_" + key
        network = json.loads(ops.command(["docker", "network", "inspect", networks[expected_name]]))[0]
        labels = network.get("Labels") or {}
        ops.require(
            network.get("Name") == expected_name
            and network.get("Internal") is internal and network.get("Driver") == "bridge"
            and network.get("Scope") == "local"
            and labels.get("com.docker.compose.project") == runtime.project
            and labels.get("com.docker.compose.network") == key
            and set(network.get("Containers") or {}) == members,
            "UNSAFE_APPLY_DRILL_TOPOLOGY",
        )
    postgres = containers["postgres"]
    configured = postgres["HostConfig"].get("PortBindings")
    live = postgres["NetworkSettings"].get("Ports")
    for bindings in (configured, live):
        ops.require(
            type(bindings) is dict and set(bindings) == {"5432/tcp"}
            and type(bindings["5432/tcp"]) is list and len(bindings["5432/tcp"]) == 1
            and set(bindings["5432/tcp"][0]) == {"HostIp", "HostPort"}
            and bindings["5432/tcp"][0]["HostIp"] == "127.0.0.1",
            "UNSAFE_APPLY_DRILL_PORT",
        )
    raw_port = live["5432/tcp"][0]["HostPort"]
    ops.require(type(raw_port) is str and raw_port.isascii() and raw_port.isdigit()
                and 1 <= int(raw_port) <= 65535, "UNSAFE_APPLY_DRILL_PORT")
    port = int(raw_port)
    ops.require(_connects(port), "APPLY_DRILL_HOST_PORT_UNREACHABLE")
    return ids, port


class ReadinessFailureRuntime(MigrationRuntime):
    readiness_checked = False

    def maintenance_readiness(self) -> None:
        super().maintenance_readiness()
        self.readiness_checked = True
        raise ops.ReleaseError("INJECTED_FAILURE")


def verify_held_boundary(runtime, adapter, ids: dict, port: int) -> None:
    evidence = adapter.barrier()
    ops.require(all(evidence.get(key) is True for key in (
        "writers_stopped", "database_fenced", "barrier_continuous",
    )), "APPLY_DRILL_WRITER_BOUNDARY_OPEN")
    for name in (*ops.APP, "postgres"):
        current = runtime.inspect(name)
        ops.require(
            current["Id"] == ids[name]
            and current["HostConfig"]["RestartPolicy"]["Name"] == "no"
            and current["State"].get("Running") is (name == "postgres"),
            "APPLY_DRILL_WRITER_BOUNDARY_OPEN",
        )
        if name == "postgres":
            ops.require(not current["NetworkSettings"]["Networks"],
                        "APPLY_DRILL_WRITER_BOUNDARY_OPEN")
    ops.require(not _connects(port), "APPLY_DRILL_HOST_PORT_NOT_FENCED")


def _scenario(request: dict, name: str, progress=None) -> dict:
    phase = name + "_fixture"

    def mark(next_step: str) -> None:
        nonlocal phase
        next_phase = name + "_" + next_step
        report_phase(progress, next_phase)
        phase = next_phase

    adapter = None
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as directory:
        root = Path(directory).resolve()
        mark("fixture")
        runtime = create_fixture(root, "iteam-migration-drill-" + secrets.token_hex(5), request)
        operation = secrets.token_hex(16)
        try:
            mark("infrastructure")
            runtime.compose("up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis")
            mark("source_migrate")
            runtime.run_backend("alembic", "upgrade", request["source_schema_head"])
            ops.require(runtime.schema() == request["source_schema_head"], "APPLY_DRILL_SOURCE_STATE_MISMATCH")
            mark("seed")
            rehearsal.seed_fixture(runtime)
            mark("source_ready")
            runtime.compose("up", "-d", "--no-build", *ops.APP)
            runtime.ready()
            runtime.frontend_ready()
            runtime.workers_ready()
            mark("source_topology")
            ids, port = verify_live_fixture(runtime, request)
            mark("source_data")
            source = json.loads(runtime.compose("exec", "-T", "backend", "python", "-c", backup.SNAPSHOT_CODE))
            backup.validate_snapshot(source)
            ops.require(source["schema"] == request["source_schema_head"]
                        and source["build_sha"] == request["source_release_sha"],
                        "APPLY_DRILL_SOURCE_STATE_MISMATCH")
            require_business_fixture(source["business_state"]["table_counts"])
            manifest, approval, context = fixture_inputs(request, name, operation)
            state = root / "state"
            with ops.locked_state(state):
                ops.save(state / "disposable-fixture-authorization.json", context)
                journal = Journal(state, operation, manifest, approval)
                adapter_type = ReadinessFailureRuntime if name == "recovery" else MigrationRuntime
                adapter = adapter_type(runtime, manifest, operation, state, root / "backups", root / "keys")
                mark("apply")
                try:
                    outcome = engine.execute(adapter, journal, progress=mark)
                    ops.require(name == "success", "APPLY_DRILL_OUTCOME_MISMATCH")
                except engine.ApplyFailure as error:
                    outcome = error.result
                    if not (
                        name == "recovery" and adapter.readiness_checked
                        and outcome["status"] == "ROLLED_BACK_PRE_ACTIVATION"
                        and outcome["error_code"] == "INJECTED_FAILURE"
                        and outcome["recovery_error_code"] is None
                    ):
                        # Keep the engine's closed primary/recovery error codes;
                        # a secondary outcome assertion would hide the cause.
                        failed = [event for event in journal.data["history"] if event["kind"] == "failed"]
                        if failed and failed[0]["action"] in ACTION_PHASES:
                            error._apply_drill_failure_phase = name + "_" + failed[0]["action"]
                        raise
                expected_status = "PRE_ACTIVATION_READY" if name == "success" else "ROLLED_BACK_PRE_ACTIVATION"
                expected_schema = request["target_schema_head"] if name == "success" else request["source_schema_head"]
                ops.require(outcome["status"] == expected_status
                            and outcome["writer_boundary"] == "PROVEN_CLOSED"
                            and outcome["automatic_retry"] is False,
                            "APPLY_DRILL_OUTCOME_MISMATCH")
                data = journal.data
                ops.require(data["status"] == expected_status
                            and data["writer_activation_requested"] is False
                            and not journal.has_pending and journal.marker_path.is_file(),
                            "APPLY_DRILL_JOURNAL_MISMATCH")
                backups = [event["evidence"] for event in data["history"]
                           if event["kind"] == "complete" and event["phase"] == "BACKUP_VERIFIED"]
                ops.require(len(backups) == 1 and backups[0]["checks"] == {
                    "backup_verified": True, "restored_data_verified": True,
                }, "APPLY_DRILL_JOURNAL_MISMATCH")
                mark("held_boundary")
                final = adapter.snapshot(sorted(source["business_state"]["table_counts"]))
                backup.validate_snapshot(final)
                ops.require(
                    final["schema"] == expected_schema
                    and final["database_identity_sha256"] == source["database_identity_sha256"]
                    and final["business_state"] == source["business_state"]
                    and backups[0]["source_business_sha256"] == source["business_state"]["sha256"]
                    and backups[0]["restored_business_sha256"] == source["business_state"]["sha256"],
                    "APPLY_DRILL_FINAL_STATE_MISMATCH",
                )
                verify_held_boundary(runtime, adapter, ids, port)
                result = {
                    "status": expected_status, "schema": expected_schema,
                    "writer_boundary": "PROVEN_CLOSED", "writers_activated": False,
                    "host_postgres_port_reachable_before": True,
                    "host_postgres_port_blocked_after": True,
                    "postgres_networks_remaining": 0,
                    "stopped_application_services": len(ops.APP),
                    "restart_disabled_services": len(ops.APP) + 1,
                    "source_business_sha256": source["business_state"]["sha256"],
                    "final_business_sha256": final["business_state"]["sha256"],
                    "source_table_counts": source["business_state"]["table_counts"],
                    "encrypted_backup_sha256": backups[0]["encrypted_backup_sha256"],
                    "encrypted_backup_isolated_restore": True,
                    "database_identity_unchanged": True, "barrier_continuous": True,
                    "automatic_retry": False,
                }
                if name == "recovery":
                    result.update(injected_failure="INJECTED_FAILURE", injection_after_readiness=True)
                # The live observation ends while the apply lock is still held.
                # A subsequent reader must describe records without reviving it.
                adapter.close()
                adapter = None
            mark("record_inspection")
            report = inspection.inspect_operation(state, operation)
            ops.require(
                report["record_state"] == "CONSISTENT_TERMINAL"
                and report["recorded_status"] == expected_status
                and report["error_code"] is None
                and report["writer_boundary"] == "UNKNOWN"
                and all(report[key] is False for key in (
                    "live_runtime_checked", "historical_continuity_verified",
                    "receipt_signature_verified", "automatic_retry",
                    "activation_authorized", "schema_downgrade_allowed",
                )),
                "APPLY_DRILL_RECORD_INSPECTION_FAILED",
            )
            result["post_close_record_inspection"] = report
        except Exception as error:
            if not hasattr(error, "_apply_drill_failure_phase"):
                error._apply_drill_failure_phase = phase
            raise
        finally:
            mark("cleanup")
            try:
                if adapter is not None:
                    adapter.close()
                for resource in ("container", "volume", "network"):
                    flags = ["--all"] if resource == "container" else []
                    remaining = ops.command([
                        "docker", resource, "ls", *flags, "--quiet", "--filter",
                        "label=iteam.migration.operation=" + operation,
                    ])
                    ops.require(not remaining, "APPLY_DRILL_HELPER_CLEANUP_INCOMPLETE")
            finally:
                rehearsal.remove_disposable_project(runtime)
            mark("temporary_cleanup")
    ops.require(not root.exists(), "APPLY_DRILL_TEMPORARY_CLEANUP_INCOMPLETE")
    result.update(disposable_project_removed=True, temporary_backup_and_key_removed=True,
                  maintenance_helpers_removed=True)
    return result


def _publish(output: Path, proof: dict) -> None:
    # Complete bytes become visible atomically; a concurrent file is never replaced.
    fd, temporary = tempfile.mkstemp(dir=output.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_bytes(proof))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, output, follow_symlinks=False)
        except FileExistsError:
            raise ops.ReleaseError("APPLY_DRILL_OUTPUT_EXISTS") from None
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.unlink(temporary)


def run_drill(request: dict, output: Path, cosign: str, *, progress=None) -> dict:
    report_phase(progress, "preflight")
    rehearsal.validate_rehearsal_request(request)
    rehearsal.require_clean_compose_environment()
    ops.require(not output.exists() and not output.is_symlink(), "APPLY_DRILL_OUTPUT_EXISTS")
    ops.require(request["pre_activation_rollback_policy"] == "PRE_ACTIVATION_DOWNGRADE",
                "APPLY_DRILL_DOWNGRADE_POLICY_REQUIRED")
    ops.require(int(ops.command(["docker", "info", "--format", "{{.MemTotal}}"])) >= 6 * 1024**3,
                "APPLY_DRILL_REQUIRES_6_GIB")
    report_phase(progress, "image_verification")
    rehearsal.verify_images(request, cosign, lambda phase: report_phase(progress, phase))
    success = _scenario(request, "success", progress)
    recovery = _scenario(request, "recovery", progress)
    result = {
        "status": "PASS", "proof_contract_version": 2, "proof_type": PROOF_TYPE,
        "authorization": AUTHORIZATION, "production_approval_verified": False,
        "repository": ops.REPO, **rehearsal.rehearsal_scope(request),
        "compatibility_class": request["compatibility_class"],
        "pre_activation_rollback_policy": request["pre_activation_rollback_policy"],
        "success": success, "recovery": recovery,
        "writers_activated": False, "production_credentials": False,
        "publishing_enabled": False, "application_external_network": False,
        "postgres_has_host_publish_network": True, "target_maintenance_external_network": False,
        "disposable_services_per_scenario": len(ops.ALL), "scenario_count": 2,
        "disposable_projects_removed": True, "temporary_backups_and_keys_removed": True,
        "maintenance_helpers_removed": True, "created_at": datetime.now(UTC).isoformat(),
    }
    report_phase(progress, "publish_proof")
    _publish(output, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cosign", default="cosign")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    os.umask(0o077)
    phase = "request_load"

    def progress(value: str) -> None:
        nonlocal phase
        ops.require(value in _PHASES, "INVALID_APPLY_DRILL_PHASE")
        phase = value
        print(json.dumps({"status": "RUNNING", "phase": value}), file=sys.stderr, flush=True)

    try:
        result = run_drill(ops.load(args.request), args.output, args.cosign, progress=progress)
        print(json.dumps(result, sort_keys=True))
    except (ManifestContractError, ops.ReleaseError, OSError, ValueError, KeyError,
            TypeError, subprocess.SubprocessError) as error:
        failed_phase = getattr(error, "_apply_drill_failure_phase", phase)
        if failed_phase not in _PHASES:
            failed_phase = phase
        code = str(error) if isinstance(error, ops.ReleaseError) and str(error) in _SAFE_ERROR_CODES else rehearsal.diagnostic_error_code(error)
        result = {"status": "FAIL", "phase": failed_phase, "automatic_retry": False, "error_code": code}
        if isinstance(error, engine.ApplyFailure):
            outcome = error.result
            if outcome.get("status") in {"ROLLED_BACK_PRE_ACTIVATION", "OPERATOR_RECOVERY_REQUIRED"}:
                result["operation_status"] = outcome["status"]
            if outcome.get("recovery_error_code") in _SAFE_ERROR_CODES:
                result["recovery_error_code"] = outcome["recovery_error_code"]
        print(json.dumps(result))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
