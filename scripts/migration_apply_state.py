"""Durable, one-use state for a migration operation before writer activation.

The caller holds release_ops.locked_state on the canonical shared directory.
There is deliberately no load/resume API: an existing journal or approval
marker requires operator recovery rather than replaying a possibly run action.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import release_ops as ops
from migration_writer_barrier import ERROR_CODES as WRITER_BARRIER_ERROR_CODES
from release_manifest import ManifestContractError, validate_migration_manifest

JournalError = ops.ReleaseError
ACTION_PHASES = {
    "backup": "BACKUP_VERIFIED",
    "stop_schedulers": "SCHEDULERS_STOPPED",
    "drain_workers": "WORKERS_DRAINED",
    "stop_writers": "WRITERS_STOPPED",
    "fence_database": "DATABASE_FENCED",
    "source_checks": "SOURCE_VERIFIED",
    "target_preflight": "TARGET_PREFLIGHT_VERIFIED",
    "upgrade": "UPGRADE_COMPLETED",
    "target_checks": "TARGET_VERIFIED",
    "readiness": "MAINTENANCE_READY",
    "containment": "CONTAINED",
    "downgrade": "DOWNGRADE_COMPLETED",
    "restore_source_configuration": "SOURCE_CONFIGURATION_RESTORED",
}
FORWARD_PREDECESSORS = {
    "backup": "APPROVAL_CONSUMED",
    "stop_schedulers": "BACKUP_VERIFIED",
    "drain_workers": "SCHEDULERS_STOPPED",
    "stop_writers": "WORKERS_DRAINED",
    "fence_database": "WRITERS_STOPPED",
    "source_checks": "DATABASE_FENCED",
    "target_preflight": "SOURCE_VERIFIED",
    "upgrade": "TARGET_PREFLIGHT_VERIFIED",
    "target_checks": "UPGRADE_COMPLETED",
    "readiness": "TARGET_VERIFIED",
}
RECOVERY_ACTIONS = frozenset({
    "containment", "downgrade", "source_checks", "restore_source_configuration",
})
TERMINAL_STATUSES = frozenset({
    "PRE_ACTIVATION_READY", "ROLLED_BACK_PRE_ACTIVATION",
    "OPERATOR_RECOVERY_REQUIRED", "FAILED_BEFORE_MUTATION",
})
ERROR_CODES = frozenset({
    "INVALID_MIGRATION_OPERATION_ID", "INVALID_MIGRATION_STATE_MANIFEST",
    "INVALID_MIGRATION_STATE_APPROVAL", "MIGRATION_APPROVAL_EXPIRED",
    "UNSAFE_MIGRATION_STATE_DIRECTORY", "MIGRATION_OPERATION_ALREADY_EXISTS",
    "APPROVAL_ALREADY_USED_NO_AUTOMATIC_RETRY", "MIGRATION_STATE_NOT_CREATED",
    "MIGRATION_STATE_WRITE_FAILED", "MIGRATION_STATE_UNUSABLE",
    "MIGRATION_STATE_CHANGED", "MIGRATION_APPROVAL_MARKER_CHANGED",
    "INVALID_MIGRATION_STATE_TRANSITION", "MIGRATION_ACTION_PENDING",
    "MIGRATION_ACTION_ALREADY_ATTEMPTED", "MIGRATION_APPROVAL_NOT_CONSUMED",
    "INVALID_MIGRATION_STATE_EVIDENCE", "UNSAFE_MIGRATION_STATE_ERROR_CODE",
})
# Engine/adapters map unexpected exceptions to this closed vocabulary. Arbitrary
# exception text cannot be made safe merely by matching an uppercase regex.
FAILURE_CODES = frozenset({
    "MIGRATION_APPLY_FAILED", "MIGRATION_RUNTIME_FAILED",
    "MIGRATION_SOURCE_CHANGED", "MIGRATION_SCHEMA_MISMATCH",
    "MIGRATION_DATA_CHANGED", "MIGRATION_BACKUP_BINDING_MISMATCH",
    "MIGRATION_DRAIN_TIMEOUT", "MIGRATION_WRITER_BARRIER_LOST",
    "MIGRATION_TARGET_PREFLIGHT_FAILED", "MIGRATION_TARGET_CHECKS_FAILED",
    "MIGRATION_READINESS_FAILED", "MIGRATION_DOWNGRADE_FAILED",
    "MIGRATION_SOURCE_CHECKS_FAILED", "MIGRATION_SOURCE_RESTORE_FAILED",
    "MIGRATION_CONTAINMENT_FAILED", "UNEXPECTED_MIGRATION_APPLY_ERROR",
    "INJECTED_FAILURE", "EXTERNAL_COMMAND_FAILED", "TOOL_UNAVAILABLE",
}) | ERROR_CODES | WRITER_BARRIER_ERROR_CODES

_OPERATION = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SCHEMA = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{0,63}\Z")
_TABLE = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_HASH_FIELDS = frozenset({
    "business_sha256", "encrypted_backup_sha256", "source_business_sha256",
    "restored_business_sha256", "database_identity_sha256",
    "runtime_identity_sha256", "barrier_sha256",
})
_CHECKS = frozenset({
    "source_verified", "backup_verified", "restored_data_verified",
    "schedulers_stopped", "workers_drained", "writers_stopped",
    "database_fenced", "target_preflight_verified", "target_schema_verified",
    "invariants_verified", "maintenance_readiness", "source_schema_verified",
    "source_configuration_restored", "cleanup_complete", "containment_verified",
})


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def manifest_hash(manifest: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(manifest)).hexdigest()


def _require(condition: Any, code: str) -> None:
    ops.require(condition, code)


def _approval_time(value: Any) -> datetime:
    _require(type(value) is str and len(value) <= 64, "INVALID_MIGRATION_STATE_APPROVAL")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.utcoffset() is not None, "INVALID_MIGRATION_STATE_APPROVAL")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise JournalError("INVALID_MIGRATION_STATE_APPROVAL") from None


def validate_evidence(value: Any) -> dict[str, Any]:
    """Only non-payload audit fields may be written to the operation journal."""
    if value is None:
        return {}
    _require(
        type(value) is dict
        and set(value) <= _HASH_FIELDS | {
            "schema", "source_release_sha", "table_counts", "source_image_ids", "checks",
        },
        "INVALID_MIGRATION_STATE_EVIDENCE",
    )
    for key, item in value.items():
        if key in _HASH_FIELDS:
            valid = type(item) is str and _DIGEST.fullmatch(item)
        elif key == "schema":
            valid = type(item) is str and _SCHEMA.fullmatch(item)
        elif key == "source_release_sha":
            valid = type(item) is str and _SHA.fullmatch(item)
        elif key == "table_counts":
            valid = type(item) is dict and 0 < len(item) <= 2048 and all(
                type(name) is str and _TABLE.fullmatch(name)
                and type(count) is int and 0 <= count <= 2**63 - 1
                for name, count in item.items()
            )
        elif key == "source_image_ids":
            valid = type(item) is dict and bool(item) and set(item) <= set(ops.APP) and all(
                type(image_id) is str and _DIGEST.fullmatch(image_id)
                for image_id in item.values()
            )
        else:
            valid = type(item) is dict and set(item) <= _CHECKS and all(
                type(flag) is bool for flag in item.values()
            )
        _require(valid, "INVALID_MIGRATION_STATE_EVIDENCE")
    return copy.deepcopy(value)


def _safe_error(value: Any) -> str:
    _require(type(value) is str and value in FAILURE_CODES, "UNSAFE_MIGRATION_STATE_ERROR_CODE")
    return value


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_write(path: Path, value: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


class Journal:
    def __init__(
        self, state_dir: Path, operation_id: str, manifest: dict[str, Any],
        approval: dict[str, Any],
    ):
        _require(
            type(operation_id) is str and _OPERATION.fullmatch(operation_id),
            "INVALID_MIGRATION_OPERATION_ID",
        )
        _require(
            state_dir.is_absolute() and state_dir.is_dir() and not state_dir.is_symlink(),
            "UNSAFE_MIGRATION_STATE_DIRECTORY",
        )
        metadata = state_dir.stat()
        _require(
            metadata.st_uid == os.getuid() and stat.S_IMODE(metadata.st_mode) == 0o700,
            "UNSAFE_MIGRATION_STATE_DIRECTORY",
        )
        try:
            validate_migration_manifest(manifest)
        except (ManifestContractError, KeyError, TypeError, ValueError):
            raise JournalError("INVALID_MIGRATION_STATE_MANIFEST") from None
        _require(
            type(approval) is dict and set(approval) == {
                "approval_run_id", "artifact_id", "receipt_sha256", "approved_at",
            }
            and type(approval.get("approval_run_id")) is int and approval["approval_run_id"] > 0
            and type(approval.get("artifact_id")) is int and approval["artifact_id"] > 0
            and type(approval.get("receipt_sha256")) is str
            and _DIGEST.fullmatch(approval["receipt_sha256"]),
            "INVALID_MIGRATION_STATE_APPROVAL",
        )
        _approval_time(approval["approved_at"])
        self.state_dir = state_dir
        self._directory_identity = (metadata.st_dev, metadata.st_ino)
        self.path = state_dir / f"operation-{operation_id}.json"
        self.marker_path = state_dir / f"approval-{approval['approval_run_id']}.consumed"
        self._broken = False
        self._created = False
        self._marker: bytes | None = None
        self._persisted: bytes | None = None
        now = datetime.now(UTC).isoformat()
        self._data = {
            "version": 1, "repository": ops.REPO, "operation_id": operation_id,
            "manifest": copy.deepcopy(manifest), "manifest_sha256": manifest_hash(manifest),
            "approval": copy.deepcopy(approval), "phase": "VERIFIED", "status": "RUNNING",
            "pending_action": None, "sequence": 0, "consumed_at": None,
            "created_at": now, "updated_at": now, "failure_code": None,
            "recovery_error_code": None, "writer_activation_requested": False, "history": [],
        }

    @property
    def data(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    @property
    def has_pending(self) -> bool:
        return self._data["pending_action"] is not None

    def _check_directory(self) -> None:
        metadata = self.state_dir.stat()
        _require(
            not self.state_dir.is_symlink()
            and (metadata.st_dev, metadata.st_ino) == self._directory_identity
            and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700,
            "UNSAFE_MIGRATION_STATE_DIRECTORY",
        )

    def _live(self) -> None:
        _require(self._created, "MIGRATION_STATE_NOT_CREATED")
        _require(not self._broken, "MIGRATION_STATE_UNUSABLE")
        _require(self._data["status"] == "RUNNING", "INVALID_MIGRATION_STATE_TRANSITION")
        try:
            self._check_directory()
            _require(
                self.path.is_file() and not self.path.is_symlink()
                and self.path.stat().st_uid == os.getuid()
                and stat.S_IMODE(self.path.stat().st_mode) == 0o600
                and self.path.stat().st_size == len(self._persisted)
                and self.path.read_bytes() == self._persisted,
                "MIGRATION_STATE_CHANGED",
            )
            if self._marker is not None:
                _require(
                    self.marker_path.is_file() and not self.marker_path.is_symlink()
                    and self.marker_path.stat().st_uid == os.getuid()
                    and stat.S_IMODE(self.marker_path.stat().st_mode) == 0o600
                    and self.marker_path.stat().st_size == len(self._marker)
                    and self.marker_path.read_bytes() == self._marker,
                    "MIGRATION_APPROVAL_MARKER_CHANGED",
                )
        except JournalError:
            self._broken = True
            raise
        except OSError:
            self._broken = True
            raise JournalError("MIGRATION_STATE_CHANGED") from None

    def _event(self, data: dict, kind: str, *, evidence: dict | None = None) -> None:
        data["sequence"] += 1
        data["updated_at"] = datetime.now(UTC).isoformat()
        event = {
            "sequence": data["sequence"], "kind": kind, "phase": data["phase"],
            "action": data["pending_action"], "at": data["updated_at"],
            "recovery": data["failure_code"] is not None,
        }
        if evidence is not None:
            event["evidence"] = evidence
        data["history"].append(event)

    def _save(self, data: dict[str, Any]) -> None:
        try:
            ops.save(self.path, data)
        except (OSError, JournalError):
            # Even an after-rename fsync failure makes durability unknown. This
            # object must not authorize another action or schema recovery.
            self._broken = True
            raise JournalError("MIGRATION_STATE_WRITE_FAILED") from None
        self._data = data
        self._persisted = canonical_bytes(data)

    def create(self, evidence: dict | None = None) -> None:
        _require(not self._created and not self._broken, "MIGRATION_OPERATION_ALREADY_EXISTS")
        try:
            self._check_directory()
        except (OSError, JournalError):
            self._broken = True
            raise JournalError("UNSAFE_MIGRATION_STATE_DIRECTORY") from None
        initial = validate_evidence(evidence)
        _require(
            not self.marker_path.exists() and not self.marker_path.is_symlink(),
            "APPROVAL_ALREADY_USED_NO_AUTOMATIC_RETRY",
        )
        data = copy.deepcopy(self._data)
        self._event(data, "created", evidence=initial)
        try:
            _exclusive_write(self.path, data)
        except FileExistsError:
            self._broken = True
            raise JournalError("MIGRATION_OPERATION_ALREADY_EXISTS") from None
        except OSError:
            self._broken = True
            raise JournalError("MIGRATION_STATE_WRITE_FAILED") from None
        self._data, self._persisted, self._created = data, canonical_bytes(data), True

    def consume(self) -> None:
        self._live()
        _require(self._data["phase"] == "VERIFIED" and self._marker is None, "INVALID_MIGRATION_STATE_TRANSITION")
        age = (datetime.now(UTC) - _approval_time(self._data["approval"]["approved_at"])).total_seconds()
        _require(0 <= age <= 3600, "MIGRATION_APPROVAL_EXPIRED")
        consumed_at = datetime.now(UTC).isoformat()
        marker = {
            "version": 1, "operation_id": self._data["operation_id"],
            "approval_run_id": self._data["approval"]["approval_run_id"],
            "artifact_id": self._data["approval"]["artifact_id"],
            "receipt_sha256": self._data["approval"]["receipt_sha256"],
            "manifest_sha256": self._data["manifest_sha256"], "consumed_at": consumed_at,
        }
        try:
            _exclusive_write(self.marker_path, marker)
        except FileExistsError:
            self._broken = True
            raise JournalError("APPROVAL_ALREADY_USED_NO_AUTOMATIC_RETRY") from None
        except OSError:
            self._broken = True
            raise JournalError("MIGRATION_STATE_WRITE_FAILED") from None
        self._marker = canonical_bytes(marker)
        data = copy.deepcopy(self._data)
        data.update(phase="APPROVAL_CONSUMED", consumed_at=consumed_at)
        self._event(data, "consumed")
        self._save(data)

    def begin(self, action: str) -> None:
        self._live()
        _require(self._marker is not None, "MIGRATION_APPROVAL_NOT_CONSUMED")
        _require(not self.has_pending, "MIGRATION_ACTION_PENDING")
        _require(type(action) is str and action in ACTION_PHASES, "INVALID_MIGRATION_STATE_TRANSITION")
        recovery = self._data["failure_code"] is not None
        if not recovery:
            valid = action in FORWARD_PREDECESSORS and self._data["phase"] == FORWARD_PREDECESSORS[action]
        else:
            valid = action in RECOVERY_ACTIONS and (
                action == "containment"
                or action == "downgrade" and self._data["phase"] == "CONTAINED"
                or action == "source_checks" and self._data["phase"] in {"CONTAINED", "DOWNGRADE_COMPLETED"}
                or action == "restore_source_configuration" and self._data["phase"] == "SOURCE_VERIFIED"
            )
        _require(valid, "INVALID_MIGRATION_STATE_TRANSITION")
        _require(
            not any(e["kind"] == "begin" and e["action"] == action and e["recovery"] is recovery for e in self._data["history"]),
            "MIGRATION_ACTION_ALREADY_ATTEMPTED",
        )
        data = copy.deepcopy(self._data)
        data["pending_action"] = action
        self._event(data, "begin")
        self._save(data)

    def complete(self, phase: str, evidence: dict | None = None) -> None:
        self._live()
        _require(self.has_pending and phase == ACTION_PHASES[self._data["pending_action"]], "INVALID_MIGRATION_STATE_TRANSITION")
        safe = validate_evidence(evidence)
        data = copy.deepcopy(self._data)
        data["phase"] = phase
        self._event(data, "complete", evidence=safe)
        data["pending_action"] = None
        self._save(data)

    def fail_action(self, error_code: str) -> None:
        """Persist an observed same-process failure before guarded recovery."""
        self._live()
        _require(self.has_pending, "INVALID_MIGRATION_STATE_TRANSITION")
        code = _safe_error(error_code)
        data = copy.deepcopy(self._data)
        if data["failure_code"] is None:
            data["failure_code"] = code
        else:
            data["recovery_error_code"] = code
        self._event(data, "failed")
        data["history"][-1]["error_code"] = code
        data["pending_action"] = None
        self._save(data)

    def finish(
        self, status: str, error_code: str | None = None,
        recovery_error_code: str | None = None,
    ) -> None:
        self._live()
        _require(type(status) is str and status in TERMINAL_STATUSES, "INVALID_MIGRATION_STATE_TRANSITION")
        if error_code is not None:
            _safe_error(error_code)
        if recovery_error_code is not None:
            _safe_error(recovery_error_code)
        if status == "PRE_ACTIVATION_READY":
            valid = self._data["phase"] == "MAINTENANCE_READY" and not self.has_pending and self._data["failure_code"] is None and error_code is None and recovery_error_code is None
        elif status == "ROLLED_BACK_PRE_ACTIVATION":
            valid = self._data["phase"] == "SOURCE_CONFIGURATION_RESTORED" and not self.has_pending and self._data["failure_code"] is not None and self._data["recovery_error_code"] is None and recovery_error_code is None
        elif status == "FAILED_BEFORE_MUTATION":
            valid = self._marker is None and not self.has_pending and error_code is not None
        else:
            valid = self._data["failure_code"] is not None or error_code is not None
        _require(valid, "INVALID_MIGRATION_STATE_TRANSITION")
        data = copy.deepcopy(self._data)
        data["status"] = status
        if data["failure_code"] is None:
            data["failure_code"] = error_code
        if recovery_error_code is not None:
            data["recovery_error_code"] = recovery_error_code
        self._event(data, "finished")
        self._save(data)
