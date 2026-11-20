"""Offline regressions: no Docker, credentials, sockets or application data."""

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

REVIEW_SOURCES = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REVIEW_SOURCES))

import review_validation as validation  # noqa: E402
import start_iteam as launcher  # noqa: E402
import windows_local_access as bridge  # noqa: E402


def config_fixture():
    return {
        "name": validation.PROJECT,
        "volumes": {
            key: {"name": name}
            for key, name, _destination in validation.VOLUMES.values()
        },
        "services": {
            service: {
                "volumes": [
                    {"type": "volume", "source": expected[0], "target": expected[2]}
                ]
                if (expected := validation.VOLUMES.get(service))
                else []
            }
            for service in validation.SERVICES
        },
    }


def snapshot_fixture():
    release = {
        "images": {"backend": "backend@sha256:accepted", "frontend": "frontend@sha256:accepted"},
        "release_sha": "accepted-release",
    }
    rows = {}
    for service in validation.SERVICES:
        expected = validation.VOLUMES.get(service)
        rows[service] = {
            "Id": f"{service}-container",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": validation.PROJECT,
                    "com.docker.compose.service": service,
                },
                "Image": release["images"].get(service, "data-image"),
                "Env": [f"{flag}=false" for flag in bridge.FLAGS]
                + [f"BUILD_SHA={release['release_sha']}"],
            },
            "State": {"Status": "running"},
            "HostConfig": {},
            "NetworkSettings": {"Networks": {"review-internal": {}}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": expected[1],
                    "Destination": expected[2],
                    "Driver": "local",
                    "RW": True,
                }
            ]
            if expected
            else [],
        }
    return rows, {"review-internal": {"Internal": True}}, release


class ComposeMountTests(unittest.TestCase):
    def test_accepted_compose_mounts(self):
        validation.validate_compose_mounts(config_fixture())

    def test_foreign_or_redirected_volume_declarations(self):
        for field, value in (
            ("name", "unrelated_database"),
            ("external", True),
            ("driver", "another-driver"),
            ("driver_opts", {"type": "none", "device": "/other-data", "o": "bind"}),
        ):
            with self.subTest(field=field):
                config = config_fixture()
                config["volumes"]["windows_postgres"][field] = value
                with self.assertRaisesRegex(RuntimeError, "REVIEW_VOLUME_CONFIG_MISMATCH"):
                    validation.validate_compose_mounts(config)

    def test_missing_extra_or_swapped_mounts(self):
        for mutation in ("missing", "extra", "swapped", "wrong-destination", "read-only", "subpath"):
            with self.subTest(mutation=mutation):
                config = config_fixture()
                mounts = config["services"]["postgres"]["volumes"]
                if mutation == "missing":
                    mounts.clear()
                elif mutation == "extra":
                    mounts.append(copy.deepcopy(mounts[0]))
                elif mutation == "swapped":
                    mounts[0]["source"] = "windows_redis"
                elif mutation == "wrong-destination":
                    mounts[0]["target"] = "/another-database"
                elif mutation == "read-only":
                    mounts[0]["read_only"] = True
                else:
                    mounts[0]["volume"] = {"subpath": "other-database"}
                with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_REVIEW_MOUNT"):
                    validation.validate_compose_mounts(config)

    def test_application_bind_and_credential_mounts_are_rejected(self):
        for service, mount in (
            ("backend", {"type": "volume", "source": "windows_postgres", "target": "/data"}),
            ("frontend", {"type": "volume", "source": "credentials", "target": "/secrets"}),
            ("postgres", {"type": "bind", "source": "C:/other", "target": "/data"}),
        ):
            with self.subTest(service=service, mount_type=mount["type"]):
                config = config_fixture()
                config["services"][service]["volumes"] = [mount]
                with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_REVIEW_MOUNT"):
                    validation.validate_compose_mounts(config)

    def test_indirect_mount_sources_are_rejected(self):
        for field in ("volumes_from", "tmpfs", "secrets", "configs"):
            with self.subTest(field=field):
                config = config_fixture()
                config["services"]["backend"][field] = ["unexpected"]
                with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_REVIEW_MOUNT"):
                    validation.validate_compose_mounts(config)

    def test_volume_set_and_project_identity_are_fixed(self):
        for mutation in ("extra-declaration", "missing-declaration", "different-project"):
            with self.subTest(mutation=mutation):
                config = config_fixture()
                if mutation == "extra-declaration":
                    config["volumes"]["credentials"] = {}
                elif mutation == "missing-declaration":
                    del config["volumes"]["windows_redis"]
                else:
                    config["name"] = "other-project"
                with self.assertRaises(RuntimeError):
                    validation.validate_compose_mounts(config)


