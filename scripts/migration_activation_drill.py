#!/usr/bin/env python3
"""Rehearse signed migration, passive admission and activation on owned fixtures.

The controller under test is the production implementation. Its authority here
is explicitly disposable; no production receipt is issued or authenticated.
No production checkout, namespace, database, environment or credential override
is accepted by the CLI. All application networks remain internal; only the
synthetic PostgreSQL also has a host-publishing bridge to exercise real fencing.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import migration_activation_contract as contract
import migration_apply_backup as backup
import migration_apply_drill as apply_drill
import migration_release_drill as rehearsal
import release_ops as ops
import runtime_admission_drill as admission
from migration_apply_runtime import MigrationRuntime
from migration_apply_state import ACTION_PHASES, Journal, manifest_hash
from release_manifest import ManifestContractError

TEMP_PREFIX = "iteam-migration-activation-drill-"
PROJECT_PREFIX = "iteam-activation-drill-"
SCHEMA = "20261121_0030"
CANARY_NAME = "Signed activation committed canary"
RUN_ID = "00000000-0000-4000-8000-000000000c05"
TASK_ID = "00000000-0000-4000-8000-000000000c04"
JOB_ID = "00000000-0000-4000-8000-000000000c06"
_CONTROLLER_PHASES = frozenset(ACTION_PHASES) | frozenset({
    "activation_passive_postflight", "activation_handoff", "activation_recreate",
    "activation_reconnect", "activation_start", "activation_postflight",
    "activation_finalize", "activation_close",
})
_PHASES = frozenset({
    "request_load", "preflight", "migration_rehearsal", "adversarial_passive",
    "success_fixture", "success_source", "success_controller", "success_cleanup",
    "failure_fixture", "failure_source", "failure_controller", "failure_cleanup",
    "proof_validation", "proof_publication", "unknown",
}) | frozenset("migration_" + phase for phase in rehearsal._DIAGNOSTIC_PHASES) | frozenset(
    scenario + "_" + phase for scenario in ("success", "failure") for phase in _CONTROLLER_PHASES
)
ERROR_CODES = frozenset({
    "ACTIVATION_DRILL_ARGUMENTS_INVALID", "ACTIVATION_DRILL_OUTPUT_UNSAFE",
    "ACTIVATION_DRILL_CHECKOUT_UNSAFE", "ACTIVATION_DRILL_PHASE_INVALID",
    "ACTIVATION_DRILL_UNSAFE_DIRECTORY", "ACTIVATION_DRILL_UNSAFE_PROJECT",
    "ACTIVATION_DRILL_UNSAFE_TOPOLOGY", "ACTIVATION_DRILL_IMAGE_MISMATCH",
    "ACTIVATION_DRILL_PASSIVE_MISMATCH", "ACTIVATION_DRILL_PREACTIVATION_MISMATCH",
    "ACTIVATION_DRILL_ACTIVE_MISMATCH", "ACTIVATION_DRILL_WRITE_NOT_PROVED",
    "ACTIVATION_DRILL_CLAIM_NOT_PROVED", "ACTIVATION_DRILL_CONTAINMENT_MISMATCH",
    "ACTIVATION_DRILL_DATA_CHANGED", "ACTIVATION_DRILL_CLEANUP_FAILED",
    "ACTIVATION_DRILL_FAILED", "INJECTED_POST_ACTIVATION_FAILURE",
    "ACTIVATION_DRILL_REQUIRES_6_GIB",
})


def _phase(value: str, progress=None) -> None:
    ops.require(value in _PHASES, "ACTIVATION_DRILL_PHASE_INVALID")
    if progress is not None:
        progress(value)


def _private_root(root: Path, *, empty: bool = False) -> None:
    ops.require(
        root.is_absolute() and root.resolve() == root and not root.is_symlink()
        and re.fullmatch(re.escape(TEMP_PREFIX) + r"[a-z0-9_-]{8}", root.name),
        "ACTIVATION_DRILL_UNSAFE_DIRECTORY",
    )
    value = root.stat()
    ops.require(stat.S_ISDIR(value.st_mode) and value.st_uid == os.getuid()
                and stat.S_IMODE(value.st_mode) == 0o700
                and (not empty or not any(root.iterdir())),
                "ACTIVATION_DRILL_UNSAFE_DIRECTORY")


def checkout_identity(root: Path) -> dict[str, str]:
    """Bind the controller code to the actual clean tracked CI checkout."""
    sha = ops.command(["git", "rev-parse", "HEAD"], cwd=root)
    tree = ops.command(["git", "rev-parse", "HEAD^{tree}"], cwd=root)
    ops.require(ops.SHA.fullmatch(sha) and ops.SHA.fullmatch(tree)
                and not ops.command(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root),
                "ACTIVATION_DRILL_CHECKOUT_UNSAFE")
    for name in ("migration_activation_drill.py", "migration_activation_apply.py",
                 "migration_activation_runtime.py", "migration_activation_journal.py",
                 "migration_activation_contract.py"):
        ops.command(["git", "ls-files", "--error-unmatch", "scripts/" + name], cwd=root)
    return {"controller_sha": sha, "controller_tree": tree}


class PassiveMigrationRuntime(rehearsal.MigrationDrillRuntime):
    """The v1 migration-only rehearsal never opens the gate-capable target."""

    def __init__(self, root: Path, *, project: str):
        super().__init__(root, project=project)
        self.target_passive: dict | None = None

    def app_ready(self, request: dict, target: bool) -> None:
        mode = "passive" if target else "active"
        self.env_path.write_text(ops.set_env(self.env_path.read_text(), {"RUNTIME_ADMISSION_MODE": mode}))
        self.env_path.chmod(0o600)
        # A cold process selects admission. A restart of a previous process is
        # not evidence that its immutable admission setting changed.
        self.compose("up", "-d", "--no-build", "--force-recreate", "backend", "frontend")
        super().app_ready(request, target)
        if target:
            payload = json.loads(self.compose(
                "exec", "-T", "backend", "python", "-c",
                "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=5).read().decode())",
            ))
            admission.validate_health(payload, mode="passive", sha=request["target_release_sha"], ready=True)
            self.target_passive = {
                "admission_protocol": 1, "admission_mode": "passive", "business_ready": False,
                "database_read_only": True, "build_sha": request["target_release_sha"],
                "before_downgrade": True,
            }


def migration_rehearsal(request: dict, *, cosign: str, progress=None) -> tuple[dict, dict]:
    instances: list[PassiveMigrationRuntime] = []

    def factory(root: Path, project: str, migration: dict) -> PassiveMigrationRuntime:
        # The original factory owns creation and validation of the four-service
        # migration fixture. The subclass changes only target admission.
        rehearsal.create_drill(root, project, migration)
        instance = PassiveMigrationRuntime(root, project=project)
        instances.append(instance)
        return instance

    with tempfile.TemporaryDirectory(prefix="iteam-activation-rehearsal-proof-") as temporary:
        root = Path(temporary).resolve()
        proof = rehearsal.rehearse(
            request, root / "migration-proof.json", cosign, runtime_factory=factory,
            progress=lambda phase: _phase("migration_" + phase, progress),
        )
        ops.require(len(instances) == 1 and instances[0].target_passive is not None,
                    "ACTIVATION_DRILL_PASSIVE_MISMATCH")
        passive = copy.deepcopy(instances[0].target_passive)
    ops.require(not root.exists(), "ACTIVATION_DRILL_CLEANUP_FAILED")
    return proof, passive


def target_images(request: dict) -> dict[str, str]:
    """Resolve already verified registry refs to the actual local platform IDs."""
    result = {}
    for component, reference in request["target_images"].items():
        values = json.loads(ops.command(["docker", "image", "inspect", reference]))
        ops.require(type(values) is list and len(values) == 1, "ACTIVATION_DRILL_IMAGE_MISMATCH")
        value = values[0]
        ops.require(ops.DIGEST.fullmatch(value.get("Id", ""))
                    and (value.get("Config", {}).get("Labels") or {}).get("org.opencontainers.image.revision")
                    == request["target_release_sha"], "ACTIVATION_DRILL_IMAGE_MISMATCH")
        if component == "backend":
            ops.require("BUILD_SHA=" + request["target_release_sha"] in value["Config"].get("Env", []),
                        "ACTIVATION_DRILL_IMAGE_MISMATCH")
        result[component] = value["Id"]
    return result


def adversarial_passive(request: dict, images: dict[str, str]) -> dict:
    """Run all seven queued canaries against the verified signed target itself."""
    commands = admission.Commands()
    infra = {}
    for service, reference in (("postgres", "postgres:17-alpine"), ("redis", "redis:8-alpine")):
        commands.run(["docker", "pull", reference], timeout=180)
        value = json.loads(commands.run(["docker", "image", "inspect", reference]).stdout)[0]
        ops.require(ops.DIGEST.fullmatch(value.get("Id", "")), "ACTIVATION_DRILL_IMAGE_MISMATCH")
        infra[service] = value["Id"]
    metadata = {"candidate_sha": request["target_release_sha"], "candidate_image_id": images["backend"]}
    failure = None
    passive = None
    with tempfile.TemporaryDirectory(prefix=admission.TEMP_PREFIX) as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        runtime = admission.Runtime(commands, metadata, root, infra)
        try:
            runtime.start()
            passive, _ = admission.passive_checks(runtime)
        except BaseException as error:
            failure = error
        cleaned = runtime.cleanup()
        ops.require(cleaned, "ACTIVATION_DRILL_CLEANUP_FAILED")
        if failure is not None:
            raise failure
    ops.require(not root.exists() and passive is not None, "ACTIVATION_DRILL_CLEANUP_FAILED")
    return {"target_image_id": images["backend"], "candidate_sha": request["target_release_sha"],
            "passive": passive, "cleanup_verified": True}


class ActivationDrillRuntime(rehearsal.MigrationDrillRuntime):
    """Exact new fixture namespace; old migration fixture guards stay unchanged."""

    def __init__(self, root: Path, *, project: str):
        _private_root(root)
        ops.require(re.fullmatch(re.escape(PROJECT_PREFIX) + r"[0-9a-f]{10}", project),
                    "ACTIVATION_DRILL_UNSAFE_PROJECT")
        rehearsal.require_clean_compose_environment()
        ops.Runtime.__init__(self, root, project=project)
        self.project = project
        self._select_files(target=False)


def create_fixture(root: Path, project: str, request: dict) -> ActivationDrillRuntime:
    """Own synthetic services, isolated application network and explicit PG port."""
    _private_root(root, empty=True)
    ops.require(re.fullmatch(re.escape(PROJECT_PREFIX) + r"[0-9a-f]{10}", project),
                "ACTIVATION_DRILL_UNSAFE_PROJECT")
    rehearsal.validate_rehearsal_request(request)
    env = {
        "APP_ENV": "development", "RUNTIME_ADMISSION_MODE": "active",
        "BUILD_SHA": request["source_release_sha"],
        "DATABASE_URL": "postgresql+asyncpg://iteam:drill-only@postgres:5432/" + rehearsal.DATABASE_NAME,
        "REDIS_URL": "redis://redis:6379/15", "JWT_SECRET": secrets.token_urlsafe(40),
        "APP_SECRET": secrets.token_urlsafe(40), "ADMIN_EMAIL": "migration-admin@example.com",
        "ADMIN_PASSWORD": secrets.token_urlsafe(40), "OPENAI_AGENTS_DISABLE_TRACING": "true",
        "TASK_DISPATCH_INTERVAL_SECONDS": "3600", "OPTIMIZATION_ANALYSIS_SCAN_INTERVAL_SECONDS": "3600",
        "METRICS_SYNC_INTERVAL_SECONDS": "3600",
        **{key: "false" for key in apply_drill.PROVIDER_FLAGS},
        **{key: "" for key in apply_drill.PROVIDER_CREDENTIALS},
    }
    (root / ".env").write_text("".join(key + "=" + value + "\n" for key, value in sorted(env.items())))
    (root / ".env").chmod(0o600)
    services = {
        "postgres": {
            "image": "postgres:17-alpine", "restart": "unless-stopped",
            "environment": {"POSTGRES_USER": "iteam", "POSTGRES_PASSWORD": "drill-only",
                            "POSTGRES_DB": rehearsal.DATABASE_NAME},
            "volumes": ["postgres_data:/var/lib/postgresql/data"],
            "ports": ["127.0.0.1::5432"], "networks": ["default", "host_publish"],
            "healthcheck": {"test": ["CMD", "pg_isready", "-U", "iteam", "-d", rehearsal.DATABASE_NAME],
                            "interval": "1s", "timeout": "3s", "retries": 60},
        },
        "redis": {"image": "redis:8-alpine", "volumes": ["redis_data:/data"],
                  "healthcheck": {"test": ["CMD", "redis-cli", "ping"], "interval": "1s",
                                  "timeout": "3s", "retries": 60}},
        "backend": {"env_file": ".env", "restart": "unless-stopped", "stop_grace_period": "210s",
                    "mem_limit": "512m", "cpus": 1.0,
                    "depends_on": {"postgres": {"condition": "service_healthy"},
                                   "redis": {"condition": "service_healthy"}}},
        "frontend": {"restart": "unless-stopped", "stop_grace_period": "210s", "mem_limit": "256m",
                     "cpus": 0.5, "depends_on": {"backend": {"condition": "service_started"}}},
    }
    for name in ops.WORKERS:
        services[name] = apply_drill._worker_service(name, request["source_images"]["backend"])
    ops.save(root / "compose.json", {
        "services": services,
        "volumes": {"postgres_data": {}, "redis_data": {}, "vk_publishing_credentials": {}},
        "networks": {"default": {"internal": True}, "host_publish": {"driver": "bridge", "internal": False}},
    })
    for side in ("source", "target"):
        ops.save(root / (side + "-images.json"), {
            "services": {name: {"image": request[side + "_images"][name]} for name in ("backend", "frontend")},
        })
    return ActivationDrillRuntime(root, project=project)


def verify_source_fixture(runtime: ActivationDrillRuntime, request: dict) -> tuple[dict[str, str], int]:
    _private_root(runtime.root)
    containers = {name: runtime.inspect(name) for name in sorted(ops.ALL)}
    ids = {name: value["Id"] for name, value in containers.items()}
    observed = ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter",
                            "label=com.docker.compose.project=" + runtime.project]).splitlines()
    ops.require(len(set(ids.values())) == len(ops.ALL) and set(observed) == set(ids.values()),
                "ACTIVATION_DRILL_UNSAFE_TOPOLOGY")
    for name, value in containers.items():
        labels = value["Config"].get("Labels") or {}
        expected_networks = {runtime.project + "_default"}
        if name == "postgres":
            expected_networks.add(runtime.project + "_host_publish")
        ops.require(labels.get("com.docker.compose.project") == runtime.project
                    and labels.get("com.docker.compose.service") == name
                    and value["State"].get("Running") is True
                    and value["HostConfig"].get("Privileged") is False
                    and set(value["NetworkSettings"].get("Networks", {})) == expected_networks,
                    "ACTIVATION_DRILL_UNSAFE_TOPOLOGY")
        mounts = {"postgres": {runtime.project + "_postgres_data"},
                  "redis": {runtime.project + "_redis_data"},
                  "publication_worker": {runtime.project + "_vk_publishing_credentials"}}.get(name, set())
        ops.require(all(item["Type"] == "volume" for item in value["Mounts"])
                    and {item["Name"] for item in value["Mounts"]} == mounts,
                    "ACTIVATION_DRILL_UNSAFE_TOPOLOGY")
        if name != "postgres":
            ops.require(not value["HostConfig"].get("PortBindings")
                        and not any((value["NetworkSettings"].get("Ports") or {}).values()),
                        "ACTIVATION_DRILL_UNSAFE_TOPOLOGY")
        if name in ops.APP:
            component = "frontend" if name == "frontend" else "backend"
            ops.require(value["Config"]["Image"] == request["source_images"][component],
                        "ACTIVATION_DRILL_IMAGE_MISMATCH")
    for name, internal, members in (("default", True, set(ids.values())), ("host_publish", False, {ids["postgres"]})):
        value = json.loads(ops.command(["docker", "network", "inspect", runtime.project + "_" + name]))[0]
        ops.require(value.get("Internal") is internal and value.get("Driver") == "bridge"
                    and set(value.get("Containers", {})) == members,
                    "ACTIVATION_DRILL_UNSAFE_TOPOLOGY")
    bindings = containers["postgres"]["NetworkSettings"]["Ports"]
    ops.require(set(bindings) == {"5432/tcp"} and len(bindings["5432/tcp"]) == 1
                and bindings["5432/tcp"][0].get("HostIp") == "127.0.0.1",
                "ACTIVATION_DRILL_UNSAFE_TOPOLOGY")
    port = int(bindings["5432/tcp"][0]["HostPort"])
    ops.require(1 <= port <= 65535 and apply_drill._connects(port), "ACTIVATION_DRILL_UNSAFE_TOPOLOGY")
    return ids, port


HTTP_CODE = r'''
import asyncio,json,sys,urllib.request,urllib.error
from sqlalchemy import text
from app.core.config import settings
from app.core.database import async_session_factory,engine
from app.core.security import encode_access_token
async def identity():
 async with async_session_factory() as s:
  row=(await s.execute(text("SELECT id,auth_version FROM users WHERE email=:email AND is_active AND role::text='ADMIN'"),{'email':settings.admin_email})).one()
 await engine.dispose()
 return row
user_id,version=asyncio.run(identity())
request=json.loads(sys.argv[1])
headers={'Cookie':settings.auth_cookie_name+'='+encode_access_token(user_id,version),'Content-Type':'application/json'}
body=None if 'body' not in request else json.dumps(request['body']).encode()
req=urllib.request.Request('http://127.0.0.1:8000'+request['path'],data=body,headers=headers,method=request.get('method','GET'))
try:
 with urllib.request.urlopen(req,timeout=8) as response: code=response.status;data=json.load(response)
except urllib.error.HTTPError as response: code=response.code;data=json.load(response)
print(json.dumps({'http_status':code,'body':data},sort_keys=True))
'''

ENQUEUE_CODE = r'''
import asyncio,hashlib,json,sys
from uuid import UUID
from sqlalchemy import select,text
from app.core.database import async_session_factory,engine
from app.models import Agent,AgentRun,AgentRunStatus,Task,TaskStatus,TaskType,TaskPriority
request=json.loads(sys.argv[1])
async def main():
 async with async_session_factory() as s:
  assert await s.scalar(text('SELECT current_database()'))=='iteam_migration_drill'
  assert await s.scalar(text('SELECT version_num FROM alembic_version'))=='20261121_0030'
  writer=(await s.execute(select(Agent).where(Agent.slug=='writer'))).scalar_one()
  s.add(Task(id=UUID('00000000-0000-4000-8000-000000000c04'),campaign_id=UUID(request['campaign_id']),task_type=TaskType.MANUAL,title='Signed activation claim canary',assigned_agent_id=writer.id,priority=TaskPriority.NORMAL,status=TaskStatus.READY,input_data={},output_data={}))
  await s.flush()
  s.add(AgentRun(id=UUID('00000000-0000-4000-8000-000000000c05'),agent_id=writer.id,task_id=UUID('00000000-0000-4000-8000-000000000c04'),campaign_id=UUID(request['campaign_id']),status=AgentRunStatus.QUEUED,input_data={'text':'Synthetic activation fixture','allowed_knowledge_pack_ids':[],'allowed_content_version_ids':[]},model='gpt-4.1-mini',prompt_snapshot='Synthetic fixture.',prompt_hash=hashlib.sha256(b'Synthetic fixture.').hexdigest(),queue_job_id=None))
  await s.commit()
 await engine.dispose()
asyncio.run(main())
from app.workers.celery_app import celery_app
celery_app.send_task('execute_agent_run',args=['00000000-0000-4000-8000-000000000c05'],queue='ai',task_id='00000000-0000-4000-8000-000000000c06')
print(json.dumps({'enqueued':True}))
'''

CANARY_CODE = r'''
import asyncio,json
from sqlalchemy import text
from app.core.database import async_session_factory,engine
async def main():
 async with async_session_factory() as s:
  row=(await s.execute(text("SELECT status::text,started_at IS NOT NULL,completed_at IS NOT NULL FROM agent_runs WHERE id='00000000-0000-4000-8000-000000000c05'"))).one()
  count=await s.scalar(text("SELECT count(*) FROM campaigns WHERE name='Signed activation committed canary'"))
  print(json.dumps({'status':row[0],'started':row[1],'finished':row[2],'count':count}))
 await engine.dispose()
asyncio.run(main())
'''


def _backend_json(adapter, code: str, value: dict | None = None) -> dict:
    identifier = adapter.target_ids["backend"]
    ops.require(re.fullmatch(r"[0-9a-f]{64}", identifier), "ACTIVATION_DRILL_ACTIVE_MISMATCH")
    args = ["docker", "exec", identifier, "python", "-c", code]
    if value is not None:
        args.append(json.dumps(value))
    raw = ops.command(args)
    result = json.loads(raw.splitlines()[-1])
    ops.require(type(result) is dict, "ACTIVATION_DRILL_ACTIVE_MISMATCH")
    return result


def verify_active_target(adapter, request: dict, images: dict, source_ids: dict) -> None:
    ops.require(set(adapter.target_ids) == set(ops.APP)
                and len(set(adapter.target_ids.values())) == len(ops.APP),
                "ACTIVATION_DRILL_ACTIVE_MISMATCH")
    for name, identifier in adapter.target_ids.items():
        value = json.loads(ops.command(["docker", "inspect", identifier]))[0]
        component = "frontend" if name == "frontend" else "backend"
        ops.require(identifier != source_ids[name] and value["Image"] == images[component]
                    and value["State"].get("Running") is True
                    and value["State"].get("Restarting") is False,
                    "ACTIVATION_DRILL_ACTIVE_MISMATCH")
        if name != "frontend":
            env = dict(item.split("=", 1) for item in value["Config"].get("Env", []))
            ops.require(env.get("BUILD_SHA") == request["target_release_sha"]
                        and env.get("RUNTIME_ADMISSION_MODE") == "active",
                        "ACTIVATION_DRILL_ACTIVE_MISMATCH")
    health = _backend_json(adapter, HTTP_CODE, {"path": "/health/ready"})
    ops.require(health.get("http_status") == 200, "ACTIVATION_DRILL_ACTIVE_MISMATCH")
    admission.validate_health(health["body"], mode="active", sha=request["target_release_sha"], ready=True)


def committed_work(adapter) -> dict:
    created = _backend_json(adapter, HTTP_CODE, {
        "path": "/api/v1/campaigns", "method": "POST",
        "body": {"name": CANARY_NAME, "goal": "Prove signed activation preserves committed work"},
    })
    ops.require(created.get("http_status") == 201 and created.get("body", {}).get("name") == CANARY_NAME,
                "ACTIVATION_DRILL_WRITE_NOT_PROVED")
    seeded = _backend_json(adapter, ENQUEUE_CODE, {"campaign_id": created["body"]["id"]})
    ops.require(seeded == {"enqueued": True}, "ACTIVATION_DRILL_CLAIM_NOT_PROVED")
    until = time.monotonic() + 90
    while time.monotonic() < until:
        canary = _backend_json(adapter, CANARY_CODE)
        if (canary.get("started") is True and canary.get("finished") is True
                and canary.get("status") in {"COMPLETED", "FAILED"}):
            break
        time.sleep(1)
    else:
        raise ops.ReleaseError("ACTIVATION_DRILL_CLAIM_NOT_PROVED")
    ops.require(canary.get("count") == 1, "ACTIVATION_DRILL_WRITE_NOT_PROVED")
    # The run's terminal commit can precede its final task/log bookkeeping.
    # Observe a stable full database before deliberately triggering the fault.
    snapshot = adapter.full_snapshot()
    for _ in range(20):
        time.sleep(0.25)
        following = adapter.full_snapshot()
        if snapshot == following:
            ops.require(snapshot.get("schema") == SCHEMA and len(snapshot.get("table_counts", {})) == 37,
                        "ACTIVATION_DRILL_WRITE_NOT_PROVED")
            return snapshot
        snapshot = following
    raise ops.ReleaseError("ACTIVATION_DRILL_WRITE_NOT_PROVED")


class ObservedMigrationRuntime(MigrationRuntime):
    """Observe forbidden recovery calls without changing the successful path."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.activation_state = None
        self.post_activation_downgrade_attempts = 0
        self.post_activation_restore_attempts = 0

    def _after_boundary(self) -> bool:
        return (self.activation_state is not None
                and self.activation_state.summary()["activation_boundary_recorded"] is True)

    def downgrade_exact(self) -> None:
        if self._after_boundary():
            self.post_activation_downgrade_attempts += 1
            raise ops.ReleaseError("ACTIVATION_DRILL_CONTAINMENT_MISMATCH")
        super().downgrade_exact()

    def backup(self) -> dict:
        # This method includes an isolated pg_restore. No restoration of any
        # kind is part of the post-activation failure path under test.
        if self._after_boundary():
            self.post_activation_restore_attempts += 1
            raise ops.ReleaseError("ACTIVATION_DRILL_CONTAINMENT_MISMATCH")
        return super().backup()

    def restore_source_configuration(self) -> None:
        if self._after_boundary():
            self.post_activation_restore_attempts += 1
            raise ops.ReleaseError("ACTIVATION_DRILL_CONTAINMENT_MISMATCH")
        super().restore_source_configuration()


