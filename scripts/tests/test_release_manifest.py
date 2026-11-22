"""Pure contract tests: no Docker, GitHub, provider, or production access."""

from __future__ import annotations

import copy
import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
contracts = importlib.import_module("release_manifest")

SOURCE_RELEASE = "1" * 40
TARGET_RELEASE = "2" * 40
SOURCE_SCHEMA = "20261009_0028"
TARGET_SCHEMA = "20261121_0030"


def image(component: str, marker: str) -> str:
    return (
        f"ghcr.io/{contracts.REPOSITORY}-{component}@sha256:"
        + marker * 64
    )


def path() -> list[dict[str, str]]:
    return [
        {
            "revision": "20261121_0029",
            "down_revision": SOURCE_SCHEMA,
            "script_sha256": "sha256:" + "a" * 64,
        },
        {
            "revision": TARGET_SCHEMA,
            "down_revision": "20261121_0029",
            "script_sha256": "sha256:" + "b" * 64,
        },
    ]


def manifest() -> dict:
    migration_path = path()
    plan_hash = contracts.migration_plan_hash(
        source_schema_head=SOURCE_SCHEMA,
        target_schema_head=TARGET_SCHEMA,
        migration_path=migration_path,
        compatibility_class="EXPAND_ONLY",
        pre_activation_rollback_policy="PRE_ACTIVATION_DOWNGRADE",
    )
    source_images = {
        "backend": image("backend", "c"),
        "frontend": image("frontend", "d"),
    }
    target_images = {
        "backend": image("backend", "e"),
        "frontend": image("frontend", "f"),
    }
    return {
        "version": contracts.MIGRATION_MANIFEST_VERSION,
        "release_type": contracts.MIGRATION_RELEASE_TYPE,
        "repository": contracts.REPOSITORY,
        "source_release_sha": SOURCE_RELEASE,
        "target_release_sha": TARGET_RELEASE,
        "target_build_run_id": 101,
        "target_build_attempt": 1,
        "source_schema_head": SOURCE_SCHEMA,
        "target_schema_head": TARGET_SCHEMA,
        "migration_path": migration_path,
        "migration_plan_hash": plan_hash,
        "compatibility_class": "EXPAND_ONLY",
        "pre_activation_rollback_policy": "PRE_ACTIVATION_DOWNGRADE",
        "frontend_api_url": contracts.FRONTEND_API_URL,
        "source_images": source_images,
        "target_images": target_images,
        "rehearsal_proof": {
            "workflow_run_id": 202,
            "artifact_id": 303,
            "proof_sha256": "sha256:" + "9" * 64,
            "source_release_sha": SOURCE_RELEASE,
            "target_release_sha": TARGET_RELEASE,
            "target_build_run_id": 101,
            "target_build_attempt": 1,
            "source_schema_head": SOURCE_SCHEMA,
            "target_schema_head": TARGET_SCHEMA,
            "migration_plan_hash": plan_hash,
            "source_images": copy.deepcopy(source_images),
            "target_images": copy.deepcopy(target_images),
        },
    }


