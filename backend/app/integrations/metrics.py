from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from telethon import TelegramClient, errors, functions, utils
from telethon.sessions import StringSession

from app.core.config import settings


class MetricsProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderMetricsResult:
    observed_at: datetime
    views: int | None = None
    impressions: int | None = None
    reactions: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    clicks: int | None = None
    subscribers: int | None = None
    provider: str | None = None


class MetricsProvider(Protocol):
    async def get_metrics(self, *, external_id: str) -> ProviderMetricsResult: ...


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise MetricsProviderError("METRICS_RESPONSE_INVALID", "Некорректный ответ метрик.")
    return value


def _message_id(external_id: str) -> int:
    if not external_id.isascii() or not external_id.isdecimal() or int(external_id) <= 0:
        raise MetricsProviderError("METRICS_EXTERNAL_ID_INVALID", "Некорректный id публикации.")
    return int(external_id)


def _metrics_role() -> None:
    if settings.worker_role != "metrics" or settings.scheduler_role is not None:
        raise MetricsProviderError(
            "METRICS_ROLE_FORBIDDEN", "Метрики доступны только metrics worker."
        )


class TelegramMetricsProvider:
    """User-only MTProto read path; no login, view increment or session file writes."""

    async def get_metrics(self, *, external_id: str) -> ProviderMetricsResult:
        if not settings.telegram_metrics_enabled:
            raise MetricsProviderError("TELEGRAM_METRICS_DISABLED", "Метрики Telegram отключены.")
        _metrics_role()
        message_id = _message_id(external_id)
        client: Any = None
        try:
            # Suppress SDK diagnostic bodies; never pass a session to application logs.
            logger = logging.getLogger("telegram_metrics_private")
            logger.setLevel(logging.CRITICAL + 1)
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
            client = TelegramClient(
                StringSession(settings.telegram_metrics_session),
                settings.telegram_metrics_api_id,
                settings.telegram_metrics_api_hash,
                request_retries=0,
                connection_retries=0,
                auto_reconnect=False,
                flood_sleep_threshold=0,
                receive_updates=False,
                base_logger=logger,
            )
            client.session.save_entities = False
            async with asyncio.timeout(settings.metrics_request_timeout_seconds):
                await client.connect()
                if not await client.is_user_authorized():
                    raise MetricsProviderError(
                        "TELEGRAM_METRICS_AUTH_ERROR", "Сессия метрик не авторизована."
                    )
                me = await client.get_me()
                if me is None or me.bot:
                    raise MetricsProviderError(
                        "TELEGRAM_METRICS_AUTH_ERROR", "Требуется user MTProto session."
                    )
                peer = await client.get_entity(settings.telegram_metrics_peer)
                if utils.get_peer_id(peer) != settings.telegram_metrics_chat_id:
                    raise MetricsProviderError(
                        "TELEGRAM_METRICS_PEER_MISMATCH", "Peer метрик не совпадает с target."
                    )
                message = await client.get_messages(peer, ids=message_id)
                if message is None or message.id != message_id:
                    raise MetricsProviderError(
                        "TELEGRAM_METRICS_RESPONSE_INVALID", "Сообщение метрик недоступно."
                    )
                reactions = getattr(message, "reactions", None)
                results = getattr(reactions, "results", None)
                reaction_count = None
                if results is not None:
                    counts = [_count(getattr(x, "count", None)) for x in results]
                    if all(x is not None for x in counts):
                        reaction_count = sum(x for x in counts if x is not None)
                replies = getattr(message, "replies", None)
                views = _count(getattr(message, "views", None))
                shares = _count(getattr(message, "forwards", None))
                comments = _count(getattr(replies, "replies", None))
                try:
                    response = await client(
                        functions.messages.GetMessagesViewsRequest(
                            peer=peer,
                            id=[message_id],
                            increment=False,
                        )
                    )
                    if len(response.views) == 1:
                        view = response.views[0]
                        enriched_replies = getattr(view, "replies", None)
                        enriched_views = _count(getattr(view, "views", None))
                        enriched_shares = _count(getattr(view, "forwards", None))
                        enriched_comments = _count(getattr(enriched_replies, "replies", None))
                        views = enriched_views if enriched_views is not None else views
                        shares = enriched_shares if enriched_shares is not None else shares
                        comments = enriched_comments if enriched_comments is not None else comments
                except errors.RPCError:
                    # Some supergroup messages have no channel-style view counter.
                    # Preserve exact-message metrics instead of discarding partial evidence.
                    pass
                return ProviderMetricsResult(
                    observed_at=datetime.now(UTC),
                    provider="telegram_mtproto",
                    views=views,
                    shares=shares,
                    comments=comments,
                    reactions=reaction_count,
                )
        except MetricsProviderError:
            raise
        except (errors.UnauthorizedError, ValueError):
            raise MetricsProviderError(
                "TELEGRAM_METRICS_AUTH_ERROR", "Сессия метрик недоступна."
            ) from None
        except errors.FloodWaitError:
            raise MetricsProviderError(
                "TELEGRAM_METRICS_RATE_LIMIT", "Лимит чтения метрик.", retryable=True
            ) from None
        except Exception:
            raise MetricsProviderError(
                "TELEGRAM_METRICS_READ_ERROR",
                "Не удалось прочитать метрики Telegram.",
                retryable=True,
            ) from None
        finally:
            if client is not None:
                try:
                    async with asyncio.timeout(settings.metrics_request_timeout_seconds):
                        await client.disconnect()
                except Exception:
                    pass  # Never expose SDK/session exception text during cleanup.


