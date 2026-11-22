"""Production and disposable adapters for a network-fenced migration.

No method reconnects PostgreSQL, starts an existing application container,
modifies the production checkout/environment, or restores a live database.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import migration_apply_backup as backup
import migration_release_drill as drill
import release_ops as ops
from migration_apply_state import canonical_bytes, manifest_hash
from migration_writer_barrier import WriterBarrier
from release_manifest import validate_migration_manifest

ERROR_CODES = frozenset({
    "MIGRATION_RUNTIME_IDENTITY_INVALID", "MIGRATION_SOURCE_CHANGED",
    "MIGRATION_SOURCE_PROVIDERS_ENABLED", "MIGRATION_SOURCE_NOT_QUIET",
    "MIGRATION_DATABASE_IDENTITY_CHANGED", "MIGRATION_DATABASE_CLIENTS_PRESENT",
    "MIGRATION_SERVER_WRITERS_PRESENT", "MIGRATION_UNSAFE_POSTGRES_CONFIGURATION",
    "MIGRATION_SOURCE_CONFIGURATION_CHANGED", "MIGRATION_HELPER_FAILED",
    "MIGRATION_HELPER_CLEANUP_FAILED", "MIGRATION_HELPER_TIMEOUT",
    "MIGRATION_READINESS_FAILED", "MIGRATION_RUNTIME_BASELINE_EXISTS",
    "MIGRATION_UNSAFE_PRODUCTION_ROOT", "MIGRATION_UNSAFE_PRODUCTION_COMPOSE",
    "MIGRATION_SOURCE_IMAGE_MISMATCH", "MIGRATION_TARGET_IMAGE_MISMATCH",
})
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
CONTAINER = re.compile(r"[0-9a-f]{64}\Z")

# Credentials travel over the attached stdin stream, never container argv,
# Docker Config.Env, an on-disk environment file, a journal or diagnostic.
BOOTSTRAP = r'''
import json,os,sys
value=json.load(sys.stdin)
assert set(value)=={'environment','argv'}
assert type(value['environment']) is dict and type(value['argv']) is list
assert all(type(k) is str and type(v) is str for k,v in value['environment'].items())
os.environ.update(value['environment'])
os.execvp(value['argv'][0],value['argv'])
'''

READINESS = r'''
import asyncio,json
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from sqlalchemy.pool import NullPool
from app.core.config import settings
import app.core.database as database
assert settings.app_env=='maintenance'
assert not any((settings.vk_publishing_enabled,settings.telegram_publishing_enabled,
 settings.metrics_sync_enabled,settings.vk_metrics_enabled,settings.telegram_metrics_enabled))
assert not any((settings.openai_api_key,settings.vk_access_token,settings.vk_credentials_file,
 settings.telegram_bot_token,settings.vk_metrics_access_token,settings.telegram_metrics_session))
database.engine=create_async_engine(settings.database_url,poolclass=NullPool,
 connect_args={'server_settings':{'default_transaction_read_only':'on',
 'application_name':'iteam-migration-readiness'}})
database.async_session_factory=async_sessionmaker(database.engine,expire_on_commit=False)
async def check():
 async with database.engine.connect() as c:
  assert await c.scalar(text('SHOW transaction_read_only'))=='on'
 await database.engine.dispose()
asyncio.run(check())
# Router imports bind to the replacement factory, after the read-only settings.
from app.main import app
Path('/tmp/migration-readiness.json').write_text(json.dumps({
 'status':'PASS','build_sha':settings.build_sha,'transaction_read_only':'on'}))
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=8000,log_level='warning',access_log=False)
'''

IDENTITY_SQL = """SELECT json_build_object(
'system_identifier',(SELECT system_identifier::text FROM pg_control_system()),
'database_oid',(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),
'database_name',current_database())"""
QUIET_SQL = """SELECT json_build_object(
'schemas',(SELECT json_agg(version_num) FROM alembic_version),
'agent_runs',(SELECT count(*) FROM agent_runs WHERE status::text IN ('QUEUED','RUNNING')),
'publications',(SELECT count(*) FROM publications WHERE status::text IN ('QUEUED','PUBLISHING')
 OR (status::text='SCHEDULED' AND (scheduled_at IS NULL OR scheduled_at <= now()+interval '30 minutes'))))"""
SERVER_SQL = """SELECT json_build_object(
'extensions',(SELECT coalesce(json_agg(extname ORDER BY extname),'[]'::json) FROM pg_extension),
'subscriptions',(SELECT count(*) FROM pg_subscription),
'prepared',(SELECT count(*) FROM pg_prepared_xacts))"""
SERVER_CONFIGURATION_SQL = """SELECT json_build_object(
'unix_socket_directories',current_setting('unix_socket_directories'),
'shared_preload_libraries',current_setting('shared_preload_libraries'),
'session_preload_libraries',current_setting('session_preload_libraries'),
'local_preload_libraries',current_setting('local_preload_libraries'))"""
CLIENTS_SQL = """SELECT count(*) FROM pg_stat_activity
WHERE backend_type='client backend' AND pid<>pg_backend_pid()
AND NOT (application_name='pg_isready' AND client_addr IS NULL)"""


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def _environment(container: dict) -> dict[str, str]:
    result = {}
    for item in container["Config"].get("Env") or []:
        ops.require(isinstance(item, str) and "=" in item, "MIGRATION_RUNTIME_IDENTITY_INVALID")
        key, value = item.split("=", 1)
        ops.require(key not in result, "MIGRATION_RUNTIME_IDENTITY_INVALID")
        result[key] = value
    return result


class ExplicitRuntime(ops.Runtime):
    def __init__(self, root: Path, project: str, files: list[Path]):
        super().__init__(root, project=project)
        self.base = [
            "docker", "compose", "--project-directory", str(self.root),
            "--project-name", project, "--env-file", str(self.env_path),
            "--profile", "publishing",
        ]
        for path in files:
            self.base += ["-f", str(path)]

    def inspect(self, service: str) -> dict[str, Any]:
        ids = self.compose("ps", "--all", "-q", service).splitlines()
        ops.require(len(ids) == 1, "MIGRATION_RUNTIME_IDENTITY_INVALID")
        return json.loads(ops.command(["docker", "inspect", ids[0]]))[0]

    def compose(self, *args: str) -> str:
        drill.require_clean_compose_environment()
        return super().compose(*args)


class MigrationRuntime:
    def __init__(
        self, runtime: ops.Runtime, manifest: dict, operation_id: str,
        state_dir: Path, backup_dir: Path, key_dir: Path,
    ):
        validate_migration_manifest(manifest)
        ops.require(re.fullmatch(r"[0-9a-f]{32}", operation_id), "MIGRATION_RUNTIME_IDENTITY_INVALID")
        self.runtime = runtime
        self.manifest = copy.deepcopy(manifest)
        self.operation_id = operation_id
        self.state_dir, self.backup_dir, self.key_dir = state_dir, backup_dir, key_dir
        self.writer_barrier = WriterBarrier(runtime, operation_id)
        self._captured = False
        self._fenced = False
        self._counter = 0
        self._helpers: dict[str, subprocess.Popen | None] = {}
        self._pending_names: set[str] = set()
        self._baseline: dict[str, Any] = {}
        self._inventory: dict[str, Any] = {}
        self._database_environment: dict[str, str] = {}
        self._initial_config_hash = ""
        self._root_env_hash = ""
        self._database_identity = ""
        self._redis_database = 0
        self._images: dict[str, str] = {}

    def _sql(self, sql: str) -> str:
        env = self._database_environment
        return ops.command([
            "docker", "exec", self.writer_barrier.postgres_id, "psql", "-X", "-At",
            "-v", "ON_ERROR_STOP=1", "-U", env["POSTGRES_USER"], "-d", env["POSTGRES_DB"],
            "-c", sql,
        ])

    def _json_query(self, sql: str) -> Any:
        return json.loads(self._sql(sql))

    def _unchanged_configuration(self) -> None:
        ops.require(
            _hash(self.runtime.env_path.read_text()) == self._root_env_hash
            and _hash(json.loads(self.runtime.compose("config", "--format", "json"))) == self._initial_config_hash,
            "MIGRATION_SOURCE_CONFIGURATION_CHANGED",
        )

    def inventory(self) -> dict[str, Any]:
        ops.require(not self._captured, "MIGRATION_RUNTIME_IDENTITY_INVALID")
        drill.require_clean_compose_environment()
        containers = {name: self.runtime.inspect(name) for name in sorted(ops.ALL)}
        envs = {name: _environment(value) for name, value in containers.items()}
        backend = envs["backend"]
        expected = self.manifest["source_release_sha"]
        for name in ops.APP:
            side = "frontend" if name == "frontend" else "backend"
            reference = self.manifest["source_images"][side]
            if reference not in self._images:
                image = json.loads(ops.command(["docker", "image", "inspect", reference]))[0]
                ops.require((image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") == expected, "MIGRATION_SOURCE_IMAGE_MISMATCH")
                self._images[reference] = image["Id"]
            value = containers[name]
            ops.require(
                value["Image"] == self._images[reference]
                and (value["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") == expected,
                "MIGRATION_SOURCE_IMAGE_MISMATCH",
            )
            if name != "frontend":
                ops.require(
                    envs[name].get("BUILD_SHA") == expected
                    and envs[name].get("DATABASE_URL") == backend.get("DATABASE_URL")
                    and envs[name].get("REDIS_URL") == backend.get("REDIS_URL"),
                    "MIGRATION_RUNTIME_IDENTITY_INVALID",
                )
                ops.require(
                    all(envs[name].get(key, "false").lower() == "false" for key in (
                        "VK_PUBLISHING_ENABLED", "TELEGRAM_PUBLISHING_ENABLED",
                        "METRICS_SYNC_ENABLED", "VK_METRICS_ENABLED", "TELEGRAM_METRICS_ENABLED",
                    )),
                    "MIGRATION_SOURCE_PROVIDERS_ENABLED",
                )
        for component, reference in self.manifest["target_images"].items():
            image = json.loads(ops.command(["docker", "image", "inspect", reference]))[0]
            ops.require((image["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") == self.manifest["target_release_sha"], "MIGRATION_TARGET_IMAGE_MISMATCH")
            self._images[reference] = image["Id"]
        pg = envs["postgres"]
        ops.require(
            all(isinstance(pg.get(key), str) and IDENTIFIER.fullmatch(pg[key]) for key in ("POSTGRES_USER", "POSTGRES_DB"))
            and isinstance(pg.get("POSTGRES_PASSWORD"), str) and bool(pg["POSTGRES_PASSWORD"]),
            "MIGRATION_UNSAFE_POSTGRES_CONFIGURATION",
        )
        redis_url = urlsplit(backend.get("REDIS_URL", ""))
        ops.require(
            redis_url.scheme == "redis" and redis_url.hostname == "redis"
            and (redis_url.port or 6379) == 6379 and not redis_url.username and not redis_url.password
            and re.fullmatch(r"/(?:[0-9]|1[0-5])", redis_url.path),
            "MIGRATION_RUNTIME_IDENTITY_INVALID",
        )
        self._redis_database = int(redis_url.path[1:])
        self._database_environment = pg
        baseline = self.writer_barrier.capture()
        self._initial_config_hash = _hash(json.loads(self.runtime.compose("config", "--format", "json")))
        self._root_env_hash = _hash(self.runtime.env_path.read_text())
        self._database_identity = backup.database_identity_hash(self._json_query(IDENTITY_SQL))
        source = json.loads(self.runtime.compose("exec", "-T", "backend", "python", "-c", backup.SNAPSHOT_CODE))
        backup.validate_snapshot(source)
        ops.require(
            source["database_identity_sha256"] == self._database_identity
            and source["build_sha"] == expected and source["schema"] == self.manifest["source_schema_head"],
            "MIGRATION_DATABASE_IDENTITY_CHANGED",
        )
        server = self._json_query(SERVER_SQL)
        ops.require(server == {"extensions": ["plpgsql"], "subscriptions": 0, "prepared": 0}, "MIGRATION_SERVER_WRITERS_PRESENT")
        server_configuration = self._json_query(SERVER_CONFIGURATION_SQL)
        ops.require(
            type(server_configuration) is dict
            and set(server_configuration) == {
                "unix_socket_directories", "shared_preload_libraries",
                "session_preload_libraries", "local_preload_libraries",
            }
            and server_configuration["unix_socket_directories"] in {
                "/var/run/postgresql", "/run/postgresql", "/tmp",
            }
            and all(server_configuration[key] == "" for key in (
                "shared_preload_libraries", "session_preload_libraries", "local_preload_libraries",
            )),
            "MIGRATION_UNSAFE_POSTGRES_CONFIGURATION",
        )
        mounts = containers["postgres"].get("Mounts") or []
        data = [item for item in mounts if item.get("Destination") == "/var/lib/postgresql/data"]
        ops.require(
            len(mounts) == len(data) == 1 and data[0].get("Type") == "volume"
            and data[0].get("Name") and pg.get("PGDATA") == "/var/lib/postgresql/data",
            "MIGRATION_UNSAFE_POSTGRES_CONFIGURATION",
        )
        self._baseline = {
            "version": 1, "operation_id": self.operation_id,
            "manifest_sha256": manifest_hash(self.manifest), "source_runtime": baseline,
            "compose_configuration_sha256": self._initial_config_hash,
            "environment_sha256": self._root_env_hash,
            "database_identity_sha256": self._database_identity,
            "postgres_data_volume": data[0]["Name"],
        }
        self._inventory = {
            "snapshot": source,
            "source_image_ids": {name: containers[name]["Image"] for name in ops.APP},
            "runtime_identity_sha256": _hash(self._baseline),
        }
        self._captured = True
        return copy.deepcopy(self._inventory)

    def quiet(self) -> None:
        self._unchanged_configuration()
        value = self._json_query(QUIET_SQL)
        ops.require(
            value == {"schemas": [self.manifest["source_schema_head"]], "agent_runs": 0, "publications": 0},
            "MIGRATION_SOURCE_NOT_QUIET",
        )
        redis_id = self.runtime.inspect("redis")["Id"]
        base = ["docker", "exec", redis_id, "redis-cli", "-n", str(self._redis_database), "--raw"]
        for queue in ("ai", "ai_control", "ai_live_test", "publication", "publication_control", "metrics", "unrouted", "celery"):
            for priority in (0, 3, 6, 9):
                key = queue if priority == 0 else queue + "\x06\x16" + str(priority)
                ops.require(ops.command(base + ["LLEN", key]) == "0", "MIGRATION_SOURCE_NOT_QUIET")
        for command, key in (("HLEN", "unacked"), ("ZCARD", "unacked_index")):
            ops.require(ops.command(base + [command, key]) == "0", "MIGRATION_SOURCE_NOT_QUIET")

    def backup(self) -> dict:
        self._unchanged_configuration()
        path = self.state_dir / ("migration-runtime-" + self.operation_id + ".json")
        try:
            with backup.private_file(path, binary=True) as stream:
                stream.write(canonical_bytes(self._baseline))
                stream.flush()
                os.fsync(stream.fileno())
            descriptor = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except FileExistsError:
            raise ops.ReleaseError("MIGRATION_RUNTIME_BASELINE_EXISTS") from None
        return backup.backup_restore(
            self.runtime, self.backup_dir, self.key_dir,
            self.state_dir / ("migration-backup-" + self.operation_id + ".json"),
            {
                "operation_id": self.operation_id, "manifest_sha256": manifest_hash(self.manifest),
                "database_identity_sha256": self._database_identity,
                "source_release_sha": self.manifest["source_release_sha"],
                "source_schema_head": self.manifest["source_schema_head"],
                "source_backend_image": self.manifest["source_images"]["backend"],
            },
        )

    def stop_schedulers(self) -> None:
        self._unchanged_configuration()
        self.writer_barrier.stop_schedulers()

    def drain_workers(self) -> None:
        self.writer_barrier.drain_workers()

    def stop_writers(self) -> None:
        self.writer_barrier.stop_writers()

    def fence_database(self) -> None:
        self.writer_barrier.fence_database()
        self._fenced = True
        self.barrier()

    def barrier(self) -> dict:
        self._unchanged_configuration()
        self.writer_barrier.barrier()
        ops.require(backup.database_identity_hash(self._json_query(IDENTITY_SQL)) == self._database_identity, "MIGRATION_DATABASE_IDENTITY_CHANGED")
        # Known local pg_isready healthchecks are excluded; no other client may
        # retain a transaction after the temporary helpers have been cleaned up.
        for _ in range(20):
            if self._sql(CLIENTS_SQL) == "0":
                # Identity/client probes must themselves stay inside the
                # continuously observed fence, including the final readiness.
                return self.writer_barrier.barrier()
            time.sleep(0.1)
        raise ops.ReleaseError("MIGRATION_DATABASE_CLIENTS_PRESENT")

    def _helper_environment(self, side: str) -> dict[str, str]:
        pg = self._database_environment
        database_url = "postgresql+asyncpg://" + quote(pg["POSTGRES_USER"], safe="") + ":" + quote(pg["POSTGRES_PASSWORD"], safe="") + "@127.0.0.1:5432/" + quote(pg["POSTGRES_DB"], safe="")
        return {
            "APP_ENV": "maintenance", "DATABASE_URL": database_url,
            "BUILD_SHA": self.manifest[side + "_release_sha"],
            "REDIS_URL": "redis://127.0.0.1:6379/15", "WORKER_ROLE": "", "SCHEDULER_ROLE": "",
            "JWT_SECRET": secrets.token_urlsafe(40), "APP_SECRET": secrets.token_urlsafe(40),
            "ADMIN_PASSWORD": secrets.token_urlsafe(40), "ADMIN_EMAIL": "maintenance@example.invalid",
            "VK_PUBLISHING_ENABLED": "false", "TELEGRAM_PUBLISHING_ENABLED": "false",
            "METRICS_SYNC_ENABLED": "false", "VK_METRICS_ENABLED": "false", "TELEGRAM_METRICS_ENABLED": "false",
            "OPENAI_API_KEY": "", "VK_ACCESS_TOKEN": "", "VK_CREDENTIALS_FILE": "",
            "TELEGRAM_BOT_TOKEN": "", "VK_METRICS_ACCESS_TOKEN": "", "TELEGRAM_METRICS_SESSION": "",
            "RUNTIME_DIAGNOSTICS_ENABLED": "false", "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _create(
        self, image: str, argv: list[str], *, bootstrap: bool = False,
        entrypoint: str | None = None, user: str | None = None,
    ) -> str:
        self._counter += 1
        name = f"iteam-migration-{self.operation_id}-{self._counter}"
        ops.require(not ops.command([
            "docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", "name=^/" + name + "$",
        ]), "MIGRATION_HELPER_FAILED")
        # Reserve the name before contacting Docker: the daemon may create a
        # stopped helper even when its response never reaches this process.
        self._pending_names.add(name)
        args = [
            "docker", "create", "--name", name,
            "--label", "iteam.migration.operation=" + self.operation_id,
            "--restart", "no", "--network", "container:" + self.writer_barrier.postgres_id,
            "--pull", "never", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "768m", "--pids-limit", "128", "--tmpfs", "/tmp:rw,nosuid,size=67108864",
        ]
        if bootstrap:
            ops.require(entrypoint is None and user is None, "MIGRATION_HELPER_FAILED")
            args += ["-i", "--entrypoint", "python", image, "-c", BOOTSTRAP]
        else:
            if entrypoint is not None:
                args += ["--entrypoint", entrypoint]
            if user is not None:
                args += ["--user", user]
            args += [image, *argv]
        try:
            identifier = ops.command(args)
            ops.require(CONTAINER.fullmatch(identifier), "MIGRATION_HELPER_FAILED")
        except BaseException:
            # Discovery never starts a helper. If this cleanup also fails, its
            # pending name is retained for containment and operator inspection.
            self._discover_pending_helpers()
            raise
        self._helpers[identifier] = None
        self._pending_names.discard(name)
        # Register before start: the barrier fails on every unexpected start,
        # including a short-lived namespace sharer that disappears between polls.
        self.writer_barrier.allowed_helpers.add(identifier)
        return identifier

    def _discover_pending_helpers(self) -> None:
        error = None
        for name in tuple(self._pending_names):
            try:
                identifiers = ops.command([
                    "docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", "name=^/" + name + "$",
                ]).splitlines()
                ops.require(len(identifiers) <= 1, "MIGRATION_HELPER_CLEANUP_FAILED")
                if identifiers:
                    identifier = identifiers[0]
                    ops.require(CONTAINER.fullmatch(identifier), "MIGRATION_HELPER_CLEANUP_FAILED")
                    value = json.loads(ops.command(["docker", "inspect", identifier]))[0]
                    ops.require(
                        value["Name"].lstrip("/") == name
                        and (value["Config"].get("Labels") or {}).get("iteam.migration.operation") == self.operation_id,
                        "MIGRATION_HELPER_CLEANUP_FAILED",
                    )
                    self._helpers.setdefault(identifier, None)
                self._pending_names.discard(name)
            except BaseException:
                error = ops.ReleaseError("MIGRATION_HELPER_CLEANUP_FAILED")
        if error is not None:
            raise error

    def _remove(self, identifier: str) -> None:
        try:
            value = json.loads(ops.command(["docker", "inspect", identifier]))[0]
            ops.require((value["Config"].get("Labels") or {}).get("iteam.migration.operation") == self.operation_id, "MIGRATION_HELPER_CLEANUP_FAILED")
            ops.command(["docker", "rm", "-f", "-v", identifier])
            remaining = ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", "id=" + identifier])
            ops.require(not remaining, "MIGRATION_HELPER_CLEANUP_FAILED")
        finally:
            process = self._helpers.get(identifier)
            if process is not None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        self._helpers.pop(identifier, None)

    def _run(self, side: str, argv: list[str]) -> str:
        ops.require(self._fenced, "MIGRATION_HELPER_FAILED")
        identifier = self._create(self.manifest[side + "_images"]["backend"], argv, bootstrap=True)
        try:
            result = ops.command(
                ["docker", "start", "--attach", "--interactive", identifier],
                input=json.dumps({"environment": self._helper_environment(side), "argv": argv}),
            )
            state = json.loads(ops.command(["docker", "inspect", identifier]))[0]["State"]
            ops.require(not state["Running"] and state["ExitCode"] == 0 and not state.get("OOMKilled"), "MIGRATION_HELPER_FAILED")
            return result
        finally:
            self._remove(identifier)

    def snapshot(self, tables: list[str] | None = None) -> dict:
        arguments = ["python", "-c", backup.SNAPSHOT_CODE]
        if tables is not None:
            arguments.append(json.dumps(tables))
        value = json.loads(self._run("source", arguments))
        backup.validate_snapshot(value)
        return value

    def target_preflight(self) -> None:
        self.barrier()
        head = self._run("target", ["alembic", "heads"])
        ops.require(head == self.manifest["target_schema_head"] + " (head)", "MIGRATION_TARGET_IMAGE_MISMATCH")
        request = {key: value for key, value in self.manifest.items() if key not in {"version", "release_type", "rehearsal_proof"}}
        request.update(version=1, request_type=drill.REQUEST_TYPE)
        inventory = json.loads(self._run("target", ["python", "-c", drill.IMAGE_MIGRATION_INVENTORY]))
        drill.validate_image_migration_inventory(request, inventory)
        self.barrier()

    def upgrade_exact(self) -> None:
        self.barrier()
        self._run("target", ["alembic", "upgrade", self.manifest["target_schema_head"]])

    def downgrade_exact(self) -> None:
        self.barrier()
        self._run("target", ["alembic", "downgrade", self.manifest["source_schema_head"]])

    def target_checks(self) -> None:
        self._run("target", ["alembic", "check"])
        users = self._json_query("SELECT coalesce(json_agg(json_build_object('id',id::text,'role',role::text,'is_active',is_active) ORDER BY id),'[]'::json) FROM users")
        workspaces = self._json_query("SELECT coalesce(json_agg(json_build_object('id',id::text,'name',name,'slug',slug,'is_default',is_default,'is_active',is_active) ORDER BY id),'[]'::json) FROM workspaces")
        memberships = self._json_query("SELECT coalesce(json_agg(json_build_object('id',id::text,'workspace_id',workspace_id::text,'user_id',user_id::text,'role',role::text,'is_active',is_active) ORDER BY id),'[]'::json) FROM workspace_memberships")
        drill.validate_workspace_invariants(users, workspaces, memberships)

    def _readiness(self, side: str) -> None:
        self.barrier()
        created = []
        try:
            redis_image = self.runtime.inspect("redis")["Image"]
            # Bypass the image's root-to-redis entrypoint: all capabilities are
            # dropped, so changing uid inside that entrypoint would fail.
            redis = self._create(
                redis_image,
                ["--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", "/tmp"],
                entrypoint="redis-server", user="65534:65534",
            )
            created.append(redis)
            ops.command(["docker", "start", redis])
            backend = self._create(self.manifest[side + "_images"]["backend"], [], bootstrap=True)
            created.append(backend)
            process = subprocess.Popen(
                ["docker", "start", "--attach", "--interactive", backend],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True,
            )
            self._helpers[backend] = process
            process.stdin.write(json.dumps({"environment": self._helper_environment(side), "argv": ["python", "-c", READINESS]}))
            process.stdin.close()
            for _ in range(90):
                try:
                    marker = json.loads(ops.command(["docker", "exec", backend, "cat", "/tmp/migration-readiness.json"]))
                    response = json.loads(ops.command([
                        "docker", "exec", backend, "python", "-c",
                        "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read().decode())",
                    ]))
                    if marker == {"status": "PASS", "build_sha": self.manifest[side + "_release_sha"], "transaction_read_only": "on"} and all(response.get(key) == "ok" for key in ("status", "database", "redis", "schema")):
                        break
                except (ops.ReleaseError, ValueError):
                    pass
                time.sleep(1)
            else:
                raise ops.ReleaseError("MIGRATION_READINESS_FAILED")
            frontend = self._create(self.manifest[side + "_images"]["frontend"], [])
            created.append(frontend)
            ops.command(["docker", "start", frontend])
            for _ in range(60):
                try:
                    ops.command([
                        "docker", "exec", frontend, "node", "-e",
                        "fetch('http://127.0.0.1:3000/publications',{signal:AbortSignal.timeout(3000)}).then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))",
                    ])
                    break
                except ops.ReleaseError:
                    time.sleep(1)
            else:
                raise ops.ReleaseError("MIGRATION_READINESS_FAILED")
        finally:
            cleanup_error = None
            for identifier in reversed(created):
                try:
                    self._remove(identifier)
                except Exception:
                    cleanup_error = ops.ReleaseError("MIGRATION_HELPER_CLEANUP_FAILED")
            if cleanup_error is not None:
                raise cleanup_error from None
        self.barrier()

    def maintenance_readiness(self) -> None:
        self._readiness("target")

    def source_checks(self) -> None:
        self._run("source", ["alembic", "check"])
        absent = self._json_query("SELECT json_build_object('workspaces',to_regclass('public.workspaces') IS NULL,'memberships',to_regclass('public.workspace_memberships') IS NULL,'role_type',NOT EXISTS(SELECT 1 FROM pg_type WHERE typname='workspace_role'))")
        ops.require(absent == {"workspaces": True, "memberships": True, "role_type": True}, "MIGRATION_SOURCE_CHANGED")
        self._readiness("source")

    def restore_source_configuration(self) -> None:
        # All migration commands used separate helpers. Original source image
        # IDs, checkout and .env were never replaced and remain stopped.
        self._unchanged_configuration()
        for name, image in self._inventory["source_image_ids"].items():
            current = self.runtime.inspect(name)
            ops.require(current["Image"] == image and not current["State"]["Running"], "MIGRATION_SOURCE_CHANGED")
        self.barrier()

    def contain(self) -> None:
        cleanup_error = None
        try:
            self._discover_pending_helpers()
        except BaseException:
            cleanup_error = ops.ReleaseError("MIGRATION_HELPER_CLEANUP_FAILED")
        for identifier in list(self._helpers):
            try:
                self._remove(identifier)
            except BaseException:
                cleanup_error = ops.ReleaseError("MIGRATION_HELPER_CLEANUP_FAILED")
        # A failed helper removal must not prevent the attempt to stop writers
        # and sever PostgreSQL's networks. Continuity still fails closed.
        try:
            self.writer_barrier.contain()
            self._fenced = True
            self.barrier()
        finally:
            if cleanup_error is not None:
                raise cleanup_error from None

    def close(self) -> None:
        error = None
        try:
            self._discover_pending_helpers()
        except BaseException:
            error = ops.ReleaseError("MIGRATION_HELPER_CLEANUP_FAILED")
        for identifier in list(self._helpers):
            try:
                self._remove(identifier)
            except BaseException:
                error = ops.ReleaseError("MIGRATION_HELPER_CLEANUP_FAILED")
        self.writer_barrier.close()
        if error is not None:
            raise error


def production_runtime(root: Path, manifest: dict, operation_id: str, state: Path, backup_dir: Path, key_dir: Path) -> MigrationRuntime:
    drill.require_clean_compose_environment()
    ops.require(root.is_absolute() and root.is_dir() and not root.is_symlink(), "MIGRATION_UNSAFE_PRODUCTION_ROOT")
    root = root.resolve()
    env_path = root / ".env"
    metadata = env_path.stat()
    ops.require(not env_path.is_symlink() and metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o600, "MIGRATION_UNSAFE_PRODUCTION_ROOT")
    content = env_path.read_text()
    ops.require(ops.env_value(content, "APP_ENV") in {"production", "prod"} and ops.env_value(content, "BUILD_SHA") == manifest["source_release_sha"], "MIGRATION_UNSAFE_PRODUCTION_ROOT")
    ops.require(
        ops.command(["git", "rev-parse", "HEAD"], cwd=root) == manifest["source_release_sha"]
        and not ops.command(["git", "status", "--porcelain"], cwd=root),
        "MIGRATION_UNSAFE_PRODUCTION_ROOT",
    )
    files = []
    for entry in (ops.env_value(content, "COMPOSE_FILE") or "docker-compose.yml").split(os.pathsep):
        path = Path(entry)
        if not path.is_absolute():
            path = root / path
        ops.require(path.is_file() and not path.is_symlink() and (path.resolve() == root / "docker-compose.yml" or path.resolve().parent == state.resolve()), "MIGRATION_UNSAFE_PRODUCTION_COMPOSE")
        files.append(path.resolve())
    runtime = ExplicitRuntime(root, "iteam-ai-marketing", files)
    config = json.loads(runtime.compose("config", "--format", "json"))
    ops.require(config.get("name") == "iteam-ai-marketing" and set(ops.ALL) <= set(config.get("services", {})), "MIGRATION_UNSAFE_PRODUCTION_COMPOSE")
    for path in (state, backup_dir, key_dir):
        ops.require(path.is_absolute() and not path.is_symlink() and root != path.resolve() and root not in path.resolve().parents, "MIGRATION_UNSAFE_PRODUCTION_ROOT")
    return MigrationRuntime(runtime, manifest, operation_id, state, backup_dir, key_dir)
