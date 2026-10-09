import json
import os
import subprocess
import sys
from pathlib import Path
from shlex import split

import pytest

from app.core.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKER_QUEUES = {
    "ai_worker": "ai",
    "ai_control_worker": "ai_control",
    "publication_worker": "publication",
    "publication_control_worker": "publication_control",
    "metrics_worker": "metrics",
}
PUBLICATION_FIELDS = {
    "TELEGRAM_PUBLISHING_ENABLED": "true",
    "VK_PUBLISHING_ENABLED": "true",
    "TELEGRAM_BOT_TOKEN": "synthetic-telegram-token",
    "TELEGRAM_TARGET_CHAT_ID": "synthetic-chat-id",
    "VK_ACCESS_TOKEN": "synthetic-vk-token",
    "VK_OWNER_ID": "-123456789",
}


@pytest.fixture(scope="module")
def compose_config(tmp_path_factory):
    # Resolve the real Compose model using a temporary, synthetic env file.
    # This command is offline and never creates/starts containers.
    project_dir = tmp_path_factory.mktemp("worker-compose")
    env_file = project_dir / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=synthetic-openai-key\nOPENAI_DEFAULT_MODEL=synthetic-model\n"
        + "\n".join(
            f"{key}={value}" for key, value in {**PUBLICATION_FIELDS, **METRICS_FIELDS}.items()
        )
        + "\n"
    )
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("COMPOSE_")
    }
    command = [
        "docker",
        "compose",
        "--file",
        str(REPO_ROOT / "docker-compose.yml"),
        "--project-directory",
        str(project_dir),
        "--env-file",
        str(env_file),
        "config",
    ]
    checked = subprocess.run(
        [*command, "--quiet"], env=environment, capture_output=True, text=True, timeout=30
    )
    assert checked.returncode == 0, checked.stderr
    resolved = subprocess.run(
        [*command, "--format", "json"], env=environment, capture_output=True, text=True, timeout=30
    )
    assert resolved.returncode == 0, resolved.stderr
    default_services = set(json.loads(resolved.stdout)["services"])
    publishing = subprocess.run(
        [*command[:-1], "--profile", "publishing", "config", "--format", "json"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert publishing.returncode == 0, publishing.stderr
    config = json.loads(publishing.stdout)
    config["default_services"] = default_services
    return config


def worker_command(service):
    command = service.get("command") or []
    return split(command) if isinstance(command, str) else command


@pytest.mark.parametrize(("service_name", "queue"), WORKER_QUEUES.items())
def test_worker_consumes_only_owned_queue(compose_config, service_name, queue):
    command = worker_command(compose_config["services"][service_name])
    assert "worker" in command
    queue_options = [arg for arg in command if arg.startswith("--queues=")]
    assert queue_options == [f"--queues={queue}"]


def test_no_combined_worker_or_unrouted_consumer(compose_config):
    workers = {}
    for name, service in compose_config["services"].items():
        command = worker_command(service) if "command" in service else []
        if "celery" not in command:
            continue
        assert "--beat" not in command
        assert "-B" not in command
        if "worker" in command:
            assert "beat" not in command
            workers[name] = service
            queues = next(arg for arg in command if arg.startswith("--queues="))
            assert "unrouted" not in queues.partition("=")[2].split(",")
            assert "ai_live_test" not in queues.partition("=")[2].split(",")
    assert set(workers) == set(WORKER_QUEUES)
    assert "worker" not in compose_config["services"]


@pytest.mark.parametrize("service_name", ["ai_worker", "ai_control_worker"])
def test_ai_workers_always_available_with_no_publishing_capability(compose_config, service_name):
    service = compose_config["services"][service_name]
    assert not service.get("profiles")
    assert service["restart"] == "unless-stopped"
    environment = service["environment"]
    assert environment["WORKER_ROLE"] == WORKER_QUEUES[service_name]
    for field in PUBLICATION_FIELDS:
        expected = "false" if field.endswith("_ENABLED") else ""
        assert environment[field] == expected
    config = Settings(_env_file=None, **{key.lower(): value for key, value in environment.items()})
    assert config.vk_owner_id is None
    config.validate_worker_capabilities()


@pytest.mark.parametrize(
    "service_name", ["publication_worker", "publication_control_worker", "metrics_worker"]
)
def test_publication_workers_opt_in_and_without_openai_credential(compose_config, service_name):
    service = compose_config["services"][service_name]
    assert service["profiles"] == ["publishing"]
    environment = service["environment"]
    assert environment["WORKER_ROLE"] == WORKER_QUEUES[service_name]
    assert environment["OPENAI_API_KEY"] == ""
    # Publishing workers retain their own synthetic provider capabilities.
    for field, value in PUBLICATION_FIELDS.items():
        assert environment[field] == (
            ("false" if field.endswith("_ENABLED") else "")
            if service_name == "metrics_worker"
            else value
        )
    config = Settings(_env_file=None, **{key.lower(): value for key, value in environment.items()})
    config.validate_worker_capabilities()


@pytest.mark.parametrize("role", ["ai", "ai_control"])
@pytest.mark.parametrize("field", list(PUBLICATION_FIELDS))
def test_ai_role_rejects_each_publication_capability_without_leaking_values(role, field):
    config = Settings(
        _env_file=None,
        worker_role=role,
        **{key.lower(): "" for key in PUBLICATION_FIELDS if not key.endswith("_ENABLED")},
    )
    field_name = field.lower()
    value = PUBLICATION_FIELDS[field]
    setattr(
        config,
        field_name,
        True if field.endswith("_ENABLED") else (int(value) if field == "VK_OWNER_ID" else value),
    )
    with pytest.raises(RuntimeError) as error:
        config.validate_worker_capabilities()
    assert field in str(error.value)
    if not field.endswith("_ENABLED"):
        assert value not in str(error.value)


@pytest.mark.parametrize("role", ["ai", "ai_control"])
@pytest.mark.parametrize("provider", ["telegram", "vk"])
def test_ai_role_rejects_working_publication_capability_even_when_disabled(role, provider):
    capability = (
        {"telegram_bot_token": "synthetic-token", "telegram_target_chat_id": "synthetic-chat"}
        if provider == "telegram"
        else {"vk_access_token": "synthetic-token", "vk_owner_id": -123}
    )
    config = Settings(
        _env_file=None,
        worker_role=role,
        telegram_publishing_enabled=False,
        vk_publishing_enabled=False,
        **capability,
    )
    with pytest.raises(RuntimeError, match="AI worker publication capability is forbidden"):
        config.validate_worker_capabilities()


def test_unknown_worker_role_fails_closed():
    with pytest.raises(RuntimeError, match="WORKER_ROLE is invalid"):
        Settings(_env_file=None, worker_role="unknown").validate_worker_capabilities()


def import_worker_app(role, capabilities=None):
    environment = dict(os.environ)
    environment.update(
        {
            "WORKER_ROLE": role,
            "SCHEDULER_ROLE": "",
            "APP_ENV": "production",
            "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/worker_config",
            "REDIS_URL": "redis://localhost:6379/15",
            "OPENAI_API_KEY": "",
            "OPENAI_DEFAULT_MODEL": "",
            "TELEGRAM_PUBLISHING_ENABLED": "false",
            "VK_PUBLISHING_ENABLED": "false",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_TARGET_CHAT_ID": "",
            "VK_ACCESS_TOKEN": "",
            "VK_OWNER_ID": "",
        }
    )
    environment.update(capabilities or {})
    # Import and task registration only: no worker, broker or provider is started.
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from app.workers.celery_app import celery_app; "
                "celery_app.loader.import_default_modules()"
            ),
        ],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("role", ["ai", "ai_control"])
