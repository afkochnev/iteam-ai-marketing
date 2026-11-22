"""Bounded, server-classified distribution cards from one database snapshot."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.models.campaign import CampaignStatus
from app.models.content import ContentChannel, ContentStatus
from app.models.publication import PublicationStatus

DistributionSection = Literal[
    "ready",
    "needs_plan",
    "overdue",
    "active",
    "upcoming",
    "attention",
    "cancelled",
    "cancelled_history",
    "published",
]
PublicationAction = Literal[
    "approve",
    "schedule",
    "replan",
    "cancel",
    "publish_now",
    "retry",
    "reconcile_published",
    "reconcile_not_published",
    "reuse",
    "change_channel",
    "batch_replan",
]
CandidateAction = Literal["reschedule_plan", "cancel_plan"]


class DistributionCampaign(BaseModel):
    id: UUID
    name: str
    status: CampaignStatus


class DistributionPublication(BaseModel):
    kind: Literal["publication"] = "publication"
    publication_id: UUID
    content_item_id: UUID
    content_version_id: UUID
    title: str
    campaign: DistributionCampaign
    channel: ContentChannel
    status: PublicationStatus
    scheduled_at: datetime | None
    published_at: datetime | None
    external_url: str | None
    publication_plan_item_id: UUID | None
    content_status: ContentStatus | None
    content_source_archived: bool
    provider_enabled: bool
    failure_code: str | None
    failure_message: str | None
    is_overdue: bool
    lateness_seconds: int
    sections: list[DistributionSection]
    has_replacement: bool
    allowed_actions: list[PublicationAction]


class DistributionCandidate(BaseModel):
    kind: Literal["candidate"] = "candidate"
    id: UUID
    title: str
    campaign: DistributionCampaign
    channel: ContentChannel | None
    plan_channel: ContentChannel | None
    plan_scheduled_at: datetime | None
    approved_version_id: UUID
    approved_version_number: int
    state: Literal["ready", "needs_plan", "overdue"]
    allowed_actions: list[CandidateAction]


DistributionItem = Annotated[
    DistributionPublication | DistributionCandidate, Field(discriminator="kind")
]


class DistributionPagination(BaseModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class DistributionCounts(BaseModel):
    """Counts describe this page only; overlapping sections are intentional."""

    ready: int = 0
    needs_plan: int = 0
    overdue: int = 0
    active: int = 0
    upcoming: int = 0
    attention: int = 0
    cancelled: int = 0
    cancelled_history: int = 0
    published: int = 0


class DistributionWorkspaceResponse(BaseModel):
    as_of: datetime
    campaign: DistributionCampaign | None
    items: list[DistributionItem]
    pagination: DistributionPagination
    counts: DistributionCounts
