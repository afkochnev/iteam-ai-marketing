import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from celery.beat import Scheduler
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.content import ContentDerivation, ContentType
from app.models.publication import Publication, PublicationStatus
from app.models.task import Task, TaskStatus, TaskType
from app.schemas.publication import PublicationCreate
from app.schemas.task import TaskCreate
from app.services.publication_service import PublicationService
from app.services.task_service import TaskService
from app.tests.test_publications import _approved_post
from app.tests.test_worker_config import (
    PUBLICATION_FIELDS,
    REPO_ROOT,
)
from app.tests.test_worker_config import (
    compose_config as worker_compose_config,
)
from app.workers import dispatcher_worker, metrics_worker, recovery_worker
from app.workers.celery_app import celery_app
from app.workers.scheduler_config import build_beat_schedule

compose_config = worker_compose_config

ROLE_TASKS = {
    "ai": {"dispatch_ready_tasks", "recover_stuck_ai_tasks"},
    "publication": {
        "dispatch_due_publications",
        "recover_stuck_publications",
        "sync_recent_publication_metrics",
        "advance_marketing_experiments",
    },
}


@pytest.mark.parametrize("role", ["ai", "publication"])
def test_scheduler_role_has_only_owned_jobs(role):
    schedule = build_beat_schedule(role, settings)
    assert {entry["task"] for entry in schedule.values()} == ROLE_TASKS[role]
    assert (
        not {entry["task"] for entry in schedule.values()}
        & ROLE_TASKS["publication" if role == "ai" else "ai"]
    )


def test_no_scheduler_role_has_no_jobs():
    assert build_beat_schedule(None, settings) == {}
    assert celery_app.conf.beat_schedule == {}


def test_invalid_scheduler_role_fails_closed():
    with pytest.raises(RuntimeError, match="SCHEDULER_ROLE is invalid"):
        build_beat_schedule("mixed", settings)


@pytest.mark.parametrize("role", ["ai", "publication"])
def test_scheduler_process_selects_schedule_before_start(role):
    environment = dict(os.environ)
    environment.update(
        {
            "WORKER_ROLE": "",
            "SCHEDULER_ROLE": role,
            "TELEGRAM_PUBLISHING_ENABLED": "false",
            "VK_PUBLISHING_ENABLED": "false",
            "TELEGRAM_BOT_TOKEN": "",
            "TELEGRAM_TARGET_CHAT_ID": "",
            "VK_ACCESS_TOKEN": "",
            "VK_OWNER_ID": "",
            "OPENAI_API_KEY": "",
        }
    )
    started = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; from app.workers.celery_app import celery_app; "
                "print(json.dumps(sorted(e['task'] "
                "for e in celery_app.conf.beat_schedule.values())))"
            ),
        ],
        cwd=REPO_ROOT / "backend",
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert started.returncode == 0, started.stderr
    assert set(json.loads(started.stdout)) == ROLE_TASKS[role]


@pytest.mark.parametrize("field", list(PUBLICATION_FIELDS))
def test_ai_scheduler_fail_fast_with_publication_capability(field):
    config = Settings(
        _env_file=None,
        scheduler_role="ai",
        worker_role=None,
        telegram_publishing_enabled=False,
        vk_publishing_enabled=False,
        telegram_bot_token="",
        telegram_target_chat_id="",
        vk_access_token="",
        vk_owner_id=None,
    )
    value = PUBLICATION_FIELDS[field]
    setattr(
        config,
        field.lower(),
        True if field.endswith("_ENABLED") else (int(value) if field == "VK_OWNER_ID" else value),
    )
    with pytest.raises(
        RuntimeError, match="AI scheduler publication capability is forbidden"
    ) as exc:
        config.validate_scheduler_capabilities()
    if not field.endswith("_ENABLED"):
        assert value not in str(exc.value)


def test_worker_and_scheduler_roles_cannot_be_combined():
    with pytest.raises(RuntimeError, match="must not be combined"):
        Settings(
            _env_file=None, worker_role="ai", scheduler_role="ai"
        ).validate_scheduler_capabilities()


@pytest.mark.parametrize("service_name", ["ai_scheduler", "publication_scheduler"])
def test_compose_schedulers_are_separate_and_capability_scoped(compose_config, service_name):
    service = compose_config["services"][service_name]
    command = service["command"]
    assert "beat" in command
    assert "worker" not in command
    assert "--beat" not in command
    environment = service["environment"]
    assert environment["WORKER_ROLE"] == ""
    if service_name == "ai_scheduler":
        assert service_name in compose_config["default_services"]
        assert not service.get("profiles")
        assert service["restart"] == "unless-stopped"
        assert environment["SCHEDULER_ROLE"] == "ai"
        for field in PUBLICATION_FIELDS:
            assert environment[field] == ("false" if field.endswith("_ENABLED") else "")
    else:
        assert service_name not in compose_config["default_services"]
        assert service["profiles"] == ["publishing"]
        assert environment["SCHEDULER_ROLE"] == "publication"
        assert environment["OPENAI_API_KEY"] == ""
    config = Settings(_env_file=None, **{key.lower(): value for key, value in environment.items()})
    config.validate_scheduler_capabilities()