def test_ai_startup_fails_before_worker_import_with_publishing_capability(role):
    started = import_worker_app(
        role,
        {
            "TELEGRAM_BOT_TOKEN": "synthetic-startup-secret",
            "TELEGRAM_TARGET_CHAT_ID": "synthetic-id",
        },
    )
    assert started.returncode != 0
    assert "AI worker publication capability is forbidden" in started.stderr
    assert "synthetic-startup-secret" not in started.stderr
    assert "synthetic-id" not in started.stderr


@pytest.mark.parametrize("role", ["publication", "publication_control", "metrics"])
def test_publication_worker_startup_without_openai_execution_capability(role):
    started = import_worker_app(role, {} if role == "metrics" else PUBLICATION_FIELDS)
    assert started.returncode == 0, started.stderr


def test_default_compose_only_enables_ai_workers(compose_config):
    assert set(WORKER_QUEUES) & compose_config["default_services"] == {
        "ai_worker",
        "ai_control_worker",
    }


@pytest.mark.parametrize("role", ["ai", "ai_control"])
def test_ai_worker_startup_with_cleared_publication_capability(role):
    started = import_worker_app(role)
    assert started.returncode == 0, started.stderr


METRICS_FIELDS = {
    "TELEGRAM_METRICS_ENABLED": "true",
    "TELEGRAM_METRICS_API_ID": "123",
    "TELEGRAM_METRICS_API_HASH": "private-metrics-secret",
    "TELEGRAM_METRICS_SESSION": "private-metrics-secret",
    "TELEGRAM_METRICS_PEER": "@synthetic",
    "TELEGRAM_METRICS_CHAT_ID": "-100123",
    "VK_METRICS_ENABLED": "true",
    "VK_METRICS_ACCESS_TOKEN": "private-metrics-secret",
    "VK_METRICS_OWNER_ID": "-123",
}


@pytest.mark.parametrize(
    "service_name",
    [
        "backend",
        "ai_worker",
        "ai_control_worker",
        "ai_scheduler",
        "publication_worker",
        "publication_control_worker",
        "publication_scheduler",
    ],
)
def test_compose_clears_all_metrics_credentials(compose_config, service_name):
    environment = compose_config["services"][service_name]["environment"]
    for field in METRICS_FIELDS:
        assert environment[field] == ("false" if field.endswith("_ENABLED") else "")


def test_metrics_worker_receives_only_read_credentials(compose_config):
    environment = compose_config["services"]["metrics_worker"]["environment"]
    assert environment["OPENAI_API_KEY"] == ""
    for field, value in METRICS_FIELDS.items():
        assert environment[field] == value
    for field in PUBLICATION_FIELDS:
        assert environment[field] == ("false" if field.endswith("_ENABLED") else "")
    Settings(
        _env_file=None, **{k.lower(): v for k, v in environment.items()}
    ).validate_worker_capabilities()
