from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

VK_ID_TOKEN_URL = "https://id.vk.ru/oauth2/auth"
VK_API_BASE_URL = "https://api.vk.com/method"
VK_REQUIRED_SCOPE = "wall groups"
VK_WALL_PERMISSION = 8192
VK_GROUPS_PERMISSION = 262144
VK_REQUIRED_PERMISSIONS = VK_WALL_PERMISSION | VK_GROUPS_PERMISSION


class VKCredentialError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class VKCredentialSnapshot:
    schema_version: int
    client_id: int
    user_id: int
    owner_id: int
    scope: str
    permissions_mask: int
    access_token: str
    refresh_token: str
    device_id: str
    expires_at: str
    updated_at: str

    @property
    def expires_at_datetime(self) -> datetime:
        try:
            value = datetime.fromisoformat(self.expires_at)
        except ValueError as error:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential expiry is invalid."
            ) from error
        if value.tzinfo is None or value.utcoffset() is None:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential expiry must include timezone."
            )
        return value.astimezone(UTC)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> VKCredentialSnapshot:
        try:
            snapshot = cls(
                schema_version=int(payload["schema_version"]),
                client_id=int(payload["client_id"]),
                user_id=int(payload["user_id"]),
                owner_id=int(payload["owner_id"]),
                scope=str(payload["scope"]),
                permissions_mask=int(payload["permissions_mask"]),
                access_token=str(payload["access_token"]),
                refresh_token=str(payload["refresh_token"]),
                device_id=str(payload["device_id"]),
                expires_at=str(payload["expires_at"]),
                updated_at=str(payload["updated_at"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential file is incomplete."
            ) from error
        if snapshot.schema_version != 1:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential schema is unsupported."
            )
        if not snapshot.access_token or not snapshot.refresh_token or not snapshot.device_id:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential values are missing."
            )
        snapshot.expires_at_datetime
        return snapshot


