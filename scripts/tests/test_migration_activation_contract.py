"""Reject incomplete, cross-bound and contradictory signed activation evidence."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import migration_activation_contract as contract
import release_ops as ops
from test_migration_rehearsal_proof import proof as migration_proof
from test_migration_rehearsal_proof import request as migration_request

STARTED = "2026-11-22T10:00:00Z"
FINISHED = "2026-11-22T11:00:00Z"
CONTROLLER_SHA = "a" * 40
CONTROLLER_TREE = "b" * 40


def hashed(character: str) -> str:
    return "sha256:" + character * 64


def request() -> dict:
    return {
        "version": 1, "request_type": contract.REQUEST_TYPE, "repository": ops.REPO,
        "migration_request": migration_request(), "activation_policy": contract.activation_policy(),
    }


def scenario(*, failure: bool) -> dict:
    value = {
        "status": "PASS", "operation_id": ("3" if failure else "2") * 32,
        "project": "iteam-activation-drill-" + ("3" if failure else "2") * 10,
        "pre_activation_status": "PRE_ACTIVATION_READY", "pre_activation_writer_boundary": "PROVEN_CLOSED",
        "pre_activation_business_preserved": True,
        "database_identity_sha256": hashed("3" if failure else "2"), "target_plan_sha256": hashed("4"),
        "activation_phase": "CONTAINED" if failure else "ACTIVE", "activation_boundary_recorded": True,
        "state_durability": "CONFIRMED", "admission_protocol": 1, "target_services_verified": 9,
        "cold_recreation_verified": True, "source_runtime_restarted": False, "automatic_retry": False,
        "post_activation_downgrade_attempts": 0, "post_activation_restore_attempts": 0, "cleanup_verified": True,
    }
    if failure:
        value.update({
            "injected_failure": "INJECTED_POST_ACTIVATION_FAILURE", "http_write_committed": True,
            "real_worker_claim_committed": True, "committed_canary_count": 1,
            "committed_all_tables_sha256": hashed("5"), "contained_all_tables_sha256": hashed("5"),
            "target_schema_preserved": True, "runtime_stopped": True, "new_claims_blocked": True,
            "database_fenced": True, "postgres_host_port_blocked": True,
        })
    return value


def proof(value: dict | None = None) -> dict:
    value = request() if value is None else value
    old = value["migration_request"]
    nested = migration_proof(old)
    counts = copy.deepcopy(nested["source_table_counts"])
    counts.update(alembic_version=1, workspaces=1, workspace_memberships=1)
    counts.update(users=1, agent_runs=3, campaigns=1, publications=2)
    passive = {
        "status": "PASS", "project": "iteam-admission-drill-" + "1" * 10, "operation_id": "1" * 32,
        "matrix": dict.fromkeys((
            "authenticated_api_write", "stale_chat_get", "direct_provider_producer_session",
            "worker_ai", "worker_ai_control", "worker_publication", "worker_publication_control",
            "worker_metrics", "worker_unset", "scheduler_ai", "scheduler_publication",
        ), "BLOCKED_RUNTIME_PASSIVE"),
        "business_writes_blocked": True, "claims_blocked": True, "provider_connections": 0,
        "provider_witness_selftest_connections": 4, "all_tables_sha256": hashed("6"), "table_counts": counts,
        "database_identity_sha256": hashed("1"), "schema": old["target_schema_head"],
        "broker_snapshot_sha256": hashed("7"), "broker_keys": 6,
        "redis_witness": {
            "continuous_monitor_verified": True, "allowed_readiness_commands": 6,
            "forbidden_commands": 0, "queue_activity_observed": False,
        },
        "real_broker_queued_canaries": 7, "fixture_kind": "ADVERSARIAL_TARGET_SCHEMA", "business_ready": False,
        "runtime_stopped_before_observation_closed": True,
    }
    return {
        "proof_contract_version": 1, "proof_type": contract.PROOF_TYPE, "repository": ops.REPO,
        "authorization": contract.FIXTURE_AUTHORITY, "status": "PASS", **contract.activation_scope(value),
        "controller_sha": CONTROLLER_SHA, "controller_tree": CONTROLLER_TREE,
        "migration_rehearsal": nested,
        "migration_target_passive": {
            "admission_protocol": 1, "admission_mode": "passive", "business_ready": False,
            "database_read_only": True, "build_sha": old["target_release_sha"], "before_downgrade": True,
        },
        "adversarial_passive": {
            "target_image_id": hashed("8"), "candidate_sha": old["target_release_sha"],
            "passive": passive, "cleanup_verified": True,
        },
        "success": scenario(failure=False), "failure": scenario(failure=True),
        "created_at": "2026-11-22T10:55:00Z", "production_approval_verified": False,
        "production_credentials": False, "application_external_network": False,
        "postgres_has_host_publish_network": True, "publishing_enabled": False,
        "disposable_projects_removed": True, "temporary_backups_and_keys_removed": True, "observers_closed": True,
    }


class ActivationPolicyTests(unittest.TestCase):
    def test_policy_is_explicit_and_returns_an_independent_value(self):
        value = contract.activation_policy()
        self.assertTrue(value["openai_requests_authorized"])
        self.assertTrue(value["claims_authorized"])
        self.assertFalse(value["publication_requests_authorized"])
        self.assertFalse(value["metrics_requests_authorized"])
        self.assertFalse(value["automatic_retry"])
        self.assertEqual(value["successful_restart_policy"], "RESTORE_BOUND_POLICIES_BEFORE_SUCCESS")
        value["services"].clear()
        self.assertEqual(contract.activation_policy()["services"], sorted(ops.APP))

    def test_policy_cannot_broaden_capabilities_or_enable_retry_or_downgrade(self):
        value = contract.activation_policy()
        for key, changed in (
            ("publication_requests_authorized", True), ("metrics_requests_authorized", True),
            ("openai_requests_authorized", False), ("automatic_retry", True),
            ("post_activation_schema_downgrade", True), ("post_activation_backup_restore", True),
            ("admission_protocol", True), ("services", list(ops.APP)),
            ("successful_restart_policy", "RETRY_OPERATION"),
        ):
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                contract.validate_activation_policy(dict(value, **{key: changed}))
        with self.assertRaises(ops.ReleaseError):
            contract.validate_activation_policy(dict(value, future_permission=True))

    def test_old_migration_request_has_no_activation_authority(self):
        with self.assertRaises(ops.ReleaseError):
            contract.validate_activation_request(migration_request())


class ActivationProofTests(unittest.TestCase):
    def validate(self, result: dict, value: dict | None = None, **kwargs) -> None:
        contract.validate_activation_rehearsal_proof(
            result, request() if value is None else value, run_started_at=STARTED, run_updated_at=FINISHED,
            expected_controller_sha=CONTROLLER_SHA, expected_controller_tree=CONTROLLER_TREE, **kwargs,
        )

    def test_complete_signed_disposable_evidence_is_accepted_without_mutation(self):
        value, result = request(), proof()
        before = copy.deepcopy((value, result))
        self.validate(result, value)
        self.assertEqual((value, result), before)
        self.assertEqual(len(result["adversarial_passive"]["passive"]["table_counts"]), 37)

    def test_v1_and_candidate_only_proofs_never_satisfy_activation(self):
        for other in (migration_proof(), {"proof_kind": "RUNTIME_ADMISSION_CANDIDATE", "status": "PASS"}):
            with self.subTest(other=other.get("proof_kind")), self.assertRaises(ops.ReleaseError):
                self.validate(other)
        changed = proof()
        changed["authorization"] = "PRODUCTION_MIGRATION_ACTIVATION"
        with self.assertRaises(ops.ReleaseError):
            self.validate(changed)

    def test_exact_scope_includes_build_images_plan_request_and_controller(self):
        for key, changed in (
            ("target_release_sha", "f" * 40), ("source_schema_head", "wrong"),
            ("target_build_attempt", True), ("migration_plan_hash", hashed("f")),
            ("target_images", {"backend": "unbound", "frontend": "unbound"}),
            ("request_sha256", hashed("e")), ("controller_sha", "c" * 40), ("controller_tree", "c" * 40),
        ):
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                self.validate(dict(proof(), **{key: changed}))

    def test_nested_migration_rehearsal_and_passive_before_downgrade_are_required(self):
        for section, key, changed in (
            ("migration_rehearsal", "migration_specific_invariants", False),
            ("migration_rehearsal", "writers_activated", True),
            ("migration_rehearsal", "pre_activation_downgrade_tested", False),
            ("migration_target_passive", "admission_mode", "active"),
            ("migration_target_passive", "business_ready", True),
            ("migration_target_passive", "database_read_only", False),
            ("migration_target_passive", "before_downgrade", False),
        ):
            value = proof()
            value[section][key] = changed
            with self.subTest(section=section, key=key), self.assertRaises(ops.ReleaseError):
                self.validate(value)

    def test_empty_or_consumed_queue_and_missing_business_tables_are_not_passive_proof(self):
        for key, changed in (
            ("real_broker_queued_canaries", 0), ("real_broker_queued_canaries", True),
            ("broker_keys", 0), ("claims_blocked", False), ("business_writes_blocked", False),
            ("provider_connections", 1), ("provider_witness_selftest_connections", 0),
            ("runtime_stopped_before_observation_closed", False), ("business_ready", True),
            ("table_counts", {"campaigns": 1}), ("matrix", {}),
        ):
            value = proof()
            value["adversarial_passive"]["passive"][key] = changed
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                self.validate(value)

    def test_redis_loss_queue_activity_and_unverified_cleanup_cannot_pass(self):
        for key, changed in (
            ("continuous_monitor_verified", False), ("queue_activity_observed", True),
            ("forbidden_commands", 1), ("allowed_readiness_commands", 0), ("forbidden_commands", False),
        ):
            value = proof()
            value["adversarial_passive"]["passive"]["redis_witness"][key] = changed
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                self.validate(value)
        for key in ("disposable_projects_removed", "temporary_backups_and_keys_removed", "observers_closed"):
            value = proof()
            value[key] = False
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                self.validate(value)

    def test_start_without_boundary_or_unverified_target_services_is_rejected(self):
        for key, changed in (
            ("pre_activation_writer_boundary", "UNKNOWN"), ("pre_activation_business_preserved", False),
            ("activation_boundary_recorded", False), ("state_durability", "UNKNOWN"),
            ("target_services_verified", 8), ("cold_recreation_verified", False),
            ("source_runtime_restarted", True), ("automatic_retry", True),
            ("post_activation_downgrade_attempts", 1), ("post_activation_restore_attempts", 1),
        ):
            for section in ("success", "failure"):
                value = proof()
                value[section][key] = changed
                with self.subTest(section=section, key=key), self.assertRaises(ops.ReleaseError):
                    self.validate(value)

    def test_postwrite_failure_requires_real_commit_claim_and_physical_preservation(self):
        for key, changed in (
            ("http_write_committed", False), ("real_worker_claim_committed", False),
            ("committed_canary_count", 0), ("contained_all_tables_sha256", hashed("a")),
            ("runtime_stopped", False), ("new_claims_blocked", False), ("database_fenced", False),
            ("postgres_host_port_blocked", False), ("target_schema_preserved", False),
            ("activation_phase", "OPERATOR_RECOVERY_REQUIRED"),
        ):
            value = proof()
            value["failure"][key] = changed
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                self.validate(value)

    def test_success_and_failure_must_use_independent_operations_and_databases(self):
        for key in ("operation_id", "project", "database_identity_sha256"):
            value = proof()
            value["failure"][key] = value["success"][key]
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                self.validate(value)

    def test_times_and_exact_json_types_cannot_silently_broaden_contract(self):
        for key, changed in (
            ("created_at", "2026-11-22T09:55:00Z"), ("created_at", "2026-11-22T11:01:00Z"),
            ("created_at", "2026-11-22T10:55:00"), ("proof_contract_version", True),
            ("production_approval_verified", 0), ("application_external_network", True),
        ):
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                self.validate(dict(proof(), **{key: changed}))
        value = proof()
        value["success"]["unexpected"] = True
        with self.assertRaises(ops.ReleaseError):
            self.validate(value)


if __name__ == "__main__":
    unittest.main()
