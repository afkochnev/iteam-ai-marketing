from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class MetricsProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderMetricsResult:
    observed_at: datetime
    views: int | None = None
    impressions: int | None = None
    reactions: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    clicks: int | None = None
    subscribers: int | None = None
    provider: str | None = None


class MetricsProvider(Protocol):
    async def get_metrics(self, *, external_id: str) -> ProviderMetricsResult: ...


class TelegramMetricsProvider:
    """The Bot API does not expose reliable historical channel-post insights."""

    async def get_metrics(self, *, external_id: str) -> ProviderMetricsResult:
        raise MetricsProviderError(
            "TELEGRAM_METRICS_UNSUPPORTED",
            "Автоматические метрики Telegram недоступны в текущей интеграции.",
        )


class VKMetricsProvider:
    async def get_metrics(self, *, external_id: str) -> ProviderMetricsResult:
        del external_id
        raise MetricsProviderError(
            "VK_METRICS_UNSUPPORTED",
            "Автоматические метрики VK недоступны для текущего токена сообщества.",
        )
