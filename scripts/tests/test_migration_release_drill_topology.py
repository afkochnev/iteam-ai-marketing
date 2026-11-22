"""Disposable topology regression for the migration rehearsal."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
drill = importlib.import_module("migration_release_drill")


def image(component: str, marker: str) -> str:
    return f"ghcr.io/{drill.REPOSITORY}-{component}@sha256:" + marker * 64


def request() -> dict:
    path = [
        {
            "revision": "20261121_0029",
            "down_revision": "20261009_0028",
            "script_sha256": "sha256:" + "a" * 64,
        },
        {
            "revision": "20261121_0030",
            "down_revision": "20261121_0029",
            "script_sha256": "sha256:" + "b" * 64,
        },
    ]
    plan_hash = drill.migration_plan_hash(
        source_schema_head="20261009_0028",
        target_schema_head="20261121_0030",
        migration_path=path,
        compatibility_class="EXPAND_ONLY",
        pre_activation_rollback_policy="PRE_ACTIVATION_DOWNGRADE",
    )
    return {
        "version": drill.REQUEST_VERSION,
        "request_type": drill.REQUEST_TYPE,
        "repository": drill.REPOSITORY,
        "source_release_sha": "1" * 40,
        "target_release_sha": "2" * 40,
        "target_build_run_id": 101,
        "target_build_attempt": 1,
        "source_schema_head": "20261009_0028",
        "target_schema_head": "20261121_0030",
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


class MigrationReleaseDrillTopologyTests(unittest.TestCase):
    def test_redis_volume_is_named_and_inside_project_teardown_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drill.create_drill(root, "iteam-migration-drill-0000000000", request())
            config = json.loads((root / "compose.json").read_text())
        self.assertEqual(config["services"]["redis"]["volumes"], ["redis_data:/data"])
        self.assertEqual(config["volumes"]["redis_data"], {})
        self.assertNotIn("/var/run/docker.sock", json.dumps(config))

    def test_source_and_target_commands_pin_generated_compose_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = drill.create_drill(root, "iteam-migration-drill-0000000000", request())
            with patch.object(drill, "command", return_value="") as command:
                runtime.compose("config")
                source_args = command.call_args.args[0]
                runtime.switch_images(request(), target=True)
                runtime.compose("config")
                target_args = command.call_args.args[0]
            for args, side in ((source_args, "source"), (target_args, "target")):
                selected = [args[i + 1] for i, flag in enumerate(args) if flag == "-f"]
                self.assertEqual(selected, [str(root / "compose.json"), str(root / f"{side}-images.json")])
                self.assertEqual(args[args.index("--project-directory") + 1], str(root))
                self.assertEqual(args[args.index("--project-name") + 1], runtime.project)
            self.assertNotIn("COMPOSE_FILE=", runtime.env_path.read_text())

    def test_host_compose_overrides_fail_before_any_external_command(self):
        for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PATH_SEPARATOR", "COMPOSE_ENV_FILES", "COMPOSE_PROFILES"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                with patch.dict(os.environ, {key: "untrusted"}), patch.object(drill, "command") as command:
                    with self.assertRaisesRegex(drill.ReleaseError, "MIGRATION_DRILL_COMPOSE_ENVIRONMENT_OVERRIDE"):
                        drill.rehearse(request(), Path(directory) / "proof.json", "cosign")
                    command.assert_not_called()

    def test_cleanup_checks_stopped_containers_volumes_and_networks(self):
        runtime = Mock(project="iteam-migration-drill-0000000000")
        with patch.object(drill, "command", return_value="") as command:
            drill.remove_disposable_project(runtime)
        runtime.compose.assert_called_once_with("down", "--volumes", "--remove-orphans")
        args = [call.args[0] for call in command.call_args_list]
        self.assertIn("--all", args[0])
        self.assertEqual([call[1] for call in args], ["container", "volume", "network"])
        with patch.object(drill, "command", return_value="leaked-resource"):
            with self.assertRaisesRegex(drill.ReleaseError, "MIGRATION_DRILL_CLEANUP_INCOMPLETE"):
                drill.remove_disposable_project(runtime)


if __name__ == "__main__":
    unittest.main()