def fixture_inputs(request: dict, name: str, operation: str) -> tuple[dict, dict, dict]:
    """Shape-valid local authority; these IDs are never fetched or signed."""
    migration = contract.migration_request(request)
    ops.require(name in {"success", "failure"} and re.fullmatch(r"[0-9a-f]{32}", operation),
                "ACTIVATION_DRILL_ARGUMENTS_INVALID")
    synthetic_id = 1 if name == "success" else 2
    context = {
        "authorization": contract.FIXTURE_AUTHORITY, "production_approval_verified": False,
        "scenario": name, "operation_id": operation, "request_sha256": contract.hash_json(request),
        "synthetic_receipt": True,
    }
    manifest = {key: copy.deepcopy(value) for key, value in migration.items()
                if key not in {"version", "request_type"}}
    manifest.update(version=2, release_type="MIGRATION", rehearsal_proof={
        "workflow_run_id": synthetic_id, "artifact_id": synthetic_id,
        "proof_sha256": contract.hash_json(context), **rehearsal.rehearsal_scope(migration),
    })
    apply_drill.validate_migration_manifest(manifest)
    context["manifest_sha256"] = manifest_hash(manifest)
    approval = {
        "approval_run_id": synthetic_id, "artifact_id": synthetic_id,
        "receipt_sha256": contract.hash_json(context), "approved_at": datetime.now(UTC).isoformat(),
    }
    return manifest, approval, context


