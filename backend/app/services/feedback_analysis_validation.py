"""Application-authored constraints and safe diagnostics, never exception prose."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agents import ModelBehaviorError
from pydantic import ValidationError

from app.core.errors import AppError
from app.models.marketing_experiment import ExperimentMetric
from app.services.optimization_proposal_service import ACTION_TARGET_TYPES, EVIDENCE_SNAPSHOT_FIELDS
from app.services.performance_evidence import canonical_json


class ValidationCode(StrEnum):
    SCHEMA_INVALID = "SCHEMA_INVALID"
    INTERPRETATION_INDEX_INVALID = "INTERPRETATION_INDEX_INVALID"
    EVIDENCE_REF_INVALID = "EVIDENCE_REF_INVALID"
    OPTIMIZATION_TARGET_INVALID = "OPTIMIZATION_TARGET_INVALID"
    EXPERIMENT_SPEC_INVALID = "EXPERIMENT_SPEC_INVALID"
    MODEL_BEHAVIOR_INVALID = "MODEL_BEHAVIOR_INVALID"


@dataclass(frozen=True)
class SafeValidationFailure:
    validation_code: ValidationCode
    validation_stage: str


REPAIR_INSTRUCTIONS = {
    ValidationCode.SCHEMA_INVALID: (
        "Return only fields defined by FeedbackAnalystResult; no additional fields. "
        "Use the exact schema types."
    ),
    ValidationCode.INTERPRETATION_INDEX_INVALID: (
        "supporting_findings must contain zero-based indexes that exist "
        "in the findings array you return."
    ),
    ValidationCode.EVIDENCE_REF_INVALID: (
        "Previous response referenced evidence outside the frozen allowlist. "
        "Use only the exact IDs listed in Allowed evidence."
    ),
    ValidationCode.OPTIMIZATION_TARGET_INVALID: (
        "Previous proposed_action used an invalid target or version. "
        "Choose only an exact target/version pair from optimization_target_allowlist. "
        "Do not infer a target from text."
    ),
    ValidationCode.EXPERIMENT_SPEC_INVALID: (
        "EXPERIMENT requires experiment_spec. Only EXPERIMENT may include it. "
        "success_metric must be one of " + "/".join(ExperimentMetric) + "."
    ),
    ValidationCode.MODEL_BEHAVIOR_INVALID: (
        "Return a single structured FeedbackAnalystResult "
        "matching the schema and frozen allowlists."
    ),
}


def classify_feedback_analysis_validation_error(error: Exception) -> SafeValidationFailure:
    if isinstance(error, AppError):
        code = (
            ValidationCode.EVIDENCE_REF_INVALID
            if error.code == "FEEDBACK_EVIDENCE_INVALID"
            or (
                error.code == "OPTIMIZATION_ACTION_INVALID"
                and error.details.get("validation_code") == "EVIDENCE_REF_INVALID"
            )
            else ValidationCode.OPTIMIZATION_TARGET_INVALID
            if error.code == "OPTIMIZATION_ACTION_INVALID"
            else ValidationCode.SCHEMA_INVALID
        )
        return SafeValidationFailure(code, "APPLICATION")
    if isinstance(error, ModelBehaviorError):
        return SafeValidationFailure(ValidationCode.MODEL_BEHAVIOR_INVALID, "MODEL_OUTPUT")
    if isinstance(error, ValidationError):
        errors = error.errors(include_input=False, include_context=False, include_url=False)
        if any(
            e["type"] == "interpretation_index_invalid" or "supporting_findings" in e["loc"]
            for e in errors
        ):
            return SafeValidationFailure(ValidationCode.INTERPRETATION_INDEX_INVALID, "SCHEMA")
        if any(
            e["type"] == "experiment_spec_invalid" or "experiment_spec" in e["loc"] for e in errors
        ):
            return SafeValidationFailure(ValidationCode.EXPERIMENT_SPEC_INVALID, "SCHEMA")
    return SafeValidationFailure(ValidationCode.SCHEMA_INVALID, "SCHEMA")


def feedback_analysis_constraints(snapshot: dict[str, Any]) -> str:
    allowed = snapshot.get("optimization_target_allowlist", {})
    campaign = allowed.get("campaign", {})
    campaign_id = snapshot["campaign_id"]
    actions: dict[str, Any] = {}
    for kind, entity in ACTION_TARGET_TYPES.items():
        if kind == "CONTENT_REVISION":
            targets = [
                {
                    "target_entity_id": row["content_item_id"],
                    "target_version_id": row["content_version_id"],
                }
                for row in allowed.get("content", [])
                if row["campaign_id"] == campaign_id
            ]
        elif kind == "PUBLICATION_PLAN_REVISION":
            targets = [
                {"target_entity_id": row["id"], "target_version_id": None}
                for row in allowed.get("publication_plans", [])
                if row["campaign_id"] == campaign_id
            ]
        else:
            targets = [{"target_entity_id": campaign_id, "target_version_id": None}]
        actions[kind] = {
            "type": kind,
            "target_entity_type": entity,
            "targets": targets,
            "experiment_spec": "required" if kind == "EXPERIMENT" else None,
        }
    return (
        "\nApplication constraints (server validation remains authoritative):\n"
        + canonical_json(
            {
                "campaign_id": campaign_id,
                "strategy_version": snapshot["strategy_version"],
                "allowlist_campaign": campaign,
                "Allowed evidence": {
                    kind: snapshot.get(key, []) for kind, key in EVIDENCE_SNAPSHOT_FIELDS.items()
                },
                "Allowed actions": actions,
                "experiment_success_metrics": list(ExperimentMetric),
            }
        )
        + "\nsupporting_findings uses zero-based indexes into the findings array you return. "
        "Return only FeedbackAnalystResult fields and types, no additional fields. "
        "If no justified executable change exists, use a valid NO_CHANGE or recommendations = []. "
        "Do not invent targets. NO_CHANGE requires experiment_spec = null."
    )


def feedback_analysis_repair_instruction(failure: SafeValidationFailure) -> str:
    return (
        "\nRepair category: "
        + failure.validation_code
        + "\n"
        + REPAIR_INSTRUCTIONS[failure.validation_code]
    )
