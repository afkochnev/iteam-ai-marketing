from collections import defaultdict
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.errors import AppError
from app.models.agent_run import AgentRun
from app.models.approval import Approval, ApprovalObjectType, ApprovalStatus
from app.models.campaign import Campaign, CampaignStatus
from app.models.content import ContentItem, ContentStatus, ContentType
from app.models.knowledge import KnowledgeItem, KnowledgeItemStatus, KnowledgeSource
from app.models.knowledge_pack import KnowledgePackStatus
from app.models.marketing_feedback import (
    FeedbackAnalysisStatus,
    MarketingFeedback,
    MarketingFeedbackAnalysis,
)
from app.models.publication import Publication, PublicationStatus
from app.models.publication_metrics import PublicationMetricsSnapshot
from app.models.publication_plan import (
    PublicationPlan,
    PublicationPlanItem,
    PublicationPlanItemStatus,
    PublicationPlanStatus,
)
from app.models.task import Task, TaskStatus
from app.repositories.campaigns import CampaignRepository
from app.repositories.content import ContentRepository
from app.repositories.knowledge import KnowledgeRepository
from app.repositories.knowledge_packs import KnowledgePackRepository
from app.repositories.tasks import TaskRepository
from app.schemas.campaign import CampaignResponse
from app.schemas.campaign_workspace import (
    CampaignDirectorBrief,
    CampaignWorkspaceResponse,
    WorkspaceArticle,
    WorkspaceContentReference,
    WorkspaceFeedbackState,
    WorkspaceKnowledgePack,
    WorkspaceKnowledgeState,
    WorkspaceNextStep,
    WorkspacePipelineStage,
    WorkspacePlan,
    WorkspacePlanItem,
    WorkspacePublicationReference,
    WorkspaceTask,
)