def preactivation_evidence(journal: Journal, initial: dict) -> None:
    data = journal.data
    ops.require(data["status"] == "PRE_ACTIVATION_READY" and data["phase"] == "MAINTENANCE_READY"
                and data["writer_activation_requested"] is False and not journal.has_pending
                and journal.marker_path.is_file(), "ACTIVATION_DRILL_PREACTIVATION_MISMATCH")
    started = [item["evidence"] for item in data["history"] if item["kind"] == "created"]
    maintenance = [item["evidence"] for item in data["history"]
                   if item["kind"] == "complete" and item["phase"] == "MAINTENANCE_READY"]
    backups = [item["evidence"] for item in data["history"]
               if item["kind"] == "complete" and item["phase"] == "BACKUP_VERIFIED"]
    ops.require(len(started) == len(maintenance) == len(backups) == 1,
                "ACTIVATION_DRILL_PREACTIVATION_MISMATCH")
    baseline = initial["business_state"]["sha256"]
    ops.require(started[0]["business_sha256"] == maintenance[0]["business_sha256"] == baseline
                and maintenance[0]["database_identity_sha256"] == initial["database_identity_sha256"]
                and maintenance[0]["checks"] == {
                    "writers_stopped": True, "database_fenced": True,
                    "maintenance_readiness": True, "cleanup_complete": True,
                }
                and backups[0]["source_business_sha256"] == backups[0]["restored_business_sha256"] == baseline,
                "ACTIVATION_DRILL_PREACTIVATION_MISMATCH")


