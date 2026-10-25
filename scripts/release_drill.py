#!/usr/bin/env python3
"""Real image upgrade, rollback and injected-failure rollback in a disposable internal network.

No root/project/env/port/volume arguments: this command can never target production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path

from release_backup import backup_restore, check_proof
from release_ops import (
    APP,
    REPO,
    SCHEMA,
    ReleaseError,
    Runtime,
    command,
    gh,
    overlay,
    prepare,
    require,
    save,
    switch,
)


class DrillRuntime(Runtime):
    fail_frontend_once = False

    def compose(self, *args: str) -> str:
        if self.fail_frontend_once and args[0] == "up" and args[-1] == "frontend":
            self.fail_frontend_once = False
            raise ReleaseError("INJECTED_FRONTEND_FAILURE")
        return super().compose(*args)

    def ready(self) -> None:
        for _ in range(90):
            try:
                result = json.loads(
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
                    result.get(key) == "ok"
                    for key in ("status", "database", "redis", "schema")
                ):
                    return
            except (ReleaseError, ValueError):
                pass
            time.sleep(1)
        raise ReleaseError("DRILL_READINESS_TIMEOUT")

    def workers_ready(self) -> None:
        print("DRILL: exact worker queue readiness", flush=True)
        try:
            super().workers_ready()
        except ReleaseError:
            print("DRILL: worker queue readiness failed", flush=True)
            raise
        print("DRILL: worker queue readiness PASS", flush=True)

    def frontend_ready(self) -> None:
        print("DRILL: frontend HTTP readiness", flush=True)
        for _ in range(60):
            try:
                self.compose(
                    "exec",
                    "-T",
                    "frontend",
                    "node",
                    "-e",
                    "fetch('http://127.0.0.1:3000/publications').then(r=>{if(r.status!==200)process.exit(1)}).catch(()=>process.exit(1))",
                )
                return
            except ReleaseError:
                time.sleep(1)
        print("DRILL: frontend HTTP readiness failed", flush=True)
        raise ReleaseError("DRILL_FRONTEND_READINESS_TIMEOUT")

    def data_hash(self) -> str:
        result = self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "iteam",
            "-d",
            "iteam_release_test",
            "-At",
            "-c",
            "SELECT id,email,password_hash,role,is_active FROM users ORDER BY id",
        )
        # Only the digest leaves the process; never put account data in the report.
        return hashlib.sha256(result.encode()).hexdigest()


def create_drill(root: Path, project: str, first: dict) -> DrillRuntime:
    sha = first["release_sha"]
    env = {
        "APP_ENV": "test",
        "BUILD_SHA": sha,
        "COMPOSE_FILE": "compose.json:first-images.json",
        "DATABASE_URL": "postgresql+asyncpg://iteam:drill-only@postgres:5432/iteam_release_test",
        "REDIS_URL": "redis://redis:6379/15",
        "JWT_SECRET": secrets.token_urlsafe(40),
        "APP_SECRET": secrets.token_urlsafe(40),
        "ADMIN_EMAIL": "drill@example.com",
        "ADMIN_PASSWORD": secrets.token_urlsafe(40),
        "TELEGRAM_PUBLISHING_ENABLED": "false",
        "VK_PUBLISHING_ENABLED": "false",
        "TELEGRAM_METRICS_ENABLED": "false",
        "VK_METRICS_ENABLED": "false",
        "METRICS_SYNC_ENABLED": "false",
        "OPENAI_API_KEY": "",
        "VK_ACCESS_TOKEN": "",
        "VK_CREDENTIALS_FILE": "",
    }
    (root / ".env").write_text(
        "\n".join(key + "=" + value for key, value in env.items()) + "\n"
    )
    (root / ".env").chmod(0o600)
    services = {
        "postgres": {
            "image": "postgres:17-alpine",
            "environment": {
                "POSTGRES_USER": "iteam",
                "POSTGRES_PASSWORD": "drill-only",
                "POSTGRES_DB": "iteam_release_test",
            },
            "volumes": ["postgres_data:/var/lib/postgresql/data"],
            "healthcheck": {
                "test": [
                    "CMD",
                    "pg_isready",
                    "-U",
                    "iteam",
                    "-d",
                    "iteam_release_test",
                ],
                "interval": "1s",
                "timeout": "3s",
                "retries": 60,
            },
        },
        "redis": {
            "image": "redis:8-alpine",
            "healthcheck": {
                "test": ["CMD", "redis-cli", "ping"],
                "interval": "1s",
                "timeout": "3s",
                "retries": 60,
            },
        },
    }
    for name in APP:
        service = {
            "image": first["images"]["frontend" if name == "frontend" else "backend"],
            "env_file": ".env",
            "stop_grace_period": "210s",
            "depends_on": {
                "postgres": {"condition": "service_healthy"},
                "redis": {"condition": "service_healthy"},
            },
        }
        if name == "frontend":
            service.pop("env_file")
        elif name.endswith("_scheduler"):
            service["environment"] = {
                "WORKER_ROLE": "",
                "SCHEDULER_ROLE": "publication"
                if name.startswith("publication")
                else "ai",
            }
            service["command"] = [
                "celery",
                "-A",
                "app.workers.celery_app:celery_app",
                "beat",
                "--loglevel=WARNING",
                "--schedule=/tmp/beat",
            ]
        elif name.endswith("_worker"):
            role = name.removesuffix("_worker")
            service["environment"] = {"WORKER_ROLE": role, "SCHEDULER_ROLE": ""}
            service["command"] = [
                "celery",
                "-A",
                "app.workers.celery_app:celery_app",
                "worker",
                "--loglevel=WARNING",
                "--queues=" + role,
                "--concurrency=1",
                "--pool=solo",
            ]
        if name == "publication_worker":
            service["volumes"] = ["vk_publishing_credentials:/run/iteam-secrets/vk"]
        service["mem_limit"] = "256m" if name != "frontend" else "192m"
        service["cpus"] = 0.5
        services[name] = service
    # No ports, external network, bind mount or production credential is accepted here.
    save(
        root / "compose.json",
        {
            "services": services,
            "volumes": {"postgres_data": {}, "vk_publishing_credentials": {}},
            "networks": {"default": {"internal": True}},
        },
    )
    save(root / "first-images.json", overlay(first, admin=False))
    return DrillRuntime(root, project=project)


def drill(first_run: int, second_run: int, output: Path, cosign: str) -> dict:
    require(
        int(command(["docker", "info", "--format", "{{.MemTotal}}"])) >= 6 * 1024**3,
        "DRILL_REQUIRES_DEDICATED_RUNNER_WITH_6_GIB",
    )
    with tempfile.TemporaryDirectory(prefix="iteam-release-drill-") as directory:
        root = Path(directory)
        project = "iteam-release-drill-" + secrets.token_hex(5)
        print("DRILL: verify previous release", flush=True)
        first = prepare(first_run, root / "first.json", cosign)
        print("DRILL: verify candidate release", flush=True)
        second = prepare(second_run, root / "second.json", cosign)
        require(
            first["release_sha"] != second["release_sha"], "DRILL_NEEDS_TWO_RELEASES"
        )
        runtime = create_drill(root, project, first)
        state = root / "state"
        state.mkdir(mode=0o700)
        try:
            print("DRILL: pulling verified images", flush=True)
            command(["docker", "pull", first["images"]["backend"]])
            command(["docker", "pull", first["images"]["frontend"]])
            runtime.compose(
                "up", "-d", "--wait", "--wait-timeout", "120", "postgres", "redis"
            )
            runtime.compose(
                "run",
                "--rm",
                "--no-deps",
                "-T",
                "backend",
                "alembic",
                "upgrade",
                "head",
            )
            runtime.compose(
                "run", "--rm", "--no-deps", "-T", "backend", "python", "-m", "app.seed"
            )
            runtime.compose("up", "-d", "--no-build", *APP)
            runtime.ready()
            require(runtime.snapshot()["schema"] == SCHEMA, "DRILL_SCHEMA_MISMATCH")
            for name in APP:
                data = runtime.inspect(name)
                require(
                    not data["HostConfig"].get("PortBindings"), "DRILL_PORT_EXPOSED"
                )
                for network in data["NetworkSettings"]["Networks"].values():
                    detail = json.loads(
                        command(["docker", "network", "inspect", network["NetworkID"]])
                    )[0]
                    require(detail["Internal"], "DRILL_NETWORK_NOT_INTERNAL")
                for mount in data["Mounts"]:
                    require(
                        mount["Type"] == "volume"
                        and mount.get("Name", "").startswith(project + "_"),
                        "DRILL_PRODUCTION_MOUNT",
                    )
            data_hash = runtime.data_hash()
            print("DRILL: encrypted backup and isolated restore", flush=True)
            backup_restore(
                runtime, root / "backups", root / "keys", root / "backup-proof.json"
            )
            check_proof(root / "backup-proof.json", first["release_sha"])
            print("DRILL: upgrade", flush=True)
            upgraded = switch(runtime, second, state, "upgrade", git_checkout=False)
            require(
                runtime.data_hash() == data_hash, "DRILL_DATA_CHANGED_AFTER_UPGRADE"
            )
            print("DRILL: rollback", flush=True)
            rolled_back = switch(runtime, first, state, "rollback", git_checkout=False)
            require(
                runtime.data_hash() == data_hash, "DRILL_DATA_CHANGED_AFTER_ROLLBACK"
            )
            print("DRILL: injected failure and automatic rollback", flush=True)
            runtime.fail_frontend_once = True
            try:
                switch(runtime, second, state, "failure", git_checkout=False)
            except ReleaseError as error:
                require(
                    str(error) == "DEPLOY_FAILED_ROLLBACK_COMPLETED",
                    "DRILL_ROLLBACK_FAILED",
                )
            else:
                raise ReleaseError("DRILL_FAILURE_NOT_INJECTED")
            require(
                runtime.snapshot()["build_sha"] == first["release_sha"]
                and runtime.data_hash() == data_hash,
                "DRILL_RECOVERY_DATA_CHANGED",
            )
            # The actual frontend container also answers HTTP after rollback; no browser is opened.
            runtime.compose(
                "exec",
                "-T",
                "frontend",
                "node",
                "-e",
                "fetch('http://127.0.0.1:3000/publications').then(r=>{if(r.status!==200)process.exit(1)}).catch(()=>process.exit(1))",
            )
            result = {
                "status": "PASS",
                "repository": REPO,
                "from_build_run": first_run,
                "to_build_run": second_run,
                "from_sha": first["release_sha"],
                "to_sha": second["release_sha"],
                "upgrade": upgraded["status"],
                "rollback": rolled_back["status"],
                "injected_failure_recovered": True,
                "encrypted_backup_isolated_restore": True,
                "business_data_hash_unchanged": True,
                "schema_unchanged": True,
                "database_downgrade": False,
                "external_network": False,
                "production_credentials": False,
                "publishing_enabled": False,
                "services": len(APP) + 2,
            }
        finally:
            # This unique project contains only disposable fixture data.
            runtime.compose("down", "--volumes", "--remove-orphans")
        result["disposable_project_removed"] = True
        save(output, result)
        return result


def recent_build_pair() -> tuple[int, int]:
    runs = gh(
        "actions/workflows/release-images.yml/runs?branch=main&status=success&per_page=30"
    )
    selected = []
    seen = set()
    for run in runs["workflow_runs"]:
        if (
            run["status"] == "completed"
            and run["conclusion"] == "success"
            and run["head_sha"] not in seen
        ):
            selected.append(run["id"])
            seen.add(run["head_sha"])
        if len(selected) == 2:
            return selected[1], selected[0]
    raise ReleaseError("TWO_SUCCESSFUL_RELEASES_REQUIRED")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-build-run", type=int)
    parser.add_argument("--to-build-run", type=int)
    parser.add_argument("--latest-two", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cosign", default="cosign")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.latest_two:
            require(
                args.from_build_run is None and args.to_build_run is None,
                "AMBIGUOUS_DRILL_RELEASES",
            )
            first, second = recent_build_pair()
        else:
            require(
                args.from_build_run and args.to_build_run, "TWO_BUILD_RUNS_REQUIRED"
            )
            first, second = args.from_build_run, args.to_build_run
        print(json.dumps(drill(first, second, args.output, args.cosign)))
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as error:
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error_code": str(error)
                    if isinstance(error, ReleaseError)
                    else "LOCAL_VERIFICATION_FAILED",
                    "production_changed": False,
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
