from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.models.approval import ApprovalStatus
from app.models.campaign import CampaignStatus
from app.models.content import ContentChannel, ContentStatus, ContentType
from app.models.knowledge_pack import KnowledgePackStatus
from app.models.publication import PublicationStatus
from app.models.publication_plan import PublicationPlanStatus
from app.models.task import TaskPriority, TaskStatus, TaskType
from app.schemas.campaign import CampaignResponse


class WorkspaceContentReference(BaseModel):
    id: UUID
    title: str
    status: ContentStatus
    content_type: ContentType
    current_version_id: UUID | None
    current_version_number: int | None
    approved_version_id: UUID | None
    approved_version_number: int | None
    source_task_id: UUID
    source_task_title: str | None
    source_task_status: TaskStatus | None
    parent_content_item_id: UUID | None
    publication_plan_item_id: UUID | None
    channel: ContentChannel | None


class WorkspacePublicationReference(BaseModel):
    id: UUID
    content_item_id: UUID
    content_version_id: UUID
    channel: ContentChannel
    status: PublicationStatus
    scheduled_at: datetime | None
    published_at: datetime | None
    publication_plan_item_id: UUID | None


class WorkspacePipelineStage(BaseModel):
    label: str
    status: str
    href: str | None = None
    action_label: str | None = None


class WorkspacePlanItem(BaseModel):
    id: UUID
    position: int
    scheduled_at: datetime
    channel: ContentChannel
    topic: str
    purpose: str
    format: str
    message_brief: str
    source_content_item_id: UUID
    source_content_item_title: str | None
    source_content_version_id: UUID
    source_version_number: int | None
    source_claim_ids: list[str] | None
    source_support_summary: str | None
    status: str
    social_posts: list[WorkspaceContentReference]
    publications: list[WorkspacePublicationReference]
    pipeline: list[WorkspacePipelineStage]


class WorkspacePlan(BaseModel):
    id: UUID
    status: PublicationPlanStatus
    planning_horizon_start: datetime
    planning_horizon_end: datetime
    timezone_policy: str
    generated_by_agent_run_id: UUID | None
    item_count: int
    items: list[WorkspacePlanItem]


class WorkspaceArticle(WorkspaceContentReference):
    campaign_role: str
    plan_item_count: int
    social_post_count: int
    scheduled_publication_count: int
    published_count: int


class WorkspaceKnowledgePack(BaseModel):
    id: UUID
    status: KnowledgePackStatus
    task_id: UUID
    task_title: str | None
    agent_run_id: UUID
    created_at: datetime
    summary: str
    gaps: list[str]
    source_count: int


class WorkspaceKnowledgeState(BaseModel):
    store_ready: bool
    source_count: int
    item_count: int
    ready_item_count: int
    processing_item_count: int
    failed_item_count: int
    campaign_packs: list[WorkspaceKnowledgePack]
    has_current_strategy_pack: bool


class WorkspaceTask(BaseModel):
    id: UUID
    title: str
    display_title: str
    task_type: TaskType
    status: TaskStatus
    priority: TaskPriority
    created_at: datetime
    updated_at: datetime
    plan_item_id: UUID | None
    error_code: str | None
    error_summary: str | None
    next_action: str | None
    retry_allowed: bool


class WorkspaceNextStep(BaseModel):
    title: str
    description: str
    href: str
    entity_type: str | None = None
    entity_id: UUID | None = None


class WorkspaceFeedbackState(BaseModel):
    new_feedback_count: int
    new_metrics_count: int


class CampaignDirectorBrief(BaseModel):
    strategy_status: ApprovalStatus | CampaignStatus
    article_count: int
    approved_article_count: int
    plan_status: PublicationPlanStatus | None
    plan_item_count: int
    plan_items_with_posts: int
    plan_items_without_posts: int
    social_post_count: int
    posts_waiting_approval: int
    scheduled_publication_count: int
    published_count: int
    failed_task_count: int
    blocked_task_count: int
    next_step: WorkspaceNextStep


class CampaignWorkspaceResponse(BaseModel):
    campaign: CampaignResponse
    director: CampaignDirectorBrief
    knowledge: WorkspaceKnowledgeState
    articles: list[WorkspaceArticle]
    social_posts: list[WorkspaceContentReference]
    publication_plan: WorkspacePlan | None
    other_plans: list[WorkspacePlan]
    publications: list[WorkspacePublicationReference]
    attention_tasks: list[WorkspaceTask]
    feedback: WorkspaceFeedbackState
