from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class QueueDiagnostic(BaseModel):
    name: str
    waiting_messages: int | None = None


class ProcessDiagnostic(BaseModel):
    id: str
    kind: Literal["worker", "scheduler"]
    role: str
    state: Literal["recent", "stale", "unobserved", "unknown"] = "unknown"
    last_seen_at: datetime | None = None
    age_seconds: float | None = None
    observed_instances: int | None = None
    fresh_instances: int | None = None
    queues: list[str] = []
    build_mismatch: bool | None = None


class RuntimeDiagnostics(BaseModel):
    sampled_at: datetime
    broker_state: Literal["ok", "partial", "unavailable", "unsupported", "disabled"]
    stale_after_seconds: int
    queues: list[QueueDiagnostic]
    processes: list[ProcessDiagnostic]
