from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

TRANSIENT_CODES = frozenset(
    {
        "AGENT_PROVIDER_ERROR",
        "AGENT_PROVIDER_TIMEOUT",
        "AGENT_TIMEOUT",
        "AGENT_RUNTIME_ERROR",
        "QUEUE_ENQUEUE_FAILED",
        "REDIS_UNAVAILABLE",
        "RATE_LIMITED",
    }
)


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    exhausted: bool
    delay_seconds: int


def classify_error(code: str, retry_count: int = 0) -> RetryDecision:
    retryable = code in TRANSIENT_CODES
    if not retryable:
        return RetryDecision(False, False, 0)
    return RetryDecision(
        retryable=True,
        exhausted=retry_count >= settings.agent_max_retries,
        delay_seconds=settings.agent_retry_backoff_seconds * (2**retry_count),
    )


def can_retry(code: str, retry_count: int) -> bool:
    return code in TRANSIENT_CODES and retry_count < settings.agent_max_retries


def retry_exhausted(code: str, retry_count: int) -> bool:
    return code in TRANSIENT_CODES and retry_count >= settings.agent_max_retries
