"""One-way activation records for an isolated, unsigned candidate fixture.

The caller holds release_ops.locked_state for the generated fixture directory.
This module has no production authority, resume, Docker, SQL or downgrade API.
Only the same live object may authorize the fixture's transition to ACTIVE;
persisted files are evidence, never a permit for a replacement process.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import release_ops as ops
from migration_apply_state import canonical_bytes

AUTHORIZATION = "DISPOSABLE_CANDIDATE_ONLY"
TEMP_PREFIX = "iteam-runtime-admission-drill-"
PROJECT_PREFIX = "iteam-admission-drill-"
ActivationStateError = ops.ReleaseError
ERROR_CODES = frozenset({
    "INVALID_CANDIDATE_ACTIVATION_BINDING", "UNSAFE_CANDIDATE_ACTIVATION_DIRECTORY",
    "CANDIDATE_ACTIVATION_STATE_EXISTS", "CANDIDATE_ACTIVATION_STATE_NOT_CREATED",
    "CANDIDATE_ACTIVATION_STATE_UNUSABLE", "CANDIDATE_ACTIVATION_STATE_CHANGED",
    "CANDIDATE_ACTIVATION_MARKER_CHANGED", "CANDIDATE_ACTIVATION_INVALID_TRANSITION",
    "CANDIDATE_ACTIVATION_STATE_WRITE_FAILED", "CANDIDATE_ACTIVATION_UNSAFE_ERROR",
    "CANDIDATE_ACTIVATION_INVALID_EVIDENCE", "CANDIDATE_ACTIVATION_PROCESS_CHANGED",
})
FAILURE_CODES = ERROR_CODES | frozenset({
    "INJECTED_POST_ACTIVATION_FAILURE", "RUNTIME_ADMISSION_FAILED",
    "CANDIDATE_ACTIVATION_FAILED", "CANDIDATE_CONTAINMENT_FAILED",
})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_BINDING_FIELDS = {
    "authorization", "operation_id", "project", "candidate_sha",
    "candidate_image_id", "target_schema_head",
}
_PASSIVE_FIELDS = {
    "candidate_sha", "candidate_image_id", "project", "passive_checks_sha256",
    "business_writes_blocked", "claims_blocked",
}
_CONTAINMENT_FIELDS = {
    "target_schema_head", "committed_write_preserved", "new_claims_blocked",
    "runtime_stopped", "evidence_sha256",
}


def _require(value: Any, code: str) -> None:
    ops.require(value, code)


def _matches(value: Any, pattern: str) -> bool:
    return type(value) is str and re.fullmatch(pattern, value) is not None


def _digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def validate_binding(value: Any) -> None:
    _require(
        type(value) is dict and set(value) == _BINDING_FIELDS
        and value["authorization"] == AUTHORIZATION
        and _matches(value["operation_id"], r"[0-9a-f]{32}")
        and _matches(value["project"], re.escape(PROJECT_PREFIX) + r"[0-9a-f]{10}")
        and _matches(value["candidate_sha"], r"[0-9a-f]{40}")
        and _digest(value["candidate_image_id"])
        and _matches(value["target_schema_head"], r"[A-Za-z0-9][A-Za-z0-9_]{0,63}"),
        "INVALID_CANDIDATE_ACTIVATION_BINDING",
    )


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _exclusive_write(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(canonical_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)


def _directory_identity(path: Path) -> tuple[int, int]:
    _require(path.is_absolute() and path.resolve() == path and not path.is_symlink(),
             "UNSAFE_CANDIDATE_ACTIVATION_DIRECTORY")
    metadata = path.stat()
    _require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
             and stat.S_IMODE(metadata.st_mode) == 0o700,
             "UNSAFE_CANDIDATE_ACTIVATION_DIRECTORY")
    return metadata.st_dev, metadata.st_ino


class CandidateActivationState:
    def __init__(self, state_dir: Path, binding: dict):
        validate_binding(binding)
        _require(isinstance(state_dir, Path) and state_dir.name == "state"
                 and _matches(state_dir.parent.name, re.escape(TEMP_PREFIX) + r"[a-z0-9_-]{8}"),
                 "UNSAFE_CANDIDATE_ACTIVATION_DIRECTORY")
        try:
            self._root_identity = _directory_identity(state_dir.parent)
            self._directory_identity = _directory_identity(state_dir)
        except (OSError, RuntimeError):
            raise ActivationStateError("UNSAFE_CANDIDATE_ACTIVATION_DIRECTORY") from None
        self.state_dir = state_dir
        self.path = state_dir / f"activation-{binding['operation_id']}.json"
        self.marker_path = state_dir / f"activation-{binding['operation_id']}.started"
        self._created = False
        self._broken = False
        self._activation_blocked = False
        self._activation_may_have_started = False
        self._boundary_recorded = False
        self._persisted: bytes | None = None
        self._marker: bytes | None = None
        self._journal_identity: tuple | None = None
        self._marker_identity: tuple | None = None
        self._owner_pid = os.getpid()
        self._error: str | None = None
        now = datetime.now(UTC).isoformat()
        self._data = {
            "version": 1, "binding": copy.deepcopy(binding), "binding_sha256": _hash(binding),
            "phase": "NEW", "sequence": 0, "created_at": now, "updated_at": now,
            "activation_boundary_recorded": False, "activation_may_have_started": False,
            "schema_downgrade_allowed": False, "passive_evidence": None,
            "containment_evidence": None, "failure_code": None, "history": [],
        }

    @property
    def data(self) -> dict:
        return copy.deepcopy(self._data)

    def _break(self, code: str) -> None:
        self._broken = self._activation_blocked = True
        # Missing or uncertain state cannot substantiate an un-crossed boundary.
        self._activation_may_have_started = True
        self._error = code

    def _check_directory(self) -> None:
        _require(_directory_identity(self.state_dir.parent) == self._root_identity
                 and _directory_identity(self.state_dir) == self._directory_identity,
                 "UNSAFE_CANDIDATE_ACTIVATION_DIRECTORY")

    def _check_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            self._break("CANDIDATE_ACTIVATION_PROCESS_CHANGED")
            raise ActivationStateError("CANDIDATE_ACTIVATION_PROCESS_CHANGED")

    def _check_file(self, path: Path, expected: bytes, code: str, *, identity: tuple | None = None) -> tuple:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(fd)
            _require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
                     and stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
                     and metadata.st_size == len(expected) <= 128 * 1024, code)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                observed = stream.read(len(expected) + 1)
            after = os.fstat(fd)
            current = path.lstat()
            fields = lambda item: (
                item.st_dev, item.st_ino, item.st_mode, item.st_uid, item.st_nlink,
                item.st_size, item.st_mtime_ns, item.st_ctime_ns,
            )
            _require(observed == expected and fields(metadata) == fields(after) == fields(current)
                     and (identity is None or fields(metadata) == identity), code)
            return fields(after)
        finally:
            os.close(fd)

    def _live(self) -> None:
        self._check_owner()
        _require(self._created, "CANDIDATE_ACTIVATION_STATE_NOT_CREATED")
        _require(not self._broken, "CANDIDATE_ACTIVATION_STATE_UNUSABLE")
        try:
            self._check_directory()
            self._check_file(self.path, self._persisted, "CANDIDATE_ACTIVATION_STATE_CHANGED",
                             identity=self._journal_identity)
            if self._marker is not None:
                self._check_file(self.marker_path, self._marker, "CANDIDATE_ACTIVATION_MARKER_CHANGED",
                                 identity=self._marker_identity)
        except (OSError, RuntimeError) as error:
            code = str(error) if isinstance(error, ActivationStateError) and str(error) in ERROR_CODES else "CANDIDATE_ACTIVATION_STATE_CHANGED"
            self._break(code)
            raise ActivationStateError(code) from None

    def _event(self, data: dict, phase: str) -> None:
        data["phase"] = phase
        data["sequence"] += 1
        data["updated_at"] = datetime.now(UTC).isoformat()
        data["history"].append({
            "sequence": data["sequence"], "phase": phase, "at": data["updated_at"],
        })

    def _write(self, data: dict, *, initial: bool = False) -> None:
        self._check_owner()
        try:
            self._check_directory()
            if initial:
                _exclusive_write(self.path, data)
            else:
                ops.save(self.path, data)
            encoded = canonical_bytes(data)
            self._check_directory()
            identity = self._check_file(self.path, encoded, "CANDIDATE_ACTIVATION_STATE_CHANGED")
            if self._marker is not None:
                self._check_file(self.marker_path, self._marker, "CANDIDATE_ACTIVATION_MARKER_CHANGED",
                                 identity=self._marker_identity)
        except (Exception, KeyboardInterrupt):
            self._break("CANDIDATE_ACTIVATION_STATE_WRITE_FAILED")
            raise ActivationStateError("CANDIDATE_ACTIVATION_STATE_WRITE_FAILED") from None
        self._data, self._persisted = data, encoded
        # Only our own successful durable write may replace the journal inode.
        self._journal_identity = identity

    def create(self) -> None:
        self._check_owner()
        _require(not self._created and not self._broken, "CANDIDATE_ACTIVATION_STATE_EXISTS")
        try:
            self._check_directory()
            _require(not list(self.state_dir.glob("activation-*.json"))
                     and not list(self.state_dir.glob("activation-*.started")),
                     "CANDIDATE_ACTIVATION_STATE_EXISTS")
        except (OSError, RuntimeError) as error:
            code = str(error) if isinstance(error, ActivationStateError) and str(error) in ERROR_CODES else "CANDIDATE_ACTIVATION_STATE_CHANGED"
            self._break(code)
            raise ActivationStateError(code) from None
        data = self.data
        self._event(data, "PREPARED")
        self._write(data, initial=True)
        self._created = True

    def mark_passive_verified(self, evidence: dict) -> None:
        self._live()
        _require(self._data["phase"] == "PREPARED" and not self._activation_blocked,
                 "CANDIDATE_ACTIVATION_INVALID_TRANSITION")
        binding = self._data["binding"]
        _require(type(evidence) is dict and set(evidence) == _PASSIVE_FIELDS
                 and all(evidence[key] == binding[key] for key in ("candidate_sha", "candidate_image_id", "project"))
                 and _digest(evidence["passive_checks_sha256"])
                 and evidence["business_writes_blocked"] is True and evidence["claims_blocked"] is True,
                 "CANDIDATE_ACTIVATION_INVALID_EVIDENCE")
        data = self.data
        data["passive_evidence"] = copy.deepcopy(evidence)
        self._event(data, "PASSIVE_VERIFIED")
        self._write(data)

    def begin_activation(self) -> None:
        """Must complete before the first ACTIVE recreate/start or reconnect."""
        self._live()
        _require(self._data["phase"] == "PASSIVE_VERIFIED" and not self._activation_blocked,
                 "CANDIDATE_ACTIVATION_INVALID_TRANSITION")
        self._activation_may_have_started = True
        data = self.data
        data["activation_may_have_started"] = True
        self._event(data, "ACTIVATION_REQUESTED")
        self._write(data)
        marker = {
            "version": 1, "binding": copy.deepcopy(data["binding"]),
            "binding_sha256": data["binding_sha256"],
            "passive_evidence_sha256": _hash(data["passive_evidence"]),
            "activation_requested_at": data["updated_at"], "schema_downgrade_allowed": False,
        }
        try:
            self._check_owner()
            _exclusive_write(self.marker_path, marker)
            encoded = canonical_bytes(marker)
            self._check_directory()
            identity = self._check_file(self.marker_path, encoded, "CANDIDATE_ACTIVATION_MARKER_CHANGED")
        except (Exception, KeyboardInterrupt):
            self._break("CANDIDATE_ACTIVATION_STATE_WRITE_FAILED")
            raise ActivationStateError("CANDIDATE_ACTIVATION_STATE_WRITE_FAILED") from None
        self._marker, self._boundary_recorded = encoded, True
        self._marker_identity = identity
        data = self.data
        data["activation_boundary_recorded"] = True
        self._event(data, "ACTIVATION_STARTED")
        self._write(data)
        self.assert_activation_permitted()

    def assert_activation_permitted(self) -> None:
        self._live()
        _require(self._data["phase"] == "ACTIVATION_STARTED" and self._boundary_recorded
                 and self._marker is not None and not self._activation_blocked,
                 "CANDIDATE_ACTIVATION_INVALID_TRANSITION")

    def mark_active(self) -> None:
        self.assert_activation_permitted()
        data = self.data
        self._event(data, "ACTIVE")
        self._write(data)

    def request_containment(self, error_code: str) -> bool:
        """Revoke this object's permit even if recording fails; never block stop."""
        self._activation_blocked = True
        if type(error_code) is not str or error_code not in FAILURE_CODES:
            self._break("CANDIDATE_ACTIVATION_UNSAFE_ERROR")
            return False
        if self._broken or not self._created:
            return False
        try:
            self._live()
            if self._data["phase"] in {"CONTAINMENT_REQUESTED", "CONTAINED", "OPERATOR_RECOVERY_REQUIRED"}:
                return True
            data = self.data
            data["failure_code"] = error_code
            self._event(data, "CONTAINMENT_REQUESTED")
            self._write(data)
            return True
        except (Exception, KeyboardInterrupt):
            self._break(self._error or "CANDIDATE_ACTIVATION_STATE_WRITE_FAILED")
            return False

    def finish_containment(self, verified: bool, *, evidence: dict | None = None) -> dict:
        """Record independent physical/data checks, never their desired outcome."""
        self._activation_blocked = True
        if self._broken or not self._created:
            return self.summary()
        try:
            self._live()
            _require(self._data["phase"] == "CONTAINMENT_REQUESTED",
                     "CANDIDATE_ACTIVATION_INVALID_TRANSITION")
            _require(type(verified) is bool, "CANDIDATE_ACTIVATION_INVALID_EVIDENCE")
            if verified:
                _require(self._boundary_recorded and type(evidence) is dict and set(evidence) == _CONTAINMENT_FIELDS
                         and evidence["target_schema_head"] == self._data["binding"]["target_schema_head"]
                         and all(evidence[key] is True for key in (
                             "committed_write_preserved", "new_claims_blocked", "runtime_stopped",
                         )) and _digest(evidence["evidence_sha256"]),
                         "CANDIDATE_ACTIVATION_INVALID_EVIDENCE")
            else:
                _require(evidence is None, "CANDIDATE_ACTIVATION_INVALID_EVIDENCE")
            data = self.data
            data["containment_evidence"] = copy.deepcopy(evidence)
            self._event(data, "CONTAINED" if verified else "OPERATOR_RECOVERY_REQUIRED")
            self._write(data)
        except (Exception, KeyboardInterrupt) as error:
            code = str(error) if isinstance(error, ActivationStateError) and str(error) in ERROR_CODES else "CANDIDATE_ACTIVATION_STATE_WRITE_FAILED"
            self._break(code)
        return self.summary()

    def summary(self) -> dict:
        if self._created and not self._broken:
            try:
                self._live()
            except (Exception, KeyboardInterrupt):
                self._break(self._error or "CANDIDATE_ACTIVATION_STATE_CHANGED")
        verified = not self._broken and self._created and self._data["phase"] == "CONTAINED"
        return {
            "state_contract_version": 1, **copy.deepcopy(self._data["binding"]),
            "phase": "OPERATOR_RECOVERY_REQUIRED" if self._broken else self._data["phase"],
            "state_durability": "UNKNOWN" if self._broken or not self._created else "CONFIRMED",
            "activation_boundary_recorded": self._boundary_recorded,
            "activation_may_have_started": self._activation_may_have_started,
            "containment_verified": verified, "committed_write_preserved": verified,
            "target_schema_preserved": verified,
            "error_code": self._error or self._data["failure_code"],
            "automatic_retry": False, "schema_downgrade_allowed": False,
            "production_approval_verified": False, "production_activation_authorized": False,
            "external_side_effects_authorized": False,
        }
