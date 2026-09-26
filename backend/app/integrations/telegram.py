from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.core.config import settings


class TelegramProviderError(Exception):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False, ambiguous: bool = False
    ):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class ProviderPublicationResult:
    external_id: str
    external_url: str | None
    published_at: datetime


class PublicationProvider(Protocol):
    async def publish(self, *, text: str, chat_id: str) -> ProviderPublicationResult: ...


class TelegramProvider:
    def __init__(self, token: str | None = None) -> None:
        token = token or settings.telegram_bot_token
        if not token:
            raise TelegramProviderError(
                "TELEGRAM_AUTH_ERROR", "Telegram publishing is not configured."
            )
        self._token = token

    async def publish(self, *, text: str, chat_id: str) -> ProviderPublicationResult:
        if not text.strip() or len(text) > 4096:
            raise TelegramProviderError("TELEGRAM_BAD_REQUEST", "Telegram message is invalid.")
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            async with httpx.AsyncClient(
                timeout=settings.telegram_request_timeout_seconds
            ) as client:
                response = await client.post(url, json={"chat_id": chat_id, "text": text})
        except httpx.TimeoutException as exc:
            raise TelegramProviderError(
                "TELEGRAM_PROVIDER_TIMEOUT", "Telegram request timed out.", ambiguous=True
            ) from exc
        except httpx.NetworkError as exc:
            raise TelegramProviderError(
                "TELEGRAM_PROVIDER_ERROR",
                "Telegram request failed before delivery confirmation.",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise TelegramProviderError(
                "TELEGRAM_PROVIDER_ERROR", "Telegram request failed."
            ) from exc
        if response.status_code == 401:
            raise TelegramProviderError("TELEGRAM_AUTH_ERROR", "Telegram authentication failed.")
        if response.status_code in {403}:
            raise TelegramProviderError("TELEGRAM_PERMISSION_ERROR", "Telegram access was denied.")
        if response.status_code == 429:
            raise TelegramProviderError(
                "TELEGRAM_RATE_LIMIT", "Telegram rate limit reached.", retryable=True
            )
        if response.status_code >= 400:
            raise TelegramProviderError("TELEGRAM_BAD_REQUEST", "Telegram rejected the message.")
        try:
            payload: dict[str, Any] = response.json()
            result = payload["result"]
            message_id = str(result["message_id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise TelegramProviderError(
                "TELEGRAM_PROVIDER_ERROR", "Telegram returned an invalid response."
            ) from exc
        return ProviderPublicationResult(
            external_id=message_id,
            external_url=None,
            published_at=datetime.now(UTC),
        )
