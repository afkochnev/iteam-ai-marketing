"""Read-only, bounded distribution projection over the legacy write model.

All relationship decisions inspect the complete relevant scope in SQL before
pagination. Only page-sized column projections reach Python; ORM relationships
(notably ContentItem.versions) and publication serializers are never loaded.
Command endpoints remain responsible for revalidating any advertised action.
"""

import base64
import binascii
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import (
    String,
    and_,
    case,
    column,
    exists,
    func,
    literal,
    or_,
    select,
    true,
    union_all,
)
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.core.vk_canary_retry import CANARY_ID, RETRY_KEY, RETRY_V2_KEY
from app.models.activity import ActivityLog
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
)
from app.models.publication import ACTIVE_PUBLICATION_STATUSES, Publication, PublicationStatus
from app.models.publication_plan import PublicationPlan, PublicationPlanItem, PublicationPlanStatus
from app.models.user import User
from app.schemas.distribution_workspace import (
    DistributionCampaign,
    DistributionCandidate,
    DistributionCounts,
    DistributionPagination,
    DistributionPublication,
    DistributionSection,
    DistributionWorkspaceResponse,
    PublicationAction,
)

RECONCILIATION_CODES = {"TELEGRAM_RECONCILIATION_REQUIRED", "VK_RECONCILIATION_REQUIRED"}
RETRY_CODES = {
    "TELEGRAM_RATE_LIMIT",
    "TELEGRAM_PROVIDER_TIMEOUT",
    "TELEGRAM_PROVIDER_ERROR",
    "VK_RATE_LIMIT",
    "VK_PROVIDER_ERROR",
    "PUBLICATION_RECONCILED_NOT_PUBLISHED",
}


def _text_id(value: Any) -> Any:
    return sql_cast(value, String)


def _approved(content: Any, version: Any) -> Any:
    """Exact direct approval wins; otherwise use the exact parent-pack post.

    Use scalar subqueries, not approval-history hydration. Optional hashes follow
    the current write guard. PostgreSQL's built-in sha256 needs no extension.
    Malformed legacy JSON cannot turn a read into an array-expansion failure.
    """
    approval = Approval.__table__.alias()
    direct = (
        select(approval.c.subject_snapshot)
        .where(
            approval.c.object_type == ApprovalObjectType.CONTENT_ITEM,
            approval.c.object_id == content.c.id,
            approval.c.status == ApprovalStatus.APPROVED,
            approval.c.subject_version == version.c.version_number,
            approval.c.subject_snapshot["content_version_id"].as_string() == _text_id(version.c.id),
        )
        .order_by(
            approval.c.resolved_at.desc().nullslast(),
            approval.c.created_at.desc(),
            approval.c.id.desc(),
        )
        .limit(1)
        .correlate(content, version)
        .scalar_subquery()
    )
    parent = ContentItem.__table__.alias()
    posts = approval.c.subject_snapshot["posts"]
    safe_posts = case(
        (func.jsonb_typeof(posts) == "array", posts),
        else_=sql_cast(literal("[]"), JSONB),
    )
    post = func.jsonb_array_elements(safe_posts).table_valued(column("value", JSONB))
    pack_post = (
        select(post.c.value)
        .select_from(approval.join(parent, parent.c.id == approval.c.object_id).join(post, true()))
        .where(
            approval.c.object_type == ApprovalObjectType.CONTENT_ITEM,
            approval.c.object_id == content.c.parent_content_item_id,
            approval.c.status == ApprovalStatus.APPROVED,
            parent.c.campaign_id == content.c.campaign_id,
            parent.c.content_type == ContentType.SOCIAL_POST_PACK,
            post.c.value["content_item_id"].as_string() == _text_id(content.c.id),
            post.c.value["content_version_id"].as_string() == _text_id(version.c.id),
        )
        .order_by(
            approval.c.resolved_at.desc().nullslast(),
            approval.c.created_at.desc(),
            approval.c.id.desc(),
        )
        .limit(1)
        .correlate(content, version)
        .scalar_subquery()
    )
    digest = func.encode(func.sha256(func.convert_to(version.c.content, "UTF8")), "hex")

    def valid_hash(snapshot: Any) -> Any:
        value = snapshot["content_hash"].as_string()
        return or_(value.is_(None), value == "", value == digest)

    return and_(
        version.c.id.is_not(None),
        case(
            (direct.is_not(None), valid_hash(direct)),
            else_=and_(pack_post.is_not(None), valid_hash(pack_post)),
        ),
    )


