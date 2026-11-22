"""Pure, side-effect-free contracts for migration release manifests.

This module deliberately does not prepare, approve, apply, or deploy a release.
It only validates the immutable data that later migration-release stages must use.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REPOSITORY = "afkochnev/iteam-ai-marketing"
FRONTEND_API_URL = "http://localhost:8000/api/v1"
MIGRATION_MANIFEST_VERSION = 2
MIGRATION_RELEASE_TYPE = "MIGRATION"
MAX_MIGRATION_STEPS = 64

COMPATIBILITY_CLASSES = frozenset({"EXPAND_ONLY"})
ROLLBACK_POLICIES = frozenset(
    {"PRE_ACTIVATION_DOWNGRADE", "FAIL_FORWARD_ONLY"}
)

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCHEMA_HEAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,63}\Z")


class ManifestContractError(ValueError):
    """A stable fail-closed validation code for an untrusted manifest."""


def _require(value: Any, code: str) -> None:
    if not value:
        raise ManifestContractError(code)


def _exact_object(value: Any, fields: set[str], code: str) -> dict[str, Any]:
    _require(type(value) is dict and set(value) == fields, code)
    return value


def _positive_int(value: Any, code: str) -> int:
    _require(type(value) is int and value > 0, code)
    return value


def _sha(value: Any, code: str) -> str:
    _require(isinstance(value, str) and _SHA.fullmatch(value), code)
    return value


def _sha256(value: Any, code: str) -> str:
    _require(isinstance(value, str) and _SHA256.fullmatch(value), code)
    return value


def _schema_head(value: Any, code: str) -> str:
    _require(isinstance(value, str) and _SCHEMA_HEAD.fullmatch(value), code)
    return value


def _image_ref(component: str, value: Any) -> str:
    prefix = f"ghcr.io/{REPOSITORY}-{component}@"
    _require(
        isinstance(value, str)
        and value.startswith(prefix)
        and _SHA256.fullmatch(value[len(prefix) :]),
        "INVALID_MIGRATION_IMAGE",
    )
    return value


def _images(value: Any, code: str) -> dict[str, str]:
    item = _exact_object(value, {"backend", "frontend"}, code)
    return {
        component: _image_ref(component, item[component])
        for component in ("backend", "frontend")
    }


def _migration_path(
    value: Any,
    *,
    source_schema_head: str,
    target_schema_head: str,
) -> list[dict[str, str]]:
    _require(
        type(value) is list and 1 <= len(value) <= MAX_MIGRATION_STEPS,
        "INVALID_MIGRATION_PATH",
    )
    previous = source_schema_head
    revisions: set[str] = set()
    result: list[dict[str, str]] = []
    for raw_step in value:
        step = _exact_object(
            raw_step,
            {"revision", "down_revision", "script_sha256"},
            "INVALID_MIGRATION_STEP",
        )
        revision = _schema_head(step["revision"], "INVALID_MIGRATION_REVISION")
        down_revision = _schema_head(
            step["down_revision"], "INVALID_MIGRATION_DOWN_REVISION"
        )
        script_sha256 = _sha256(
            step["script_sha256"], "INVALID_MIGRATION_SCRIPT_HASH"
        )
        _require(down_revision == previous, "MIGRATION_PATH_NOT_CONTIGUOUS")
        _require(
            revision != source_schema_head and revision not in revisions,
            "DUPLICATE_MIGRATION_REVISION",
        )
        revisions.add(revision)
        previous = revision
        result.append(
            {
                "revision": revision,
                "down_revision": down_revision,
                "script_sha256": script_sha256,
            }
        )
    _require(previous == target_schema_head, "MIGRATION_PATH_TARGET_MISMATCH")
    return result


def migration_plan_hash(
    *,
    source_schema_head: str,
    target_schema_head: str,
    migration_path: Any,
    compatibility_class: str,
    pre_activation_rollback_policy: str,
) -> str:
    """Return the canonical hash that binds an ordered migration plan."""
    source = _schema_head(source_schema_head, "INVALID_SOURCE_SCHEMA_HEAD")
    target = _schema_head(target_schema_head, "INVALID_TARGET_SCHEMA_HEAD")
    _require(source != target, "SOURCE_AND_TARGET_SCHEMA_MATCH")
    _require(
        isinstance(compatibility_class, str)
        and compatibility_class in COMPATIBILITY_CLASSES,
        "UNSUPPORTED_MIGRATION_COMPATIBILITY_CLASS",
    )
    _require(
        isinstance(pre_activation_rollback_policy, str)
        and pre_activation_rollback_policy in ROLLBACK_POLICIES,
        "UNSUPPORTED_PRE_ACTIVATION_ROLLBACK_POLICY",
    )
    path = _migration_path(
        migration_path,
        source_schema_head=source,
        target_schema_head=target,
    )
    payload = {
        "contract_version": 1,
        "source_schema_head": source,
        "target_schema_head": target,
        "migration_path": path,
        "compatibility_class": compatibility_class,
        "pre_activation_rollback_policy": pre_activation_rollback_policy,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def validate_migration_manifest(value: Any) -> None:
    """Validate a migration-release manifest without external I/O."""
    manifest = _exact_object(
        value,
        {
            "version",
            "release_type",
            "repository",
            "source_release_sha",
            "target_release_sha",
            "target_build_run_id",
            "target_build_attempt",
            "source_schema_head",
            "target_schema_head",
            "migration_path",
            "migration_plan_hash",
            "compatibility_class",
            "pre_activation_rollback_policy",
            "frontend_api_url",
            "source_images",
            "target_images",
            "rehearsal_proof",
        },
        "UNEXPECTED_MIGRATION_MANIFEST_FIELDS",
    )
    _require(
        type(manifest["version"]) is int
        and manifest["version"] == MIGRATION_MANIFEST_VERSION
        and manifest["release_type"] == MIGRATION_RELEASE_TYPE
        and manifest["repository"] == REPOSITORY,
        "INVALID_MIGRATION_MANIFEST_IDENTITY",
    )
    source_release = _sha(
        manifest["source_release_sha"], "INVALID_SOURCE_RELEASE_SHA"
    )
    target_release = _sha(
        manifest["target_release_sha"], "INVALID_TARGET_RELEASE_SHA"
    )
    _require(source_release != target_release, "SOURCE_AND_TARGET_RELEASE_MATCH")
    _positive_int(manifest["target_build_run_id"], "INVALID_TARGET_BUILD_RUN")
    _positive_int(manifest["target_build_attempt"], "INVALID_TARGET_BUILD_ATTEMPT")

    source_schema = _schema_head(
        manifest["source_schema_head"], "INVALID_SOURCE_SCHEMA_HEAD"
    )
    target_schema = _schema_head(
        manifest["target_schema_head"], "INVALID_TARGET_SCHEMA_HEAD"
    )
    _require(
        manifest["frontend_api_url"] == FRONTEND_API_URL,
        "UNREVIEWED_FRONTEND_ENDPOINT",
    )
    _images(manifest["source_images"], "INVALID_SOURCE_IMAGES")
    _images(manifest["target_images"], "INVALID_TARGET_IMAGES")

    expected_plan_hash = migration_plan_hash(
        source_schema_head=source_schema,
        target_schema_head=target_schema,
        migration_path=manifest["migration_path"],
        compatibility_class=manifest["compatibility_class"],
        pre_activation_rollback_policy=manifest[
            "pre_activation_rollback_policy"
        ],
    )
    _sha256(manifest["migration_plan_hash"], "INVALID_MIGRATION_PLAN_HASH")
    _require(
        manifest["migration_plan_hash"] == expected_plan_hash,
        "MIGRATION_PLAN_HASH_MISMATCH",
    )

    proof = _exact_object(
        manifest["rehearsal_proof"],
        {
            "workflow_run_id",
            "artifact_id",
            "proof_sha256",
            "source_release_sha",
            "target_release_sha",
            "source_schema_head",
            "target_schema_head",
            "migration_plan_hash",
        },
        "INVALID_REHEARSAL_PROOF_FIELDS",
    )
    _positive_int(proof["workflow_run_id"], "INVALID_REHEARSAL_WORKFLOW_RUN")
    _positive_int(proof["artifact_id"], "INVALID_REHEARSAL_ARTIFACT")
    _sha256(proof["proof_sha256"], "INVALID_REHEARSAL_PROOF_HASH")
    _require(
        proof["source_release_sha"] == source_release
        and proof["target_release_sha"] == target_release
        and proof["source_schema_head"] == source_schema
        and proof["target_schema_head"] == target_schema
        and proof["migration_plan_hash"] == expected_plan_hash,
        "REHEARSAL_PROOF_SCOPE_MISMATCH",
    )
