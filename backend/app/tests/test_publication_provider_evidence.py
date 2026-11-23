from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import settings
from app.integrations.telegram import (
    PublicationProviderError,
    TelegramProvider,
    VKProvider,
    VKProviderError,
)


def _transport(monkeypatch, *replies):
    calls = []

    def reply(request):
        index = len(calls)
        calls.append(request.method)
        value = replies[index]
        if isinstance(value, Exception):
            raise value
        return value

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(reply), **kw),
    )
    return calls


@pytest.fixture(params=["TELEGRAM", "VK"])
def provider(request, monkeypatch):
    if request.param == "TELEGRAM":
        return request.param, TelegramProvider(token="synthetic")
    monkeypatch.setattr(settings, "vk_owner_id", -123)
    return request.param, VKProvider(token="synthetic")


@pytest.mark.parametrize(
    ("failure", "suffix", "before_send"),
    [
        (httpx.ConnectError, "ERROR", True),
        (httpx.ConnectTimeout, "TIMEOUT", True),
        (httpx.PoolTimeout, "TIMEOUT", True),
        (httpx.ReadTimeout, "TIMEOUT", False),
        (httpx.WriteTimeout, "TIMEOUT", False),
        (httpx.ReadError, "ERROR", False),
        (httpx.WriteError, "ERROR", False),
        (httpx.RemoteProtocolError, "ERROR", False),
    ],
)
async def test_transport_error_tracks_whether_send_might_have_happened(
    provider, monkeypatch, failure, suffix, before_send
):
    name, adapter = provider
    calls = _transport(monkeypatch, failure("synthetic transport failure"))
    with pytest.raises(PublicationProviderError) as caught:
        await adapter.publish(text="test post", chat_id="-123")
    assert caught.value.code == f"{name}_PROVIDER_{suffix}"
    assert caught.value.retryable is before_send
    assert caught.value.ambiguous is not before_send
    assert calls == ["POST"]


@pytest.mark.parametrize("status", [302, 408, 500, 503])
async def test_unconfirmed_http_status_overrides_a_valid_looking_receipt(
    provider, monkeypatch, status
):
    name, adapter = provider
    payload = (
        {"ok": True, "result": {"chat": {"id": -123}, "message_id": 18}}
        if name == "TELEGRAM"
        else {"response": {"post_id": 18}}
    )
    calls = _transport(monkeypatch, httpx.Response(status, json=payload))
    with pytest.raises(PublicationProviderError) as caught:
        await adapter.publish(text="test post", chat_id="-123")
    assert caught.value.code == f"{name}_PROVIDER_ERROR"
    assert caught.value.ambiguous and not caught.value.retryable
    assert calls == ["POST"]


@pytest.mark.parametrize("value", [None, True, 0, -5, "7", {}, 2**63])
async def test_invalid_provider_id_is_not_a_receipt(provider, monkeypatch, value):
    name, adapter = provider
    payload = (
        {"ok": True, "result": {"chat": {"id": -123}, "message_id": value}}
        if name == "TELEGRAM"
        else {"response": {"post_id": value}}
    )
    calls = _transport(monkeypatch, httpx.Response(200, json=payload))
    with pytest.raises(PublicationProviderError) as caught:
        await adapter.publish(text="test post", chat_id="-123")
    assert caught.value.ambiguous and not caught.value.retryable
    assert calls == ["POST"]


async def test_truncated_response_after_send_remains_uncertain(provider, monkeypatch):
    _, adapter = provider
    calls = _transport(monkeypatch, httpx.Response(200, content=b'{"result":'))
    with pytest.raises(PublicationProviderError) as caught:
        await adapter.publish(text="test post", chat_id="-123")
    assert caught.value.ambiguous and not caught.value.retryable
    assert calls == ["POST"]


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "result": {"message_id": 18}},
        {"ok": True, "result": {"message_id": 18, "chat": {"id": 0}}},
        {"ok": True, "result": {"message_id": 18, "chat": {"id": -124}}},
        {"result": {"message_id": 18, "chat": {"id": -123}}},
        {"ok": False, "error_code": 429, "result": {"message_id": 18, "chat": {"id": -123}}},
    ],
)
async def test_telegram_requires_consistent_message_and_target(monkeypatch, payload):
    _transport(monkeypatch, httpx.Response(200, json=payload))
    with pytest.raises(PublicationProviderError) as caught:
        await TelegramProvider(token="synthetic").publish(text="test post", chat_id="-123")
    assert caught.value.ambiguous and not caught.value.retryable


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "result": {"message_id": 18, "chat": {"id": -123}}},
        {"ok": False, "error_code": 401},
        {},
    ],
)
async def test_telegram_conflicting_or_missing_refusal_is_ambiguous(monkeypatch, payload):
    calls = _transport(monkeypatch, httpx.Response(429, json=payload))
    with pytest.raises(PublicationProviderError) as caught:
        await TelegramProvider(token="synthetic").publish(text="test post", chat_id="-123")
    assert caught.value.code == "TELEGRAM_PROVIDER_ERROR"
    assert caught.value.ambiguous and not caught.value.retryable
    assert calls == ["POST"]