class MigrationManifestContractTests(unittest.TestCase):
    def test_valid_manifest_and_deterministic_plan_hash(self):
        value = manifest()
        contracts.validate_migration_manifest(value)
        self.assertEqual(
            value["migration_plan_hash"],
            contracts.migration_plan_hash(
                source_schema_head=SOURCE_SCHEMA,
                target_schema_head=TARGET_SCHEMA,
                migration_path=copy.deepcopy(value["migration_path"]),
                compatibility_class="EXPAND_ONLY",
                pre_activation_rollback_policy="PRE_ACTIVATION_DOWNGRADE",
            ),
        )

    def test_plan_hash_binds_script_content_and_ordered_chain(self):
        value = manifest()
        changed = copy.deepcopy(value["migration_path"])
        changed[1]["script_sha256"] = "sha256:" + "8" * 64
        changed_hash = contracts.migration_plan_hash(
            source_schema_head=SOURCE_SCHEMA,
            target_schema_head=TARGET_SCHEMA,
            migration_path=changed,
            compatibility_class="EXPAND_ONLY",
            pre_activation_rollback_policy="PRE_ACTIVATION_DOWNGRADE",
        )
        self.assertNotEqual(value["migration_plan_hash"], changed_hash)

        reordered = list(reversed(copy.deepcopy(value["migration_path"])))
        with self.assertRaisesRegex(
            contracts.ManifestContractError, "MIGRATION_PATH_NOT_CONTIGUOUS"
        ):
            contracts.migration_plan_hash(
                source_schema_head=SOURCE_SCHEMA,
                target_schema_head=TARGET_SCHEMA,
                migration_path=reordered,
                compatibility_class="EXPAND_ONLY",
                pre_activation_rollback_policy="PRE_ACTIVATION_DOWNGRADE",
            )

    def test_manifest_rejects_unknown_fields_and_identity_changes(self):
        for key, replacement in [
            ("version", 1),
            ("release_type", "STATIC"),
            ("repository", "other/repository"),
            ("frontend_api_url", "https://other.example/api"),
        ]:
            with self.subTest(key=key):
                value = manifest()
                value[key] = replacement
                with self.assertRaises(contracts.ManifestContractError):
                    contracts.validate_migration_manifest(value)

        value = manifest()
        value["unexpected"] = True
        with self.assertRaisesRegex(
            contracts.ManifestContractError,
            "UNEXPECTED_MIGRATION_MANIFEST_FIELDS",
        ):
            contracts.validate_migration_manifest(value)

    def test_manifest_rejects_invalid_release_and_build_identity(self):
        for key, replacement, code in [
            ("source_release_sha", "not-a-sha", "INVALID_SOURCE_RELEASE_SHA"),
            ("target_release_sha", SOURCE_RELEASE, "SOURCE_AND_TARGET_RELEASE_MATCH"),
            ("target_build_run_id", True, "INVALID_TARGET_BUILD_RUN"),
            ("target_build_attempt", 0, "INVALID_TARGET_BUILD_ATTEMPT"),
        ]:
            with self.subTest(key=key):
                value = manifest()
                value[key] = replacement
                if key in {"source_release_sha", "target_release_sha"}:
                    value["rehearsal_proof"][key] = replacement
                with self.assertRaisesRegex(contracts.ManifestContractError, code):
                    contracts.validate_migration_manifest(value)

    def test_manifest_rejects_broken_or_ambiguous_migration_path(self):
        cases: list[tuple[str, object, str]] = [
            (
                "non_contiguous",
                [
                    {
                        "revision": "20261121_0029",
                        "down_revision": "other",
                        "script_sha256": "sha256:" + "a" * 64,
                    },
                    path()[1],
                ],
                "MIGRATION_PATH_NOT_CONTIGUOUS",
            ),
            ("empty", [], "INVALID_MIGRATION_PATH"),
            (
                "duplicate",
                [path()[0], {**path()[1], "revision": "20261121_0029"}],
                "DUPLICATE_MIGRATION_REVISION",
            ),
            (
                "bad_hash",
                [{**path()[0], "script_sha256": "a" * 64}, path()[1]],
                "INVALID_MIGRATION_SCRIPT_HASH",
            ),
        ]
        for name, migration_path, code in cases:
            with self.subTest(name=name):
                value = manifest()
                value["migration_path"] = migration_path
                with self.assertRaisesRegex(contracts.ManifestContractError, code):
                    contracts.validate_migration_manifest(value)

    def test_manifest_rejects_tampered_plan_hash_and_unapproved_policy(self):
        value = manifest()
        value["migration_plan_hash"] = "sha256:" + "0" * 64
        value["rehearsal_proof"]["migration_plan_hash"] = value[
            "migration_plan_hash"
        ]
        with self.assertRaisesRegex(
            contracts.ManifestContractError, "MIGRATION_PLAN_HASH_MISMATCH"
        ):
            contracts.validate_migration_manifest(value)

        for key, replacement, code in [
            (
                "compatibility_class",
                "DESTRUCTIVE",
                "UNSUPPORTED_MIGRATION_COMPATIBILITY_CLASS",
            ),
            (
                "pre_activation_rollback_policy",
                "AUTOMATIC_AFTER_WRITES",
                "UNSUPPORTED_PRE_ACTIVATION_ROLLBACK_POLICY",
            ),
        ]:
            with self.subTest(key=key):
                value = manifest()
                value[key] = replacement
                with self.assertRaisesRegex(contracts.ManifestContractError, code):
                    contracts.validate_migration_manifest(value)

    def test_manifest_rejects_untrusted_images(self):
        for section, component, replacement in [
            ("source_images", "backend", "backend:latest"),
            (
                "target_images",
                "frontend",
                "ghcr.io/other/repo-frontend@sha256:" + "f" * 64,
            ),
        ]:
            with self.subTest(section=section, component=component):
                value = manifest()
                value[section][component] = replacement
                with self.assertRaisesRegex(
                    contracts.ManifestContractError, "INVALID_MIGRATION_IMAGE"
                ):
                    contracts.validate_migration_manifest(value)

    def test_rehearsal_proof_is_bound_to_exact_release_schema_plan_and_build(self):
        for key, replacement in [
            ("source_release_sha", "3" * 40),
            ("target_release_sha", "4" * 40),
            ("target_build_run_id", 999),
            ("target_build_attempt", 2),
            ("source_schema_head", "20261009_0027"),
            ("target_schema_head", "20261121_0031"),
            ("migration_plan_hash", "sha256:" + "7" * 64),
        ]:
            with self.subTest(key=key):
                value = manifest()
                value["rehearsal_proof"][key] = replacement
                with self.assertRaisesRegex(
                    contracts.ManifestContractError,
                    "REHEARSAL_PROOF_SCOPE_MISMATCH",
                ):
                    contracts.validate_migration_manifest(value)

    def test_rehearsal_proof_cannot_be_reused_for_other_image_digests(self):
        for section, component in [
            ("source_images", "backend"),
            ("target_images", "frontend"),
        ]:
            with self.subTest(section=section, component=component):
                value = manifest()
                value["rehearsal_proof"][section][component] = image(component, "8")
                with self.assertRaisesRegex(
                    contracts.ManifestContractError,
                    "REHEARSAL_PROOF_SCOPE_MISMATCH",
                ):
                    contracts.validate_migration_manifest(value)

        value = manifest()
        value["rehearsal_proof"]["extra"] = "not-allowed"
        with self.assertRaisesRegex(
            contracts.ManifestContractError, "INVALID_REHEARSAL_PROOF_FIELDS"
        ):
            contracts.validate_migration_manifest(value)


if __name__ == "__main__":
    unittest.main()