def fake_recovery_session(monkeypatch):
    session = AsyncMock()
    context = AsyncMock()
    context.__aenter__.return_value = session
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(recovery_worker, "create_async_engine", Mock(return_value=engine))
    monkeypatch.setattr(recovery_worker, "async_sessionmaker", Mock(return_value=lambda: context))
    return session, engine


async def test_ai_recovery_only_recovers_tasks_and_indexing(monkeypatch):
    session, engine = fake_recovery_session(monkeypatch)
    recover_ai = AsyncMock()
    recover_indexing = AsyncMock()
    recover_publications = AsyncMock()
    monkeypatch.setattr(recovery_worker.TaskRecoveryService, "recover_stuck", recover_ai)
    monkeypatch.setattr(recovery_worker, "recover_stale_indexing", recover_indexing)
    monkeypatch.setattr(
        recovery_worker.PublicationService, "recover_stuck_publishing", recover_publications
    )
    await recovery_worker._recover_ai()
    recover_ai.assert_awaited_once()
    recover_indexing.assert_awaited_once_with(session)
    recover_publications.assert_not_awaited()
    engine.dispose.assert_awaited_once()


async def test_publication_recovery_only_recovers_publications(monkeypatch):
    _, engine = fake_recovery_session(monkeypatch)
    recover_ai = AsyncMock()
    recover_indexing = AsyncMock()
    recover_publications = AsyncMock()
    monkeypatch.setattr(recovery_worker.TaskRecoveryService, "recover_stuck", recover_ai)
    monkeypatch.setattr(recovery_worker, "recover_stale_indexing", recover_indexing)
    monkeypatch.setattr(
        recovery_worker.PublicationService, "recover_stuck_publishing", recover_publications
    )
    before = datetime.now(UTC) - timedelta(seconds=settings.publication_publishing_stale_seconds)
    await recovery_worker._recover_publications()
    after = datetime.now(UTC) - timedelta(seconds=settings.publication_publishing_stale_seconds)
    recover_publications.assert_awaited_once()
    assert before <= recover_publications.await_args.kwargs["cutoff"] <= after
    recover_ai.assert_not_awaited()
    recover_indexing.assert_not_awaited()
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("path", ["_recover_ai", "_recover_publications"])
async def test_recovery_disposes_engine_on_error(monkeypatch, path):
    _, engine = fake_recovery_session(monkeypatch)
    if path == "_recover_ai":
        monkeypatch.setattr(
            recovery_worker.TaskRecoveryService,
            "recover_stuck",
            AsyncMock(side_effect=RuntimeError("test failure")),
        )
    else:
        monkeypatch.setattr(
            recovery_worker.PublicationService,
            "recover_stuck_publishing",
            AsyncMock(side_effect=RuntimeError("test failure")),
        )
    monkeypatch.setattr(recovery_worker, "report_exception", Mock())
    with pytest.raises(RuntimeError, match="test failure"):
        await getattr(recovery_worker, path)()
    engine.dispose.assert_awaited_once()


@pytest.fixture
def memory_queues():
    assert celery_app.conf.broker_url == "memory://"
    celery_app.loader.import_default_modules()
    with celery_app.connection_for_read() as connection:
        queues = {
            name: connection.SimpleQueue(name)
            for name in (
                "ai",
                "ai_control",
                "publication",
                "publication_control",
                "metrics",
                "unrouted",
            )
        }
        for queue in queues.values():
            queue.clear()
        yield queues
        for queue in queues.values():
            queue.clear()
            queue.close()


def scheduler_tick(role):
    scheduler = Scheduler(app=celery_app, lazy=True)
    scheduler.schedule = {
        name: scheduler.Entry(
            name=name,
            app=celery_app,
            last_run_at=celery_app.now() - timedelta(seconds=entry["schedule"] * 2),
            **entry,
        )
        for name, entry in build_beat_schedule(role, settings).items()
    }
    # Each tick publishes one due entry; make all role-specific entries due deterministically.
    for _ in scheduler.schedule:
        scheduler.tick()


