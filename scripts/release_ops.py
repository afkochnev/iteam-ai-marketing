#!/usr/bin/env python3
"""Verified release preparation and digest-only local Compose operations.

No SSH runner, publishing call, database business-state edit or database downgrade.
Command output is captured: subprocess errors must never echo resolved secrets.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = "afkochnev/iteam-ai-marketing"
SCHEMA = "20261009_0028"
API_URL = "http://localhost:8000/api/v1"
REVIEWER = "afkochnev"
APP = (
    "backend",
    "frontend",
    "ai_worker",
    "ai_control_worker",
    "ai_scheduler",
    "publication_worker",
    "publication_control_worker",
    "publication_scheduler",
    "metrics_worker",
)
WORKERS = APP[2:]
SCHEDULERS = ("ai_scheduler", "publication_scheduler")
ALL = {"postgres", "redis", *APP}
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ReleaseError(RuntimeError):
    pass


def require(value: Any, code: str) -> None:
    if not value:
        raise ReleaseError(code)


def command(
    args: list[str], *, cwd: Path | None = None, input: str | None = None
) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            input=input,
            text=True,
            capture_output=True,
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("TOOL_UNAVAILABLE") from None
    require(result.returncode == 0, "EXTERNAL_COMMAND_FAILED")
    return result.stdout.strip()


def load(path: Path) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            require(key not in result, "DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    return json.loads(path.read_text(), object_pairs_hook=pairs)


def atomic(path: Path, content: str) -> None:
    require(not path.is_symlink(), "SYMLINK_OUTPUT")
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(path: Path, value: Any) -> None:
    atomic(path, json.dumps(value, sort_keys=True, indent=2) + "\n")


def gh(endpoint: str) -> Any:
    return json.loads(command(["gh", "api", "repos/" + REPO + "/" + endpoint]))


def accepted(sha: str) -> None:
    require(SHA.fullmatch(sha), "INVALID_RELEASE_SHA")
    comparison = gh("compare/" + sha + "...main")
    require(comparison["status"] in {"identical", "ahead"}, "RELEASE_NOT_ON_MAIN")
    runs = gh("actions/workflows/ci.yml/runs?branch=main&event=push&head_sha=" + sha)
    matching = [run for run in runs["workflow_runs"] if run["head_sha"] == sha]
    require(
        matching
        and matching[0]["conclusion"] == "success"
        and matching[0]["status"] == "completed",
        "EXACT_MAIN_CI_NOT_GREEN",
    )


def gates() -> None:
    protection = gh("branches/main/protection")
    require(protection.get("enforce_admins", {}).get("enabled"), "MAIN_ADMIN_BYPASS")
    checks = protection.get("required_status_checks") or {}
    require(
        checks.get("strict")
        and {"backend", "frontend", "release-tools"} <= set(checks.get("contexts", [])),
        "MAIN_CHECKS_MISSING",
    )
    require(
        protection.get("required_pull_request_reviews") is not None,
        "MAIN_PR_GATE_MISSING",
    )
    for key in ("allow_force_pushes", "allow_deletions"):
        require(
            not (protection.get(key) or {}).get("enabled", False), "UNSAFE_MAIN_POLICY"
        )
    require(
        protection.get("required_conversation_resolution", {}).get("enabled"),
        "UNRESOLVED_CONVERSATIONS_ALLOWED",
    )
    environment = gh("environments/production")
    require(
        (environment.get("deployment_branch_policy") or {}).get("protected_branches"),
        "PRODUCTION_BRANCH_GATE_MISSING",
    )
    rules = environment.get("protection_rules", [])
    reviewers = [
        entry
        for rule in rules
        if rule["type"] == "required_reviewers"
        for entry in rule.get("reviewers", [])
    ]
    require(
        len(reviewers) == 1
        and reviewers[0]["type"] == "User"
        and reviewers[0]["reviewer"]["login"] == REVIEWER,
        "PRODUCTION_REVIEWER_GATE_MISSING",
    )


def run_info(run_id: int, workflow: str, event: str) -> dict[str, Any]:
    require(run_id > 0, "INVALID_RUN_ID")
    info = gh(f"actions/runs/{run_id}")
    require(
        info["status"] == "completed" and info["conclusion"] == "success",
        "WORKFLOW_NOT_SUCCESSFUL",
    )
    require(
        info["path"] == ".github/workflows/" + workflow
        and info["event"] == event
        and info["head_branch"] == "main"
        and info["repository"]["full_name"] == REPO,
        "UNTRUSTED_WORKFLOW",
    )
    return info


def image_ref(component: str, value: str) -> str:
    prefix = "ghcr.io/" + REPO + "-" + component + "@"
    require(
        isinstance(value, str)
        and value.startswith(prefix)
        and DIGEST.fullmatch(value[len(prefix) :]),
        "IMAGE_MUST_BE_EXPECTED_REPOSITORY_DIGEST",
    )
    return value


def validate_manifest(value: dict[str, Any]) -> None:
    require(
        set(value)
        == {
            "version",
            "repository",
            "release_sha",
            "build_run_id",
            "build_attempt",
            "alembic_head",
            "frontend_api_url",
            "images",
        },
        "UNEXPECTED_MANIFEST_FIELDS",
    )
    require(
        value["version"] == 1
        and value["repository"] == REPO
        and SHA.fullmatch(value["release_sha"]),
        "INVALID_MANIFEST_IDENTITY",
    )
    require(
        type(value["build_run_id"]) is int
        and value["build_run_id"] > 0
        and type(value["build_attempt"]) is int
        and value["build_attempt"] > 0,
        "INVALID_BUILD_RUN",
    )
    require(
        value["alembic_head"] == SCHEMA and value["frontend_api_url"] == API_URL,
        "UNREVIEWED_SCHEMA_OR_FRONTEND_ENDPOINT",
    )
    require(set(value["images"]) == {"backend", "frontend"}, "INVALID_COMPONENTS")
    for component, ref in value["images"].items():
        image_ref(component, ref)


def verify_image(component: str, ref: str, sha: str, cosign: str) -> None:
    image_ref(component, ref)
    command(
        [
            cosign,
            "verify",
            "--certificate-identity",
            f"https://github.com/{REPO}/.github/workflows/release-images.yml@refs/heads/main",
            "--certificate-oidc-issuer",
            "https://token.actions.githubusercontent.com",
            ref,
        ]
    )

    def inspect(fmt: str) -> Any:
        return json.loads(
            command(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    ref,
                    "--format",
                    "{{json ." + fmt + "}}",
                ]
            )
        )

    manifest = inspect("Manifest")
    require(ref.endswith(manifest["digest"]), "REGISTRY_DIGEST_MISMATCH")
    actual = {
        f"{item['platform']['os']}/{item['platform']['architecture']}"
        for item in manifest["manifests"]
    }
    require({"linux/arm64", "linux/amd64"} <= actual, "MISSING_ARCHITECTURE")
    sbom, provenance = inspect("SBOM"), inspect("Provenance")
    for platform in ("linux/amd64", "linux/arm64"):
        require(sbom[platform]["SPDX"]["packages"], "SBOM_MISSING")
        args = provenance[platform]["SLSA"]["buildDefinition"]["externalParameters"][
            "request"
        ]["args"]
        require(
            args.get("build-arg:BUILD_SHA") == sha
            and args.get("label:org.opencontainers.image.revision") == sha
            and args.get("label:org.opencontainers.image.source")
            == "https://github.com/" + REPO,
            "PROVENANCE_SHA_MISMATCH",
        )
        if component == "frontend":
            require(
                args.get("build-arg:NEXT_PUBLIC_API_URL") == API_URL,
                "FRONTEND_ENDPOINT_MISMATCH",
            )


def prepare(build_run_id: int, output: Path, cosign: str) -> dict[str, Any]:
    info = run_info(build_run_id, "release-images.yml", "workflow_run")
    sha = info["head_sha"]
    accepted(sha)
    images = {}
    with tempfile.TemporaryDirectory(prefix="iteam-release-records-") as temporary:
        for component in ("backend", "frontend"):
            directory = Path(temporary) / component
            command(
                [
                    "gh",
                    "run",
                    "download",
                    str(build_run_id),
                    "--repo",
                    REPO,
                    "--name",
                    f"release-{component}-{sha}",
                    "--dir",
                    str(directory),
                ]
            )
            record = load(directory / f"release-{component}.json")
            require(
                record["release_sha"] == sha
                and set(record["platforms"]) == {"linux/arm64", "linux/amd64"}
                and record["frontend_api_url"] == API_URL,
                "RELEASE_RECORD_MISMATCH",
            )
            images[component] = record["image"]
            verify_image(component, images[component], sha, cosign)
    value = {
        "version": 1,
        "repository": REPO,
        "release_sha": sha,
        "build_run_id": build_run_id,
        "build_attempt": info["run_attempt"],
        "alembic_head": SCHEMA,
        "frontend_api_url": API_URL,
        "images": images,
    }
    validate_manifest(value)
    save(output, value)
    return value


def approved(approval_id: int, output: Path, cosign: str) -> tuple[dict[str, Any], str]:
    gates()
    approval = run_info(approval_id, "approve-release.yml", "workflow_dispatch")
    require(approval["run_attempt"] == 1, "APPROVAL_RERUN_FORBIDDEN")
    accepted(approval["head_sha"])
    age = (
        datetime.now(UTC)
        - datetime.fromisoformat(approval["updated_at"].replace("Z", "+00:00"))
    ).total_seconds()
    require(0 <= age <= 3600, "APPROVAL_EXPIRED")
    history = gh(f"actions/runs/{approval_id}/approvals")
    reviews = [
        item
        for item in history
        if any(env["name"] == "production" for env in item["environments"])
    ]
    require(
        reviews
        and all(
            item["state"] == "approved"
            and item["user"]["login"] == REVIEWER
            and item["user"]["type"] == "User"
            for item in reviews
        ),
        "EXPLICIT_HUMAN_APPROVAL_MISSING",
    )
    command(
        [
            "gh",
            "run",
            "download",
            str(approval_id),
            "--repo",
            REPO,
            "--name",
            "approved-release",
            "--dir",
            str(output),
        ]
    )
    manifest_path = output / "release.json"
    command(
        [
            cosign,
            "verify-blob",
            "--bundle",
            str(output / "release.sigstore.json"),
            "--certificate-identity",
            f"https://github.com/{REPO}/.github/workflows/approve-release.yml@refs/heads/main",
            "--certificate-oidc-issuer",
            "https://token.actions.githubusercontent.com",
            str(manifest_path),
        ]
    )
    receipt = load(manifest_path)
    require(
        set(receipt)
        == {
            "approval_run_id",
            "approval_attempt",
            "approval_workflow_sha",
            "expected_source_release",
            "release",
        }
        and receipt["approval_run_id"] == approval_id
        and receipt["approval_attempt"] == 1
        and receipt["approval_workflow_sha"] == approval["head_sha"],
        "APPROVAL_RECEIPT_MISMATCH",
    )
    require(
        SHA.fullmatch(receipt["expected_source_release"]), "INVALID_EXPECTED_SOURCE"
    )
    value = receipt["release"]
    validate_manifest(value)
    # Re-verify the build rather than trusting self-asserted fields in an artifact.
    rebuilt = prepare(value["build_run_id"], output / "verified.json", cosign)
    require(value == rebuilt, "APPROVED_MANIFEST_CHANGED")
    return value, receipt["expected_source_release"]


def overlay(value: dict[str, Any], *, admin: bool = True) -> dict[str, Any]:
    validate_manifest(value)
    services = {
        name: {
            "image": value["images"]["frontend" if name == "frontend" else "backend"]
        }
        for name in APP
    }
    if admin:
        services["vk_credentials_admin"] = {"image": value["images"]["backend"]}
    return {"services": services}


def env_value(content: str, key: str) -> str:
    values = [
        line.split("=", 1)[1]
        for line in content.splitlines()
        if line.startswith(key + "=")
    ]
    require(len(values) <= 1, "DUPLICATE_ENV_KEY")
    return values[0] if values else ""


def set_env(content: str, updates: dict[str, str]) -> str:
    result = []
    remaining = dict(updates)
    for line in content.splitlines(keepends=True):
        key = line.split("=", 1)[0]
        if key in updates:
            require(key in remaining, "DUPLICATE_ENV_KEY")
            ending = (
                "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            )
            result.append(key + "=" + remaining.pop(key) + ending)
        else:
            result.append(line)
    if remaining and result and not result[-1].endswith("\n"):
        result[-1] += "\n"
    result.extend(key + "=" + value + "\n" for key, value in remaining.items())
    return "".join(result)


# Executed inside an existing backend container. Only persisted read models leave it.
SNAPSHOT_CODE = """
import asyncio,json
from sqlalchemy import text
from app.core.database import async_session_factory
from app.core.config import settings
async def main():
 async with async_session_factory() as s:
  await s.execute(text('SET TRANSACTION READ ONLY'))
  unsafe=await s.scalar(text("SELECT count(*) FROM publications WHERE status='PUBLISHING' OR (status='SCHEDULED' AND (scheduled_at IS NULL OR scheduled_at <= now()+interval '30 minutes'))"))
  runs=await s.scalar(text("SELECT count(*) FROM agent_runs WHERE status='RUNNING'"))
  rows=(await s.execute(text("SELECT id,channel,status,content_item_id,content_version_id,scheduled_at,provider_target_id,external_id,published_at,retry_count,failure_code,execution_token IS NOT NULL AS has_execution_token FROM publications ORDER BY id"))).mappings().all()
  audit=(await s.execute(text("SELECT id,event_type,operation_key,metadata FROM activity_logs WHERE metadata ? 'publication_id' ORDER BY id"))).mappings().all()
  schema=await s.scalar(text('SELECT version_num FROM alembic_version'))
  import hashlib
  print(json.dumps({'build_sha':settings.build_sha,'schema':schema,'unsafe':unsafe,'agent_runs':runs,'vk':settings.vk_publishing_enabled,'telegram':settings.telegram_publishing_enabled,'canary':str(settings.vk_canary_publication_id or ''),'publication_hash':hashlib.sha256(json.dumps([dict(r) for r in rows],default=str,sort_keys=True).encode()).hexdigest(),'audit_hash':hashlib.sha256(json.dumps([dict(r) for r in audit],default=str,sort_keys=True).encode()).hexdigest()}))
