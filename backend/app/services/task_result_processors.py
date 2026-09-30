import hashlib
import logging
import re
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.agent_run import AgentRun, ToolCall, ToolCallStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import (
    ContentChannel,
    ContentDerivation,
    ContentItem,
    ContentStatus,
    ContentType,
    ContentVersion,
    ContentVersionSource,
)
from app.models.knowledge import KnowledgeItem, KnowledgeItemStatus
from app.models.knowledge_pack import (
    KnowledgePack,
    KnowledgePackItem,
    KnowledgePackStatus,
)
from app.models.publication_plan import PublicationPlan, PublicationPlanItem, PublicationPlanStatus
from app.models.task import Task, TaskStatus, TaskType
from app.repositories.knowledge_packs import KnowledgePackRepository
from app.schemas.agent_outputs import (
    ArticleWritingResult,
    CampaignPlan,
    KnowledgeResearchResult,
    SingleSocialPostResult,
    SocialPostPackResult,
)
from app.schemas.knowledge import KnowledgeSearchResult
from app.services.activity_log_service import ActivityLogService
from app.services.approval_service import ApprovalService
from app.services.knowledge_search_service import build_result_key
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)


class TaskResultProcessor(Protocol):
    async def process(
        self,
        session: AsyncSession,
        run: AgentRun,
        task: Task,
        output: dict[str, object],
    ) -> None: ...


class DefaultTaskResultProcessor:
    async def process(
        self,
        session: AsyncSession,
        run: AgentRun,
        task: Task,
        output: dict[str, object],
    ) -> None:
        await TaskService(session).complete_task(task.id, output, commit=False)


class CampaignPlanningResultProcessor:
    async def process(
        self,
        session: AsyncSession,
        run: AgentRun,
        task: Task,
        output: dict[str, object],
    ) -> None:
        try:
            plan = CampaignPlan.model_validate(output)
        except ValidationError as exc:
            raise AppError(
                "INVALID_CAMPAIGN_PLAN", "Структура стратегии не прошла проверку.", 422
            ) from exc
        campaign = (
            await session.execute(
                select(Campaign).where(Campaign.id == task.campaign_id).with_for_update()
            )
        ).scalar_one()
        target_version = int(task.input_data["strategy_version"])
        if (
            campaign.status is not CampaignStatus.PLANNING
            or target_version != campaign.strategy_version + 1
        ):
            raise AppError("INVALID_CAMPAIGN_PLAN", "Версия стратегии больше не актуальна.", 409)
        snapshot = plan.model_dump(mode="json")
        campaign.strategy = snapshot
        campaign.strategy_version = target_version
        campaign.status = CampaignStatus.WAITING_APPROVAL
        await ApprovalService(session).create_strategy_approval(
            campaign.id, target_version, snapshot, run.agent_id
        )
        await TaskService(session).complete_task(task.id, snapshot, commit=False)


_QUANTITATIVE_TERMS = (
    "roi",
    "quantitative",
    "количествен",
    "метрик",
    "до/после",
    "до и после",
    "before/after",
    "before after",
)
_COMMON_BRIEF_WORDS = {
    "какой",
    "какие",
    "какую",
    "компании",
    "company",
    "нужен",
    "нужна",
    "для",
    "and",
    "the",
    "with",
    "find",
    "facts",
    "найти",
    "релевантные",
    "источники",
}


def _words(value: str) -> set[str]:
    return {
        word
        for word in "".join(char.lower() if char.isalnum() else " " for char in value).split()
        if len(word) >= 4 and word not in _COMMON_BRIEF_WORDS
    }


def _is_knowledge_sufficient(
    task: Task,
    research: KnowledgeResearchResult,
    selected_rows: list[tuple[KnowledgeSearchResult, ToolCall]],
) -> bool:
    """Apply the deterministic, deliverable-aware sufficiency policy.

    ``gaps`` are retained as constraints for the writer and do not, by
    themselves, make a pack unusable.  The application only blocks when the
    requested brief explicitly requires evidence that is absent, or when the
    minimum grounded-material invariants are not met.
    """

    if not research.summary.strip() or not selected_rows:
        return False

    brief = str(task.input_data.get("brief", ""))
    evidence = " ".join(
        [
            research.research_query,
            research.summary,
            *(source.excerpt for source, _call in selected_rows),
            *(source.source_title for source, _call in selected_rows),
        ]
    )
    # For a non-trivial brief, require at least one lexical anchor in the
    # verified evidence.  This is intentionally conservative: the search
    # query and selected excerpts are already produced by the real tool path,
    # while provenance validation above remains authoritative.
    brief_words = _words(brief)
    if brief_words and not brief_words.intersection(_words(evidence)):
        return False

    lowered = brief.lower()
    if any(term in lowered for term in _QUANTITATIVE_TERMS):
        source_evidence = " ".join(
            [source.excerpt for source, _call in selected_rows]
            + [source.source_title for source, _call in selected_rows]
        )
        evidence_lower = source_evidence.lower()
        # Quantitative deliverables need actual numeric/measurement evidence,
        # not merely a qualitative gap or testimonial.
        has_measurement = any(char.isdigit() for char in evidence_lower) or any(
            term in evidence_lower
            for term in ("percent", "%", "процент", "metric", "метрик", "roi")
        )
        if not has_measurement:
            return False

    return True


