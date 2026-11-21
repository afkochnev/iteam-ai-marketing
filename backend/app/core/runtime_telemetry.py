"""Small Redis observations; never store task bodies, credentials or hostnames."""

import json
import math
import re
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

QUEUES = (
    "ai",
    "ai_control",
    "ai_live_test",
    "publication",
    "publication_control",
    "metrics",
    "unrouted",
)
WORKER_ROLES = ("ai", "ai_control", "publication", "publication_control", "metrics")
SCHEDULER_ROLES = ("ai", "publication")
HEARTBEAT_INTERVAL_SECONDS = 15
STALE_SECONDS = 60
RETENTION_SECONDS = 600
MAX_ROLE_OBSERVATIONS = 100
SOCKET_TIMEOUT_SECONDS = 0.3

_INSTANCE_ID = re.compile(r"[0-9a-f]{32}\Z")
_BUILD_SHA = re.compile(r"[0-9a-f]{40}\Z")


def heartbeat_key(kind: str, role: str) -> str:
    roles = {"worker": WORKER_ROLES, "scheduler": SCHEDULER_ROLES}
    if kind not in roles or role not in roles[kind]:
        raise ValueError("Unknown runtime observation role")
    return f"iteam:runtime:v1:{kind}:{role}"


def normalize_key_prefix(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 128:
        return None
    return value


def canonical_member(instance_id: str, build_sha: str | None, queues: Iterable[str]) -> str:
    if _INSTANCE_ID.fullmatch(instance_id) is None:
        raise ValueError("Invalid runtime observation instance")
    names = tuple(queues)
    if any(not isinstance(name, str) or name not in QUEUES for name in names):
        raise ValueError("Unknown runtime observation queue")
    return json.dumps(
        {
            "instance_id": instance_id,
            "build_sha": (
                build_sha
                if isinstance(build_sha, str) and _BUILD_SHA.fullmatch(build_sha)
                else None
            ),
            "queues": sorted(set(names)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def redis_broker_supported(broker_url: object) -> bool:
    if not isinstance(broker_url, str):
        return False
    try:
        parsed = urlsplit(broker_url)
        return parsed.scheme in {"redis", "rediss"} and bool(parsed.hostname) and parsed.port != 0
    except (TypeError, ValueError):
        return False


def _redis_client(broker_url: str) -> Any:
    from redis import Redis
    from redis.backoff import NoBackoff
    from redis.retry import Retry

    return Redis.from_url(
        broker_url,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), 0),
    )


class RuntimeTelemetryWriter:
    """Best-effort, throttled observations, created without opening a connection."""

    def __init__(
        self,
        broker_url: str,
        *,
        enabled: bool = True,
        build_sha: str | None = None,
        instance_id: str | None = None,
        key_prefix: object = "",
        clock: Callable[[], float] = time.monotonic,
        client_factory: Callable[[str], Any] = _redis_client,
    ) -> None:
        prefix = normalize_key_prefix(key_prefix)
        self.enabled = enabled and prefix is not None and redis_broker_supported(broker_url)
        self.instance_id = instance_id or uuid4().hex
        self.build_sha = build_sha
        self._broker_url = broker_url
        self._prefix = prefix or ""
        self._clock = clock
        self._client_factory = client_factory
        self._client: Any = None
        self._last_attempt: float | None = None
        self._members: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def _connection(self) -> Any:
        if self._client is None:
            self._client = self._client_factory(self._broker_url)
        return self._client

    def report(self, kind: str, role: str, queues: Iterable[str] = ()) -> bool:
        if not self.enabled or not self._lock.acquire(blocking=False):
            return False
        try:
            key = self._prefix + heartbeat_key(kind, role)
            member = canonical_member(self.instance_id, self.build_sha, queues)
            now = self._clock()
            if not math.isfinite(now):
                return False
            if (
                self._last_attempt is not None
                and now - self._last_attempt < HEARTBEAT_INTERVAL_SECONDS
            ):
                return False
            self._last_attempt = now
            client = self._connection()
            seconds, microseconds = client.time()
            score = float(seconds) + float(microseconds) / 1_000_000
            if not math.isfinite(score) or score < 0:
                return False
            owned = self._members.setdefault(key, set())
            previous = owned - {member}
            # Retain attempted identities even after an ambiguous Redis response.
            owned.add(member)
            with client.pipeline(transaction=True) as pipeline:
                if previous:
                    pipeline.zrem(key, *sorted(previous))
                pipeline.zadd(key, {member: score})
                pipeline.zremrangebyscore(key, "-inf", score - RETENTION_SECONDS)
                pipeline.zremrangebyrank(key, 0, -MAX_ROLE_OBSERVATIONS - 1)
                pipeline.expire(key, RETENTION_SECONDS)
                pipeline.execute()
            owned.intersection_update({member})
            return True
        except Exception:
            # Telemetry must not interrupt business processing or leak Redis URLs.
            return False
        finally:
            self._lock.release()

    def remove(self, kind: str, role: str) -> None:
        if not self.enabled or not self._lock.acquire(timeout=SOCKET_TIMEOUT_SECONDS):
            return
        try:
            key = self._prefix + heartbeat_key(kind, role)
            owned = self._members.get(key)
            if owned and self._client is not None:
                self._client.zrem(key, *sorted(owned))
                self._members.pop(key, None)
        except Exception:
            pass
        finally:
            self._lock.release()
