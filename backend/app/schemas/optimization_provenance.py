from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.optimization import OptimizationActionResponse


class ProvenanceNode(BaseModel):
    id: UUID
    type: str
    title: str
    status: str | None = None
    href: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ProvenanceAnalysis(BaseModel):
    id: UUID
    status: str
    strategy_version: int
    evidence_fingerprint: str | None
    analysis_period_start: str | None = None
    analysis_period_end: str | None = None
    data_quality: dict[str, Any] | None = None


class ProvenanceRecommendation(BaseModel):
    index: int
    text: str | None = None
    category: str | None = None
    expected_effect: str | None = None
    priority: str | None = None


class OptimizationProvenanceResponse(BaseModel):
    campaign: ProvenanceNode
    analysis: ProvenanceAnalysis
    source_recommendation: ProvenanceRecommendation
    proposal: ProvenanceNode
    action: OptimizationActionResponse
    resolved_evidence: list[ProvenanceNode]
    downstream_artifacts: list[ProvenanceNode]
    content_versions: list[ProvenanceNode]
    publications: list[ProvenanceNode]
    limitations: list[str]