class KnowledgeResearchResultProcessor:
    async def process(
        self,
        session: AsyncSession,
        run: AgentRun,
        task: Task,
        output: dict[str, object],
    ) -> None:
        existing = await KnowledgePackRepository(session).get_by_run(run.id)
        if existing:
            return
        try:
            research = KnowledgeResearchResult.model_validate(output)
        except ValidationError as exc:
            raise AppError(
                "INVALID_KNOWLEDGE_RESEARCH_RESULT",
                "Структура результата исследования не прошла проверку.",
                422,
            ) from exc
        tool_calls = list(
            (
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.agent_run_id == run.id,
                        ToolCall.tool_name == "search_knowledge",
                        ToolCall.status == ToolCallStatus.COMPLETED,
                    )
                )
            ).all()
        )
        if not tool_calls:
            raise AppError(
                "KNOWLEDGE_TOOL_NOT_USED",
                "Knowledge Keeper не выполнил поиск по базе знаний.",
                422,
            )
        verified: dict[str, tuple[KnowledgeSearchResult, ToolCall]] = {}
        for tool_call in tool_calls:
            result = tool_call.result or {}
            for raw in result.get("results", []):
                try:
                    item = KnowledgeSearchResult.model_validate(raw)
                except ValidationError:
                    continue
                if item.result_key != build_result_key(
                    item.knowledge_item_id, item.file_id, item.excerpt
                ):
                    continue
                verified.setdefault(item.result_key, (item, tool_call))
        selected_keys = [item.result_key for item in research.selected_results]
        if any(key not in verified for key in selected_keys):
            raise AppError(
                "INVALID_KNOWLEDGE_SELECTION",
                "Выбран источник, которого не было в результатах поиска.",
                422,
            )
        selected_rows = [verified[key] for key in selected_keys]
        local_ids = [item.knowledge_item_id for item, _call in selected_rows]
        if local_ids:
            ready_ids = set(
                (
                    await session.scalars(
                        select(KnowledgeItem.id).where(
                            KnowledgeItem.id.in_(local_ids),
                            KnowledgeItem.status == KnowledgeItemStatus.READY,
                        )
                    )
                ).all()
            )
            if any(item_id not in ready_ids for item_id in local_ids):
                raise AppError(
                    "INVALID_KNOWLEDGE_SELECTION",
                    "Один из выбранных источников больше не доступен.",
                    409,
                )
        # The model's ``sufficient`` flag is a proposal, not the business
        # decision.  A useful pack may still have explicit gaps (for example,
        # no quantitative case study), while a deliverable which explicitly
        # asks for that evidence must remain blocked.  Keep this validation
        # deterministic and local to the application boundary.
        sufficient = _is_knowledge_sufficient(task, research, selected_rows)
        status = KnowledgePackStatus.READY if sufficient else KnowledgePackStatus.INSUFFICIENT
        pack = KnowledgePack(
            campaign_id=task.campaign_id,
            task_id=task.id,
            agent_run_id=run.id,
            created_by_agent_id=run.agent_id,
            strategy_version=_optional_int(task.input_data.get("strategy_version")),
            status=status,
            research_query=research.research_query,
            summary=research.summary,
            gaps=research.gaps,
            metadata_={},
        )
        session.add(pack)
        await session.flush()
        await ActivityLogService(session).record(
            "KNOWLEDGE_PACK_CREATED",
            operation_key=f"knowledge-pack-created:{run.id}",
            campaign_id=task.campaign_id,
            task_id=task.id,
            agent_id=run.agent_id,
            metadata={"knowledge_pack_id": str(pack.id)},
        )
        reasons = {item.result_key: item.selection_reason for item in research.selected_results}
        for position, (source, tool_call) in enumerate(selected_rows, start=1):
            session.add(
                KnowledgePackItem(
                    knowledge_pack_id=pack.id,
                    knowledge_item_id=source.knowledge_item_id,
                    tool_call_id=tool_call.id,
                    result_key=source.result_key,
                    source_id=source.source_id,
                    source_title=source.source_title,
                    filename=source.filename,
                    file_id=source.file_id,
                    excerpt=source.excerpt,
                    relevance_score=source.score,
                    selection_reason=reasons[source.result_key],
                    position=position,
                    metadata_=source.metadata,
                )
            )
        output_data: dict[str, object] = {
            "knowledge_pack_id": str(pack.id),
            "status": status.value,
        }
        if sufficient:
            await TaskService(session).complete_task(task.id, output_data, commit=False)
        else:
            task.status = TaskStatus.FAILED
            task.output_data = {**output_data, "error_code": "INSUFFICIENT_KNOWLEDGE"}
            task.error_message = "INSUFFICIENT_KNOWLEDGE: Недостаточно материалов в базе знаний."
            task.completed_at = datetime.now(UTC)
            await session.flush()