class VKCredentialStore:
    """Process-safe rotating VK ID credential file for the publication worker only."""

    def __init__(
        self,
        path: str,
        *,
        client_id: int,
        expected_user_id: int,
        owner_id: int,
        api_version: str,
        request_timeout_seconds: int,
        refresh_leeway_seconds: int,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")
        self.client_id = client_id
        self.expected_user_id = expected_user_id
        self.owner_id = owner_id
        self.api_version = api_version
        self.request_timeout_seconds = request_timeout_seconds
        self.refresh_leeway_seconds = refresh_leeway_seconds

    def _ensure_parent(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)

    def _open_lock(self) -> Any:
        self._ensure_parent()
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(self.lock_path, 0o600)
        return os.fdopen(descriptor, "r+")

    def _read_unlocked(self) -> VKCredentialSnapshot:
        try:
            mode = stat.S_IMODE(self.path.stat().st_mode)
            if mode & 0o077:
                raise VKCredentialError(
                    "VK_CREDENTIALS_PERMISSIONS", "VK credential file permissions are too broad."
                )
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise VKCredentialError(
                "VK_CREDENTIALS_MISSING", "VK publishing credential is not provisioned."
            ) from error
        except json.JSONDecodeError as error:
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential file is invalid."
            ) from error
        if not isinstance(payload, dict):
            raise VKCredentialError(
                "VK_CREDENTIALS_INVALID", "VK credential file is invalid."
            )
        snapshot = VKCredentialSnapshot.from_payload(payload)
        self._validate_binding(snapshot)
        return snapshot

    def _validate_binding(self, snapshot: VKCredentialSnapshot) -> None:
        if snapshot.client_id != self.client_id:
            raise VKCredentialError(
                "VK_CREDENTIALS_APP_MISMATCH", "VK credential belongs to another application."
            )
        if snapshot.user_id != self.expected_user_id:
            raise VKCredentialError(
                "VK_CREDENTIALS_USER_MISMATCH", "VK credential belongs to another user."
            )
        if snapshot.owner_id != self.owner_id or snapshot.owner_id >= 0:
            raise VKCredentialError(
                "VK_CREDENTIALS_TARGET_MISMATCH", "VK credential target does not match community."
            )
        if snapshot.permissions_mask & VK_REQUIRED_PERMISSIONS != VK_REQUIRED_PERMISSIONS:
            raise VKCredentialError(
                "VK_CREDENTIALS_SCOPE_MISSING", "VK credential lacks wall/groups access."
            )

    def _write_unlocked(self, snapshot: VKCredentialSnapshot, *, owner_uid: int | None) -> None:
        self._ensure_parent()
        content = json.dumps(asdict(snapshot), ensure_ascii=False, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            if owner_uid is not None and os.geteuid() == 0:
                os.chown(temp_path, owner_uid, owner_uid)
                os.chown(self.path.parent, owner_uid, owner_uid)
                if self.lock_path.exists():
                    os.chown(self.lock_path, owner_uid, owner_uid)
            os.replace(temp_path, self.path)
            directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temp_path.unlink(missing_ok=True)

    def provision(self, snapshot: VKCredentialSnapshot, *, owner_uid: int = 10001) -> None:
        self._validate_binding(snapshot)
        with self._open_lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._write_unlocked(snapshot, owner_uid=owner_uid)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def safe_status(self) -> dict[str, Any]:
        with self._open_lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            snapshot = self._read_unlocked()
            status = {
                "client_id": snapshot.client_id,
                "user_id": snapshot.user_id,
                "owner_id": snapshot.owner_id,
                "permissions_mask": snapshot.permissions_mask,
                "expires_at": snapshot.expires_at,
                "expired": snapshot.expires_at_datetime <= datetime.now(UTC),
                "refresh_token_present": bool(snapshot.refresh_token),
            }
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return status

    async def access_token(self, *, force_refresh: bool = False) -> str:
        with self._open_lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            snapshot = self._read_unlocked()
            refresh_after = datetime.now(UTC) + timedelta(seconds=self.refresh_leeway_seconds)
            if not force_refresh and snapshot.expires_at_datetime > refresh_after:
                token = snapshot.access_token
            else:
                refreshed = await self._refresh(snapshot)
                self._write_unlocked(refreshed, owner_uid=None)
                token = refreshed.access_token
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return token

    async def _refresh(self, snapshot: VKCredentialSnapshot) -> VKCredentialSnapshot:
        state = secrets.token_urlsafe(32)
        try:
            async with httpx.AsyncClient(timeout=self.request_timeout_seconds) as client:
                response = await client.post(
                    VK_ID_TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": snapshot.refresh_token,
                        "client_id": str(self.client_id),
                        "device_id": snapshot.device_id,
                        "state": state,
                        "scope": VK_REQUIRED_SCOPE,
                    },
                )
                payload = self._json_payload(response)
                if response.status_code >= 400 or payload.get("error"):
                    raise VKCredentialError(
                        "VK_AUTH_ERROR", "VK token refresh was rejected."
                    )
                access_token = str(payload.get("access_token") or "")
                refresh_token = str(payload.get("refresh_token") or snapshot.refresh_token)
                expires_in = int(payload.get("expires_in") or 0)
                if not access_token or not refresh_token or expires_in <= 0:
                    raise VKCredentialError(
                        "VK_AUTH_ERROR", "VK token refresh returned an invalid response."
                    )
                permissions = await self._app_permissions(client, access_token)
                if permissions & VK_REQUIRED_PERMISSIONS != VK_REQUIRED_PERMISSIONS:
                    raise VKCredentialError(
                        "VK_CREDENTIALS_SCOPE_MISSING", "Refreshed VK token lacks wall/groups access."
                    )
                user_id = await self._user_id(client, access_token)
                if user_id != self.expected_user_id:
                    raise VKCredentialError(
                        "VK_CREDENTIALS_USER_MISMATCH", "Refreshed VK token belongs to another user."
                    )
                await self._read_wall(client, access_token)
        except VKCredentialError:
            raise
        except httpx.TimeoutException as error:
            raise VKCredentialError(
                "VK_AUTH_REFRESH_TIMEOUT", "VK token refresh timed out.", retryable=True
            ) from error
        except httpx.NetworkError as error:
            raise VKCredentialError(
                "VK_AUTH_REFRESH_ERROR", "VK token refresh failed.", retryable=True
            ) from error
        except (httpx.HTTPError, TypeError, ValueError) as error:
            raise VKCredentialError(
                "VK_AUTH_REFRESH_ERROR", "VK token refresh failed."
            ) from error
        now = datetime.now(UTC)
        return replace(
            snapshot,
            access_token=access_token,
            refresh_token=refresh_token,
            scope=str(payload.get("scope") or snapshot.scope or VK_REQUIRED_SCOPE),
            permissions_mask=permissions,
            expires_at=(now + timedelta(seconds=expires_in)).isoformat(),
            updated_at=now.isoformat(),
        )

    @staticmethod
    def _json_payload(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise VKCredentialError(
                "VK_AUTH_REFRESH_ERROR", "VK returned an invalid token response."
            ) from error
        if not isinstance(payload, dict):
            raise VKCredentialError(
                "VK_AUTH_REFRESH_ERROR", "VK returned an invalid token response."
            )
        return payload

    async def _vk_method(
        self,
        client: httpx.AsyncClient,
        method: str,
        access_token: str,
        **parameters: Any,
    ) -> Any:
        response = await client.post(
            f"{VK_API_BASE_URL}/{method}",
            data={
                **parameters,
                "access_token": access_token,
                "v": self.api_version,
            },
        )
        payload = self._json_payload(response)
        error = payload.get("error")
        if response.status_code >= 400 or error:
            code = int(error.get("error_code", 0)) if isinstance(error, dict) else 0
            if code in {5, 27}:
                raise VKCredentialError("VK_AUTH_ERROR", "VK authentication failed.")
            if code in {7, 15}:
                raise VKCredentialError("VK_PERMISSION_ERROR", "VK access was denied.")
            raise VKCredentialError("VK_AUTH_REFRESH_ERROR", "VK read preflight failed.")
        if "response" not in payload:
            raise VKCredentialError(
                "VK_AUTH_REFRESH_ERROR", "VK read preflight returned an invalid response."
            )
        return payload["response"]

    async def _app_permissions(self, client: httpx.AsyncClient, access_token: str) -> int:
        response = await self._vk_method(client, "account.getAppPermissions", access_token)
        return int(response)

    async def _user_id(self, client: httpx.AsyncClient, access_token: str) -> int:
        response = await self._vk_method(client, "users.get", access_token)
        if not isinstance(response, list) or not response:
            raise VKCredentialError(
                "VK_AUTH_REFRESH_ERROR", "VK user identity was not returned."
            )
        return int(response[0]["id"])

    async def _read_wall(self, client: httpx.AsyncClient, access_token: str) -> None:
        response = await self._vk_method(
            client,
            "wall.get",
            access_token,
            owner_id=str(self.owner_id),
            count="1",
        )
        if not isinstance(response, dict) or "count" not in response:
            raise VKCredentialError(
                "VK_AUTH_REFRESH_ERROR", "VK community wall was not readable."
            )
