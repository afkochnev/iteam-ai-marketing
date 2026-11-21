import asyncio
import json
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.api import system
from app.core.config import Settings, settings
from app.core.runtime_telemetry import QUEUES, RuntimeTelemetryWriter, heartbeat_key
from app.core.test_redis import ensure_safe_test_redis_url
from app.models.user import UserRole
from app.services import runtime_diagnostics_service as diagnostics
from app.tests.test_agents import create_user_and_login

NOW = 1_800_000_000
BUILD = "a" * 40


class ReadPipeline:
    def __init__(self, values):
        self.values = values
        self.commands = []

    def llen(self, key):
        self.commands.append(("llen", key))
        return self

    def zrevrange(self, key, start, end, *, withscores):
        assert (start, end, withscores) == (0, 99, True)
        self.commands.append(("zrevrange", key))
        return self

    async def execute(self, *, raise_on_error):
        assert raise_on_error is False
        return [
            self.values.get(key, 0 if command == "llen" else []) for command, key in self.commands
        ]


def client_fixture(values=None):
    pipe = ReadPipeline(values or {})
    client = Mock()
    client.time = AsyncMock(return_value=(NOW, 0))
    client.pipeline = Mock(return_value=pipe)
    client.aclose = AsyncMock()
    return client, pipe


def config(**overrides):
    return Settings(
        _env_file=None,
        app_env=overrides.pop("app_env", "development"),
        database_url="postgresql+asyncpg://unit:unit@db/diagnostics",
        redis_url=overrides.pop("redis_url", "redis://unit:unit@redis/15"),
        build_sha=BUILD,
        **overrides,
    )


def member(instance="1" * 32, queues=None, build=BUILD):
    return json.dumps(
        {"instance_id": instance, "build_sha": build, "queues": queues or []}
    ).encode()


async def collect(values=None, *, options=None, **overrides):
    client, pipe = client_fixture(values)
    result = await diagnostics.collect_runtime_diagnostics(
        config(**overrides),
        transport_options=options or {},
        client_factory=lambda *_a, **_k: client,
    )
    client.aclose.assert_awaited_once()
    return result, pipe


async def test_snapshot_reads_only_bounded_lengths_and_observations():
    key = heartbeat_key("worker", "ai")
    result, pipe = await collect(
        {"ai": 2, "ai\x06\x163": 3, key: [(member(queues=["ai_live_test"]), NOW - 10)]}
    )
    assert result.broker_state == "ok"
    assert result.queues[0].waiting_messages == 5
    ai = next(row for row in result.processes if row.id == "worker:ai")
    assert ai.state == "recent" and ai.age_seconds == 10
    assert ai.queues == ["ai_live_test"]  # A test consumer does not prove an ai consumer.
    assert ai.fresh_instances == ai.observed_instances == 1
    assert ai.build_mismatch is False
    assert len(pipe.commands) == len(QUEUES) * 4 + 7
    assert {command for command, _key in pipe.commands} == {"llen", "zrevrange"}


async def test_priority_buckets_and_heartbeat_namespace_follow_transport_options():
    prefix = "other-app:"
    result, pipe = await collect(
        {
            "ai": 999,
            prefix + "ai": 1,
            prefix + "ai:2": 2,
            prefix + "ai:9": 3,
            prefix + heartbeat_key("worker", "ai"): [(member(queues=["ai"]), NOW - 1)],
        },
        options={"global_keyprefix": prefix, "priority_steps": [0, 2, 9], "sep": ":"},
    )
    assert result.queues[0].waiting_messages == 6
    assert result.processes[0].state == "recent"
    assert all(key.startswith(prefix) for _command, key in pipe.commands)


async def test_stale_expired_and_never_observed_are_not_disabled_workers():
    result, _pipe = await collect(
        {
            heartbeat_key("worker", "ai"): [(member(), NOW - 61)],
            heartbeat_key("worker", "ai_control"): [(member(), NOW - 601)],
        }
    )
    assert result.processes[0].state == "stale"
    assert result.processes[0].fresh_instances == 0
    assert result.processes[1].state == "unobserved"
    assert result.processes[2].state == "unobserved"
    assert result.processes[2].last_seen_at is None


async def test_restart_instances_are_deduplicated_and_current_queues_win():
    result, _pipe = await collect(
        {
            heartbeat_key("worker", "ai"): [
                (member(queues=["ai"]), NOW - 2),
                (member(queues=["ai_live_test"]), NOW - 20),
                (member(instance="2" * 32, queues=["ai"], build="b" * 40), NOW - 1),
            ]
        }
    )
    row = result.processes[0]
    assert row.observed_instances == row.fresh_instances == 2
    assert row.queues == ["ai"]
    assert row.build_mismatch is True


@pytest.mark.parametrize(
    "record",
    [
        [(b'{"secret":"do-not-expose"}', NOW)],
        [(member(), NOW + 30)],
        [(member(), float("inf"))],
        [(member(queues=["unknown-private-queue"]), NOW)],
        [(b"x" * 1025, NOW)],
        ResponseError("do-not-expose redis password"),
    ],
)
async def test_malformed_or_future_observation_is_unknown_and_redacted(record):
    result, _pipe = await collect({heartbeat_key("worker", "ai"): record})
    assert result.broker_state == "partial"
    assert result.processes[0].state == "unknown"
    assert result.processes[0].fresh_instances is None
    assert "do-not-expose" not in result.model_dump_json()
    assert "unknown-private-queue" not in result.model_dump_json()