async def execute_control_messages(queue, expected_tasks):
    executed = []
    while queue.qsize():
        message = queue.get(block=False)
        task_name = message.headers["task"]
        assert task_name in expected_tasks
        args, kwargs, _ = message.payload
        await asyncio.to_thread(celery_app.tasks[task_name].run, *args, **kwargs)
        message.ack()
        executed.append(task_name)
    assert set(executed) == expected_tasks
    assert len(executed) == len(expected_tasks)


async def revision_fixture(session):
    user, campaign, post, version = await _approved_post(session)
    source = await session.get(Task, post.source_task_id)
    source.status = TaskStatus.COMPLETED
    article_id = await session.scalar(
        select(ContentDerivation.source_content_version_id).where(
            ContentDerivation.derived_content_version_id == version.id
        )
    )
    revision = await TaskService(session).create_task(
        TaskCreate(
            campaign_id=campaign.id,
            title="Autonomous content revision",
            task_type=TaskType.CONTENT_REVISION,
            assigned_agent_id=post.author_agent_id,
            input_data={
                "content_item_id": str(post.id),
                "base_content_version_id": str(version.id),
                "revision_target_type": ContentType.SOCIAL_POST.value,
                "revision_comment": "Clarify the CTA",
                "immutable_source_context": {"article_version_ids": [str(article_id)]},
            },
        )
    )
    assert revision.status == TaskStatus.READY
    await session.commit()
    return user, post, version, revision


@pytest.mark.integration
async def test_autonomous_content_revision_dispatch_once(db_session, memory_queues):
    _, _, _, revision = await revision_fixture(db_session)
    task_id = revision.id
    for tick in range(2):
        scheduler_tick("ai")
        await execute_control_messages(memory_queues["ai_control"], ROLE_TASKS["ai"])
        db_session.expire_all()
        runs = list(
            (await db_session.scalars(select(AgentRun).where(AgentRun.task_id == task_id))).all()
        )
        assert len(runs) == 1
        assert runs[0].status == AgentRunStatus.QUEUED
        if tick == 0:
            message = memory_queues["ai"].get(block=False)
            assert message.headers["task"] == "execute_agent_run"
            assert message.payload[0] == [str(runs[0].id)]
            assert runs[0].queue_job_id == message.headers["id"]
            message.ack()
        else:
            assert memory_queues["ai"].qsize() == 0


@pytest.mark.integration
async def test_ai_scheduler_leaves_due_publication_untouched(
    db_session, memory_queues, monkeypatch
):
    user, post, version, revision = await revision_fixture(db_session)
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    service = PublicationService(db_session)
    publication = await service.create(
        PublicationCreate(
            content_item_id=post.id,
            content_version_id=version.id,
            channel=post.channel,
        ),
        user,
    )
    await service.approve(publication.id, user)
    await service.schedule(publication.id, datetime.now(UTC) + timedelta(minutes=5), user)
    row = await db_session.get(Publication, publication.id)
    row.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    publication_id, task_id = row.id, revision.id
    scheduler_tick("ai")
    await execute_control_messages(memory_queues["ai_control"], ROLE_TASKS["ai"])
    db_session.expire_all()
    row = await db_session.get(Publication, publication_id)
    assert row.status == PublicationStatus.SCHEDULED
    assert row.execution_token is None
    assert memory_queues["publication"].qsize() == 0
    assert memory_queues["publication_control"].qsize() == 0
    assert memory_queues["metrics"].qsize() == 0
    run = await db_session.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
    assert run.status == AgentRunStatus.QUEUED


async def test_publication_scheduler_never_calls_ai_dispatch(memory_queues, monkeypatch):
    ai_dispatch = Mock()
    ai_recovery = Mock()
    monkeypatch.setattr(dispatcher_worker.dispatch_ready_tasks, "run", ai_dispatch)
    monkeypatch.setattr(recovery_worker.recover_stuck_ai_tasks, "run", ai_recovery)
    jobs = [
        dispatcher_worker.dispatch_due_publications,
        recovery_worker.recover_stuck_publications,
        metrics_worker.sync_recent_publication_metrics,
    ]
    calls = []
    for job in jobs:
        call = Mock()
        monkeypatch.setattr(job, "run", call)
        calls.append(call)
    scheduler_tick("publication")
    for queue_name in ("publication_control", "metrics"):
        await execute_control_messages(
            memory_queues[queue_name],
            {
                entry["task"]
                for entry in build_beat_schedule("publication", settings).values()
                if entry["options"]["queue"] == queue_name
            },
        )
    for call in calls:
        call.assert_called_once()
    ai_dispatch.assert_not_called()
    ai_recovery.assert_not_called()
    assert memory_queues["ai_control"].qsize() == 0
    assert memory_queues["ai"].qsize() == 0
