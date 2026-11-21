import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from celery.beat import PersistentScheduler

from app.core.runtime_telemetry import (
    HEARTBEAT_INTERVAL_SECONDS,
    MAX_ROLE_OBSERVATIONS,
    RETENTION_SECONDS,
    RuntimeTelemetryWriter,
    canonical_member,
    heartbeat_key,
)
from app.workers.runtime_heartbeat import (
    RuntimeHeartbeat,
    RuntimeHeartbeatScheduler,
    register_runtime_heartbeat,
)


class MemoryRedis:
    def __init__(self):
        self.seconds = 1_000
        self.members = {}
        self.expiries = {}
        self.commands = []

    def time(self):
        self.seconds += 1
        return self.seconds, 123_456

    def zrem(self, key, *members):
        self.commands.append(("zrem", key, members))
        for member in members:
            self.members.get(key, {}).pop(member, None)

    def pipeline(self, *, transaction):
        assert transaction is True
        return MemoryPipeline(self)


class MemoryPipeline:
    def __init__(self, client):
        self.client = client
        self.pending = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def __getattr__(self, command):
        def enqueue(*args):
            self.pending.append((command, args))
            return self

        return enqueue

    def execute(self):
        for command, args in self.pending:
            key, *values = args
            self.client.commands.append((command, key, tuple(values)))
            members = self.client.members.setdefault(key, {})
            if command == "zrem":
                for member in values:
                    members.pop(member, None)
            elif command == "zadd":
                members.update(values[0])
            elif command == "zremrangebyscore":
                cutoff = values[1]
                for member, score in list(members.items()):
                    if score <= cutoff:
                        del members[member]
            elif command == "zremrangebyrank":
                ordered = sorted(members, key=lambda member: (members[member], member))
                stop = len(ordered) + values[1]
                if stop >= 0:
                    for member in ordered[: stop + 1]:
                        del members[member]
            elif command == "expire":
                self.client.expiries[key] = values[0]
            else:
                raise AssertionError(command)


def writer(client, **kwargs):
    return RuntimeTelemetryWriter(
        "redis://redis:6379/15",
        client_factory=lambda _url: client,
        **kwargs,
    )


def state_fixture(*, role="ai", scheduler=None, enabled=True, broker="redis://redis:6379/15"):
    app = SimpleNamespace(conf=SimpleNamespace(broker_transport_options={}, broker_url=broker))
    config = SimpleNamespace(
        worker_role=role,
        scheduler_role=scheduler,
        runtime_diagnostics_enabled=enabled,
        is_test_context=False,
        celery_broker_url=broker,
        build_sha="a" * 40,
    )
    state = RuntimeHeartbeat(app, config)
    consumer = SimpleNamespace(
        app=app,
        hostname="worker@internal",
        task_consumer=SimpleNamespace(queues=[SimpleNamespace(name="ai_live_test")]),
    )
    heart = SimpleNamespace(eventer=SimpleNamespace(app=app, hostname=consumer.hostname))
    return app, config, state, consumer, heart


def test_members_are_canonical_and_contain_only_safe_fields():
    member = canonical_member("a" * 32, "b" * 40, ["ai_live_test", "ai", "ai"])
    assert member == canonical_member("a" * 32, "b" * 40, ["ai", "ai_live_test"])
    assert json.loads(member) == {
        "instance_id": "a" * 32,
        "build_sha": "b" * 40,
        "queues": ["ai", "ai_live_test"],
    }
    assert json.loads(canonical_member("a" * 32, "unknown", []))["build_sha"] is None
    with pytest.raises(ValueError):
        canonical_member("not-an-instance", None, [])
    with pytest.raises(ValueError):
        canonical_member("a" * 32, None, ["arbitrary-secret-queue"])
    with pytest.raises(ValueError):
        heartbeat_key("worker", "arbitrary-role")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"enabled": False},
        {"broker_url": "memory://"},
        {"broker_url": "amqp://broker"},
        {"key_prefix": "x" * 129},
        {"key_prefix": False},
    ],
)
def test_disabled_or_unsupported_registration_and_reports_do_not_open_connections(kwargs):
    factory = Mock(side_effect=AssertionError("Unexpected connection"))
    options = {"broker_url": "redis://redis:6379/15", "client_factory": factory, **kwargs}
    telemetry = RuntimeTelemetryWriter(**options)
    assert not telemetry.report("worker", "ai", ["ai"])
    telemetry.remove("worker", "ai")
    factory.assert_not_called()