asyncio.run(main())
"""


CREDENTIAL_CODE = """
import json,os,stat
from pathlib import Path
from app.core.config import settings
result={'vk_checked':False,'telegram_checked':False}
if settings.vk_publishing_enabled:
 from app.integrations.telegram import VKProvider
 from app.integrations.vk_credentials import VKCredentialStore
 provider=VKProvider(single_attempt=True)
 assert provider._token is None and isinstance(provider._credential_store,VKCredentialStore)
 assert not settings.vk_access_token
 store=provider._credential_store
 meta=store.path.lstat()
 assert os.geteuid()==meta.st_uid==10001 and stat.S_IMODE(meta.st_mode)==0o600
 assert stat.S_ISREG(meta.st_mode) and stat.S_IMODE(store.path.parent.stat().st_mode)==0o700
 value=store._read_unlocked()
 assert value.client_id==settings.vk_client_id and value.user_id==settings.vk_expected_user_id
 assert value.owner_id==settings.vk_owner_id and value.refresh_token
 result.update(vk_checked=True,vk_owner=value.owner_id,file_uid=meta.st_uid,file_mode='0600')
if settings.telegram_publishing_enabled:
 assert settings.telegram_bot_token and settings.telegram_target_chat_id
 result['telegram_checked']=True
print(json.dumps(result))
"""


class Runtime:
    def __init__(
        self,
        root: Path,
        *,
        project: str | None = None,
        ready_url: str = "http://127.0.0.1:8000/health/ready",
    ):
        self.root = root.resolve()
        self.env_path = self.root / ".env"
        self.ready_url = ready_url
        self.base = ["docker", "compose", "--env-file", str(self.env_path)]
        if project:
            self.base += ["--project-name", project]
        self.base += ["--profile", "publishing"]

    def compose(self, *args: str) -> str:
        return command(self.base + list(args), cwd=self.root)

    def inspect(self, service: str) -> dict[str, Any]:
        ids = self.compose("ps", "-q", service).splitlines()
        require(len(ids) == 1, "SERVICE_INSTANCE_COUNT:" + service)
        return json.loads(command(["docker", "inspect", ids[0]]))[0]

    def snapshot(self) -> dict[str, Any]:
        result = json.loads(
            self.compose("exec", "-T", "backend", "python", "-c", SNAPSHOT_CODE)
        )
        require(result["schema"] == SCHEMA, "SCHEMA_HEAD_MISMATCH")
        require(
            result["unsafe"] == result["agent_runs"] == 0, "WORK_IN_FLIGHT_OR_DUE_SOON"
        )
        require(not result["canary"], "CANARY_GATE_ACTIVE")
        return result

    def topology(self) -> dict[str, Any]:
        result = {}
        for name in sorted(ALL):
            data = self.inspect(name)
            require(data["State"]["Running"], "SERVICE_NOT_RUNNING:" + name)
            config = data["Config"]
            env = sorted(
                line
                for line in config["Env"]
                if not line.startswith(("BUILD_SHA=", "COMPOSE_FILE="))
            )
            mounts = sorted(
                (
                    item["Destination"],
                    item.get("Name", item.get("Source", "")),
                    item["Type"],
                    item["RW"],
                )
                for item in data["Mounts"]
            )
            result[name] = {
                "image": data["Image"],
                "runtime_policy_hash": hashlib.sha256(
                    json.dumps(
                        {
                            "user": config.get("User"),
                            "workdir": config.get("WorkingDir"),
                            "networks": sorted(data["NetworkSettings"]["Networks"]),
                            **{
                                key: data["HostConfig"].get(key)
                                for key in (
                                    "PortBindings",
                                    "Privileged",
                                    "ReadonlyRootfs",
                                    "CapAdd",
                                    "CapDrop",
                                    "RestartPolicy",
                                    "Memory",
                                    "NanoCpus",
                                    "PidsLimit",
                                    "ExtraHosts",
                                )
                            },
                        },
                        sort_keys=True,
                    ).encode()
                ).hexdigest(),
                "reference": config["Image"],
                "env_hash": hashlib.sha256(json.dumps(env).encode()).hexdigest(),
                "command": config["Cmd"],
                "mounts": mounts,
                "revision": (config.get("Labels") or {}).get(
                    "org.opencontainers.image.revision"
                ),
            }
        configured = json.loads(self.compose("config", "--format", "json"))
        # A rendered Compose config may contain secrets. Persist only a hash of the policy.
        publication_mounts = [
            mount
            for mount in configured["services"]["publication_worker"].get("volumes", [])
            if mount.get("target") == "/run/iteam-secrets/vk"
        ]
        require(
            len(publication_mounts) == 1
            and publication_mounts[0].get("type") == "volume"
            and not publication_mounts[0].get("read_only", False),
            "CREDENTIAL_VOLUME_MISSING",
        )
        source = publication_mounts[0]["source"]
        credential_services = {
            name
            for name, service in configured["services"].items()
            for mount in service.get("volumes", [])
            if mount.get("source") == source
        }
        require(
            credential_services <= {"publication_worker", "vk_credentials_admin"},
            "CREDENTIAL_MOUNT_LEAK",
        )
        return result

    def credentials(self) -> dict[str, Any]:
        return json.loads(
            self.compose(
                "exec", "-T", "publication_worker", "python", "-c", CREDENTIAL_CODE
            )
        )

    def ready(self) -> None:
        for _ in range(120):
            try:
                with urllib.request.urlopen(self.ready_url, timeout=3) as response:
                    data = json.load(response)
                    if response.status == 200 and all(
                        data.get(key) == "ok"
                        for key in ("status", "database", "redis", "schema")
                    ):
                        return
            except (OSError, ValueError):
                pass
            time.sleep(1)
        raise ReleaseError("READINESS_TIMEOUT")

    def frontend_ready(self) -> None:
        for _ in range(90):
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:3000/publications", timeout=3
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(1)
        raise ReleaseError("FRONTEND_READINESS_TIMEOUT")

    def workers_ready(self) -> None:
        expected = {
            "celery@" + self.inspect(name)["Config"]["Hostname"]: name.removesuffix(
                "_worker"
            )
            for name in WORKERS
            if name.endswith("_worker")
        }
        code = """
