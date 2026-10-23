from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.core.config import settings
from app.integrations.vk_credentials import VKCredentialError, VKCredentialStore


class PublicationProviderError(Exception):
    def __init__(
        self, code: str, message: str, *, retryable: bool = False, ambiguous: bool = False
    ):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.ambiguous = ambiguous


class TelegramProviderError(PublicationProviderError):
    pass


class VKProviderError(PublicationProviderError):
    pass


@dataclass(frozen=True)
class ProviderPublicationResult:
    external_id: str
    external_url: str | None
    published_at: datetime
    provider_target_id: str | None = None


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
            target_id = str(result["chat"]["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise TelegramProviderError(
                "TELEGRAM_PROVIDER_ERROR", "Telegram returned an invalid response."
            ) from exc
        return ProviderPublicationResult(
            external_id=message_id,
            external_url=None,
            published_at=datetime.now(UTC),
            provider_target_id=target_id,
        )


class VKProvider:
    """VK wall.post adapter with refreshable, target-bound user credentials."""

    def __init__(
        self,
        token: str | None = None,
        credential_store: VKCredentialStore | None = None,
    ) -> None:
        self._token = token or settings.vk_access_token
        self._credential_store = credential_store
        if self._credential_store is None and not self._token and settings.vk_credentials_file:
            self._credential_store = VKCredentialStore(
                settings.vk_credentials_file,
                owner_id=settings.vk_owner_id or 0,
                token_endpoint=settings.vk_id_token_endpoint,
                refresh_skew_seconds=settings.vk_refresh_skew_seconds,
            )
        if (not self._token and self._credential_store is None) or settings.vk_owner_id is None:
            raise VKProviderError("VK_AUTH_ERROR", "VK publishing is not configured.")
        if settings.vk_owner_id >= 0:
            raise VKProviderError(
                "VK_TARGET_INVALID", "VK publishing target must be a community wall."
            )

    async def _access_token(self) -> str:
        if self._token:
            return self._token
        assert self._credential_store is not None
        try:
            return await self._credential_store.access_token()
        except VKCredentialError as exc:
            raise VKProviderError(
                exc.code,
                exc.safe_message,
                retryable=exc.retryable,
            ) from exc

    async def publish(self, *, text: str, chat_id: str) -> ProviderPublicationResult:
        if chat_id != str(settings.vk_owner_id):
            raise VKProviderError(
                "VK_TARGET_MISMATCH", "VK publishing target does not match configured owner."
            )
        if not text.strip() or len(text) > 4096:
            raise VKProviderError("VK_BAD_REQUEST", "VK message is invalid.")
        token = await self._access_token()
        try:
            async with httpx.AsyncClient(timeout=settings.vk_request_timeout_seconds) as client:
                response = await client.post(
                    "https://api.vk.com/method/wall.post",
                    # Keep credentials out of request URLs. httpx may log
                    # URLs at INFO level; form data is not included there.
                    data={
                        "owner_id": settings.vk_owner_id,
                        "from_group": 1,
                        "access_token": token,
                        "v": settings.vk_api_version,
                        "message": text,
                    },
                )
        except httpx.TimeoutException as exc:
            raise VKProviderError(
                "VK_PROVIDER_TIMEOUT", "VK request timed out.", ambiguous=True
            ) from exc
        except httpx.NetworkError as exc:
            raise VKProviderError(
                "VK_PROVIDER_ERROR",
                "VK request failed before delivery confirmation.",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise VKProviderError("VK_PROVIDER_ERROR", "VK request failed.") from exc
        try:
            payload: dict[str, Any] = response.json()
            if "error" in payload:
                error = payload["error"]
                code = int(error.get("error_code", 0))
                if code in {5, 27}:
                    raise VKProviderError("VK_AUTH_ERROR", "VK authentication failed.")
                if code in {7, 15}:
                    raise VKProviderError("VK_PERMISSION_ERROR", "VK access was denied.")
                if code in {6, 9}:
                    raise VKProviderError("VK_RATE_LIMIT", "VK rate limit reached.", retryable=True)
                raise VKProviderError("VK_BAD_REQUEST", "VK rejected the message.")
            post_id = str(payload["response"]["post_id"])
        except VKProviderError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise VKProviderError(
                "VK_PROVIDER_ERROR", "VK returned an invalid response.", ambiguous=True
            ) from exc
        return ProviderPublicationResult(
            external_id=post_id,
            external_url=None,
            published_at=datetime.now(UTC),
            provider_target_id=str(settings.vk_owner_id),
        )