def _helpers_removed(operation: str) -> None:
    for resource in ("container", "volume", "network"):
        flags = ["--all"] if resource == "container" else []
        remaining = ops.command([
            "docker", resource, "ls", *flags, "--quiet", "--filter",
            "label=iteam.migration.operation=" + operation,
        ])
        ops.require(not remaining, "ACTIVATION_DRILL_CLEANUP_FAILED")


def accept_expected_failure(name: str, observations: dict, error) -> None:
    """Only the committed-work fault with proven containment is expected."""
    outcome = error.result
    state = outcome.get("activation_state")
    if not (
        name == "failure" and "committed" in observations
        and outcome.get("error_code") == "INJECTED_POST_ACTIVATION_FAILURE"
        and outcome.get("status") == "CONTAINED" and outcome.get("recovery_error_code") is None
        and outcome.get("observer_cleanup_verified") is True
        and type(state) is dict and state.get("phase") == "CONTAINED"
        and state.get("state_durability") == "CONFIRMED"
    ):
        # Retain the controller's closed primary/recovery codes. Replacing them
        # with a fixture assertion would hide a real recreate/start failure.
        raise error


def scenario(request: dict, name: str, images: dict, *, progress=None) -> dict:
    """One lock, unchanged source quiet gates, and the real activation controller."""
    # Lazy imports keep CLI help and fixture-payload contract tests independent
    # of the executable production entry point.
    from migration_activation_apply import ActivationFailure, execute
    from migration_activation_journal import ActivationJournal
    from migration_activation_runtime import MigrationActivationRuntime

    migration = contract.migration_request(request)
    ops.require(name in {"success", "failure"}, "ACTIVATION_DRILL_ARGUMENTS_INVALID")
    phase = name + "_fixture"

    def mark(step: str) -> None:
        nonlocal phase
        phase = name + "_" + step
        _phase(phase, progress)

    adapter = None
    result = None
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        project = PROJECT_PREFIX + secrets.token_hex(5)
        operation = secrets.token_hex(16)
        mark("fixture")
        runtime = create_fixture(root, project, migration)
        try:
            mark("source")
            runtime.compose("up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis")
            runtime.run_backend("alembic", "upgrade", migration["source_schema_head"])
            ops.require(runtime.schema() == migration["source_schema_head"],
                        "ACTIVATION_DRILL_PREACTIVATION_MISMATCH")
            rehearsal.seed_fixture(runtime)
            runtime.compose("up", "-d", "--no-build", *ops.APP)
            runtime.ready()
            runtime.frontend_ready()
            runtime.workers_ready()
            ids, port = verify_source_fixture(runtime, migration)
            manifest, approval, context = fixture_inputs(request, name, operation)
            state = root / "state"
            with ops.locked_state(state):
                ops.save(state / "disposable-fixture-authorization.json", context)
                planner = MigrationRuntime(runtime, manifest, operation, state, root / "backups", root / "keys")
                try:
                    inventory = planner.inventory()
                    planner.quiet()
                finally:
                    planner.close()
                initial = inventory["snapshot"]
                backup.validate_snapshot(initial)
                rehearsal.require_business_fixture(initial["business_state"]["table_counts"])
                expected_hash = inventory["runtime_identity_sha256"]
                source = ObservedMigrationRuntime(runtime, manifest, operation, state, root / "backups", root / "keys")
                adapter = MigrationActivationRuntime(source, fixture=True)
                journal = Journal(state, operation, manifest, approval)
                binding = {
                    "authorization": contract.FIXTURE_AUTHORITY, "operation_id": operation, "project": project,
                    "manifest_sha256": manifest_hash(manifest), "receipt_sha256": approval["receipt_sha256"],
                    "approval_run_id": approval["approval_run_id"], "runtime_identity_sha256": expected_hash,
                    "target_release_sha": migration["target_release_sha"],
                    "target_schema_head": migration["target_schema_head"],
                    "activation_policy_sha256": contract.hash_json(contract.activation_policy()),
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
                activation = ActivationJournal(state, binding)
                source.activation_state = activation
                observations: dict[str, Any] = {}

                def after_active(current, evidence):
                    # This is the first point at which the fixture adds queued
                    # work. No canary invalidates old quiet/hash gates or the
                    # preserved business baseline before the durable marker.
                    summary = activation.summary()
                    ops.require(summary["activation_boundary_recorded"] is True
                                and summary["state_durability"] == "CONFIRMED",
                                "ACTIVATION_DRILL_ACTIVE_MISMATCH")
                    preactivation_evidence(journal, initial)
                    verify_active_target(current, migration, images, ids)
                    observations["committed"] = committed_work(current)
                    observations["target_services_verified"] = len(current.target_ids)
                    if name == "failure":
                        raise ops.ReleaseError("INJECTED_POST_ACTIVATION_FAILURE")

                mark("controller")
                try:
                    execute(adapter, journal, activation, expected_runtime_identity_sha256=expected_hash,
                            progress=mark, after_active=after_active)
                    ops.require(name == "success", "ACTIVATION_DRILL_CONTAINMENT_MISMATCH")
                except ActivationFailure as error:
                    accept_expected_failure(name, observations, error)
                summary = activation.summary()
                preactivation_evidence(journal, initial)
                ops.require(summary["phase"] == ("ACTIVE" if name == "success" else "CONTAINED")
                            and summary["activation_boundary_recorded"] is True
                            and summary["state_durability"] == "CONFIRMED"
                            and summary["automatic_retry"] is False
                            and observations.get("target_services_verified") == len(ops.APP)
                            and source.post_activation_downgrade_attempts == 0
                            and source.post_activation_restore_attempts == 0,
                            "ACTIVATION_DRILL_CONTAINMENT_MISMATCH")
                old_ids = set(ids[name] for name in ops.APP)
                existing = set(ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
                ops.require(not old_ids & existing, "ACTIVATION_DRILL_ACTIVE_MISMATCH")
                result = {
                    "status": "PASS", "operation_id": operation, "project": project,
                    "pre_activation_status": journal.data["status"],
                    "pre_activation_writer_boundary": "PROVEN_CLOSED",
                    "pre_activation_business_preserved": True,
                    "database_identity_sha256": initial["database_identity_sha256"],
                    "target_plan_sha256": summary["target_plan_sha256"],
                    "activation_phase": summary["phase"],
                    "activation_boundary_recorded": summary["activation_boundary_recorded"],
                    "state_durability": summary["state_durability"], "admission_protocol": 1,
                    "target_services_verified": observations["target_services_verified"],
                    "cold_recreation_verified": True, "source_runtime_restarted": False,
                    "automatic_retry": summary["automatic_retry"],
                    "post_activation_downgrade_attempts": source.post_activation_downgrade_attempts,
                    "post_activation_restore_attempts": source.post_activation_restore_attempts,
                }
                if name == "failure":
                    contained = adapter.full_snapshot()
                    evidence = activation.data["containment_evidence"]
                    committed = observations["committed"]
                    ops.require(contained == committed and len(contained["table_counts"]) == 37
                                and evidence["all_tables_sha256"] == committed["all_tables_sha256"]
                                and contained["database_identity_sha256"] == initial["database_identity_sha256"]
                                and all(evidence.get(key) is True for key in (
                                    "runtime_stopped", "new_claims_blocked", "data_services_fenced",
                                    "target_schema_preserved", "database_identity_preserved",
                                )) and not apply_drill._connects(port),
                                "ACTIVATION_DRILL_DATA_CHANGED")
                    for identifier in adapter.target_ids.values():
                        current = json.loads(ops.command(["docker", "inspect", identifier]))[0]
                        ops.require(current["State"].get("Running") is False
                                    and current["State"].get("Restarting") is False
                                    and current["HostConfig"]["RestartPolicy"]["Name"] == "no",
                                    "ACTIVATION_DRILL_CONTAINMENT_MISMATCH")
                    result.update(
                        injected_failure="INJECTED_POST_ACTIVATION_FAILURE", http_write_committed=True,
                        real_worker_claim_committed=True, committed_canary_count=1,
                        committed_all_tables_sha256=committed["all_tables_sha256"],
                        contained_all_tables_sha256=contained["all_tables_sha256"],
                        target_schema_preserved=contained["schema"] == migration["target_schema_head"],
                        runtime_stopped=evidence["runtime_stopped"], new_claims_blocked=evidence["new_claims_blocked"],
                        database_fenced=evidence["data_services_fenced"], postgres_host_port_blocked=True,
                    )
                adapter.close()
                adapter = None
        except BaseException as error:
            if not hasattr(error, "_activation_drill_failure_phase"):
                error._activation_drill_failure_phase = phase
            raise
        finally:
            mark("cleanup")
            try:
                if adapter is not None:
                    adapter.close()
                _helpers_removed(operation)
            finally:
                rehearsal.remove_disposable_project(runtime)
    ops.require(not root.exists() and result is not None, "ACTIVATION_DRILL_CLEANUP_FAILED")
    result["cleanup_verified"] = True
    return result


def run_drill(request: dict, output: Path, cosign: str, *, progress=None) -> dict:
    started = datetime.now(UTC).isoformat()
    _phase("preflight", progress)
    migration = contract.migration_request(request)
    rehearsal.require_clean_compose_environment()
    output = output.absolute()
    ops.require(not output.exists() and not output.is_symlink()
                and output.parent.is_dir() and output.parent.resolve() == output.parent,
                "ACTIVATION_DRILL_OUTPUT_UNSAFE")
    ops.require(int(ops.command(["docker", "info", "--format", "{{.MemTotal}}"])) >= 6 * 1024**3,
                "ACTIVATION_DRILL_REQUIRES_6_GIB")
    identity = checkout_identity(Path(__file__).resolve().parents[1])
    _phase("migration_rehearsal", progress)
    nested, passive = migration_rehearsal(migration, cosign=cosign, progress=progress)
    images = target_images(migration)
    _phase("adversarial_passive", progress)
    adversarial = adversarial_passive(migration, images)
    success = scenario(request, "success", images, progress=progress)
    failure = scenario(request, "failure", images, progress=progress)
    result = {
        "proof_contract_version": 1, "proof_type": contract.PROOF_TYPE,
        "repository": ops.REPO, **contract.activation_scope(request), **identity,
        "authorization": contract.FIXTURE_AUTHORITY, "status": "PASS",
        "migration_rehearsal": nested, "migration_target_passive": passive,
        "adversarial_passive": adversarial, "success": success, "failure": failure,
        "created_at": datetime.now(UTC).isoformat(), "production_approval_verified": False,
        "production_credentials": False, "application_external_network": False,
        "postgres_has_host_publish_network": True, "publishing_enabled": False,
        "disposable_projects_removed": True, "temporary_backups_and_keys_removed": True,
        "observers_closed": True,
    }
    _phase("proof_validation", progress)
    contract.validate_activation_rehearsal_proof(
        result, request, run_started_at=started, run_updated_at=datetime.now(UTC).isoformat(),
        expected_controller_sha=identity["controller_sha"], expected_controller_tree=identity["controller_tree"],
    )
    _phase("proof_publication", progress)
    apply_drill._publish(output, result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cosign", default="cosign")
    return parser


def failure_report(error: Exception, phase: str) -> dict:
    """Expose closed diagnostics only, never an arbitrary controller payload."""
    from migration_activation_apply import ERROR_CODES as controller_errors, ActivationFailure
    from migration_activation_journal import ERROR_CODES as state_errors
    from migration_activation_runtime import ERROR_CODES as runtime_errors, RECREATE_STEPS, TARGET_IDENTITY_FIELDS

    failed_phase = getattr(error, "_activation_drill_failure_phase", phase)
    if type(failed_phase) is not str or failed_phase not in _PHASES:
        failed_phase = phase if type(phase) is str and phase in _PHASES else "unknown"
    safe = (ERROR_CODES | contract.ERROR_CODES | admission.ERRORS | controller_errors | state_errors
            | runtime_errors | apply_drill._SAFE_ERROR_CODES | rehearsal._DIAGNOSTIC_ERROR_CODES)
    code = (str(error) if isinstance(error, (ops.ReleaseError, admission.DrillError))
            and str(error) in safe else "ACTIVATION_DRILL_FAILED")
    result = {"status": "FAIL", "phase": failed_phase, "automatic_retry": False, "error_code": code}
    if isinstance(error, ActivationFailure):
        outcome = error.result
        if type(outcome.get("status")) is str and outcome["status"] in {"CONTAINED", "OPERATOR_RECOVERY_REQUIRED"}:
            result["operation_status"] = outcome["status"]
        for source, target in (("error_code", "controller_error_code"), ("recovery_error_code", "recovery_error_code")):
            if source in outcome and (outcome[source] is None
                                      or type(outcome[source]) is str and outcome[source] in safe):
                result[target] = outcome[source]
        controller_phase = outcome.get("controller_phase")
        if type(controller_phase) is str and controller_phase in {
            "passive_postflight", "handoff", "recreate", "reconnect", "start", "postflight", "finalize", "close",
        }:
            result["controller_phase"] = controller_phase
        step = outcome.get("recreate_step")
        if controller_phase == "recreate" and type(step) is str and step in RECREATE_STEPS:
            result["recreate_step"] = step
        target_service = outcome.get("target_service")
        if type(target_service) is str and target_service in ops.APP:
            result["target_service"] = target_service
        identity_field = outcome.get("target_identity_field")
        if type(identity_field) is str and identity_field in TARGET_IDENTITY_FIELDS:
            result["target_identity_field"] = identity_field
        state = outcome.get("activation_state")
        if type(state) is dict:
            if type(state.get("phase")) is str and state["phase"] in {
                "NEW", "PASSIVE_VERIFIED", "ACTIVATION_REQUESTED", "ACTIVATION_STARTED",
                "ACTIVE", "CONTAINMENT_REQUESTED", "CONTAINED", "OPERATOR_RECOVERY_REQUIRED",
            }:
                result["activation_phase"] = state["phase"]
            if type(state.get("state_durability")) is str and state["state_durability"] in {"CONFIRMED", "UNKNOWN"}:
                result["state_durability"] = state["state_durability"]
            if type(state.get("error_code")) is str and state["error_code"] in safe:
                result["state_error_code"] = state["error_code"]
    return result


def main() -> None:
    args = build_parser().parse_args()
    os.umask(0o077)
    phase = "request_load"

    def progress(value: str) -> None:
        nonlocal phase
        ops.require(value in _PHASES, "ACTIVATION_DRILL_PHASE_INVALID")
        phase = value
        print(json.dumps({"status": "RUNNING", "phase": value}), file=sys.stderr, flush=True)

    try:
        result = run_drill(ops.load(args.request), args.output, args.cosign, progress=progress)
        print(json.dumps(result, sort_keys=True))
    except (ManifestContractError, ops.ReleaseError, admission.DrillError, OSError, ValueError, KeyError,
            TypeError, subprocess.SubprocessError) as error:
        print(json.dumps(failure_report(error, phase)))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