def test_report_uses_server_time_and_bounded_prefixed_role_storage():
    client = MemoryRedis()
    factory = Mock(return_value=client)
    telemetry = RuntimeTelemetryWriter(
        "redis://redis:6379/15", key_prefix="review:", client_factory=factory
    )
    factory.assert_not_called()
    assert telemetry.report("worker", "ai", ["ai_live_test"])
    key = "review:" + heartbeat_key("worker", "ai")
    assert list(client.members) == [key]
    assert list(client.members[key].values()) == [1001.123456]
    assert client.expiries[key] == RETENTION_SECONDS
    assert [name for name, *_ in client.commands] == [
        "zadd",
        "zremrangebyscore",
        "zremrangebyrank",
        "expire",
    ]
    assert client.commands[2] == ("zremrangebyrank", key, (0, -MAX_ROLE_OBSERVATIONS - 1))


def test_throttle_and_redis_failures_are_bounded_and_do_not_log_errors(capsys):
    moment = [0.0]
    client = Mock()
    client.time.side_effect = RuntimeError("redis://private:credential@host/0")
    telemetry = writer(client, clock=lambda: moment[0])
    assert not telemetry.report("worker", "ai", ["ai"])
    assert not telemetry.report("worker", "ai", ["ai"])
    client.time.assert_called_once_with()
    moment[0] = HEARTBEAT_INTERVAL_SECONDS
    assert not telemetry.report("worker", "ai", ["ai"])
    assert client.time.call_count == 2
    assert capsys.readouterr() == ("", "")


def test_clock_failure_does_not_open_connection_and_can_recover():
    client = MemoryRedis()
    clock = Mock(side_effect=[RuntimeError("clock unavailable"), float("nan"), 0.0])
    factory = Mock(return_value=client)
    telemetry = RuntimeTelemetryWriter("redis://redis:6379/15", clock=clock, client_factory=factory)
    assert not telemetry.report("worker", "ai", ["ai"])
    assert not telemetry.report("worker", "ai", ["ai"])
    factory.assert_not_called()
    assert telemetry.report("worker", "ai", ["ai"])


def test_queue_changes_and_shutdown_remove_only_the_same_instance():
    client = MemoryRedis()
    moment = [0.0]
    first = writer(client, clock=lambda: moment[0])
    second = writer(client, clock=lambda: moment[0])
    assert first.report("worker", "ai", ["ai_live_test"])
    assert second.report("worker", "ai", ["ai"])
    key = heartbeat_key("worker", "ai")
    assert len(client.members[key]) == 2
    moment[0] = HEARTBEAT_INTERVAL_SECONDS
    assert first.report("worker", "ai", ["ai"])
    assert len(client.members[key]) == 2
    first.remove("worker", "ai")
    assert len(client.members[key]) == 1
    assert json.loads(next(iter(client.members[key])))["instance_id"] == second.instance_id


def test_role_observations_do_not_grow_beyond_the_bound():
    client = MemoryRedis()
    for _ in range(MAX_ROLE_OBSERVATIONS + 2):
        assert writer(client).report("worker", "ai", ["ai"])
    assert len(client.members[heartbeat_key("worker", "ai")]) == MAX_ROLE_OBSERVATIONS


def test_ready_and_heartbeat_use_actual_queues_and_ignore_other_workers():
    app, _config, state, consumer, heart = state_fixture()
    state.writer = Mock()
    state.on_heartbeat(heart)
    state.writer.report.assert_not_called()
    state.on_worker_ready(consumer)
    state.writer.report.assert_called_once_with("worker", "ai", ("ai_live_test",))
    state.writer.report.reset_mock()
    heart.eventer.app = object()
    state.on_heartbeat(heart)
    heart.eventer.app = app
    heart.eventer.hostname = "another-worker"
    state.on_heartbeat(heart)
    state.writer.report.assert_not_called()
    heart.eventer.hostname = consumer.hostname
    consumer.task_consumer.queues = [SimpleNamespace(name="ai")]
    state.on_heartbeat(heart)
    state.writer.report.assert_called_once_with("worker", "ai", ("ai",))


