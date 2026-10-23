from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.integrations.vk_credentials import (
    VKCredentialError,
    VKCredentialSnapshot,
    VKCredentialStore,
)


def snapshot(*, expires_at: datetime | None = None, owner_id: int = -150574411):
    now = datetime.now(UTC)
    return VKCredentialSnapshot(
        schema_version=1,
        client_id=54815172,
        user_id=176199961,
        owner_id=owner_id,
        scope="wall groups",
        permissions_mask=270336,
        access_token="old-access-token",
        refresh_token="old-refresh-token",
        device_id="device-id",
        expires_at=(expires_at or now + timedelta(hours=1)).isoformat(),
        updated_at=now.isoformat(),
    )


def store(path: Path) -> VKCredentialStore:
    return VKCredentialStore(
        str(path),
        client_id=54815172,
        expected_user_id=176199961,
        owner_id=-150574411,
        api_version="5.199",
        request_timeout_seconds=30,
        refresh_leeway_seconds=300,
    )


def write(path: Path, value: VKCredentialSnapshot, *, mode: int = 0o600) -> None:
    path.write_text(json.dumps(value.__dict__), encoding="utf-8")
    os.chmod(path, mode)


async def test_unexpired_credential_is_read_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credentials.json"
    write(path, snapshot())

    class NeverClient:
        def __init__(self, **_kwargs: Any):
            pytest.fail("network must not be used for an unexpired credential")

    monkeypatch.setattr("app.integrations.vk_credentials.httpx.AsyncClient", NeverClient)
    assert await store(path).access_token() == "old-access-token"


async def test_expired_credential_refreshes_read_only_and_rotates_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credentials.json"
    write(path, snapshot(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    calls: list[tuple[str, dict[str, Any]]] = []

    class Response:
        def __init__(self, payload: dict[str, Any], status_code: int = 200):
            self.payload = payload
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return self.payload

    class Client:
        def __init__(self, **kwargs: Any):
            assert kwargs["timeout"] == 30

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: Any):
            return None

        async def post(self, url: str, **kwargs: Any) -> Response:
            data = dict(kwargs["data"])
            calls.append((url, data))
            if url.endswith("/oauth2/auth"):
                return Response(
                    {
                        "access_token": "new-access-token",
                        "refresh_token": "new-refresh-token",
                        "expires_in": 3600,
                        "scope": "wall groups",
                    }
                )
            if url.endswith("account.getAppPermissions"):
                return Response({"response": 270336})
            if url.endswith("users.get"):
                return Response({"response": [{"id": 176199961}]})
            if url.endswith("wall.get"):
                return Response({"response": {"count": 835, "items": []}})
            raise AssertionError(url)

    monkeypatch.setattr("app.integrations.vk_credentials.httpx.AsyncClient", Client)
    assert await store(path).access_token() == "new-access-token"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["access_token"] == "new-access-token"
    assert persisted["refresh_token"] == "new-refresh-token"
    assert os.stat(path).st_mode & 0o077 == 0
    token_call = calls[0]
    assert "old-refresh-token" not in token_call[0]
    assert token_call[1]["refresh_token"] == "old-refresh-token"
    assert len(token_call[1]["state"]) >= 32
    assert [url.rsplit("/", 1)[-1] for url, _ in calls[1:]] == [
        "account.getAppPermissions",
        "users.get",
        "wall.get",
    ]


async def test_credential_binding_mismatch_blocks_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "credentials.json"
    write(path, snapshot(owner_id=-999))

    class NeverClient:
        def __init__(self, **_kwargs: Any):
            pytest.fail("network must not be used for a target mismatch")

    monkeypatch.setattr("app.integrations.vk_credentials.httpx.AsyncClient", NeverClient)
    with pytest.raises(VKCredentialError) as error:
        await store(path).access_token()
    assert error.value.code == "VK_CREDENTIALS_TARGET_MISMATCH"
    assert "old-access-token" not in error.value.safe_message


def test_credential_permissions_must_be_private(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    write(path, snapshot(), mode=0o644)
    with pytest.raises(VKCredentialError) as error:
        store(path).safe_status()
    assert error.value.code == "VK_CREDENTIALS_PERMISSIONS"


async def test_provider_target_mismatch_does_not_open_credential_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.config import settings
    from app.integrations.telegram import VKProvider, VKProviderError

    monkeypatch.setattr(settings, "vk_access_token", None)
    monkeypatch.setattr(settings, "vk_credentials_file", None)
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)

    class Store:
        async def access_token(self, *, force_refresh: bool = False) -> str:
            pytest.fail(f"credential store must not be opened: {force_refresh}")

    provider = VKProvider(credential_store=Store())  # type: ignore[arg-type]
    with pytest.raises(VKProviderError) as error:
        await provider.publish(text="Тест", chat_id="-999")
    assert error.value.code == "VK_TARGET_MISMATCH"