async def test_telegram_definite_error_envelope_keeps_rate_limit_classification(monkeypatch):
    _transport(monkeypatch, httpx.Response(200, json={"ok": False, "error_code": 429}))
    with pytest.raises(PublicationProviderError) as caught:
        await TelegramProvider(token="synthetic").publish(text="test post", chat_id="-123")
    assert caught.value.code == "TELEGRAM_RATE_LIMIT"
    assert caught.value.retryable and not caught.value.ambiguous


@pytest.mark.parametrize(
    ("responses", "refreshes", "error_code"),
    [
        ([httpx.Response(503, json={"error": {"error_code": 5}})], [False], "VK_PROVIDER_ERROR"),
        (
            [httpx.Response(200, json={"response": {"post_id": 3}, "error": {"error_code": 5}})],
            [False],
            "VK_PROVIDER_ERROR",
        ),
        ([httpx.Response(200, json={"error": {"error_code": 10}})], [False], "VK_BAD_REQUEST"),
        (
            [httpx.Response(200, json={"error": {"error_code": 5}}), httpx.ReadTimeout("lost")],
            [False, True],
            "VK_PROVIDER_TIMEOUT",
        ),
    ],
)
async def test_vk_refresh_only_after_definite_auth_refusal(
    monkeypatch, responses, refreshes, error_code
):
    monkeypatch.setattr(settings, "vk_access_token", None)
    monkeypatch.setattr(settings, "vk_owner_id", -123)
    seen = []

    class Store:
        async def access_token(self, *, force_refresh=False):
            seen.append(force_refresh)
            return "synthetic"

    calls = _transport(monkeypatch, *responses)
    with pytest.raises(VKProviderError) as caught:
        await VKProvider(credential_store=Store()).publish(text="test post", chat_id="-123")
    assert caught.value.code == error_code
    assert caught.value.ambiguous and not caught.value.retryable
    assert seen == refreshes
    assert calls == ["POST"] * len(responses)


async def test_vk_refresh_and_receipt_keep_the_authorized_wall(monkeypatch):
    monkeypatch.setattr(settings, "vk_access_token", None)
    monkeypatch.setattr(settings, "vk_owner_id", -123)
    owners = []
    refreshes = []

    class Store:
        async def access_token(self, *, force_refresh=False):
            refreshes.append(force_refresh)
            # The account setting changes after target validation but before wall.post.
            monkeypatch.setattr(settings, "vk_owner_id", -777)
            return "synthetic"

    def reply(request):
        owners.append(parse_qs(request.content.decode())["owner_id"])
        if len(owners) == 1:
            return httpx.Response(200, json={"error": {"error_code": 5}})
        # Also change it before the successful receipt is assembled.
        monkeypatch.setattr(settings, "vk_owner_id", -456)
        return httpx.Response(200, json={"response": {"post_id": 37}})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client(transport=httpx.MockTransport(reply), **kw),
    )
    adapter = VKProvider(credential_store=Store())
    receipt = await adapter.publish(text="test post", chat_id="-123")

    assert owners == [["-123"], ["-123"]]
    assert refreshes == [False, True]
    assert receipt.provider_target_id == "-123"
    assert receipt.external_url == "https://vk.com/wall-123_37"

    with pytest.raises(VKProviderError) as stale_adapter:
        await adapter.publish(text="new post", chat_id="-123")
    assert stale_adapter.value.code == "VK_TARGET_MISMATCH"
    assert refreshes == [False, True]