def render_article_markdown(article: object) -> str:
    draft = ArticleWritingResult.model_validate({"sufficient": True, "article": article}).article
    assert draft is not None
    parts = [draft.title]
    if draft.subtitle:
        parts.append(draft.subtitle)
    parts.append(draft.lead)
    for section in draft.sections:
        parts.extend([section.heading, section.body_markdown])
    # CTA is structured metadata, not part of the publishable Article body.
    # Keeping this boundary here prevents arbitrary model values from leaking
    # into persisted plain text.
    parts.extend(["Заключение", draft.conclusion])
    return "\n\n".join(parts)


def article_quality_errors(text: str) -> list[str]:
    """Validate objective defects in the stored reader-facing Article body."""
    errors: list[str] = []
    if len(text.strip()) < 100:
        errors.append("too_short")
    if "**" in text:
        errors.append("markdown_bold")
    if "```" in text:
        errors.append("markdown_fence")
    # Structured-model residue must never reach the reader-facing body.  Keep
    # this deliberately narrow: reject only unmistakable JSON-like scalar
    # tails such as ``:null},`` rather than trying to score prose quality.
    if re.search(r"(?i)(?::\s*(?:null|true|false)\s*[,}\]]+)\s*$", text):
        errors.append("serialization_artifact")
    if re.search(r"\{[^{}]*:\s*[^{}]*\}\s*,?\s*$", text):
        errors.append("serialization_artifact")
    lowered = text.casefold()
    for label in ("cta:", "section_key", "content_version_id", "provenance", "debug metadata"):
        if label in lowered:
            errors.append(f"internal_label:{label}")
    return errors


