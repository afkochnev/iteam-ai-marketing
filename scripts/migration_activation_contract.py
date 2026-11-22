"""Pure contracts for an explicitly authorized migration-and-activation release.

The signed-image rehearsal remains disposable evidence. Passing these checks
does not authenticate a workflow, verify an image or grant production authority.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import migration_release_drill as migration
import release_ops as ops
from migration_rehearsal_proof import validate_rehearsal_proof
from release_manifest import ManifestContractError

REQUEST_TYPE = "MIGRATION_ACTIVATION_REHEARSAL_REQUEST"
PROOF_TYPE = "MIGRATION_ACTIVATION_REHEARSAL"
FIXTURE_AUTHORITY = "DISPOSABLE_SIGNED_ACTIVATION_ONLY"
APPROVAL_TTL_SECONDS = 3600
ADMISSION_PROTOCOL = 1
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OPERATION = re.compile(r"[0-9a-f]{32}\Z")

ERROR_CODES = frozenset({
    "INVALID_ACTIVATION_POLICY", "INVALID_ACTIVATION_REQUEST",
    "INVALID_ACTIVATION_PROOF", "ACTIVATION_PROOF_SCOPE_MISMATCH",
    "ACTIVATION_PROOF_POLICY_MISMATCH", "ACTIVATION_PROOF_CHECKS_FAILED",
    "ACTIVATION_PROOF_UNSAFE_RUNTIME", "ACTIVATION_PROOF_TIME_INVALID",
    "ACTIVATION_PROOF_DATA_MISMATCH", "ACTIVATION_PROOF_IDENTITY_MISMATCH",
})


def activation_policy() -> dict[str, Any]:
    """The only supported production policy; callers receive independent data."""
    return {
        "version": 1,
        "admission_protocol": ADMISSION_PROTOCOL,
        "activation": "COLD_RECREATE_AFTER_DURABLE_BOUNDARY",
        "pre_activation_boundary": "SAME_LIVE_OPERATION",
        "services": sorted(ops.APP),
        "business_writes_authorized": True,
        "claims_authorized": True,
        "openai_requests_authorized": True,
        "publication_requests_authorized": False,
        "metrics_requests_authorized": False,
        "automatic_retry": False,
        "post_activation_schema_downgrade": False,
        "post_activation_backup_restore": False,
        "failure_policy": "STOP_FENCE_PRESERVE_TARGET",
        "successful_restart_policy": "RESTORE_BOUND_POLICIES_BEFORE_SUCCESS",
    }


def same_json(actual: Any, expected: Any) -> bool:
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return set(actual) == set(expected) and all(
            same_json(actual[key], value) for key, value in expected.items()
        )
    if type(expected) is list:
        return len(actual) == len(expected) and all(
            same_json(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def hash_json(value: Any) -> str:
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def validate_activation_policy(value: Any) -> None:
    ops.require(same_json(value, activation_policy()), "INVALID_ACTIVATION_POLICY")


def validate_activation_request(value: Any) -> None:
    ops.require(
        type(value) is dict and set(value) == {
            "version", "request_type", "repository", "migration_request", "activation_policy",
        }
        and type(value["version"]) is int and value["version"] == 1
        and value["request_type"] == REQUEST_TYPE and value["repository"] == ops.REPO,
        "INVALID_ACTIVATION_REQUEST",
    )
    validate_activation_policy(value["activation_policy"])
    try:
        migration.validate_rehearsal_request(value["migration_request"])
    except (ops.ReleaseError, ManifestContractError, KeyError, TypeError, ValueError):
        raise ops.ReleaseError("INVALID_ACTIVATION_REQUEST") from None


def migration_request(value: Any) -> dict[str, Any]:
    validate_activation_request(value)
    return copy.deepcopy(value["migration_request"])


def activation_scope(value: Any) -> dict[str, Any]:
    request = migration_request(value)
    return {
        **migration.rehearsal_scope(request),
        "admission_protocol": ADMISSION_PROTOCOL,
        "activation_policy": activation_policy(),
        "request_sha256": hash_json(value),
    }


def _timestamp(value: Any) -> datetime:
    ops.require(type(value) is str and len(value) <= 64, "ACTIVATION_PROOF_TIME_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        ops.require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
                    "ACTIVATION_PROOF_TIME_INVALID")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ops.ReleaseError("ACTIVATION_PROOF_TIME_INVALID") from None


_TARGET_TABLES = frozenset("""
activity_logs agent_runs agent_tools agents approvals campaign_kpis
campaign_optimization_actions campaign_optimization_proposals campaigns
content_derivations content_items content_version_sources content_versions
knowledge_items knowledge_pack_items knowledge_packs knowledge_sources
knowledge_stores marketing_context_snapshots marketing_conversations
marketing_experiment_publications marketing_experiments marketing_feedback
marketing_feedback_analyses marketing_messages publication_metrics_snapshots
publication_plan_items publication_plans publication_reconciliations
publications task_dependencies tasks tool_calls users alembic_version
workspaces workspace_memberships
""".split())
_PASSIVE_MATRIX = frozenset({
    "authenticated_api_write", "stale_chat_get", "direct_provider_producer_session",
    "worker_ai", "worker_ai_control", "worker_publication", "worker_publication_control",
    "worker_metrics", "worker_unset", "scheduler_ai", "scheduler_publication",
})
_PASSIVE_FIELDS = {
    "status", "project", "operation_id", "matrix", "business_writes_blocked", "claims_blocked",
    "provider_connections", "provider_witness_selftest_connections", "all_tables_sha256", "table_counts",
    "database_identity_sha256", "schema", "broker_snapshot_sha256", "broker_keys", "redis_witness",
    "real_broker_queued_canaries", "fixture_kind", "business_ready", "runtime_stopped_before_observation_closed",
}
_SCENARIO_FIELDS = {
    "status", "operation_id", "project", "pre_activation_status", "pre_activation_writer_boundary",
    "pre_activation_business_preserved", "database_identity_sha256", "target_plan_sha256",
    "activation_phase", "activation_boundary_recorded", "state_durability", "admission_protocol",
    "target_services_verified", "cold_recreation_verified", "source_runtime_restarted", "automatic_retry",
    "post_activation_downgrade_attempts", "post_activation_restore_attempts", "cleanup_verified",
}
_FAILURE_FIELDS = {
    "injected_failure", "http_write_committed", "real_worker_claim_committed", "committed_canary_count",
    "committed_all_tables_sha256", "contained_all_tables_sha256", "target_schema_preserved",
    "runtime_stopped", "new_claims_blocked", "database_fenced", "postgres_host_port_blocked",
}
_PROOF_FIELDS = {
    "proof_contract_version", "proof_type", "repository", "authorization", "status",
    "controller_sha", "controller_tree", "migration_rehearsal", "migration_target_passive",
    "adversarial_passive", "success", "failure", "created_at", "production_approval_verified",
    "production_credentials", "application_external_network", "postgres_has_host_publish_network",
    "publishing_enabled", "disposable_projects_removed", "temporary_backups_and_keys_removed", "observers_closed",
}


def _digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _exact(value: Any, fields: set[str] | frozenset[str]) -> None:
    ops.require(type(value) is dict and set(value) == fields, "INVALID_ACTIVATION_PROOF")


def _identity(value: dict[str, Any], prefix: str) -> None:
    ops.require(
        type(value["operation_id"]) is str and _OPERATION.fullmatch(value["operation_id"])
        and type(value["project"]) is str and re.fullmatch(re.escape(prefix) + r"[0-9a-f]{10}", value["project"]),
        "ACTIVATION_PROOF_IDENTITY_MISMATCH",
    )


def _validate_passive(value: Any, request: dict[str, Any]) -> None:
    _exact(value, {"target_image_id", "candidate_sha", "passive", "cleanup_verified"})
    ops.require(_digest(value["target_image_id"]) and value["candidate_sha"] == request["target_release_sha"]
                and value["cleanup_verified"] is True, "ACTIVATION_PROOF_IDENTITY_MISMATCH")
    passive = value["passive"]
    _exact(passive, _PASSIVE_FIELDS)
    _identity(passive, "iteam-admission-drill-")
    ops.require(
        passive["status"] == "PASS" and passive["fixture_kind"] == "ADVERSARIAL_TARGET_SCHEMA"
        and passive["schema"] == request["target_schema_head"]
        and passive["business_ready"] is False
        and all(passive[key] is True for key in (
            "business_writes_blocked", "claims_blocked", "runtime_stopped_before_observation_closed",
        ))
        and type(passive["matrix"]) is dict and set(passive["matrix"]) == _PASSIVE_MATRIX
        and all(item == "BLOCKED_RUNTIME_PASSIVE" for item in passive["matrix"].values()),
        "ACTIVATION_PROOF_CHECKS_FAILED",
    )
    ops.require(
        type(passive["provider_connections"]) is int and passive["provider_connections"] == 0
        and type(passive["provider_witness_selftest_connections"]) is int
        and passive["provider_witness_selftest_connections"] == 4
        and type(passive["real_broker_queued_canaries"]) is int and passive["real_broker_queued_canaries"] == 7
        and type(passive["broker_keys"]) is int and passive["broker_keys"] > 0,
        "ACTIVATION_PROOF_UNSAFE_RUNTIME",
    )
    for key in ("all_tables_sha256", "database_identity_sha256", "broker_snapshot_sha256"):
        ops.require(_digest(passive[key]), "ACTIVATION_PROOF_DATA_MISMATCH")
    counts = passive["table_counts"]
    ops.require(type(counts) is dict and set(counts) == _TARGET_TABLES
                and all(type(count) is int and count >= 0 for count in counts.values())
                and all(counts[key] > 0 for key in (
                    "users", "agent_runs", "campaigns", "publications", "workspaces", "workspace_memberships",
                )), "ACTIVATION_PROOF_DATA_MISMATCH")
    witness = passive["redis_witness"]
    _exact(witness, {"continuous_monitor_verified", "allowed_readiness_commands", "forbidden_commands", "queue_activity_observed"})
    ops.require(
        witness["continuous_monitor_verified"] is True and witness["queue_activity_observed"] is False
        and type(witness["allowed_readiness_commands"]) is int and witness["allowed_readiness_commands"] > 0
        and type(witness["forbidden_commands"]) is int and witness["forbidden_commands"] == 0,
        "ACTIVATION_PROOF_UNSAFE_RUNTIME",
    )


def _validate_scenario(value: Any, *, failure: bool) -> None:
    _exact(value, _SCENARIO_FIELDS | (_FAILURE_FIELDS if failure else set()))
    _identity(value, "iteam-activation-drill-")
    ops.require(
        value["status"] == "PASS" and value["pre_activation_status"] == "PRE_ACTIVATION_READY"
        and value["pre_activation_writer_boundary"] == "PROVEN_CLOSED"
        and value["activation_phase"] == ("CONTAINED" if failure else "ACTIVE")
        and value["state_durability"] == "CONFIRMED"
        and type(value["admission_protocol"]) is int and value["admission_protocol"] == 1
        and type(value["target_services_verified"]) is int and value["target_services_verified"] == len(ops.APP)
        and all(value[key] is True for key in (
            "pre_activation_business_preserved", "activation_boundary_recorded", "cold_recreation_verified", "cleanup_verified",
        ))
        and value["source_runtime_restarted"] is False and value["automatic_retry"] is False,
        "ACTIVATION_PROOF_CHECKS_FAILED",
    )
    ops.require(
        all(type(value[key]) is int and value[key] == 0 for key in (
            "post_activation_downgrade_attempts", "post_activation_restore_attempts",
        )), "ACTIVATION_PROOF_UNSAFE_RUNTIME",
    )
    ops.require(_digest(value["database_identity_sha256"]) and _digest(value["target_plan_sha256"]),
                "ACTIVATION_PROOF_DATA_MISMATCH")
    if failure:
        ops.require(
            value["injected_failure"] == "INJECTED_POST_ACTIVATION_FAILURE"
            and all(value[key] is True for key in (
                "http_write_committed", "real_worker_claim_committed", "target_schema_preserved", "runtime_stopped",
                "new_claims_blocked", "database_fenced", "postgres_host_port_blocked",
            ))
            and type(value["committed_canary_count"]) is int and value["committed_canary_count"] == 1
            and _digest(value["committed_all_tables_sha256"])
            and value["contained_all_tables_sha256"] == value["committed_all_tables_sha256"],
            "ACTIVATION_PROOF_DATA_MISMATCH",
        )


def validate_activation_rehearsal_proof(
    proof: Any, request: dict[str, Any], *, run_started_at: str, run_updated_at: str,
    expected_controller_sha: str | None = None, expected_controller_tree: str | None = None,
) -> None:
    """Validate declared evidence; caller separately authenticates run/images."""
    scope = activation_scope(request)
    migration_request = request["migration_request"]
    _exact(proof, set(scope) | _PROOF_FIELDS)
    ops.require(
        type(proof["proof_contract_version"]) is int and proof["proof_contract_version"] == 1
        and proof["proof_type"] == PROOF_TYPE and proof["repository"] == ops.REPO
        and proof["authorization"] == FIXTURE_AUTHORITY and proof["status"] == "PASS",
        "INVALID_ACTIVATION_PROOF",
    )
    ops.require(all(same_json(proof[key], item) for key, item in scope.items()), "ACTIVATION_PROOF_SCOPE_MISMATCH")
    ops.require(
        type(proof["controller_sha"]) is str and re.fullmatch(r"[0-9a-f]{40}", proof["controller_sha"])
        and type(proof["controller_tree"]) is str and re.fullmatch(r"[0-9a-f]{40}", proof["controller_tree"])
        and (expected_controller_sha is None or proof["controller_sha"] == expected_controller_sha)
        and (expected_controller_tree is None or proof["controller_tree"] == expected_controller_tree),
        "ACTIVATION_PROOF_IDENTITY_MISMATCH",
    )
    ops.require(
        all(proof[key] is False for key in (
            "production_approval_verified", "production_credentials", "application_external_network", "publishing_enabled",
        ))
        and all(proof[key] is True for key in (
            "postgres_has_host_publish_network", "disposable_projects_removed", "temporary_backups_and_keys_removed", "observers_closed",
        )), "ACTIVATION_PROOF_UNSAFE_RUNTIME",
    )
    validate_rehearsal_proof(
        proof["migration_rehearsal"], migration_request,
        run_started_at=run_started_at, run_updated_at=run_updated_at,
    )
    ops.require(same_json(proof["migration_target_passive"], {
        "admission_protocol": 1, "admission_mode": "passive", "business_ready": False,
        "database_read_only": True, "build_sha": migration_request["target_release_sha"], "before_downgrade": True,
    }), "ACTIVATION_PROOF_CHECKS_FAILED")
    _validate_passive(proof["adversarial_passive"], migration_request)
    _validate_scenario(proof["success"], failure=False)
    _validate_scenario(proof["failure"], failure=True)
    identities = [proof["adversarial_passive"]["passive"], proof["success"], proof["failure"]]
    ops.require(all(len({item[key] for item in identities}) == 3 for key in ("operation_id", "project", "database_identity_sha256")),
                "ACTIVATION_PROOF_IDENTITY_MISMATCH")
    ops.require(
        _timestamp(run_started_at) <= _timestamp(proof["migration_rehearsal"]["created_at"])
        <= _timestamp(proof["created_at"]) <= _timestamp(run_updated_at),
        "ACTIVATION_PROOF_TIME_INVALID",
    )
