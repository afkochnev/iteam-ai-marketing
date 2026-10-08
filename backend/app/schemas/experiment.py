from datetime import datetime
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.models.marketing_experiment import (
    ExperimentMetric,
    ExperimentPublicationRole,
    ExperimentStatus,
)


class ExperimentConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_start: AwareDatetime
    baseline_end: AwareDatetime
    experiment_start: AwareDatetime
    experiment_end: AwareDatetime
    baseline_publication_ids: list[UUID] = Field(min_length=1, max_length=100)
    experiment_publication_ids: list[UUID] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_configuration(self) -> "ExperimentConfiguration":
        if (
            not self.baseline_start
            < self.baseline_end
            <= self.experiment_start
            < self.experiment_end
        ):
            raise ValueError("Периоды должны быть последовательными и не пересекаться.")
        baseline, experiment = self.baseline_publication_ids, self.experiment_publication_ids
        if len(set(baseline + experiment)) != len(baseline) + len(experiment):
            raise ValueError("Публикация может входить только в одну группу, один раз.")
        return self


class ExperimentCreate(ExperimentConfiguration):
    source_optimization_action_id: UUID


class ExperimentPublicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    publication_id: UUID
    content_version_id: UUID
    role: ExperimentPublicationRole


class ExperimentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    campaign_id: UUID
    source_optimization_action_id: UUID
    proposal_id: UUID
    feedback_analysis_id: UUID
    hypothesis: str
    proposed_change: str
    success_metric: ExperimentMetric
    minimum_observation_requirement: str | None
    baseline_start: datetime
    baseline_end: datetime
    experiment_start: datetime
    experiment_end: datetime
    status: ExperimentStatus
    result_summary: str | None
    limitations: list[str]
    result_data: dict[str, object]
    publications: list[ExperimentPublicationResponse]
    created_by_user_id: UUID
    approved_by_user_id: UUID | None
    approved_at: datetime | None
    cancelled_by_user_id: UUID | None
    cancelled_at: datetime | None
    created_at: datetime
    updated_at: datetime
