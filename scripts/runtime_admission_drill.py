#!/usr/bin/env python3
"""Exercise an unsigned checkout candidate in two isolated disposable runtimes.

This is not a release approval, a production activation command, or a stage-4
migration rehearsal. The actual tracked backend is built locally and addressed
by its image ID. No production paths, credentials, ports or volumes are used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import release_ops as ops
import runtime_admission_fixture as fixture
from migration_activation_state import (
    AUTHORIZATION, PROJECT_PREFIX, TEMP_PREFIX, CandidateActivationState,
)

PROTOCOL = 1
IMAGE_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
BUILD_PREFIX = "iteam-runtime-admission-build-"
ERRORS = frozenset({
    "ARGUMENTS_INVALID", "OUTPUT_UNSAFE", "CHECKOUT_UNSAFE", "ARCHIVE_UNSAFE",
    "COMMAND_FAILED", "COMMAND_TIMEOUT", "COMMAND_FORBIDDEN", "IMAGE_MISMATCH",
    "FIXTURE_UNSAFE", "FIXTURE_NOT_READY", "FIXTURE_SEED_FAILED", "PROBE_INVALID",
    "ADMISSION_MISMATCH", "PASSIVE_WRITE_DETECTED", "PASSIVE_PROCESS_NOT_BLOCKED",
    "REDIS_WITNESS_FAILED", "REDIS_ACTIVITY_DETECTED", "PROVIDER_ACTIVITY_DETECTED",
    "ACTIVATION_FAILED", "ACTIVE_WRITE_NOT_PROVED", "ACTIVE_CLAIM_NOT_PROVED",
    "CONTAINMENT_FAILED", "COMMITTED_DATA_CHANGED", "CLEANUP_FAILED", "DRILL_FAILED",
})
STAGES = frozenset({
    "CHECKOUT", "CANDIDATE_BUILD", "CANDIDATE_IDENTITY", "INFRASTRUCTURE_IMAGES",
    "FIXTURE_START", "SCHEMA_PREPARE", "CANARY_SEED", "PROVIDER_WITNESS_SETUP",
    "PASSIVE_BASELINE", "PASSIVE_BOOT", "PASSIVE_HTTP", "PASSIVE_WORKERS", "PASSIVE_SCHEDULERS",
    "PASSIVE_DIRECT_PROBES", "PASSIVE_WITNESS_CLOSE", "PASSIVE_FINAL_SNAPSHOT",
    "ACTIVATION_BOUNDARY", "ACTIVE_BOOT", "ACTIVE_WORKER", "ACTIVE_HTTP_COMMIT", "ACTIVE_CLAIM",
    "ACTIVE_STALE_GET", "INJECTED_FAILURE", "PHYSICAL_CONTAINMENT", "CONTAINED_SNAPSHOT",
    "CONTAINMENT_RECORD", "FIXTURE_CLEANUP", "CANDIDATE_CLEANUP", "PROOF_PUBLICATION", "UNKNOWN",
})
FALSE_AUTHORITY = {
    "production_approval_verified": False,
    "production_activation_authorized": False,
    "external_side_effects_authorized": False,
    "signed_release_verified": False,
    "full_stage4_to_stage5_verified": False,
    "automatic_retry": False,
    "schema_downgrade_allowed": False,
}


class DrillError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in ERRORS else "DRILL_FAILED"
        super().__init__(self.code)


def require(value: object, code: str) -> None:
    if not value:
        raise DrillError(code)


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def safe_error(error: BaseException) -> str:
    return error.code if isinstance(error, DrillError) else "DRILL_FAILED"


class Commands:
    """Captured subprocesses; command output never becomes a diagnostic."""

    def __init__(self):
        self.count = 0
        self.forbidden_attempts = 0
        self.stage = "CHECKOUT"

    def run(self, args: list[str], *, timeout: int = 90, check: bool = True,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        require(bool(args) and all(type(arg) is str for arg in args), "COMMAND_FORBIDDEN")
        if any(arg in {"downgrade", "pg_restore", "dropdb", "stamp"} for arg in args):
            self.forbidden_attempts += 1
            raise DrillError("COMMAND_FORBIDDEN")
        self.count += 1
        try:
            result = subprocess.run(args, cwd=cwd, text=True, capture_output=True,
                                    timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise DrillError("COMMAND_TIMEOUT") from None
        except OSError:
            raise DrillError("COMMAND_FAILED") from None
        if check:
            require(result.returncode == 0, "COMMAND_FAILED")
        return result


def object_output(result: subprocess.CompletedProcess) -> dict:
    """Application log lines may precede the final bounded JSON object."""
    try:
        line = result.stdout.strip().splitlines()[-1]
        require(len(line) <= 256 * 1024, "PROBE_INVALID")
        value = json.loads(line)
    except (ValueError, IndexError, TypeError):
        raise DrillError("PROBE_INVALID") from None
    require(type(value) is dict, "PROBE_INVALID")
    return value


def extract_backend(archive: Path, destination: Path) -> None:
    """Only tracked regular backend files; reject links and path traversal."""
    with tarfile.open(archive, "r:") as stream:
        members = stream.getmembers()
        require(bool(members), "ARCHIVE_UNSAFE")
        total = 0
        seen = set()
        for item in members:
            path = PurePosixPath(item.name)
            require(not path.is_absolute() and path.parts and path.parts[0] == "backend"
                    and ".." not in path.parts and item.name not in seen
                    and (item.isfile() or item.isdir())
                    and 0 <= item.size <= 32 * 1024 * 1024, "ARCHIVE_UNSAFE")
            seen.add(item.name)
            total += item.size
            require(total <= 256 * 1024 * 1024, "ARCHIVE_UNSAFE")
        for item in members:
            target = destination.joinpath(*PurePosixPath(item.name).parts)
            if item.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = stream.extractfile(item)
                require(source is not None, "ARCHIVE_UNSAFE")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
    require((destination / "backend/Dockerfile").is_file()
            and (destination / "backend/requirements.lock").is_file(), "ARCHIVE_UNSAFE")


class Candidate:
    def __init__(self, commands: Commands, root: Path, temporary: Path):
        self.commands = commands
        self.temporary = temporary
        self.build_id = uuid.uuid4().hex
        self.image_id: str | None = None
        self.metadata: dict = {}
        self.root = root

    def build(self) -> dict:
        def git(*args: str) -> str:
            return self.commands.run(["git", *args], cwd=self.root).stdout.strip()

        sha = git("rev-parse", "HEAD")
        tree = git("rev-parse", "HEAD^{tree}")
        backend_tree = git("rev-parse", "HEAD:backend")
        require(all(SHA_PATTERN.fullmatch(x) for x in (sha, tree, backend_tree)), "CHECKOUT_UNSAFE")
        require(not git("status", "--porcelain", "--untracked-files=no"), "CHECKOUT_UNSAFE")
        archive = self.temporary / "candidate.tar"
        git("archive", "--format=tar", "--output=" + str(archive), sha, "backend")
        extract_backend(archive, self.temporary / "source")
        context = self.temporary / "source/backend"
        iid = self.temporary / "candidate.iid"
        self.commands.stage = "CANDIDATE_BUILD"
        self.commands.run([
            "docker", "build", "--iidfile", str(iid), "--build-arg", "BUILD_SHA=" + sha,
            "--label", "org.opencontainers.image.revision=" + sha,
            "--label", "iteam.admission.build=" + self.build_id,
            "--file", str(context / "Dockerfile"), str(context),
        ], timeout=900)
        image_id = iid.read_text().strip()
        self.commands.stage = "CANDIDATE_IDENTITY"
        require(IMAGE_PATTERN.fullmatch(image_id), "IMAGE_MISMATCH")
        self.image_id = image_id
        inspected = json.loads(self.commands.run(["docker", "image", "inspect", image_id]).stdout)
        require(type(inspected) is list and len(inspected) == 1, "IMAGE_MISMATCH")
        image = inspected[0]
        labels = image.get("Config", {}).get("Labels") or {}
        require(image.get("Id") == image_id
                and labels.get("org.opencontainers.image.revision") == sha
                and labels.get("iteam.admission.build") == self.build_id
                and "BUILD_SHA=" + sha in image.get("Config", {}).get("Env", []), "IMAGE_MISMATCH")
        self.metadata = {
            "candidate_sha": sha, "candidate_tree": tree,
            "backend_tree": backend_tree, "candidate_image_id": image_id,
            "tracked_archive_sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
            "admission_protocol": PROTOCOL, "source": "TRACKED_GIT_ARCHIVE",
            "image_source": "LOCAL_CANDIDATE_BUILD", "authorization": AUTHORIZATION,
        }
        return dict(self.metadata)

    def cleanup(self) -> bool:
        # The label also catches a completed build whose iidfile read failed.
        try:
            ids = self.commands.run(["docker", "image", "ls", "--quiet", "--no-trunc",
                                     "--filter", "label=iteam.admission.build=" + self.build_id]).stdout.split()
            for image_id in set(ids):
                require(IMAGE_PATTERN.fullmatch(image_id), "CLEANUP_FAILED")
                self.commands.run(["docker", "image", "rm", image_id])
            remaining = self.commands.run(["docker", "image", "ls", "--quiet", "--filter",
                                           "label=iteam.admission.build=" + self.build_id]).stdout.strip()
            return not remaining
        except Exception:
            return False


class RedisObservation:
    """Closed MONITOR vocabulary with sticky loss; no raw commands are retained."""

    def __init__(self):
        self.started = False
        self.healthy = True
        self.allowed_commands = 0
        self.forbidden_commands = 0
        self._sentinels: set[str] = set()
        self._condition = threading.Condition()

    def feed(self, line: str) -> None:
        with self._condition:
            if not self.started:
                if line.strip() == "OK":
                    self.started = True
                else:
                    self.healthy = False
                self._condition.notify_all()
                return
            match = re.fullmatch(r'\d+\.\d+ \[\d+ [^\]\r\n]+\] (.+)\r?\n?', line)
            try:
                words = shlex.split(match.group(1)) if match else []
            except ValueError:
                words = []
            if not words or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", words[0]):
                self.healthy = False
            else:
                command = words[0].upper()
                allowed = (command == "PING" and len(words) <= 2
                           or command == "SELECT" and len(words) == 2 and words[1] == "15"
                           or command == "HELLO" and len(words) in {2, 5}
                           or command == "CLIENT" and len(words) >= 2
                           and words[1].upper() in {"SETINFO", "SETNAME", "ID"})
                if allowed:
                    self.allowed_commands += 1
                else:
                    self.forbidden_commands += 1
                if command == "PING" and len(words) == 2 and re.fullmatch(r"admission-sync-[0-9a-f]{32}", words[1]):
                    self._sentinels.add(words[1])
            self._condition.notify_all()

    def lost(self) -> None:
        with self._condition:
            self.healthy = False
            self._condition.notify_all()

    def wait(self, *, sentinel: str | None = None, timeout: float = 8) -> None:
        until = time.monotonic() + timeout
        with self._condition:
            while self.healthy and not (self.started if sentinel is None else sentinel in self._sentinels):
                left = until - time.monotonic()
                if left <= 0:
                    self.healthy = False
                    break
                self._condition.wait(left)
            require(self.healthy, "REDIS_WITNESS_FAILED")

    def report(self) -> dict:
        require(self.started and self.healthy, "REDIS_WITNESS_FAILED")
        require(self.forbidden_commands == 0, "REDIS_ACTIVITY_DETECTED")
        return {"continuous_monitor_verified": True, "allowed_readiness_commands": self.allowed_commands,
                "forbidden_commands": 0, "queue_activity_observed": False}


class RedisWitness:
    def __init__(self, commands: Commands, container: str):
        self.commands, self.container = commands, container
        self.observation = RedisObservation()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.closing = False

    def start(self) -> None:
        try:
            self.process = subprocess.Popen(
                ["docker", "exec", self.container, "redis-cli", "--raw", "MONITOR"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError:
            raise DrillError("REDIS_WITNESS_FAILED") from None
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self.observation.wait()
        self.sync()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:
                self.observation.feed(line)
        finally:
            if not self.closing:
                self.observation.lost()

    def sync(self) -> None:
        require(self.process is not None and self.process.poll() is None, "REDIS_WITNESS_FAILED")
        sentinel = "admission-sync-" + uuid.uuid4().hex
        self.commands.run(["docker", "exec", self.container, "redis-cli", "PING", sentinel])
        self.observation.wait(sentinel=sentinel)
        require(self.process.poll() is None, "REDIS_WITNESS_FAILED")

    def finish(self) -> dict:
        self.sync()
        return self.observation.report()

    def close(self) -> None:
        self.closing = True
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            if self.thread is not None:
                self.thread.join(timeout=5)
                require(not self.thread.is_alive(), "REDIS_WITNESS_FAILED")
            require(self.process.poll() is not None, "REDIS_WITNESS_FAILED")
            if self.process.stdout is not None:
                self.process.stdout.close()


def environment(*, sha: str) -> dict[str, str]:
    require(SHA_PATTERN.fullmatch(sha), "CHECKOUT_UNSAFE")
    return {
        "APP_ENV": "development", "BUILD_SHA": sha, "RUNTIME_ADMISSION_MODE": "passive",
        "DATABASE_URL": "postgresql+asyncpg://iteam:fixture-only-password@postgres:5432/" + fixture.DATABASE,
        "REDIS_URL": "redis://redis:6379/15", "JWT_SECRET": "candidate-fixture-only-jwt-secret",
        "APP_SECRET": "candidate-fixture-only-app-secret", "ALLOWED_HOSTS": "backend,localhost,127.0.0.1",
        "OPENAI_API_KEY": "", "OPENAI_AGENTS_DISABLE_TRACING": "true",
        "WORKER_ROLE": "", "SCHEDULER_ROLE": "", "RUNTIME_DIAGNOSTICS_ENABLED": "true",
        "TELEGRAM_PUBLISHING_ENABLED": "false", "VK_PUBLISHING_ENABLED": "false",
        "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_TARGET_CHAT_ID": "", "VK_ACCESS_TOKEN": "",
        "VK_CREDENTIALS_FILE": "", "VK_OWNER_ID": "", "VK_CLIENT_ID": "", "VK_EXPECTED_USER_ID": "",
        "VK_METRICS_ENABLED": "false", "TELEGRAM_METRICS_ENABLED": "false", "METRICS_SYNC_ENABLED": "false",
        "VK_METRICS_ACCESS_TOKEN": "", "VK_METRICS_OWNER_ID": "", "TELEGRAM_METRICS_SESSION": "",
        "TELEGRAM_METRICS_API_ID": "", "TELEGRAM_METRICS_API_HASH": "", "TELEGRAM_METRICS_PEER": "",
        "TELEGRAM_METRICS_CHAT_ID": "", "AGENT_RUN_TIMEOUT_SECONDS": "20",
        "AGENT_PROVIDER_REQUEST_TIMEOUT_SECONDS": "5", "AGENT_PROVIDER_MAX_RETRIES": "0",
        "LOG_LEVEL": "WARNING", "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
        "NO_PROXY": "*", "PYTHONDONTWRITEBYTECODE": "1",
    }


class Runtime:
    def __init__(self, commands: Commands, candidate: dict, root: Path, infra_images: dict, *, schema: str):
        self.schema = schema
        self.commands, self.candidate, self.root = commands, candidate, root
        self.project = PROJECT_PREFIX + uuid.uuid4().hex[:10]
        self.operation = uuid.uuid4().hex
        self.network = self.project + "-network"
        self.volume = self.project + "-postgres-data"
        self.infra_images = infra_images
        self.containers: dict[str, str] = {}
        self.apps: set[str] = set()
        self.counter = 0
        self.env = environment(sha=candidate["candidate_sha"])
        self.env_file = root / "candidate.env"
        fd = os.open(self.env_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write("".join(key + "=" + value + "\n" for key, value in sorted(self.env.items())))

    def labels(self) -> list[str]:
        return ["--label", "iteam.admission.project=" + self.project,
                "--label", "iteam.admission.operation=" + self.operation]

    def docker(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.commands.run(["docker", *args], **kwargs)

    def run(self, service: str, command: list[str], *, mode: str = "passive", detached: bool = False,
            overrides: dict[str, str] | None = None, network: str | None = None,
            timeout: int = 90, check: bool = True, app: bool = False) -> subprocess.CompletedProcess:
        require(mode in {"passive", "active"} and re.fullmatch(r"[a-z][a-z0-9-]{0,40}", service), "FIXTURE_UNSAFE")
        name = self.project + "-" + service
        require(name not in self.containers, "FIXTURE_UNSAFE")
        self.containers[name] = service
        if app:
            self.apps.add(name)
        args = ["run", "--pull=never", "--name", name, *self.labels(), "--restart=no", "--network", network or self.network,
                "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=768m",
                "--env-file", str(self.env_file), "--env", "RUNTIME_ADMISSION_MODE=" + mode]
        for key, value in (overrides or {}).items():
            require(key in self.env and "\n" not in value, "FIXTURE_UNSAFE")
            args += ["--env", key + "=" + value]
        if service == "backend":
            args += ["--network-alias", "backend"]
        args += ["--detach"] if detached else []
        args += [self.candidate["candidate_image_id"], *command]
        result = self.docker(*args, timeout=timeout, check=check)
        self.inspect(name)
        if not detached:
            self.docker("rm", "--force", name)
            self.containers.pop(name)
            self.apps.discard(name)
        return result

    def python(self, code: str, *args: str, mode: str = "active", overrides: dict | None = None,
               network: str | None = None) -> dict:
        self.counter += 1
        return object_output(self.run("probe-" + str(self.counter), ["python", "-c", code, *args],
                                      mode=mode, overrides=overrides, network=network))

    def inspect(self, name: str) -> dict:
        value = json.loads(self.docker("inspect", name).stdout)
        require(type(value) is list and len(value) == 1, "FIXTURE_UNSAFE")
        item = value[0]
        labels = item.get("Config", {}).get("Labels") or {}
        require(labels.get("iteam.admission.operation") == self.operation
                and labels.get("iteam.admission.project") == self.project
                and item.get("HostConfig", {}).get("RestartPolicy", {}).get("Name") == "no"
                and not item.get("HostConfig", {}).get("PortBindings")
                and item.get("HostConfig", {}).get("Privileged") is False, "FIXTURE_UNSAFE")
        service = self.containers.get(name)
        if service is None:
            service = self.containers.get(item.get("Name", "").lstrip("/"))
        expected = self.infra_images.get(service, self.candidate["candidate_image_id"])
        require(item.get("Image") == expected, "IMAGE_MISMATCH")
        return item

    def start(self) -> None:
        self.commands.stage = "FIXTURE_START"
        self.docker("network", "create", "--internal", *self.labels(), self.network)
        self.docker("volume", "create", *self.labels(), self.volume)
        pg = self.project + "-postgres"
        redis = self.project + "-redis"
        sink = self.project + "-provider-sink"
        for name, service in ((pg, "postgres"), (redis, "redis"), (sink, "provider-sink")):
            self.containers[name] = service
        self.docker("run", "--pull=never", "--detach", "--name", pg, *self.labels(), "--restart=no",
                    "--network", self.network, "--network-alias", "postgres",
                    "--env", "POSTGRES_DB=" + fixture.DATABASE, "--env", "POSTGRES_USER=iteam",
                    "--env", "POSTGRES_PASSWORD=fixture-only-password", "--mount",
                    "type=volume,src=" + self.volume + ",dst=/var/lib/postgresql/data",
                    self.infra_images["postgres"])
        self.docker("run", "--pull=never", "--detach", "--name", redis, *self.labels(), "--restart=no",
                    "--network", self.network, "--network-alias", "redis", self.infra_images["redis"],
                    "redis-server", "--save", "", "--appendonly", "no")
        self.docker("run", "--pull=never", "--detach", "--name", sink, *self.labels(), "--restart=no",
                    "--network", self.network, "--network-alias", "provider-sink",
                    "--network-alias", "api.openai.com", "--network-alias", "api.telegram.org",
                    "--network-alias", "api.vk.com", "--network-alias", "api.vk.ru",
                    "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                    "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
                    self.candidate["candidate_image_id"], "python", "-c", fixture.SINK_CODE)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            pg_ok = self.docker("exec", pg, "pg_isready", "-U", "iteam", "-d", fixture.DATABASE, check=False).returncode == 0
            redis_ok = self.docker("exec", redis, "redis-cli", "PING", check=False).stdout.strip() == "PONG"
            if pg_ok and redis_ok:
                break
            time.sleep(1)
        else:
            raise DrillError("FIXTURE_NOT_READY")
        self.commands.stage = "SCHEMA_PREPARE"
        self.run("migrate", ["alembic", "upgrade", self.schema], mode="active")
        self.commands.stage = "CANARY_SEED"
        seed = self.python(fixture.SEED_CODE, self.schema)
        require(seed.get("status") == "PASS" and seed.get("queued_canaries") == 7
                and seed.get("stale_get_canary") is True and seed.get("stale_recovery_canary") is True,
                "FIXTURE_SEED_FAILED")
        self.commands.stage = "PROVIDER_WITNESS_SETUP"
        require(self.python(fixture.SINK_PROBE_CODE, "selftest").get("connections") == 4,
                "PROVIDER_ACTIVITY_DETECTED")
        topology = json.loads(self.docker("network", "inspect", self.network).stdout)[0]
        require(topology.get("Internal") is True and len(topology.get("Containers", {})) == 3, "FIXTURE_UNSAFE")
        for name in self.containers:
            self.inspect(name)

    def snapshot(self, *, fenced: bool = False) -> dict:
        overrides = None
        network = None
        if fenced:
            overrides = {"DATABASE_URL": self.env["DATABASE_URL"].replace("@postgres:", "@127.0.0.1:")}
            network = "container:" + self.project + "-postgres"
        value = self.python(fixture.SNAPSHOT_CODE, overrides=overrides, network=network)
        require(value.get("schema") == self.schema
                and IMAGE_PATTERN.fullmatch(str(value.get("all_tables_sha256")))
                and IMAGE_PATTERN.fullmatch(str(value.get("database_identity_sha256")))
                and type(value.get("table_counts")) is dict
                and all(re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", key) and type(count) is int and count >= 0
                        for key, count in value["table_counts"].items()), "PROBE_INVALID")
        return value

    def http(self, path: str, *, method: str = "GET", body: dict | None = None) -> dict:
        request = {"path": path, "method": method}
        if body is not None:
            request["body"] = body
        return self.python(fixture.HTTP_CODE, json.dumps(request))

    def api(self, mode: str) -> None:
        self.run("backend", ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000",
                             "--no-access-log"], detached=True, mode=mode, app=True)
        until = time.monotonic() + 90
        while time.monotonic() < until:
            item = self.inspect(self.project + "-backend")
            require(item.get("State", {}).get("Running") is True, "FIXTURE_NOT_READY")
            try:
                response = self.http("/health/ready")
                if response.get("http_status") == 200:
                    validate_health(response["body"], mode=mode, sha=self.candidate["candidate_sha"], ready=True)
                    live = self.http("/health")
                    require(live.get("http_status") == 200, "ADMISSION_MISMATCH")
                    validate_health(live["body"], mode=mode, sha=self.candidate["candidate_sha"], ready=False)
                    return
            except DrillError as error:
                if error.code not in {"COMMAND_FAILED", "PROBE_INVALID"}:
                    raise
            time.sleep(1)
        raise DrillError("FIXTURE_NOT_READY")

    def worker(self, role: str, *, mode: str, detached: bool = False) -> subprocess.CompletedProcess:
        service = "worker-" + (role.replace("_", "-") or "unset")
        return self.run(service, ["celery", "-A", "app.workers.celery_app:celery_app",
                        "worker", "--loglevel=WARNING", "--queues=" + (role or "ai"), "--concurrency=1",
                        "--pool=solo", "--without-gossip", "--without-mingle"], mode=mode, detached=detached,
                        overrides={"WORKER_ROLE": role}, check=detached, timeout=30, app=True)

    def stop_apps(self, *, remove: bool = False) -> None:
        for name in sorted(self.apps):
            self.docker("stop", "--time", "10", name, timeout=30)
            item = self.inspect(name)
            require(item.get("State", {}).get("Running") is False, "CONTAINMENT_FAILED")
            if remove:
                self.docker("rm", name)
                self.containers.pop(name)
        if remove:
            self.apps.clear()

    def fence(self) -> None:
        for service in ("postgres", "redis"):
            name = self.project + "-" + service
            item = self.inspect(name)
            networks = item.get("NetworkSettings", {}).get("Networks", {})
            require(set(networks) <= {self.network}, "FIXTURE_UNSAFE")
            if networks:
                self.docker("network", "disconnect", "--force", self.network, name)
            require(not self.inspect(name).get("NetworkSettings", {}).get("Networks"), "CONTAINMENT_FAILED")

    def reconnect(self, state: CandidateActivationState) -> None:
        for service in ("postgres", "redis"):
            state.assert_activation_permitted()
            self.docker("network", "connect", "--alias", service, self.network, self.project + "-" + service)

    def containment(self) -> dict:
        self.stop_and_fence()
        # Check every labelled app, including exited CLI attempts, through Docker.
        require(bool(self.apps), "CONTAINMENT_FAILED")
        for name in self.apps:
            item = self.inspect(name)
            require(item["State"]["Running"] is False and item["State"].get("Restarting") is False,
                    "CONTAINMENT_FAILED")
        return {"runtime_stopped": True, "data_services_fenced": True,
                "new_claims_blocked": True, "verification": "PHYSICAL_STOP_AND_NETWORK_FENCE"}

    def stop_and_fence(self) -> None:
        failures = []
        for action in (self.stop_apps, self.fence):
            try:
                action()
            except BaseException as error:
                failures.append(error)
        require(not failures, "CONTAINMENT_FAILED")

    def cleanup(self) -> bool:
        try:
            ids = self.docker("ps", "--all", "--quiet", "--filter",
                              "label=iteam.admission.operation=" + self.operation).stdout.split()
            for container in ids:
                require(re.fullmatch(r"[0-9a-f]{12,64}", container), "CLEANUP_FAILED")
                self.inspect(container)
                self.docker("rm", "--force", "--volumes", container)
            for kind in ("network", "volume"):
                names = self.docker(kind, "ls", "--quiet", "--filter",
                                    "label=iteam.admission.operation=" + self.operation).stdout.split()
                for name in names:
                    require(name == self.volume if kind == "volume" else bool(re.fullmatch(r"[0-9a-f]{12,64}", name)), "CLEANUP_FAILED")
                    self.docker(kind, "rm", name)
                require(not self.docker(kind, "ls", "--quiet", "--filter",
                                        "label=iteam.admission.operation=" + self.operation).stdout.strip(), "CLEANUP_FAILED")
            require(not self.docker("ps", "--all", "--quiet", "--filter",
                                    "label=iteam.admission.operation=" + self.operation).stdout.strip(), "CLEANUP_FAILED")
            return True
        except Exception:
            return False


def validate_health(value: dict, *, mode: str, sha: str, ready: bool) -> None:
    require(type(value) is dict and value.get("status") == "ok" and value.get("build_sha") == sha
            and value.get("admission_protocol") == PROTOCOL and type(value["admission_protocol"]) is int
            and value.get("admission_mode") == mode
            and value.get("business_ready") is (ready and mode == "active"), "ADMISSION_MISMATCH")
    if ready:
        require(all(value.get(key) == "ok" for key in ("database", "redis", "schema")), "ADMISSION_MISMATCH")
        if mode == "passive":
            require(value.get("database_read_only") is True, "ADMISSION_MISMATCH")


CAMPAIGN_REQUEST = {"name": fixture.CANARY_NAME, "goal": "Prove an actual committed HTTP write"}
CHAT_PATH = "/api/v1/marketing-conversations/" + fixture.CHAT_ID + "/messages"


def verify_unchanged(before: dict, after: dict) -> None:
    require(before == after and before.get("claim_status") == "QUEUED"
            and before.get("claim_started") is False and before.get("committed_canary_count") == 0,
            "PASSIVE_WRITE_DETECTED")


def passive_checks(runtime: Runtime) -> tuple[dict, dict]:
    runtime.commands.stage = "PASSIVE_BASELINE"
    before = runtime.snapshot()
    redis_before = runtime.python(fixture.REDIS_SNAPSHOT_CODE)
    sink_before = runtime.python(fixture.SINK_PROBE_CODE, "status")
    witness = RedisWitness(runtime.commands, runtime.project + "-redis")
    matrix = {}
    try:
        witness.start()
        runtime.commands.stage = "PASSIVE_BOOT"
        runtime.api("passive")
        runtime.commands.stage = "PASSIVE_HTTP"
        for name, response in (
            ("authenticated_api_write", runtime.http("/api/v1/campaigns", method="POST", body=CAMPAIGN_REQUEST)),
            ("stale_chat_get", runtime.http(CHAT_PATH)),
        ):
            require(response.get("http_status") == 503 and response.get("body", {}).get("error", {}).get("code") == "RUNTIME_PASSIVE",
                    "ADMISSION_MISMATCH")
            matrix[name] = "BLOCKED_RUNTIME_PASSIVE"
        runtime.commands.stage = "PASSIVE_WORKERS"
        for role in ("ai", "ai_control", "publication", "publication_control", "metrics", ""):
            result = runtime.worker(role, mode="passive")
            require(result.returncode != 0 and "RUNTIME_PASSIVE" in result.stderr + result.stdout,
                    "PASSIVE_PROCESS_NOT_BLOCKED")
            matrix["worker_" + (role or "unset")] = "BLOCKED_RUNTIME_PASSIVE"
        runtime.commands.stage = "PASSIVE_SCHEDULERS"
        for role in ("ai", "publication"):
            result = runtime.run("beat-" + role, ["celery", "-A", "app.workers.celery_app:celery_app", "beat",
                                  "--loglevel=WARNING", "--schedule=/tmp/celerybeat-schedule"],
                                 overrides={"SCHEDULER_ROLE": role}, timeout=30, check=False, app=True)
            require(result.returncode != 0 and "RUNTIME_PASSIVE" in result.stderr + result.stdout,
                    "PASSIVE_PROCESS_NOT_BLOCKED")
            matrix["scheduler_" + role] = "BLOCKED_RUNTIME_PASSIVE"
        runtime.commands.stage = "PASSIVE_DIRECT_PROBES"
        probes = runtime.python(fixture.PASSIVE_PROBES_CODE, mode="passive")
        require(probes.get("blocked") == ["database_session", "openai", "producer", "telegram", "vk"],
                "ADMISSION_MISMATCH")
        matrix["direct_provider_producer_session"] = "BLOCKED_RUNTIME_PASSIVE"
        require(probes.get("direct_engine_read_only") is True, "ADMISSION_MISMATCH")
        runtime.commands.stage = "PASSIVE_WITNESS_CLOSE"
        runtime.stop_apps()
        sink_after = runtime.python(fixture.SINK_PROBE_CODE, "status")
        require(sink_before == sink_after and sink_after.get("connections") == 4, "PROVIDER_ACTIVITY_DETECTED")
        observed = witness.finish()
    finally:
        witness.close()
    runtime.commands.stage = "PASSIVE_FINAL_SNAPSHOT"
    after = runtime.snapshot()
    redis_after = runtime.python(fixture.REDIS_SNAPSHOT_CODE)
    verify_unchanged(before, after)
    require(redis_before == redis_after, "REDIS_ACTIVITY_DETECTED")
    report = {
        "status": "PASS", "project": runtime.project, "operation_id": runtime.operation,
        "matrix": matrix, "business_writes_blocked": True, "claims_blocked": True,
        "provider_connections": 0, "provider_witness_selftest_connections": 4,
        "all_tables_sha256": before["all_tables_sha256"], "table_counts": before["table_counts"],
        "database_identity_sha256": before["database_identity_sha256"], "schema": runtime.schema,
        "broker_snapshot_sha256": redis_before["sha256"], "broker_keys": redis_before["key_count"],
        "redis_witness": observed, "real_broker_queued_canaries": 7,
        "fixture_kind": "ADVERSARIAL_TARGET_SCHEMA", "business_ready": False,
        "runtime_stopped_before_observation_closed": True,
    }
    return report, before


def activation_checks(runtime: Runtime, passive: dict, baseline: dict, state_dir: Path) -> dict:
    binding = {"authorization": AUTHORIZATION, "operation_id": runtime.operation,
               "project": runtime.project, "candidate_sha": runtime.candidate["candidate_sha"],
               "candidate_image_id": runtime.candidate["candidate_image_id"], "target_schema_head": runtime.schema}
    with ops.locked_state(state_dir):
        state = CandidateActivationState(state_dir, binding)
        try:
            runtime.commands.stage = "ACTIVATION_BOUNDARY"
            state.create()
            state.mark_passive_verified({
                "candidate_sha": binding["candidate_sha"], "candidate_image_id": binding["candidate_image_id"],
                "project": runtime.project, "passive_checks_sha256": digest(passive),
                "business_writes_blocked": True, "claims_blocked": True,
            })
            runtime.stop_apps(remove=True)
            runtime.fence()
            state.begin_activation()
            runtime.reconnect(state)
            state.assert_activation_permitted()
            runtime.commands.stage = "ACTIVE_BOOT"
            runtime.api("active")
            state.assert_activation_permitted()
            runtime.commands.stage = "ACTIVE_WORKER"
            runtime.worker("ai", mode="active", detached=True)
            state.mark_active()
            runtime.commands.stage = "ACTIVE_HTTP_COMMIT"
            response = runtime.http("/api/v1/campaigns", method="POST", body=CAMPAIGN_REQUEST)
            require(response.get("http_status") == 201 and response.get("body", {}).get("name") == fixture.CANARY_NAME,
                    "ACTIVE_WRITE_NOT_PROVED")
            until = time.monotonic() + 90
            runtime.commands.stage = "ACTIVE_CLAIM"
            while time.monotonic() < until:
                committed = runtime.snapshot()
                if (committed.get("claim_started") is True and committed.get("claim_finished") is True
                        and committed.get("claim_status") in {"COMPLETED", "FAILED"}):
                    break
                time.sleep(1)
            else:
                raise DrillError("ACTIVE_CLAIM_NOT_PROVED")
            require(committed.get("committed_canary_count") == 1
                    and committed["all_tables_sha256"] != baseline["all_tables_sha256"]
                    and committed["database_identity_sha256"] == baseline["database_identity_sha256"],
                    "ACTIVE_WRITE_NOT_PROVED")
            # The same stale GET is a positive control: it reaches the actual
            # business route and commits its recovery only in the active runtime.
            runtime.commands.stage = "ACTIVE_STALE_GET"
            response = runtime.http(CHAT_PATH)
            require(response.get("http_status") == 200, "ACTIVE_WRITE_NOT_PROVED")
            committed = runtime.snapshot()
            require(committed.get("stale_get_status") == "FAILED", "ACTIVE_WRITE_NOT_PROVED")
            runtime.commands.stage = "INJECTED_FAILURE"
            require(state.request_containment("INJECTED_POST_ACTIVATION_FAILURE"), "ACTIVATION_FAILED")
            runtime.commands.stage = "PHYSICAL_CONTAINMENT"
            physical = runtime.containment()
            runtime.commands.stage = "CONTAINED_SNAPSHOT"
            after = runtime.snapshot(fenced=True)
            require(after == committed, "COMMITTED_DATA_CHANGED")
            evidence = {"target_schema_head": runtime.schema, "committed_write_preserved": True,
                        "new_claims_blocked": physical["new_claims_blocked"], "runtime_stopped": physical["runtime_stopped"],
                        "evidence_sha256": digest({"physical": physical, "after": after})}
            runtime.commands.stage = "CONTAINMENT_RECORD"
            summary = state.finish_containment(True, evidence=evidence)
            require(summary.get("phase") == "CONTAINED" and summary.get("state_durability") == "CONFIRMED"
                    and summary.get("containment_verified") is True, "CONTAINMENT_FAILED")
            blocked = False
            try:
                state.assert_activation_permitted()
            except ops.ReleaseError:
                blocked = True
            require(blocked, "CONTAINMENT_FAILED")
            return {
                "status": "PASS", "project": runtime.project, "operation_id": runtime.operation,
                "passive_checks_sha256": digest(passive), "baseline_schema": runtime.schema,
                "http_write_committed": True, "real_worker_claim_committed": True,
                "active_stale_get_recovery_committed": True, "claim_status": committed["claim_status"],
                "committed_all_tables_sha256": committed["all_tables_sha256"],
                "contained_all_tables_sha256": after["all_tables_sha256"],
                "database_identity_sha256": after["database_identity_sha256"],
                "committed_canary_count": after["committed_canary_count"],
                "target_schema_preserved": True, "committed_write_preserved": True,
                "downgrade_attempts": 0, "restore_attempts": 0, "same_process_reactivation_blocked": True,
                "physical_containment": physical, "activation_state": summary,
                "boundary_before_reconnect_and_recreate": True,
            }
        except BaseException:
            # A journal error cannot prevent the independent physical stop.
            state.request_containment("CANDIDATE_ACTIVATION_FAILED")
            try:
                runtime.stop_and_fence()
            finally:
                state.finish_containment(False)
            raise


def run_scenario(commands: Commands, candidate: dict, infra: dict, *, activate: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temporary:
        root = Path(temporary).resolve()
        root.chmod(0o700)
        runtime = Runtime(commands, candidate, root, infra, schema=fixture.CHECKOUT_SCHEMA)
        report = None
        failure = None
        try:
            runtime.start()
            passive, baseline = passive_checks(runtime)
            report = {"passive": passive}
            if activate:
                report["activation"] = activation_checks(runtime, passive, baseline, root / "state")
        except BaseException as error:
            failure = DrillError(safe_error(error))
            failure.stage = commands.stage if commands.stage in STAGES else "UNKNOWN"
        commands.stage = "FIXTURE_CLEANUP"
        cleaned = runtime.cleanup()
        require(cleaned, "CLEANUP_FAILED")
        if failure is not None:
            raise failure
        report["cleanup_verified"] = True
        return report


def run_drill(root: Path) -> dict:
    commands = Commands()
    with tempfile.TemporaryDirectory(prefix=BUILD_PREFIX) as temporary:
        candidate = Candidate(commands, root, Path(temporary))
        failure = None
        report = None
        try:
            metadata = candidate.build()
            commands.stage = "INFRASTRUCTURE_IMAGES"
            infra = {}
            for service, image in (("postgres", "postgres:17-alpine"), ("redis", "redis:8-alpine")):
                commands.run(["docker", "pull", image], timeout=180)
                values = json.loads(commands.run(["docker", "image", "inspect", image]).stdout)
                image_id = values[0]["Id"]
                require(IMAGE_PATTERN.fullmatch(image_id), "IMAGE_MISMATCH")
                infra[service] = image_id
            passive = run_scenario(commands, metadata, infra, activate=False)
            active = run_scenario(commands, metadata, infra, activate=True)
            require(commands.forbidden_attempts == 0, "COMMAND_FORBIDDEN")
            report = {
                "proof_version": 1, "proof_kind": "RUNTIME_ADMISSION_CANDIDATE",
                "authorization": AUTHORIZATION, "status": "PASS", "created_at": datetime.now(UTC).isoformat(),
                **FALSE_AUTHORITY, "candidate": metadata, "infrastructure_image_ids": infra,
                "passive_scenario": passive, "activation_failure_scenario": active,
                "downgrade_attempts": 0, "restore_attempts": 0,
            }
        except BaseException as error:
            failure = DrillError(safe_error(error))
            failure.stage = getattr(error, "stage", commands.stage)
        commands.stage = "CANDIDATE_CLEANUP"
        cleaned = candidate.cleanup()
        require(cleaned, "CLEANUP_FAILED")
        if failure is not None:
            raise failure
        report["candidate_image_cleanup_verified"] = True
        report["cleanup_verified"] = True
        return report


def publish(path: Path, value: dict) -> None:
    require(not path.exists() and not path.is_symlink() and path.parent.is_dir()
            and path.parent.resolve() == path.parent.absolute(), "OUTPUT_UNSAFE")
    fd, name = tempfile.mkstemp(prefix=".admission-proof-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DrillError("ARGUMENTS_INVALID")


def main(argv: list[str] | None = None) -> int:
    try:
        parser = Parser(description=__doc__, allow_abbrev=False)
        parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(argv)
        output = args.output.absolute()
        require(not output.exists() and not output.is_symlink() and output.parent.is_dir()
                and output.parent.resolve() == output.parent, "OUTPUT_UNSAFE")
        report = run_drill(Path(__file__).resolve().parents[1])
        publish(output, report)
        print(json.dumps(report, sort_keys=True))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps({"status": "FAIL", "error_code": safe_error(error),
                          "error_stage": getattr(error, "stage", "UNKNOWN") if getattr(error, "stage", "UNKNOWN") in STAGES else "UNKNOWN",
                          "authorization": AUTHORIZATION, **FALSE_AUTHORITY}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