class CampaignWorkspaceService:
    """Read-only composition of persisted campaign workflow state."""

    _TASK_ERRORS: dict[str, tuple[str, str]] = {
        "WORKER_INTERRUPTED": (
            "Выполнение остановилось до завершения.",
            "Откройте задачу, проверьте состояние и доступность повтора.",
        ),
        "QUEUE_ENQUEUE_FAILED": (
            "Задачу не удалось передать в очередь.",
            "Проверьте состояние очереди и повторите запуск, если он доступен.",
        ),
        "TASK_RETRY_EXHAUSTED": (
            "Автоматические попытки выполнения исчерпаны.",
            "Разберите задачу и устраните причину до нового запуска.",
        ),
        "PUBLICATION_PLAN_SOURCE_INVALID": (
            "План ссылается на другую версию утверждённой статьи.",
            "Проверьте источник в медиаплане и создайте пост после исправления связи.",
        ),
    }

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get(self, campaign_id: UUID) -> CampaignWorkspaceResponse:
        campaign = await CampaignRepository(self.session).get_by_id(campaign_id)
        if campaign is None:
            raise AppError("CAMPAIGN_NOT_FOUND", "Кампания не найдена.", 404)

        contents = await ContentRepository(self.session).list(campaign_id=campaign_id)
        tasks = await TaskRepository(self.session).list_tasks(campaign_id=campaign_id)
        plans = list(
            (
                await self.session.scalars(
                    select(PublicationPlan)
                    .where(PublicationPlan.campaign_id == campaign_id)
                    .options(
                        selectinload(PublicationPlan.items).selectinload(
                            PublicationPlanItem.source_content_item
                        ),
                        selectinload(PublicationPlan.items).selectinload(
                            PublicationPlanItem.source_content_version
                        ),
                    )
                    .order_by(PublicationPlan.created_at.desc(), PublicationPlan.id.desc())
                )
            ).all()
        )
        publication_rows = list(
            (
                await self.session.scalars(
                    select(Publication)
                    .where(Publication.campaign_id == campaign_id)
                    .order_by(Publication.created_at.desc(), Publication.id.desc())
                )
            ).all()
        )
        content_ids = [item.id for item in contents]
        if content_ids:
            approvals = list(
                (
                    await self.session.scalars(
                        select(Approval)
                        .where(
                            Approval.object_type == ApprovalObjectType.CONTENT_ITEM,
                            Approval.object_id.in_(content_ids),
                        )
                        .order_by(
                            Approval.resolved_at.desc().nullslast(), Approval.created_at.desc()
                        )
                    )
                ).all()
            )
        else:
            approvals = []
        strategy_approvals = list(
            (
                await self.session.scalars(
                    select(Approval)
                    .where(
                        Approval.object_type == ApprovalObjectType.CAMPAIGN_STRATEGY,
                        Approval.object_id == campaign_id,
                        Approval.subject_version == campaign.strategy_version,
                    )
                    .order_by(Approval.created_at.desc())
                )
            ).all()
        )
        latest_runs = await self._latest_runs([task.id for task in tasks])
        strategy_status = strategy_approvals[0].status if strategy_approvals else campaign.status
        approved_versions = self._approved_versions(approvals)
        task_by_id = {task.id: task for task in tasks}
        plan_items = [item for plan in plans for item in plan.items]
        plan_item_by_id = {item.id: item for item in plan_items}
        current_plan = self._current_plan(plans)
        posts_by_item: dict[UUID, list[ContentItem]] = defaultdict(list)
        post_item_ids: dict[UUID, UUID] = {}
        post_by_id: dict[UUID, ContentItem] = {}
        social_posts = [item for item in contents if item.content_type is ContentType.SOCIAL_POST]
        articles = [item for item in contents if item.content_type is ContentType.ARTICLE]

        for post in social_posts:
            post_by_id[post.id] = post
            plan_item_id = self._metadata_uuid(post, "publication_plan_item_id")
            if plan_item_id in plan_item_by_id:
                posts_by_item[plan_item_id].append(post)
                post_item_ids[post.id] = plan_item_id

        publications_by_item: dict[UUID, list[Publication]] = defaultdict(list)
        for publication in publication_rows:
            plan_item_id = self._metadata_uuid(
                post_by_id.get(publication.content_item_id), "publication_plan_item_id"
            )
            if plan_item_id in plan_item_by_id:
                publications_by_item[plan_item_id].append(publication)

        content_refs = {
            item.id: self._content_reference(item, task_by_id, approved_versions)
            for item in contents
        }
        workspace_plans = {
            plan.id: self._plan(
                plan,
                posts_by_item,
                publications_by_item,
                content_refs,
            )
            for plan in plans
        }
        current_workspace_plan = workspace_plans.get(current_plan.id) if current_plan else None
        other_workspace_plans = [
            workspace_plans[plan.id]
            for plan in plans
            if current_plan is None or plan.id != current_plan.id
        ]

        selected_plan_items = (
            [
                item
                for item in current_plan.items
                if item.status is PublicationPlanItemStatus.PLANNED
            ]
            if current_plan
            else []
        )
        article_plan_items: dict[UUID, list[PublicationPlanItem]] = defaultdict(list)
        for item in selected_plan_items:
            article_plan_items[item.source_content_item_id].append(item)
        publication_refs = [
            self._publication_reference(item, post_item_ids.get(item.content_item_id))
            for item in publication_rows
        ]
        article_cards = [
            self._article(
                item,
                task_by_id,
                approved_versions,
                article_plan_items.get(item.id, []),
                posts_by_item,
                publications_by_item,
                campaign,
            )
            for item in articles
        ]
        issue_tasks = [
            self._task_summary(task, latest_runs.get(task.id), plan_item_by_id)
            for task in tasks
            if task.status
            in {
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
                TaskStatus.IN_PROGRESS,
                TaskStatus.WAITING_APPROVAL,
                TaskStatus.WAITING_REVIEW,
                TaskStatus.READY,
            }
        ]
        knowledge = await self._knowledge_state(campaign, task_by_id)
        feedback = await self._feedback_state(campaign_id)
        director = self._director(
            campaign,
            strategy_status,
            knowledge,
            article_cards,
            social_posts,
            current_plan,
            posts_by_item,
            publication_rows,
            tasks,
            feedback,
        )

        return CampaignWorkspaceResponse(
            campaign=CampaignResponse.model_validate(campaign),
            director=director,
            knowledge=knowledge,
            articles=article_cards,
            social_posts=[content_refs[item.id] for item in social_posts],
            publication_plan=current_workspace_plan,
            other_plans=other_workspace_plans,
            publications=publication_refs,
            attention_tasks=issue_tasks,
            feedback=feedback,
        )

    async def _latest_runs(self, task_ids: list[UUID]) -> dict[UUID, AgentRun]:
        if not task_ids:
            return {}
        runs = list(
            (
                await self.session.scalars(
                    select(AgentRun)
                    .where(AgentRun.task_id.in_(task_ids))
                    .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                )
            ).all()
        )
        latest: dict[UUID, AgentRun] = {}
        for run in runs:
            latest.setdefault(run.task_id, run)
        return latest

    @staticmethod
    def _approved_versions(approvals: list[Approval]) -> dict[UUID, UUID]:
        approved: dict[UUID, UUID] = {}
        for approval in approvals:
            if approval.status is not ApprovalStatus.APPROVED:
                continue
            snapshot = approval.subject_snapshot or {}
            raw_version = snapshot.get("content_version_id")
            posts = snapshot.get("posts")
            if isinstance(posts, list):
                raw_version = next(
                    (
                        post.get("content_version_id")
                        for post in posts
                        if isinstance(post, dict)
                        and str(post.get("content_item_id")) == str(approval.object_id)
                    ),
                    raw_version,
                )
            if approval.object_id in approved or not raw_version:
                continue
            try:
                approved[approval.object_id] = UUID(str(raw_version))
            except ValueError:
                continue
        return approved

    @staticmethod
    def _metadata_uuid(item: ContentItem | None, key: str) -> UUID | None:
        raw = (item.metadata_ or {}).get(key) if item else None
        try:
            return UUID(str(raw)) if raw else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _current_plan(plans: list[PublicationPlan]) -> PublicationPlan | None:
        active = [plan for plan in plans if plan.status is not PublicationPlanStatus.ARCHIVED]
        for status in (
            PublicationPlanStatus.APPROVED,
            PublicationPlanStatus.WAITING_APPROVAL,
            PublicationPlanStatus.DRAFT,
            PublicationPlanStatus.REJECTED,
        ):
            match = next((plan for plan in active if plan.status is status), None)
            if match:
                return match
        return active[0] if active else None

    @staticmethod
    def _content_reference(
        item: ContentItem,
        tasks: dict[UUID, Task],
        approved_versions: dict[UUID, UUID],
    ) -> WorkspaceContentReference:
        versions = {version.id: version.version_number for version in item.versions}
        approved_version_id = approved_versions.get(item.id)
        source_task = tasks.get(item.source_task_id)
        return WorkspaceContentReference(
            id=item.id,
            title=item.title,
            status=item.status,
            content_type=item.content_type,
            current_version_id=item.current_version_id,
            current_version_number=(
                versions.get(item.current_version_id) if item.current_version_id else None
            ),
            approved_version_id=approved_version_id,
            approved_version_number=(
                versions.get(approved_version_id) if approved_version_id is not None else None
            ),
            source_task_id=item.source_task_id,
            source_task_title=source_task.title if source_task else None,
            source_task_status=source_task.status if source_task else None,
            parent_content_item_id=item.parent_content_item_id,
            publication_plan_item_id=CampaignWorkspaceService._metadata_uuid(
                item, "publication_plan_item_id"
            ),
            channel=item.channel,
        )

    @staticmethod
    def _publication_reference(
        item: Publication, plan_item_id: UUID | None = None
    ) -> WorkspacePublicationReference:
        return WorkspacePublicationReference(
            id=item.id,
            content_item_id=item.content_item_id,
            content_version_id=item.content_version_id,
            channel=item.channel,
            status=item.status,
            scheduled_at=item.scheduled_at,
            published_at=item.published_at,
            publication_plan_item_id=plan_item_id,
        )

    def _plan(
        self,
        plan: PublicationPlan,
        posts_by_item: dict[UUID, list[ContentItem]],
        publications_by_item: dict[UUID, list[Publication]],
        content_refs: dict[UUID, WorkspaceContentReference],
    ) -> WorkspacePlan:
        items: list[WorkspacePlanItem] = []
        for item in plan.items:
            if item.status is not PublicationPlanItemStatus.PLANNED:
                continue
            posts = posts_by_item.get(item.id, [])
            pubs = publications_by_item.get(item.id, [])
            source = item.source_content_item
            source_version = item.source_content_version
            source_version_matches_article = bool(
                source and source_version and source_version.content_item_id == source.id
            )
            source_version_number = (
                source_version.version_number if source_version_matches_article else None
            )
            post_refs = [content_refs[post.id] for post in posts if post.id in content_refs]
            publication_refs = [self._publication_reference(pub, item.id) for pub in pubs]
            item_anchor = f"/campaigns/{plan.campaign_id}#plan-item-{item.id}"
            article_stage = WorkspacePipelineStage(
                label="Статья",
                status=(
                    "available"
                    if source_version_matches_article
                    else "unverified"
                    if source
                    else "missing"
                ),
                href=f"/content/{source.id}" if source else None,
                action_label=(
                    f"v{source_version_number}"
                    if source_version_matches_article
                    else "Версия источника не подтверждена"
                    if source
                    else "Связь не зафиксирована"
                ),
            )
            post_stage_status = self._post_stage_status(posts)
            post_stage = WorkspacePipelineStage(
                label="Пост",
                status=post_stage_status,
                href=f"/content/{posts[0].id}" if posts else item_anchor,
                action_label=(
                    "Создать пост"
                    if (
                        not posts
                        and plan.status is PublicationPlanStatus.APPROVED
                        and source_version_matches_article
                    )
                    else None
                ),
            )
            approval_stage = WorkspacePipelineStage(
                label="Согласование",
                status=(
                    "approved"
                    if any(post.status is ContentStatus.APPROVED for post in posts)
                    else "waiting"
                    if any(post.status is ContentStatus.WAITING_APPROVAL for post in posts)
                    else "not_started"
                ),
                href=f"/content/{posts[0].id}#approval" if posts else None,
            )
            publication_stages = self._publication_stages(publication_refs, item_anchor)
            items.append(
                WorkspacePlanItem(
                    id=item.id,
                    position=item.position,
                    scheduled_at=item.scheduled_at,
                    channel=item.channel,
                    topic=item.topic,
                    purpose=item.purpose,
                    format=item.format,
                    message_brief=item.message_brief,
                    source_content_item_id=item.source_content_item_id,
                    source_content_item_title=source.title if source else None,
                    source_content_version_id=(
                        item.source_content_version_id if source_version_matches_article else None
                    ),
                    source_version_number=source_version_number,
                    source_claim_ids=item.source_claim_ids,
                    source_support_summary=item.source_support_summary,
                    status=item.status.value,
                    social_posts=post_refs,
                    publications=publication_refs,
                    pipeline=[article_stage, post_stage, approval_stage, *publication_stages],
                )
            )
        return WorkspacePlan(
            id=plan.id,
            status=plan.status,
            planning_horizon_start=plan.planning_horizon_start,
            planning_horizon_end=plan.planning_horizon_end,
            timezone_policy=plan.timezone_policy,
            generated_by_agent_run_id=plan.generated_by_agent_run_id,
            item_count=len(items),
            items=items,
        )

    @staticmethod
    def _post_stage_status(posts: list[ContentItem]) -> str:
        if not posts:
            return "not_created"
        if any(post.status is ContentStatus.APPROVED for post in posts):
            return "approved"
        if any(post.status is ContentStatus.WAITING_APPROVAL for post in posts):
            return "waiting_approval"
        return posts[0].status.value.lower()

    @staticmethod
    def _publication_stages(
        publications: list[WorkspacePublicationReference], anchor: str
    ) -> list[WorkspacePipelineStage]:
        statuses = {publication.status for publication in publications}
        href = "/publications" if publications else anchor
        if PublicationStatus.FAILED in statuses:
            schedule_status, schedule_label = "failed", "Ошибка"
        elif statuses & {
            PublicationStatus.SCHEDULED,
            PublicationStatus.PUBLISHING,
            PublicationStatus.PUBLISHED,
        }:
            schedule_status, schedule_label = "scheduled", "Запланировано"
        elif statuses & {PublicationStatus.APPROVED, PublicationStatus.WAITING_APPROVAL}:
            schedule_status, schedule_label = "approved", "Пост утверждён"
        else:
            schedule_status, schedule_label = "not_scheduled", "Не запланировано"
        is_published = PublicationStatus.PUBLISHED in statuses
        return [
            WorkspacePipelineStage(
                label="Запланировано",
                status=schedule_status,
                href=href,
                action_label=schedule_label,
            ),
            WorkspacePipelineStage(
                label="Опубликовано",
                status="published" if is_published else "not_published",
                href=href if is_published else None,
                action_label="Доставлено" if is_published else "Ожидает доставки",
            ),
        ]

    def _article(
        self,
        item: ContentItem,
        tasks: dict[UUID, Task],
        approved_versions: dict[UUID, UUID],
        plan_items: list[PublicationPlanItem],
        posts_by_item: dict[UUID, list[ContentItem]],
        publications_by_item: dict[UUID, list[Publication]],
        campaign: Campaign,
    ) -> WorkspaceArticle:
        reference = self._content_reference(item, tasks, approved_versions)
        posts = [post for plan_item in plan_items for post in posts_by_item.get(plan_item.id, [])]
        publications = [
            publication
            for plan_item in plan_items
            for publication in publications_by_item.get(plan_item.id, [])
        ]
        recommended_article = (campaign.strategy or {}).get("recommended_article")
        recommended = (
            isinstance(recommended_article, dict) and recommended_article.get("title") == item.title
        )
        return WorkspaceArticle(
            **reference.model_dump(),
            campaign_role="Рекомендована стратегией" if recommended else "Материал кампании",
            plan_item_count=len(plan_items),
            social_post_count=len({post.id for post in posts}),
            scheduled_publication_count=sum(
                publication.status is PublicationStatus.SCHEDULED for publication in publications
            ),
            published_count=sum(
                publication.status is PublicationStatus.PUBLISHED for publication in publications
            ),
        )

    @staticmethod
    def _task_summary(
        task: Task,
        run: AgentRun | None,
        plan_items: dict[UUID, PublicationPlanItem],
    ) -> WorkspaceTask:
        raw_plan_item_id = (task.input_data or {}).get("publication_plan_item_id")
        try:
            plan_item_id = UUID(str(raw_plan_item_id)) if raw_plan_item_id else None
        except (TypeError, ValueError):
            plan_item_id = None
        plan_item = plan_items.get(plan_item_id) if plan_item_id else None
        display_title = (
            f"Создать {plan_item.channel.value}-пост: «{plan_item.topic}»"
            if plan_item
            else task.title
        )
        error_code = run.error_code if run else None
        if task.status is TaskStatus.BLOCKED:
            error_summary = "Задача ожидает выполнения предыдущего шага."
            next_action = "Откройте задачу и проверьте её зависимости."
        elif task.status is TaskStatus.FAILED:
            error_summary, next_action = CampaignWorkspaceService._TASK_ERRORS.get(
                error_code or "",
                (
                    "Задача не завершилась. Подробности доступны в карточке задачи.",
                    "Откройте задачу и проверьте технические сведения.",
                ),
            )
        else:
            error_summary = None
            next_action = None
        return WorkspaceTask(
            id=task.id,
            title=task.title,
            display_title=display_title,
            task_type=task.task_type,
            status=task.status,
            priority=task.priority,
            created_at=task.created_at,
            updated_at=task.updated_at,
            plan_item_id=plan_item_id,
            error_code=error_code,
            error_summary=error_summary,
            next_action=next_action,
            retry_allowed=(
                task.status is TaskStatus.FAILED and task.retry_count < settings.agent_max_retries
            ),
        )

    async def _knowledge_state(
        self, campaign: Campaign, tasks: dict[UUID, Task]
    ) -> WorkspaceKnowledgeState:
        counts = {
            status: int(count)
            for status, count in (
                await self.session.execute(
                    select(KnowledgeItem.status, func.count(KnowledgeItem.id))
                    .where(KnowledgeItem.archived_at.is_(None))
                    .group_by(KnowledgeItem.status)
                )
            ).all()
        }
        source_count = int(await self.session.scalar(select(func.count(KnowledgeSource.id))) or 0)
        store = await KnowledgeRepository(self.session).active_store()
        packs = await KnowledgePackRepository(self.session).list_packs(campaign_id=campaign.id)
        current_pack = next(
            (
                pack
                for pack in packs
                if pack.strategy_version == campaign.strategy_version
                and pack.status is KnowledgePackStatus.READY
            ),
            None,
        )
        workspace_packs = [
            WorkspaceKnowledgePack(
                id=pack.id,
                status=pack.status,
                task_id=pack.task_id,
                task_title=tasks[pack.task_id].title if pack.task_id in tasks else None,
                agent_run_id=pack.agent_run_id,
                created_at=pack.created_at,
                summary=pack.summary,
                gaps=pack.gaps,
                source_count=len(pack.items),
            )
            for pack in packs
        ]
        return WorkspaceKnowledgeState(
            store_ready=bool(store and store.status.value == "ACTIVE"),
            source_count=source_count,
            item_count=sum(counts.values()),
            ready_item_count=counts.get(KnowledgeItemStatus.READY, 0),
            processing_item_count=(
                counts.get(KnowledgeItemStatus.INDEXING, 0)
                + counts.get(KnowledgeItemStatus.UPLOADING, 0)
            ),
            failed_item_count=counts.get(KnowledgeItemStatus.FAILED, 0),
            campaign_packs=workspace_packs,
            has_current_strategy_pack=current_pack is not None,
        )

    async def _feedback_state(self, campaign_id: UUID) -> WorkspaceFeedbackState:
        latest_accepted = await self.session.scalar(
            select(func.max(MarketingFeedbackAnalysis.generated_at)).where(
                MarketingFeedbackAnalysis.campaign_id == campaign_id,
                MarketingFeedbackAnalysis.status == FeedbackAnalysisStatus.ACCEPTED,
            )
        )
        feedback_query = select(func.count(MarketingFeedback.id)).where(
            MarketingFeedback.campaign_id == campaign_id
        )
        metrics_query = (
            select(func.count(PublicationMetricsSnapshot.id))
            .join(
                Publication,
                Publication.id == PublicationMetricsSnapshot.publication_id,
            )
            .where(Publication.campaign_id == campaign_id)
        )
        if latest_accepted is not None:
            feedback_query = feedback_query.where(MarketingFeedback.created_at > latest_accepted)
            metrics_query = metrics_query.where(
                PublicationMetricsSnapshot.observed_at > latest_accepted
            )
        feedback_count = int(await self.session.scalar(feedback_query) or 0)
        metric_count = int(await self.session.scalar(metrics_query) or 0)
        return WorkspaceFeedbackState(
            new_feedback_count=feedback_count,
            new_metrics_count=metric_count,
        )

    def _director(
        self,
        campaign: Campaign,
        strategy_status: ApprovalStatus | CampaignStatus,
        knowledge: WorkspaceKnowledgeState,
        articles: list[WorkspaceArticle],
        social_posts: list[ContentItem],
        current_plan: PublicationPlan | None,
        posts_by_item: dict[UUID, list[ContentItem]],
        publications: list[Publication],
        tasks: list[Task],
        feedback: WorkspaceFeedbackState,
    ) -> CampaignDirectorBrief:
        active_items = (
            [
                item
                for item in current_plan.items
                if item.status is PublicationPlanItemStatus.PLANNED
            ]
            if current_plan
            else []
        )
        items_with_posts = sum(bool(posts_by_item.get(item.id)) for item in active_items)
        failed_tasks = [task for task in tasks if task.status is TaskStatus.FAILED]
        blocked_tasks = [task for task in tasks if task.status is TaskStatus.BLOCKED]
        waiting_posts = [
            post for post in social_posts if post.status is ContentStatus.WAITING_APPROVAL
        ]
        scheduled = [
            publication
            for publication in publications
            if publication.status is PublicationStatus.SCHEDULED
        ]
        published = [
            publication
            for publication in publications
            if publication.status is PublicationStatus.PUBLISHED
        ]
        next_step = self._next_step(
            campaign,
            strategy_status,
            knowledge,
            articles,
            current_plan,
            active_items,
            posts_by_item,
            publications,
            tasks,
            feedback,
        )
        return CampaignDirectorBrief(
            strategy_status=strategy_status,
            article_count=len(articles),
            approved_article_count=sum(
                article.status is ContentStatus.APPROVED for article in articles
            ),
            plan_status=current_plan.status if current_plan else None,
            plan_item_count=len(active_items),
            plan_items_with_posts=items_with_posts,
            plan_items_without_posts=max(0, len(active_items) - items_with_posts),
            social_post_count=len(social_posts),
            posts_waiting_approval=len(waiting_posts),
            scheduled_publication_count=len(scheduled),
            published_count=len(published),
            failed_task_count=len(failed_tasks),
            blocked_task_count=len(blocked_tasks),
            next_step=next_step,
        )

    def _next_step(
        self,
        campaign: Campaign,
        strategy_status: ApprovalStatus | CampaignStatus,
        knowledge: WorkspaceKnowledgeState,
        articles: list[WorkspaceArticle],
        current_plan: PublicationPlan | None,
        active_items: list[PublicationPlanItem],
        posts_by_item: dict[UUID, list[ContentItem]],
        publications: list[Publication],
        tasks: list[Task],
        feedback: WorkspaceFeedbackState,
    ) -> WorkspaceNextStep:
        campaign_url = f"/campaigns/{campaign.id}"
        issue = next(
            (task for task in tasks if task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}),
            None,
        )
        if issue:
            return WorkspaceNextStep(
                title=(
                    "Разобрать ошибку задачи"
                    if issue.status is TaskStatus.FAILED
                    else "Снять блокировку задачи"
                ),
                description=issue.title,
                href=f"/tasks/{issue.id}",
                entity_type="task",
                entity_id=issue.id,
            )
        if campaign.status is CampaignStatus.DRAFT and campaign.strategy is None:
            return WorkspaceNextStep(
                title="Подготовить стратегию кампании",
                description="Стратегию можно сформировать отдельным действием и затем проверить.",
                href=f"{campaign_url}#strategy",
                entity_type="campaign",
                entity_id=campaign.id,
            )
        if (
            strategy_status is ApprovalStatus.PENDING
            or campaign.status is CampaignStatus.WAITING_APPROVAL
        ):
            return WorkspaceNextStep(
                title="Проверить стратегию",
                description="Стратегия ожидает решения человека.",
                href=f"{campaign_url}#strategy",
                entity_type="campaign",
                entity_id=campaign.id,
            )
        if not knowledge.has_current_strategy_pack:
            return WorkspaceNextStep(
                title="Проверить базу знаний кампании",
                description="Для текущей версии стратегии ещё нет готового пакета знаний.",
                href="/knowledge",
                entity_type="knowledge",
            )
        waiting_article = next(
            (article for article in articles if article.status is ContentStatus.WAITING_APPROVAL),
            None,
        )
        if waiting_article:
            return WorkspaceNextStep(
                title="Согласовать статью",
                description=waiting_article.title,
                href=f"/content/{waiting_article.id}#approval",
                entity_type="article",
                entity_id=waiting_article.id,
            )
        if current_plan is None:
            return WorkspaceNextStep(
                title="Создать медиаплан",
                description=(
                    "Система предложит расписание по утверждённым статьям; "
                    "план нужно проверить и утвердить."
                ),
                href=f"{campaign_url}#publication-plan",
                entity_type="campaign",
                entity_id=campaign.id,
            )
        if current_plan.status is PublicationPlanStatus.WAITING_APPROVAL:
            return WorkspaceNextStep(
                title="Проверить медиаплан",
                description="Медиаплан ожидает решения человека.",
                href=f"{campaign_url}#publication-plan",
                entity_type="publication_plan",
                entity_id=current_plan.id,
            )
        if current_plan.status is PublicationPlanStatus.APPROVED:
            missing_post = next(
                (item for item in active_items if not posts_by_item.get(item.id)),
                None,
            )
            if missing_post:
                return WorkspaceNextStep(
                    title=(
                        f"Создать {missing_post.channel.value}-пост "
                        f"для пункта №{missing_post.position}"
                    ),
                    description=f"{missing_post.topic} · {missing_post.scheduled_at:%d.%m.%Y}",
                    href=f"{campaign_url}#plan-item-{missing_post.id}",
                    entity_type="publication_plan_item",
                    entity_id=missing_post.id,
                )
        waiting_post = next(
            (
                post
                for item in active_items
                for post in posts_by_item.get(item.id, [])
                if post.status is ContentStatus.WAITING_APPROVAL
            ),
            None,
        )
        if waiting_post:
            return WorkspaceNextStep(
                title="Согласовать пост",
                description=waiting_post.title,
                href=f"/content/{waiting_post.id}#approval",
                entity_type="social_post",
                entity_id=waiting_post.id,
            )
        approved_post_without_publication = next(
            (
                post
                for item in active_items
                for post in posts_by_item.get(item.id, [])
                if post.status is ContentStatus.APPROVED
                and not any(pub.content_item_id == post.id for pub in publications)
            ),
            None,
        )
        if approved_post_without_publication:
            return WorkspaceNextStep(
                title="Запланировать публикацию",
                description=approved_post_without_publication.title,
                href=f"/content/{approved_post_without_publication.id}#publication",
                entity_type="social_post",
                entity_id=approved_post_without_publication.id,
            )
        if any(pub.status is PublicationStatus.SCHEDULED for pub in publications):
            return WorkspaceNextStep(
                title="Публикация уже запланирована",
                description="Следите за расписанием и фактом доставки в разделе публикаций.",
                href="/publications#upcoming",
                entity_type="publication",
            )
        if feedback.new_feedback_count or feedback.new_metrics_count:
            return WorkspaceNextStep(
                title="Разобрать новые результаты",
                description=(
                    f"Новые отзывы: {feedback.new_feedback_count}; "
                    f"новых наблюдений метрик: {feedback.new_metrics_count}."
                ),
                href=f"{campaign_url}#feedback",
                entity_type="feedback",
            )
        return WorkspaceNextStep(
            title="Кампания работает штатно",
            description="Откройте медиаплан или расписание, чтобы продолжить контроль.",
            href=f"{campaign_url}#publication-plan",
            entity_type="campaign",
            entity_id=campaign.id,
        )