import json,sys
from app.workers.celery_app import celery_app
expected=json.loads(sys.argv[1])
actual=celery_app.control.inspect(timeout=3).active_queues() or {}
print(json.dumps({name:any(entry.get('name')==queue for entry in actual.get(name,[])) for name,queue in expected.items()}))
"""
        for _ in range(24):
            try:
                result = json.loads(
                    self.compose(
                        "exec",
                        "-T",
                        "backend",
                        "python",
                        "-c",
                        code,
                        json.dumps(expected),
                    )
                )
                if result.keys() == expected.keys() and all(
                    value is True for value in result.values()
                ):
                    return
            except (ReleaseError, ValueError):
                pass
            time.sleep(2)
        raise ReleaseError("WORKER_QUEUES_NOT_READY")

    def restart(self, *, migration_check: bool = True) -> None:
        if migration_check:
            self.compose(
                "run", "--rm", "--no-deps", "-T", "backend", "alembic", "check"
            )
        self.compose(
            "up", "-d", "--no-deps", "--no-build", "--force-recreate", "backend"
        )
        self.ready()
        self.compose(
            "up", "-d", "--no-deps", "--no-build", "--force-recreate", "frontend"
        )
        self.frontend_ready()
        self.snapshot()
        self.compose(
            "up", "-d", "--no-deps", "--no-build", "--force-recreate", *WORKERS
        )
        self.ready()
        self.workers_ready()


def compare(before: dict[str, Any], after: dict[str, Any], sha: str) -> None:
    require(before.keys() == after.keys(), "SERVICE_TOPOLOGY_CHANGED")
    for name, old in before.items():
        new = after[name]
        for key in ("env_hash", "runtime_policy_hash", "command", "mounts"):
            require(old[key] == new[key], "RUNTIME_POLICY_CHANGED:" + name + ":" + key)
        if name in APP:
            require(new["revision"] == sha, "IMAGE_REVISION_MISMATCH:" + name)
        else:
            require(new["image"] == old["image"], "DATA_SERVICE_IMAGE_CHANGED")


def compare_snapshot(before: dict[str, Any], after: dict[str, Any], sha: str) -> None:
    require(after["build_sha"] == sha, "BUILD_SHA_MISMATCH")
    for key in ("schema", "vk", "telegram", "canary", "publication_hash", "audit_hash"):
        require(after[key] == before[key], "PERSISTED_STATE_CHANGED:" + key)


def pull_and_validate(runtime: Runtime, value: dict[str, Any]) -> None:
    for component, ref in value["images"].items():
        command(["docker", "pull", ref])
        data = json.loads(command(["docker", "image", "inspect", ref]))[0]
        require(
            (data["Config"].get("Labels") or {}).get(
                "org.opencontainers.image.revision"
            )
            == value["release_sha"],
            "PULLED_IMAGE_REVISION_MISMATCH",
        )
        if component == "backend":
            require(
                "BUILD_SHA=" + value["release_sha"] in data["Config"]["Env"],
                "PULLED_IMAGE_BUILD_SHA_MISMATCH",
            )
            head = command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--entrypoint",
                    "alembic",
                    ref,
                    "heads",
                ]
            )
            require(head == SCHEMA + " (head)", "IMAGE_SCHEMA_NOT_COMPATIBLE")


def switch(
    runtime: Runtime,
    value: dict[str, Any],
    state: Path,
    operation: str,
    *,
    git_checkout: bool = True,
) -> dict[str, Any]:
    """The same switching/rollback engine is used by production and the isolated drill."""
    validate_manifest(value)
    content = runtime.env_path.read_text()
    before = runtime.snapshot()
    old_sha = before["build_sha"]
    require(
        SHA.fullmatch(old_sha) and env_value(content, "BUILD_SHA") == old_sha,
        "SOURCE_BUILD_SHA_MISMATCH",
    )
    baseline = runtime.topology()
    credentials = runtime.credentials()
    require(
        all(baseline[name]["revision"] == old_sha for name in APP),
        "SOURCE_IMAGE_REVISION_MISMATCH",
    )
    pull_and_validate(runtime, value)
    original_compose = env_value(content, "COMPOSE_FILE") or "docker-compose.yml"
    old_root = (
        command(["git", "rev-parse", "HEAD"], cwd=runtime.root)
        if git_checkout
        else old_sha
    )
    if git_checkout:
        require(
            old_root == old_sha
            and not command(["git", "status", "--porcelain"], cwd=runtime.root),
            "DIRTY_OR_UNEXPECTED_PRODUCTION_CHECKOUT",
        )
        command(["git", "fetch", "origin", "main"], cwd=runtime.root)
        require(
            not command(
                ["git", "diff", old_sha, value["release_sha"], "--", "backend/alembic"],
                cwd=runtime.root,
            ),
            "SCHEMA_CHANGE_REQUIRES_SEPARATE_REVIEW",
        )
        require(
            not command(
                [
                    "git",
                    "diff",
                    old_sha,
                    value["release_sha"],
                    "--",
                    "docker-compose.yml",
                ],
                cwd=runtime.root,
            ),
            "COMPOSE_CHANGE_REQUIRES_REVIEW",
        )
    # Pins protect the exact pre-deploy local image IDs from routine image pruning.
    for name in APP:
        command(
            [
                "docker",
                "tag",
                baseline[name]["image"],
                f"iteam-rollback-{operation}-{name}",
            ]
        )
    save(
        state / (operation + "-before.json"),
        {
            "manifest_sha": old_sha,
            "topology": baseline,
            "snapshot": before,
            "compose_file": original_compose,
        },
    )
    new_overlay = state / (operation + "-images.json")
    save(
        new_overlay,
        overlay(
            value,
            admin="vk_credentials_admin"
            in json.loads(runtime.compose("config", "--format", "json"))["services"],
        ),
    )
    paused = False
    try:
        runtime.snapshot()
        paused = True
        runtime.compose("stop", *SCHEDULERS)
        runtime.compose("stop", *(name for name in WORKERS if name not in SCHEDULERS))
        runtime.snapshot()
        if git_checkout:
            command(
                ["git", "switch", "--detach", value["release_sha"]], cwd=runtime.root
            )
        atomic(
            runtime.env_path,
            set_env(
                content,
                {
                    "BUILD_SHA": value["release_sha"],
                    "COMPOSE_FILE": original_compose + os.pathsep + str(new_overlay),
                },
            ),
        )
        runtime.restart()
        after, topology = runtime.snapshot(), runtime.topology()
        compare_snapshot(before, after, value["release_sha"])
        compare(baseline, topology, value["release_sha"])
        require(runtime.credentials() == credentials, "CREDENTIAL_POLICY_CHANGED")
        result = {
            "status": "PASS",
            "release_sha": value["release_sha"],
            "previous_sha": old_sha,
            "provider_gates_unchanged": True,
            "publication_state_unchanged": True,
            "database_downgrade": False,
            "services": len(topology),
        }
        save(state / (operation + "-result.json"), result)
        save(state / "current-release.json", value)
        return result
    except Exception:
        # Never restore business data or lower the Alembic revision automatically.
        if paused:
            try:
                runtime.compose("stop", *SCHEDULERS)
                runtime.compose(
                    "stop", *(name for name in WORKERS if name not in SCHEDULERS)
                )
                rollback_overlay = state / (operation + "-rollback-images.json")
                save(
                    rollback_overlay,
                    {
                        "services": {
                            name: {"image": baseline[name]["image"]} for name in APP
                        }
                    },
                )
                if git_checkout:
                    command(["git", "switch", "--detach", old_root], cwd=runtime.root)
                atomic(
                    runtime.env_path,
                    set_env(
                        content,
                        {
                            "COMPOSE_FILE": original_compose
                            + os.pathsep
                            + str(rollback_overlay)
                        },
                    ),
                )
                runtime.restart(migration_check=False)
                compare_snapshot(before, runtime.snapshot(), old_sha)
                compare(baseline, runtime.topology(), old_sha)
                require(
                    runtime.credentials() == credentials,
                    "ROLLBACK_CREDENTIAL_POLICY_CHANGED",
                )
                save(
                    state / (operation + "-result.json"),
                    {
                        "status": "ROLLED_BACK",
                        "previous_sha": old_sha,
                        "database_downgrade": False,
                    },
                )
            except Exception:
                raise ReleaseError("ROLLBACK_REQUIRES_OPERATOR_ATTENTION") from None
        raise ReleaseError("DEPLOY_FAILED_ROLLBACK_COMPLETED") from None


@contextlib.contextmanager
def locked_state(state: Path):
    require(state.is_absolute() and not state.is_symlink(), "UNSAFE_STATE_DIRECTORY")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = state.stat()
    require(
        metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
        "UNSAFE_STATE_DIRECTORY_MODE_OR_OWNER",
    )
    fd = os.open(
        state / "operation.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise ReleaseError("ANOTHER_RELEASE_OPERATION_RUNNING") from None
    finally:
        os.close(fd)


def apply(
    root: Path, state: Path, approval_id: int, backup_proof: Path, cosign: str
) -> Any:
    from release_backup import check_proof

    with locked_state(state):
        with tempfile.TemporaryDirectory(prefix="approved-release-") as temporary:
            value, expected_source = approved(approval_id, Path(temporary), cosign)
        runtime = Runtime(root)
        before = runtime.snapshot()
        require(
            before["build_sha"] == expected_source, "APPROVED_SOURCE_RELEASE_CHANGED"
        )
        accepted(before["build_sha"])
        require(
            env_value(runtime.env_path.read_text(), "APP_ENV")
            in {"production", "prod"},
            "NOT_PRODUCTION_ENVIRONMENT",
        )
        require(
            json.loads(runtime.compose("config", "--format", "json"))["name"]
            == "iteam-ai-marketing",
            "UNEXPECTED_PRODUCTION_PROJECT",
        )
        check_proof(backup_proof, before["build_sha"])
        marker = state / f"approval-{approval_id}.consumed"
        try:
            fd = os.open(
                marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            raise ReleaseError("APPROVAL_ALREADY_USED_NO_AUTOMATIC_RETRY") from None
        with os.fdopen(fd, "w") as stream:
            stream.write(datetime.now(UTC).isoformat())
            stream.flush()
            os.fsync(stream.fileno())
        return switch(runtime, value, state, str(approval_id))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cosign", default="cosign")
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--build-run", type=int, required=True)
    prep.add_argument("--output", type=Path, required=True)
    sub.add_parser("check-gates")
    deployment = sub.add_parser("apply")
    deployment.add_argument("--root", type=Path, required=True)
    deployment.add_argument("--approval-run", type=int, required=True)
    deployment.add_argument("--backup-proof", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.action == "prepare":
            result = prepare(args.build_run, args.output, args.cosign)
        elif args.action == "check-gates":
            gates()
            result = {"status": "PASS"}
        else:
            result = apply(
                args.root,
                Path.home()
                / "iTeam-private"
                / "release-operations"
                / "iteam-ai-marketing",
                args.approval_run,
                args.backup_proof,
                args.cosign,
            )
        print(json.dumps(result))
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as error:
        # Do not echo exception text: third-party output and config may contain secrets.
        code = (
            str(error)
            if isinstance(error, ReleaseError)
            else "LOCAL_VERIFICATION_FAILED"
        )
        require(re.fullmatch(r"[A-Za-z0-9_:]+", code), "UNSAFE_ERROR_CODE")
        print(
            json.dumps({"status": "FAIL", "error_code": code, "automatic_retry": False})
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
