from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent import Agent, AgentStatus
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.content import ContentItem, ContentStatus, ContentType, ContentVersion
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)


@dataclass(frozen=True)
class PlanItemSmmContext:
    plan: PublicationPlan
    item: PublicationPlanItem
    article: ContentItem
    version: ContentVersion
    agent: Agent

    def task_input(self) -> dict[str, object]:
        return {
            "publication_plan_id": str(self.plan.id),
            "publication_plan_item_id": str(self.item.id),
            "source_content_item_id": str(self.article.id),
            "source_content_version_id": str(self.version.id),
            "publication_plan_source_claim_ids": self.item.source_claim_ids or [],
            "plan_topic": self.item.topic,
            "plan_angle": self.item.angle,
            "plan_purpose": self.item.purpose,
            "plan_format": self.item.format,
            "plan_message_brief": self.item.message_brief,
            "plan_channel": self.item.channel.value,
        }


class PlanItemSmmService:
    """Authoritative preconditions for one post bound to a publication-plan item."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _uuid(value: object, code: str, message: str) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as exc:
            raise AppError(code, message, 409) from exc

    async def validate(
        self,
        campaign_id: UUID,
        input_data: dict[str, Any],
        *,
        assigned_agent_id: UUID | None = None,
    ) -> PlanItemSmmContext:
        plan_id = self._uuid(
            input_data.get("publication_plan_id"),
            "PUBLICATION_PLAN_REQUIRED",
            "Для создания поста нужен утверждённый медиаплан.",
        )
        item_id = self._uuid(
            input_data.get("publication_plan_item_id"),
            "PUBLICATION_PLAN_ITEM_REQUIRED",
            "Для создания поста нужен пункт медиаплана.",
        )
        plan = await self.session.get(PublicationPlan, plan_id)
        if (
            plan is None
            or plan.campaign_id != campaign_id
            or plan.status is not PublicationPlanStatus.APPROVED
        ):
            raise AppError(
                "PUBLICATION_PLAN_NOT_APPROVED",
                "Пост можно создать только по утверждённому медиаплану этой кампании.",
                409,
            )
        item = await self.session.get(PublicationPlanItem, item_id)
        if item is None or item.publication_plan_id != plan.id:
            raise AppError(
                "PUBLICATION_PLAN_ITEM_NOT_FOUND",
                "Пункт не принадлежит выбранному медиаплану.",
                409,
            )
        if item.status is not PublicationPlanItemStatus.PLANNED:
            raise AppError(
                "PUBLICATION_PLAN_ITEM_NOT_ACTIVE",
                "Пост можно создать только для активного пункта медиаплана.",
                409,
            )
        article = await self.session.get(ContentItem, item.source_content_item_id)
        version = await self.session.get(ContentVersion, item.source_content_version_id)
        if (
            article is None
            or version is None
            or article.campaign_id != campaign_id
            or article.content_type is not ContentType.ARTICLE
            or article.status is ContentStatus.ARCHIVED
            or version.content_item_id != article.id
        ):
            raise AppError(
                "PUBLICATION_PLAN_SOURCE_INVALID",
                "Пункт медиаплана не связан с точной активной версией статьи.",
                409,
            )
        approved = await self.session.scalar(
            select(Approval.id).where(
                Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                Approval.object_id == article.id,
                Approval.subject_version == version.version_number,
                Approval.status == ApprovalStatus.APPROVED,
            )
        )
        if approved is None or article.status is not ContentStatus.APPROVED:
            raise AppError(
                "PUBLICATION_PLAN_SOURCE_NOT_APPROVED",
                "Версия статьи-источника больше не является утверждённой.",
                409,
            )
        provided_article = input_data.get("source_content_item_id")
        provided_version = input_data.get("source_content_version_id")
        if (
            provided_article
            and self._uuid(
                provided_article,
                "PUBLICATION_PLAN_SOURCE_INVALID",
                "Идентификатор статьи-источника указан некорректно.",
            )
            != article.id
        ):
            raise AppError(
                "PUBLICATION_PLAN_SOURCE_INVALID",
                "Переданная статья не совпадает с источником утверждённого пункта медиаплана.",
                409,
            )
        if (
            provided_version
            and self._uuid(
                provided_version,
                "PUBLICATION_PLAN_SOURCE_INVALID",
                "Идентификатор версии статьи указан некорректно.",
            )
            != version.id
        ):
            raise AppError(
                "PUBLICATION_PLAN_SOURCE_INVALID",
                "Переданная версия не совпадает с источником утверждённого пункта медиаплана.",
                409,
            )
        if not item.source_claim_ids or not (item.source_support_summary or "").strip():
            raise AppError(
                "PUBLICATION_PLAN_PROVENANCE_REQUIRED",
                "В пункте медиаплана не зафиксированы подтверждённые тезисы источника.",
                409,
            )
        existing_post = await self.session.scalar(
            select(ContentItem.id).where(
                ContentItem.campaign_id == campaign_id,
                ContentItem.content_type == ContentType.SOCIAL_POST,
                ContentItem.status != ContentStatus.ARCHIVED,
                ContentItem.metadata_["publication_plan_item_id"].astext == str(item.id),
            )
        )
        if existing_post is not None:
            raise AppError(
                "SOCIAL_POST_ALREADY_EXISTS",
                "Для этого пункта медиаплана пост уже создан.",
                409,
            )
        agent_query = select(Agent).where(
            Agent.slug == "smm_manager",
            Agent.status == AgentStatus.ACTIVE,
        )
        if assigned_agent_id is not None:
            agent_query = agent_query.where(Agent.id == assigned_agent_id)
        agent = await self.session.scalar(agent_query)
        if agent is None:
            raise AppError(
                "AGENT_INACTIVE",
                "Активный SMM Manager не найден.",
                409,
            )
        return PlanItemSmmContext(plan, item, article, version, agent)
