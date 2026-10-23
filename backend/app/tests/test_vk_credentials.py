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
                        "state": data["state"],
                        "user_id": 176199961,
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


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("client_id", 1, "VK_CREDENTIALS_APP_MISMATCH"),
        ("user_id", 1, "VK_CREDENTIALS_USER_MISMATCH"),
        ("scope", "wall", "VK_CREDENTIALS_SCOPE_MISSING"),
        ("scope", "groups", "VK_CREDENTIALS_SCOPE_MISSING"),
        ("schema_version", 2, "VK_CREDENTIALS_INVALID"),
    ],
)
async def test_invalid_stored_credential_blocks_before_network(
    tmp_path, monkeypatch, field, value, code
):
    from dataclasses import replace

    path = tmp_path / "credentials.json"
    write(path, replace(snapshot(), **{field: value}))

    def never_client(**kwargs):
        pytest.fail("invalid credential must not reach the network")

    monkeypatch.setattr("app.integrations.vk_credentials.httpx.AsyncClient", never_client)
    with pytest.raises(VKCredentialError) as error:
        await store(path).access_token()
    assert error.value.code == code


def test_wrong_file_owner_and_symlink_rejected(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    write(path, snapshot())
    credential_store = store(path)
    credential_store.owner_uid = os.geteuid() + 1
    with pytest.raises(VKCredentialError, match="permissions"):
        credential_store.safe_status()
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(VKCredentialError):
        store(link).safe_status()


@pytest.mark.parametrize(
    "patch",
    [
        {"state": None},
        {"state": "wrong"},
        {"user_id": 1},
        {"scope": "wall"},
        {"scope": None},
        {"refresh_token": None},
        {"access_token": None},
        {"expires_in": 0},
    ],
)
async def test_bad_refresh_response_never_persisted(tmp_path, monkeypatch, patch):
    import httpx

    path = tmp_path / "credentials.json"
    write(path, snapshot(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    original = path.read_bytes()
    calls = []

    def handler(request):
        from urllib.parse import parse_qs

        calls.append(request.url.path)
        assert request.url.path.endswith("oauth2/auth")
        values = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "new",
                "refresh_token": "rotated",
                "expires_in": 3600,
                "state": values["state"][0],
                "user_id": 176199961,
                "scope": "wall groups",
                **patch,
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.integrations.vk_credentials.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(VKCredentialError):
        await store(path).access_token()
    assert path.read_bytes() == original
    assert len(calls) == 1


async def test_persistence_failure_prevents_provider_write(tmp_path, monkeypatch):
    from dataclasses import replace

    from app.core.config import settings
    from app.integrations.telegram import VKProvider, VKProviderError

    path = tmp_path / "credentials.json"
    write(path, snapshot(expires_at=datetime.now(UTC) - timedelta(minutes=1)))
    credential_store = store(path)

    async def refresh(current):
        return replace(current, access_token="new", refresh_token="rotated")

    def fail_write(*args, **kwargs):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(credential_store, "_refresh", refresh)
    monkeypatch.setattr(credential_store, "_write_unlocked", fail_write)
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    monkeypatch.setattr(settings, "vk_access_token", None)
    provider = VKProvider(credential_store=credential_store)

    async def no_send(**kwargs):
        pytest.fail("wall.post must not run before durable credential rotation")

    monkeypatch.setattr(provider, "_send", no_send)
    with pytest.raises(VKProviderError):
        await provider.publish(text="canary", chat_id="-150574411")


@pytest.mark.parametrize("environment", ["prod", "production"])
def test_production_provider_rejects_static_token(monkeypatch, environment):
    from app.core.config import settings
    from app.integrations.telegram import VKProvider, VKProviderError

    monkeypatch.setattr(settings, "app_env", environment)
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    with pytest.raises(VKProviderError, match="Static"):
        VKProvider(token="synthetic-static-token")


@pytest.mark.parametrize("code,subcode", [(7, None), (15, 1133), (15, 1134), (100, 999)])
def test_vk_error_diagnostics_allow_only_numeric_codes(code, subcode):
    from app.integrations.telegram import VKProvider

    secret = "sentinel-access-refresh-callback-password"
    raw = {
        "error_code": code,
        "error_subcode": subcode,
        "error_msg": secret,
        "request_params": [{"key": "access_token", "value": secret}],
        "access_token": secret,
        "refresh_token": secret,
        "redirect_uri": secret,
    }
    result = VKProvider._error({"error": raw})
    assert result is not None
    numeric_code, error = result
    assert numeric_code == code
    expected = {"vk_error_code": code}
    if subcode is not None:
        expected["vk_error_subcode"] = subcode
    assert error.safe_diagnostics() == expected
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in json.dumps(vars(error))
    if code in {7, 15}:
        assert not error.retryable and not error.ambiguous


@pytest.mark.parametrize("invalid", ["sentinel-secret", "15", True, -1, 2**80, {}, [], None])
def test_vk_error_diagnostics_reject_non_integer_or_unbounded_values(invalid):
    from app.integrations.telegram import VKProvider

    result = VKProvider._error({"error": {"error_code": invalid, "error_subcode": invalid}})
    assert result is not None
    code, error = result
    assert code == 0
    assert error.code == "VK_BAD_REQUEST"
    assert error.safe_diagnostics() == {}


@pytest.mark.parametrize("blank", [None, "", " \t "])
@pytest.mark.parametrize("expired", [False, True])
async def test_production_blank_static_token_selects_store_and_refreshes(
    tmp_path, monkeypatch, blank, expired
):
    from urllib.parse import parse_qs

    import httpx

    from app.core.config import settings
    from app.integrations.telegram import VKProvider

    path = tmp_path / "credentials.json"
    write(path, snapshot(expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1)))
    original_inode = path.stat().st_ino
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "vk_access_token", blank)
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    monkeypatch.setattr(settings, "vk_client_id", 54815172)
    monkeypatch.setattr(settings, "vk_expected_user_id", 176199961)
    monkeypatch.setattr(settings, "vk_credentials_file", str(path))
    methods = []

    def handler(request):
        data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        if method == "auth":
            assert expired and data["refresh_token"] == "old-refresh-token"
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 3600,
                    "scope": "wall groups",
                    "state": data["state"],
                    "user_id": 176199961,
                },
            )
        expected = "new-access-token" if expired else "old-access-token"
        assert data["access_token"] == expected
        if method == "account.getAppPermissions":
            return httpx.Response(200, json={"response": 270336})
        if method == "users.get":
            return httpx.Response(200, json={"response": [{"id": 176199961}]})
        if method == "wall.get":
            return httpx.Response(200, json={"response": {"count": 0, "items": []}})
        assert method == "wall.post"
        assert data["owner_id"] == "-150574411"
        # Rotation must be durable before the adapter uses the refreshed token.
        saved = json.loads(path.read_text())
        assert saved["access_token"] == expected
        if expired:
            assert saved["refresh_token"] == "new-refresh-token"
            assert path.stat().st_ino != original_inode
        return httpx.Response(200, json={"response": {"post_id": 123}})

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.integrations.telegram.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    provider = VKProvider(token=blank, single_attempt=True)
    assert isinstance(provider._credential_store, VKCredentialStore)
    assert provider._token is None
    result = await provider.publish(text="mocked only", chat_id="-150574411")
    assert result.external_id == "123"
    assert methods == (
        ["auth", "account.getAppPermissions", "users.get", "wall.get", "wall.post"]
        if expired
        else ["wall.post"]
    )
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("blank", [None, "", " \t "])
def test_blank_token_without_store_fails_closed(monkeypatch, blank):
    from app.core.config import settings
    from app.integrations.telegram import VKProvider, VKProviderError

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "vk_access_token", blank)
    monkeypatch.setattr(settings, "vk_credentials_file", None)
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    with pytest.raises(VKProviderError, match="not configured"):
        VKProvider(token=blank)


