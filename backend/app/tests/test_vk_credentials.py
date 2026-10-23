import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import settings
from app.integrations.telegram import VKProvider, VKProviderError
from app.integrations.vk_credentials import (
    REQUIRED_PERMISSION_MASK,
    VKCredentialError,
    VKCredentialStore,
    scope_allows_publishing,
)


def write_credentials(path: Path, *, owner_id: int = -150574411, expired: bool = True) -> None:
    path.parent.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "client_id": 123456,
        "redirect_uri": "https://example.com/vk/callback",
        "device_id": "device-1",
        "user_id": 176199961,
        "owner_id": owner_id,
        "scope": "wall groups",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": (
            datetime.now(UTC) + (timedelta(seconds=-1) if expired else timedelta(hours=1))
        ).isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def successful_refresh(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "id_token": "id-token",
            "state": request.url.params["state"],
            "token_type": "Bearer",
            "user_id": 176199961,
            "scope": "wall groups",
        },
    )


async def test_expired_vk_credentials_are_refreshed_and_rotated_atomically(
    tmp_path: Path,
) -> None:
    path = tmp_path / "vk" / "credentials.json"
    write_credentials(path)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.host == "id.vk.ru"
        assert request.url.params["grant_type"] == "refresh_token"
        assert request.url.params["client_id"] == "123456"
        assert request.url.params["device_id"] == "device-1"
        assert parse_qs(request.content.decode())["refresh_token"] == ["old-refresh"]
        return successful_refresh(request)

    store = VKCredentialStore(
        path,
        owner_id=-150574411,
        token_endpoint="https://id.vk.ru/oauth2/auth",
        refresh_skew_seconds=120,
        transport=httpx.MockTransport(handler),
    )

    assert await store.access_token() == "new-access"
    assert requests == 1
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["access_token"] == "new-access"
    assert persisted["refresh_token"] == "new-refresh"
    assert persisted["owner_id"] == -150574411
    assert path.stat().st_mode & 0o777 == 0o600


async def test_fresh_vk_credentials_do_not_call_refresh_endpoint(tmp_path: Path) -> None:
    path = tmp_path / "vk" / "credentials.json"
    write_credentials(path, expired=False)

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("refresh endpoint must not be called")

    store = VKCredentialStore(
        path,
        owner_id=-150574411,
        token_endpoint="https://id.vk.ru/oauth2/auth",
        refresh_skew_seconds=120,
        transport=httpx.MockTransport(handler),
    )

    assert await store.access_token() == "old-access"


async def test_vk_refresh_state_mismatch_does_not_replace_credentials(tmp_path: Path) -> None:
    path = tmp_path / "vk" / "credentials.json"
    write_credentials(path)
    before = path.read_text(encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "id_token": "id-token",
                "state": "wrong-state",
                "token_type": "Bearer",
                "user_id": 176199961,
                "scope": "wall groups",
            },
        )

    store = VKCredentialStore(
        path,
        owner_id=-150574411,
        token_endpoint="https://id.vk.ru/oauth2/auth",
        refresh_skew_seconds=120,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(VKCredentialError) as mismatch:
        await store.access_token()
    assert mismatch.value.code == "VK_TOKEN_REFRESH_ERROR"
    assert path.read_text(encoding="utf-8") == before


async def test_vk_refresh_does_not_return_new_token_when_persistence_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "vk" / "credentials.json"
    write_credentials(path)
    before = path.read_text(encoding="utf-8")

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated volume failure")

    monkeypatch.setattr("app.integrations.vk_credentials.os.replace", fail_replace)
    store = VKCredentialStore(
        path,
        owner_id=-150574411,
        token_endpoint="https://id.vk.ru/oauth2/auth",
        refresh_skew_seconds=120,
        transport=httpx.MockTransport(successful_refresh),
    )

    with pytest.raises(VKCredentialError) as failure:
        await store.access_token()
    assert failure.value.code == "VK_CREDENTIALS_IO_ERROR"
    assert failure.value.retryable is True
    assert path.read_text(encoding="utf-8") == before


async def test_vk_credentials_require_exact_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "vk" / "credentials.json"
    write_credentials(path, expired=False)
    os.chmod(path, 0o640)
    store = VKCredentialStore(
        path,
        owner_id=-150574411,
        token_endpoint="https://id.vk.ru/oauth2/auth",
        refresh_skew_seconds=120,
    )

    with pytest.raises(VKCredentialError) as permissions:
        await store.access_token()
    assert permissions.value.code == "VK_CREDENTIALS_PERMISSIONS"


async def test_vk_credentials_reject_unknown_schema(tmp_path: Path) -> None:
    path = tmp_path / "vk" / "credentials.json"
    write_credentials(path, expired=False)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    store = VKCredentialStore(
        path,
        owner_id=-150574411,
        token_endpoint="https://id.vk.ru/oauth2/auth",
        refresh_skew_seconds=120,
    )

    with pytest.raises(VKCredentialError) as schema:
        await store.access_token()
    assert schema.value.code == "VK_CREDENTIALS_INVALID"


async def test_vk_credentials_are_bound_to_exact_community_owner(tmp_path: Path) -> None:
    path = tmp_path / "vk" / "credentials.json"
    write_credentials(path, owner_id=-150574411, expired=False)
    store = VKCredentialStore(
        path,
        owner_id=-999,
        token_endpoint="https://id.vk.ru/oauth2/auth",
        refresh_skew_seconds=120,
    )

    with pytest.raises(VKCredentialError) as mismatch:
        await store.access_token()
    assert mismatch.value.code == "VK_TARGET_MISMATCH"


async def test_vk_provider_rejects_target_before_reading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Store:
        calls = 0

        async def access_token(self) -> str:
            self.calls += 1
            return "token"

    store = Store()
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    provider = VKProvider(credential_store=store)  # type: ignore[arg-type]

    with pytest.raises(VKProviderError) as mismatch:
        await provider.publish(text="Post", chat_id="-999")
    assert mismatch.value.code == "VK_TARGET_MISMATCH"
    assert store.calls == 0


def test_vk_scope_parser_accepts_names_and_exact_permission_mask() -> None:
    assert scope_allows_publishing("wall groups") is True
    assert scope_allows_publishing(str(REQUIRED_PERMISSION_MASK)) is True
    assert scope_allows_publishing("wall") is False