def test_offline_heartbeat_does_not_refresh_an_observation_after_shutdown():
    _app, _config, state, consumer, heart = state_fixture()
    state.writer = Mock()
    state.on_worker_ready(consumer)
    state.writer.report.reset_mock()
    state.on_worker_stopping(sender="another-worker")
    state.writer.remove.assert_not_called()
    state.on_worker_stopping(sender=consumer.hostname)
    state.on_heartbeat(heart)  # Heart.stop also emits heartbeat_sent.
    state.writer.report.assert_not_called()
    state.writer.remove.assert_called_once_with("worker", "ai")


def test_unknown_consumer_queues_are_not_published():
    _app, _config, state, consumer, _heart = state_fixture()
    state.writer = Mock()
    consumer.task_consumer.queues = [SimpleNamespace(name="unknown-queue")]
    state.on_worker_ready(consumer)
    state.writer.report.assert_not_called()


@pytest.mark.parametrize("actual_broker", ["memory://", None, 42, ["redis://other/0"]])
def test_hooks_use_actual_celery_broker_and_reject_unsupported_overrides(actual_broker):
    app, config, _state, consumer, heart = state_fixture()
    app.conf.broker_url = actual_broker
    state = RuntimeHeartbeat(app, config)
    factory = Mock(side_effect=AssertionError("Wrong Redis must not be contacted"))
    state.writer._client_factory = factory
    state.on_worker_ready(consumer)
    state.on_heartbeat(heart)
    assert not state.writer.enabled
    factory.assert_not_called()


def test_test_context_does_not_publish_to_overridden_redis_broker():
    app, config, _state, consumer, heart = state_fixture()
    config.is_test_context = True
    state = RuntimeHeartbeat(app, config)
    factory = Mock(side_effect=AssertionError("Test context must stay isolated"))
    state.writer._client_factory = factory
    state.on_worker_ready(consumer)
    state.on_heartbeat(heart)
    assert not state.writer.enabled
    factory.assert_not_called()


def test_registration_is_idempotent_and_creates_no_connection(monkeypatch):
    app, config, _state, _consumer, _heart = state_fixture(role=None, scheduler="publication")
    factory = Mock(side_effect=AssertionError("No Redis at import/registration"))
    monkeypatch.setattr("redis.Redis.from_url", factory)
    registered = register_runtime_heartbeat(app, config)
    try:
        assert register_runtime_heartbeat(app, config) is registered
        assert app.conf.beat_scheduler.endswith(":RuntimeHeartbeatScheduler")
        factory.assert_not_called()
    finally:
        registered.disconnect()


@pytest.mark.parametrize("enabled", [True, False])
def test_scheduler_observes_completed_ticks_and_keeps_disabled_behavior(monkeypatch, enabled):
    app, _config, state, _consumer, _heart = state_fixture(
        role=None, scheduler="ai", enabled=enabled
    )
    state.writer = Mock(enabled=enabled)
    app._iteam_runtime_heartbeat = state

    def initialize(scheduler, *_args, **_kwargs):
        scheduler.app = app
        scheduler.max_interval = 300

    monkeypatch.setattr(PersistentScheduler, "__init__", initialize)
    parent_tick = Mock(return_value=300)
    monkeypatch.setattr(PersistentScheduler, "tick", parent_tick)
    monkeypatch.setattr(PersistentScheduler, "close", Mock())
    scheduler = RuntimeHeartbeatScheduler(app=app)
    assert scheduler.max_interval == (HEARTBEAT_INTERVAL_SECONDS if enabled else 300)
    assert scheduler.tick() == (HEARTBEAT_INTERVAL_SECONDS if enabled else 300)
    if enabled:
        state.writer.report.assert_called_once_with("scheduler", "ai")
    else:
        state.writer.report.assert_not_called()
    state.writer.report.reset_mock()
    parent_tick.side_effect = RuntimeError("scheduler failed")
    with pytest.raises(RuntimeError, match="scheduler failed"):
        scheduler.tick()
    state.writer.report.assert_not_called()
    scheduler.close()
    state.writer.remove.assert_called_once_with("scheduler", "ai")
