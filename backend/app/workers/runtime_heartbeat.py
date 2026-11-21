"""Celery lifecycle observations, independent of tasks and provider capabilities."""

from collections.abc import Mapping
from typing import Any

from celery import Celery, signals
from celery.beat import PersistentScheduler

from app.core.config import Settings
from app.core.runtime_telemetry import (
    HEARTBEAT_INTERVAL_SECONDS,
    QUEUES,
    SCHEDULER_ROLES,
    WORKER_ROLES,
    RuntimeTelemetryWriter,
)

_STATE_ATTRIBUTE = "_iteam_runtime_heartbeat"


class RuntimeHeartbeat:
    def __init__(self, app: Celery, config: Settings) -> None:
        self.app = app
        self.worker_role = config.worker_role
        self.scheduler_role = config.scheduler_role
        options = app.conf.broker_transport_options
        prefix = options.get("global_keyprefix", "") if isinstance(options, Mapping) else None
        broker_url = app.conf.broker_url
        if config.is_test_context or not isinstance(broker_url, str):
            broker_url = "memory://"
        self.writer = RuntimeTelemetryWriter(
            broker_url,
            enabled=config.runtime_diagnostics_enabled and not config.is_test_context,
            build_sha=config.build_sha,
            key_prefix=prefix,
        )
        self.consumer: Any = None
        self.hostname: str | None = None
        self.worker_ready = False
        self.worker_stopping = False
        self.scheduler_stopping = False
        self._signal_id = f"iteam-runtime-{id(app)}"

    def connect(self) -> None:
        signals.worker_ready.connect(
            self.on_worker_ready, weak=False, dispatch_uid=self._signal_id + "-ready"
        )
        signals.heartbeat_sent.connect(
            self.on_heartbeat, weak=False, dispatch_uid=self._signal_id + "-heartbeat"
        )
        signals.worker_shutting_down.connect(
            self.on_worker_stopping, weak=False, dispatch_uid=self._signal_id + "-stopping"
        )
        signals.worker_shutdown.connect(
            self.on_worker_shutdown, weak=False, dispatch_uid=self._signal_id + "-shutdown"
        )

    def disconnect(self) -> None:
        signals.worker_ready.disconnect(dispatch_uid=self._signal_id + "-ready")
        signals.heartbeat_sent.disconnect(dispatch_uid=self._signal_id + "-heartbeat")
        signals.worker_shutting_down.disconnect(dispatch_uid=self._signal_id + "-stopping")
        signals.worker_shutdown.disconnect(dispatch_uid=self._signal_id + "-shutdown")

    def _queues(self) -> tuple[str, ...] | None:
        try:
            queues = self.consumer.task_consumer.queues
            names = tuple(queue.name for queue in queues)
            if any(not isinstance(name, str) or name not in QUEUES for name in names):
                return None
            return tuple(sorted(set(names)))
        except Exception:
            return None

    def _report_worker(self) -> None:
        if self.worker_role is None or self.worker_role not in WORKER_ROLES:
            return
        queues = self._queues()
        if queues is not None:
            self.writer.report("worker", self.worker_role, queues)

    def on_worker_ready(self, sender: Any = None, **_kwargs: Any) -> None:
        if getattr(sender, "app", None) is not self.app or self.worker_role not in WORKER_ROLES:
            return
        hostname = getattr(sender, "hostname", None)
        if not isinstance(hostname, str) or not hostname:
            return
        self.consumer = sender
        self.hostname = hostname
        self.worker_stopping = False
        self.worker_ready = True
        self._report_worker()

    def on_heartbeat(self, sender: Any = None, **_kwargs: Any) -> None:
        if not self.worker_ready or self.worker_stopping:
            return
        eventer = getattr(sender, "eventer", None)
        if (
            getattr(eventer, "app", None) is not self.app
            or getattr(eventer, "hostname", None) != self.hostname
        ):
            return
        self._report_worker()

    def _stop_worker(self) -> None:
        self.worker_stopping = True
        self.worker_ready = False
        if self.worker_role is not None and self.worker_role in WORKER_ROLES:
            self.writer.remove("worker", self.worker_role)

    def on_worker_stopping(self, sender: Any = None, **_kwargs: Any) -> None:
        # Celery supplies the nodename, not the app, for this signal.
        if self.hostname is not None and sender == self.hostname:
            self._stop_worker()

    def on_worker_shutdown(self, sender: Any = None, **_kwargs: Any) -> None:
        if (
            getattr(sender, "app", None) is self.app
            and getattr(sender, "hostname", None) == self.hostname
        ):
            self._stop_worker()

    def report_scheduler(self) -> None:
        if (
            not self.scheduler_stopping
            and self.scheduler_role is not None
            and self.scheduler_role in SCHEDULER_ROLES
        ):
            self.writer.report("scheduler", self.scheduler_role)

    def stop_scheduler(self) -> None:
        self.scheduler_stopping = True
        if self.scheduler_role is not None and self.scheduler_role in SCHEDULER_ROLES:
            self.writer.remove("scheduler", self.scheduler_role)


# Celery 5.5 does not ship a typed scheduler interface.
class RuntimeHeartbeatScheduler(PersistentScheduler):  # type: ignore[misc]
    """Keep Beat's persistent schedule and observe completed loop iterations."""

    max_interval: float

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        state = getattr(self.app, _STATE_ATTRIBUTE, None)
        self._runtime_state = state if isinstance(state, RuntimeHeartbeat) else None
        if self._observed():
            self.max_interval = min(self.max_interval, HEARTBEAT_INTERVAL_SECONDS)

    def _observed(self) -> bool:
        return (
            self._runtime_state is not None
            and self._runtime_state.writer.enabled
            and self._runtime_state.scheduler_role in SCHEDULER_ROLES
        )

    def tick(self, *args: Any, **kwargs: Any) -> float:
        delay = float(super().tick(*args, **kwargs))
        if self._observed():
            assert self._runtime_state is not None
            self._runtime_state.report_scheduler()
            return min(delay, HEARTBEAT_INTERVAL_SECONDS)
        return delay

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._runtime_state is not None:
                self._runtime_state.stop_scheduler()


def register_runtime_heartbeat(app: Celery, config: Settings) -> RuntimeHeartbeat:
    existing = getattr(app, _STATE_ATTRIBUTE, None)
    if isinstance(existing, RuntimeHeartbeat):
        return existing
    state = RuntimeHeartbeat(app, config)
    setattr(app, _STATE_ATTRIBUTE, state)
    # Install before Celery constructs Heart, which snapshots signal receivers.
    state.connect()
    if config.scheduler_role in SCHEDULER_ROLES:
        app.conf.beat_scheduler = "app.workers.runtime_heartbeat:RuntimeHeartbeatScheduler"
    return state
