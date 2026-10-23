from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest

from app.maintenance.vk_publishing_credentials import ProvisioningError, _callback_values


def runtime_module():
    path = Path(__file__).resolve().parents[3] / "scripts/vk_publishing_runtime.py"
    spec = importlib.util.spec_from_file_location("vk_runtime_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "callback",
    [
        "http://localhost?state=wrong&code=secret&device_id=device",
        "http://localhost?code=secret&device_id=device",
    ],
)
def test_callback_state_required(callback):
    with pytest.raises(ProvisioningError, match="state"):
        _callback_values(callback, "expected")


def test_inventory_disabled_read_only_with_runtime_window(monkeypatch, capsys):
    runtime = runtime_module()
    config = {
        "VK_CLIENT_ID": "54815172",
        "VK_EXPECTED_USER_ID": "176199961",
        "VK_OWNER_ID": "-150574411",
        "VK_PUBLISHING_ENABLED": "false",
        "PUBLICATION_AUTO_DISPATCH_MAX_LATENESS_SECONDS": "17",
    }
    monkeypatch.setattr(runtime, "env_values", lambda: config)
    queries = []
    monkeypatch.setattr(runtime, "sql", lambda query: queries.append(query) or "0")
    runtime.inventory()
    assert "make_interval(secs => 17)" in queries[0]
    assert "content_version_id" in queries[0]
    assert "c.channel" in queries[0]
    assert all(query.startswith("SELECT") for query in queries)
    assert "INVENTORY: PASS" in capsys.readouterr().out
    config["VK_PUBLISHING_ENABLED"] = "true"
    with pytest.raises(runtime.RuntimeErrorSafe, match="disabled"):
        runtime.inventory()


def test_canary_requires_inventory_before_any_runtime_change(monkeypatch):
    runtime = runtime_module()
    monkeypatch.setattr(runtime, "ensure_release", lambda: "release")

    def reject():
        raise runtime.RuntimeErrorSafe("inventory rejected")

    monkeypatch.setattr(runtime, "inventory", reject)
    monkeypatch.setattr(runtime, "apply_runtime", lambda *_: pytest.fail("must not activate"))
    with pytest.raises(runtime.RuntimeErrorSafe, match="inventory rejected"):
        runtime.arm_canary(uuid4())


def test_inventory_sql_is_read_only_transaction(monkeypatch):
    runtime = runtime_module()
    monkeypatch.setattr(runtime, "postgres_identity", lambda: ("test", "test"))
    calls = []
    monkeypatch.setattr(runtime, "run", lambda *args, **kwargs: calls.append(args) or "0")
    runtime.sql("SELECT 1;")
    assert calls[0][-1] == "BEGIN READ ONLY; SELECT 1; COMMIT;"
    assert "ON_ERROR_STOP=1" in calls[0]


@pytest.mark.parametrize(
    "token_patch",
    [
        {},
        {"state": None},
        {"state": "wrong"},
        {"scope": "wall"},
        {"user_id": 1},
    ],
)
async def test_preflight_refreshes_before_provision(monkeypatch, token_patch):
    from app.integrations.vk_credentials import VKCredentialSnapshot
    from app.maintenance import vk_publishing_credentials as operator
    from app.tests.test_vk_credentials import snapshot

    initial = snapshot()
    calls = []

    class Store:
        client_id = 54815172
        expected_user_id = 176199961
        owner_id = -150574411

        async def _refresh(self, credential):
            calls.append("refresh")
            assert isinstance(credential, VKCredentialSnapshot)
            return credential

    monkeypatch.setattr(operator, "_store", Store)
    monkeypatch.setattr(operator, "_pkce", lambda: ("verifier", "challenge"))
    monkeypatch.setattr(operator.secrets, "token_urlsafe", lambda _: "expected")
    monkeypatch.setattr(operator.webbrowser, "open", lambda _: None)
    monkeypatch.setattr(
        operator.getpass,
        "getpass",
        lambda _: ("http://localhost?state=expected&code=secret&device_id=device"),
    )
    import httpx

    original_client = httpx.AsyncClient

    def handler(request):
        assert request.url.path.endswith("oauth2/auth")
        return httpx.Response(
            200,
            json={
                "access_token": initial.access_token,
                "refresh_token": initial.refresh_token,
                "expires_in": 3600,
                "state": "expected",
                "user_id": 176199961,
                "scope": "wall groups",
                **token_patch,
            },
        )

    monkeypatch.setattr(
        operator.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def method(client, name, token, **kwargs):
        return {
            "users.get": [{"id": 176199961}],
            "account.getAppPermissions": 270336,
            "groups.getById": [{"id": 150574411}],
            "groups.get": {"items": [{"id": 150574411, "admin_level": 3}]},
            "groups.getMembers": {"items": [{"id": 176199961, "role": "creator"}]},
            "wall.get": {"count": 1},
        }[name]

    monkeypatch.setattr(operator, "_vk_method", method)
    if token_patch:
        with pytest.raises(ProvisioningError):
            await operator._authorize()
        assert calls == []
    else:
        await operator._authorize()
        assert calls == ["refresh"]
