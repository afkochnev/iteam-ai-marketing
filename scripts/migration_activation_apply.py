#!/usr/bin/env python3
"""Plan or execute a new, explicitly approved migration and activation.

Execution never resumes saved pre-activation records. The pre-activation engine
and activation adapter share one live operation and release lock. The old
engine's downgrade handler has returned before any activation intent is written.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import migration_activation_approval as approval
import migration_release_apply as preapply
import release_ops as ops
from migration_activation_contract import activation_policy, hash_json
from migration_activation_journal import (
    ERROR_CODES as STATE_ERRORS,
    FAILURE_CODES,
    FIXTURE_AUTHORIZATION,
    PRODUCTION_AUTHORIZATION,
    ActivationJournal,
    operation_records_exist,
    state_directory,
)
from migration_apply_state import Journal, manifest_hash
import migration_activation_runtime as activation_runtime

ERROR_CODES = STATE_ERRORS | activation_runtime.ERROR_CODES | frozenset({
    "ACTIVATION_ARGUMENTS_INVALID", "ACTIVATION_OUTPUT_UNSAFE",
    "ACTIVATION_OPERATION_EXISTS", "ACTIVATION_RUNTIME_BINDING_MISMATCH",
    "ACTIVATION_CONTROLLER_CHECKOUT_MISMATCH", "ACTIVATION_SCOPE_MISMATCH",
    "ACTIVATION_PREAPPLY_FAILED", "ACTIVATION_OBSERVER_CLOSE_FAILED",
    "ACTIVATION_FAILED", "ACTIVATION_PASSIVE_POSTFLIGHT_FAILED",
    "ACTIVATION_HANDOFF_FAILED", "ACTIVATION_RECREATE_FAILED",
    "ACTIVATION_RECONNECT_FAILED", "ACTIVATION_START_FAILED",
    "ACTIVATION_POSTFLIGHT_FAILED", "ACTIVATION_FINALIZE_FAILED",
    "ACTIVATION_CONTAINMENT_FAILED", "INJECTED_POST_ACTIVATION_FAILURE",
})


class ActivationFailure(ops.ReleaseError):
    def __init__(self, result: dict):
        super().__init__(result["error_code"])
        self.result = result


def failure_code(error: BaseException, phase: str) -> str:
    if isinstance(error, ops.ReleaseError) and str(error) in ERROR_CODES | FAILURE_CODES:
        return str(error)
    return {
        "passive_postflight": "ACTIVATION_PASSIVE_POSTFLIGHT_FAILED",
        "handoff": "ACTIVATION_HANDOFF_FAILED",
        "recreate": "ACTIVATION_RECREATE_FAILED",
        "reconnect": "ACTIVATION_RECONNECT_FAILED",
        "start": "ACTIVATION_START_FAILED",
        "postflight": "ACTIVATION_POSTFLIGHT_FAILED",
        "finalize": "ACTIVATION_FINALIZE_FAILED",
        "close": "ACTIVATION_OBSERVER_CLOSE_FAILED",
    }.get(phase, "ACTIVATION_FAILED")


class _BoundSource:
    def __init__(self, runtime: Any, activation: ActivationJournal, expected: str):
        self.runtime, self.activation, self.expected = runtime, activation, expected
        self.plan: dict | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.runtime.source, name)

    def inventory(self) -> dict:
        self.activation.assert_fresh()
        value = self.runtime.source.inventory()
        ops.require(value.get("runtime_identity_sha256") == self.expected,
                    "ACTIVATION_RUNTIME_BINDING_MISMATCH")
        self.plan = copy.deepcopy(self.runtime.prepare_target_plan())
        self.activation.assert_fresh()
        return value

    def backup(self) -> dict:
        # The final check before the first actual runtime mutation is distinct
        # from the consumed marker's historical one-use/freshness validation.
        self.activation.assert_fresh()
        return self.runtime.source.backup()


class _FreshJournal:
    def __init__(self, journal: Journal, activation: ActivationJournal):
        self.journal, self.activation = journal, activation

    def __getattr__(self, name: str) -> Any:
        return getattr(self.journal, name)

    def consume(self) -> None:
        self.activation.assert_fresh()
        self.journal.consume()


def _result(binding: dict, state: ActivationJournal, *, status: str, **details: Any) -> dict:
    return {
        "status": status, "operation_id": binding["operation_id"],
        "authorization": binding["authorization"],
        "target_release_sha": binding["target_release_sha"],
        "target_schema_head": binding["target_schema_head"],
        "runtime_identity_sha256": binding["runtime_identity_sha256"],
        "activation_state": state.summary(), "automatic_retry": False,
        "resume_authorized": False, "schema_downgrade_allowed": False,
        "backup_restore_allowed": False,
        # The core has no verifier. Only the production CLI, after successful
        # verification, adds its live approval result. Fixture callers keep false.
        "production_approval_verified": False,
        "production_activation_authorized": False,
        "external_side_effects_authorized": False,
        **details,
    }


def execute(
    runtime: Any, preapply_journal: Journal, activation_state: ActivationJournal, *,
    expected_runtime_identity_sha256: str, progress=None, after_active=None,
) -> dict:
    """Caller verified authority and holds the common lock until this returns.

    `after_active` is an internal disposable test seam, never a production CLI
    option. It runs after actual active readiness but before normal restart
    policies and the terminal ACTIVE journal are committed.
    """
    binding = activation_state.data["binding"]
    try:
        previous = preapply_journal.data
        expected_scope = FIXTURE_AUTHORIZATION if runtime.fixture else PRODUCTION_AUTHORIZATION
        ops.require(
            binding["authorization"] == expected_scope
            and binding["project"] == runtime.project
            and binding["operation_id"] == runtime.source.operation_id == previous["operation_id"]
            and binding["manifest_sha256"] == manifest_hash(previous["manifest"])
            and binding["runtime_identity_sha256"] == expected_runtime_identity_sha256
            and binding["receipt_sha256"] == previous["approval"]["receipt_sha256"]
            and binding["approval_run_id"] == previous["approval"]["approval_run_id"],
            "ACTIVATION_SCOPE_MISMATCH",
        )
        guarded = _BoundSource(runtime, activation_state, expected_runtime_identity_sha256)
        # This call and its old recovery handler end before the activation try.
        preflight = preapply.execute(guarded, _FreshJournal(preapply_journal, activation_state), progress=progress)
    except BaseException:
        try:
            runtime.close()
        except BaseException:
            raise ActivationFailure(_result(
                binding, activation_state, status="OPERATOR_RECOVERY_REQUIRED",
                error_code="ACTIVATION_PREAPPLY_FAILED", recovery_error_code="ACTIVATION_OBSERVER_CLOSE_FAILED",
                observer_cleanup_verified=False,
            )) from None
        raise
    phase = "passive_postflight"

    def announce(name: str) -> None:
        nonlocal phase
        phase = name
        if progress is not None:
            progress("activation_" + name)

    try:
        ops.require(preflight.get("status") == "PRE_ACTIVATION_READY"
                    and preflight.get("writer_boundary") == "PROVEN_CLOSED"
                    and guarded.plan is not None, "ACTIVATION_SCOPE_MISMATCH")
        announce("passive_postflight")
        passive = runtime.passive_postflight()
        activation_state.create(preapply_journal, guarded.plan, passive)
        announce("handoff")
        runtime.handoff_boundary(activation_state)
        for name, method in (
            ("recreate", runtime.recreate_target_stopped),
            ("reconnect", runtime.reconnect_data_services),
            ("start", runtime.start_target),
        ):
            announce(name)
            activation_state.assert_activation_permitted()
            method(activation_state)
        announce("postflight")
        ready = runtime.postflight()
        if after_active is not None:
            after_active(runtime, ready)
        announce("finalize")
        activation_state.assert_activation_permitted()
        final = runtime.finalize_configuration(activation_state)
        ops.require(type(ready) is dict and type(final) is dict, "ACTIVATION_EVIDENCE_INVALID")
        evidence = {**ready, **final}
        activation_state.mark_active(evidence)
        announce("close")
        runtime.close()
        result = _result(
            binding, activation_state, status="ACTIVE", observer_cleanup_verified=True,
            passive_postflight_sha256=hash_json(passive), active_postflight_sha256=hash_json(evidence),
            preactivation_status=preflight["status"], activation_boundary_before_writers=True,
        )
        # Validate the exact summary being returned, not an earlier observation.
        summary = result["activation_state"]
        ops.require(summary["phase"] == "ACTIVE" and summary["state_durability"] == "CONFIRMED",
                    "ACTIVATION_STATE_UNUSABLE")
        return result
    except BaseException as error:
        original = failure_code(error, phase)
        # No persistence failure, expired grant or failed observer may prevent
        # the independent physical stop/fence attempt.
        activation_state.request_containment(original if original in FAILURE_CODES else "ACTIVATION_FAILED")
        physical = None
        recovery_error = None
        try:
            physical = runtime.contain_target()
        except BaseException:
            recovery_error = "ACTIVATION_CONTAINMENT_FAILED"
        activation_state.finish_containment(physical)
        closed = False
        try:
            runtime.close()
            closed = True
        except BaseException:
            recovery_error = recovery_error or "ACTIVATION_OBSERVER_CLOSE_FAILED"
        result = _result(
            binding, activation_state, status="OPERATOR_RECOVERY_REQUIRED",
            error_code=original, recovery_error_code=recovery_error,
            controller_phase=phase,
            recreate_step=(getattr(runtime, "recreate_step", None)
                           if phase == "recreate" and type(getattr(runtime, "recreate_step", None)) is str
                           and getattr(runtime, "recreate_step", None)
                           in activation_runtime.RECREATE_STEPS else None),
            observer_cleanup_verified=closed,
            physical_containment_sha256=hash_json(physical) if physical is not None else None,
        )
        summary = result["activation_state"]
        if (closed and recovery_error is None and physical is not None
                and summary["phase"] == "CONTAINED" and summary["containment_verified"] is True
                and summary["state_durability"] == "CONFIRMED"):
            result["status"] = "CONTAINED"
        raise ActivationFailure(result) from None


def _new_operation(state: Path) -> None:
    ops.require(not any(state.glob("operation-*.json"))
                and not operation_records_exist(state), "ACTIVATION_OPERATION_EXISTS")


def _controller_checkout(expected_sha: str) -> None:
    root = Path(__file__).resolve().parents[1]
    ops.require(type(expected_sha) is str and re.fullmatch(r"[0-9a-f]{40}", expected_sha)
                and ops.command(["git", "rev-parse", "HEAD"], cwd=root) == expected_sha
                and not ops.command(["git", "status", "--porcelain"], cwd=root),
                "ACTIVATION_CONTROLLER_CHECKOUT_MISMATCH")


def _trusted_controller(evidence: dict) -> str:
    run = approval.trusted_run(evidence["workflow_run_id"], "ci.yml", "push")
    _controller_checkout(run["head_sha"])
    return run["head_sha"]


def inventory_plan(
    root: Path, backup_dir: Path, key_dir: Path, *, rehearsal_parameters: dict,
) -> dict:
    from migration_activation_runtime import production_adapter

    request, _proof, evidence = approval.load_rehearsal(**rehearsal_parameters)
    controller_sha = _trusted_controller(evidence)
    manifest = approval.build_migration_manifest(request, evidence)
    operation = secrets.token_hex(16)
    state = state_directory()
    with ops.locked_state(state):
        _new_operation(state)
        runtime = production_adapter(root, manifest, operation, state, backup_dir, key_dir)
        try:
            inventory = runtime.source.inventory()
            runtime.source.quiet()
            plan = runtime.prepare_target_plan()
        finally:
            runtime.close()
        return {
            "version": 1, "plan_type": "MIGRATION_ACTIVATION_INVENTORY", "repository": ops.REPO,
            "operation_id": operation, "expected_runtime_identity_sha256": inventory["runtime_identity_sha256"],
            "migration_manifest_sha256": manifest_hash(manifest), "target_plan_sha256": hash_json(plan),
            "source_release_sha": manifest["source_release_sha"], "source_schema_head": manifest["source_schema_head"],
            "target_release_sha": manifest["target_release_sha"], "target_schema_head": manifest["target_schema_head"],
            "controller_sha": controller_sha, "activation_policy": activation_policy(),
            "activation_rehearsal": evidence, "created_at": datetime.now(UTC).isoformat(),
            "production_mutation_performed": False, "production_approval_verified": False,
            "activation_authorized": False, "automatic_retry": False,
        }


def apply(
    root: Path, approval_run: int, artifact_id: int, backup_dir: Path, key_dir: Path, cosign: str,
) -> dict:
    from migration_activation_runtime import production_adapter

    state = state_directory()
    with ops.locked_state(state):
        _new_operation(state)
        receipt = approval.verify_approved(approval_run, artifact_id, cosign)
        approval.assert_receipt_fresh(receipt)
        review = receipt["review"]
        _trusted_controller(review["activation_rehearsal"])
        operation, manifest = review["operation_id"], review["migration_manifest"]
        receipt_hash = approval.sha256(approval.encode(receipt))
        journal = Journal(state, operation, manifest, {
            "approval_run_id": approval_run, "artifact_id": artifact_id,
            "receipt_sha256": receipt_hash, "approved_at": receipt["issued_at"],
        })
        activation = ActivationJournal(state, {
            "authorization": PRODUCTION_AUTHORIZATION, "operation_id": operation,
            "project": "iteam-ai-marketing", "manifest_sha256": manifest_hash(manifest),
            "receipt_sha256": receipt_hash, "approval_run_id": approval_run,
            "runtime_identity_sha256": review["expected_runtime_identity_sha256"],
            "target_release_sha": manifest["target_release_sha"], "target_schema_head": manifest["target_schema_head"],
            "activation_policy_sha256": hash_json(review["activation_policy"]), "expires_at": receipt["expires_at"],
        })
        runtime = production_adapter(root, manifest, operation, state, backup_dir, key_dir)
        authority = {
            "production_approval_verified": True, "production_activation_authorized": True,
            "external_side_effects_authorized": True, "openai_requests_authorized": True,
            "publication_requests_authorized": False, "metrics_requests_authorized": False,
        }
        try:
            result = execute(
                runtime, journal, activation,
                expected_runtime_identity_sha256=review["expected_runtime_identity_sha256"],
                progress=lambda phase: print(json.dumps({"status": "RUNNING", "phase": phase}), flush=True),
            )
            return {**result, **authority}
        except ActivationFailure as error:
            error.result.update(authority)
            raise
        # execute owns cleanup, including validation failures. A second close
        # must not replace its structured post-boundary recovery result.


def publish_plan(path: Path, value: dict) -> None:
    path = path.absolute()
    ops.require(not path.exists() and not path.is_symlink() and path.parent.is_dir()
                and path.parent.resolve() == path.parent, "ACTIVATION_OUTPUT_UNSAFE")
    descriptor, name = tempfile.mkstemp(prefix=".activation-plan-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ops.ReleaseError("ACTIVATION_ARGUMENTS_INVALID")


def build_parser() -> argparse.ArgumentParser:
    parser = Parser(description=__doc__, allow_abbrev=False)
    sub = parser.add_subparsers(dest="action", required=True, parser_class=Parser)
    for name in ("plan", "apply"):
        command = sub.add_parser(name, allow_abbrev=False)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--backup-dir", type=Path, required=True)
        command.add_argument("--key-dir", type=Path, required=True)
        command.add_argument("--cosign", default="cosign")
        if name == "plan":
            command.add_argument("--rehearsal-run", type=int, required=True)
            command.add_argument("--rehearsal-artifact", type=int, required=True)
            command.add_argument("--proof-sha256", required=True)
            command.add_argument("--expected-source-release", required=True)
            command.add_argument("--expected-source-schema", required=True)
            command.add_argument("--target-build-run", type=int, required=True)
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--approval-run", type=int, required=True)
            command.add_argument("--approval-artifact", type=int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        os.umask(0o077)
        if args.action == "plan":
            ops.require(not args.output.exists() and not args.output.is_symlink(), "ACTIVATION_OUTPUT_UNSAFE")
            parameters = {
                "rehearsal_run_id": args.rehearsal_run, "rehearsal_artifact_id": args.rehearsal_artifact,
                "proof_sha256": args.proof_sha256, "expected_source_release": args.expected_source_release,
                "expected_source_schema": args.expected_source_schema,
                "target_build_run_id": args.target_build_run, "cosign": args.cosign,
            }
            result = inventory_plan(args.root, args.backup_dir, args.key_dir, rehearsal_parameters=parameters)
            publish_plan(args.output, result)
        else:
            result = apply(args.root, args.approval_run, args.approval_artifact, args.backup_dir, args.key_dir, args.cosign)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ActivationFailure, preapply.ApplyFailure) as error:
        print(json.dumps(error.result, sort_keys=True))
        return 1
    except (Exception, KeyboardInterrupt) as error:
        code = str(error) if isinstance(error, ops.ReleaseError) and str(error) in ERROR_CODES | approval.ERROR_CODES else "ACTIVATION_FAILED"
        print(json.dumps({"status": "FAIL", "error_code": code, "automatic_retry": False,
                          "resume_authorized": False, "production_approval_verified": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