@dataclass
class ContentProjection:
    source: Any
    content: Any
    version: Any
    current: Any
    plan_item: Any
    plan: Any
    plan_bound: Any
    plan_valid: Any
    plan_current: Any

    def columns(self) -> list[Any]:
        c, v, current, item, plan = (
            self.content,
            self.version,
            self.current,
            self.plan_item,
            self.plan,
        )
        display_item = PublicationPlanItem.__table__.alias()
        display_plan = PublicationPlan.__table__.alias()
        display_plan_item = (
            select(display_item.c.id)
            .join(display_plan, display_plan.c.id == display_item.c.publication_plan_id)
            .where(
                display_plan.c.campaign_id == c.c.campaign_id,
                _text_id(display_item.c.id)
                == func.coalesce(
                    c.c.metadata["publication_plan_item_id"].as_string(),
                    c.c.metadata["channel_change_origin_plan_item_id"].as_string(),
                ),
            )
            .limit(1)
            .correlate(c)
            .scalar_subquery()
        )
        return [
            c.c.id.label("joined_content_id"),
            c.c.title.label("title"),
            c.c.status.label("content_status"),
            c.c.content_type.label("content_type"),
            c.c.channel.label("content_channel"),
            v.c.id.label("joined_version_id"),
            v.c.version_number.label("version_number"),
            current.c.id.label("current_version_id"),
            _approved(c, v).label("exact_approved"),
            _approved(c, current).label("current_approved"),
            c.c.metadata["channel_change_source_publication_id"].as_string().label("change_source"),
            c.c.metadata["channel_change_applied"].as_string().label("change_applied"),
            display_plan_item.label("display_plan_item_id"),
            item.c.id.label("plan_item_id"),
            item.c.channel.label("plan_channel"),
            item.c.scheduled_at.label("plan_scheduled_at"),
            plan.c.id.label("plan_id"),
            self.plan_bound.label("plan_bound"),
            self.plan_valid.label("plan_valid"),
            self.plan_current.label("plan_current"),
        ]


