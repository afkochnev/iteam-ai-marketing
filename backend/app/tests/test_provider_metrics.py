from datetime import UTC
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings, settings
from app.integrations import metrics

SECRET = "PRIVATE_METRICS_SECRET_123"


@pytest.fixture
def metrics_config(monkeypatch):
    for field, value in {
        "worker_role": "metrics",
        "scheduler_role": None,
        "telegram_metrics_enabled": True,
        "telegram_metrics_api_id": 123,
        "telegram_metrics_api_hash": SECRET,
        "telegram_metrics_session": SECRET,
        "telegram_metrics_peer": "@iteam2022",
        "telegram_metrics_chat_id": -1001321892281,
        "vk_metrics_enabled": True,
        "vk_metrics_access_token": SECRET,
        "vk_metrics_owner_id": -150574411,
    }.items():
        monkeypatch.setattr(settings, field, value)


@pytest.fixture
def telegram(monkeypatch, metrics_config):
    client = NS(
        session=NS(save_entities=True),
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=NS(bot=False)),
        get_entity=AsyncMock(return_value=NS(id=1321892281)),
        get_messages=AsyncMock(return_value=NS(id=964, reactions=None)),
    )
    request = AsyncMock(return_value=NS(views=[NS(views=12, forwards=None, replies=None)]))

    class Client:
        def __init__(self, *args, **kwargs):
            kwargs["base_logger"].getChild("network").error(SECRET)
            self.__dict__.update(client.__dict__)

        async def __call__(self, value):
            return await request(value)

    monkeypatch.setattr(metrics, "StringSession", lambda value: NS())
    monkeypatch.setattr(metrics, "TelegramClient", Client)
    monkeypatch.setattr(metrics.utils, "get_peer_id", lambda peer: -1001321892281)
    return client, request


async def test_telegram_exact_read_only_request_and_nulls(telegram):
    client, request = telegram
    result = await metrics.TelegramMetricsProvider().get_metrics(external_id="964")
    req = request.call_args.args[0]
    assert req.id == [964] and req.increment is False
    client.get_entity.assert_awaited_once_with("@iteam2022")
    client.get_messages.assert_awaited_once_with(client.get_entity.return_value, ids=964)
    assert result.views == 12 and result.observed_at.tzinfo == UTC
    assert all(
        getattr(result, name) is None
        for name in [
            "shares",
            "comments",
            "reactions",
            "likes",
            "impressions",
            "clicks",
            "subscribers",
        ]
    )
    client.disconnect.assert_awaited_once()


async def test_telegram_supplied_counts_including_zero(telegram):
    client, req = telegram
    req.return_value = NS(views=[NS(views=0, forwards=3, replies=NS(replies=0))])
    client.get_messages.return_value = NS(id=964, reactions=NS(results=[NS(count=2), NS(count=4)]))
    result = await metrics.TelegramMetricsProvider().get_metrics(external_id="964")
    assert (result.views, result.shares, result.comments, result.reactions) == (0, 3, 0, 6)
    assert result.likes is None


async def test_telegram_wrong_peer_never_reads_messages(telegram, monkeypatch):
    client, request = telegram
    monkeypatch.setattr(metrics.utils, "get_peer_id", lambda peer: -100999)
    with pytest.raises(metrics.MetricsProviderError) as error:
        await metrics.TelegramMetricsProvider().get_metrics(external_id="964")
    assert error.value.code == "TELEGRAM_METRICS_PEER_MISMATCH"
    request.assert_not_awaited()
    client.get_messages.assert_not_awaited()


@pytest.mark.parametrize("failure", ["unauthorized", "bot", "session", "network", "cleanup"])
async def test_telegram_auth_and_session_privacy(telegram, monkeypatch, caplog, failure):
    client, request = telegram
    if failure == "unauthorized":
        client.is_user_authorized.return_value = False
    if failure == "bot":
        client.get_me.return_value = NS(bot=True)
    if failure == "session":
        monkeypatch.setattr(
            metrics, "StringSession", lambda _: (_ for _ in ()).throw(ValueError(SECRET))
        )
    if failure == "network":
        client.connect.side_effect = RuntimeError(SECRET)
    if failure == "cleanup":
        client.connect.side_effect = RuntimeError(SECRET)
        client.disconnect.side_effect = RuntimeError(SECRET)
    with pytest.raises(metrics.MetricsProviderError) as error:
        await metrics.TelegramMetricsProvider().get_metrics(external_id="964")
    assert SECRET not in str(error.value) + repr(error.value) + caplog.text
    assert error.value.code in ["TELEGRAM_METRICS_AUTH_ERROR", "TELEGRAM_METRICS_READ_ERROR"]


@pytest.fixture
def vk(monkeypatch, metrics_config):
    response = NS(status_code=200, json=lambda: {"response": [{"id": 964, "owner_id": -150574411}]})
    post = AsyncMock(return_value=response)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return await post(*args, **kwargs)

    monkeypatch.setattr(metrics.httpx, "AsyncClient", Client)
    return response, post