class WriterResultProcessor:
    async def process(
        self,
        session: AsyncSession,
        run: AgentRun,
        task: Task,
        output: dict[str, object],
    ) -> None:
        existing = await session.scalar(
            select(ContentVersion).where(ContentVersion.source_agent_run_id == run.id)
        )
        if existing:
            return
        try:
            result = ArticleWritingResult.model_validate(output)
        except ValidationError as exc:
            raise AppError(
                "INVALID_ARTICLE_RESULT", "Структура статьи не прошла проверку.", 422
            ) from exc
        calls = list(
            (
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.agent_run_id == run.id,
                        ToolCall.tool_name == "read_knowledge_pack",
                        ToolCall.status == ToolCallStatus.COMPLETED,
                    )
                )
            ).all()
        )
        if not calls:
            raise AppError("KNOWLEDGE_PACK_NOT_READ", "Writer не прочитал пакет знаний.", 422)
        allowed = {
            UUID(str(value)) for value in run.input_data.get("allowed_knowledge_pack_ids", [])
        }
        read_pack_ids: set[UUID] = set()
        verified: set[UUID] = set()
        for call in calls:
            payload = call.result or {}
            if payload.get("pack_id"):
                read_pack_ids.add(UUID(str(payload["pack_id"])))
            for item in payload.get("items", []):
                if item.get("knowledge_pack_item_id"):
                    verified.add(UUID(str(item["knowledge_pack_item_id"])))
        if not allowed.issubset(read_pack_ids):
            raise AppError(
                "KNOWLEDGE_PACK_NOT_READ",
                "Writer не прочитал все необходимые пакеты знаний.",
                422,
            )
        if not result.sufficient:
            task.status = TaskStatus.FAILED
            task.error_message = (
                "INSUFFICIENT_ARTICLE_EVIDENCE: Недостаточно подтверждённых материалов для статьи."
            )
            task.output_data = {
                "gaps": result.gaps,
                "error_code": "INSUFFICIENT_ARTICLE_EVIDENCE",
            }
            task.completed_at = datetime.now(UTC)
            await session.flush()
            return
        assert result.article is not None
        selected = [
            source_id
            for section in result.article.sections
            for source_id in section.knowledge_pack_item_ids
        ]
        if not selected:
            raise AppError(
                "INVALID_ARTICLE_SOURCE",
                "Статья не содержит подтверждённых источников.",
                422,
            )
        if any(source_id not in verified for source_id in selected):
            raise AppError(
                "INVALID_ARTICLE_SOURCE",
                "Статья содержит неподтверждённый источник.",
                422,
            )
        rows = {
            item.id: item
            for item in (
                await session.scalars(
                    select(KnowledgePackItem).where(KnowledgePackItem.id.in_(selected))
                )
            ).all()
        }
        if any(source_id not in rows for source_id in selected):
            raise AppError("INVALID_ARTICLE_SOURCE", "Источник статьи не найден.", 422)
        rendered_content = render_article_markdown(result.article)
        if article_quality_errors(rendered_content):
            raise AppError(
                "INVALID_ARTICLE_QUALITY",
                "Статья содержит недопустимые служебные или форматные артефакты.",
                422,
            )
        if task.task_type is TaskType.CONTENT_REVISION:
            if task.input_data.get("revision_target_type") != ContentType.ARTICLE.value:
                raise AppError(
                    "INVALID_REVISION_TARGET",
                    "Эта задача не является доработкой статьи.",
                    409,
                )
            content_id = UUID(str(task.input_data["content_item_id"]))
            item = (
                await session.execute(
                    select(ContentItem).where(ContentItem.id == content_id).with_for_update()
                )
            ).scalar_one_or_none()
            if item is None or item.content_type is not ContentType.ARTICLE:
                raise AppError("INVALID_REVISION_TARGET", "Статья для доработки не найдена.", 409)
            current_number = int(
                await session.scalar(
                    select(ContentVersion.version_number).where(
                        ContentVersion.id == item.current_version_id
                    )
                )
                or 0
            )
            generation_key = f"article:revision:{task.id}"
            existing_version = await session.scalar(
                select(ContentVersion).where(
                    ContentVersion.source_agent_run_id == run.id,
                    ContentVersion.generation_key == generation_key,
                )
            )
            if existing_version is not None:
                return
            version = ContentVersion(
                content_item_id=item.id,
                version_number=current_number + 1,
                content=rendered_content,
                structured_content=result.article.model_dump(mode="json"),
                created_by_agent_id=run.agent_id,
                source_agent_run_id=run.id,
                generation_key=generation_key,
                change_description=(
                    f"Article revision: {task.input_data.get('revision_comment', '')}"
                ),
            )
            session.add(version)
            await session.flush()
            item.current_version_id = version.id
            for position, section in enumerate(result.article.sections, start=1):
                for source_position, source_id in enumerate(
                    section.knowledge_pack_item_ids, start=1
                ):
                    session.add(
                        ContentVersionSource(
                            content_version_id=version.id,
                            knowledge_pack_item_id=source_id,
                            section_key=section.key,
                            position=(position * 1000) + source_position,
                        )
                    )
            await ApprovalService(session).create_content_approval(
                item.id,
                version.version_number,
                {
                    "content_item_id": str(item.id),
                    "content_version_id": str(version.id),
                    "version_number": version.version_number,
                    "title": result.article.title,
                    "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
                },
                run.agent_id,
            )
            await TaskService(session).complete_task(
                task.id,
                {
                    "content_item_id": str(item.id),
                    "content_version_id": str(version.id),
                    "content_type": "ARTICLE",
                },
                commit=False,
            )
            await ActivityLogService(session).record(
                "CONTENT_REVISION_COMPLETED",
                operation_key=f"revision-completed:{run.id}",
                campaign_id=task.campaign_id,
                task_id=task.id,
                agent_id=run.agent_id,
                content_item_id=item.id,
            )
            return
        item = ContentItem(
            campaign_id=task.campaign_id,
            source_task_id=task.id,
            content_type=ContentType.ARTICLE,
            title=result.article.title,
            status=ContentStatus.WAITING_APPROVAL,
            author_agent_id=run.agent_id,
            metadata_={},
        )
        session.add(item)
        await session.flush()
        version = ContentVersion(
            content_item_id=item.id,
            version_number=1,
            content=rendered_content,
            structured_content=result.article.model_dump(mode="json"),
            created_by_agent_id=run.agent_id,
            source_agent_run_id=run.id,
            generation_key="article",
            change_description="Initial article draft generated by Writer",
        )
        session.add(version)
        await session.flush()
        item.current_version_id = version.id
        for position, section in enumerate(result.article.sections, start=1):
            for source_position, source_id in enumerate(section.knowledge_pack_item_ids, start=1):
                session.add(
                    ContentVersionSource(
                        content_version_id=version.id,
                        knowledge_pack_item_id=source_id,
                        section_key=section.key,
                        position=(position * 1000) + source_position,
                    )
                )
        await ApprovalService(session).create_content_approval(
            item.id,
            version.version_number,
            {
                "content_item_id": str(item.id),
                "content_version_id": str(version.id),
                "version_number": 1,
                "title": result.article.title,
                "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
            },
            run.agent_id,
        )
        await ActivityLogService(session).record(
            "ARTICLE_CREATED",
            operation_key=f"article-created:{run.id}",
            campaign_id=task.campaign_id,
            task_id=task.id,
            agent_id=run.agent_id,
            content_item_id=item.id,
        )
        await TaskService(session).complete_task(
            task.id,
            {
                "content_item_id": str(item.id),
                "content_version_id": str(version.id),
                "content_type": "ARTICLE",
            },
            commit=False,
        )