async def test_wrongtype_queue_is_unknown_not_zero_and_other_queues_survive():
    result, _pipe = await collect({"ai": ResponseError("private broker details"), "metrics": 4})
    assert result.broker_state == "partial"
    assert result.queues[0].waiting_messages is None
    assert next(row for row in result.queues if row.name == "metrics").waiting_messages == 4
    assert "private broker" not in result.model_dump_json()


@pytest.mark.parametrize("disabled,test_mode", [(True, False), (False, True)])
async def test_disabled_or_memory_transport_never_connects(disabled, test_mode):
    factory = Mock(side_effect=AssertionError("No Redis connection"))
    result = await diagnostics.collect_runtime_diagnostics(
        config(
            runtime_diagnostics_enabled=not disabled, app_env="test" if test_mode else "development"
        ),
        client_factory=factory,
    )
    assert result.broker_state == ("disabled" if disabled else "unsupported")
    assert all(row.waiting_messages is None for row in result.queues)
    factory.assert_not_called()


async def test_actual_memory_broker_does_not_read_the_configured_redis(monkeypatch):
    from app.workers.celery_app import celery_app

    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
    monkeypatch.setitem(celery_app.conf, "broker_url", "memory://")
    factory = Mock(side_effect=AssertionError("Wrong broker must not be contacted"))
    result = await diagnostics.collect_runtime_diagnostics(config(), client_factory=factory)
    assert result.broker_state == "unsupported"
    factory.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [{"priority_steps": [0, 3, 3]}, {"priority_steps": []}, {"global_keyprefix": "x" * 129}],
)
async def test_unsupported_queue_layout_does_not_read_other_keys(options):
    factory = Mock(side_effect=AssertionError("No Redis connection"))
    result = await diagnostics.collect_runtime_diagnostics(
        config(), transport_options=options, client_factory=factory
    )
    assert result.broker_state == "unsupported"
    factory.assert_not_called()


async def test_broker_failure_and_timeout_are_unknown_and_close_client(monkeypatch):
    client, _pipe = client_fixture()
    client.time.side_effect = ConnectionError("private connection string")
    result = await diagnostics.collect_runtime_diagnostics(
        config(), transport_options={}, client_factory=lambda *_a, **_k: client
    )
    assert result.broker_state == "unavailable"
    assert all(row.waiting_messages is None for row in result.queues)
    assert "private" not in result.model_dump_json()
    client.aclose.assert_awaited_once()

    async def slow_time():
        await asyncio.sleep(1)

    client.time.side_effect = slow_time
    client.aclose.reset_mock()
    monkeypatch.setattr(diagnostics, "TOTAL_TIMEOUT_SECONDS", 0.01)
    result = await diagnostics.collect_runtime_diagnostics(
        config(), transport_options={}, client_factory=lambda *_a, **_k: client
    )
    assert result.broker_state == "unavailable"
    client.aclose.assert_awaited_once()


async def test_runtime_api_is_admin_only_before_collecting(client, db_session, monkeypatch):
    collector = AsyncMock(return_value=diagnostics._empty("disabled"))
    monkeypatch.setattr(system, "collect_runtime_diagnostics", collector)
    assert (await client.get("/api/v1/system/runtime")).status_code == 401
    await create_user_and_login(client, db_session, UserRole.MANAGER)
    assert (await client.get("/api/v1/system/runtime")).status_code == 403
    collector.assert_not_awaited()
    await client.post("/api/v1/auth/logout")
    await create_user_and_login(client, db_session, UserRole.ADMIN)
    response = await client.get("/api/v1/system/runtime")
    assert response.status_code == 200
    assert response.json()["broker_state"] == "disabled"
    collector.assert_awaited_once_with()


@pytest.mark.integration
async def test_real_redis_snapshot_does_not_consume_or_rewrite_messages():
    url = ensure_safe_test_redis_url(settings.redis_url)
    prefix = f"runtime-observation-test:{uuid4().hex}:"
    client = Redis.from_url(url)
    queue_key = prefix + "ai"
    observation_key = prefix + heartbeat_key("worker", "ai")
    writer = RuntimeTelemetryWriter(url, key_prefix=prefix, build_sha=BUILD)
    other = RuntimeTelemetryWriter(url, key_prefix=prefix, build_sha=BUILD)
    try:
        await client.rpush(queue_key, b"opaque synthetic message")
        assert writer.report("worker", "ai", ["ai_live_test"]) is True
        assert other.report("worker", "ai", ["ai"]) is True
        before = await client.lrange(queue_key, 0, -1)
        result = await diagnostics.collect_runtime_diagnostics(
            config(redis_url=url), transport_options={"global_keyprefix": prefix}
        )
        assert result.broker_state == "ok"
        assert result.queues[0].waiting_messages == 1
        assert result.processes[0].state == "recent"
        assert result.processes[0].fresh_instances == 2
        assert result.processes[0].queues == ["ai", "ai_live_test"]
        writer.remove("worker", "ai")
        remaining = await diagnostics.collect_runtime_diagnostics(
            config(redis_url=url), transport_options={"global_keyprefix": prefix}
        )
        assert remaining.processes[0].fresh_instances == 1
        assert remaining.processes[0].queues == ["ai"]
        assert await client.lrange(queue_key, 0, -1) == before
        assert "opaque synthetic" not in result.model_dump_json()
    finally:
        writer.remove("worker", "ai")
        other.remove("worker", "ai")
        await client.delete(queue_key, observation_key)
        await client.aclose()