class SnapshotMountTests(unittest.TestCase):
    def check(self, rows, networks, release):
        # Any accidental subprocess call in the tested path fails before execution.
        with patch.object(bridge.subprocess, "run", side_effect=AssertionError("No Docker")):
            return bridge.check_snapshot(list(rows.values()), networks, release)

    def test_accepted_actual_mounts(self):
        self.assertEqual(self.check(*snapshot_fixture()), "backend-container")

    def test_unrelated_volume_cannot_pass_with_same_images_and_build_sha(self):
        rows, networks, release = snapshot_fixture()
        rows["postgres"]["Mounts"][0]["Name"] = "another_postgres"
        rows["redis"]["Mounts"][0]["Name"] = "another_redis"
        with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_REVIEW_MOUNT"):
            self.check(rows, networks, release)

    def test_actual_mount_identity_destination_and_access_are_fixed(self):
        for field, value in (
            ("Type", "bind"),
            ("Name", validation.VOLUMES["redis"][1]),
            ("Destination", "/another-database"),
            ("RW", False),
            ("Driver", "another-driver"),
        ):
            with self.subTest(field=field):
                rows, networks, release = snapshot_fixture()
                rows["postgres"]["Mounts"][0][field] = value
                with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_REVIEW_MOUNT"):
                    self.check(rows, networks, release)

    def test_actual_missing_duplicate_and_application_mounts_are_rejected(self):
        for mutation in ("missing", "duplicate", "backend-data", "frontend-credentials"):
            with self.subTest(mutation=mutation):
                rows, networks, release = snapshot_fixture()
                mount = copy.deepcopy(rows["postgres"]["Mounts"][0])
                if mutation == "missing":
                    rows["postgres"]["Mounts"] = []
                elif mutation == "duplicate":
                    rows["postgres"]["Mounts"].append(mount)
                elif mutation == "backend-data":
                    rows["backend"]["Mounts"].append(mount)
                else:
                    mount["Name"] = "iteam_windows_transfer_vk_publishing_credentials"
                    rows["frontend"]["Mounts"].append(mount)
                with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_REVIEW_MOUNT"):
                    self.check(rows, networks, release)


    def test_hidden_mount_options_do_not_bypass_actual_mount_checks(self):
        for field, value in (
            ("VolumesFrom", ["other-container"]),
            ("Tmpfs", {"/secrets": "rw"}),
            ("Mounts", [{"VolumeOptions": {"Subpath": "another-database"}}]),
        ):
            with self.subTest(field=field):
                rows, networks, release = snapshot_fixture()
                rows["postgres"]["HostConfig"][field] = value
                with self.assertRaisesRegex(RuntimeError, "UNEXPECTED_REVIEW_MOUNT"):
                    self.check(rows, networks, release)


class LauncherMountTests(unittest.TestCase):
    class RestoredRoot:
        def __truediv__(self, _path):
            return self

        def is_file(self):
            return True

    def runtime(self, config):
        return SimpleNamespace(
            validate_review_config=Mock(),
            compose=Mock(return_value=SimpleNamespace(stdout=json.dumps(config).encode())),
        )

    def test_bad_config_is_rejected_before_any_startup(self):
        config = config_fixture()
        config["volumes"]["windows_postgres"]["name"] = "another_database"
        runtime = self.runtime(config)
        with patch.object(launcher, "ROOT", self.RestoredRoot()):
            with self.assertRaisesRegex(RuntimeError, "REVIEW_VOLUME_CONFIG_MISMATCH"):
                launcher.start_review(runtime)
        runtime.validate_review_config.assert_called_once_with()
        runtime.compose.assert_called_once_with("config", "--format", "json")

    def test_accepted_config_retains_exact_four_service_start_command(self):
        runtime = self.runtime(config_fixture())
        with patch.object(launcher, "ROOT", self.RestoredRoot()):
            launcher.start_review(runtime)
        self.assertEqual(
            runtime.compose.call_args_list,
            [
                call("config", "--format", "json"),
                call(
                    "up", "--pull", "never", "--no-build", "-d", "--wait",
                    "--wait-timeout", "90", "postgres", "redis", "backend", "frontend",
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
