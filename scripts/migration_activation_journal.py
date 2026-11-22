"""One-way records for a new, explicitly authorized live migration operation.

The caller holds the common release lock and verifies the signed receipt before
constructing a production binding. This journal does not verify signatures or
load/resume an operation. Its permit belongs only to the original live object
and process. Expiry/state failures revoke that permit but never prohibit stop.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import release_ops as ops
import migration_apply_backup as backup
from migration_activation_contract import activation_policy, hash_json
from migration_apply_state import Journal, canonical_bytes, manifest_hash

PRODUCTION_AUTHORIZATION = "PRODUCTION_MIGRATION_ACTIVATION"
FIXTURE_AUTHORIZATION = "DISPOSABLE_SIGNED_ACTIVATION_ONLY"
TEMP_PREFIX = "iteam-migration-activation-drill-"
PROJECT_PREFIX = "iteam-activation-drill-"
ActivationStateError = ops.ReleaseError
MAX_FILE_BYTES = 4 * 1024 * 1024
ERROR_CODES = frozenset({
    "ACTIVATION_BINDING_INVALID", "ACTIVATION_DIRECTORY_UNSAFE",
    "ACTIVATION_STATE_EXISTS", "ACTIVATION_STATE_NOT_CREATED",
    "ACTIVATION_STATE_UNUSABLE", "ACTIVATION_STATE_CHANGED",
    "ACTIVATION_DEPENDENCY_CHANGED", "ACTIVATION_MARKER_CHANGED",
    "ACTIVATION_STATE_WRITE_FAILED", "ACTIVATION_PROCESS_CHANGED",
    "ACTIVATION_TRANSITION_INVALID", "ACTIVATION_EVIDENCE_INVALID",
    "ACTIVATION_AUTHORITY_EXPIRED", "ACTIVATION_ERROR_UNSAFE",
})
FAILURE_CODES = ERROR_CODES | frozenset({
    "ACTIVATION_FAILED", "ACTIVATION_PASSIVE_POSTFLIGHT_FAILED",
    "ACTIVATION_HANDOFF_FAILED", "ACTIVATION_RECREATE_FAILED",
    "ACTIVATION_RECONNECT_FAILED", "ACTIVATION_START_FAILED",
    "ACTIVATION_POSTFLIGHT_FAILED", "ACTIVATION_FINALIZE_FAILED",
    "ACTIVATION_CONTAINMENT_FAILED", "INJECTED_POST_ACTIVATION_FAILURE",
})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BINDING_FIELDS = {
    "authorization", "operation_id", "project", "manifest_sha256", "receipt_sha256",
    "approval_run_id", "runtime_identity_sha256", "target_release_sha",
    "target_schema_head", "activation_policy_sha256", "expires_at",
}
_PLAN_BINDINGS = {
    "operation_id", "project", "manifest_sha256", "runtime_identity_sha256",
    "target_release_sha", "target_schema_head", "activation_policy_sha256",
}


def state_directory() -> Path:
    return Path.home() / "iTeam-private" / "release-operations" / "iteam-ai-marketing"


def operation_records_exist(state_dir: Path) -> bool:
    """Auxiliary target plan/config files are not activation journals."""
    return any(re.fullmatch(r"activation-[0-9a-f]{32}\.(?:json|started)", path.name)
               for path in state_dir.glob("activation-*"))


def _require(value: Any, code: str) -> None:
    ops.require(value, code)


def _matches(value: Any, pattern: str) -> bool:
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _timestamp(value: Any) -> float:
    _require(type(value) is str, "ACTIVATION_BINDING_INVALID")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.utcoffset() is not None, "ACTIVATION_BINDING_INVALID")
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise ActivationStateError("ACTIVATION_BINDING_INVALID") from None


def validate_binding(value: Any) -> None:
    _require(
        type(value) is dict and set(value) == _BINDING_FIELDS
        and type(value["authorization"]) is str
        and value["authorization"] in {PRODUCTION_AUTHORIZATION, FIXTURE_AUTHORIZATION}
        and _matches(value["operation_id"], r"[0-9a-f]{32}")
        and type(value["approval_run_id"]) is int and value["approval_run_id"] > 0
        and all(_digest(value[key]) for key in (
            "manifest_sha256", "receipt_sha256", "runtime_identity_sha256", "activation_policy_sha256",
        ))
        and _matches(value["target_release_sha"], r"[0-9a-f]{40}")
        and _matches(value["target_schema_head"], r"[A-Za-z0-9][A-Za-z0-9_]{0,63}")
        and value["activation_policy_sha256"] == hash_json(activation_policy()),
        "ACTIVATION_BINDING_INVALID",
    )
    expected_project = (
        value["project"] == "iteam-ai-marketing"
        if value["authorization"] == PRODUCTION_AUTHORIZATION
        else _matches(value["project"], re.escape(PROJECT_PREFIX) + r"[0-9a-f]{10}")
    )
    _require(expected_project, "ACTIVATION_BINDING_INVALID")
    _timestamp(value["expires_at"])


def _directory_identity(path: Path) -> tuple[int, int]:
    _require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
             "ACTIVATION_DIRECTORY_UNSAFE")
    item = path.stat()
    _require(stat.S_ISDIR(item.st_mode) and item.st_uid == os.getuid()
             and stat.S_IMODE(item.st_mode) == 0o700, "ACTIVATION_DIRECTORY_UNSAFE")
    return item.st_dev, item.st_ino


def _file_identity(item: os.stat_result) -> tuple:
    return (
        item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_nlink,
        item.st_size, item.st_mtime_ns, item.st_ctime_ns,
    )


def _read_private(path: Path) -> tuple[bytes, tuple]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                 and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
                 and 0 < before.st_size <= MAX_FILE_BYTES, "ACTIVATION_STATE_CHANGED")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(before.st_size + 1)
        after, current = os.fstat(descriptor), path.lstat()
        _require(len(data) == before.st_size
                 and _file_identity(before) == _file_identity(after) == _file_identity(current),
                 "ACTIVATION_STATE_CHANGED")
        return data, _file_identity(after)
    finally:
        os.close(descriptor)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_write(path: Path, value: dict) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


class ActivationJournal:
    def __init__(self, state_dir: Path, binding: dict):
        validate_binding(binding)
        if binding["authorization"] == PRODUCTION_AUTHORIZATION:
            _require(state_dir == state_directory(), "ACTIVATION_DIRECTORY_UNSAFE")
        else:
            _require(isinstance(state_dir, Path) and state_dir.name == "state"
                     and _matches(state_dir.parent.name, re.escape(TEMP_PREFIX) + r"[a-z0-9_-]{8}"),
                     "ACTIVATION_DIRECTORY_UNSAFE")
        try:
            self._root_identity = _directory_identity(state_dir.parent)
            self._directory_identity = _directory_identity(state_dir)
        except (OSError, RuntimeError):
            raise ActivationStateError("ACTIVATION_DIRECTORY_UNSAFE") from None
        expiry = _timestamp(binding["expires_at"])
        remaining = expiry - time.time()
        _require(0 < remaining <= 3600, "ACTIVATION_AUTHORITY_EXPIRED")
        self._expires_at = expiry
        self._deadline = time.monotonic() + remaining
        self._owner_pid = os.getpid()
        self.state_dir = state_dir
        self.path = state_dir / f"activation-{binding['operation_id']}.json"
        self.marker_path = state_dir / f"activation-{binding['operation_id']}.started"
        self._created = False
        self._broken = False
        self._blocked = False
        self._may_have_started = False
        self._boundary_recorded = False
        self._persisted: bytes | None = None
        self._journal_identity: tuple | None = None
        self._marker: bytes | None = None
        self._marker_identity: tuple | None = None
        self._dependencies: dict[Path, tuple[bytes, tuple]] = {}
        self._error: str | None = None
        now = datetime.now(UTC).isoformat()
        self._data = {
            "version": 1, "binding": copy.deepcopy(binding), "binding_sha256": hash_json(binding),
            "phase": "NEW", "sequence": 0, "created_at": now, "updated_at": now,
            "activation_boundary_recorded": False, "activation_may_have_started": False,
            "preactivation_records_sha256": None, "target_plan_sha256": None,
            "database_identity_sha256": None,
            "passive_evidence": None, "active_evidence": None, "containment_evidence": None,
            "failure_code": None, "history": [],
        }

    @property
    def data(self) -> dict:
        return copy.deepcopy(self._data)

    def _break(self, code: str) -> None:
        self._broken = self._blocked = True
        self._may_have_started = True
        self._error = code

    def _owner(self) -> None:
        if os.getpid() != self._owner_pid:
            self._break("ACTIVATION_PROCESS_CHANGED")
            raise ActivationStateError("ACTIVATION_PROCESS_CHANGED")

    def assert_fresh(self) -> None:
        self._owner()
        if time.time() >= self._expires_at or time.monotonic() >= self._deadline:
            self._blocked = True
            self._error = "ACTIVATION_AUTHORITY_EXPIRED"
            raise ActivationStateError("ACTIVATION_AUTHORITY_EXPIRED")
        _require(not self._blocked and not self._broken, "ACTIVATION_STATE_UNUSABLE")

    def _directories(self) -> None:
        _require(_directory_identity(self.state_dir.parent) == self._root_identity
                 and _directory_identity(self.state_dir) == self._directory_identity,
                 "ACTIVATION_DIRECTORY_UNSAFE")

    def _dependency_check(self) -> None:
        for path, pinned in self._dependencies.items():
            _require(_read_private(path) == pinned, "ACTIVATION_DEPENDENCY_CHANGED")

    def _live(self) -> None:
        self._owner()
        _require(self._created, "ACTIVATION_STATE_NOT_CREATED")
        _require(not self._broken, "ACTIVATION_STATE_UNUSABLE")
        try:
            self._directories()
            _require(_read_private(self.path) == (self._persisted, self._journal_identity),
                     "ACTIVATION_STATE_CHANGED")
            if self._marker is not None:
                _require(_read_private(self.marker_path) == (self._marker, self._marker_identity),
                         "ACTIVATION_MARKER_CHANGED")
            self._dependency_check()
        except (OSError, RuntimeError) as error:
            code = str(error) if isinstance(error, ActivationStateError) and str(error) in ERROR_CODES else "ACTIVATION_STATE_CHANGED"
            self._break(code)
            raise ActivationStateError(code) from None

    def _event(self, data: dict, phase: str) -> None:
        data["phase"] = phase
        data["sequence"] += 1
        data["updated_at"] = datetime.now(UTC).isoformat()
        data["history"].append({"sequence": data["sequence"], "phase": phase, "at": data["updated_at"]})

    def _write(self, data: dict, *, initial: bool = False) -> None:
        self._owner()
        try:
            self._directories()
            self._dependency_check()
            if initial:
                _exclusive_write(self.path, data)
            else:
                ops.save(self.path, data)
            expected = canonical_bytes(data)
            actual, identity = _read_private(self.path)
            _require(actual == expected, "ACTIVATION_STATE_CHANGED")
            self._directories()
            self._dependency_check()
            if self._marker is not None:
                _require(_read_private(self.marker_path) == (self._marker, self._marker_identity),
                         "ACTIVATION_MARKER_CHANGED")
        except (Exception, KeyboardInterrupt):
            self._break("ACTIVATION_STATE_WRITE_FAILED")
            raise ActivationStateError("ACTIVATION_STATE_WRITE_FAILED") from None
        self._data, self._persisted, self._journal_identity = data, expected, identity

    def create(self, preapply_journal: Journal, plan: dict, passive_evidence: dict) -> None:
        self.assert_fresh()
        _require(not self._created and not self._broken, "ACTIVATION_STATE_EXISTS")
        binding = self._data["binding"]
        try:
            self._directories()
            _require(not operation_records_exist(self.state_dir), "ACTIVATION_STATE_EXISTS")
            _require(isinstance(preapply_journal, Journal) and preapply_journal.state_dir == self.state_dir,
                     "ACTIVATION_EVIDENCE_INVALID")
            # The old journal is deliberately terminal. Its mutation permit
            # rejects PRE_ACTIVATION_READY; pin the completed records below
            # without reopening or changing the pre-activation journal.
            previous = preapply_journal.data
            initial = previous["history"][0].get("evidence", {})
            _require(previous["status"] == "PRE_ACTIVATION_READY"
                     and previous["phase"] == "MAINTENANCE_READY" and previous["pending_action"] is None
                     and previous["operation_id"] == binding["operation_id"]
                     and manifest_hash(previous["manifest"]) == binding["manifest_sha256"]
                     and previous["approval"]["receipt_sha256"] == binding["receipt_sha256"]
                     and previous["approval"]["approval_run_id"] == binding["approval_run_id"]
                     and previous["manifest"]["target_release_sha"] == binding["target_release_sha"]
                     and previous["manifest"]["target_schema_head"] == binding["target_schema_head"]
                     and initial.get("runtime_identity_sha256") == binding["runtime_identity_sha256"]
                     and _digest(initial.get("database_identity_sha256")),
                     "ACTIVATION_EVIDENCE_INVALID")
            _require(type(plan) is dict and all(plan.get(key) == binding[key] for key in _PLAN_BINDINGS),
                     "ACTIVATION_EVIDENCE_INVALID")
            _require(plan.get("database_identity_sha256") == initial["database_identity_sha256"],
                     "ACTIVATION_EVIDENCE_INVALID")
            plan_hash = hash_json(plan)
            expected = {
                "admission_protocol": 1, "admission_mode": "passive", "business_ready": False,
                "business_writes_blocked": True, "claims_blocked": True, "helpers_removed": True,
                "target_release_sha": binding["target_release_sha"],
                "target_schema_head": binding["target_schema_head"], "target_plan_sha256": plan_hash,
            }
            _require(type(passive_evidence) is dict and all(
                type(passive_evidence.get(key)) is type(value) and passive_evidence[key] == value
                for key, value in expected.items()
            ) and _digest(passive_evidence.get("source_barrier_sha256")), "ACTIVATION_EVIDENCE_INVALID")
            readiness = [event.get("evidence", {}) for event in previous["history"]
                         if event["kind"] == "complete" and event["phase"] == "MAINTENANCE_READY"]
            _require(len(readiness) == 1
                     and readiness[0].get("barrier_sha256") == passive_evidence["source_barrier_sha256"],
                     "ACTIVATION_EVIDENCE_INVALID")
            paths = [
                preapply_journal.path, preapply_journal.marker_path,
                self.state_dir / f"migration-runtime-{binding['operation_id']}.json",
                self.state_dir / f"migration-backup-{binding['operation_id']}.json",
            ]
            dependencies = {path: _read_private(path) for path in paths}
            _require(preapply_journal._created and not preapply_journal._broken
                     and dependencies[preapply_journal.path][0] == preapply_journal._persisted == canonical_bytes(previous)
                     and dependencies[preapply_journal.marker_path][0] == preapply_journal._marker,
                     "ACTIVATION_DEPENDENCY_CHANGED")
            _require(hash_json(json.loads(dependencies[paths[2]][0])) == binding["runtime_identity_sha256"],
                     "ACTIVATION_DEPENDENCY_CHANGED")
            backup_proof = json.loads(dependencies[paths[3]][0])
            backup.validate_proof(backup_proof, {
                "operation_id": binding["operation_id"], "manifest_sha256": binding["manifest_sha256"],
                "database_identity_sha256": initial["database_identity_sha256"],
                "source_release_sha": previous["manifest"]["source_release_sha"],
                "source_schema_head": previous["manifest"]["source_schema_head"],
                "source_backend_image": previous["manifest"]["source_images"]["backend"],
            })
            backups = [event.get("evidence", {}) for event in previous["history"]
                       if event["kind"] == "complete" and event["phase"] == "BACKUP_VERIFIED"]
            _require(len(backups) == 1 and all(backups[0].get(key) == value for key, value in {
                "encrypted_backup_sha256": backup_proof["encrypted_backup_sha256"],
                "source_business_sha256": backup_proof["source_business_state"]["sha256"],
                "restored_business_sha256": backup_proof["restored_business_state"]["sha256"],
                "table_counts": backup_proof["source_business_state"]["table_counts"],
            }.items()), "ACTIVATION_DEPENDENCY_CHANGED")
            self._dependencies = dependencies
            records = {path.name: "sha256:" + hashlib.sha256(item[0]).hexdigest() for path, item in dependencies.items()}
            data = self.data
            data["preactivation_records_sha256"] = hash_json(records)
            data["target_plan_sha256"] = plan_hash
            data["database_identity_sha256"] = initial["database_identity_sha256"]
            data["passive_evidence"] = {
                **expected, "source_barrier_sha256": passive_evidence["source_barrier_sha256"],
                "evidence_sha256": hash_json(passive_evidence),
            }
            self._event(data, "PASSIVE_VERIFIED")
            self._write(data, initial=True)
            self._created = True
        except (Exception, KeyboardInterrupt) as error:
            code = str(error) if isinstance(error, ActivationStateError) and str(error) in ERROR_CODES else "ACTIVATION_EVIDENCE_INVALID"
            self._break(code)
            raise ActivationStateError(code) from None

    def begin_activation(self) -> None:
        self._live()
        self.assert_fresh()
        _require(self._data["phase"] == "PASSIVE_VERIFIED", "ACTIVATION_TRANSITION_INVALID")
        self._may_have_started = True
        data = self.data
        data["activation_may_have_started"] = True
        self._event(data, "ACTIVATION_REQUESTED")
        self._write(data)
        marker = {
            "version": 1, "binding": copy.deepcopy(data["binding"]),
            "binding_sha256": data["binding_sha256"],
            "preactivation_records_sha256": data["preactivation_records_sha256"],
            "target_plan_sha256": data["target_plan_sha256"],
            "passive_evidence_sha256": hash_json(data["passive_evidence"]),
            "activation_requested_at": data["updated_at"],
            "schema_downgrade_allowed": False, "backup_restore_allowed": False,
        }
        try:
            self._owner()
            self._dependency_check()
            _exclusive_write(self.marker_path, marker)
            actual, identity = _read_private(self.marker_path)
            _require(actual == canonical_bytes(marker), "ACTIVATION_MARKER_CHANGED")
            self._directories()
            self._dependency_check()
        except (Exception, KeyboardInterrupt):
            self._break("ACTIVATION_STATE_WRITE_FAILED")
            raise ActivationStateError("ACTIVATION_STATE_WRITE_FAILED") from None
        self._marker, self._marker_identity, self._boundary_recorded = actual, identity, True
        data = self.data
        data["activation_boundary_recorded"] = True
        self._event(data, "ACTIVATION_STARTED")
        self._write(data)
        self.assert_activation_permitted()

    def assert_activation_permitted(self) -> None:
        self._live()
        self.assert_fresh()
        _require(self._data["phase"] == "ACTIVATION_STARTED" and self._boundary_recorded
                 and self._marker is not None, "ACTIVATION_TRANSITION_INVALID")

    def mark_active(self, evidence: dict) -> None:
        self.assert_activation_permitted()
        binding = self._data["binding"]
        expected = {
            "runtime_ready": True, "exact_queues_verified": True, "configuration_persisted": True,
            "target_release_sha": binding["target_release_sha"],
            "target_schema_head": binding["target_schema_head"],
            "target_plan_sha256": self._data["target_plan_sha256"],
        }
        _require(type(evidence) is dict and all(
            type(evidence.get(key)) is type(value) and evidence[key] == value for key, value in expected.items()
        ) and _digest(evidence.get("target_container_ids_sha256")), "ACTIVATION_EVIDENCE_INVALID")
        data = self.data
        data["active_evidence"] = {**expected, "target_container_ids_sha256": evidence["target_container_ids_sha256"],
                                   "evidence_sha256": hash_json(evidence)}
        self._event(data, "ACTIVE")
        self._write(data)
        self._blocked = True

    def request_containment(self, code: str) -> bool:
        self._blocked = True
        if type(code) is not str or code not in FAILURE_CODES:
            self._break("ACTIVATION_ERROR_UNSAFE")
            return False
        if self._broken or not self._created:
            return False
        try:
            self._live()
            if self._data["phase"] in {"CONTAINMENT_REQUESTED", "CONTAINED", "OPERATOR_RECOVERY_REQUIRED"}:
                return True
            data = self.data
            data["failure_code"] = code
            self._event(data, "CONTAINMENT_REQUESTED")
            self._write(data)
            return True
        except (Exception, KeyboardInterrupt):
            self._break(self._error or "ACTIVATION_STATE_WRITE_FAILED")
            return False

    def finish_containment(self, evidence: dict | None) -> dict:
        self._blocked = True
        if self._broken or not self._created:
            return self.summary()
        try:
            self._live()
            _require(self._data["phase"] == "CONTAINMENT_REQUESTED", "ACTIVATION_TRANSITION_INVALID")
            data = self.data
            if evidence is not None:
                expected = {
                    "runtime_stopped": True, "new_claims_blocked": True, "data_services_fenced": True,
                    "target_schema_preserved": True, "database_identity_preserved": True,
                    "target_schema_head": self._data["binding"]["target_schema_head"],
                    "database_identity_sha256": self._data["database_identity_sha256"],
                }
                _require(type(evidence) is dict and all(
                    type(evidence.get(key)) is type(value) and evidence[key] == value for key, value in expected.items()
                ) and all(_digest(evidence.get(key)) for key in ("all_tables_sha256", "database_identity_sha256")),
                    "ACTIVATION_EVIDENCE_INVALID")
                data["containment_evidence"] = {
                    **expected, "all_tables_sha256": evidence["all_tables_sha256"],
                    "database_identity_sha256": evidence["database_identity_sha256"],
                    "evidence_sha256": hash_json(evidence),
                }
            self._event(data, "CONTAINED" if evidence is not None else "OPERATOR_RECOVERY_REQUIRED")
            self._write(data)
        except (Exception, KeyboardInterrupt) as error:
            code = str(error) if isinstance(error, ActivationStateError) and str(error) in ERROR_CODES else "ACTIVATION_STATE_WRITE_FAILED"
            self._break(code)
        return self.summary()

    def summary(self) -> dict:
        if self._created and not self._broken:
            try:
                self._live()
            except (Exception, KeyboardInterrupt):
                self._break(self._error or "ACTIVATION_STATE_CHANGED")
        contained = self._created and not self._broken and self._data["phase"] == "CONTAINED"
        return {
            "state_contract_version": 1, **copy.deepcopy(self._data["binding"]),
            "phase": "OPERATOR_RECOVERY_REQUIRED" if self._broken else self._data["phase"],
            "state_durability": "CONFIRMED" if self._created and not self._broken else "UNKNOWN",
            "activation_boundary_recorded": self._boundary_recorded,
            "activation_may_have_started": self._may_have_started,
            "containment_verified": contained, "target_schema_preserved": contained,
            "preactivation_records_sha256": self._data["preactivation_records_sha256"],
            "target_plan_sha256": self._data["target_plan_sha256"],
            "error_code": self._error or self._data["failure_code"],
            "automatic_retry": False, "resume_authorized": False,
            "schema_downgrade_allowed": False, "backup_restore_allowed": False,
            # Records do not reverify a receipt or observe the current runtime.
            "receipt_signature_verified": False, "live_runtime_observed": False,
            "production_approval_verified": False,
        }
