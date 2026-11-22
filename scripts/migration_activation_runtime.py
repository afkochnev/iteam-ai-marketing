"""Same-operation target activation and physical post-activation containment.

This adapter never authenticates an approval or resumes an operation. Its caller
holds the release lock and supplies the live activation journal. Production
configuration is derived from the exact accepted target checkout; a separate,
strictly scoped fixture constructor retains the disposable topology.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import migration_apply_backup as backup
import migration_apply_runtime as apply_runtime
import release_ops as ops
from migration_activation_contract import activation_policy, hash_json
from migration_apply_state import canonical_bytes, manifest_hash
from migration_writer_barrier import EventAudit

ERROR_CODES = frozenset({
    "ACTIVATION_RUNTIME_PHASE_INVALID", "ACTIVATION_RUNTIME_PROCESS_CHANGED",
    "ACTIVATION_PLAN_UNSAFE", "ACTIVATION_PLAN_CHANGED", "ACTIVATION_CONFIG_CHANGED",
    "ACTIVATION_TARGET_CONFIG_INVALID", "ACTIVATION_TARGET_POLICY_CHANGED",
    "ACTIVATION_TARGET_IMAGE_CHANGED", "ACTIVATION_TARGET_IDENTITY_CHANGED",
    "ACTIVATION_DATA_IDENTITY_CHANGED", "ACTIVATION_PASSIVE_CHECK_FAILED",
    "ACTIVATION_HELPER_FAILED", "ACTIVATION_HELPER_CLEANUP_FAILED",
    "ACTIVATION_OBSERVER_LOST", "ACTIVATION_UNEXPECTED_RUNTIME_EVENT",
    "ACTIVATION_TARGET_NOT_READY", "ACTIVATION_QUEUES_NOT_READY",
    "ACTIVATION_CONTAINMENT_FAILED", "ACTIVATION_FULL_SNAPSHOT_INVALID",
    "ACTIVATION_UNSAFE_FIXTURE", "ACTIVATION_RUNTIME_COMMAND_FAILED",
})
ID = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
FLAGS = (
    "TELEGRAM_PUBLISHING_ENABLED", "VK_PUBLISHING_ENABLED",
    "METRICS_SYNC_ENABLED", "VK_METRICS_ENABLED", "TELEGRAM_METRICS_ENABLED",
)
META_ENV = {"BUILD_SHA", "COMPOSE_FILE", "RUNTIME_ADMISSION_MODE"}
LABEL = "iteam.migration.activation"
FIXTURE_PROJECT = re.compile(r"iteam-activation-drill-[0-9a-f]{10}\Z")
FIXTURE_DIRECTORY = re.compile(r"iteam-migration-activation-drill-[a-z0-9_-]{8}\Z")
WORKERS = tuple(name for name in ops.APP if name.endswith("_worker"))
MUTATING_EVENTS = {"start", "restart", "die", "destroy", "kill", "pause", "unpause", "update", "rename"}
RECREATE_STEPS = frozenset({
    "permit", "data_identity", "redis_restart_policy", "checkout",
    "staged_environment", "compose_create", "target_identity", "event_sync",
    "fenced_topology", "complete",
})
TARGET_IDENTITY_FIELDS = frozenset({
    "service_instance", "replacement_id", "name", "image_id", "image_revision",
    "compose_project", "compose_service", "operation", "environment", "command",
    "entrypoint", "running", "paused", "restarting", "oom_killed",
})

FULL_SNAPSHOT_CODE = r'''
import asyncio,hashlib,json,os,re
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
def hashed(value): return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
async def main():
 e=create_async_engine(os.environ['DATABASE_URL'],poolclass=NullPool,connect_args={'timeout':5,'server_settings':{'default_transaction_read_only':'on','statement_timeout':'10000','application_name':'iteam-activation-snapshot'}})
 try:
  async with e.connect() as c:
   c=await c.execution_options(isolation_level='REPEATABLE READ')
   async with c.begin():
    assert await c.scalar(text('SHOW transaction_read_only'))=='on'
    names=(await c.execute(text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars().all()
    assert names and all(re.fullmatch(r'[a-z_][a-z0-9_]{0,62}',n) for n in names)
    rows={n:sorted((await c.execute(text('SELECT to_jsonb(t)::text FROM public."'+n+'" AS t'))).scalars().all()) for n in names}
    identity=(await c.execute(text("SELECT (SELECT system_identifier::text FROM pg_control_system()),(SELECT oid::bigint FROM pg_database WHERE datname=current_database()),current_database()"))).one()
    identity=dict(zip(('system_identifier','database_oid','database_name'),identity))
    schemas=(await c.execute(text('SELECT version_num FROM alembic_version'))).scalars().all()
    assert len(schemas)==1
    print(json.dumps({'schema':schemas[0],'database_identity':identity,'all_tables_sha256':hashed(rows),'table_counts':{n:len(v) for n,v in rows.items()}},sort_keys=True))
 finally: await e.dispose()
asyncio.run(main())
'''

PASSIVE_BOOT = r'''
import json
from pathlib import Path
from app.core.config import settings
from app.core.runtime_admission import admission_status
assert settings.runtime_admission_mode=='passive'
settings.validate_production()
from app.main import app
status=admission_status()
assert status['admission_protocol']==1 and status['admission_mode']=='passive' and status['business_ready'] is False
Path('/tmp/activation-passive.json').write_text(json.dumps(status))
import uvicorn
uvicorn.run(app,host='127.0.0.1',port=8000,log_level='warning',access_log=False)
'''

PASSIVE_CLI = r'''
import importlib,json
from app.core.config import settings
settings.validate_worker_capabilities()
settings.validate_scheduler_capabilities()
settings.validate_redis_isolation()
from app.core.runtime_admission import RuntimeAdmissionError,admission_status
try: importlib.import_module('app.workers.celery_app')
except RuntimeAdmissionError as error: assert str(error)=='RUNTIME_PASSIVE'
else: raise AssertionError('PASSIVE_CELERY_ADMITTED')
print(json.dumps({'status':'PASS',**admission_status()}))
'''

QUEUE_CODE = r'''
import json,sys
from app.workers.celery_app import celery_app
expected=json.loads(sys.argv[1])
actual=celery_app.control.inspect(timeout=3).active_queues() or {}
valid=set(actual)==set(expected) and all(type(actual[n]) is list and sorted(x.get('name') for x in actual[n])==[expected[n]] for n in expected)
print(json.dumps({'exact_queues_verified':valid}))
'''


def _require(value: Any, code: str) -> None:
    ops.require(value, code)


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _environment(value: dict) -> dict[str, str]:
    return apply_runtime._environment(value)


def _effective_environment(image: dict, configured: dict) -> dict[str, str]:
    result = _environment({"Config": image})
    for key, value in (configured.get("environment") or {}).items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = str(value)
    return result


def _identity(value: os.stat_result) -> tuple:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _private_read(path: Path) -> tuple[bytes, tuple]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                 and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
                 and before.st_size <= 512 * 1024, "ACTIVATION_PLAN_UNSAFE")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(512 * 1024 + 1)
        after, current = os.fstat(descriptor), path.lstat()
        _require(_identity(before) == _identity(after) == _identity(current)
                 and len(content) == before.st_size, "ACTIVATION_PLAN_CHANGED")
        return content, _identity(after)
    finally:
        os.close(descriptor)


def _write_once(path: Path, value: bytes) -> tuple:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        content, identity = _private_read(path)
        _require(content == value, "ACTIVATION_PLAN_CHANGED")
        return identity
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    content, identity = _private_read(path)
    _require(content == value, "ACTIVATION_PLAN_CHANGED")
    return identity


def _safe_configuration(value: dict, project: str) -> None:
    _require(type(value) is dict and set(value) <= {"name", "services", "networks", "volumes", "secrets", "configs"}
             and value.get("name") == project and not value.get("secrets") and not value.get("configs"),
             "ACTIVATION_TARGET_CONFIG_INVALID")
    services = value.get("services")
    _require(type(services) is dict and set(ops.ALL) <= set(services)
             and set(services) <= {*ops.ALL, "vk_credentials_admin"}, "ACTIVATION_TARGET_CONFIG_INVALID")
    for service in services.values():
        _require(type(service) is dict and not set(service) & {
            "pre_start", "post_start", "pre_stop", "post_stop", "develop", "hooks",
            "provider", "use_api_socket", "volumes_from", "external_links", "extends",
        }, "ACTIVATION_TARGET_CONFIG_INVALID")
        _require(not service.get("privileged") and not service.get("network_mode"),
                 "ACTIVATION_TARGET_CONFIG_INVALID")
        deploy = service.get("deploy") or {}
        _require(type(deploy) is dict and deploy.get("mode", "replicated") == "replicated"
                 and deploy.get("replicas", 1) == 1 and service.get("scale", 1) == 1,
                 "ACTIVATION_TARGET_CONFIG_INVALID")
    for name in ops.APP:
        environment = services[name].get("environment") or {}
        _require(type(environment) is dict and all(type(k) is str and type(v) is str for k, v in environment.items()),
                 "ACTIVATION_TARGET_CONFIG_INVALID")


def _normalized_configuration(value: dict) -> dict:
    result = copy.deepcopy(value)
    for name, service in result["services"].items():
        if name in ops.APP or name == "vk_credentials_admin":
            service.pop("image", None)
            environment = service.setdefault("environment", {})
            for key in META_ENV:
                environment.pop(key, None)
            if name == "vk_credentials_admin":
                environment.pop("APP_ENV", None)
            build = service.get("build")
            if type(build) is dict and type(build.get("args")) is dict:
                build["args"].pop("BUILD_SHA", None)
            labels = service.get("labels") or {}
            labels.pop(LABEL, None)
            if labels:
                service["labels"] = labels
            else:
                service.pop("labels", None)
    return result


def validate_target_configuration(source: dict, target: dict, project: str, target_sha: str) -> None:
    _safe_configuration(source, project)
    _safe_configuration(target, project)
    _require(_normalized_configuration(source) == _normalized_configuration(target),
             "ACTIVATION_TARGET_POLICY_CHANGED")
    for name in ops.APP:
        service = target["services"][name]
        environment = service.get("environment") or {}
        _require(environment.get("BUILD_SHA") == target_sha
                 and environment.get("RUNTIME_ADMISSION_MODE") == "active", "ACTIVATION_TARGET_CONFIG_INVALID")
        if name == "frontend":
            continue
        _require(all(str(environment.get(flag, "false")).lower() == "false" for flag in FLAGS),
                 "ACTIVATION_TARGET_POLICY_CHANGED")
        worker_role = name.removesuffix("_worker") if name in WORKERS else ""
        scheduler_role = name.removesuffix("_scheduler") if name in ops.SCHEDULERS else ""
        _require((environment.get("WORKER_ROLE") or "") == worker_role
                 and (environment.get("SCHEDULER_ROLE") or "") == scheduler_role,
                 "ACTIVATION_TARGET_CONFIG_INVALID")
        command = service.get("command") or []
        _require(type(command) is list and all(type(part) is str for part in command),
                 "ACTIVATION_TARGET_CONFIG_INVALID")
        if name in WORKERS:
            queues = []
            for index, part in enumerate(command):
                if part.startswith("--queues="):
                    queues.append(part.split("=", 1)[1])
                elif part in {"--queues", "-Q"} and index + 1 < len(command):
                    queues.append(command[index + 1])
            _require("worker" in command and "beat" not in command and queues == [worker_role],
                     "ACTIVATION_TARGET_CONFIG_INVALID")
        elif name in ops.SCHEDULERS:
            _require("beat" in command and "worker" not in command, "ACTIVATION_TARGET_CONFIG_INVALID")


class TargetAudit(EventAudit):
    """Sticky lifecycle witness with narrowly registered activation transitions."""

    def __init__(self, operation: str, project: str, source_ids: set[str], data_ids: set[str], helpers: set[str],
                 target_networks: dict[str, set[str]] | None = None,
                 recreations: dict[str, dict[str, str]] | None = None):
        super().__init__(operation + "-target", source_ids | data_ids, next(iter(data_ids)), helpers)
        self.project, self.operation = project, operation
        self.data_ids = data_ids
        self.old_ids = source_ids
        self.target_ids: set[str] = set()
        self.target_networks = target_networks or {}
        self.recreations = copy.deepcopy(recreations or {})
        self.rename_permits: dict[str, tuple[str, str, str]] = {}
        self.expected: set[tuple[str, str, str]] = set()
        self.creating: set[str] = set()
        self.removing_old = False
        self.sealed = True

    def allow(self, kind: str, action: str, identifier: str) -> None:
        with self.condition:
            self.expected.add((kind, action, identifier))

    def finish_connections(self, identifiers: set[str]) -> None:
        with self.condition:
            self.expected = {item for item in self.expected if not (
                item[0:2] == ("network", "connect") and item[2].split(":", 1)[-1] in identifiers)}

    def observe(self, value: Any) -> None:
        with self.condition:
            if not (type(value) is dict and type(value.get("Type")) is str
                    and type(value.get("Action")) is str and type(value.get("Actor")) is dict
                    and type(value["Actor"].get("ID")) is str):
                self.healthy = False
                self.condition.notify_all()
                return
            kind, action, actor = value["Type"], value["Action"], value["Actor"]
            identifier = actor["ID"]
            attributes = actor.get("Attributes") or {}
            self.events_observed += 1
            if kind == "volume" and identifier in self.volumes and action in {"create", "destroy"}:
                self.seen.add((identifier, action))
            if type(attributes) is not dict:
                self.healthy = False
            elif kind == "network" and action in {"connect", "disconnect"}:
                member = attributes.get("container")
                if member in self.protected | self.target_ids:
                    key = ("network", action, identifier + ":" + member)
                    if key in self.expected:
                        self.expected.discard(key)
                    elif self.removing_old and member in self.old_ids and action == "disconnect":
                        pass
                    else:
                        self.healthy = False
            elif kind == "container":
                key = (kind, action, identifier)
                if action == "create" and attributes.get("com.docker.compose.project") == self.project:
                    service = attributes.get("com.docker.compose.service")
                    recreation = self.recreations.get(service)
                    temporary = (recreation["source_id"][:12] + "_" + recreation["final_name"]) if recreation else None
                    if (not self.removing_old or service not in self.creating or attributes.get(LABEL) != self.operation
                            or recreation is None or attributes.get("name") != temporary):
                        self.healthy = False
                    else:
                        self.creating.discard(service)
                        self.target_ids.add(identifier)
                        # Compose creates a stopped replacement under a
                        # temporary name, removes the source, then renames the
                        # replacement once. This is not permission to rename a
                        # started target or any other container.
                        self.rename_permits[identifier] = (temporary, recreation["final_name"], service)
                        self.expected.update(("network", "connect", network + ":" + identifier)
                                             for network in self.target_networks.get(service, set()))
                elif action == "rename" and identifier in self.rename_permits:
                    temporary, final, service = self.rename_permits[identifier]
                    if (self.removing_old and attributes.get("com.docker.compose.project") == self.project
                            and attributes.get("com.docker.compose.service") == service
                            and attributes.get(LABEL) == self.operation
                            and attributes.get("oldName") == "/" + temporary and attributes.get("name") == final):
                        del self.rename_permits[identifier]
                    else:
                        self.healthy = False
                elif action in MUTATING_EVENTS:
                    if identifier in self.allowed_helpers:
                        pass
                    elif key in self.expected:
                        self.expected.discard(key)
                    elif self.removing_old and identifier in self.old_ids and action in {"rename", "destroy", "kill", "die"}:
                        pass
                    elif identifier in self.protected | self.target_ids or action in {"start", "restart", "unpause"}:
                        self.healthy = False
            self.condition.notify_all()

    def check(self) -> None:
        _require(self.healthy and not self.closed and self.process is not None and self.process.poll() is None,
                 "ACTIVATION_OBSERVER_LOST")

    def close(self) -> None:
        super().close()
        _require((self.process is None or self.process.poll() is not None)
                 and (self.thread is None or not self.thread.is_alive()), "ACTIVATION_OBSERVER_LOST")


class MigrationActivationRuntime:
    def __init__(self, source: apply_runtime.MigrationRuntime, *, fixture: bool = False, target_compose: dict | None = None):
        self.source = source
        self.operation_id = source.operation_id
        self.root, self.state_dir = source.runtime.root, source.state_dir
        self.fixture = fixture
        self._fixture_compose = copy.deepcopy(target_compose)
        _require(type(fixture) is bool and (fixture or target_compose is None), "ACTIVATION_UNSAFE_FIXTURE")
        if fixture:
            project = getattr(source.runtime, "project", "")
            metadata = self.root.stat()
            _require(FIXTURE_PROJECT.fullmatch(project) and FIXTURE_DIRECTORY.fullmatch(self.root.name)
                     and self.root.resolve() == self.root and not self.root.is_symlink()
                     and metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
                     "ACTIVATION_UNSAFE_FIXTURE")
        self.project = getattr(source.runtime, "project", "")
        _require(FIXTURE_PROJECT.fullmatch(self.project) if fixture else self.project == "iteam-ai-marketing",
                 "ACTIVATION_PLAN_UNSAFE")
        self._owner_pid = os.getpid()
        self._plan: dict = {}
        self._files: dict[Path, tuple[bytes, tuple]] = {}
        self._source_config: dict = {}
        self._target_config: dict = {}
        self._staged_config: dict = {}
        self._image_config: dict[str, dict] = {}
        self._data: dict[str, dict] = {}
        self._environment = b""
        self._environment_identity: tuple | None = None
        self._crossed = False
        self._recreated = False
        self._fenced = False
        self._persisted = False
        self._closed = False
        self._close_verified = False
        self._started: set[str] = set()
        self.recreate_step: str | None = None
        self.target_service: str | None = None
        self.target_identity_field: str | None = None
        self.target_ids: dict[str, str] = {}
        self.audit: TargetAudit | None = None
        self.target_runtime: apply_runtime.ExplicitRuntime | None = None
        self._final_runtime: apply_runtime.ExplicitRuntime | None = None
        self._base_path = self.state_dir / ("activation-compose-" + self.operation_id + ".yaml")
        self._staged_path = self.state_dir / ("activation-staged-" + self.operation_id + ".json")
        self._final_path = self.state_dir / ("activation-final-" + self.operation_id + ".json")

    @property
    def plan(self) -> dict:
        return copy.deepcopy(self._plan)

    def _owner(self) -> None:
        _require(os.getpid() == self._owner_pid, "ACTIVATION_RUNTIME_PROCESS_CHANGED")

    def _record_file(self, path: Path, content: bytes) -> None:
        self._files[path] = (content, _write_once(path, content))

    def _check_plan(self) -> None:
        self._owner()
        _require(bool(self._plan), "ACTIVATION_RUNTIME_PHASE_INVALID")
        _require(manifest_hash(self.source.manifest) == self._plan["manifest_sha256"], "ACTIVATION_PLAN_CHANGED")
        for path, (expected, identity) in self._files.items():
            content, current = _private_read(path)
            _require(content == expected and current == identity, "ACTIVATION_PLAN_CHANGED")
        content, identity = _private_read(self.source.runtime.env_path)
        _require(content == self._environment and identity == self._environment_identity, "ACTIVATION_CONFIG_CHANGED")
        if not self._crossed:
            self.source._unchanged_configuration()

    def _permit(self, state) -> None:
        self._check_plan()
        state.assert_activation_permitted()
        _require(self._crossed and self.audit is not None and not self._closed, "ACTIVATION_RUNTIME_PHASE_INVALID")
        self.audit.sync()

    def _inspect(self, identifier: str) -> dict:
        _require(ID.fullmatch(identifier), "ACTIVATION_TARGET_IDENTITY_CHANGED")
        value = json.loads(ops.command(["docker", "inspect", identifier]))
        _require(type(value) is list and len(value) == 1 and value[0].get("Id") == identifier,
                 "ACTIVATION_TARGET_IDENTITY_CHANGED")
        return value[0]

    def _verify_data(self) -> None:
        _require(set(self._data) == {"postgres", "redis"}, "ACTIVATION_DATA_IDENTITY_CHANGED")
        for name, baseline in self._data.items():
            value = self._inspect(baseline["Id"])
            _require(value["Image"] == baseline["Image"]
                     and value["State"]["Running"] is True and not value["State"].get("Paused")
                     and not value["State"].get("Restarting") and not value["State"].get("OOMKilled")
                     and value["State"]["StartedAt"] == baseline["State"]["StartedAt"]
                     and value["RestartCount"] == baseline["RestartCount"]
                     and value.get("Mounts") == baseline.get("Mounts"), "ACTIVATION_DATA_IDENTITY_CHANGED")
        _require(backup.database_identity_hash(self.source._json_query(apply_runtime.IDENTITY_SQL)) == self.source._database_identity,
                 "ACTIVATION_DATA_IDENTITY_CHANGED")

    def _declared_networks(self, name: str, actual: dict, configured: dict) -> None:
        """Only networks whose IDs are in the signed PostgreSQL baseline.

        App-only networks are deliberately unsupported: their replacement
        would not change the unchanged v1 runtime identity hash.
        """
        declared = configured["services"][name].get("networks") or {}
        definitions = configured.get("networks") or {}
        anchors = self.source.writer_barrier.safe_baseline["postgres_networks"]
        _require(type(declared) is dict and bool(declared), "ACTIVATION_TARGET_POLICY_CHANGED")
        expected = {}
        for key, options in declared.items():
            options = {} if options is None else options
            definition = definitions.get(key) or {}
            physical = definition.get("name")
            _require(physical in anchors and type(options) is dict, "ACTIVATION_TARGET_POLICY_CHANGED")
            expected[physical] = options
        observed = actual["NetworkSettings"].get("Networks") or {}
        _require(set(observed) == set(expected), "ACTIVATION_TARGET_POLICY_CHANGED")
        for physical, options in expected.items():
            endpoint = observed[physical]
            _require(endpoint.get("NetworkID") == anchors[physical]["NetworkID"]
                     and not endpoint.get("DriverOpts") and not endpoint.get("Links"), "ACTIVATION_TARGET_POLICY_CHANGED")
            aliases = options.get("aliases") or []
            automatic = {name, actual["Name"].lstrip("/"), actual["Id"][:12]}
            if configured["services"][name].get("hostname"):
                automatic.add(configured["services"][name]["hostname"])
            actual_aliases = set(endpoint.get("Aliases") or [])
            _require(type(aliases) is list and all(type(value) is str for value in aliases)
                     and set(aliases) <= actual_aliases <= set(aliases) | automatic,
                     "ACTIVATION_TARGET_POLICY_CHANGED")
            ipam = {key: value for key, value in (endpoint.get("IPAMConfig") or {}).items() if value not in (None, "", [])}
            expected_ipam = {engine: options[compose] for compose, engine in (
                ("ipv4_address", "IPv4Address"), ("ipv6_address", "IPv6Address")) if options.get(compose)}
            _require(not options.get("link_local_ips") and ipam == expected_ipam,
                     "ACTIVATION_TARGET_POLICY_CHANGED")

    def _source_topology(self, configured: dict) -> None:
        for name in sorted(ops.ALL):
            actual = self.source.writer_barrier._raw[name]
            self._declared_networks(name, actual, configured)
            service, host = configured["services"][name], actual["HostConfig"]
            memory = service.get("mem_limit", 0)
            if type(memory) is str:
                match = re.fullmatch(r"([0-9]+)([bkmg]?)", memory.lower())
                _require(match is not None, "ACTIVATION_TARGET_CONFIG_INVALID")
                memory = int(match[1]) * {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[match[2]]
            cpus = int(Decimal(str(service.get("cpus", 0))) * 1_000_000_000)
            _require(type(memory) is int and host.get("Memory", 0) == memory
                     and host.get("NanoCpus", 0) == cpus, "ACTIVATION_TARGET_POLICY_CHANGED")
            if service.get("pids_limit") is not None:
                _require(host.get("PidsLimit") == service["pids_limit"], "ACTIVATION_TARGET_POLICY_CHANGED")

    def _target_names(self, configured: dict) -> dict[str, str]:
        result = {}
        for name in ops.APP:
            original = self.source.writer_barrier._raw[name]
            number = (original["Config"].get("Labels") or {}).get("com.docker.compose.container-number", "")
            _require(type(number) is str and re.fullmatch(r"[1-9][0-9]*", number), "ACTIVATION_TARGET_CONFIG_INVALID")
            expected = configured["services"][name].get("container_name") or self.project + "-" + name + "-" + number
            _require(type(expected) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", expected)
                     and original.get("Name") == "/" + expected, "ACTIVATION_TARGET_POLICY_CHANGED")
            result[name] = expected
        return result

    def _namespace_members(self) -> None:
        """Check both data namespaces without requiring retired source IDs."""
        identifiers = ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines()
        _require(identifiers and all(ID.fullmatch(value) for value in identifiers),
                 "ACTIVATION_DATA_IDENTITY_CHANGED")
        fmt = '[{{json .Id}},{{json .Name}},{{json .HostConfig.NetworkMode}},{{json .HostConfig.RestartPolicy.Name}}]'
        rows = [json.loads(line) for line in ops.command(["docker", "inspect", "--format", fmt, *identifiers]).splitlines()]
        _require(len(rows) == len(identifiers) and all(
            type(row) is list and len(row) == 4 and all(type(item) is str for item in row)
            and ID.fullmatch(row[0]) for row in rows), "ACTIVATION_DATA_IDENTITY_CHANGED")
        by_id = {row[0]: row for row in rows}
        data_ids = {value["Id"] for value in self._data.values()}
        _require(len(by_id) == len(rows) and data_ids <= set(by_id), "ACTIVATION_DATA_IDENTITY_CHANGED")

        def resolve(reference: str) -> str:
            matches = [identifier for identifier, row in by_id.items()
                       if identifier.startswith(reference) or row[1].lstrip("/") == reference]
            _require(len(matches) == 1, "ACTIVATION_DATA_IDENTITY_CHANGED")
            return matches[0]

        for identifier, row in by_id.items():
            seen, cursor = set(), identifier
            while by_id[cursor][2].startswith("container:"):
                _require(cursor not in seen, "ACTIVATION_DATA_IDENTITY_CHANGED")
                seen.add(cursor)
                cursor = resolve(by_id[cursor][2].split(":", 1)[1])
            if cursor in data_ids and identifier != cursor:
                _require(cursor == self._data["postgres"]["Id"]
                         and identifier in self.source._helpers and row[3] == "no",
                         "ACTIVATION_DATA_IDENTITY_CHANGED")

    def _project_members(self) -> None:
        expected = {*self.target_ids.values(), *(value["Id"] for value in self._data.values())}
        current = ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc",
                               "--filter", "label=com.docker.compose.project=" + self.project]).splitlines()
        _require(set(self.target_ids) == set(ops.APP) and len(current) == len(expected)
                 and set(current) == expected, "ACTIVATION_TARGET_IDENTITY_CHANGED")

    @staticmethod
    def _verify_observer_closed(observer) -> None:
        if observer is not None:
            _require(observer.closed and (observer.process is None or observer.process.poll() is not None)
                     and (observer.thread is None or not observer.thread.is_alive()) and not observer.volumes,
                     "ACTIVATION_OBSERVER_LOST")

    def prepare_target_plan(self) -> dict:
        self._owner()
        _require(self.source._captured and not self._crossed and not self._closed, "ACTIVATION_RUNTIME_PHASE_INVALID")
        if self._plan:
            self._check_plan()
            return self.plan
        self.source._unchanged_configuration()
        self._environment, self._environment_identity = _private_read(self.source.runtime.env_path)
        configured = json.loads(self.source.runtime.compose("config", "--format", "json"))
        _require(configured.get("name") == self.project, "ACTIVATION_TARGET_CONFIG_INVALID")
        _safe_configuration(configured, self.project)
        self._source_topology(configured)
        if self.fixture:
            _require(FIXTURE_PROJECT.fullmatch(self.project), "ACTIVATION_UNSAFE_FIXTURE")
            raw = canonical_bytes(self._fixture_compose if self._fixture_compose is not None else ops.load(self.root / "compose.json"))
        else:
            _require(self.project == "iteam-ai-marketing", "ACTIVATION_PLAN_UNSAFE")
            raw = (ops.command(["git", "show", self.source.manifest["target_release_sha"] + ":docker-compose.yml"], cwd=self.root) + "\n").encode()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = self.state_dir.stat()
        _require(self.state_dir.resolve() == self.state_dir and not self.state_dir.is_symlink()
                 and metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
                 "ACTIVATION_PLAN_UNSAFE")
        self._record_file(self._base_path, raw)
        final_compose = os.pathsep.join((str(self._base_path), str(self._final_path)))
        services = {}
        target_sha = self.source.manifest["target_release_sha"]
        for name in (*ops.APP, *(('vk_credentials_admin',) if 'vk_credentials_admin' in configured['services'] else ())):
            side = "frontend" if name == "frontend" else "backend"
            environment = {"BUILD_SHA": target_sha, "COMPOSE_FILE": final_compose, "RUNTIME_ADMISSION_MODE": "active"}
            service = {"image": self.source.manifest["target_images"][side], "environment": environment}
            if name in ops.APP:
                service["labels"] = {LABEL: self.operation_id}
            if "build" in configured["services"][name]:
                service["build"] = {"args": {"BUILD_SHA": target_sha}}
            services[name] = service
        final = {"services": services}
        staged = copy.deepcopy(final)
        for name in ops.APP:
            staged["services"][name]["restart"] = "no"
        self._record_file(self._staged_path, canonical_bytes(staged))
        self._record_file(self._final_path, canonical_bytes(final))
        self.target_runtime = apply_runtime.ExplicitRuntime(self.root, self.project, [self._base_path, self._staged_path])
        self._final_runtime = apply_runtime.ExplicitRuntime(self.root, self.project, [self._base_path, self._final_path])
        target = json.loads(self._final_runtime.compose("config", "--format", "json"))
        validate_target_configuration(configured, target, self.project, target_sha)
        staged_config = json.loads(self.target_runtime.compose("config", "--format", "json"))
        expected_staged = copy.deepcopy(target)
        for name in ops.APP:
            expected_staged["services"][name]["restart"] = "no"
        _require(staged_config == expected_staged, "ACTIVATION_TARGET_CONFIG_INVALID")
        self._source_config, self._target_config, self._staged_config = configured, target, staged_config
        self._data = {name: copy.deepcopy(self.source.writer_barrier._raw[name]) for name in ("postgres", "redis")}
        for side, reference in self.source.manifest["target_images"].items():
            value = json.loads(ops.command(["docker", "image", "inspect", reference]))[0]
            _require(value["Id"] == self.source._images[reference]
                     and (value["Config"].get("Labels") or {}).get("org.opencontainers.image.revision") == target_sha,
                     "ACTIVATION_TARGET_IMAGE_CHANGED")
            self._image_config[side] = value["Config"]
        for name in ops.APP:
            if name == "frontend":
                continue
            environment = _effective_environment(self._image_config["backend"], target["services"][name])
            _require(all(environment.get(flag, "false").lower() == "false" for flag in FLAGS),
                     "ACTIVATION_TARGET_POLICY_CHANGED")
            if self.fixture:
                from migration_apply_drill import PROVIDER_CREDENTIALS
                _require(environment.get("APP_ENV") == "development"
                         and not any(environment.get(key) for key in PROVIDER_CREDENTIALS),
                         "ACTIVATION_UNSAFE_FIXTURE")
            else:
                _require(environment.get("APP_ENV") in {"production", "prod"},
                         "ACTIVATION_TARGET_CONFIG_INVALID")
        if self.fixture:
            networks = target.get("networks") or {}
            _require(set(networks) == {"default", "host_publish"}
                     and networks["default"].get("internal") is True
                     and networks["host_publish"].get("internal", False) is False
                     and all(not item.get("external") and item.get("driver", "bridge") == "bridge" for item in networks.values()),
                     "ACTIVATION_UNSAFE_FIXTURE")
            for name, service in target["services"].items():
                _require(set(service.get("networks") or {}) == ({"default", "host_publish"} if name == "postgres" else {"default"})
                         and (name == "postgres" or not service.get("ports")), "ACTIVATION_UNSAFE_FIXTURE")
        self._plan = {
            "version": 1, "operation_id": self.operation_id, "project": self.project,
            "manifest_sha256": manifest_hash(self.source.manifest),
            "runtime_identity_sha256": self.source._inventory["runtime_identity_sha256"],
            "target_release_sha": target_sha, "target_schema_head": self.source.manifest["target_schema_head"],
            "activation_policy_sha256": hash_json(activation_policy()),
            "target_images": copy.deepcopy(self.source.manifest["target_images"]),
            "target_image_ids": {side: self.source._images[ref] for side, ref in self.source.manifest["target_images"].items()},
            "target_container_names": self._target_names(target),
            "source_configuration_sha256": hash_json(configured),
            "target_configuration_sha256": hash_json(target),
            "staged_configuration_sha256": hash_json(staged_config),
            "target_compose_sha256": _hash_bytes(raw),
            "source_environment_sha256": _hash_bytes(self._environment),
            "database_identity_sha256": self.source._database_identity,
            "data_container_ids": {name: value["Id"] for name, value in self._data.items()},
            "staged_restart_policy": "no",
            "staged_restart_services": sorted(ops.ALL),
            "successful_restart_policies": {name: configured["services"][name].get("restart", "no") for name in sorted(ops.ALL)},
            "fixture": self.fixture,
        }
        self._record_file(self.state_dir / ("activation-plan-" + self.operation_id + ".json"), canonical_bytes(self._plan))
        self._verify_data()
        return self.plan

    def _helper_environment(self, service: str, *, passive: bool = True) -> dict[str, str]:
        environment = _effective_environment(self._image_config["backend"], self._target_config["services"][service])
        # Preserve production validators and bound OpenAI capability. Only the
        # connection endpoints and immutable admission change for this helper.
        environment.update({
            "DATABASE_URL": self.source._helper_environment("target")["DATABASE_URL"],
            "REDIS_URL": "redis://127.0.0.1:6379/15", "RUNTIME_ADMISSION_MODE": "passive" if passive else "active",
            "PYTHONDONTWRITEBYTECODE": "1", "OPENAI_AGENTS_DISABLE_TRACING": "true",
        })
        if not self.fixture:
            _require(environment.get("APP_ENV") in {"production", "prod"}, "ACTIVATION_TARGET_CONFIG_INVALID")
        return environment

    def _run_helper(self, code: str, *, service: str = "backend", environment: dict | None = None) -> dict:
        identifier = self.source._create(self.source.manifest["target_images"]["backend"], [], bootstrap=True)
        try:
            raw = ops.command(["docker", "start", "--attach", "--interactive", identifier], input=json.dumps({
                "environment": environment or self._helper_environment(service), "argv": ["python", "-c", code],
            }))
            value = self._inspect(identifier)
            _require(not value["State"]["Running"] and value["State"]["ExitCode"] == 0 and not value["State"].get("OOMKilled"),
                     "ACTIVATION_HELPER_FAILED")
            result = json.loads(raw.splitlines()[-1])
            _require(type(result) is dict, "ACTIVATION_HELPER_FAILED")
            return result
        finally:
            self.source._remove(identifier)

    def full_snapshot(self) -> dict:
        self._check_plan()
        self._verify_data()
        self._namespace_members()
        # This diagnostic is also safe after close: no admission authority is
        # revived, and this fixed read-only payload needs only DB credentials.
        result = self._run_helper(FULL_SNAPSHOT_CODE, environment=self.source._helper_environment("target"))
        identity = backup.database_identity_hash(result.pop("database_identity"))
        _require(identity == self.source._database_identity and result.get("schema") == self.source.manifest["target_schema_head"]
                 and IMAGE.fullmatch(str(result.get("all_tables_sha256")))
                 and type(result.get("table_counts")) is dict and result["table_counts"].get("alembic_version") == 1
                 and all(type(n) is str and apply_runtime.IDENTIFIER.fullmatch(n) and type(c) is int and c >= 0 for n, c in result["table_counts"].items()),
                 "ACTIVATION_FULL_SNAPSHOT_INVALID")
        result["database_identity_sha256"] = identity
        self._namespace_members()
        self._verify_data()
        return result

    def _http(self, identifier: str, path: str, *, port: int = 8000) -> dict:
        _require(path in {"/health/ready", "/api/v1/campaigns"} and port == 8000, "ACTIVATION_RUNTIME_PHASE_INVALID")
        environment = self._target_config["services"]["backend"].get("environment") or {}
        hosts = [x.strip() for x in str(environment.get("ALLOWED_HOSTS", "localhost,127.0.0.1")).split(",") if x.strip()]
        _require(bool(hosts), "ACTIVATION_TARGET_CONFIG_INVALID")
        host = hosts[0]
        host = "localhost" if host == "*" else "activation-check." + host[2:] if host.startswith("*.") else host
        _require(re.fullmatch(r"[A-Za-z0-9_.-]+", host), "ACTIVATION_TARGET_CONFIG_INVALID")
        code = "import json,sys,urllib.request,urllib.error\ntry:\n q=urllib.request.Request('http://127.0.0.1:8000'+sys.argv[1],headers={'Host':sys.argv[2]}); r=urllib.request.urlopen(q,timeout=3); print(json.dumps({'status':r.status,'body':json.loads(r.read())}))\nexcept urllib.error.HTTPError as e: print(json.dumps({'status':e.code,'body':json.loads(e.read())}))"
        return json.loads(ops.command(["docker", "exec", identifier, "python", "-c", code, path, host]))

    def passive_postflight(self) -> dict:
        self._check_plan()
        _require(not self._crossed and not self._closed, "ACTIVATION_RUNTIME_PHASE_INVALID")
        self.source.barrier()
        before = self.full_snapshot()
        created = []
        try:
            redis = self.source._create(self._data["redis"]["Image"], ["--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", "/tmp"], entrypoint="redis-server", user="65534:65534")
            created.append(redis)
            ops.command(["docker", "start", redis])
            backend = self.source._create(self.source.manifest["target_images"]["backend"], [], bootstrap=True)
            created.append(backend)
            process = subprocess.Popen(["docker", "start", "--attach", "--interactive", backend], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            self.source._helpers[backend] = process
            process.stdin.write(json.dumps({"environment": self._helper_environment("backend"), "argv": ["python", "-c", PASSIVE_BOOT]}))
            process.stdin.close()
            ready = None
            for _ in range(90):
                try:
                    response = self._http(backend, "/health/ready")
                    payload = response.get("body", {})
                    if response.get("status") == 200 and all(payload.get(k) == "ok" for k in ("status", "database", "redis", "schema")):
                        _require(payload.get("admission_protocol") == 1 and type(payload["admission_protocol"]) is int
                                 and payload.get("admission_mode") == "passive" and payload.get("business_ready") is False
                                 and payload.get("database_read_only") is True and payload.get("build_sha") == self.source.manifest["target_release_sha"],
                                 "ACTIVATION_PASSIVE_CHECK_FAILED")
                        ready = payload
                        break
                except (ops.ReleaseError, ValueError, OSError):
                    pass
                time.sleep(1)
            _require(ready is not None, "ACTIVATION_PASSIVE_CHECK_FAILED")
            denied = self._http(backend, "/api/v1/campaigns")
            _require(denied.get("status") == 503 and denied.get("body", {}).get("error", {}).get("code") == "RUNTIME_PASSIVE", "ACTIVATION_PASSIVE_CHECK_FAILED")
            for service in (*WORKERS, *ops.SCHEDULERS):
                value = self._run_helper(PASSIVE_CLI, service=service)
                _require(value.get("status") == "PASS" and value.get("admission_mode") == "passive" and value.get("business_ready") is False,
                         "ACTIVATION_PASSIVE_CHECK_FAILED")
        finally:
            errors = []
            for identifier in reversed(created):
                try:
                    self.source._remove(identifier)
                except BaseException:
                    errors.append(True)
            _require(not errors, "ACTIVATION_HELPER_CLEANUP_FAILED")
        _require(not self.source._helpers and not self.source._pending_names, "ACTIVATION_HELPER_CLEANUP_FAILED")
        after = self.full_snapshot()
        _require(before == after, "ACTIVATION_PASSIVE_CHECK_FAILED")
        barrier = self.source.barrier()
        return {
            "admission_protocol": 1, "admission_mode": "passive", "business_ready": False,
            "business_writes_blocked": True, "claims_blocked": True,
            "target_release_sha": self.source.manifest["target_release_sha"],
            "target_schema_head": self.source.manifest["target_schema_head"],
            "target_plan_sha256": hash_json(self._plan), "source_barrier_sha256": barrier["barrier_sha256"],
            "helpers_removed": True, "all_tables_sha256": after["all_tables_sha256"],
            "production_configuration_validated": not self.fixture,
        }

    def _start_audit(self) -> None:
        if self.audit is None:
            self.audit = TargetAudit(self.operation_id, self.project,
                                     {self.source.writer_barrier._ids[n] for n in ops.APP},
                                     {value["Id"] for value in self._data.values()}, self.source.writer_barrier.allowed_helpers,
                                     {name: {endpoint["NetworkID"] for endpoint in self.source.writer_barrier._raw[name]["NetworkSettings"].get("Networks", {}).values()}
                                      for name in ops.APP},
                                     {name: {"source_id": self.source.writer_barrier._ids[name],
                                             "final_name": self._plan["target_container_names"][name]} for name in ops.APP})
            self.audit.start()

    def handoff_boundary(self, state) -> None:
        self._check_plan()
        _require(not self._crossed and not self._closed, "ACTIVATION_RUNTIME_PHASE_INVALID")
        self._verify_data()
        self._start_audit()
        self.source.barrier()
        self._crossed = True  # Uncertain marker publication forbids pre-phase recovery.
        state.begin_activation()
        state.assert_activation_permitted()
        self.source.barrier()
        self.audit.sync()
        self.source.writer_barrier.close()
        self._verify_observer_closed(self.source.writer_barrier.audit)
        self.audit.sync()

    def _persist_environment(self, compose_file: str) -> None:
        updated = ops.set_env(self._environment.decode(), {
            "BUILD_SHA": self.source.manifest["target_release_sha"],
            "RUNTIME_ADMISSION_MODE": "active", "COMPOSE_FILE": compose_file,
        }).encode()
        ops.atomic(self.source.runtime.env_path, updated.decode())
        current, identity = _private_read(self.source.runtime.env_path)
        _require(current == updated, "ACTIVATION_CONFIG_CHANGED")
        self._environment, self._environment_identity = current, identity

    def _validate_target(self, name: str, value: dict, *, running: bool, final: bool = False) -> None:
        _require(name in ops.APP, "ACTIVATION_RUNTIME_PHASE_INVALID")
        self.target_service = name
        side = "frontend" if name == "frontend" else "backend"
        image = self._image_config[side]
        configured = (self._target_config if final else self._staged_config)["services"][name]
        labels = value["Config"].get("Labels") or {}
        expected_env = _effective_environment(image, configured)
        # Compose's resolved JSON retains null for an inherited command or
        # entrypoint. An explicit empty list is a distinct override.
        expected_cmd = configured.get("command")
        if expected_cmd is None:
            expected_cmd = image.get("Cmd")
        expected_entry = configured.get("entrypoint")
        if expected_entry is None:
            expected_entry = image.get("Entrypoint")
        checks = (
            ("name", lambda: value.get("Name") == "/" + self._plan["target_container_names"][name]),
            ("image_id", lambda: value["Image"] == self._plan["target_image_ids"][side]),
            ("image_revision", lambda: labels.get("org.opencontainers.image.revision") == self.source.manifest["target_release_sha"]),
            ("compose_project", lambda: labels.get("com.docker.compose.project") == self.project),
            ("compose_service", lambda: labels.get("com.docker.compose.service") == name),
            ("operation", lambda: labels.get(LABEL) == self.operation_id),
            ("environment", lambda: _environment(value) == expected_env),
            ("command", lambda: value["Config"].get("Cmd") == expected_cmd),
            ("entrypoint", lambda: value["Config"].get("Entrypoint") == expected_entry),
            ("running", lambda: value["State"].get("Running") is running),
            ("paused", lambda: not value["State"].get("Paused")),
            ("restarting", lambda: not value["State"].get("Restarting")),
            ("oom_killed", lambda: not value["State"].get("OOMKilled")),
        )
        for field, check in checks:
            self.target_identity_field = field
            _require(check(), "ACTIVATION_TARGET_IDENTITY_CHANGED")
        self.target_identity_field = None
        original = self.source.writer_barrier._raw[name]
        mounts = lambda data: sorted((x.get("Destination"), x.get("Type"), x.get("Name", x.get("Source")), x.get("RW")) for x in data.get("Mounts") or [])
        _require(mounts(value) == mounts(original), "ACTIVATION_TARGET_POLICY_CHANGED")
        for key in ("Privileged", "ReadonlyRootfs", "CapAdd", "CapDrop", "Memory", "NanoCpus", "PidsLimit", "ExtraHosts", "PortBindings"):
            _require(value["HostConfig"].get(key) == original["HostConfig"].get(key), "ACTIVATION_TARGET_POLICY_CHANGED")
        _require(value["Config"].get("User") == original["Config"].get("User")
                 and value["Config"].get("WorkingDir") == original["Config"].get("WorkingDir"), "ACTIVATION_TARGET_POLICY_CHANGED")
        expected_restart = original["HostConfig"]["RestartPolicy"] if final else {"Name": "no", "MaximumRetryCount": 0}
        _require(value["HostConfig"].get("RestartPolicy") == expected_restart, "ACTIVATION_TARGET_POLICY_CHANGED")
        if running:
            expected_networks = {k: v["NetworkID"] for k, v in original["NetworkSettings"].get("Networks", {}).items()}
            actual_networks = {k: v["NetworkID"] for k, v in value["NetworkSettings"].get("Networks", {}).items()}
            _require(actual_networks == expected_networks and value["RestartCount"] == 0, "ACTIVATION_TARGET_POLICY_CHANGED")
            self._declared_networks(name, value, self._target_config)
        self.target_service = None

    def recreate_target_stopped(self, state) -> None:
        self.recreate_step = "permit"
        self._permit(state)
        _require(not self._recreated, "ACTIVATION_RUNTIME_PHASE_INVALID")
        self.recreate_step = "data_identity"
        self._verify_data()
        self.recreate_step = "redis_restart_policy"
        redis_id = self._data["redis"]["Id"]
        self.audit.allow("container", "update", redis_id)
        ops.command(["docker", "update", "--restart=no", redis_id])
        _require(self._inspect(redis_id)["HostConfig"]["RestartPolicy"]["Name"] == "no",
                 "ACTIVATION_TARGET_POLICY_CHANGED")
        self._permit(state)
        if not self.fixture:
            self.recreate_step = "checkout"
            ops.command(["git", "switch", "--detach", self.source.manifest["target_release_sha"]], cwd=self.root)
            _require(ops.command(["git", "rev-parse", "HEAD"], cwd=self.root) == self.source.manifest["target_release_sha"]
                     and not ops.command(["git", "status", "--porcelain"], cwd=self.root), "ACTIVATION_CONFIG_CHANGED")
        self.recreate_step = "staged_environment"
        self._persist_environment(os.pathsep.join((str(self._base_path), str(self._staged_path))))
        self._permit(state)
        self.recreate_step = "compose_create"
        with self.audit.condition:
            self.audit.creating = set(ops.APP)
            self.audit.removing_old = True
        self.target_runtime.compose("up", "--no-start", "--no-deps", "--no-build", "--pull", "never", "--force-recreate", *ops.APP)
        self.recreate_step = "target_identity"
        for name in ops.APP:
            self.target_service, self.target_identity_field = name, "service_instance"
            value = self.target_runtime.inspect(name)
            self.target_identity_field = "replacement_id"
            _require(value["Id"] not in self.source.writer_barrier._ids.values(), "ACTIVATION_TARGET_IDENTITY_CHANGED")
            self._validate_target(name, value, running=False)
            self.target_ids[name] = value["Id"]
        self.recreate_step = "event_sync"
        self.audit.sync()
        self.audit.finish_connections(set(self.target_ids.values()))
        _require(set(self.target_ids.values()) == self.audit.target_ids and not self.audit.creating and not self.audit.rename_permits,
                 "ACTIVATION_TARGET_IDENTITY_CHANGED")
        with self.audit.condition:
            self.audit.removing_old = False
        self.recreate_step = "fenced_topology"
        self._verify_data()
        _require(not self._inspect(self._data["postgres"]["Id"])["NetworkSettings"].get("Networks"), "ACTIVATION_DATA_IDENTITY_CHANGED")
        self._project_members()
        self._namespace_members()
        self._recreated = True
        self.recreate_step = "complete"

    def reconnect_data_services(self, state) -> None:
        self._permit(state)
        _require(self._recreated, "ACTIVATION_RUNTIME_PHASE_INVALID")
        for name, original in self._data.items():
            current = self._inspect(original["Id"])
            expected = original["NetworkSettings"].get("Networks") or {}
            networks = current["NetworkSettings"].get("Networks") or {}
            _require(set(networks) <= set(expected), "ACTIVATION_DATA_IDENTITY_CHANGED")
            for network_name, endpoint in expected.items():
                if network_name in networks:
                    _require(networks[network_name]["NetworkID"] == endpoint["NetworkID"], "ACTIVATION_DATA_IDENTITY_CHANGED")
                    continue
                self._permit(state)
                network_id = endpoint["NetworkID"]
                network = json.loads(ops.command(["docker", "network", "inspect", network_id]))[0]
                _require(network.get("Id") == network_id and network.get("Name") == network_name
                         and network.get("Driver") == "bridge" and network.get("Scope") == "local", "ACTIVATION_DATA_IDENTITY_CHANGED")
                args = ["docker", "network", "connect"]
                for alias in endpoint.get("Aliases") or []:
                    _require(type(alias) is str and re.fullmatch(r"[A-Za-z0-9_.-]+", alias), "ACTIVATION_PLAN_UNSAFE")
                    args += ["--alias", alias]
                ipam = endpoint.get("IPAMConfig") or {}
                _require(type(ipam) is dict and set(ipam) <= {"IPv4Address", "IPv6Address", "LinkLocalIPs"}
                         and not ipam.get("LinkLocalIPs"), "ACTIVATION_PLAN_UNSAFE")
                for field, flag in (("IPv4Address", "--ip"), ("IPv6Address", "--ip6")):
                    if ipam.get(field):
                        args += [flag, ipam[field]]
                self.audit.allow("network", "connect", network_id + ":" + original["Id"])
                ops.command(args + [network_id, original["Id"]])
            self.audit.sync()
            actual = self._inspect(original["Id"])["NetworkSettings"].get("Networks") or {}
            _require({k: v["NetworkID"] for k, v in actual.items()} == {k: v["NetworkID"] for k, v in expected.items()}, "ACTIVATION_DATA_IDENTITY_CHANGED")
        self._verify_data()

    def _ready(self) -> None:
        for _ in range(90):
            self.audit.check()
            try:
                value = self._http(self.target_ids["backend"], "/health/ready")
                body = value.get("body", {})
                if value.get("status") == 200 and all(body.get(k) == "ok" for k in ("status", "database", "redis", "schema")):
                    _require(body.get("admission_protocol") == 1 and type(body["admission_protocol"]) is int
                             and body.get("admission_mode") == "active" and body.get("business_ready") is True
                             and body.get("build_sha") == self.source.manifest["target_release_sha"], "ACTIVATION_TARGET_NOT_READY")
                    return
            except (ops.ReleaseError, OSError, ValueError):
                pass
            time.sleep(1)
        raise ops.ReleaseError("ACTIVATION_TARGET_NOT_READY")

    def _frontend_ready(self) -> None:
        for _ in range(60):
            self.audit.check()
            try:
                ops.command(["docker", "exec", self.target_ids["frontend"], "node", "-e", "fetch('http://127.0.0.1:3000/publications',{signal:AbortSignal.timeout(3000)}).then(r=>process.exit(r.status===200?0:1)).catch(()=>process.exit(1))"])
                return
            except ops.ReleaseError:
                time.sleep(1)
        raise ops.ReleaseError("ACTIVATION_TARGET_NOT_READY")

    def _queues_ready(self) -> None:
        expected = {"celery@" + self._inspect(self.target_ids[name])["Config"]["Hostname"]: name.removesuffix("_worker") for name in WORKERS}
        _require(len(expected) == len(WORKERS), "ACTIVATION_QUEUES_NOT_READY")
        for _ in range(24):
            self.audit.check()
            try:
                value = json.loads(ops.command(["docker", "exec", self.target_ids["backend"], "python", "-c", QUEUE_CODE, json.dumps(expected)]))
                if value == {"exact_queues_verified": True}:
                    return
            except (ops.ReleaseError, ValueError):
                pass
            time.sleep(2)
        raise ops.ReleaseError("ACTIVATION_QUEUES_NOT_READY")

    def start_target(self, state) -> None:
        self._permit(state)
        _require(set(self.target_ids) == set(ops.APP) and not self._started, "ACTIVATION_RUNTIME_PHASE_INVALID")
        for group in (("backend",), ("frontend",), WORKERS, ops.SCHEDULERS):
            for name in group:
                self._permit(state)
                identifier = self.target_ids[name]
                self._validate_target(name, self._inspect(identifier), running=False)
                for endpoint in self.source.writer_barrier._raw[name]["NetworkSettings"].get("Networks", {}).values():
                    self.audit.allow("network", "connect", endpoint["NetworkID"] + ":" + identifier)
                self.audit.allow("container", "start", identifier)
                ops.command(["docker", "start", identifier])
                self._started.add(name)
            self.audit.sync()
            self.audit.finish_connections({self.target_ids[name] for name in group})
            if group == ("backend",):
                self._ready()
            elif group == ("frontend",):
                self._frontend_ready()
            elif group == WORKERS:
                self._queues_ready()

    def postflight(self) -> dict:
        self._check_plan()
        _require(self._started == set(ops.APP), "ACTIVATION_RUNTIME_PHASE_INVALID")
        self.audit.sync()
        self._verify_data()
        self._project_members()
        self._namespace_members()
        _require(self.source._sql("SELECT version_num FROM alembic_version") == self.source.manifest["target_schema_head"],
                 "ACTIVATION_DATA_IDENTITY_CHANGED")
        for name, identifier in self.target_ids.items():
            self._validate_target(name, self._inspect(identifier), running=True, final=self._persisted)
        for name, baseline in self._data.items():
            expected = baseline["HostConfig"]["RestartPolicy"] if self._persisted else {"Name": "no", "MaximumRetryCount": 0}
            _require(self._inspect(baseline["Id"])["HostConfig"]["RestartPolicy"] == expected,
                     "ACTIVATION_TARGET_POLICY_CHANGED")
        self._ready()
        self._frontend_ready()
        self._queues_ready()
        self.audit.sync()
        return {
            "runtime_ready": True, "target_release_sha": self.source.manifest["target_release_sha"],
            "target_schema_head": self.source.manifest["target_schema_head"],
            "target_plan_sha256": hash_json(self._plan), "target_container_ids_sha256": hash_json(self.target_ids),
            "exact_queues_verified": True, "configuration_persisted": self._persisted,
        }

    @staticmethod
    def _restart_option(value: dict) -> str:
        name, count = value.get("Name"), value.get("MaximumRetryCount")
        _require(name in {"", "no", "always", "unless-stopped", "on-failure"}
                 and type(count) is int and count >= 0, "ACTIVATION_TARGET_POLICY_CHANGED")
        return "on-failure:" + str(count) if name == "on-failure" and count else name or "no"

    def finalize_configuration(self, state) -> dict:
        self._permit(state)
        _require(self._started == set(ops.APP) and not self._persisted, "ACTIVATION_RUNTIME_PHASE_INVALID")
        for name in sorted(ops.ALL):
            self._permit(state)
            identifier = self.target_ids[name] if name in self.target_ids else self._data[name]["Id"]
            policy = self.source.writer_barrier._raw[name]["HostConfig"]["RestartPolicy"]
            self.audit.allow("container", "update", identifier)
            ops.command(["docker", "update", "--restart=" + self._restart_option(policy), identifier])
            _require(self._inspect(identifier)["HostConfig"]["RestartPolicy"] == policy, "ACTIVATION_TARGET_POLICY_CHANGED")
        self._permit(state)
        self._persist_environment(os.pathsep.join((str(self._base_path), str(self._final_path))))
        configured = json.loads(self._final_runtime.compose("config", "--format", "json"))
        _require(configured == self._target_config, "ACTIVATION_CONFIG_CHANGED")
        self.target_runtime = self._final_runtime
        self._persisted = True
        return self.postflight()

    def _discover_writers(self) -> set[str]:
        raw = ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", "label=com.docker.compose.project=" + self.project])
        identifiers = set(raw.splitlines())
        _require(all(ID.fullmatch(value) for value in identifiers), "ACTIVATION_CONTAINMENT_FAILED")
        # Stop every discovered application in this exact project, including an
        # unexpected extra role. Exclude only the two pinned data identities.
        return identifiers - {value["Id"] for value in self._data.values()}

    def _stop_writers(self, identifiers: set[str]) -> None:
        failures, running = [], set()
        for identifier in sorted(identifiers):
            try:
                value = self._inspect(identifier)
                if not value["State"].get("Running"):
                    continue
                running.add(identifier)
                if self.audit is not None:
                    self.audit.allow("container", "kill", identifier)
                    self.audit.allow("container", "die", identifier)
                    for endpoint in value["NetworkSettings"].get("Networks", {}).values():
                        self.audit.allow("network", "disconnect", endpoint["NetworkID"] + ":" + identifier)
                ops.command(["docker", "container", "kill", "--signal=TERM", identifier])
            except BaseException:
                failures.append(True)
        deadline = time.monotonic() + 210
        while running and time.monotonic() < deadline:
            for identifier in tuple(running):
                try:
                    if not self._inspect(identifier)["State"].get("Running"):
                        running.remove(identifier)
                except BaseException:
                    failures.append(True)
                    running.remove(identifier)
            if running:
                time.sleep(0.5)
        _require(not failures and not running, "ACTIVATION_CONTAINMENT_FAILED")

    def _fence_data(self) -> None:
        failures = []
        for name, baseline in self._data.items():
            try:
                value = self._inspect(baseline["Id"])
                networks = list((value["NetworkSettings"].get("Networks") or {}).values())
            except BaseException:
                failures.append(True)
                networks = list((baseline["NetworkSettings"].get("Networks") or {}).values())
            for endpoint in networks:
                try:
                    network = endpoint["NetworkID"]
                    if self.audit is not None:
                        self.audit.allow("network", "disconnect", network + ":" + baseline["Id"])
                    ops.command(["docker", "network", "disconnect", "--force", network, baseline["Id"]])
                except BaseException:
                    failures.append(True)
            try:
                _require(not self._inspect(baseline["Id"])["NetworkSettings"].get("Networks"), "ACTIVATION_CONTAINMENT_FAILED")
                interfaces = ops.command(["docker", "exec", baseline["Id"], "sh", "-c", "ls /sys/class/net"]).split()
                routes = ops.command(["docker", "exec", baseline["Id"], "cat", "/proc/net/route"]).splitlines()
                _require(interfaces == ["lo"] and all(not line.split() or line.split()[0] == "lo" for line in routes[1:]), "ACTIVATION_CONTAINMENT_FAILED")
            except BaseException:
                failures.append(True)
        _require(not failures, "ACTIVATION_CONTAINMENT_FAILED")

    def contain_target(self) -> dict:
        self._owner()
        failures = []
        try:
            self._start_audit()
        except BaseException:
            failures.append(True)
        identifiers = set(self.target_ids.values())
        try:
            identifiers |= self._discover_writers()
        except BaseException:
            failures.append(True)
            # Known source IDs may still exist when create/inspect failed.
            try:
                existing = set(ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc"]).splitlines())
                identifiers |= existing & {self.source.writer_barrier._ids[n] for n in ops.APP}
            except BaseException:
                failures.append(True)
                # Even an unavailable discovery command must not prevent the
                # independent stop/fence attempts against pinned identities.
                identifiers |= {self.source.writer_barrier._ids[n] for n in ops.APP}
        try:
            self.source._discover_pending_helpers()
        except BaseException:
            failures.append(True)
        for identifier in list(self.source._helpers):
            try:
                self.source._remove(identifier)
            except BaseException:
                failures.append(True)
        for identifier in sorted(identifiers | {v["Id"] for v in self._data.values()}):
            try:
                if self.audit is not None:
                    self.audit.allow("container", "update", identifier)
                ops.command(["docker", "update", "--restart=no", identifier])
                _require(self._inspect(identifier)["HostConfig"]["RestartPolicy"]["Name"] == "no", "ACTIVATION_CONTAINMENT_FAILED")
            except BaseException:
                failures.append(True)
        for action in (lambda: self._stop_writers(identifiers), self._fence_data):
            try:
                action()
            except BaseException:
                failures.append(True)
        self._fenced = True
        try:
            self._verify_data()
            self._namespace_members()
            _require(identifiers and all(not self._inspect(i)["State"].get("Running") and not self._inspect(i)["State"].get("Restarting") for i in identifiers), "ACTIVATION_CONTAINMENT_FAILED")
            _require(all(not self.source.writer_barrier._connects(*probe) for probe in self.source.writer_barrier._host_probes), "ACTIVATION_CONTAINMENT_FAILED")
            for _ in range(30):
                if self.source._sql(apply_runtime.CLIENTS_SQL) == "0":
                    break
                time.sleep(0.2)
            else:
                raise ops.ReleaseError("ACTIVATION_CONTAINMENT_FAILED")
            self.audit.sync()
            first = self.full_snapshot()
            second = self.full_snapshot()
            _require(first == second, "ACTIVATION_CONTAINMENT_FAILED")
            self.audit.sync()
        except BaseException:
            failures.append(True)
        _require(not failures, "ACTIVATION_CONTAINMENT_FAILED")
        return {
            "runtime_stopped": True, "new_claims_blocked": True, "data_services_fenced": True,
            "target_schema_preserved": True, "database_identity_preserved": True,
            "target_schema_head": second["schema"], "all_tables_sha256": second["all_tables_sha256"],
            "database_identity_sha256": second["database_identity_sha256"],
            "post_fence_snapshot_stable": True, "schema_downgrade_performed": False,
            "backup_restore_performed": False,
        }

    def close(self) -> None:
        self._owner()
        self._closed = True
        if self._close_verified and not self.source._helpers and not self.source._pending_names:
            return
        failures = []
        if self.audit is not None and not self.audit.closed:
            try:
                self.audit.sync()
            except BaseException:
                failures.append(True)
        for action in (self.source.close, self.audit.close if self.audit is not None else lambda: None):
            try:
                action()
            except BaseException:
                failures.append(True)
        for observer in (self.source.writer_barrier.audit, self.audit):
            try:
                self._verify_observer_closed(observer)
            except BaseException:
                failures.append(True)
        _require(not failures, "ACTIVATION_HELPER_CLEANUP_FAILED")
        self._close_verified = True


def production_adapter(root: Path, manifest: dict, operation_id: str, state: Path, backup_dir: Path, key_dir: Path) -> MigrationActivationRuntime:
    return MigrationActivationRuntime(apply_runtime.production_runtime(root, manifest, operation_id, state, backup_dir, key_dir))


def production_inventory(root: Path, manifest: dict, operation_id: str, state: Path, backup_dir: Path, key_dir: Path) -> dict:
    adapter = production_adapter(root, manifest, operation_id, state, backup_dir, key_dir)
    try:
        inventory = adapter.source.inventory()
        adapter.source.quiet()
        plan = adapter.prepare_target_plan()
        return {"operation_id": operation_id, "runtime_identity_sha256": inventory["runtime_identity_sha256"],
                "target_plan_sha256": hash_json(plan), "plan": plan}
    finally:
        adapter.close()