class SocialPostResultProcessor:
    async def process(
        self,
        session: AsyncSession,
        run: AgentRun,
        task: Task,
        output: dict[str, object],
    ) -> None:
        existing = await session.scalar(
            select(ContentVersion).where(
                ContentVersion.source_agent_run_id == run.id,
                ContentVersion.generation_key.like("pack%"),
            )
        )
        if existing:
            return
        try:
            result = (
                SingleSocialPostResult.model_validate(output)
                if task.input_data.get("publication_plan_item_id")
                else SocialPostPackResult.model_validate(output)
            )
        except ValidationError as exc:
            raise AppError(
                "INVALID_SOCIAL_POST_RESULT",
                "Структура публикаций не прошла проверку.",
                422,
            ) from exc
        calls = list(
            (
                await session.scalars(
                    select(ToolCall).where(
                        ToolCall.agent_run_id == run.id,
                        ToolCall.tool_name == "read_content_version",
                        ToolCall.status == ToolCallStatus.COMPLETED,
                    )
                )
            ).all()
        )
        if not calls:
            raise AppError("CONTENT_VERSION_NOT_READ", "SMM Manager не прочитал статью.", 422)
        allowed = {
            UUID(str(value)) for value in run.input_data.get("allowed_content_version_ids", [])
        }
        read_ids = {
            UUID(str((call.result or {}).get("content_version_id")))
            for call in calls
            if (call.result or {}).get("content_version_id")
        }
        if not allowed.issubset(read_ids):
            raise AppError(
                "CONTENT_VERSION_NOT_READ",
                "SMM Manager не прочитал все версии статьи.",
                422,
            )
        if not result.sufficient:
            task.status = TaskStatus.FAILED
            task.error_message = (
                "INSUFFICIENT_SOCIAL_SOURCE: Недостаточно материала для публикаций."
            )
            task.output_data = {
                "gaps": result.gaps,
                "error_code": "INSUFFICIENT_SOCIAL_SOURCE",
            }
            task.completed_at = datetime.now(UTC)
            await session.flush()
            return
        assert result.pack is not None
        plan_item_id_raw = task.input_data.get("publication_plan_item_id")
        if plan_item_id_raw:
            plan_id = UUID(str(task.input_data.get("publication_plan_id")))
            plan_item_id = UUID(str(plan_item_id_raw))
            plan = await session.scalar(
                select(PublicationPlan).where(
                    PublicationPlan.id == plan_id,
                    PublicationPlan.status == PublicationPlanStatus.APPROVED,
                )
            )
            plan_item = await session.scalar(
                select(PublicationPlanItem).where(
                    PublicationPlanItem.id == plan_item_id,
                    PublicationPlanItem.publication_plan_id == plan_id,
                )
            )
            if plan is None or plan_item is None or len(result.pack.posts) != 1:
                raise AppError(
                    "INVALID_PUBLICATION_PLAN_ITEM",
                    "Результат не соответствует утверждённому пункту плана.",
                    422,
                )
            post = result.pack.posts[0]
            authoritative_channel = plan_item.channel.value
            generation_key = f"plan-item:{plan_item.id}"
            existing_version = await session.scalar(
                select(ContentVersion).where(
                    ContentVersion.source_agent_run_id == run.id,
                    ContentVersion.generation_key == generation_key,
                )
            )
            if existing_version is not None:
                return
            child = ContentItem(
                campaign_id=task.campaign_id,
                source_task_id=task.id,
                content_type=ContentType.SOCIAL_POST,
                title=post.title,
                status=ContentStatus.WAITING_APPROVAL,
                author_agent_id=run.agent_id,
                channel=ContentChannel(authoritative_channel),
                metadata_={
                    "publication_plan_id": str(plan.id),
                    "publication_plan_item_id": str(plan_item.id),
                    "source_content_version_id": str(plan_item.source_content_version_id),
                    "source_claim_ids": plan_item.source_claim_ids or [],
                },
            )
            session.add(child)
            await session.flush()
            version = ContentVersion(
                content_item_id=child.id,
                version_number=1,
                content=post.text_markdown,
                structured_content={
                    **post.model_dump(mode="json"),
                    "channel": authoritative_channel,
                },
                created_by_agent_id=run.agent_id,
                source_agent_run_id=run.id,
                generation_key=generation_key,
                change_description="Social post generated from approved publication plan item",
            )
            session.add(version)
            await session.flush()
            child.current_version_id = version.id
            session.add(
                ContentDerivation(
                    derived_content_version_id=version.id,
                    source_content_version_id=plan_item.source_content_version_id,
                    source_section_key="publication_plan_item",
                )
            )
            await ApprovalService(session).create_content_approval(
                child.id,
                1,
                {
                    "content_item_id": str(child.id),
                    "content_version_id": str(version.id),
                    "version_number": 1,
                    "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
                },
                run.agent_id,
            )
            await TaskService(session).complete_task(
                task.id,
                {
                    "content_item_id": str(child.id),
                    "content_version_id": str(version.id),
                    "content_type": "SOCIAL_POST",
                    "publication_plan_id": str(plan.id),
                    "publication_plan_item_id": str(plan_item.id),
                },
                commit=False,
            )
            await ActivityLogService(session).record(
                "PUBLICATION_PLAN_ITEM_USED_FOR_SMM",
                operation_key=f"publication-plan-item-used:{plan_item.id}",
                campaign_id=task.campaign_id,
                task_id=task.id,
                agent_id=run.agent_id,
                content_item_id=child.id,
                metadata={
                    "publication_plan_id": str(plan.id),
                    "publication_plan_item_id": str(plan_item.id),
                    "source_content_version_id": str(plan_item.source_content_version_id),
                    "source_claim_ids": plan_item.source_claim_ids or [],
                },
            )
            return
        campaign = await session.get(Campaign, task.campaign_id)
        strategy = (campaign.strategy if campaign else {}) or {}
        social = strategy.get("social_strategy", {})
        expected_count = int(social.get("post_count", len(result.pack.posts)))
        channels = set(social.get("channels", []))
        if (
            len(result.pack.posts) != expected_count
            or any(post.channel not in channels for post in result.pack.posts)
            or not channels.issubset({post.channel for post in result.pack.posts})
        ):
            logger.warning(
                "SMM result rejected by campaign strategy",
                extra={
                    "event": "smm_strategy_validation_failed",
                    "agent_run_id": str(run.id),
                    "task_id": str(task.id),
                    "validation_code": "INVALID_SOCIAL_POST_RESULT",
                    "strategy_version": task.input_data.get("strategy_version"),
                    "source_content_version_id": task.input_data.get("source_content_version_id"),
                    "expected_post_count": expected_count,
                    "actual_post_count": len(result.pack.posts),
                    "allowed_channels": sorted(channels),
                    "actual_channels": sorted({post.channel for post in result.pack.posts}),
                },
            )
            raise AppError(
                "INVALID_SOCIAL_POST_RESULT",
                "Посты не соответствуют стратегии кампании.",
                422,
            )
        available: dict[UUID, set[str]] = {}
        for call in calls:
            payload = call.result or {}
            version_id = payload.get("content_version_id")
            if version_id:
                available[UUID(str(version_id))] = set(payload.get("section_keys", []))
        for post in result.pack.posts:
            for source in post.sources:
                if source.content_version_id not in allowed:
                    raise AppError(
                        "INVALID_SOCIAL_SOURCE",
                        "Пост ссылается на недоступную версию статьи.",
                        422,
                    )
                if source.section_key not in available.get(source.content_version_id, set()):
                    raise AppError(
                        "INVALID_SOCIAL_SOURCE_SECTION",
                        "Пост ссылается на неизвестный раздел статьи.",
                        422,
                    )
        if task.task_type is TaskType.CONTENT_REVISION:
            if task.input_data.get("revision_target_type") != ContentType.SOCIAL_POST_PACK.value:
                raise AppError(
                    "INVALID_REVISION_TARGET",
                    "Эта задача не является доработкой пакета.",
                    409,
                )
            pack_id = UUID(str(task.input_data["content_item_id"]))
            pack_item = (
                await session.execute(
                    select(ContentItem).where(ContentItem.id == pack_id).with_for_update()
                )
            ).scalar_one_or_none()
            if pack_item is None or pack_item.content_type is not ContentType.SOCIAL_POST_PACK:
                raise AppError("INVALID_REVISION_TARGET", "Пакет публикаций не найден.", 409)
            assert pack_item is not None
            previous = await session.scalar(
                select(ContentVersion).where(ContentVersion.id == pack_item.current_version_id)
            )
            if previous is None:
                raise AppError("INVALID_REVISION_TARGET", "Версия пакета не найдена.", 409)
            generation_key = f"pack:revision:{task.id}"
            existing_version = await session.scalar(
                select(ContentVersion).where(
                    ContentVersion.source_agent_run_id == run.id,
                    ContentVersion.generation_key == generation_key,
                )
            )
            if existing_version is not None:
                return
            pack_version = ContentVersion(
                content_item_id=pack_item.id,
                version_number=previous.version_number + 1,
                content="\n\n".join(
                    [f"# {result.pack.strategy_summary}"]
                    + [f"## {p.title}\n\n{p.text_markdown}\n\n{p.cta}" for p in result.pack.posts]
                ),
                structured_content=result.pack.model_dump(mode="json"),
                created_by_agent_id=run.agent_id,
                source_agent_run_id=run.id,
                generation_key=generation_key,
                change_description=(
                    f"Social post pack revision: {task.input_data.get('revision_comment', '')}"
                ),
            )
            session.add(pack_version)
            await session.flush()
            children = list(
                (
                    await session.scalars(
                        select(ContentItem)
                        .where(ContentItem.parent_content_item_id == pack_item.id)
                        .with_for_update()
                    )
                ).all()
            )
            by_key: dict[str, ContentItem] = {}
            for child in children:
                current = await session.scalar(
                    select(ContentVersion).where(ContentVersion.id == child.current_version_id)
                )
                if current:
                    by_key[str(current.structured_content.get("key"))] = child
            incoming_keys = {post.key for post in result.pack.posts}
            for old_key, child in by_key.items():
                if old_key not in incoming_keys:
                    # Preserve historical child content, but remove it from the
                    # active pack without hard-deleting its audit trail.
                    child.status = ContentStatus.ARCHIVED
                    child.archived_at = datetime.now(UTC)
            revision_approval_posts: list[dict[str, object]] = []
            for post in result.pack.posts:
                existing_child = by_key.get(post.key)
                if existing_child is None:
                    child = ContentItem(
                        campaign_id=task.campaign_id,
                        source_task_id=task.id,
                        parent_content_item_id=pack_item.id,
                        channel=ContentChannel(post.channel),
                        content_type=ContentType.SOCIAL_POST,
                        title=post.title,
                        status=ContentStatus.WAITING_APPROVAL,
                        author_agent_id=run.agent_id,
                        metadata_={},
                    )
                    session.add(child)
                    await session.flush()
                    number = 1
                else:
                    child = existing_child
                    number = (
                        int(
                            await session.scalar(
                                select(ContentVersion.version_number).where(
                                    ContentVersion.id == child.current_version_id
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    child.status = ContentStatus.WAITING_APPROVAL
                    child.channel = ContentChannel(post.channel)
                    child.title = post.title
                version = ContentVersion(
                    content_item_id=child.id,
                    version_number=number,
                    content=post.text_markdown,
                    structured_content=post.model_dump(mode="json"),
                    created_by_agent_id=run.agent_id,
                    source_agent_run_id=run.id,
                    generation_key=f"post:revision:{task.id}:{post.key}",
                    change_description=(
                        f"Social post revision: {task.input_data.get('revision_comment', '')}"
                    ),
                )
                session.add(version)
                await session.flush()
                child.current_version_id = version.id
                revision_approval_posts.append(
                    {
                        "content_item_id": str(child.id),
                        "content_version_id": str(version.id),
                        "channel": post.channel,
                        "title": post.title,
                        "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
                    }
                )
                for source in post.sources:
                    session.add(
                        ContentDerivation(
                            derived_content_version_id=version.id,
                            source_content_version_id=source.content_version_id,
                            source_section_key=source.section_key,
                        )
                    )
            pack_item.current_version_id = pack_version.id
            await ApprovalService(session).create_content_approval(
                pack_item.id,
                pack_version.version_number,
                {
                    "content_item_id": str(pack_item.id),
                    "content_version_id": str(pack_version.id),
                    "version_number": pack_version.version_number,
                    "posts": revision_approval_posts,
                },
                run.agent_id,
            )
            await TaskService(session).complete_task(
                task.id,
                {
                    "content_item_id": str(pack_item.id),
                    "content_type": "SOCIAL_POST_PACK",
                    "post_count": len(result.pack.posts),
                },
                commit=False,
            )
            await ActivityLogService(session).record(
                "CONTENT_REVISION_COMPLETED",
                operation_key=f"revision-completed:{run.id}",
                campaign_id=task.campaign_id,
                task_id=task.id,
                agent_id=run.agent_id,
                content_item_id=pack_item.id,
            )
            return
        article_title = "Статья"
        if calls:
            article_title = str((calls[0].result or {}).get("title") or article_title)
        pack_item = ContentItem(
            campaign_id=task.campaign_id,
            source_task_id=task.id,
            content_type=ContentType.SOCIAL_POST_PACK,
            title=f"Публикации: {article_title}",
            status=ContentStatus.WAITING_APPROVAL,
            author_agent_id=run.agent_id,
            metadata_={},
        )
        session.add(pack_item)
        await session.flush()
        pack_content = "\n\n".join(
            [f"# {result.pack.strategy_summary}"]
            + [
                f"## {post.title}\n\n{post.text_markdown}\n\n{post.cta}"
                for post in result.pack.posts
            ]
        )
        pack_version = ContentVersion(
            content_item_id=pack_item.id,
            version_number=1,
            content=pack_content,
            structured_content=result.pack.model_dump(mode="json"),
            created_by_agent_id=run.agent_id,
            source_agent_run_id=run.id,
            generation_key="pack",
            change_description="Social post pack generated by SMM Manager",
        )
        session.add(pack_version)
        await session.flush()
        pack_item.current_version_id = pack_version.id
        approval_posts: list[dict[str, object]] = []
        for post in result.pack.posts:
            channel = ContentChannel(post.channel)
            child = ContentItem(
                campaign_id=task.campaign_id,
                source_task_id=task.id,
                parent_content_item_id=pack_item.id,
                channel=channel,
                content_type=ContentType.SOCIAL_POST,
                title=post.title,
                status=ContentStatus.WAITING_APPROVAL,
                author_agent_id=run.agent_id,
                metadata_={},
            )
            session.add(child)
            await session.flush()
            version = ContentVersion(
                content_item_id=child.id,
                version_number=1,
                content=post.text_markdown,
                structured_content=post.model_dump(mode="json"),
                created_by_agent_id=run.agent_id,
                source_agent_run_id=run.id,
                generation_key=f"post:{post.key}",
                change_description="Social post generated by SMM Manager",
            )
            session.add(version)
            await session.flush()
            child.current_version_id = version.id
            approval_posts.append(
                {
                    "content_item_id": str(child.id),
                    "content_version_id": str(version.id),
                    "channel": post.channel,
                    "title": post.title,
                    "content_hash": hashlib.sha256(version.content.encode()).hexdigest(),
                }
            )
            for source in post.sources:
                session.add(
                    ContentDerivation(
                        derived_content_version_id=version.id,
                        source_content_version_id=source.content_version_id,
                        source_section_key=source.section_key,
                    )
                )
        await ApprovalService(session).create_content_approval(
            pack_item.id,
            1,
            {
                "content_item_id": str(pack_item.id),
                "content_version_id": str(pack_version.id),
                "version_number": 1,
                "posts": approval_posts,
            },
            run.agent_id,
        )
        await ActivityLogService(session).record(
            "SOCIAL_POST_PACK_CREATED",
            operation_key=f"pack-created:{run.id}",
            campaign_id=task.campaign_id,
            task_id=task.id,
            agent_id=run.agent_id,
            content_item_id=pack_item.id,
        )
        await TaskService(session).complete_task(
            task.id,
            {
                "content_item_id": str(pack_item.id),
                "content_type": "SOCIAL_POST_PACK",
                "post_count": len(result.pack.posts),
            },
            commit=False,
        )


class ContentRevisionResultProcessor:
    async def process(
        self,
        session: AsyncSession,
        run: AgentRun,
        task: Task,
        output: dict[str, object],
    ) -> None:
        if task.input_data.get("revision_target_type") == ContentType.ARTICLE.value:
            await WriterResultProcessor().process(session, run, task, output)
        elif task.input_data.get("revision_target_type") == ContentType.SOCIAL_POST_PACK.value:
            await SocialPostResultProcessor().process(session, run, task, output)
        else:
            raise AppError("INVALID_REVISION_TARGET", "Недопустимый объект доработки.", 409)


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


class TaskResultProcessorRegistry:
    def get(self, task_type: TaskType) -> TaskResultProcessor:
        if task_type is TaskType.CAMPAIGN_PLANNING:
            return CampaignPlanningResultProcessor()
        if task_type is TaskType.KNOWLEDGE_RESEARCH:
            return KnowledgeResearchResultProcessor()
        if task_type is TaskType.WRITE_ARTICLE:
            return WriterResultProcessor()
        if task_type is TaskType.CREATE_SOCIAL_POSTS:
            return SocialPostResultProcessor()
        if task_type is TaskType.CONTENT_REVISION:
            return ContentRevisionResultProcessor()
        return DefaultTaskResultProcessor()


result_processor_registry = TaskResultProcessorRegistry()
