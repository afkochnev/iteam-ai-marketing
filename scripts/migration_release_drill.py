#!/usr/bin/env python3
"""Isolated migration rehearsal on disposable Docker resources only.

The command accepts no checkout, Compose project, environment, port, volume, or
production credential arguments. It verifies exact signed images, restores a
source-schema fixture, rehearses the exact forward migration, validates target
invariants and, when policy permits, proves pre-activation downgrade.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from migration_business_fixture import require_business_fixture, seed_business_fixture
from migration_fixture_restore import restore_fixture
from release_backup import backup_restore, check_proof
from release_manifest import (
    COMPATIBILITY_CLASSES,
    FRONTEND_API_URL,
    REPOSITORY,
    ROLLBACK_POLICIES,
    ManifestContractError,
    migration_plan_hash,
)
from release_ops import (
    ReleaseError,
    Runtime,
    accepted,
    command,
    failure_code,
    load,
    require,
    require_image_schema,
    run_info,
    save,
    set_env,
    verify_image,
)

REQUEST_VERSION = 1
REQUEST_TYPE = "MIGRATION_REHEARSAL_REQUEST"
DATABASE_NAME = "iteam_migration_drill"
DEFAULT_WORKSPACE_ID = UUID("4183ac32-0f61-54da-ad9e-ebb31696101b")
DEFAULT_WORKSPACE_NAME = "iTeam"
DEFAULT_WORKSPACE_SLUG = "iteam"
ROLE_MAP = {"ADMIN": "WORKSPACE_ADMIN", "MANAGER": "MARKETING_MANAGER"}
ACTIVE_MANAGER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
INACTIVE_MANAGER_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SUPPORTED_SOURCE_SCHEMA = "20261009_0028"
SUPPORTED_TARGET_SCHEMA = "20261121_0030"
SUPPORTED_REVISIONS = ("20261121_0029", SUPPORTED_TARGET_SCHEMA)

Progress = Callable[[str], None]
_DIAGNOSTIC_PHASES = frozenset(
    {
        "request_load", "preflight", "image_verification",
        "source_accepted", "target_accepted", "target_build",
        "target_build_records", "source_schema_head", "target_schema_head",
        "migration_inventory", "infrastructure", "source_migrate", "seed",
        "source_ready", "source_data", "backup", "backup_check", "stop",
        "restore", "restore_checks", "target_migrate", "target_checks",
        "target_ready", "target_data", "downgrade", "downgrade_checks",
        "source_ready_after_downgrade", "source_data_after_downgrade",
        "cleanup", "temporary_cleanup", "publish_proof",
    }
    | {
        f"{side}_{component}_{operation}"
        for side in ("source", "target")
        for component in ("backend", "frontend")
        for operation in ("verify", "pull")
    }
)
_DIAGNOSTIC_ERROR_CODES = frozenset(
    {
        "INVALID_REHEARSAL_DIAGNOSTIC_PHASE",
        "MIGRATION_DRILL_COMPOSE_ENVIRONMENT_OVERRIDE",
        "MIGRATION_DRILL_OUTPUT_EXISTS",
        "MIGRATION_DRILL_REQUIRES_6_GIB",
        "MIGRATION_DRILL_READINESS_TIMEOUT",
        "MIGRATION_DRILL_FRONTEND_TIMEOUT",
        "MIGRATION_DRILL_BUILD_SHA_MISMATCH",
        "MIGRATION_DRILL_PROVIDER_POLICY_MISMATCH",
        "MIGRATION_DRILL_CONTAINER_OUTSIDE_PROJECT",
        "MIGRATION_DRILL_PORT_EXPOSED",
        "MIGRATION_DRILL_NETWORK_NOT_INTERNAL",
        "MIGRATION_DRILL_NON_DISPOSABLE_MOUNT",
        "MIGRATION_DRILL_CLEANUP_INCOMPLETE",
        "SOURCE_SCHEMA_MISMATCH", "RESTORED_SCHEMA_MISMATCH",
        "TARGET_SCHEMA_MISMATCH", "DOWNGRADE_SCHEMA_MISMATCH",
        "SOURCE_SCHEMA_OBJECTS_NOT_RESTORED",
        "BUSINESS_FIXTURE_NOT_REPRESENTATIVE",
        "BUSINESS_DATA_CHANGED_AFTER_RESTORE",
        "BUSINESS_DATA_CHANGED_AFTER_UPGRADE",
        "BUSINESS_DATA_CHANGED_AFTER_DOWNGRADE",
        "WORKSPACE_COUNT_MISMATCH", "CANONICAL_WORKSPACE_MISMATCH",
        "DUPLICATE_WORKSPACE_MEMBERSHIP", "ACTIVE_USER_MEMBERSHIP_COVERAGE_MISMATCH",
        "INACTIVE_USER_HAS_MEMBERSHIP", "UNSUPPORTED_ACTIVE_USER_ROLE",
        "WORKSPACE_MEMBERSHIP_MISMATCH",
        "MIGRATION_FIXTURE_RESTORE_FAILED", "MIGRATION_FIXTURE_RESTORE_CLEANUP_FAILED",
        "MIGRATION_FIXTURE_WRITERS_NOT_STOPPED",
        "MIGRATION_FIXTURE_CONTAINER_OUTSIDE_PROJECT",
        "MIGRATION_FIXTURE_BACKUP_OUTSIDE_PROJECT", "MIGRATION_FIXTURE_PROOF_CHANGED",
        "UNSAFE_MIGRATION_FIXTURE_DATABASE", "UNSAFE_MIGRATION_FIXTURE_PROJECT",
        "UNSAFE_MIGRATION_FIXTURE_DIRECTORY", "UNSAFE_MIGRATION_FIXTURE_COMPOSE",
        "BACKUP_RESTORE_GATE_FAILED", "BACKUP_FILE_MISSING_OR_CHANGED",
        "BACKUP_KEY_UNAVAILABLE", "ENCRYPTED_DUMP_FAILED",
        "SNAPSHOT_EXPORT_FAILED", "EMPTY_BACKUP_SNAPSHOT", "SNAPSHOT_RELEASE_FAILED",
        "ISOLATED_RESTORE_START_TIMEOUT", "ISOLATED_RESTORE_FAILED",
        "RESTORED_ROW_COUNTS_DIFFER", "EXACT_MAIN_CI_NOT_GREEN",
        "RELEASE_NOT_ON_MAIN", "WORKFLOW_NOT_SUCCESSFUL", "UNTRUSTED_WORKFLOW",
        "TARGET_BUILD_RUN_MISMATCH", "TARGET_BUILD_IMAGES_MISMATCH",
        "TARGET_BUILD_RECORD_MISMATCH", "MIGRATION_SCRIPT_MISSING_FROM_IMAGE",
        "MIGRATION_SCRIPT_IMAGE_MISMATCH", "PROVENANCE_SHA_MISMATCH",
        "REGISTRY_DIGEST_MISMATCH", "MISSING_ARCHITECTURE", "SBOM_MISSING",
        "FRONTEND_ENDPOINT_MISMATCH",
    }
)


def report_phase(progress: Progress | None, phase: str) -> None:
    """Only closed, payload-free phase names may reach a progress callback."""
    require(
        isinstance(phase, str) and phase in _DIAGNOSTIC_PHASES,
        "INVALID_REHEARSAL_DIAGNOSTIC_PHASE",
    )
    if progress is not None:
        progress(phase)


def diagnostic_error_code(error: Exception) -> str:
    """Never expose arbitrary exception text, including custom ReleaseError text."""
    safe_code = failure_code(error)
    if safe_code != "UNEXPECTED_DEPLOYMENT_ERROR":
        return safe_code
    if isinstance(error, ReleaseError) and str(error) in _DIAGNOSTIC_ERROR_CODES:
        return str(error)
    return "UNEXPECTED_MIGRATION_ERROR"

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCHEMA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,63}\Z")

_REQUEST_FIELDS = {
    "version",
    "request_type",
    "repository",
    "source_release_sha",
    "target_release_sha",
    "target_build_run_id",
    "target_build_attempt",
    "source_schema_head",
    "target_schema_head",
    "migration_path",
    "migration_plan_hash",
    "compatibility_class",
    "pre_activation_rollback_policy",
    "frontend_api_url",
    "source_images",
    "target_images",
}

IMAGE_MIGRATION_INVENTORY = r"""
import ast,hashlib,json
from pathlib import Path
result=[]
for path in sorted(Path('/app/alembic/versions').glob('*.py')):
 data=path.read_bytes()
 tree=ast.parse(data,filename=str(path))
 values={}
 for node in tree.body:
  target=None
  value=None
  if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name):
   target=node.target.id; value=node.value
  elif isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
   target=node.targets[0].id; value=node.value
  if target in {'revision','down_revision'} and value is not None:
   try: values[target]=ast.literal_eval(value)
   except (ValueError,SyntaxError): pass
 if isinstance(values.get('revision'),str) and isinstance(values.get('down_revision'),str):
  result.append({'revision':values['revision'],'down_revision':values['down_revision'],'script_sha256':'sha256:'+hashlib.sha256(data).hexdigest()})