def _content_projection(source: Any, item_id: Any, campaign_id: Any, version_id: Any) -> Any:
    content = ContentItem.__table__.alias()
    version = ContentVersion.__table__.alias()
    current = ContentVersion.__table__.alias()
    item = PublicationPlanItem.__table__.alias()
    plan = PublicationPlan.__table__.alias()
    article = ContentItem.__table__.alias()
    article_version = ContentVersion.__table__.alias()
    source = (
        source.outerjoin(
            content, and_(content.c.id == item_id, content.c.campaign_id == campaign_id)
        )
        .outerjoin(
            version, and_(version.c.id == version_id, version.c.content_item_id == content.c.id)
        )
        .outerjoin(
            current,
            and_(
                current.c.id == content.c.current_version_id,
                current.c.content_item_id == content.c.id,
            ),
        )
        .outerjoin(
            item, _text_id(item.c.id) == content.c.metadata["publication_plan_item_id"].as_string()
        )
        .outerjoin(
            plan,
            and_(
                plan.c.id == item.c.publication_plan_id,
                plan.c.campaign_id == campaign_id,
                _text_id(plan.c.id) == content.c.metadata["publication_plan_id"].as_string(),
            ),
        )
        .outerjoin(
            article,
            and_(
                article.c.id == item.c.source_content_item_id,
                article.c.campaign_id == campaign_id,
            ),
        )
        .outerjoin(
            article_version,
            and_(
                article_version.c.id == item.c.source_content_version_id,
                article_version.c.content_item_id == article.c.id,
            ),
        )
    )
    origin = ContentVersion.__table__.alias()
    derivation = ContentDerivation.__table__.alias()
    generation = and_(
        origin.c.content_item_id == content.c.id, origin.c.generation_key.like("plan-item:%")
    )
    plan_bound = or_(
        content.c.metadata.op("?")("publication_plan_id"),
        content.c.metadata.op("?")("publication_plan_item_id"),
        exists(select(1).where(generation).correlate(content)),
        exists(
            select(1)
            .select_from(
                origin.join(
                    derivation,
                    derivation.c.derived_content_version_id == origin.c.id,
                )
            )
            .where(
                origin.c.content_item_id == content.c.id,
                derivation.c.source_section_key == "publication_plan_item",
            )
            .correlate(content)
        ),
    )
    plan_valid = and_(
        plan.c.id.is_not(None),
        plan.c.status == PublicationPlanStatus.APPROVED,
        item.c.status == "PLANNED",
        item.c.channel == content.c.channel,
        article_version.c.id.is_not(None),
        _text_id(item.c.source_content_version_id)
        == content.c.metadata["source_content_version_id"].as_string(),
        ~exists(
            select(1)
            .where(
                generation,
                origin.c.generation_key != literal("plan-item:") + _text_id(item.c.id),
            )
            .correlate(content, item)
        ),
    )
    latest = PublicationPlan.__table__.alias()
    current_plan = (
        select(latest.c.id)
        .where(
            latest.c.campaign_id == campaign_id,
            latest.c.status == PublicationPlanStatus.APPROVED,
        )
        .order_by(latest.c.created_at.desc(), latest.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return ContentProjection(
        source,
        content,
        version,
        current,
        item,
        plan,
        plan_bound,
        plan_valid,
        plan.c.id == current_plan,
    )


def _campaign(row: Mapping[str, Any]) -> DistributionCampaign:
    return DistributionCampaign(
        id=row["campaign_id"], name=row["campaign_name"], status=row["campaign_status"]
    )


def _decode_cursor(
    cursor: str | None, campaign_id: UUID | None
) -> tuple[datetime, str, UUID] | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 1024:
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(data, dict) or set(data) != {"v", "scope", "created_at", "kind", "id"}:
            raise ValueError
        if type(data["v"]) is not int or data["v"] != 1:
            raise ValueError
        if not all(isinstance(data[key], str) for key in ("created_at", "kind", "id")):
            raise ValueError
        if data["scope"] != (str(campaign_id) if campaign_id else None):
            raise ValueError
        created = datetime.fromisoformat(data["created_at"])
        if created.tzinfo is None or data["kind"] not in {"publication", "candidate"}:
            raise ValueError
        return created.astimezone(UTC), data["kind"], UUID(data["id"])
    except (
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
        binascii.Error,
        UnicodeDecodeError,
    ) as error:
        raise AppError(
            "DISTRIBUTION_CURSOR_INVALID", "Некорректная страница публикаций.", 422
        ) from error


def _encode_cursor(row: Mapping[str, Any], campaign_id: UUID | None) -> str:
    data = {
        "v": 1,
        "scope": str(campaign_id) if campaign_id else None,
        "created_at": row["created_at"].astimezone(UTC).isoformat(),
        "kind": row["kind"],
        "id": str(row["id"]),
    }
    return (
        base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


class DistributionWorkspaceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(
        self,
        user: User,
        *,
        campaign_id: UUID | None = None,
        publication_id: UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> DistributionWorkspaceResponse:
        if not 1 <= limit <= 100:
            raise AppError(
                "DISTRIBUTION_LIMIT_INVALID", "Размер страницы должен быть от 1 до 100.", 422
            )
        if publication_id is not None and cursor is not None:
            raise AppError(
                "DISTRIBUTION_PAGE_TARGET_CONFLICT",
                "Выберите публикацию или страницу, но не оба параметра одновременно.",
                422,
            )
        after = _decode_cursor(cursor, campaign_id)
        # CurrentUser authenticates on the request session. Recheck the identity
        # inside the dedicated snapshot too, including revocation and inactivity.
        identity = (
            (
                await self.session.execute(
                    select(
                        User.is_active,
                        User.auth_version,
                        func.transaction_timestamp().label("as_of"),
                    ).where(User.id == user.id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if identity is None or identity["auth_version"] != user.auth_version:
            raise AppError("AUTHENTICATION_REQUIRED", "Требуется авторизация.", 401)
        if not identity["is_active"]:
            raise AppError("USER_INACTIVE", "Пользователь деактивирован.", 403)
        now = identity["as_of"]
        campaign = None
        if campaign_id is not None:
            selected = (
                (
                    await self.session.execute(
                        select(
                            Campaign.id,
                            Campaign.name,
                            Campaign.status,
                        ).where(Campaign.id == campaign_id)
                    )
                )
                .mappings()
                .one_or_none()
            )
            if selected is None:
                raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)
            campaign = DistributionCampaign(**selected)

        if publication_id is not None:
            target_query = (
                select(Publication.id, Publication.created_at)
                .join(Campaign, Campaign.id == Publication.campaign_id)
                .where(Publication.id == publication_id)
            )
            if campaign_id is not None:
                target_query = target_query.where(Publication.campaign_id == campaign_id)
            target = (await self.session.execute(target_query)).mappings().one_or_none()
            if target is None:
                raise AppError(
                    "PUBLICATION_NOT_FOUND", "Публикация не найдена в выбранной области.", 404
                )
            after = (target["created_at"], "publication", target["id"])

        keys = await self._page(campaign_id, limit, after, include_start=publication_id is not None)
        has_more = len(keys) > limit
        keys = keys[:limit]
        publication_ids = [row["id"] for row in keys if row["kind"] == "publication"]
        candidate_ids = [row["id"] for row in keys if row["kind"] == "candidate"]
        publications = await self._publications(publication_ids, now) if publication_ids else {}
        candidates = await self._candidates(candidate_ids, now) if candidate_ids else {}
        items: list[DistributionPublication | DistributionCandidate] = [
            publications[row["id"]] if row["kind"] == "publication" else candidates[row["id"]]
            for row in keys
        ]
        counts: Counter[str] = Counter()
        for entry in items:
            counts.update(
                entry.sections if isinstance(entry, DistributionPublication) else [entry.state]
            )
        return DistributionWorkspaceResponse(
            as_of=now,
            campaign=campaign,
            items=items,
            counts=DistributionCounts(**counts),
            pagination=DistributionPagination(
                limit=limit,
                has_more=has_more,
                next_cursor=_encode_cursor(keys[-1], campaign_id) if has_more else None,
            ),
        )

    async def _page(
        self,
        campaign_id: UUID | None,
        limit: int,
        after: tuple[datetime, str, UUID] | None,
        *,
        include_start: bool = False,
    ) -> list[Mapping[str, Any]]:
        p, c, v, campaign = (
            Publication.__table__,
            ContentItem.__table__,
            ContentVersion.__table__,
            Campaign.__table__,
        )
        publications = select(literal("publication").label("kind"), p.c.id, p.c.created_at).join(
            campaign,
            campaign.c.id == p.c.campaign_id,
        )
        existing = Publication.__table__.alias()
        planned = PublicationPlanItem.__table__.alias()
        plan = PublicationPlan.__table__.alias()
        planned_channel = (
            select(planned.c.channel)
            .join(plan, plan.c.id == planned.c.publication_plan_id)
            .where(
                _text_id(planned.c.id) == c.c.metadata["publication_plan_item_id"].as_string(),
                plan.c.campaign_id == c.c.campaign_id,
            )
            .limit(1)
            .correlate(c)
            .scalar_subquery()
        )
        candidates = (
            select(literal("candidate").label("kind"), c.c.id, c.c.created_at)
            .select_from(
                c.join(campaign, campaign.c.id == c.c.campaign_id).join(
                    v,
                    and_(
                        v.c.id == c.c.current_version_id,
                        v.c.content_item_id == c.c.id,
                    ),
                )
            )
            .where(
                c.c.content_type == ContentType.SOCIAL_POST,
                c.c.status == ContentStatus.APPROVED,
                campaign.c.status != CampaignStatus.ARCHIVED,
                _approved(c, v),
                ~exists(
                    select(1)
                    .where(
                        existing.c.content_item_id == c.c.id,
                        or_(
                            existing.c.status != PublicationStatus.CANCELLED,
                            and_(
                                existing.c.content_version_id == c.c.current_version_id,
                                existing.c.channel == func.coalesce(planned_channel, c.c.channel),
                            ),
                        ),
                    )
                    .correlate(c)
                ),
            )
        )
        if campaign_id is not None:
            publications = publications.where(p.c.campaign_id == campaign_id)
            candidates = candidates.where(c.c.campaign_id == campaign_id)
        combined = union_all(publications, candidates).subquery()
        statement = select(combined)
        if after is not None:
            created, kind, record_id = after
            statement = statement.where(
                or_(
                    combined.c.created_at < created,
                    and_(combined.c.created_at == created, combined.c.kind > kind),
                    and_(
                        combined.c.created_at == created,
                        combined.c.kind == kind,
                        combined.c.id <= record_id if include_start else combined.c.id < record_id,
                    ),
                )
            )
        result = await self.session.execute(
            statement.order_by(
                combined.c.created_at.desc(),
                combined.c.kind.asc(),
                combined.c.id.desc(),
            ).limit(limit + 1)
        )
        return [dict(row) for row in result.mappings()]

    async def _publications(
        self, ids: list[UUID], now: datetime
    ) -> dict[UUID, DistributionPublication]:
        p, campaign = Publication.__table__, Campaign.__table__
        context = _content_projection(
            p.join(campaign, campaign.c.id == p.c.campaign_id),
            p.c.content_item_id,
            p.c.campaign_id,
            p.c.content_version_id,
        )
        other = Publication.__table__.alias()
        copy = ContentItem.__table__.alias()
        replacement = exists(
            select(1)
            .select_from(
                other.join(
                    copy,
                    and_(
                        copy.c.id == other.c.content_item_id,
                        copy.c.campaign_id == other.c.campaign_id,
                    ),
                )
            )
            .where(
                other.c.campaign_id == p.c.campaign_id,
                other.c.channel == p.c.channel,
                other.c.id != p.c.id,
                other.c.status.in_(
                    [
                        PublicationStatus.SCHEDULED,
                        PublicationStatus.PUBLISHING,
                        PublicationStatus.PUBLISHED,
                    ]
                ),
                or_(
                    other.c.content_item_id == p.c.content_item_id,
                    copy.c.metadata["reuse_source_content_id"].as_string()
                    == _text_id(p.c.content_item_id),
                ),
            )
            .correlate(p)
        )
        # Same representative rule as the former UI: latest scheduled slot,
        # with a deterministic UUID tie break, across the complete history.
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        later_cancelled = exists(
            select(1)
            .where(
                other.c.campaign_id == p.c.campaign_id,
                other.c.content_item_id == p.c.content_item_id,
                other.c.channel == p.c.channel,
                other.c.status == PublicationStatus.CANCELLED,
                or_(
                    func.coalesce(other.c.scheduled_at, epoch)
                    > func.coalesce(p.c.scheduled_at, epoch),
                    and_(
                        func.coalesce(other.c.scheduled_at, epoch)
                        == func.coalesce(p.c.scheduled_at, epoch),
                        other.c.id > p.c.id,
                    ),
                ),
            )
            .correlate(p)
        )
        duplicate = exists(
            select(1)
            .where(
                other.c.campaign_id == p.c.campaign_id,
                other.c.content_item_id == p.c.content_item_id,
                other.c.channel == p.c.channel,
                other.c.id != p.c.id,
                or_(
                    other.c.status.in_((*ACTIVE_PUBLICATION_STATUSES, PublicationStatus.PUBLISHED)),
                    other.c.failure_code.in_(RECONCILIATION_CODES),
                    other.c.execution_token.is_not(None),
                ),
            )
            .correlate(p)
        )
        audit = ActivityLog.__table__
        canary_used = and_(
            p.c.id == CANARY_ID,
            exists(
                select(1).where(
                    audit.c.operation_key.in_([RETRY_KEY, RETRY_V2_KEY]),
                )
            ),
        )
        result = await self.session.execute(
            select(
                *p.c,
                campaign.c.name.label("campaign_name"),
                campaign.c.status.label("campaign_status"),
                *context.columns(),
                replacement.label("has_replacement"),
                later_cancelled.label("later_cancelled"),
                duplicate.label("has_duplicate"),
                canary_used.label("canary_used"),
            )
            .select_from(context.source)
            .where(p.c.id.in_(ids))
        )
        rows = [dict(row) for row in result.mappings()]
        override_keys = [f"publication-plan-slot-rescheduled:{row['id']}" for row in rows]
        overrides = (
            await self.session.execute(
                select(
                    audit.c.operation_key,
                    audit.c.event_type,
                    audit.c.campaign_id,
                    audit.c.metadata,
                ).where(audit.c.operation_key.in_(override_keys))
            )
        ).mappings()
        by_override = {row["operation_key"]: dict(row) for row in overrides}
        return {
            row["id"]: self._publication(
                row,
                now,
                by_override.get(f"publication-plan-slot-rescheduled:{row['id']}"),
            )
            for row in rows
        }

    @staticmethod
    def _publication(
        row: Mapping[str, Any],
        now: datetime,
        override: Mapping[str, Any] | None,
    ) -> DistributionPublication:
        status = row["status"]
        scheduled = row["scheduled_at"]
        overdue = bool(
            status == PublicationStatus.SCHEDULED
            and scheduled is not None
            and scheduled
            < now - timedelta(seconds=settings.publication_auto_dispatch_max_lateness_seconds)
        )
        sections: list[DistributionSection] = []
        if status in {
            PublicationStatus.DRAFT,
            PublicationStatus.WAITING_APPROVAL,
            PublicationStatus.APPROVED,
            PublicationStatus.PUBLISHING,
        }:
            sections.append("active")
        if status == PublicationStatus.SCHEDULED:
            sections.append("overdue" if overdue else "upcoming")
        if status == PublicationStatus.PUBLISHED:
            sections.append("published")
        if status == PublicationStatus.CANCELLED:
            sections.append("cancelled_history")
            if not row["has_replacement"] and not row["later_cancelled"]:
                sections.append("cancelled")
        elif status == PublicationStatus.FAILED or row["failure_code"]:
            sections.append("attention")
        archived = (
            row["content_status"] == ContentStatus.ARCHIVED
            or row["campaign_status"] == CampaignStatus.ARCHIVED
        )
        provider = (
            settings.telegram_publishing_enabled
            if row["channel"].value == "TELEGRAM"
            else settings.vk_publishing_enabled
        )
        actions = DistributionWorkspaceService._actions(row, now, override, archived, provider)
        return DistributionPublication(
            publication_id=row["id"],
            content_item_id=row["content_item_id"],
            content_version_id=row["content_version_id"],
            title=row["title"] or "Открыть материал",
            campaign=_campaign(row),
            channel=row["channel"],
            status=status,
            scheduled_at=scheduled,
            published_at=row["published_at"],
            external_url=row["external_url"],
            publication_plan_item_id=row["display_plan_item_id"],
            content_status=row["content_status"],
            content_source_archived=archived,
            provider_enabled=provider,
            failure_code=row["failure_code"],
            failure_message=row["failure_message"],
            is_overdue=overdue,
            lateness_seconds=max(0, int((now - scheduled).total_seconds())) if overdue else 0,
            sections=sections,
            has_replacement=bool(row["has_replacement"]),
            allowed_actions=actions,
        )

    @staticmethod
    def _actions(
        row: Mapping[str, Any],
        now: datetime,
        override: Mapping[str, Any] | None,
        archived: bool,
        provider: bool,
    ) -> list[PublicationAction]:
        actions: list[PublicationAction] = []
        status = row["status"]
        # Only the operational representative offers a new action; every
        # historical cancellation remains visible without reopening a slot.
        if status == PublicationStatus.CANCELLED and (
            row["has_replacement"] or row["later_cancelled"]
        ):
            return actions
        if status in {
            PublicationStatus.DRAFT,
            PublicationStatus.WAITING_APPROVAL,
            PublicationStatus.APPROVED,
            PublicationStatus.SCHEDULED,
        }:
            actions.append("cancel")
        related = row["joined_content_id"] is not None and row["joined_version_id"] is not None
        reconciliation = row["failure_code"] in RECONCILIATION_CODES
        matching_reconciliation = (
            row["failure_code"] == f"{row['channel'].value}_RECONCILIATION_REQUIRED"
        )
        if related and status == PublicationStatus.FAILED and matching_reconciliation:
            actions.extend(["reconcile_published", "reconcile_not_published"])
        if (
            related
            and status == PublicationStatus.CANCELLED
            and row["content_status"] == ContentStatus.ARCHIVED
            and row["content_type"] == ContentType.SOCIAL_POST
            and row["current_version_id"] is not None
            and row["campaign_status"] != CampaignStatus.ARCHIVED
        ):
            actions.append("reuse")
        valid_content = (
            related
            and not archived
            and row["content_status"] == ContentStatus.APPROVED
            and row["content_type"] == ContentType.SOCIAL_POST
            and row["content_channel"] == row["channel"]
            and not (row["change_source"] and row["change_applied"] != "true")
        )
        valid_plan = not row["plan_bound"] or bool(row["plan_valid"])
        approved = valid_content and bool(row["exact_approved"]) and valid_plan
        idle = row["execution_token"] is None and not reconciliation
        if not valid_content:
            return actions
        if approved and status in {PublicationStatus.DRAFT, PublicationStatus.WAITING_APPROVAL}:
            if not row["plan_bound"] or (
                row["plan_scheduled_at"] > now and row["scheduled_at"] == row["plan_scheduled_at"]
            ):
                actions.append("approve")
        if (
            approved
            and not row["plan_bound"]
            and status in {PublicationStatus.APPROVED, PublicationStatus.SCHEDULED}
        ):
            actions.append("schedule")
        current_plan = not row["plan_bound"] or bool(row["plan_current"])
        can_replan = (
            idle
            and valid_plan
            and current_plan
            and not row["has_duplicate"]
            and bool(
                row["exact_approved"]
                if status == PublicationStatus.SCHEDULED
                else row["current_approved"]
            )
        )
        if (
            status in {PublicationStatus.CANCELLED, PublicationStatus.SCHEDULED}
            and can_replan
            and not (status == PublicationStatus.CANCELLED and row["has_replacement"])
        ):
            actions.append("replan")
        if (
            approved
            and idle
            and status in {PublicationStatus.APPROVED, PublicationStatus.SCHEDULED}
        ):
            actions.append("change_channel")
            if current_plan and not row["has_duplicate"]:
                actions.append("batch_replan")
        snapshot_matches = not row["plan_bound"] or row["scheduled_at"] == row["plan_scheduled_at"]
        if (
            not snapshot_matches
            and valid_plan
            and override is not None
            and row["scheduled_at"] is not None
            and row["plan_scheduled_at"] is not None
        ):
            expected = {
                "publication_id": str(row["id"]),
                "publication_plan_item_id": str(row["plan_item_id"]),
                "content_version_id": str(row["content_version_id"]),
                "channel": row["channel"].value,
                "original_scheduled_at": row["plan_scheduled_at"].astimezone(UTC).isoformat(),
                "effective_scheduled_at": row["scheduled_at"].astimezone(UTC).isoformat(),
            }
            snapshot_matches = (
                override["event_type"] == "PUBLICATION_PLAN_SLOT_RESCHEDULED"
                and override["campaign_id"] == row["campaign_id"]
                and all(
                    str((override["metadata"] or {}).get(key)) == value
                    for key, value in expected.items()
                )
            )
        canary_scope = (
            row["channel"].value != "VK"
            or settings.vk_canary_publication_id is None
            or row["id"] == settings.vk_canary_publication_id
        )
        early_canary = (
            status == PublicationStatus.SCHEDULED
            and row["scheduled_at"] is not None
            and row["scheduled_at"] > now
            and row["channel"].value == "VK"
            and row["id"] == settings.vk_canary_publication_id
        )
        due = row["scheduled_at"] is None or row["scheduled_at"] <= now or early_canary
        delivery = (
            approved
            and snapshot_matches
            and provider
            and canary_scope
            and not row["canary_used"]
            and due
            and row["approved_for_publish_at"] is not None
            and row["approved_for_publish_by"] is not None
        )
        if (
            delivery
            and idle
            and (
                status == PublicationStatus.APPROVED
                or (status == PublicationStatus.SCHEDULED and row["scheduled_at"] is not None)
            )
        ):
            actions.append("publish_now")
        if (
            delivery
            and status == PublicationStatus.FAILED
            and row["failure_code"] in RETRY_CODES
            and row["retry_count"] < settings.publication_max_retries
        ):
            actions.append("retry")
        return actions

    async def _candidates(
        self, ids: list[UUID], now: datetime
    ) -> dict[UUID, DistributionCandidate]:
        content, campaign = ContentItem.__table__, Campaign.__table__
        context = _content_projection(
            content.join(campaign, campaign.c.id == content.c.campaign_id),
            content.c.id,
            content.c.campaign_id,
            content.c.current_version_id,
        )
        result = await self.session.execute(
            select(
                content.c.id,
                content.c.campaign_id,
                campaign.c.name.label("campaign_name"),
                campaign.c.status.label("campaign_status"),
                *context.columns(),
            )
            .select_from(context.source)
            .where(content.c.id.in_(ids))
        )
        items = {}
        for raw_row in result.mappings():
            row = dict(raw_row)
            valid = bool(row["plan_valid"])
            state: Literal["ready", "needs_plan", "overdue"] = (
                "overdue"
                if valid and row["plan_scheduled_at"] <= now
                else "ready"
                if valid
                else "needs_plan"
            )
            items[row["id"]] = DistributionCandidate(
                id=row["id"],
                title=row["title"],
                campaign=_campaign(row),
                channel=row["content_channel"],
                plan_channel=row["plan_channel"] if row["plan_id"] else None,
                plan_scheduled_at=row["plan_scheduled_at"] if row["plan_id"] else None,
                approved_version_id=row["current_version_id"],
                approved_version_number=row["version_number"],
                state=state,
                allowed_actions=["reschedule_plan", "cancel_plan"]
                if state == "overdue"
                and not (row["change_source"] and row["change_applied"] != "true")
                else [],
            )
        return items
