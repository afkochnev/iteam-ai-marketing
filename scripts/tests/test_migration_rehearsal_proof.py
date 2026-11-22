"""Adversarial checks for the proof consumed by migration approval."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migration_rehearsal_proof as contract
from migration_release_drill import rehearsal_scope
from release_manifest import migration_plan_hash
from release_ops import ReleaseError

STARTED = "2026-11-22T10:08:51Z"
FINISHED = "2026-11-22T10:16:55Z"
DATA_HASH = "sha256:" + "b" * 64
_DEFAULT_REQUEST = object()


def request() -> dict:
    path = Path(__file__).resolve().parents[1] / "fixtures/migration-0028-to-0030.json"
    return json.loads(path.read_text())


def proof(value: dict | None = None) -> dict:
    value = request() if value is None else value
    counts = dict.fromkeys(
        "activity_logs agent_runs agent_tools agents approvals campaign_kpis "
        "campaign_optimization_actions campaign_optimization_proposals campaigns "
        "content_derivations content_items content_version_sources content_versions "
        "knowledge_items knowledge_pack_items knowledge_packs knowledge_sources "
        "knowledge_stores marketing_context_snapshots marketing_conversations "
        "marketing_experiment_publications marketing_experiments marketing_feedback "
        "marketing_feedback_analyses marketing_messages publication_metrics_snapshots "
        "publication_plan_items publication_plans publication_reconciliations "
        "publications task_dependencies tasks tool_calls users".split(),
        0,
    )
    counts.update({table: 1 for table in contract.REQUIRED_BUSINESS_TABLES})
    counts.update(users=3, agents=5, agent_tools=15, knowledge_sources=1)
    downgrade = value["pre_activation_rollback_policy"] == "PRE_ACTIVATION_DOWNGRADE"
    return {
        "status": "PASS",
        "proof_contract_version": 1,
        "repository": value["repository"],
        **rehearsal_scope(value),
        "compatibility_class": value["compatibility_class"],
        "pre_activation_rollback_policy": value["pre_activation_rollback_policy"],
        "migration_revisions": [step["revision"] for step in value["migration_path"]],
        "encrypted_backup_isolated_restore": True,
        "migration_executed_on_restored_database": True,
        "encrypted_backup_sha256": "sha256:" + "a" * 64,
        "source_schema_verified": True,
        "target_schema_verified": True,
        "migration_specific_invariants": True,
        "business_data_hash_unchanged": True,
        "source_table_counts": counts,
        "business_data_sha256": {
            "source": DATA_HASH,
            "restored": DATA_HASH,
            "upgraded": DATA_HASH,
            "downgraded": DATA_HASH if downgrade else None,
        },
        "target_runtime_readiness": True,
        "pre_activation_downgrade_tested": downgrade,
        "writers_activated": False,
        "external_network": False,
        "production_credentials": False,
        "publishing_enabled": False,
        "disposable_services": 4,
        "created_at": "2026-11-22T10:10:51.946220+00:00",
        "disposable_project_removed": True,
        "temporary_backup_and_key_removed": True,
    }


class RehearsalProofTests(unittest.TestCase):
    def validate(self, result, value=_DEFAULT_REQUEST, started=STARTED, finished=FINISHED):
        contract.validate_rehearsal_proof(
            result,
            request() if value is _DEFAULT_REQUEST else value,
            run_started_at=started,
            run_updated_at=finished,
        )

    def test_complete_proof_is_accepted_without_mutation(self):
        value, result = request(), proof()
        before = copy.deepcopy((value, result))
        self.validate(result, value)
        self.assertEqual((value, result), before)
        self.validate(result, started="2026-11-22T11:08:51+01:00")

    def test_unknown_missing_and_nonobject_payloads_fail_closed(self):
        values = [None, [], "PASS", {**proof(), "credentials": "do-not-display"}]
        for field in proof():
            changed = proof()
            del changed[field]
            values.append(changed)
        for value in values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ReleaseError, "UNEXPECTED_REHEARSAL_PROOF_FIELDS"):
                    self.validate(value)

    def test_all_scope_fields_bind_the_exact_request_with_strict_types(self):
        for field, value in rehearsal_scope(request()).items():
            if type(value) is dict:
                replacements = [{**value, "extra": "secret"}]
                replacements += [
                    {**value, component: image.replace("sha256:", "sha256:0", 1)}
                    for component, image in value.items()
                ]
            elif type(value) is int:
                replacements = [value + 1, str(value), float(value), True]
            else:
                replacements = [value + "different", None]
            for replacement in replacements:
                with self.subTest(field=field, replacement_type=type(replacement).__name__):
                    changed = proof()
                    changed[field] = replacement
                    with self.assertRaisesRegex(ReleaseError, "REHEARSAL_PROOF_SCOPE_MISMATCH"):
                        self.validate(changed)

    def test_success_booleans_are_not_truthy_values(self):
        for field in contract._SUCCESS_FLAGS:
            for replacement in (False, 1, "true", None):
                with self.subTest(field=field, value=replacement):
                    changed = proof()
                    changed[field] = replacement
                    with self.assertRaisesRegex(ReleaseError, "REHEARSAL_PROOF_CHECKS_FAILED"):
                        self.validate(changed)

    def test_unsafe_flags_and_topology_cannot_be_coerced(self):
        mutations = [
            (field, item)
            for field in contract._UNSAFE_FLAGS
            for item in (True, 0, "false", None)
        ]
        mutations += [("disposable_services", item) for item in (3, 5, 4.0, "4", True)]
        for field, replacement in mutations:
            with self.subTest(field=field, value=replacement):
                changed = proof()
                changed[field] = replacement
                with self.assertRaisesRegex(ReleaseError, "REHEARSAL_PROOF_UNSAFE_RUNTIME"):
                    self.validate(changed)

    def test_identity_revision_order_and_policy_are_mandatory(self):
        for field, replacement, error in (
            ("status", "FAIL", "IDENTITY"),
            ("proof_contract_version", True, "IDENTITY"),
            ("proof_contract_version", 1.0, "IDENTITY"),
            ("repository", "other/repo", "IDENTITY"),
            ("compatibility_class", "DESTRUCTIVE", "POLICY"),
            ("pre_activation_rollback_policy", "FAIL_FORWARD_ONLY", "POLICY"),
            ("migration_revisions", list(reversed(proof()["migration_revisions"])), "REVISIONS"),
            ("migration_revisions", proof()["migration_revisions"][:1], "REVISIONS"),
        ):
            with self.subTest(field=field):
                changed = proof()
                changed[field] = replacement
                with self.assertRaisesRegex(ReleaseError, error):
                    self.validate(changed)

    def test_counts_preserve_empty_tables_and_require_nonempty_business_fixture(self):
        for table, count in (
            ("publications", 0), ("users", 2), ("tool_calls", -1),
            ("tool_calls", True), ("tool_calls", 0.0), ("tool_calls", "0"),
            ("secret payload", 1), ("workspaces", 1),
            ("workspace_memberships", 0), ("alembic_version", 1),
        ):
            with self.subTest(table=table, count=count):
                changed = proof()
                changed["source_table_counts"][table] = count
                with self.assertRaisesRegex(ReleaseError, "COUNTS_INVALID"):
                    self.validate(changed)
        for counts in ({}, [], {"campaigns": 1}, None):
            changed = proof()
            changed["source_table_counts"] = counts
            with self.assertRaisesRegex(ReleaseError, "COUNTS_INVALID"):
                self.validate(changed)

    def test_restore_upgrade_and_downgrade_hashes_must_preserve_source_data(self):
        for key in ("source", "restored", "upgraded", "downgraded"):
            changed = proof()
            changed["business_data_sha256"][key] = "sha256:" + "c" * 64
            with self.subTest(key=key), self.assertRaises(ReleaseError):
                self.validate(changed)
        for value in ({}, [], None, {**proof()["business_data_sha256"], "payload": "secret"}):
            changed = proof()
            changed["business_data_sha256"] = value
            with self.assertRaisesRegex(ReleaseError, "DATA_HASH_MISMATCH"):
                self.validate(changed)
        for value in (None, "sha256:bad", "SHA256:" + "a" * 64, "a" * 64):
            changed = proof()
            changed["encrypted_backup_sha256"] = value
            with self.assertRaisesRegex(ReleaseError, "DATA_HASH_MISMATCH"):
                self.validate(changed)

    def test_downgrade_policy_controls_both_flag_and_hash(self):
        for value in (False, 1, "true", None):
            changed = proof()
            changed["pre_activation_downgrade_tested"] = value
            with self.assertRaisesRegex(ReleaseError, "DOWNGRADE_MISMATCH"):
                self.validate(changed)
        forward = request()
        forward["pre_activation_rollback_policy"] = "FAIL_FORWARD_ONLY"
        forward["migration_plan_hash"] = migration_plan_hash(
            **{key: forward[key] for key in (
                "source_schema_head", "target_schema_head", "migration_path",
                "compatibility_class", "pre_activation_rollback_policy",
            )}
        )
        self.validate(proof(forward), forward)
        for field, value in (
            ("pre_activation_downgrade_tested", True),
            ("pre_activation_downgrade_tested", 0),
        ):
            changed = proof(forward)
            changed[field] = value
            with self.assertRaisesRegex(ReleaseError, "DOWNGRADE_MISMATCH"):
                self.validate(changed, forward)
        changed = proof(forward)
        changed["business_data_sha256"]["downgraded"] = DATA_HASH
        with self.assertRaisesRegex(ReleaseError, "DOWNGRADE_MISMATCH"):
            self.validate(changed, forward)

    def test_timestamp_must_be_aware_and_inside_authenticated_run_window(self):
        for value in (
            None, 1, "", "not-a-time", "2026-11-22", "2026-11-22T10:10:51",
            "2026-11-22T10:08:50Z", "2026-11-22T10:16:56Z",
            "2026-11-22T10:10:51+25:00",
        ):
            with self.subTest(timestamp=value):
                changed = proof()
                changed["created_at"] = value
                with self.assertRaisesRegex(ReleaseError, "TIME_INVALID"):
                    self.validate(changed)
        for started, finished in (
            (FINISHED, STARTED),
            ("bad", FINISHED),
            (STARTED, "2026-11-22T11:00:00"),
        ):
            with self.assertRaisesRegex(ReleaseError, "TIME_INVALID"):
                self.validate(proof(), started=started, finished=finished)

    def test_malformed_request_errors_are_fixed_and_do_not_include_payload(self):
        for value in (
            None, [], {"secret": "never-log-me"},
            {**request(), "compatibility_class": []},
        ):
            with self.assertRaises(ReleaseError) as raised:
                self.validate(proof(), value=value)
            self.assertEqual(str(raised.exception), "INVALID_REHEARSAL_PROOF_REQUEST")


if __name__ == "__main__":
    unittest.main()