async def test_vk_exact_post_and_missing_null(vk):
    response, post = vk
    result = await metrics.VKMetricsProvider().get_metrics(external_id="964")
    assert post.call_args.kwargs["data"]["posts"] == "-150574411_964"
    assert SECRET not in post.call_args.args[0]
    assert (
        result.views is None
        and result.likes is None
        and result.comments is None
        and result.shares is None
    )
    assert result.observed_at.tzinfo == UTC


async def test_vk_maps_only_actual_values(vk):
    response, post = vk
    response.json = lambda: {
        "response": {
            "items": [
                {
                    "id": 964,
                    "owner_id": -150574411,
                    "views": {"count": 0},
                    "likes": {"count": 2},
                    "comments": {"count": 3},
                    "reposts": {"count": 4},
                }
            ]
        }
    }
    r = await metrics.VKMetricsProvider().get_metrics(external_id="964")
    assert (r.views, r.likes, r.comments, r.shares) == (0, 2, 3, 4)
    assert all(getattr(r, x) is None for x in ["reactions", "impressions", "clicks", "subscribers"])


@pytest.mark.parametrize("code", [5, 27, 7, 15, 6, 9, 100])
async def test_vk_api_errors_are_safe(vk, caplog, code):
    response, post = vk
    response.json = lambda: {
        "error": {"error_code": code, "error_msg": SECRET, "request_params": [{"value": SECRET}]}
    }
    with pytest.raises(metrics.MetricsProviderError) as error:
        await metrics.VKMetricsProvider().get_metrics(external_id="964")
    assert error.value.code.startswith("VK_METRICS_")
    assert SECRET not in str(error.value) + repr(error.value) + caplog.text


async def test_vk_transport_error_safe(vk, caplog):
    response, post = vk
    post.side_effect = RuntimeError(SECRET)
    with pytest.raises(metrics.MetricsProviderError) as error:
        await metrics.VKMetricsProvider().get_metrics(external_id="964")
    assert error.value.code == "VK_METRICS_READ_ERROR"
    assert SECRET not in str(error.value) + caplog.text


@pytest.mark.parametrize("provider", [metrics.TelegramMetricsProvider, metrics.VKMetricsProvider])
@pytest.mark.parametrize("external_id", ["-1", "0", "owner_964", "secret", "١٢"])
async def test_invalid_external_id_never_network(metrics_config, provider, external_id):
    with pytest.raises(metrics.MetricsProviderError, match="Некорректный id"):
        await provider().get_metrics(external_id=external_id)


@pytest.mark.parametrize("role", [None, "ai", "ai_control", "publication", "publication_control"])
async def test_provider_cannot_execute_outside_metrics_worker(metrics_config, monkeypatch, role):
    monkeypatch.setattr(settings, "worker_role", role)
    with pytest.raises(metrics.MetricsProviderError) as error:
        await metrics.VKMetricsProvider().get_metrics(external_id="964")
    assert error.value.code == "METRICS_ROLE_FORBIDDEN"


@pytest.mark.parametrize("role", [None, "ai", "ai_control", "publication", "publication_control"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("telegram_metrics_api_id", 123),
        ("telegram_metrics_api_hash", SECRET),
        ("telegram_metrics_session", SECRET),
        ("telegram_metrics_peer", "@synthetic"),
        ("telegram_metrics_chat_id", -100123),
        ("vk_metrics_access_token", SECRET),
        ("vk_metrics_owner_id", -123),
        ("telegram_metrics_enabled", True),
        ("vk_metrics_enabled", True),
    ],
)
def test_role_metrics_capabilities_forbidden(role, field, value):
    config = Settings(_env_file=None, worker_role=role, **{field: value})
    with pytest.raises(RuntimeError) as error:
        config.validate_metrics_capabilities()
    assert field.upper() in str(error.value) and SECRET not in str(error.value)


@pytest.mark.parametrize("channel", ["telegram", "vk"])
def test_enabled_metrics_requires_complete_config(channel):
    c = Settings(_env_file=None, worker_role="metrics", **{channel + "_metrics_enabled": True})
    with pytest.raises(RuntimeError, match="Metrics configuration is invalid"):
        c.validate_worker_capabilities()


def test_metrics_single_channel_without_write_or_openai_credentials():
    Settings(
        _env_file=None,
        worker_role="metrics",
        metrics_sync_enabled=True,
        vk_metrics_enabled=True,
        vk_metrics_access_token=SECRET,
        vk_metrics_owner_id=-123,
    ).validate_worker_capabilities()
    with pytest.raises(RuntimeError, match="publication capability is forbidden"):
        Settings(
            _env_file=None, worker_role="metrics", telegram_bot_token=SECRET
        ).validate_worker_capabilities()
