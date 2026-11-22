#!/usr/bin/env python3
"""Apply an authenticated migration only up to the closed writer boundary.

Success deliberately leaves production applications stopped and PostgreSQL
network-fenced. This stage has no activation, automatic resume or live restore.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import secrets
from pathlib import Path
from typing import Any, Protocol

import migration_apply_backup as backup
import migration_release_approval as approval
import release_ops as ops
from migration_apply_state import ACTION_PHASES, ERROR_CODES, FAILURE_CODES, Journal
from release_manifest import ManifestContractError, validate_migration_manifest


class Runtime(Protocol):
    def inventory(self) -> dict[str, Any]: ...
    def quiet(self) -> None: ...
    def backup(self) -> dict[str, Any]: ...
    def stop_schedulers(self) -> None: ...
    def drain_workers(self) -> None: ...
    def stop_writers(self) -> None: ...
    def fence_database(self) -> None: ...
    def barrier(self) -> dict[str, Any]: ...
    def snapshot(self, tables: list[str] | None = None) -> dict[str, Any]: ...
    def target_preflight(self) -> None: ...
    def upgrade_exact(self) -> None: ...
    def target_checks(self) -> None: ...
    def maintenance_readiness(self) -> None: ...
    def downgrade_exact(self) -> None: ...
    def source_checks(self) -> None: ...
    def restore_source_configuration(self) -> None: ...
    def contain(self) -> None: ...
    def close(self) -> None: ...


class ApplyFailure(ops.ReleaseError):
    def __init__(self, result: dict[str, Any]):
        super().__init__(result["error_code"])
        self.result = result


def failure_code(error: BaseException, action: str) -> str:
    if isinstance(error, ops.ReleaseError) and str(error) in FAILURE_CODES:
        return str(error)
    return {
        "backup": "MIGRATION_BACKUP_BINDING_MISMATCH",
        "drain_workers": "MIGRATION_DRAIN_TIMEOUT",
        "fence_database": "MIGRATION_WRITER_BARRIER_LOST",
        "source_checks": "MIGRATION_SOURCE_CHECKS_FAILED",
        "target_preflight": "MIGRATION_TARGET_PREFLIGHT_FAILED",
        "target_checks": "MIGRATION_TARGET_CHECKS_FAILED",
        "readiness": "MIGRATION_READINESS_FAILED",
        "downgrade": "MIGRATION_DOWNGRADE_FAILED",
        "restore_source_configuration": "MIGRATION_SOURCE_RESTORE_FAILED",
        "containment": "MIGRATION_CONTAINMENT_FAILED",
    }.get(action, "UNEXPECTED_MIGRATION_APPLY_ERROR")


def barrier_evidence(value: Any) -> dict[str, Any]:
    ops.require(
        type(value) is dict
        and all(value.get(key) is True for key in (
            "writers_stopped", "database_fenced", "barrier_continuous",
        ))
        and isinstance(value.get("barrier_sha256"), str)
        and ops.DIGEST.fullmatch(value["barrier_sha256"]),
        "MIGRATION_WRITER_BARRIER_LOST",
    )
    return {
        "barrier_sha256": value["barrier_sha256"],
        "checks": {"writers_stopped": True, "database_fenced": True},
    }


def snapshot_evidence(
    value: Any, schema: str, database_identity: str, expected: dict | None = None,
) -> dict[str, Any]:
    backup.validate_snapshot(value)
    ops.require(value["schema"] == schema, "MIGRATION_SCHEMA_MISMATCH")
    ops.require(
        value["database_identity_sha256"] == database_identity,
        "MIGRATION_SOURCE_CHANGED",
    )
    state = value["business_state"]
    if expected is not None:
        ops.require(state == expected, "MIGRATION_DATA_CHANGED")
    return {
        "schema": schema, "database_identity_sha256": database_identity,
        "business_sha256": state["sha256"], "table_counts": state["table_counts"],
    }


def execute(runtime: Runtime, journal: Journal, *, progress=None) -> dict[str, Any]:
    """Caller authenticated receipt and holds the shared static/migration lock."""
    manifest = journal.data["manifest"]
    validate_migration_manifest(manifest)
    inventory = runtime.inventory()
    runtime.quiet()
    source = inventory["snapshot"]
    database_identity = source["database_identity_sha256"]
    initial = snapshot_evidence(source, manifest["source_schema_head"], database_identity)
    ops.require(source["build_sha"] == manifest["source_release_sha"], "MIGRATION_SOURCE_CHANGED")
    initial.update(
        source_release_sha=manifest["source_release_sha"],
        source_image_ids=inventory["source_image_ids"],
        runtime_identity_sha256=inventory["runtime_identity_sha256"],
        checks={"source_verified": True},
    )
    journal.create(initial)
    # A failed consume may have created a durable marker. No runtime mutation is
    # attempted, and the journal is never automatically loaded or retried.
    journal.consume()
    baseline: dict | None = None
    fence_proven = False
    action = "backup"

    def perform(name: str, function, evidence=None):
        nonlocal action
        action = name
        journal.begin(name)
        if progress is not None:
            progress(name)
        result = function()
        details = evidence(result) if evidence is not None else None
        journal.complete(ACTION_PHASES[name], details)
        return result

    def checked_barrier() -> dict:
        return barrier_evidence(runtime.barrier())

    def matching(schema: str) -> dict:
        checked_barrier()
        value = runtime.snapshot(sorted(baseline["table_counts"]))
        evidence = snapshot_evidence(value, schema, database_identity, baseline)
        # Observation and the read-only snapshot must both remain within the fence.
        evidence.update(checked_barrier())
        return evidence

    def source_checks() -> dict:
        runtime.source_checks()
        return matching(manifest["source_schema_head"])

    try:
        def checked_backup():
            proof = runtime.backup()
            backup.validate_proof(proof, {
                "operation_id": journal.data["operation_id"],
                "manifest_sha256": journal.data["manifest_sha256"],
                "database_identity_sha256": database_identity,
                "source_release_sha": manifest["source_release_sha"],
                "source_schema_head": manifest["source_schema_head"],
                "source_backend_image": manifest["source_images"]["backend"],
            })
            ops.require(
                proof["operation_id"] == journal.data["operation_id"]
                and proof["manifest_sha256"] == journal.data["manifest_sha256"]
                and proof["database_identity_sha256"] == database_identity
                and proof["source_release_sha"] == manifest["source_release_sha"]
                and proof["source_schema_head"] == manifest["source_schema_head"]
                and proof["source_backend_image"] == manifest["source_images"]["backend"],
                "MIGRATION_BACKUP_BINDING_MISMATCH",
            )
            return proof

        proof = perform("backup", checked_backup, lambda value: {
            "encrypted_backup_sha256": value["encrypted_backup_sha256"],
            "source_business_sha256": value["source_business_state"]["sha256"],
            "restored_business_sha256": value["restored_business_state"]["sha256"],
            "table_counts": value["source_business_state"]["table_counts"],
            "checks": {"backup_verified": True, "restored_data_verified": True},
        })
        baseline = copy.deepcopy(proof["source_business_state"])
        perform("stop_schedulers", runtime.stop_schedulers, lambda _: {
            "checks": {"schedulers_stopped": True},
        })
        def drained():
            runtime.drain_workers()
            runtime.quiet()
        perform("drain_workers", drained, lambda _: {"checks": {"workers_drained": True}})
        perform("stop_writers", runtime.stop_writers, lambda _: {"checks": {"writers_stopped": True}})
        perform("fence_database", runtime.fence_database, lambda _: checked_barrier())
        fence_proven = True
        # No new snapshot is substituted when writers changed data after backup.
        perform("source_checks", source_checks, lambda value: value)
        perform("target_preflight", runtime.target_preflight, lambda _: {
            **checked_barrier(), "checks": {
                "writers_stopped": True, "database_fenced": True,
                "target_preflight_verified": True,
            },
        })
        def upgrade():
            matching(manifest["source_schema_head"])
            runtime.upgrade_exact()
            return matching(manifest["target_schema_head"])
        perform("upgrade", upgrade, lambda value: value)
        def target_checks():
            runtime.target_checks()
            return matching(manifest["target_schema_head"])
        perform("target_checks", target_checks, lambda value: {
            **value, "checks": {
                "writers_stopped": True, "database_fenced": True,
                "target_schema_verified": True, "invariants_verified": True,
            },
        })
        def ready():
            runtime.maintenance_readiness()
            return matching(manifest["target_schema_head"])
        perform("readiness", ready, lambda value: {
            **value, "checks": {
                "writers_stopped": True, "database_fenced": True,
                "maintenance_readiness": True, "cleanup_complete": True,
            },
        })
        journal.finish("PRE_ACTIVATION_READY")
        return {
            "status": "PRE_ACTIVATION_READY", "operation_id": journal.data["operation_id"],
            "schema": manifest["target_schema_head"], "writer_boundary": "PROVEN_CLOSED",
            "business_sha256": baseline["sha256"], "automatic_retry": False,
        }
    except BaseException as error:
        original = failure_code(error, action)
        recovery_error = None
        journal_usable = True
        try:
            journal.fail_action(original)
        except BaseException:
            journal_usable = False
        # Even when state durability is unknown, containment is permitted; schema
        # recovery is not. No live database backup is ever restored here.
        try:
            if journal_usable:
                perform("containment", runtime.contain, lambda _: {
                    **checked_barrier(), "checks": {
                        "writers_stopped": True, "database_fenced": True,
                        "containment_verified": True,
                    },
                })
            else:
                runtime.contain()
        except BaseException as containment_error:
            recovery_error = failure_code(containment_error, "containment")
            journal_usable = False
        outcome = "OPERATOR_RECOVERY_REQUIRED"
        if journal_usable and fence_proven and baseline is not None:
            try:
                checked_barrier()
                current = runtime.snapshot(sorted(baseline["table_counts"]))
                backup.validate_snapshot(current)
                ops.require(
                    current["database_identity_sha256"] == database_identity
                    and current["business_state"] == baseline,
                    "MIGRATION_DATA_CHANGED",
                )
                if current["schema"] == manifest["target_schema_head"]:
                    ops.require(
                        manifest["pre_activation_rollback_policy"] == "PRE_ACTIVATION_DOWNGRADE",
                        "MIGRATION_DOWNGRADE_FAILED",
                    )
                    def downgrade():
                        matching(manifest["target_schema_head"])
                        runtime.target_preflight()
                        checked_barrier()
                        runtime.downgrade_exact()
                        return matching(manifest["source_schema_head"])
                    perform("downgrade", downgrade, lambda value: value)
                else:
                    ops.require(current["schema"] == manifest["source_schema_head"], "MIGRATION_SCHEMA_MISMATCH")
                perform("source_checks", source_checks, lambda value: {
                    **value, "checks": {"source_schema_verified": True},
                })
                perform("restore_source_configuration", runtime.restore_source_configuration, lambda _: {
                    **checked_barrier(), "checks": {
                        "source_configuration_restored": True, "writers_stopped": True,
                        "database_fenced": True,
                    },
                })
                journal.finish("ROLLED_BACK_PRE_ACTIVATION")
                outcome = "ROLLED_BACK_PRE_ACTIVATION"
            except BaseException as recovery:
                recovery_error = failure_code(recovery, action)
        if outcome == "OPERATOR_RECOVERY_REQUIRED":
            try:
                if journal.has_pending:
                    journal.fail_action(recovery_error or original)
                journal.finish(outcome, original, recovery_error)
            except BaseException:
                pass  # The existing pending intent/consumed marker blocks replay.
        raise ApplyFailure({
            "status": outcome, "operation_id": journal.data["operation_id"],
            "error_code": original, "recovery_error_code": recovery_error,
            "writer_boundary": "PROVEN_CLOSED" if outcome == "ROLLED_BACK_PRE_ACTIVATION" else "UNKNOWN",
            "automatic_retry": False,
        }) from None


def state_directory() -> Path:
    return Path.home() / "iTeam-private" / "release-operations" / "iteam-ai-marketing"


def apply(root: Path, approval_run: int, artifact_id: int, backup_dir: Path, key_dir: Path, cosign: str) -> dict:
    from migration_apply_runtime import production_runtime
    state = state_directory()
    with ops.locked_state(state):
        # A different approval must not silently continue an interrupted or
        # maintenance-held migration. Activation/recovery is a separate stage.
        ops.require(not list(state.glob("operation-*.json")), "MIGRATION_OPERATION_ALREADY_EXISTS")
        receipt = approval.verify_approved(approval_run, artifact_id, cosign)
        run = approval.trusted_run(approval_run, approval.WORKFLOW, "workflow_dispatch")
        approval.approval_fresh(run)
        operation = secrets.token_hex(16)
        manifest = receipt["review"]["migration_manifest"]
        journal = Journal(state, operation, manifest, {
            "approval_run_id": approval_run, "artifact_id": artifact_id,
            "receipt_sha256": approval.sha256(approval.encode(receipt)),
            "approved_at": run["updated_at"],
        })
        runtime = production_runtime(root, manifest, operation, state, backup_dir, key_dir)
        try:
            return execute(runtime, journal, progress=lambda phase: print(json.dumps({"status": "RUNNING", "phase": phase}), flush=True))
        finally:
            runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--approval-run", type=int, required=True)
    parser.add_argument("--approval-artifact", type=int, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--key-dir", type=Path, required=True)
    parser.add_argument("--cosign", default="cosign")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = apply(args.root, args.approval_run, args.approval_artifact, args.backup_dir, args.key_dir, args.cosign)
        print(json.dumps(result, sort_keys=True))
    except ApplyFailure as error:
        print(json.dumps(error.result, sort_keys=True))
        raise SystemExit(1) from None
    except (Exception, KeyboardInterrupt) as error:
        code = str(error) if isinstance(error, ops.ReleaseError) and str(error) in ERROR_CODES | FAILURE_CODES else "UNEXPECTED_MIGRATION_APPLY_ERROR"
        print(json.dumps({"status": "FAIL", "error_code": code, "automatic_retry": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
