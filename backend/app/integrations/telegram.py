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
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
        error_code: int | None = None,
        error_subcode: int | None = None,
    ):
        super().__init__(code, message, retryable=retryable, ambiguous=ambiguous)
        # Accept only bounded JSON integers, never provider text or request parameters.
        self.error_code = self._numeric_code(error_code)
        self.error_subcode = self._numeric_code(error_subcode)

    @staticmethod
    def _numeric_code(value: object) -> int | None:
        return value if type(value) is int and 0 <= value <= 2_147_483_647 else None

    def safe_diagnostics(self) -> dict[str, int]:
        return {
            key: value
            for key, value in (
                ("vk_error_code", self.error_code),
                ("vk_error_subcode", self.error_subcode),
            )
            if value is not None
        }


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
    """VK wall.post adapter with target binding and refreshable user credentials."""

    def __init__(
        self,
        token: str | None = None,
        *,
        credential_store: VKCredentialStore | None = None,
        single_attempt: bool = False,
    ) -> None:
        if settings.vk_owner_id is None:
            raise VKProviderError("VK_AUTH_ERROR", "VK publishing is not configured.")
        if settings.vk_owner_id >= 0:
            raise VKProviderError(
                "VK_TARGET_INVALID", "VK publishing target must be a community wall."
            )
        explicit_token = (token or "").strip() or None
        configured_token = (settings.vk_access_token or "").strip() or None
        if settings.app_env.lower() in {"production", "prod"} and (
            explicit_token or configured_token
        ):
            raise VKProviderError("VK_AUTH_ERROR", "Static VK token is forbidden in production.")
        self._single_attempt = single_attempt
        # Compose deliberately supplies an empty static token in production.
        # Treat blank values as absent so they cannot bypass the rotating store.
        self._token = explicit_token or configured_token
        self._credential_store = credential_store
        if self._token is None and self._credential_store is None and settings.vk_credentials_file:
            if settings.vk_client_id is None or settings.vk_expected_user_id is None:
                raise VKProviderError("VK_AUTH_ERROR", "VK publishing is not configured.")
            self._credential_store = VKCredentialStore(
                settings.vk_credentials_file,
                client_id=settings.vk_client_id,
                expected_user_id=settings.vk_expected_user_id,
                owner_id=settings.vk_owner_id,
                api_version=settings.vk_api_version,
                request_timeout_seconds=settings.vk_request_timeout_seconds,
                refresh_leeway_seconds=settings.vk_token_refresh_leeway_seconds,
            )
        if self._token is None and self._credential_store is None:
            raise VKProviderError("VK_AUTH_ERROR", "VK publishing is not configured.")

    async def _access_token(self, *, force_refresh: bool = False) -> str:
        if self._credential_store is None:
            if not self._token or not self._token.strip():
                raise VKProviderError("VK_AUTH_ERROR", "VK publishing is not configured.")
            return self._token
        try:
            access_token = await self._credential_store.access_token(force_refresh=force_refresh)
        except VKCredentialError as error:
            raise VKProviderError(
                error.code,
                error.safe_message,
                retryable=error.retryable,
            ) from error

        if not access_token or not access_token.strip():
            raise VKProviderError("VK_AUTH_ERROR", "VK publishing credential is empty.")
        return access_token

    async def _send(self, *, text: str, access_token: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=settings.vk_request_timeout_seconds) as client:
                response = await client.post(
                    "https://api.vk.com/method/wall.post",
                    # Credentials remain in form data, never in URLs or logs.
                    data={
                        "owner_id": settings.vk_owner_id,
                        "from_group": 1,
                        "access_token": access_token,
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
        except (ValueError, TypeError) as exc:
            raise VKProviderError(
                "VK_PROVIDER_ERROR", "VK returned an invalid response.", ambiguous=True
            ) from exc
        return payload

    @staticmethod
    def _error(payload: dict[str, Any]) -> tuple[int, VKProviderError] | None:
        raw_error = payload.get("error")
        if not isinstance(raw_error, dict):
            return None
        code = VKProviderError._numeric_code(raw_error.get("error_code")) or 0
        subcode = VKProviderError._numeric_code(raw_error.get("error_subcode"))
        if code in {5, 27}:
            label, message, retryable = "VK_AUTH_ERROR", "VK authentication failed.", False
        elif code in {7, 15}:
            label, message, retryable = "VK_PERMISSION_ERROR", "VK access was denied.", False
        elif code in {6, 9}:
            label, message, retryable = "VK_RATE_LIMIT", "VK rate limit reached.", True
        else:
            label, message, retryable = "VK_BAD_REQUEST", "VK rejected the message.", False
        return code, VKProviderError(
            label,
            message,
            retryable=retryable,
            error_code=VKProviderError._numeric_code(raw_error.get("error_code")),
            error_subcode=subcode,
        )

    async def publish(self, *, text: str, chat_id: str) -> ProviderPublicationResult:
        # Target validation is deliberately before credential access or any network call.
        if chat_id != str(settings.vk_owner_id):
            raise VKProviderError(
                "VK_TARGET_MISMATCH", "VK publishing target does not match configured owner."
            )
        if not text.strip() or len(text) > 4096:
            raise VKProviderError("VK_BAD_REQUEST", "VK message is invalid.")
        access_token = await self._access_token()
        payload = await self._send(text=text, access_token=access_token)
        error = self._error(payload)
        # Error 5/27 is a definitive pre-delivery auth failure. Refresh and retry once;
        # timeouts and ambiguous results are never retried automatically.
        if (
            error is not None
            and error[0] in {5, 27}
            and self._credential_store is not None
            and not self._single_attempt
        ):
            access_token = await self._access_token(force_refresh=True)
            payload = await self._send(text=text, access_token=access_token)
            error = self._error(payload)
        if error is not None:
            raise error[1]
        try:
            post_id = str(payload["response"]["post_id"])
        except (KeyError, TypeError) as exc:
            raise VKProviderError(
                "VK_PROVIDER_ERROR", "VK returned an invalid response.", ambiguous=True
            ) from exc
        return ProviderPublicationResult(
            external_id=post_id,
            external_url=f"https://vk.com/wall{settings.vk_owner_id}_{post_id}",
            published_at=datetime.now(UTC),
            provider_target_id=str(settings.vk_owner_id),
        )
