from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.models.marketing_experiment import ExperimentMetric
from app.models.marketing_feedback import (
    FeedbackAnalysisStatus,
    FeedbackCategory,
)
from app.models.optimization import OptimizationActionType, OptimizationTargetEntityType


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publication_id: UUID | None = None
    content_item_id: UUID | None = None
    content_version_id: UUID | None = None
    category: FeedbackCategory
    rating: int | None = Field(default=None, ge=1, le=5)
    comment: str = Field(min_length=1, max_length=4000)

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        return value.strip()


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    publication_id: UUID | None
    content_item_id: UUID | None
    content_version_id: UUID | None
    source_type: str
    category: FeedbackCategory
    rating: int | None
    comment: str | None
    observed_at: datetime | None
    created_by_user_id: UUID | None
    created_at: datetime


class FeedbackAnalysisResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    campaign_id: UUID
    status: FeedbackAnalysisStatus
    strategy_version: int
    summary: str
    input_snapshot: dict[str, object]
    findings: list[dict[str, object]]
    recommendations: list[dict[str, object]]
    experiment_ideas: list[dict[str, object]]
    limitations: list[str]
    agent_run_id: UUID | None
    generated_at: datetime | None
    reviewed_by_user_id: UUID | None
    reviewed_at: datetime | None


class FeedbackEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["publication", "content_version", "metrics_snapshot", "marketing_feedback"]
    id: UUID


class FeedbackFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1, max_length=80)
    observation: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[FeedbackEvidenceRef] = Field(max_length=8)
    confidence: str = Field(min_length=1, max_length=40)


class OptimizationExperimentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypothesis: str = Field(min_length=1, max_length=1000)
    proposed_change: str = Field(min_length=1, max_length=1000)
    success_metric: ExperimentMetric
    minimum_observation_requirement: str | None = Field(default=None, max_length=1000)


class OptimizationActionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: OptimizationActionType
    target_entity_type: OptimizationTargetEntityType
    target_entity_id: UUID
    target_version_id: UUID | None
    reason: str = Field(min_length=1, max_length=2000)
    expected_effect: str = Field(min_length=1, max_length=1000)
    priority: str = Field(min_length=1, max_length=40)
    evidence_refs: list[FeedbackEvidenceRef] = Field(max_length=8)
    experiment_spec: OptimizationExperimentSpec | None = None

    @model_validator(mode="after")
    def validate_experiment_spec(self, info: ValidationInfo) -> "OptimizationActionDraft":
        if self.type is OptimizationActionType.EXPERIMENT:
            if self.experiment_spec is None and not (info.context or {}).get("legacy_experiment"):
                raise ValueError("EXPERIMENT requires experiment_spec")
        elif self.experiment_spec is not None:
            raise ValueError("Only EXPERIMENT may include experiment_spec")
        return self


class FeedbackRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposed_action: OptimizationActionDraft

    category: str = Field(min_length=1, max_length=80)
    recommendation: str = Field(min_length=1, max_length=2000)
    evidence_refs: list[FeedbackEvidenceRef] = Field(max_length=8)
    expected_effect: str = Field(min_length=1, max_length=1000)
    priority: str = Field(min_length=1, max_length=40)


class FeedbackExperiment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1, max_length=1000)
    proposed_change: str = Field(min_length=1, max_length=1000)
    success_metric: str = Field(min_length=1, max_length=500)


class FeedbackAnalystResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4000)
    findings: list[FeedbackFinding] = Field(max_length=12)
    recommendations: list[FeedbackRecommendation] = Field(max_length=12)
    experiment_ideas: list[FeedbackExperiment] = Field(max_length=8)
    limitations: list[str] = Field(max_length=20)