print(json.dumps(result,sort_keys=True))
"""

NO_PROVIDER_POLICY = r"""
from app.core.config import settings
assert settings.vk_publishing_enabled is False
assert settings.telegram_publishing_enabled is False
assert settings.metrics_sync_enabled is False
assert settings.telegram_metrics_enabled is False
assert settings.vk_metrics_enabled is False
assert not settings.openai_api_key
assert not settings.vk_access_token
assert not settings.telegram_bot_token
assert not settings.vk_credentials_file
print('PASS')
"""


def _exact_object(value: Any, fields: set[str], code: str) -> dict[str, Any]:
    require(type(value) is dict and set(value) == fields, code)
    return value


def _positive_int(value: Any, code: str) -> int:
    require(type(value) is int and value > 0, code)
    return value


def _sha(value: Any, code: str) -> str:
    require(isinstance(value, str) and _SHA.fullmatch(value), code)
    return value


def _schema(value: Any, code: str) -> str:
    require(isinstance(value, str) and _SCHEMA.fullmatch(value), code)
    return value


def _images(value: Any, code: str) -> dict[str, str]:
    item = _exact_object(value, {"backend", "frontend"}, code)
    result: dict[str, str] = {}
    for component in ("backend", "frontend"):
        prefix = f"ghcr.io/{REPOSITORY}-{component}@"
        ref = item[component]
        require(
            isinstance(ref, str)
            and ref.startswith(prefix)
            and _DIGEST.fullmatch(ref[len(prefix) :]),
            "INVALID_REHEARSAL_IMAGE",
        )
        result[component] = ref
    return result


def validate_rehearsal_request(value: Any) -> None:
    request = _exact_object(value, _REQUEST_FIELDS, "UNEXPECTED_REHEARSAL_REQUEST_FIELDS")
    require(
        type(request["version"]) is int
        and request["version"] == REQUEST_VERSION
        and request["request_type"] == REQUEST_TYPE
        and request["repository"] == REPOSITORY,
        "INVALID_REHEARSAL_REQUEST_IDENTITY",
    )
    source_release = _sha(request["source_release_sha"], "INVALID_SOURCE_RELEASE_SHA")
    target_release = _sha(request["target_release_sha"], "INVALID_TARGET_RELEASE_SHA")
    require(source_release != target_release, "SOURCE_AND_TARGET_RELEASE_MATCH")
    _positive_int(request["target_build_run_id"], "INVALID_TARGET_BUILD_RUN")
    _positive_int(request["target_build_attempt"], "INVALID_TARGET_BUILD_ATTEMPT")
    source_schema = _schema(request["source_schema_head"], "INVALID_SOURCE_SCHEMA_HEAD")
    target_schema = _schema(request["target_schema_head"], "INVALID_TARGET_SCHEMA_HEAD")
    require(source_schema != target_schema, "SOURCE_AND_TARGET_SCHEMA_MATCH")
    require(
        (source_schema, target_schema)
        == (SUPPORTED_SOURCE_SCHEMA, SUPPORTED_TARGET_SCHEMA),
        "UNSUPPORTED_REHEARSAL_TRANSITION",
    )
    require(request["frontend_api_url"] == FRONTEND_API_URL, "UNREVIEWED_FRONTEND_ENDPOINT")
    _images(request["source_images"], "INVALID_SOURCE_IMAGES")
    _images(request["target_images"], "INVALID_TARGET_IMAGES")
    require(
        request["compatibility_class"] in COMPATIBILITY_CLASSES,
        "UNSUPPORTED_MIGRATION_COMPATIBILITY_CLASS",
    )
    require(
        request["pre_activation_rollback_policy"] in ROLLBACK_POLICIES,
        "UNSUPPORTED_PRE_ACTIVATION_ROLLBACK_POLICY",
    )
    expected = migration_plan_hash(
        source_schema_head=source_schema,
        target_schema_head=target_schema,
        migration_path=request["migration_path"],
        compatibility_class=request["compatibility_class"],
        pre_activation_rollback_policy=request["pre_activation_rollback_policy"],
    )
    require(
        isinstance(request["migration_plan_hash"], str)
        and _DIGEST.fullmatch(request["migration_plan_hash"]),
        "INVALID_MIGRATION_PLAN_HASH",
    )
    require(request["migration_plan_hash"] == expected, "MIGRATION_PLAN_HASH_MISMATCH")
    require(
        tuple(step["revision"] for step in request["migration_path"])
        == SUPPORTED_REVISIONS,
        "UNSUPPORTED_REHEARSAL_MIGRATION_PATH",
    )


def rehearsal_scope(request: dict[str, Any]) -> dict[str, Any]:
    validate_rehearsal_request(request)
    return {
        key: copy.deepcopy(request[key])
        for key in (
            "source_release_sha",
            "target_release_sha",
            "target_build_run_id",
            "target_build_attempt",
            "source_schema_head",
            "target_schema_head",
            "migration_plan_hash",
            "source_images",
            "target_images",
        )
    }


def validate_image_migration_inventory(
    request: dict[str, Any], inventory: Any
) -> None:
    validate_rehearsal_request(request)
    require(type(inventory) is list, "INVALID_IMAGE_MIGRATION_INVENTORY")
    by_revision: dict[str, dict[str, str]] = {}
    for item in inventory:
        step = _exact_object(
            item,
            {"revision", "down_revision", "script_sha256"},
            "INVALID_IMAGE_MIGRATION_STEP",
        )
        revision = _schema(step["revision"], "INVALID_IMAGE_MIGRATION_REVISION")
        require(revision not in by_revision, "DUPLICATE_IMAGE_MIGRATION_REVISION")
        by_revision[revision] = step
    for expected in request["migration_path"]:
        actual = by_revision.get(expected["revision"])
        require(actual is not None, "MIGRATION_SCRIPT_MISSING_FROM_IMAGE")
        require(actual == expected, "MIGRATION_SCRIPT_IMAGE_MISMATCH")


def image_migration_inventory(ref: str) -> list[dict[str, str]]:
    return json.loads(
        command(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                ref,
                "-c",
                IMAGE_MIGRATION_INVENTORY,
            ]
        )
    )


def validate_workspace_invariants(
    users: Any, workspaces: Any, memberships: Any
) -> None:
    require(
        type(users) is list and type(workspaces) is list and type(memberships) is list,
        "INVALID_WORKSPACE_INVARIANT_DATA",
    )
    require(len(workspaces) == 1, "WORKSPACE_COUNT_MISMATCH")
    workspace = workspaces[0]
    require(
        workspace
        == {
            "id": str(DEFAULT_WORKSPACE_ID),
            "name": DEFAULT_WORKSPACE_NAME,
            "slug": DEFAULT_WORKSPACE_SLUG,
            "is_default": True,
            "is_active": True,
        },
        "CANONICAL_WORKSPACE_MISMATCH",
    )
    active = {item["id"]: item for item in users if item["is_active"] is True}
    inactive = {item["id"] for item in users if item["is_active"] is False}
    stored = {item["user_id"]: item for item in memberships}
    require(len(stored) == len(memberships), "DUPLICATE_WORKSPACE_MEMBERSHIP")
    require(set(stored) == set(active), "ACTIVE_USER_MEMBERSHIP_COVERAGE_MISMATCH")
    require(not (set(stored) & inactive), "INACTIVE_USER_HAS_MEMBERSHIP")
    for user_id, user in active.items():
        expected_role = ROLE_MAP.get(user["role"])
        require(expected_role is not None, "UNSUPPORTED_ACTIVE_USER_ROLE")
        expected_id = str(uuid5(DEFAULT_WORKSPACE_ID, f"membership:{user_id}"))
        require(
            stored[user_id]
            == {
                "id": expected_id,
                "workspace_id": str(DEFAULT_WORKSPACE_ID),
                "user_id": user_id,
                "role": expected_role,
                "is_active": True,
            },
            "WORKSPACE_MEMBERSHIP_MISMATCH",
        )


def canonical_business_state(tables: dict[str, list[str]]) -> dict[str, Any]:
    """Hash PostgreSQL JSON row strings without losing numeric precision.

    Table and row order do not depend on pg_dump or restored heap layout. JSONB
    supplies stable object-key order; duplicate rows and empty tables still
    contribute to the hash. Only hashes and counts enter the exported proof.
    """
    require(
        type(tables) is dict
        and bool(tables)
        and all(
            isinstance(name, str)
            and type(rows) is list
            and all(isinstance(row, str) for row in rows)
            for name, rows in tables.items()
        ),
        "INVALID_BUSINESS_DATA_SNAPSHOT",
    )
    canonical = json.dumps(
        {name: sorted(rows) for name, rows in tables.items()},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return {
        "table_counts": {name: len(rows) for name, rows in sorted(tables.items())},
        "sha256": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
    }


def require_clean_compose_environment() -> None:
    require(
        not any(key.startswith("COMPOSE_") and value for key, value in os.environ.items()),
        "MIGRATION_DRILL_COMPOSE_ENVIRONMENT_OVERRIDE",
    )


class MigrationDrillRuntime(Runtime):
    def __init__(self, root: Path, *, project: str):
        require_clean_compose_environment()
        require(
            re.fullmatch(r"iteam-migration-drill-[0-9a-f]{10}", project),
            "INVALID_MIGRATION_DRILL_PROJECT",
        )
        super().__init__(root, project=project)
        self.project = project
        self._select_files(target=False)

    def _select_files(self, *, target: bool) -> None:
        image_file = "target-images.json" if target else "source-images.json"
        self.base = [
            "docker", "compose",
            "--project-directory", str(self.root),
            "--project-name", self.project,
            "--env-file", str(self.env_path),
            "-f", str(self.root / "compose.json"),
            "-f", str(self.root / image_file),
        ]

    def compose(self, *args: str) -> str:
        require_clean_compose_environment()
        return command(self.base + list(args), cwd=self.root)

    def inspect(self, service: str) -> dict[str, Any]:
        ids = self.compose("ps", "--all", "-q", service).splitlines()
        require(len(ids) == 1, "SERVICE_INSTANCE_COUNT:" + service)
        return json.loads(command(["docker", "inspect", ids[0]]))[0]

    def ready(self) -> None:
        for _ in range(90):
            try:
                value = json.loads(
                    self.compose(
                        "exec",
                        "-T",
                        "backend",
                        "python",
                        "-c",
                        "import json,urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read().decode())",
                    )
                )
                if all(
                    value.get(key) == "ok"
                    for key in ("status", "database", "redis", "schema")
                ):
                    return
            except (ReleaseError, ValueError):
                pass
            time.sleep(1)
        raise ReleaseError("MIGRATION_DRILL_READINESS_TIMEOUT")

    def frontend_ready(self) -> None:
        for _ in range(60):
            try:
                self.compose(
                    "exec",
                    "-T",
                    "frontend",
                    "node",
                    "-e",
                    "fetch('http://127.0.0.1:3000/publications',{signal:AbortSignal.timeout(3000)}).then(r=>{process.exit(r.status===200?0:1)}).catch(()=>process.exit(1))",
                )
                return
            except ReleaseError:
                time.sleep(1)
        raise ReleaseError("MIGRATION_DRILL_FRONTEND_TIMEOUT")

    def schema(self) -> str:
        return self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "iteam",
            "-d",
            DATABASE_NAME,
            "-At",
            "-c",
            "SELECT version_num FROM alembic_version",
        )

    def business_state(self) -> dict[str, Any]:
        names = self.json_query(
            "SELECT coalesce(json_agg(tablename ORDER BY tablename),'[]'::json) "
            "FROM pg_tables WHERE schemaname='public' "
            "AND tablename NOT IN ('alembic_version','workspaces','workspace_memberships')"
        )
        require(
            type(names) is list and all(isinstance(name, str) for name in names),
            "INVALID_BUSINESS_TABLE_INVENTORY",
        )
        tables = {}
        for name in names:
            identifier = '"' + name.replace('"', '""') + '"'
            # Keep each row as PostgreSQL JSON text; a Python float round-trip
            # could hide changes to high-precision numeric business values.
            tables[name] = self.json_query(
                "SELECT coalesce(json_agg(to_jsonb(row_data)::text),'[]'::json) "
                f"FROM public.{identifier} AS row_data"
            )
        return canonical_business_state(tables)

    def json_query(self, sql: str) -> Any:
        return json.loads(
            self.compose(
                "exec",
                "-T",
                "postgres",
                "psql",
                "-X",
                "-U",
                "iteam",
                "-d",
                DATABASE_NAME,
                "-At",
                "-c",
                sql,
            )
        )

    def workspace_state(self) -> tuple[Any, Any, Any]:
        users = self.json_query(
            "SELECT coalesce(json_agg(json_build_object('id',id::text,'role',role::text,'is_active',is_active) ORDER BY id),'[]'::json) FROM users"
        )
        workspaces = self.json_query(
            "SELECT coalesce(json_agg(json_build_object('id',id::text,'name',name,'slug',slug,'is_default',is_default,'is_active',is_active) ORDER BY id),'[]'::json) FROM workspaces"
        )
        memberships = self.json_query(
            "SELECT coalesce(json_agg(json_build_object('id',id::text,'workspace_id',workspace_id::text,'user_id',user_id::text,'role',role::text,'is_active',is_active) ORDER BY id),'[]'::json) FROM workspace_memberships"
        )
        return users, workspaces, memberships

    def source_schema_restored(self) -> None:
        value = self.json_query(
            "SELECT json_build_object('workspaces',to_regclass('public.workspaces') IS NULL,'memberships',to_regclass('public.workspace_memberships') IS NULL,'role_type',NOT EXISTS(SELECT 1 FROM pg_type WHERE typname='workspace_role'))"
        )
        require(
            value == {"workspaces": True, "memberships": True, "role_type": True},
            "SOURCE_SCHEMA_OBJECTS_NOT_RESTORED",
        )

    def switch_images(self, request: dict[str, Any], target: bool) -> None:
        sha = request["target_release_sha"] if target else request["source_release_sha"]
        self.env_path.write_text(
            set_env(
                self.env_path.read_text(),
                {"BUILD_SHA": sha},
            )
        )
        self.env_path.chmod(0o600)
        self._select_files(target=target)

    def run_backend(self, *args: str) -> str:
        return self.compose("run", "--rm", "--no-deps", "-T", "backend", *args)

    def app_ready(self, request: dict[str, Any], target: bool) -> None:
        self.compose("up", "-d", "--no-build", "backend", "frontend")
        self.ready()
        self.frontend_ready()
        expected = request["target_release_sha"] if target else request["source_release_sha"]
        actual = self.compose(
            "exec",
            "-T",
            "backend",
            "python",
            "-c",
            "from app.core.config import settings;print(settings.build_sha)",
        )
        require(actual == expected, "MIGRATION_DRILL_BUILD_SHA_MISMATCH")
        require(
            self.compose("exec", "-T", "backend", "python", "-c", NO_PROVIDER_POLICY)
            == "PASS",
            "MIGRATION_DRILL_PROVIDER_POLICY_MISMATCH",
        )

    def stop_app(self) -> None:
        self.compose("stop", "frontend", "backend")


def create_drill(
    root: Path, project: str, request: dict[str, Any]
) -> MigrationDrillRuntime:
    validate_rehearsal_request(request)
    env = {
        "APP_ENV": "development",
        "BUILD_SHA": request["source_release_sha"],
        "DATABASE_URL": f"postgresql+asyncpg://iteam:drill-only@postgres:5432/{DATABASE_NAME}",
        "REDIS_URL": "redis://redis:6379/15",
        "JWT_SECRET": secrets.token_urlsafe(40),
        "APP_SECRET": secrets.token_urlsafe(40),
        "ADMIN_EMAIL": "migration-admin@example.com",
        "ADMIN_PASSWORD": secrets.token_urlsafe(40),
        "TELEGRAM_PUBLISHING_ENABLED": "false",
        "VK_PUBLISHING_ENABLED": "false",
        "TELEGRAM_METRICS_ENABLED": "false",
        "VK_METRICS_ENABLED": "false",
        "METRICS_SYNC_ENABLED": "false",
        "OPENAI_API_KEY": "",
        "TELEGRAM_BOT_TOKEN": "",
        "VK_ACCESS_TOKEN": "",
        "VK_CREDENTIALS_FILE": "",
    }
    (root / ".env").write_text("\n".join(f"{key}={value}" for key, value in env.items()) + "\n")
    (root / ".env").chmod(0o600)
    services: dict[str, Any] = {
        "postgres": {
            "image": "postgres:17-alpine",
            "environment": {
                "POSTGRES_USER": "iteam",
                "POSTGRES_PASSWORD": "drill-only",
                "POSTGRES_DB": DATABASE_NAME,
            },
            "volumes": ["postgres_data:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": ["CMD", "pg_isready", "-U", "iteam", "-d", DATABASE_NAME],
                "interval": "1s",
                "timeout": "3s",
                "retries": 60,
            },
        },
        "redis": {
            "image": "redis:8-alpine",
            "volumes": ["redis_data:/data"],
            "healthcheck": {
                "test": ["CMD", "redis-cli", "ping"],
                "interval": "1s",
                "timeout": "3s",
                "retries": 60,
            },
        },
        "backend": {
            "env_file": ".env",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
                "redis": {"condition": "service_healthy"},
            },
            "mem_limit": "512m",
            "cpus": 1.0,
        },
        "frontend": {
            "depends_on": {"backend": {"condition": "service_started"}},
            "mem_limit": "256m",
            "cpus": 0.5,
        },
    }
    save(
        root / "compose.json",
        {
            "services": services,
            "volumes": {"postgres_data": {}, "redis_data": {}},
            "networks": {"default": {"internal": True}},
        },
    )
    for label, images in (
        ("source", request["source_images"]),
        ("target", request["target_images"]),
    ):
        save(
            root / f"{label}-images.json",
            {
                "services": {
                    "backend": {"image": images["backend"]},
                    "frontend": {"image": images["frontend"]},
                }
            },
        )
    return MigrationDrillRuntime(root, project=project)


def verify_disposable_topology(runtime: MigrationDrillRuntime, project: str) -> None:
    for name in ("postgres", "redis", "backend", "frontend"):
        data = runtime.inspect(name)
        labels = data["Config"].get("Labels", {})
        require(
            labels.get("com.docker.compose.project") == project
            and labels.get("com.docker.compose.service") == name,
            "MIGRATION_DRILL_CONTAINER_OUTSIDE_PROJECT",
        )
        require(not data["HostConfig"].get("PortBindings"), "MIGRATION_DRILL_PORT_EXPOSED")
        for network in data["NetworkSettings"]["Networks"].values():
            detail = json.loads(command(["docker", "network", "inspect", network["NetworkID"]]))[0]
            require(
                detail["Internal"]
                and detail.get("Labels", {}).get("com.docker.compose.project") == project,
                "MIGRATION_DRILL_NETWORK_NOT_INTERNAL",
            )
        for mount in data["Mounts"]:
            require(
                mount["Type"] == "volume"
                and mount.get("Name", "").startswith(project + "_"),
                "MIGRATION_DRILL_NON_DISPOSABLE_MOUNT",
            )


def seed_fixture(runtime: MigrationDrillRuntime) -> None:
    runtime.run_backend("python", "-m", "app.seed")
    runtime.compose(
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "iteam",
        "-d",
        DATABASE_NAME,
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        "INSERT INTO users (id,email,password_hash,full_name,role,is_active,auth_version) VALUES "
        f"('{ACTIVE_MANAGER_ID}','active.manager@example.com','fixture','Active Manager','MANAGER',true,1),"
        f"('{INACTIVE_MANAGER_ID}','inactive.manager@example.com','fixture','Inactive Manager','MANAGER',false,1)",
    )
    seed_business_fixture(runtime)


def validate_target_build_records(
    request: dict[str, Any], records: Any
) -> dict[str, str]:
    """Bind exact target digests to the declared release-images workflow run."""
    validate_rehearsal_request(request)
    require(
        type(records) is dict and set(records) == {"backend", "frontend"},
        "INVALID_TARGET_BUILD_RECORDS",
    )
    images: dict[str, str] = {}
    expected_fields = {"release_sha", "image", "platforms", "frontend_api_url"}
    for component in ("backend", "frontend"):
        record = records[component]
        require(
            type(record) is dict and set(record) == expected_fields,
            "INVALID_TARGET_BUILD_RECORD",
        )
        require(
            record["release_sha"] == request["target_release_sha"]
            and record["image"] == request["target_images"][component]
            and type(record["platforms"]) is list
            and len(record["platforms"]) == 2
            and all(isinstance(platform, str) for platform in record["platforms"])
            and set(record["platforms"]) == {"linux/amd64", "linux/arm64"}
            and record["frontend_api_url"] == FRONTEND_API_URL,
            "TARGET_BUILD_RECORD_MISMATCH",
        )
        images[component] = record["image"]
    return images


def target_build_record_images(request: dict[str, Any]) -> dict[str, str]:
    records: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="iteam-migration-build-records-") as directory:
        root = Path(directory)
        for component in ("backend", "frontend"):
            target = root / component
            command(
                [
                    "gh",
                    "run",
                    "download",
                    str(request["target_build_run_id"]),
                    "--repo",
                    REPOSITORY,
                    "--name",
                    f"release-{component}-{request['target_release_sha']}",
                    "--dir",
                    str(target),
                ]
            )
            records[component] = load(target / f"release-{component}.json")
    return validate_target_build_records(request, records)


def verify_images(
    request: dict[str, Any], cosign: str, progress: Progress | None = None
) -> None:
    validate_rehearsal_request(request)
    report_phase(progress, "source_accepted")
    accepted(request["source_release_sha"])
    report_phase(progress, "target_accepted")
    accepted(request["target_release_sha"])
    report_phase(progress, "target_build")
    build = run_info(
        request["target_build_run_id"], "release-images.yml", "workflow_run"
    )
    require(
        build["head_sha"] == request["target_release_sha"]
        and build["run_attempt"] == request["target_build_attempt"],
        "TARGET_BUILD_RUN_MISMATCH",
    )
    report_phase(progress, "target_build_records")
    require(
        target_build_record_images(request) == request["target_images"],
        "TARGET_BUILD_IMAGES_MISMATCH",
    )
    for side in ("source", "target"):
        sha = request[f"{side}_release_sha"]
        images = request[f"{side}_images"]
        for component in ("backend", "frontend"):
            report_phase(progress, f"{side}_{component}_verify")
            verify_image(component, images[component], sha, cosign)
            report_phase(progress, f"{side}_{component}_pull")
            command(["docker", "pull", images[component]])
    report_phase(progress, "source_schema_head")
    require_image_schema(
        request["source_images"]["backend"],
        request["source_schema_head"],
        pull=False,
    )
    report_phase(progress, "target_schema_head")
    require_image_schema(
        request["target_images"]["backend"],
        request["target_schema_head"],
        pull=False,
    )
    report_phase(progress, "migration_inventory")
    validate_image_migration_inventory(
        request,
        image_migration_inventory(request["target_images"]["backend"]),
    )


def remove_disposable_project(runtime: MigrationDrillRuntime) -> None:
    runtime.compose("down", "--volumes", "--remove-orphans")
    label = "label=com.docker.compose.project=" + runtime.project
    for resource in ("container", "volume", "network"):
        flags = ["--all"] if resource == "container" else []
        remaining = command(["docker", resource, "ls", *flags, "--quiet", "--filter", label])
        require(not remaining, "MIGRATION_DRILL_CLEANUP_INCOMPLETE")


def rehearse(
    request: dict[str, Any],
    output: Path,
    cosign: str,
    *,
    progress: Progress | None = None,
) -> dict[str, Any]:
    phase = "preflight"

    def mark(next_phase: str) -> None:
        nonlocal phase
        report_phase(progress, next_phase)
        phase = next_phase

    mark("preflight")
    validate_rehearsal_request(request)
    require_clean_compose_environment()
    require(not output.exists() and not output.is_symlink(), "MIGRATION_DRILL_OUTPUT_EXISTS")
    require(
        int(command(["docker", "info", "--format", "{{.MemTotal}}"])) >= 6 * 1024**3,
        "MIGRATION_DRILL_REQUIRES_6_GIB",
    )
    mark("image_verification")
    if progress is None:
        # Preserve the original dependency call shape for direct callers.
        verify_images(request, cosign)
    else:
        verify_images(request, cosign, mark)
    with tempfile.TemporaryDirectory(prefix="iteam-migration-drill-") as directory:
        root = Path(directory).resolve()
        project = "iteam-migration-drill-" + secrets.token_hex(5)
        runtime = create_drill(root, project, request)
        downgrade_tested = False
        downgraded_state = None
        result: dict[str, Any]
        try:
            mark("infrastructure")
            runtime.compose(
                "up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis"
            )
            mark("source_migrate")
            runtime.run_backend("alembic", "upgrade", request["source_schema_head"])
            require(runtime.schema() == request["source_schema_head"], "SOURCE_SCHEMA_MISMATCH")
            mark("seed")
            seed_fixture(runtime)
            mark("source_ready")
            runtime.app_ready(request, target=False)
            verify_disposable_topology(runtime, project)
            mark("source_data")
            baseline = runtime.business_state()
            require_business_fixture(baseline["table_counts"])
            mark("backup")
            backup_restore(runtime, root / "backups", root / "keys", root / "backup-proof.json")
            mark("backup_check")
            backup_proof = check_proof(root / "backup-proof.json", request["source_release_sha"])

            mark("stop")
            runtime.stop_app()
            mark("restore")
            restore_fixture(runtime, backup_proof)
            mark("restore_checks")
            require(runtime.schema() == request["source_schema_head"], "RESTORED_SCHEMA_MISMATCH")
            runtime.source_schema_restored()
            restored_state = runtime.business_state()
            require(restored_state == baseline, "BUSINESS_DATA_CHANGED_AFTER_RESTORE")
            runtime.run_backend("alembic", "check")

            mark("target_migrate")
            runtime.switch_images(request, target=True)
            runtime.run_backend("alembic", "upgrade", request["target_schema_head"])
            mark("target_checks")
            require(runtime.schema() == request["target_schema_head"], "TARGET_SCHEMA_MISMATCH")
            runtime.run_backend("alembic", "check")
            validate_workspace_invariants(*runtime.workspace_state())
            mark("target_ready")
            runtime.app_ready(request, target=True)
            verify_disposable_topology(runtime, project)
            mark("target_data")
            upgraded_state = runtime.business_state()
            require(upgraded_state == baseline, "BUSINESS_DATA_CHANGED_AFTER_UPGRADE")

            if request["pre_activation_rollback_policy"] == "PRE_ACTIVATION_DOWNGRADE":
                mark("downgrade")
                runtime.stop_app()
                runtime.run_backend("alembic", "downgrade", request["source_schema_head"])
                runtime.switch_images(request, target=False)
                mark("downgrade_checks")
                require(runtime.schema() == request["source_schema_head"], "DOWNGRADE_SCHEMA_MISMATCH")
                runtime.run_backend("alembic", "check")
                runtime.source_schema_restored()
                mark("source_ready_after_downgrade")
                runtime.app_ready(request, target=False)
                verify_disposable_topology(runtime, project)
                mark("source_data_after_downgrade")
                downgraded_state = runtime.business_state()
                require(downgraded_state == baseline, "BUSINESS_DATA_CHANGED_AFTER_DOWNGRADE")
                downgrade_tested = True

            result = {
                "status": "PASS",
                "proof_contract_version": 1,
                "repository": REPOSITORY,
                **rehearsal_scope(request),
                "compatibility_class": request["compatibility_class"],
                "pre_activation_rollback_policy": request[
                    "pre_activation_rollback_policy"
                ],
                "migration_revisions": [
                    step["revision"] for step in request["migration_path"]
                ],
                "encrypted_backup_isolated_restore": True,
                "migration_executed_on_restored_database": True,
                "encrypted_backup_sha256": "sha256:" + backup_proof["checksum"],
                "source_schema_verified": True,
                "target_schema_verified": True,
                "migration_specific_invariants": True,
                "business_data_hash_unchanged": True,
                "source_table_counts": baseline["table_counts"],
                "business_data_sha256": {
                    "source": baseline["sha256"],
                    "restored": restored_state["sha256"],
                    "upgraded": upgraded_state["sha256"],
                    "downgraded": downgraded_state["sha256"] if downgraded_state else None,
                },
                "target_runtime_readiness": True,
                "pre_activation_downgrade_tested": downgrade_tested,
                "writers_activated": False,
                "external_network": False,
                "production_credentials": False,
                "publishing_enabled": False,
                "disposable_services": 4,
                "created_at": datetime.now(UTC).isoformat(),
            }
        except Exception as error:
            # Successful cleanup must not replace the phase that actually
            # failed. A new cleanup exception naturally reports cleanup.
            error._migration_failure_phase = phase
            raise
        finally:
            mark("cleanup")
            remove_disposable_project(runtime)
            mark("temporary_cleanup")
    # TemporaryDirectory also owns encrypted backup/key files. Publish a PASS
    # proof only after both Docker and filesystem cleanup have succeeded.
    result["disposable_project_removed"] = True
    result["temporary_backup_and_key_removed"] = True
    mark("publish_proof")
    save(output, result)
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

    def progress(next_phase: str) -> None:
        nonlocal phase
        require(
            isinstance(next_phase, str) and next_phase in _DIAGNOSTIC_PHASES,
            "INVALID_REHEARSAL_DIAGNOSTIC_PHASE",
        )
        phase = next_phase
        print(
            json.dumps({"status": "RUNNING", "phase": phase}),
            file=sys.stderr,
            flush=True,
        )

    try:
        request = load(args.request)
        result = rehearse(request, args.output, args.cosign, progress=progress)
        print(json.dumps(result, sort_keys=True))
    except (
        ManifestContractError,
        ReleaseError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
    ) as error:
        failed_phase = getattr(error, "_migration_failure_phase", phase)
        if not isinstance(failed_phase, str) or failed_phase not in _DIAGNOSTIC_PHASES:
            failed_phase = phase
        print(json.dumps({
            "status": "FAIL",
            "automatic_retry": False,
            "phase": failed_phase,
            "error_code": diagnostic_error_code(error),
        }))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
