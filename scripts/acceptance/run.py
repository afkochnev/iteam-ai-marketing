"""Isolated default Compose acceptance and optional full backend regression."""

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVICES = {
    "postgres",
    "redis",
    "backend",
    "frontend",
    "ai_worker",
    "ai_control_worker",
    "ai_scheduler",
}


def run_acceptance(full_backend: bool):
    project = "iteam-acceptance-" + uuid4().hex[:10]
    environment = dict(os.environ)
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
        "COMPOSE_PROFILES",
        "COMPOSE_FILE",
    ):
        environment.pop(key, None)
    environment["NEXT_TELEMETRY_DISABLED"] = "1"
    with tempfile.TemporaryDirectory(prefix=project + "-") as directory:
        work = Path(directory)
        env_file = work / ".env"
        env_file.write_text(
            "\n".join(
                [
                    "APP_ENV=development",
                    "POSTGRES_USER=acceptance",
                    "POSTGRES_PASSWORD=synthetic-only",
                    "POSTGRES_DB=iteam_acceptance",
                    "DATABASE_URL=postgresql+asyncpg://acceptance:synthetic-only@postgres:5432/iteam_acceptance",
                    "REDIS_URL=redis://redis:6379/2",
                    "JWT_SECRET=acceptance-only",
                    "APP_SECRET=acceptance-only",
                    "ADMIN_PASSWORD=acceptance-only",
                    "OPENAI_API_KEY=",
                    "OPENAI_DEFAULT_MODEL=acceptance-fake",
                    "OPENAI_AGENTS_DISABLE_TRACING=true",
                    "TELEGRAM_PUBLISHING_ENABLED=false",
                    "VK_PUBLISHING_ENABLED=false",
                    "TELEGRAM_BOT_TOKEN=",
                    "TELEGRAM_TARGET_CHAT_ID=",
                    "VK_ACCESS_TOKEN=",
                    "VK_OWNER_ID=",
                    "TASK_DISPATCH_INTERVAL_SECONDS=1",
                    "ACCEPTANCE_DENY_PROVIDERS=1",
                    "PYTHONPATH=/acceptance:/app",
                ]
            )
            + "\n"
        )
        ca = work / "proxy-ca.pem"
        ca.write_bytes(Path("/etc/ssl/certs/ca-certificates.crt").read_bytes())
        proxy_ca = os.environ.get("CODEX_PROXY_CERT")
        if proxy_ca:
            ca.write_bytes(ca.read_bytes() + Path(proxy_ca).read_bytes())
        # Use the production Dockerfiles with build-only proxy trust injected.
        for target in ("backend", "frontend"):
            source = (ROOT / target / "Dockerfile").read_text()
            if target == "backend":
                source = source.replace(
                    "RUN pip install",
                    "RUN --mount=type=secret,id=proxy_ca "
                    "PIP_CERT=/run/secrets/proxy_ca pip install",
                )
            else:
                source = source.replace(
                    "RUN npm ",
                    "RUN --mount=type=secret,id=proxy_ca "
                    "NODE_EXTRA_CA_CERTS=/run/secrets/proxy_ca npm ",
                )
            (work / f"{target}.Dockerfile").write_text(source)
        override = ["services:"]
        for service in DEFAULT_SERVICES | {
            "publication_worker",
            "publication_control_worker",
            "publication_scheduler",
            "metrics_worker",
        }:
            override += [f"  {service}:"]
            if service not in {"postgres", "redis", "frontend"}:
                override += [
                    f"    env_file: !override [{env_file}]",
                    f"    volumes: [{ROOT / 'scripts/acceptance'}:/acceptance:ro]",
                ]
            if service in {"backend", "ai_worker", "ai_control_worker", "ai_scheduler"}:
                override += [
                    f"    image: {project}-backend",
                    "    build:",
                    f"      dockerfile: {work / 'backend.Dockerfile'}",
                    "      secrets: [proxy_ca]",
                ]
            if service == "ai_worker":
                override += ["    command: python /acceptance/fake_worker.py"]
            if service == "ai_control_worker":
                override += [
                    "    command: celery -A app.workers.celery_app:celery_app worker "
                    "--queues=ai_control --concurrency=1 --loglevel=INFO"
                ]
            if service == "frontend":
                override += [
                    f"    image: {project}-frontend",
                    "    build:",
                    f"      dockerfile: {work / 'frontend.Dockerfile'}",
                    "      secrets: [proxy_ca]",
                ]
            port = {
                "postgres": 5432,
                "redis": 6379,
                "backend": 8000,
                "frontend": 3000,
            }.get(service)
            if port:
                override += [f'    ports: !override ["127.0.0.1::{port}"]']
            if service in {"postgres", "redis"}:
                # Only test infrastructure joins the host-accessible regression network.
                override += ["    networks: [default, regression]"]
            if service == "postgres":
                override += [
                    "    environment:",
                    "      POSTGRES_USER: acceptance",
                    "      POSTGRES_PASSWORD: synthetic-only",
                    "      POSTGRES_DB: iteam_acceptance",
                ]
        override += [
            "networks:",
            "  default:",
            "    internal: true",
            "  regression: {}",
            "secrets:",
            "  proxy_ca:",
            f"    file: {ca}",
        ]
        override_file = work / "compose.acceptance.yml"
        override_file.write_text("\n".join(override) + "\n")
        command = [
            "docker",
            "--host=unix:///var/run/docker.sock",
            "compose",
            "--project-name",
            project,
            "--project-directory",
            str(ROOT),
            "--env-file",
            str(env_file),
            "--file",
            str(ROOT / "docker-compose.yml"),
            "--file",
            str(override_file),
        ]

        def compose(*args, capture=False):
            completed = subprocess.run(
                [*command, *args], env=environment, text=True, capture_output=capture
            )
            if completed.returncode and capture:
                print(completed.stderr, flush=True)
            completed.check_returncode()
            return completed

        def execute(service, *args, capture=False):
            return compose("exec", "-T", service, *args, capture=capture)

        def port(service, internal):
            return int(
                compose("port", service, str(internal), capture=True)
                .stdout.strip()
                .rsplit(":", 1)[1]
            )

        try:
            compose("config", "--quiet")
            compose("--profile", "publishing", "config", "--quiet")
            compose("build", "backend", "frontend")
            compose("up", "-d")
            execute("backend", "alembic", "upgrade", "head")
            for _ in range(40):
                try:
                    execute(
                        "backend",
                        "python",
                        "-c",
                        "import urllib.request; "
                        "assert urllib.request.urlopen("
                        "'http://localhost:8000/health/ready').status == 200",
                        capture=True,
                    )
                    break
                except subprocess.CalledProcessError:
                    time.sleep(0.5)
            else:
                raise AssertionError("Backend readiness did not pass")
            records = compose("ps", "--format", "json", capture=True).stdout
            running = {
                json.loads(line)["Service"]
                for line in records.splitlines()
                if line.strip()
            }
            assert running == DEFAULT_SERVICES, running
            execute(
                "backend",
                "python",
                "-c",
                "import urllib.request; "
                "assert urllib.request.urlopen('http://localhost:8000/health').status == 200",
                capture=True,
            )
            execute(
                "frontend",
                "node",
                "-e",
                "fetch('http://localhost:3000/login').then(r=>{if(r.status!==200)process.exit(1)})",
            )
            seeded = execute(
                "backend", "python", "/acceptance/scenario.py", "seed", capture=True
            )
            ids = json.loads(seeded.stdout.strip().splitlines()[-1])
            checked = execute(
                "backend",
                "python",
                "/acceptance/scenario.py",
                "verify",
                json.dumps(ids),
                capture=True,
            )
            report = json.loads(checked.stdout.strip().splitlines()[-1])
            report["default_services"] = sorted(running)
            report["health"] = report["readiness"] = "PASS"
            execute("redis", "redis-cli", "-n", "1", "DBSIZE", capture=True)
            Path("/tmp/iteam-d4-acceptance-report.json").write_text(
                json.dumps(report, indent=2)
            )
            if full_backend:
                execute(
                    "postgres", "createdb", "-U", "acceptance", "iteam_regression_test"
                )
                test_url = (
                    "postgresql+asyncpg://acceptance:synthetic-only@127.0.0.1:"
                    f"{port('postgres', 5432)}/iteam_regression_test"
                )
                redis_url = f"redis://127.0.0.1:{port('redis', 6379)}/15"
                test_environment = dict(environment)
                test_environment.update(
                    {
                        "APP_ENV": "test",
                        "DATABASE_URL": test_url,
                        "TEST_DATABASE_URL": test_url,
                        "REDIS_URL": redis_url,
                        "TEST_REDIS_URL": redis_url,
                        "WORKER_ROLE": "",
                        "SCHEDULER_ROLE": "",
                        "OPENAI_API_KEY": "",
                        "TELEGRAM_PUBLISHING_ENABLED": "false",
                        "VK_PUBLISHING_ENABLED": "false",
                        "TELEGRAM_BOT_TOKEN": "",
                        "TELEGRAM_TARGET_CHAT_ID": "",
                        "VK_ACCESS_TOKEN": "",
                        "VK_OWNER_ID": "",
                        "ACCEPTANCE_DENY_PROVIDERS": "1",
                        "PYTHONPATH": str(ROOT / "scripts/acceptance")
                        + ":"
                        + str(ROOT / "backend"),
                    }
                )
                backend = ROOT / "backend"
                for args in (
                    [".venv/bin/alembic", "upgrade", "head"],
                    [".venv/bin/alembic", "current"],
                    [".venv/bin/alembic", "check"],
                    [".venv/bin/pytest", "-q"],
                ):
                    subprocess.run(args, env=test_environment, cwd=backend, check=True)
            for db in (1, 15):
                size = execute(
                    "redis", "redis-cli", "-n", str(db), "DBSIZE", capture=True
                ).stdout.strip()
                # pytest uses memory Celery and health PING only; DB1 is untouched.
                assert size == "0", f"Unexpected changes in isolated Redis DB{db}"
            report["redis_db1"] = "untouched"
            report["pytest_redis_db"] = 15
            Path("/tmp/iteam-d4-acceptance-report.json").write_text(
                json.dumps(report, indent=2)
            )
            print(json.dumps(report, indent=2), flush=True)
        finally:
            compose("down", "--volumes", "--remove-orphans")
            subprocess.run(
                [
                    "docker",
                    "--host=unix:///var/run/docker.sock",
                    "image",
                    "rm",
                    f"{project}-backend",
                    f"{project}-frontend",
                ],
                env=environment,
                check=False,
            )
            print("Isolated acceptance project removed:", project, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-backend", action="store_true")
    run_acceptance(parser.parse_args().full_backend)
