from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.api.system import system_status
from app.core.config import Settings, settings
from app.core.errors import AppError
from app.main import app
from app.models.content import ContentChannel
from app.models.publication import Publication, PublicationStatus
from app.models.task import Task, TaskStatus
from app.schemas.publication import PublicationCreate
from app.services.publication_operations import operational_fields
from app.services.publication_service import PublicationService
from app.tests.test_publications import _approved_post
from app.tests.test_worker_config import compose_config as worker_compose_config
from app.workers import dispatcher_worker

compose_config = worker_compose_config


async def scheduled_fixture(session, channel):
    user, campaign, post, version = await _approved_post(session)
    post.channel = channel
    await session.commit()
    service = PublicationService(session)
    publication = await service.create(
        PublicationCreate(content_item_id=post.id, content_version_id=version.id, channel=channel),
        user,
    )
    await service.approve(publication.id, user)
    await service.schedule(publication.id, datetime.now(UTC) + timedelta(minutes=5), user)
    row = await session.get(Publication, publication.id)
    assert row is not None
    return user, campaign, post, version, row


@pytest.mark.parametrize("channel", [ContentChannel.TELEGRAM, ContentChannel.VK])
@pytest.mark.parametrize(
    "seconds,enabled,dispatched", [(60, True, True), (3601, True, False), (60, False, False)]
)
async def test_dispatch_catchup_and_disabled_provider(
    db_session: AsyncSession, monkeypatch, channel, seconds, enabled, dispatched
):
    monkeypatch.setattr(
        settings, "telegram_publishing_enabled", enabled and channel == ContentChannel.TELEGRAM
    )
    monkeypatch.setattr(settings, "vk_publishing_enabled", enabled and channel == ContentChannel.VK)
    monkeypatch.setattr(settings, "publication_auto_dispatch_max_lateness_seconds", 3600)
    _, campaign, _, version, row = await scheduled_fixture(db_session, channel)
    row.scheduled_at = datetime.now(UTC) - timedelta(seconds=seconds)
    await db_session.commit()
    calls = []
    monkeypatch.setattr(
        dispatcher_worker.publish_telegram_publication,
        "apply_async",
        lambda **kw: calls.append(("TELEGRAM", kw)),
    )
    monkeypatch.setattr(
        dispatcher_worker.publish_vk_publication,
        "apply_async",
        lambda **kw: calls.append(("VK", kw)),
    )
    original = PublicationService.claim_for_publish
    claimed = []

    async def probe(self, publication_id, user=None):
        claimed.append(publication_id)
        return await original(self, publication_id, user)

    monkeypatch.setattr(PublicationService, "claim_for_publish", probe)
    await dispatcher_worker._dispatch_publications()
    await dispatcher_worker._dispatch_publications()
    assert len(calls) == len(claimed) == int(dispatched)
    if dispatched:
        assert calls[0][0] == channel.value
        assert calls[0][1]["queue"] == "publication"
        assert calls[0][1]["args"][0] == str(row.id)
    await db_session.refresh(row)
    assert row.status == (
        PublicationStatus.PUBLISHING if dispatched else PublicationStatus.SCHEDULED
    )
    assert row.content_version_id == version.id
    assert row.failure_code is None
    if seconds > 3600:
        response = await PublicationService(db_session).get(row.id)
        assert response.is_overdue and response.lateness_seconds >= seconds
        calendar = await PublicationService(db_session).calendar(
            campaign.id,
            datetime.now(UTC) - timedelta(days=1),
            datetime.now(UTC) + timedelta(days=1),
        )
        assert calendar[0].is_overdue


@pytest.mark.parametrize("invalid", [None, "authorization", "version", "reconciliation"])
async def test_explicit_overdue_publish_now_keeps_authorization_and_version_guards(
    db_session, client, monkeypatch, invalid
):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", True)
    user, _, _, version, row = await scheduled_fixture(db_session, ContentChannel.TELEGRAM)
    row.scheduled_at = datetime.now(UTC) - timedelta(days=2)
    if invalid == "authorization":
        row.approved_for_publish_by = None
    elif invalid == "version":
        from sqlalchemy import update

        from app.models.approval import Approval, ApprovalStatus

        await db_session.execute(update(Approval).values(status=ApprovalStatus.REJECTED))
    elif invalid == "reconciliation":
        row.failure_code = "TELEGRAM_RECONCILIATION_REQUIRED"
    await db_session.commit()
    await db_session.refresh(row)
    version_id = version.id
    calls = []
    monkeypatch.setattr(
        dispatcher_worker.publish_telegram_publication, "apply_async", lambda **kw: calls.append(kw)
    )
    assert (await client.post(f"/api/v1/publications/{row.id}/publish-now")).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        detail = await client.get(f"/api/v1/publications/{row.id}")
        assert detail.json()["is_overdue"] is True
        assert detail.json()["content_version_id"] == str(version_id)
        response = await client.post(f"/api/v1/publications/{row.id}/publish-now")
        assert response.status_code == (202 if invalid is None else 409)
        assert len(calls) == int(invalid is None)
        await db_session.refresh(row)
        assert row.content_version_id == version_id
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_overdue_boundary_and_non_scheduled_states(monkeypatch):
    now = datetime.now(UTC)
    monkeypatch.setattr(settings, "publication_auto_dispatch_max_lateness_seconds", 3600)
    row = SimpleNamespace(
        status=PublicationStatus.SCHEDULED, scheduled_at=now - timedelta(seconds=3600)
    )
    assert not operational_fields(row, now)["is_overdue"]
    row.scheduled_at -= timedelta(seconds=1)
    assert operational_fields(row, now)["is_overdue"]
    for status in [
        PublicationStatus.PUBLISHING,
        PublicationStatus.CANCELLED,
        PublicationStatus.PUBLISHED,
    ]:
        row.status = status
        assert not operational_fields(row, now)["is_overdue"]
    with pytest.raises(ValueError):
        Settings(_env_file=None, publication_auto_dispatch_max_lateness_seconds=0)


