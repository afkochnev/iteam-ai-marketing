"""Pure validation of a migration rehearsal proof's declared result.

The caller must separately authenticate the producing workflow and artifact,
verify the downloaded bytes' hash, and reverify the signed release images.
Passing this contract alone does not authorize any production operation.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from migration_business_fixture import REQUIRED_BUSINESS_TABLES
from migration_release_drill import rehearsal_scope, validate_rehearsal_request
from release_manifest import ManifestContractError, REPOSITORY
from release_ops import ReleaseError, require

ERROR_CODES = frozenset(
    {
        "INVALID_REHEARSAL_PROOF_REQUEST",
        "UNEXPECTED_REHEARSAL_PROOF_FIELDS",
        "INVALID_REHEARSAL_PROOF_IDENTITY",
        "REHEARSAL_PROOF_SCOPE_MISMATCH",
        "REHEARSAL_PROOF_POLICY_MISMATCH",
        "REHEARSAL_PROOF_REVISIONS_MISMATCH",
        "REHEARSAL_PROOF_CHECKS_FAILED",
        "REHEARSAL_PROOF_UNSAFE_RUNTIME",
        "REHEARSAL_PROOF_DATA_HASH_MISMATCH",
        "REHEARSAL_PROOF_DOWNGRADE_MISMATCH",
        "REHEARSAL_PROOF_COUNTS_INVALID",
        "REHEARSAL_PROOF_TIME_INVALID",
    }
)

_SUCCESS_FLAGS = frozenset(
    {
        "encrypted_backup_isolated_restore",
        "migration_executed_on_restored_database",
        "source_schema_verified",
        "target_schema_verified",
        "migration_specific_invariants",
        "business_data_hash_unchanged",
        "target_runtime_readiness",
        "disposable_project_removed",
        "temporary_backup_and_key_removed",
    }
)
_UNSAFE_FLAGS = frozenset(
    {
        "writers_activated",
        "external_network",
        "production_credentials",
        "publishing_enabled",
    }
)
_RESULT_FIELDS = {
    "status",
    "proof_contract_version",
    "repository",
    "compatibility_class",
    "pre_activation_rollback_policy",
    "migration_revisions",
    "encrypted_backup_sha256",
    "source_table_counts",
    "business_data_sha256",
    "pre_activation_downgrade_tested",
    "disposable_services",
    "created_at",
} | _SUCCESS_FLAGS | _UNSAFE_FLAGS
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TABLE = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")


def _same_json(actual: Any, expected: Any) -> bool:
    """Python's True == 1 must not broaden the JSON contract."""
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        return set(actual) == set(expected) and all(
            _same_json(actual[key], value) for key, value in expected.items()
        )
    if type(expected) is list:
        return len(actual) == len(expected) and all(
            _same_json(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def _timestamp(value: Any) -> datetime:
    require(
        type(value) is str and len(value) <= 64,
        "REHEARSAL_PROOF_TIME_INVALID",
    )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(
            parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "REHEARSAL_PROOF_TIME_INVALID",
        )
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ReleaseError("REHEARSAL_PROOF_TIME_INVALID") from None


def validate_rehearsal_proof(
    proof: Any,
    request: dict[str, Any],
    *,
    run_started_at: str,
    run_updated_at: str,
) -> None:
    """Require a complete PASS for exactly the reviewed rehearsal request."""
    try:
        validate_rehearsal_request(request)
        scope = rehearsal_scope(request)
    except (ReleaseError, ManifestContractError, KeyError, TypeError, ValueError):
        raise ReleaseError("INVALID_REHEARSAL_PROOF_REQUEST") from None
    require(
        type(proof) is dict and set(proof) == set(scope) | _RESULT_FIELDS,
        "UNEXPECTED_REHEARSAL_PROOF_FIELDS",
    )
    require(
        proof["status"] == "PASS"
        and type(proof["proof_contract_version"]) is int
        and proof["proof_contract_version"] == 1
        and proof["repository"] == REPOSITORY,
        "INVALID_REHEARSAL_PROOF_IDENTITY",
    )
    require(
        all(_same_json(proof[key], value) for key, value in scope.items()),
        "REHEARSAL_PROOF_SCOPE_MISMATCH",
    )
    require(
        all(
            _same_json(proof[key], request[key])
            for key in ("compatibility_class", "pre_activation_rollback_policy")
        ),
        "REHEARSAL_PROOF_POLICY_MISMATCH",
    )
    require(
        _same_json(
            proof["migration_revisions"],
            [step["revision"] for step in request["migration_path"]],
        ),
        "REHEARSAL_PROOF_REVISIONS_MISMATCH",
    )
    require(
        all(proof[key] is True for key in _SUCCESS_FLAGS),
        "REHEARSAL_PROOF_CHECKS_FAILED",
    )
    require(
        all(proof[key] is False for key in _UNSAFE_FLAGS)
        and type(proof["disposable_services"]) is int
        and proof["disposable_services"] == 4,
        "REHEARSAL_PROOF_UNSAFE_RUNTIME",
    )

    counts = proof["source_table_counts"]
    require(
        type(counts) is dict
        and all(
            type(table) is str
            and _TABLE.fullmatch(table)
            and table not in {"alembic_version", "workspaces", "workspace_memberships"}
            and type(count) is int
            and count >= 0
            for table, count in counts.items()
        )
        and all(counts.get(table, 0) > 0 for table in REQUIRED_BUSINESS_TABLES)
        and counts.get("users", 0) >= 3,
        "REHEARSAL_PROOF_COUNTS_INVALID",
    )
    hashes = proof["business_data_sha256"]
    require(
        type(proof["encrypted_backup_sha256"]) is str
        and _DIGEST.fullmatch(proof["encrypted_backup_sha256"])
        and type(hashes) is dict
        and set(hashes) == {"source", "restored", "upgraded", "downgraded"}
        and type(hashes["source"]) is str
        and _DIGEST.fullmatch(hashes["source"])
        and hashes["restored"] == hashes["source"]
        and hashes["upgraded"] == hashes["source"],
        "REHEARSAL_PROOF_DATA_HASH_MISMATCH",
    )
    downgrade = request["pre_activation_rollback_policy"] == "PRE_ACTIVATION_DOWNGRADE"
    require(
        proof["pre_activation_downgrade_tested"] is downgrade
        and (
            hashes["downgraded"] == hashes["source"]
            if downgrade
            else hashes["downgraded"] is None
        ),
        "REHEARSAL_PROOF_DOWNGRADE_MISMATCH",
    )
    require(
        _timestamp(run_started_at)
        <= _timestamp(proof["created_at"])
        <= _timestamp(run_updated_at),
        "REHEARSAL_PROOF_TIME_INVALID",
    )
