"""Hardening tests for the public migration rehearsal entrypoint."""

from __future__ import annotations

import importlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
drill = importlib.import_module("migration_release_drill")

SOURCE_RELEASE = "1" * 40
TARGET_RELEASE = "2" * 40
SOURCE_SCHEMA = "20261009_0028"
TARGET_SCHEMA = "20261121_0030"


def image(component: str, marker: str) -> str:
    return f"ghcr.io/{drill.REPOSITORY}-{component}@sha256:" + marker * 64


def request() -> dict:
    path = [
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
    plan_hash = drill.migration_plan_hash(
        source_schema_head=SOURCE_SCHEMA,
        target_schema_head=TARGET_SCHEMA,
        migration_path=path,
        compatibility_class="EXPAND_ONLY",
        pre_activation_rollback_policy="PRE_ACTIVATION_DOWNGRADE",
    )
    return {
        "version": drill.REQUEST_VERSION,
        "request_type": drill.REQUEST_TYPE,
        "repository": drill.REPOSITORY,
        "source_release_sha": SOURCE_RELEASE,
        "target_release_sha": TARGET_RELEASE,
        "target_build_run_id": 101,
        "target_build_attempt": 1,
        "source_schema_head": SOURCE_SCHEMA,
        "target_schema_head": TARGET_SCHEMA,
        "migration_path": path,
        "migration_plan_hash": plan_hash,
        "compatibility_class": "EXPAND_ONLY",
        "pre_activation_rollback_policy": "PRE_ACTIVATION_DOWNGRADE",
        "frontend_api_url": drill.FRONTEND_API_URL,
        "source_images": {
            "backend": image("backend", "c"),
            "frontend": image("frontend", "d"),
        },
        "target_images": {
            "backend": image("backend", "e"),
            "frontend": image("frontend", "f"),
        },
    }


def build_records(value: dict) -> dict:
    return {
        component: {
            "release_sha": TARGET_RELEASE,
            "image": value["target_images"][component],
            "platforms": ["linux/amd64", "linux/arm64"],
            "frontend_api_url": drill.FRONTEND_API_URL,
        }
        for component in ("backend", "frontend")
    }


class MigrationReleaseDrillHardeningTests(unittest.TestCase):
    def test_business_hash_ignores_row_order_but_preserves_all_values(self):
        first = {"users": ['{"id":1}', '{"id":2}'], "empty": []}
        reordered = {"empty": [], "users": ['{"id":2}', '{"id":1}']}
        self.assertEqual(drill.canonical_business_state(first), drill.canonical_business_state(reordered))
        for changed in (
            {"users": ['{"id":1}', '{"id":3}'], "empty": []},
            {"users": ['{"id":1}', '{"id":2}', '{"id":2}'], "empty": []},
            {"users": ['{"id":1}', '{"id":2}']},
            {"renamed": ['{"id":1}', '{"id":2}'], "empty": []},
        ):
            self.assertNotEqual(drill.canonical_business_state(first), drill.canonical_business_state(changed))
        self.assertNotEqual(
            drill.canonical_business_state({"values": ['{"value":0.123456789012345678901}']}),
            drill.canonical_business_state({"values": ['{"value":0.123456789012345678902}']}),
        )

    def test_proof_scope_matches_manifest_and_cannot_change_with_request(self):
        import release_manifest as contracts
        value = request()
        proof = drill.rehearsal_scope(value)
        manifest = dict(value)
        manifest.pop("request_type")
        manifest.update(version=contracts.MIGRATION_MANIFEST_VERSION, release_type=contracts.MIGRATION_RELEASE_TYPE)
        manifest["rehearsal_proof"] = {
            "workflow_run_id": 501, "artifact_id": 502,
            "proof_sha256": "sha256:" + "6" * 64, **proof,
        }
        contracts.validate_migration_manifest(manifest)
        value["target_images"]["backend"] = image("backend", "7")
        self.assertNotEqual(value["target_images"], proof["target_images"])
        with self.assertRaisesRegex(contracts.ManifestContractError, "REHEARSAL_PROOF_SCOPE_MISMATCH"):
            contracts.validate_migration_manifest(manifest)

    def test_target_build_records_bind_exact_artifact_digests(self):
        value = request()
        self.assertEqual(
            drill.validate_target_build_records(value, build_records(value)),
            value["target_images"],
        )
        changed = build_records(value)
        changed["backend"]["image"] = image("backend", "9")
        with self.assertRaisesRegex(drill.ReleaseError, "TARGET_BUILD_RECORD_MISMATCH"):
            drill.validate_target_build_records(value, changed)
        changed = build_records(value)
        changed["backend"]["unexpected"] = True
        with self.assertRaisesRegex(drill.ReleaseError, "INVALID_TARGET_BUILD_RECORD"):
            drill.validate_target_build_records(value, changed)

    def test_verify_images_binds_target_build_run_to_exact_sha_and_attempt(self):
        value = request()
        with (
            patch.object(drill, "accepted") as accepted,
            patch.object(
                drill,
                "run_info",
                return_value={"head_sha": TARGET_RELEASE, "run_attempt": 1},
            ) as run_info,
            patch.object(
                drill,
                "target_build_record_images",
                return_value=value["target_images"],
            ) as records,
            patch.object(drill, "verify_image") as verify_image,
            patch.object(drill, "command", return_value="") as command,
            patch.object(drill, "require_image_schema") as require_schema,
            patch.object(
                drill,
                "image_migration_inventory",
                return_value=value["migration_path"],
            ),
        ):
            drill.verify_images(value, "cosign")
        self.assertEqual(accepted.call_count, 2)
        accepted.assert_any_call(SOURCE_RELEASE)
        accepted.assert_any_call(TARGET_RELEASE)
        run_info.assert_called_once_with(101, "release-images.yml", "workflow_run")
        records.assert_called_once_with(value)
        self.assertEqual(verify_image.call_count, 4)
        self.assertEqual(command.call_count, 4)
        self.assertEqual(require_schema.call_count, 2)

        with (
            patch.object(drill, "accepted"),
            patch.object(
                drill,
                "run_info",
                return_value={"head_sha": "3" * 40, "run_attempt": 1},
            ),
        ):
            with self.assertRaisesRegex(drill.ReleaseError, "TARGET_BUILD_RUN_MISMATCH"):
                drill.verify_images(value, "cosign")

    def test_cli_redacts_subprocess_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request()))
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "migration_release_drill",
                        "--request",
                        str(request_path),
                        "--output",
                        str(root / "proof.json"),
                    ],
                ),
                patch.object(
                    drill,
                    "rehearse",
                    side_effect=subprocess.TimeoutExpired(
                        ["pg_dump", "access_token=secret"], 30
                    ),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaises(SystemExit):
                    drill.main()
            self.assertNotIn("secret", output.getvalue())
            self.assertEqual(
                json.loads(output.getvalue()),
                {"status": "FAIL", "automatic_retry": False},
            )


if __name__ == "__main__":
    unittest.main()