async def test_safe_operational_status_counts(db_session, monkeypatch):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", False)
    monkeypatch.setattr(settings, "vk_publishing_enabled", False)
    _, _, post, _, row = await scheduled_fixture(db_session, ContentChannel.TELEGRAM)
    task = await db_session.get(Task, post.source_task_id)
    assert task is not None
    task.status = TaskStatus.READY
    task.updated_at = datetime.now(UTC) - timedelta(seconds=settings.task_stuck_after_seconds + 60)
    row.scheduled_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.commit()
    result = await system_status(SimpleNamespace(), db_session)
    assert result["overdue_publications"] == result["scheduled_publications"] == 1
    assert result["due_publications"] == 0
    assert result["publications"]["provider_disabled"] == 1
    assert result["publishing_providers_enabled"] == {"telegram": False, "vk": False}
    assert result["ready_auto_ai_tasks"] == 1
    assert result["stalled_ready_auto_ai_tasks"] == 1
    assert isinstance(result["running_agent_runs"], int)
    assert "heartbeat" not in result


def test_all_operational_services_restart(compose_config):
    for name in [
        "postgres",
        "redis",
        "backend",
        "frontend",
        "ai_worker",
        "ai_control_worker",
        "ai_scheduler",
        "publication_worker",
        "publication_control_worker",
        "publication_scheduler",
        "metrics_worker",
    ]:
        assert compose_config["services"][name]["restart"] == "unless-stopped"


def test_runtime_script_matrix(tmp_path):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[3]
    fake = tmp_path / "docker"
    capture = tmp_path / "calls"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CAPTURE"\n')
    fake.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CAPTURE": str(capture),
    }
    subprocess.run([str(root / "scripts/runtime-up.sh"), "normal"], env=environment, check=True)
    call = capture.read_text().strip().split()
    assert call[:6] == ["compose", "--profile", "publishing", "up", "-d", "postgres"]
    assert set(call[5:]) == {
        "postgres",
        "redis",
        "backend",
        "frontend",
        "ai_worker",
        "ai_control_worker",
        "ai_scheduler",
        "publication_worker",
        "publication_control_worker",
        "publication_scheduler",
        "metrics_worker",
    }
    capture.write_text("")
    subprocess.run(
        [str(root / "scripts/runtime-up.sh"), "maintenance"], env=environment, check=True
    )
    calls = capture.read_text().splitlines()
    assert (
        "stop ai_worker ai_control_worker ai_scheduler "
        "publication_worker publication_control_worker publication_scheduler metrics_worker"
        in calls[0]
    )
    assert calls[1] == "compose up -d postgres redis backend frontend"
    capture.write_text("")
    subprocess.run([str(root / "scripts/runtime-status.sh")], env=environment, check=True)
    assert "ps -a" in capture.read_text()
    assert "config" not in capture.read_text()


async def test_locked_auto_claim_rejects_overdue_but_explicit_plan_decision_preserves_snapshot(
    db_session, monkeypatch
):
    from app.tests.test_plan_publication_scheduling import plan_post

    monkeypatch.setattr(settings, "vk_publishing_enabled", True)
    user, _, post, version, item = await plan_post(db_session)
    service = PublicationService(db_session)
    response = await service.schedule_content(post.id, user)
    row = await db_session.get(Publication, response.id)
    assert row is not None
    item.scheduled_at = row.scheduled_at = datetime.now(UTC) - timedelta(days=2)
    await db_session.commit()
    with pytest.raises(AppError) as blocked:
        await service.claim_for_publish(row.id)
    assert blocked.value.code == "PUBLICATION_OVERDUE"
    result = await service.claim_for_publish(row.id, user)
    assert result.status == PublicationStatus.PUBLISHING
    assert result.content_version_id == version.id
    assert result.channel == item.channel and result.scheduled_at == item.scheduled_at


async def test_locked_auto_claim_rechecks_disabled_provider(db_session, monkeypatch):
    monkeypatch.setattr(settings, "telegram_publishing_enabled", False)
    _, _, _, _, row = await scheduled_fixture(db_session, ContentChannel.TELEGRAM)
    row.scheduled_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    with pytest.raises(AppError) as blocked:
        await PublicationService(db_session).claim_for_publish(row.id)
    assert blocked.value.code == "PUBLICATION_PROVIDER_DISABLED"
    assert row.status == PublicationStatus.SCHEDULED and row.execution_token is None
