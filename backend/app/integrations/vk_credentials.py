from __future__ import annotations

import asyncio
import fcntl
import json
import os
import secrets
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

WALL_PERMISSION = 8192
GROUPS_PERMISSION = 262144
REQUIRED_PERMISSION_MASK = WALL_PERMISSION | GROUPS_PERMISSION
ALLOWED_TOKEN_HOSTS = {"id.vk.ru", "id.vk.com"}


class VKCredentialError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class VKCredentialState:
    client_id: int
    redirect_uri: str
    device_id: str
    user_id: int
    owner_id: int
    scope: str
    access_token: str
    refresh_token: str
    expires_at: datetime

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VKCredentialState:
        try:
            expires_at = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
            state = cls(
                client_id=int(payload["client_id"]),
                redirect_uri=str(payload["redirect_uri"]),
                device_id=str(payload["device_id"]),
                user_id=int(payload["user_id"]),
                owner_id=int(payload["owner_id"]),
                scope=str(payload["scope"]),
                access_token=str(payload["access_token"]),
                refresh_token=str(payload["refresh_token"]),
                expires_at=expires_at,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID",
                "VK publishing credentials are incomplete or invalid.",
            ) from exc
        state.validate()
        return state

    def validate(self) -> None:
        if self.client_id <= 0 or self.user_id <= 0 or self.owner_id >= 0:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK publishing credentials contain invalid ids."
            )
        if not self.device_id or not self.access_token or not self.refresh_token:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK publishing credentials are incomplete."
            )
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential expiry must contain a timezone."
            )
        redirect = urlparse(self.redirect_uri)
        if redirect.scheme not in {"https", "http"} or not redirect.netloc:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK redirect URI is invalid."
            )
        if redirect.scheme == "http" and redirect.hostname not in {"localhost", "127.0.0.1"}:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK redirect URI must use HTTPS."
            )
        if not scope_allows_publishing(self.scope):
            raise VKCredentialError(
                "VK_PERMISSION_ERROR", "VK token lacks wall or groups permissions."
            )

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "device_id": self.device_id,
            "user_id": self.user_id,
            "owner_id": self.owner_id,
            "scope": self.scope,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
        }


def scope_allows_publishing(scope: str) -> bool:
    normalized = scope.replace(",", " ").strip()
    if normalized.isdigit():
        return int(normalized) & REQUIRED_PERMISSION_MASK == REQUIRED_PERMISSION_MASK
    names = {item for item in normalized.lower().split() if item}
    return {"wall", "groups"}.issubset(names)


class VKCredentialStore:
    def __init__(
        self,
        path: str | Path,
        *,
        owner_id: int,
        token_endpoint: str,
        refresh_skew_seconds: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.path = Path(path)
        self.owner_id = owner_id
        self.token_endpoint = token_endpoint
        self.refresh_skew = timedelta(seconds=refresh_skew_seconds)
        self.transport = transport
        parsed = urlparse(token_endpoint)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_TOKEN_HOSTS:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK ID token endpoint is not allowed."
            )

    @asynccontextmanager
    async def _locked(self) -> AsyncIterator[None]:
        parent = self.path.parent
        if not parent.is_dir() or parent.is_symlink():
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential directory is unavailable."
            )
        lock_path = parent / ".credentials.lock"
        lock_file = await asyncio.to_thread(open, lock_path, "a+", encoding="utf-8")
        try:
            await asyncio.to_thread(os.chmod, lock_path, stat.S_IRUSR | stat.S_IWUSR)
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
            await asyncio.to_thread(lock_file.close)

    def _read(self) -> VKCredentialState:
        if self.path.is_symlink() or not self.path.is_file():
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential file is unavailable."
            )
        mode = stat.S_IMODE(self.path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise VKCredentialError(
                "VK_CREDENTIALS_PERMISSIONS",
                "VK credential file permissions must be 0600.",
            )
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential file cannot be read."
            ) from exc
        if not isinstance(payload, dict):
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential file has an invalid format."
            )
        state = VKCredentialState.from_payload(payload)
        if state.owner_id != self.owner_id:
            raise VKCredentialError(
                "VK_TARGET_MISMATCH",
                "VK credential target does not match configured owner.",
            )
        return state

    def _write(self, state: VKCredentialState) -> None:
        state.validate()
        parent = self.path.parent
        data = json.dumps(state.as_payload(), ensure_ascii=False, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(prefix=".credentials.", dir=parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()

    async def access_token(self) -> str:
        async with self._locked():
            current = await asyncio.to_thread(self._read)
            if current.expires_at.astimezone(UTC) > datetime.now(UTC) + self.refresh_skew:
                return current.access_token
            refreshed = await self._refresh(current)
            await asyncio.to_thread(self._write, refreshed)
            return refreshed.access_token

    async def _refresh(self, current: VKCredentialState) -> VKCredentialState:
        request_state = secrets.token_urlsafe(32)
        params = {
            "grant_type": "refresh_token",
            "redirect_uri": current.redirect_uri,
            "client_id": current.client_id,
            "device_id": current.device_id,
            "state": request_state,
        }
        try:
            async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                response = await client.post(
                    self.token_endpoint,
                    params=params,
                    data={"refresh_token": current.refresh_token},
                )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError) as exc:
            raise VKCredentialError(
                "VK_TOKEN_REFRESH_ERROR",
                "VK ID token refresh failed before publication.",
            ) from exc
        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise VKCredentialError(
                "VK_TOKEN_REFRESH_ERROR", "VK ID returned an invalid token response."
            ) from exc
        if response.status_code >= 400 or "error" in payload:
            raise VKCredentialError(
                "VK_TOKEN_REFRESH_ERROR",
                "VK ID rejected the refresh token; repeat user authorization.",
            )
        try:
            response_state = str(payload["state"])
            user_id = int(payload["user_id"])
            access_token = str(payload["access_token"])
            refresh_token = str(payload["refresh_token"])
            expires_in = int(payload["expires_in"])
            scope = str(payload["scope"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VKCredentialError(
                "VK_TOKEN_REFRESH_ERROR", "VK ID returned an incomplete token response."
            ) from exc
        if response_state != request_state or user_id != current.user_id:
            raise VKCredentialError(
                "VK_TOKEN_REFRESH_ERROR", "VK ID token identity validation failed."
            )
        if not access_token or not refresh_token or expires_in <= 0:
            raise VKCredentialError(
                "VK_TOKEN_REFRESH_ERROR", "VK ID returned unusable credentials."
            )
        if not scope_allows_publishing(scope):
            raise VKCredentialError(
                "VK_PERMISSION_ERROR", "Refreshed VK token lacks required permissions."
            )
        return replace(
            current,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            scope=scope,
        )
