"""Bounded passive Redis observations; never inspect workers or read message bodies."""

import asyncio
import json
import math
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from redis.asyncio import Redis

from app.core.config import Settings, settings
from app.core.runtime_telemetry import (
    MAX_ROLE_OBSERVATIONS,
    QUEUES,
    SCHEDULER_ROLES,
    WORKER_ROLES,
    heartbeat_key,
)
from app.core.runtime_telemetry import (
    RETENTION_SECONDS as HEARTBEAT_RETENTION_SECONDS,
)
from app.core.runtime_telemetry import (
    STALE_SECONDS as HEARTBEAT_STALE_SECONDS,
)
from app.schemas.runtime_diagnostics import (
    ProcessDiagnostic,
    QueueDiagnostic,
    RuntimeDiagnostics,
)

TOTAL_TIMEOUT_SECONDS = 2.0
_PRIORITY_STEPS = (0, 3, 6, 9)
_PRIORITY_SEPARATOR = "\x06\x16"


def _empty(state: Any) -> RuntimeDiagnostics:
    processes = [
        ProcessDiagnostic(id=f"worker:{role}", kind="worker", role=role) for role in WORKER_ROLES
    ] + [
        ProcessDiagnostic(id=f"scheduler:{role}", kind="scheduler", role=role)
        for role in SCHEDULER_ROLES
    ]
    return RuntimeDiagnostics(
        sampled_at=datetime.now(UTC),
        broker_state=state,
        stale_after_seconds=HEARTBEAT_STALE_SECONDS,
        queues=[QueueDiagnostic(name=name) for name in QUEUES],
        processes=processes,
    )


def _transport_keys(options: Mapping[str, Any]) -> tuple[str, list[list[str]]]:
    prefix = options.get("global_keyprefix", "")
    separator = options.get("sep", _PRIORITY_SEPARATOR)
    priorities = options.get("priority_steps", _PRIORITY_STEPS)
    if (
        not isinstance(prefix, str)
        or len(prefix) > 128
        or not isinstance(separator, str)
        or not 1 <= len(separator) <= 8
        or not isinstance(priorities, tuple | list)
        or not 1 <= len(priorities) <= 10
        or any(type(value) is not int or not 0 <= value <= 9 for value in priorities)
        or list(priorities) != sorted(set(priorities))
        or priorities[0] != 0
    ):
        raise ValueError("Unsupported broker queue layout")
    return prefix, [
        [
            prefix + queue + (separator + str(priority) if priority else "")
            for priority in priorities
        ]
        for queue in QUEUES
    ]


def _observations(
    process: ProcessDiagnostic, rows: Any, now: float, expected_build: str | None
) -> bool:
    if not isinstance(rows, list) or len(rows) > MAX_ROLE_OBSERVATIONS:
        return False
    seen: dict[str, tuple[float, dict[str, Any]]] = {}
    try:
        for raw, score in rows:
            if not isinstance(raw, bytes | str) or len(raw) > 1024:
                return False
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"instance_id", "build_sha", "queues"}:
                return False
            instance = value["instance_id"]
            build = value["build_sha"]
            queues = value["queues"]
            if (
                not isinstance(instance, str)
                or re.fullmatch(r"[0-9a-f]{32}", instance) is None
                or (
                    build is not None
                    and (not isinstance(build, str) or re.fullmatch(r"[0-9a-f]{40}", build) is None)
                )
                or not isinstance(queues, list)
                or len(queues) > len(QUEUES)
                or any(not isinstance(queue, str) or queue not in QUEUES for queue in queues)
                or len(queues) != len(set(queues))
                or (process.kind == "scheduler" and queues)
            ):
                return False
            timestamp = float(score)
            if not math.isfinite(timestamp) or timestamp < 0 or timestamp > now + 5:
                return False
            if timestamp < now - HEARTBEAT_RETENTION_SECONDS:
                continue
            if instance not in seen or timestamp > seen[instance][0]:
                seen[instance] = (timestamp, value)
    except (ValueError, TypeError, OverflowError):
        return False
    process.observed_instances = len(seen)
    recent = [
        (score, value) for score, value in seen.values() if now - score <= HEARTBEAT_STALE_SECONDS
    ]
    process.fresh_instances = len(recent)
    if not seen:
        process.state = "unobserved"
        return True
    latest = max(score for score, _value in seen.values())
    process.last_seen_at = datetime.fromtimestamp(latest, UTC)
    process.age_seconds = round(max(0.0, now - latest), 1)
    process.state = "recent" if recent else "stale"
    process.queues = sorted({queue for _score, value in recent for queue in value["queues"]})
    known_builds = [value["build_sha"] for _score, value in recent if value["build_sha"]]
    if expected_build and re.fullmatch(r"[0-9a-f]{40}", expected_build) and known_builds:
        process.build_mismatch = any(build != expected_build for build in known_builds)
    return True


async def collect_runtime_diagnostics(
    config: Settings = settings,
    *,
    transport_options: Mapping[str, Any] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> RuntimeDiagnostics:
    if not config.runtime_diagnostics_enabled:
        return _empty("disabled")
    broker_url = config.celery_broker_url
    try:
        if urlsplit(broker_url).scheme not in {"redis", "rediss"}:
            return _empty("unsupported")
        if transport_options is None:
            from app.workers.celery_app import celery_app

            broker_url = celery_app.conf.broker_url
            if not isinstance(broker_url, str) or urlsplit(broker_url).scheme not in {
                "redis",
                "rediss",
            }:
                return _empty("unsupported")
            transport_options = celery_app.conf.broker_transport_options
        if not isinstance(transport_options, Mapping):
            return _empty("unsupported")
        prefix, queue_keys = _transport_keys(transport_options)
    except (ValueError, TypeError):
        return _empty("unsupported")
    snapshot = _empty("unavailable")
    client = None
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
            factory = client_factory or Redis.from_url
            client = factory(
                broker_url,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
                decode_responses=False,
            )
            seconds, microseconds = await client.time()
            now = float(seconds) + float(microseconds) / 1_000_000
            if not math.isfinite(now) or now < 0:
                return snapshot
            snapshot.sampled_at = datetime.fromtimestamp(now, UTC)
            pipe = client.pipeline(transaction=False)
            for keys in queue_keys:
                for key in keys:
                    pipe.llen(key)
            for process in snapshot.processes:
                pipe.zrevrange(
                    prefix + heartbeat_key(process.kind, process.role),
                    0,
                    MAX_ROLE_OBSERVATIONS - 1,
                    withscores=True,
                )
            results = await pipe.execute(raise_on_error=False)
            expected = sum(map(len, queue_keys)) + len(snapshot.processes)
            if not isinstance(results, list) or len(results) != expected:
                return snapshot
            snapshot.broker_state = "ok"
            offset = 0
            for queue, keys in zip(snapshot.queues, queue_keys, strict=True):
                counts = results[offset : offset + len(keys)]
                if all(type(count) is int and count >= 0 for count in counts):
                    queue.waiting_messages = sum(counts)
                else:
                    snapshot.broker_state = "partial"
                offset += len(keys)
            for process, rows in zip(snapshot.processes, results[offset:], strict=True):
                if not _observations(process, rows, now, config.build_sha):
                    snapshot.broker_state = "partial"
            return snapshot
    except Exception:
        return _empty("unavailable")
    finally:
        if client is not None:
            try:
                async with asyncio.timeout(0.25):
                    await client.aclose()
            except Exception:
                pass
