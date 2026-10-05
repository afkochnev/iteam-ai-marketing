from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from app.api import content
from app.models.agent_run import AgentRunStatus
from app.models.task import TaskStatus
from app.workers import availability


@pytest.mark.parametrize(
    ("queues", "expected"),
    [
        ({"ai-node": [{"name": "ai"}], "control-node": [{"name": "ai_control"}]}, True),
        ({"control-node": [{"name": "ai_control"}]}, False),
        (None, False),
    ],
)
async def test_availability_checks_the_ai_queue_consumer(monkeypatch, queues, expected):
    inspector = Mock(active_queues=Mock(return_value=queues))
    monkeypatch.setattr(availability.celery_app.control, "inspect", Mock(return_value=inspector))
    assert await availability.queue_has_consumer("ai") is expected


async def test_failed_inspection_is_unknown(monkeypatch):
    inspector = Mock(active_queues=Mock(side_effect=RuntimeError("broker unavailable")))
    monkeypatch.setattr(availability.celery_app.control, "inspect", Mock(return_value=inspector))
    assert await availability.queue_has_consumer("ai") is None


@pytest.mark.parametrize(
    ("status", "available"),
    [
        (AgentRunStatus.QUEUED, True),
        (AgentRunStatus.QUEUED, False),
        (AgentRunStatus.QUEUED, None),
        (AgentRunStatus.RUNNING, None),
        (AgentRunStatus.COMPLETED, None),
    ],
)
async def test_revision_read_model_uses_observed_executor_state(monkeypatch, status, available):
    stale = datetime.now(UTC) - timedelta(seconds=60)
    task = SimpleNamespace(
        id=uuid4(),
        status=TaskStatus.READY
        if status == AgentRunStatus.QUEUED
        else (TaskStatus.IN_PROGRESS if status == AgentRunStatus.RUNNING else TaskStatus.COMPLETED),
        input_data={"base_content_version_id": str(uuid4())},
        output_data={},
        error_message=None,
        created_at=stale,
        updated_at=stale,
    )
    run = SimpleNamespace(
        id=uuid4(), status=status, error_code=None, error_message=None, updated_at=stale
    )
    session = AsyncMock()
    session.scalar.side_effect = [task, run]
    consumer = AsyncMock(return_value=available)
    monkeypatch.setattr(content, "queue_has_consumer", consumer)
    progress = await content.get_revision_status(uuid4(), None, session)
    assert progress.executor_available is available
    assert progress.agent_run_status == status
    if status == AgentRunStatus.QUEUED:
        consumer.assert_awaited_once_with("ai")
    else:
        consumer.assert_not_awaited()
