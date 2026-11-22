#!/usr/bin/env python3
"""Inspect existing migration records without resuming or authorizing execution.

The report describes the consistency of saved records only. A stopped process
cannot recover its in-memory writer-barrier observation from those records.
No Docker, database, receipt-verification, backup-key or ciphertext access occurs.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import migration_apply_backup as backup
import migration_apply_state as state
import release_ops as ops
from migration_release_apply import state_directory
from migration_release_approval import MAX_BYTES, parse_json
from release_manifest import ManifestContractError, validate_migration_manifest

_OPERATION = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_ID = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_EVENT_FIELDS = {"sequence", "kind", "phase", "action", "at", "recovery"}
_JOURNAL_FIELDS = {
    "version", "repository", "operation_id", "manifest", "manifest_sha256",
    "approval", "phase", "status", "pending_action", "sequence", "consumed_at",
    "created_at", "updated_at", "failure_code", "recovery_error_code",
    "writer_activation_requested", "history",
}
_SNAPSHOT_FIELDS = {
    "schema", "database_identity_sha256", "business_sha256", "table_counts",
}
_INITIAL_FIELDS = _SNAPSHOT_FIELDS | {
    "source_release_sha", "source_image_ids", "runtime_identity_sha256", "checks",
}
_NETWORK_FIELDS = {
    "NetworkID", "EndpointID", "Aliases", "IPAMConfig", "IPAddress",
    "IPPrefixLen", "Gateway", "GlobalIPv6Address", "GlobalIPv6PrefixLen",
    "IPv6Gateway", "MacAddress",
}
_MISSING = object()
_ERROR_STATES = {
    **{name: "INVALID" for name in (
        "INVALID_OPERATION", "UNSAFE_DIRECTORY", "UNSAFE_FILE", "INVALID_JSON",
        "INVALID_JOURNAL", "MARKER_MISMATCH", "RUNTIME_MISMATCH",
        "BACKUP_MISMATCH", "STATE_CHANGED", "INVALID_TIME", "INVALID_ARGUMENTS",
    )},
    **{name: "UNAVAILABLE" for name in (
        "NOT_FOUND", "LOCK_UNAVAILABLE", "UNAVAILABLE",
    )},
    "INCOMPLETE": "INCOMPLETE",
}


class _InspectionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__("MIGRATION_INSPECTION_" + code)


def _require(value: Any, code: str) -> None:
    if not value:
        raise _InspectionError(code)


def _exact(value: Any, fields: set[str], code: str) -> None:
    _require(type(value) is dict and set(value) == fields, code)


def _time(value: Any, code: str) -> datetime:
    _require(type(value) is str and 0 < len(value) <= 64, code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(parsed.utcoffset() is not None, code)
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise _InspectionError(code) from None


def _digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _number(value: Any, *, minimum: int = 0, maximum: int = 2**63 - 1) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _identity(metadata: os.stat_result) -> tuple:
    # Reading may change atime; it must not change content, ownership or mode.
    return (
        metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
        metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
    )


def _private_file(metadata: os.stat_result, *, lock: bool = False) -> None:
    _require(
        stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600 and metadata.st_nlink == 1
        and (metadata.st_size == 0 if lock else 0 < metadata.st_size <= MAX_BYTES),
        "UNSAFE_FILE",
    )


class _Records:
    """Read a stable, bounded snapshot under the existing shared lock."""

    def __init__(self, path: Path, directory_fd: int, metadata: os.stat_result):
        self.path = path
        self.directory_fd = directory_fd
        self.directory_identity = _identity(metadata)
        self.observed: dict[str, tuple | None] = {}

    def open(self, name: str, *, lock: bool = False) -> int | None:
        try:
            metadata = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            self.observed[name] = None
            return None
        _private_file(metadata, lock=lock)
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=self.directory_fd,
            )
        except OSError:
            raise _InspectionError("STATE_CHANGED") from None
        try:
            opened = os.fstat(fd)
            _private_file(opened, lock=lock)
            _require(_identity(opened) == _identity(metadata), "STATE_CHANGED")
            self.observed[name] = _identity(opened)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, name: str) -> Any:
        fd = self.open(name)
        if fd is None:
            return _MISSING
        try:
            chunks = []
            remaining = MAX_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            _require(len(data) <= MAX_BYTES, "UNSAFE_FILE")
            _require(_identity(os.fstat(fd)) == self.observed[name], "STATE_CHANGED")
            _require(len(data) == self.observed[name][5], "STATE_CHANGED")
            try:
                return parse_json(data)
            except (ops.ReleaseError, ValueError, TypeError, RecursionError):
                raise _InspectionError("INVALID_JSON") from None
        finally:
            os.close(fd)

    def check(self) -> None:
        try:
            _require(self.path.resolve(strict=True) == self.path, "STATE_CHANGED")
            metadata = self.path.lstat()
            _require(_identity(metadata) == self.directory_identity, "STATE_CHANGED")
            _require(_identity(os.fstat(self.directory_fd)) == self.directory_identity, "STATE_CHANGED")
            for name, expected in self.observed.items():
                try:
                    actual = _identity(os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False))
                except FileNotFoundError:
                    actual = None
                _require(actual == expected, "STATE_CHANGED")
        except (OSError, RuntimeError):
            raise _InspectionError("STATE_CHANGED") from None


@contextlib.contextmanager
def _locked_records(path: Path):
    _require(isinstance(path, Path) and path.is_absolute(), "UNSAFE_DIRECTORY")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise _InspectionError("NOT_FOUND") from None
    _require(
        stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700,
        "UNSAFE_DIRECTORY",
    )
    try:
        _require(path.resolve(strict=True) == path, "UNSAFE_DIRECTORY")
    except (OSError, RuntimeError):
        raise _InspectionError("UNSAFE_DIRECTORY") from None
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    lock_fd = None
    try:
        _require(_identity(os.fstat(directory_fd)) == _identity(metadata), "STATE_CHANGED")
        records = _Records(path, directory_fd, metadata)
        lock_fd = records.open("operation.lock", lock=True)
        _require(lock_fd is not None, "LOCK_UNAVAILABLE")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            raise _InspectionError("LOCK_UNAVAILABLE") from None
        records.check()
        yield records
        records.check()
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)


def _business(digest: Any, counts: Any, code: str) -> None:
    try:
        backup.validate_business_state({"sha256": digest, "table_counts": counts})
        state.validate_evidence({"table_counts": counts})
    except (ops.ReleaseError, TypeError, KeyError, ValueError):
        raise _InspectionError(code) from None


def _validate_evidence(event: dict, manifest: dict, initial: dict, baseline: dict | None) -> None:
    code = "INVALID_JOURNAL"
    value = event["evidence"]
    try:
        state.validate_evidence(value)
    except (ops.ReleaseError, TypeError, ValueError):
        raise _InspectionError(code) from None
    if event["kind"] == "created":
        _exact(value, _INITIAL_FIELDS, code)
        _require(
            value["schema"] == manifest["source_schema_head"]
            and value["source_release_sha"] == manifest["source_release_sha"]
            and set(value["source_image_ids"]) == set(ops.APP)
            and value["checks"] == {"source_verified": True}, code,
        )
        _business(value["business_sha256"], value["table_counts"], code)
        return
    action = event["action"]
    checks = {
        "backup": {"backup_verified", "restored_data_verified"},
        "stop_schedulers": {"schedulers_stopped"},
        "drain_workers": {"workers_drained"},
        "stop_writers": {"writers_stopped"},
        "fence_database": {"writers_stopped", "database_fenced"},
        "source_checks": {"source_schema_verified"} if event["recovery"] else {"writers_stopped", "database_fenced"},
        "target_preflight": {"writers_stopped", "database_fenced", "target_preflight_verified"},
        "upgrade": {"writers_stopped", "database_fenced"},
        "target_checks": {"writers_stopped", "database_fenced", "target_schema_verified", "invariants_verified"},
        "readiness": {"writers_stopped", "database_fenced", "maintenance_readiness", "cleanup_complete"},
        "containment": {"writers_stopped", "database_fenced", "containment_verified"},
        "downgrade": {"writers_stopped", "database_fenced"},
        "restore_source_configuration": {"writers_stopped", "database_fenced", "source_configuration_restored"},
    }[action]
    _require(value.get("checks") == dict.fromkeys(checks, True), code)
    if action == "backup":
        _exact(value, {
            "encrypted_backup_sha256", "source_business_sha256", "restored_business_sha256",
            "table_counts", "checks",
        }, code)
        _require(value["source_business_sha256"] == value["restored_business_sha256"], code)
        _business(value["source_business_sha256"], value["table_counts"], code)
    elif action in {"stop_schedulers", "drain_workers", "stop_writers"}:
        _exact(value, {"checks"}, code)
    elif action in {"fence_database", "target_preflight", "containment", "restore_source_configuration"}:
        _exact(value, {"barrier_sha256", "checks"}, code)
    else:
        _exact(value, _SNAPSHOT_FIELDS | {"barrier_sha256", "checks"}, code)
        side = "target" if action in {"upgrade", "target_checks", "readiness"} else "source"
        _require(
            baseline is not None and value["schema"] == manifest[side + "_schema_head"]
            and value["database_identity_sha256"] == initial["database_identity_sha256"]
            and value["business_sha256"] == baseline["source_business_sha256"]
            and value["table_counts"] == baseline["table_counts"], code,
        )


def _validate_journal(value: Any, operation_id: str) -> dict:
    """Replay records as data; never create a live Journal or recovery authority."""
    code = "INVALID_JOURNAL"
    _exact(value, _JOURNAL_FIELDS, code)
    _require(
        type(value["version"]) is int and value["version"] == 1
        and value["repository"] == ops.REPO and value["operation_id"] == operation_id
        and value["writer_activation_requested"] is False, code,
    )
    try:
        validate_migration_manifest(value["manifest"])
    except (ManifestContractError, KeyError, TypeError, ValueError):
        raise _InspectionError(code) from None
    _require(value["manifest_sha256"] == state.manifest_hash(value["manifest"]), code)
    approval = value["approval"]
    _exact(approval, {"approval_run_id", "artifact_id", "receipt_sha256", "approved_at"}, code)
    _require(
        _number(approval["approval_run_id"], minimum=1)
        and _number(approval["artifact_id"], minimum=1)
        and _digest(approval["receipt_sha256"]), code,
    )
    approved = _time(approval["approved_at"], code)
    created = _time(value["created_at"], code)
    updated = _time(value["updated_at"], code)
    consumed = None if value["consumed_at"] is None else _time(value["consumed_at"], code)
    _require(type(value["status"]) is str and value["status"] in state.TERMINAL_STATUSES | {"RUNNING"}, code)
    for key in ("failure_code", "recovery_error_code"):
        _require(value[key] is None or type(value[key]) is str and value[key] in state.FAILURE_CODES, code)
    history = value["history"]
    _require(
        type(history) is list and 1 <= len(history) <= 128
        and _number(value["sequence"], minimum=1) and value["sequence"] == len(history), code,
    )
    phase, pending, failure, recovery_error = "VERIFIED", None, None, None
    previous = created
    initial = baseline = None
    consumed_event = finished = False
    attempts: set[tuple[str, bool]] = set()
    completed: set[tuple[str, bool]] = set()
    backup_begin = backup_end = None
    barrier_sha256 = None
    for sequence, event in enumerate(history, 1):
        _require(type(event) is dict and type(event.get("kind")) is str, code)
        kind = event["kind"]
        _require(kind in {"created", "consumed", "begin", "complete", "failed", "finished"}, code)
        extra = {"evidence"} if kind in {"created", "complete"} else {"error_code"} if kind == "failed" else set()
        _exact(event, _EVENT_FIELDS | extra, code)
        _require(type(event["sequence"]) is int and event["sequence"] == sequence and not finished, code)
        _require(type(event["recovery"]) is bool, code)
        at = _time(event["at"], code)
        _require(previous <= at, code)
        previous_event = previous
        previous = at
        if kind == "created":
            _require(sequence == 1, code)
            _validate_evidence(event, value["manifest"], {}, None)
            initial = event["evidence"]
        else:
            _require(sequence > 1 and initial is not None, code)
        if kind == "consumed":
            _require(not consumed_event and phase == "VERIFIED" and pending is None and failure is None, code)
            _require(consumed is not None and previous_event <= consumed <= at and 0 <= (consumed - approved).total_seconds() <= 3600, code)
            consumed_event, phase = True, "APPROVAL_CONSUMED"
        elif kind == "begin":
            action = event["action"]
            _require(consumed_event and pending is None and type(action) is str and action in state.ACTION_PHASES, code)
            recovery = failure is not None
            if recovery:
                valid = action in state.RECOVERY_ACTIONS and (
                    action == "containment"
                    or action == "downgrade" and phase == "CONTAINED"
                    or action == "source_checks" and phase in {"CONTAINED", "DOWNGRADE_COMPLETED"}
                    or action == "restore_source_configuration" and phase == "SOURCE_VERIFIED"
                )
            else:
                valid = state.FORWARD_PREDECESSORS.get(action) == phase
            _require(valid and (action, recovery) not in attempts, code)
            if recovery and action != "containment":
                # execute() permits schema/source recovery only after its
                # original fence and its guarded containment both completed.
                _require(("fence_database", False) in completed and ("containment", True) in completed and baseline is not None, code)
            if action == "downgrade":
                _require(value["manifest"]["pre_activation_rollback_policy"] == "PRE_ACTIVATION_DOWNGRADE", code)
            attempts.add((action, recovery))
            pending = action
            if action == "backup":
                backup_begin = at
        elif kind == "complete":
            _require(pending is not None and event["action"] == pending, code)
            _validate_evidence(event, value["manifest"], initial, baseline)
            recorded_barrier = event["evidence"].get("barrier_sha256")
            if recorded_barrier is not None:
                # The recorded barrier binds one operation, original runtime
                # and stopped-writer identities. Containment may be its first
                # recorded observation, but later checks cannot replace it.
                _require(barrier_sha256 is None or recorded_barrier == barrier_sha256, code)
                barrier_sha256 = recorded_barrier
            phase = state.ACTION_PHASES[pending]
            completed.add((pending, failure is not None))
            if pending == "backup":
                baseline, backup_end = event["evidence"], at
        elif kind == "failed":
            _require(pending is not None and event["action"] == pending and type(event["error_code"]) is str and event["error_code"] in state.FAILURE_CODES, code)
            if failure is None:
                failure = event["error_code"]
            else:
                recovery_error = event["error_code"]
        elif kind == "finished":
            status = value["status"]
            _require(status in state.TERMINAL_STATUSES, code)
            _require(failure is None or value["failure_code"] == failure, code)
            failure = value["failure_code"]
            if status == "PRE_ACTIVATION_READY":
                valid = phase == "MAINTENANCE_READY" and pending is None and failure is None and recovery_error is None and value["recovery_error_code"] is None
            elif status == "ROLLED_BACK_PRE_ACTIVATION":
                valid = phase == "SOURCE_CONFIGURATION_RESTORED" and pending is None and failure is not None and recovery_error is None and value["recovery_error_code"] is None
            elif status == "FAILED_BEFORE_MUTATION":
                valid = not consumed_event and phase == "VERIFIED" and pending is None and failure is not None
            else:
                valid = failure is not None
            _require(valid, code)
            if recovery_error is not None:
                _require(value["recovery_error_code"] is not None, code)
            recovery_error = value["recovery_error_code"]
            finished = True
        _require(event["phase"] == phase and event["action"] == pending and event["recovery"] is (failure is not None), code)
        if kind in {"complete", "failed"}:
            pending = None
    _require(
        value["phase"] == phase and value["pending_action"] == pending
        and value["failure_code"] == failure and value["recovery_error_code"] == recovery_error
        and updated == previous and (consumed is not None) is consumed_event
        and (value["status"] != "RUNNING") is finished, code,
    )
    return {
        "initial": initial, "baseline": baseline, "approved": approved,
        "created": created, "updated": updated, "consumed": consumed, "backup_begin": backup_begin,
        "backup_end": backup_end,
    }


def _validate_marker(marker: Any, journal: dict, replay: dict) -> None:
    code = "MARKER_MISMATCH"
    _exact(marker, {
        "version", "operation_id", "approval_run_id", "artifact_id", "receipt_sha256",
        "manifest_sha256", "consumed_at",
    }, code)
    _require(
        type(marker["version"]) is int and marker["version"] == 1
        and marker["operation_id"] == journal["operation_id"]
        and marker["manifest_sha256"] == journal["manifest_sha256"]
        and all(type(marker[key]) is type(journal["approval"][key]) and marker[key] == journal["approval"][key]
                for key in ("approval_run_id", "artifact_id", "receipt_sha256")), code,
    )
    consumed = _time(marker["consumed_at"], code)
    _require(replay["created"] <= consumed and 0 <= (consumed - replay["approved"]).total_seconds() <= 3600, code)
    if replay["consumed"] is not None:
        _require(marker["consumed_at"] == journal["consumed_at"], code)
    else:
        _require(journal["phase"] == "VERIFIED" and journal["status"] == "RUNNING" and replay["updated"] <= consumed, code)


def _ip(value: Any, *, version: int | None = None) -> bool:
    if value is None or value == "":
        return True
    if type(value) is not str:
        return False
    try:
        parsed = ipaddress.ip_address(value)
        return version is None or parsed.version == version
    except ValueError:
        return False


def _validate_runtime(runtime: Any, journal: dict, replay: dict) -> None:
    code = "RUNTIME_MISMATCH"
    _exact(runtime, {
        "version", "operation_id", "manifest_sha256", "source_runtime",
        "compose_configuration_sha256", "environment_sha256",
        "database_identity_sha256", "postgres_data_volume",
    }, code)
    initial = replay["initial"]
    _require(
        type(runtime["version"]) is int and runtime["version"] == 1
        and runtime["operation_id"] == journal["operation_id"]
        and runtime["manifest_sha256"] == journal["manifest_sha256"]
        and runtime["database_identity_sha256"] == initial["database_identity_sha256"]
        and all(_digest(runtime[key]) for key in ("compose_configuration_sha256", "environment_sha256"))
        and type(runtime["postgres_data_volume"]) is str and _NAME.fullmatch(runtime["postgres_data_volume"])
        and "sha256:" + hashlib.sha256(state.canonical_bytes(runtime)).hexdigest() == initial["runtime_identity_sha256"], code,
    )
    source = runtime["source_runtime"]
    _exact(source, {"version", "operation_id", "project", "containers", "postgres_networks", "postgres_host_bindings"}, code)
    _require(
        type(source["version"]) is int and source["version"] == 1
        and source["operation_id"] == journal["operation_id"]
        and type(source["project"]) is str and _NAME.fullmatch(source["project"])
        and type(source["containers"]) is dict and set(source["containers"]) == set(ops.ALL), code,
    )
    ids = set()
    for name, container in source["containers"].items():
        _exact(container, {"id", "image_id", "started_at", "restart_count", "restart_policy"}, code)
        _require(type(container["id"]) is str and _ID.fullmatch(container["id"]) and container["id"] not in ids, code)
        ids.add(container["id"])
        _require(_digest(container["image_id"]) and _number(container["restart_count"]) and _time(container["started_at"], code).year > 1970, code)
        if name in ops.APP:
            _require(container["image_id"] == initial["source_image_ids"][name], code)
        policy = container["restart_policy"]
        _exact(policy, {"Name", "MaximumRetryCount"}, code)
        _require(type(policy["Name"]) is str and policy["Name"] in {"", "no", "always", "unless-stopped", "on-failure"} and _number(policy["MaximumRetryCount"]), code)
    networks = source["postgres_networks"]
    _require(type(networks) is dict and 0 < len(networks) <= 64, code)
    for name, network in networks.items():
        _require(type(name) is str and _NAME.fullmatch(name) and name not in {"bridge", "host", "none"}, code)
        _exact(network, _NETWORK_FIELDS, code)
        _require(type(network["NetworkID"]) is str and _ID.fullmatch(network["NetworkID"]), code)
        _require(network["EndpointID"] is None or type(network["EndpointID"]) is str and (network["EndpointID"] == "" or _ID.fullmatch(network["EndpointID"])), code)
        aliases = network["Aliases"]
        _require(aliases is None or type(aliases) is list and len(aliases) <= 128 and all(type(alias) is str and _NAME.fullmatch(alias) for alias in aliases), code)
        for key in ("IPAddress", "Gateway"):
            _require(_ip(network[key], version=4), code)
        for key in ("GlobalIPv6Address", "IPv6Gateway"):
            _require(_ip(network[key], version=6), code)
        for key, limit in (("IPPrefixLen", 32), ("GlobalIPv6PrefixLen", 128)):
            _require(network[key] is None or _number(network[key], maximum=limit), code)
        mac = network["MacAddress"]
        _require(mac is None or type(mac) is str and (mac == "" or re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac)), code)
        ipam = network["IPAMConfig"]
        if ipam is not None:
            _require(type(ipam) is dict and set(ipam) <= {"IPv4Address", "IPv6Address", "LinkLocalIPs"}, code)
            _require(_ip(ipam.get("IPv4Address"), version=4) and _ip(ipam.get("IPv6Address"), version=6), code)
            links = ipam.get("LinkLocalIPs")
            _require(links is None or type(links) is list and len(links) <= 128 and all(_ip(item) and item not in (None, "") for item in links), code)
    bindings = source["postgres_host_bindings"]
    _require(type(bindings) is list and len(bindings) <= 128, code)
    for binding in bindings:
        _exact(binding, {"HostIp", "HostPort"}, code)
        port = binding["HostPort"]
        _require(type(binding["HostIp"]) is str and _ip(binding["HostIp"]) and type(port) is str and re.fullmatch(r"[0-9]{1,5}", port) and 1 <= int(port) <= 65535, code)


def _validate_backup(proof: Any, journal: dict, replay: dict) -> datetime:
    code = "BACKUP_MISMATCH"
    manifest = journal["manifest"]
    binding = {
        "operation_id": journal["operation_id"], "manifest_sha256": journal["manifest_sha256"],
        "database_identity_sha256": replay["initial"]["database_identity_sha256"],
        "source_release_sha": manifest["source_release_sha"],
        "source_schema_head": manifest["source_schema_head"],
        "source_backend_image": manifest["source_images"]["backend"],
    }
    try:
        created = backup.validate_proof_structure(proof, binding).astimezone(UTC)
    except (ops.ReleaseError, KeyError, TypeError, ValueError, OverflowError):
        raise _InspectionError(code) from None
    _require(created == _time(proof["created_at"], code), code)
    _business(proof["source_business_state"]["sha256"], proof["source_business_state"]["table_counts"], code)
    _require(replay["backup_begin"] is not None and replay["backup_begin"] <= created, code)
    if replay["backup_end"] is not None:
        _require(created <= replay["backup_end"], code)
    elif journal["status"] != "RUNNING":
        # A failed backup may still have published its proof before failure.
        # It cannot have published it after an already-terminal operation.
        _require(created <= replay["updated"], code)
    baseline = replay["baseline"]
    if baseline is not None:
        _require(
            proof["encrypted_backup_sha256"] == baseline["encrypted_backup_sha256"]
            and proof["source_business_state"] == {
                "sha256": baseline["source_business_sha256"], "table_counts": baseline["table_counts"],
            }, code,
        )
    return created


def _report(operation_id: str | None, observed_at: datetime | None) -> dict:
    return {
        "version": 1, "operation_id": operation_id,
        "record_state": "UNAVAILABLE", "error_code": "MIGRATION_INSPECTION_UNAVAILABLE",
        "recorded_phase": None, "recorded_status": None,
        "observed_at": observed_at.isoformat() if observed_at is not None else None,
        "approval_time_eligible": None, "backup_time_eligible": None,
        "writer_boundary": "UNKNOWN", "live_runtime_checked": False,
        "historical_continuity_verified": False, "receipt_signature_verified": False,
        "automatic_retry": False, "activation_authorized": False,
        "schema_downgrade_allowed": False,
    }


def _failure(report: dict, code: str) -> dict:
    report.update(record_state=_ERROR_STATES[code], error_code="MIGRATION_INSPECTION_" + code)
    if report["record_state"] in {"INVALID", "UNAVAILABLE"}:
        # A final snapshot check may fail after validation. Do not retain even
        # allowlisted claims from an invalid or no longer stable record set.
        report.update(
            recorded_phase=None, recorded_status=None,
            approval_time_eligible=None, backup_time_eligible=None,
        )
    return report


def inspect_operation(state_dir: Path, operation_id: str, *, now: datetime | None = None) -> dict:
    """Return only allowlisted diagnostics; never infer live safety or permission."""
    valid_id = type(operation_id) is str and _OPERATION.fullmatch(operation_id) is not None
    report = _report(operation_id if valid_id else None, None)
    try:
        observed = datetime.now(UTC) if now is None else now
        _require(isinstance(observed, datetime) and observed.utcoffset() is not None, "INVALID_TIME")
        observed = observed.astimezone(UTC)
        report["observed_at"] = observed.isoformat()
        _require(valid_id, "INVALID_OPERATION")
        with _locked_records(state_dir) as records:
            journal = records.read(f"operation-{operation_id}.json")
            _require(journal is not _MISSING, "NOT_FOUND")
            replay = _validate_journal(journal, operation_id)
            marker = records.read(f"approval-{journal['approval']['approval_run_id']}.consumed")
            runtime = records.read(f"migration-runtime-{operation_id}.json")
            proof = records.read(f"migration-backup-{operation_id}.json")
            if marker is not _MISSING:
                _validate_marker(marker, journal, replay)
            if runtime is not _MISSING:
                _require(replay["backup_begin"] is not None, "RUNTIME_MISMATCH")
                _validate_runtime(runtime, journal, replay)
            proof_time = None if proof is _MISSING else _validate_backup(proof, journal, replay)
            incomplete = (
                journal["status"] == "RUNNING"
                or replay["consumed"] is not None and marker is _MISSING
                or replay["baseline"] is not None and (runtime is _MISSING or proof is _MISSING)
                or proof is not _MISSING and runtime is _MISSING
            )
            # Publish recorded fields only after the full available record set
            # has passed structural validation and the stable snapshot check.
            records.check()
            report.update(
                recorded_phase=journal["phase"], recorded_status=journal["status"],
                approval_time_eligible=0 <= (observed - replay["approved"]).total_seconds() <= 3600,
                backup_time_eligible=None if proof_time is None else 0 <= (observed - proof_time).total_seconds() <= 3600,
            )
        if incomplete:
            return _failure(report, "INCOMPLETE")
        report.update(record_state="CONSISTENT_TERMINAL", error_code=None)
        return report
    except _InspectionError as error:
        return _failure(report, error.code)
    except (Exception, KeyboardInterrupt):
        # Neither paths, payload fields nor exception messages enter output.
        return _failure(report, "UNAVAILABLE")


class _Parser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _InspectionError("INVALID_ARGUMENTS")


def main(argv: list[str] | None = None) -> None:
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--operation-id", required=True)
    try:
        args = parser.parse_args(argv)
        result = inspect_operation(state_directory(), args.operation_id)
    except (Exception, KeyboardInterrupt) as error:
        code = error.code if isinstance(error, _InspectionError) else "UNAVAILABLE"
        result = _failure(_report(None, datetime.now(UTC)), code)
    print(json.dumps(result, sort_keys=True))
    if result["record_state"] != "CONSISTENT_TERMINAL":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
