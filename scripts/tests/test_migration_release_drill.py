"""Isolated migration drill tests: no Docker, GitHub, provider, or production access."""

from __future__ import annotations

import copy
import importlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
drill = importlib.import_module("migration_release_drill")

SOURCE_RELEASE = "1" * 40
TARGET_RELEASE = "2" * 40
SOURCE_SCHEMA = "20261009_0028"
TARGET_SCHEMA = "20261121_0030"


def image(component: str, marker: str) -> str:
    return f"ghcr.io/{drill.REPOSITORY}-{component}@sha256:" + marker * 64


def request() -> dict:
    migration_path = [
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
        migration_path=migration_path,
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
        "migration_path": migration_path,
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


class MigrationReleaseDrillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_request_contract_is_separate_from_final_manifest_and_fail_closed(self):
        value = request()
        drill.validate_rehearsal_request(value)
        self.assertEqual(
            drill.rehearsal_scope(value),
            {
                "source_release_sha": SOURCE_RELEASE,
                "target_release_sha": TARGET_RELEASE,
                "target_build_run_id": value["target_build_run_id"],
                "target_build_attempt": value["target_build_attempt"],
                "source_schema_head": SOURCE_SCHEMA,
                "target_schema_head": TARGET_SCHEMA,
                "migration_plan_hash": value["migration_plan_hash"],
                "source_images": value["source_images"],
                "target_images": value["target_images"],
            },
        )
        for key, replacement, code in [
            ("version", 2, "INVALID_REHEARSAL_REQUEST_IDENTITY"),
            ("source_release_sha", "bad", "INVALID_SOURCE_RELEASE_SHA"),
            ("target_build_run_id", True, "INVALID_TARGET_BUILD_RUN"),
            (
                "compatibility_class",
                "DESTRUCTIVE",
                "UNSUPPORTED_MIGRATION_COMPATIBILITY_CLASS",
            ),
            ("frontend_api_url", "https://other", "UNREVIEWED_FRONTEND_ENDPOINT"),
        ]:
            with self.subTest(key=key):
                changed = request()
                changed[key] = replacement
                with self.assertRaisesRegex(drill.ReleaseError, code):
                    drill.validate_rehearsal_request(changed)
        changed = request()
        changed["rehearsal_proof"] = {}
        with self.assertRaisesRegex(
            drill.ReleaseError, "UNEXPECTED_REHEARSAL_REQUEST_FIELDS"
        ):
            drill.validate_rehearsal_request(changed)

    def test_request_binds_exact_images_and_canonical_plan_hash(self):
        changed = request()
        changed["target_images"]["backend"] = "backend:latest"
        with self.assertRaisesRegex(drill.ReleaseError, "INVALID_REHEARSAL_IMAGE"):
            drill.validate_rehearsal_request(changed)

        changed = request()
        changed["migration_path"][1]["script_sha256"] = "sha256:" + "7" * 64
        with self.assertRaisesRegex(
            drill.ReleaseError, "MIGRATION_PLAN_HASH_MISMATCH"
        ):
            drill.validate_rehearsal_request(changed)

    def test_target_image_inventory_must_match_exact_scripts(self):
        value = request()
        inventory = copy.deepcopy(value["migration_path"])
        drill.validate_image_migration_inventory(value, inventory)

        changed = copy.deepcopy(inventory)
        changed[1]["script_sha256"] = "sha256:" + "7" * 64
        with self.assertRaisesRegex(
            drill.ReleaseError, "MIGRATION_SCRIPT_IMAGE_MISMATCH"
        ):
            drill.validate_image_migration_inventory(value, changed)

        with self.assertRaisesRegex(
            drill.ReleaseError, "MIGRATION_SCRIPT_MISSING_FROM_IMAGE"
        ):
            drill.validate_image_migration_inventory(value, inventory[:1])

    def test_inventory_is_read_from_network_disabled_container(self):
        inventory = request()["migration_path"]
        with patch.object(drill, "command", return_value=json.dumps(inventory)) as cmd:
            self.assertEqual(
                drill.image_migration_inventory(request()["target_images"]["backend"]),
                inventory,
            )
        args = cmd.call_args.args[0]
        self.assertEqual(args[:5], ["docker", "run", "--rm", "--network", "none"])
        self.assertIn("--entrypoint", args)

    def test_workspace_invariants_cover_active_inactive_roles_and_deterministic_ids(self):
        admin_id = "11111111-1111-4111-8111-111111111111"
        users = [
            {"id": admin_id, "role": "ADMIN", "is_active": True},
            {
                "id": str(drill.ACTIVE_MANAGER_ID),
                "role": "MANAGER",
                "is_active": True,
            },
            {
                "id": str(drill.INACTIVE_MANAGER_ID),
                "role": "MANAGER",
                "is_active": False,
            },
        ]
        workspaces = [
            {
                "id": str(drill.DEFAULT_WORKSPACE_ID),
                "name": drill.DEFAULT_WORKSPACE_NAME,
                "slug": drill.DEFAULT_WORKSPACE_SLUG,
                "is_default": True,
                "is_active": True,
            }
        ]
        memberships = [
            {
                "id": str(
                    uuid5(
                        drill.DEFAULT_WORKSPACE_ID,
                        f"membership:{user_id}",
                    )
                ),
                "workspace_id": str(drill.DEFAULT_WORKSPACE_ID),
                "user_id": user_id,
                "role": role,
                "is_active": True,
            }
            for user_id, role in [
                (admin_id, "WORKSPACE_ADMIN"),
                (str(drill.ACTIVE_MANAGER_ID), "MARKETING_MANAGER"),
            ]
        ]
        drill.validate_workspace_invariants(users, workspaces, memberships)

        changed = copy.deepcopy(memberships)
        changed.append(
            {
                "id": str(UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")),
                "workspace_id": str(drill.DEFAULT_WORKSPACE_ID),
                "user_id": str(drill.INACTIVE_MANAGER_ID),
                "role": "MARKETING_MANAGER",
                "is_active": True,
            }
        )
        with self.assertRaisesRegex(
            drill.ReleaseError, "ACTIVE_USER_MEMBERSHIP_COVERAGE_MISMATCH"
        ):
            drill.validate_workspace_invariants(users, workspaces, changed)

    def test_compose_is_disposable_internal_and_contains_no_provider_credentials(self):
        runtime = drill.create_drill(
            self.root, "iteam-migration-drill-0000000000", request()
        )
        self.assertEqual(runtime.root, self.root.resolve())
        config = json.loads((self.root / "compose.json").read_text())
        self.assertTrue(config["networks"]["default"]["internal"])
        self.assertEqual(
            set(config["services"]), {"postgres", "redis", "backend", "frontend"}
        )
        for service in config["services"].values():
            self.assertNotIn("ports", service)
            for volume in service.get("volumes", []):
                self.assertFalse(volume.startswith("/"))
        env = (self.root / ".env").read_text()
        for line in [
            "VK_PUBLISHING_ENABLED=false",
            "TELEGRAM_PUBLISHING_ENABLED=false",
            "METRICS_SYNC_ENABLED=false",
            "OPENAI_API_KEY=",
            "VK_ACCESS_TOKEN=",
            "VK_CREDENTIALS_FILE=",
        ]:
            self.assertIn(line, env)
        self.assertNotIn("/run/iteam-secrets", json.dumps(config))
        source = json.loads((self.root / "source-images.json").read_text())
        target = json.loads((self.root / "target-images.json").read_text())
        self.assertEqual(
            source["services"]["backend"]["image"],
            request()["source_images"]["backend"],
        )
        self.assertEqual(
            target["services"]["backend"]["image"],
            request()["target_images"]["backend"],
        )

    def test_cli_exposes_no_production_targeting_arguments(self):
        options = {
            option
            for action in drill.build_parser()._actions
            for option in action.option_strings
        }
        self.assertEqual(options, {"-h", "--help", "--request", "--output", "--cosign"})
        self.assertTrue(
            options.isdisjoint(
                {"--root", "--project", "--env", "--port", "--volume", "--backup-dir"}
            )
        )

    def test_cli_failure_does_not_expose_underlying_secret(self):
        request_path = self.root / "request.json"
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
                    str(self.root / "proof.json"),
                ],
            ),
            patch.object(
                drill,
                "rehearse",
                side_effect=KeyError("access_token=secret"),
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

