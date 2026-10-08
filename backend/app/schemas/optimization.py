from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.optimization import (
    OptimizationActionStatus,
    OptimizationActionType,
    OptimizationProposalStatus,
    OptimizationTargetEntityType,
)
from app.schemas.experiment import ExperimentConfiguration
from app.schemas.feedback import FeedbackEvidenceRef, OptimizationExperimentSpec


class OptimizationActionApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment: ExperimentConfiguration | None = None
    human_comment: str | None = Field(default=None, max_length=2000)


class OptimizationAppliedArtifact(BaseModel):
    artifact_type: Literal["TASK", "PUBLICATION_PLAN", "MARKETING_EXPERIMENT"]
    artifact_id: UUID
    experiment_id: UUID | None = None
    task_id: UUID | None = None
    publication_plan_id: UUID | None = None
    agent_run_id: UUID | None = None
    status: str
    href: str
    error_message: str | None = None


class OptimizationActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    position: int
    type: OptimizationActionType
    target_entity_type: OptimizationTargetEntityType
    target_entity_id: UUID
    target_version_id: UUID | None
    target_title: str | None = None
    target_version_number: int | None = None
    reason: str
    expected_effect: str
    priority: str
    experiment_spec: OptimizationExperimentSpec | None = None
    evidence_refs: list[FeedbackEvidenceRef]
    status: OptimizationActionStatus
    applied_by_user_id: UUID | None = None
    applied_at: datetime | None = None
    applied_artifact: OptimizationAppliedArtifact | None = None


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


class OptimizationActionApplyResponse(OptimizationAppliedArtifact):
    action: OptimizationActionResponse