@pytest.mark.parametrize("value", ["", " \t "])
async def test_empty_store_result_blocks_provider_request(monkeypatch, value):
    from app.core.config import settings
    from app.integrations.telegram import VKProvider, VKProviderError

    monkeypatch.setattr(settings, "vk_access_token", "")
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)

    class Store:
        async def access_token(self, **kwargs):
            return value

    provider = VKProvider(credential_store=Store())

    async def never_send(**kwargs):
        pytest.fail("empty credential must not reach wall.post")

    monkeypatch.setattr(provider, "_send", never_send)
    with pytest.raises(VKProviderError) as error:
        await provider.publish(text="mocked only", chat_id="-150574411")
    assert error.value.code == "VK_AUTH_ERROR"


@pytest.mark.parametrize("source", ["explicit", "configured"])
@pytest.mark.parametrize("environment", ["prod", "production"])
def test_nonblank_static_token_stays_forbidden(monkeypatch, source, environment):
    from app.core.config import settings
    from app.integrations.telegram import VKProvider, VKProviderError

    monkeypatch.setattr(settings, "app_env", environment)
    monkeypatch.setattr(settings, "vk_owner_id", -150574411)
    monkeypatch.setattr(
        settings, "vk_access_token", "sentinel-secret" if source == "configured" else ""
    )
    with pytest.raises(VKProviderError) as error:
        VKProvider(token="sentinel-secret" if source == "explicit" else "")
    assert error.value.code == "VK_AUTH_ERROR"
    assert "sentinel-secret" not in str(error.value)


async def test_blank_production_token_refresh_rejection_never_posts(tmp_path, monkeypatch):
    import httpx

    from app.core.config import settings
    from app.integrations.telegram import VKProvider, VKProviderError

    path = tmp_path / "credentials.json"
    write(path, snapshot(expires_at=datetime.now(UTC) - timedelta(hours=1)))
    original = path.read_bytes()
    for name, value in {
        "app_env": "production",
        "vk_access_token": "",
        "vk_owner_id": -150574411,
        "vk_client_id": 54815172,
        "vk_expected_user_id": 176199961,
        "vk_credentials_file": str(path),
    }.items():
        monkeypatch.setattr(settings, name, value)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        assert request.url.path.endswith("/oauth2/auth")
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "sentinel-secret"}
        )

    original_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.integrations.telegram.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    with pytest.raises(VKProviderError) as error:
        await VKProvider(single_attempt=True).publish(text="mocked only", chat_id="-150574411")
    assert error.value.code == "VK_AUTH_ERROR"
    assert "sentinel-secret" not in str(error.value)
    assert len(calls) == 1 and path.read_bytes() == original