class VKMetricsProvider:
    """Separate read credential; never falls back to a publishing token."""

    async def get_metrics(self, *, external_id: str) -> ProviderMetricsResult:
        if not settings.vk_metrics_enabled:
            raise MetricsProviderError("VK_METRICS_DISABLED", "Метрики VK отключены.")
        _metrics_role()
        post_id = _message_id(external_id)
        owner_id = settings.vk_metrics_owner_id
        if owner_id is None or owner_id >= 0 or not settings.vk_metrics_access_token:
            raise MetricsProviderError("VK_METRICS_CONFIG_INVALID", "Не настроен read provider VK.")
        try:
            async with httpx.AsyncClient(
                timeout=settings.metrics_request_timeout_seconds
            ) as client:
                response = await client.post(
                    "https://api.vk.com/method/wall.getById",
                    data={
                        "access_token": settings.vk_metrics_access_token,
                        "v": settings.vk_api_version,
                        "posts": f"{owner_id}_{post_id}",
                    },
                )
            if response.status_code >= 400:
                raise MetricsProviderError("VK_METRICS_HTTP_ERROR", "Ошибка чтения метрик VK.")
            payload = response.json()
            if "error" in payload:
                code = payload["error"].get("error_code")
                category = (
                    "AUTH_ERROR"
                    if code in [5, 27]
                    else "PERMISSION_ERROR"
                    if code in [7, 15]
                    else "RATE_LIMIT"
                    if code in [6, 9]
                    else "READ_ERROR"
                )
                raise MetricsProviderError(
                    "VK_METRICS_" + category,
                    "Ошибка чтения метрик VK.",
                    retryable=category == "RATE_LIMIT",
                )
            rows = payload["response"]
            if isinstance(rows, dict):
                rows = rows["items"]
            if (
                len(rows) != 1
                or rows[0].get("id") != post_id
                or rows[0].get("owner_id") != owner_id
            ):
                raise MetricsProviderError("VK_METRICS_POST_MISMATCH", "Exact VK post недоступен.")
            post = rows[0]

            def count(field: str) -> int | None:
                value = post.get(field)
                return _count(value.get("count") if isinstance(value, dict) else None)

            return ProviderMetricsResult(
                observed_at=datetime.now(UTC),
                provider="vk_wall",
                views=count("views"),
                likes=count("likes"),
                comments=count("comments"),
                shares=count("reposts"),
            )
        except MetricsProviderError:
            raise
        except Exception:
            raise MetricsProviderError(
                "VK_METRICS_READ_ERROR", "Не удалось прочитать метрики VK.", retryable=True
            ) from None
