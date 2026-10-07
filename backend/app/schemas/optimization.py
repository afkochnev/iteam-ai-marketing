from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.optimization import (
    OptimizationActionStatus,
    OptimizationActionType,
    OptimizationProposalStatus,
    OptimizationTargetEntityType,
)
from app.schemas.feedback import FeedbackEvidenceRef


class OptimizationActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    position: int
    type: OptimizationActionType
    target_entity_type: OptimizationTargetEntityType
    target_entity_id: UUID
    target_version_id: UUID | None
    reason: str
    expected_effect: str
    priority: str
    evidence_refs: list[FeedbackEvidenceRef]
    status: OptimizationActionStatus


class OptimizationProposalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    campaign_id: UUID
    feedback_analysis_id: UUID
    strategy_version: int
    status: OptimizationProposalStatus
    summary: str
    created_by_agent_run_id: UUID | None
    reviewed_by_user_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    actions: list[OptimizationActionResponse]
