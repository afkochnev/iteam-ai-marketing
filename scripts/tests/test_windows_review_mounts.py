"""Offline regressions: no Docker, credentials, sockets or application data."""

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch

REVIEW_SOURCES = Path(__file__).resolve().parents[1] / "windows_review"
sys.path.insert(0, str(REVIEW_SOURCES))

import review_validation as validation  # noqa: E402
import start_iteam as launcher  # noqa: E402
import windows_local_access as bridge  # noqa: E402
sys.path.remove(str(REVIEW_SOURCES))


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
    networks = {"review-internal": {
        "Internal": True,
        "Containers": {row["Id"]: {} for row in rows.values()},
    }}
    return rows, networks, release


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

    def test_requested_and_effective_bindings_must_both_be_loopback(self):
        rows, networks, release = snapshot_fixture()
        backend = rows["backend"]
        local = [{"HostIp": "127.0.0.1", "HostPort": "8000"}]
        backend["HostConfig"]["PortBindings"] = {"8000/tcp": local}
        backend["NetworkSettings"]["Ports"] = {"8000/tcp": None}
        self.assertEqual(self.check(rows, networks, release), "backend-container")
        backend["NetworkSettings"]["Ports"]["8000/tcp"] = local
        self.assertEqual(self.check(rows, networks, release), "backend-container")
        for ip in ("0.0.0.0", "::", ""):
            with self.subTest(ip=ip):
                backend["NetworkSettings"]["Ports"]["8000/tcp"] = [{"HostIp": ip}]
                with self.assertRaisesRegex(RuntimeError, "LOOPBACK_BIND_REQUIRED"):
                    self.check(rows, networks, release)
        backend["NetworkSettings"]["Ports"] = {}
        backend["HostConfig"]["PublishAllPorts"] = True
        with self.assertRaisesRegex(RuntimeError, "LOOPBACK_BIND_REQUIRED"):
            self.check(rows, networks, release)

    def test_isolated_services_must_share_one_internal_network(self):
        rows, networks, release = snapshot_fixture()
        networks["different-internal"] = {"Internal": True}
        rows["frontend"]["NetworkSettings"]["Networks"] = {"different-internal": {}}
        with self.assertRaisesRegex(RuntimeError, "SHARED_INTERNAL_NETWORK_REQUIRED"):
            self.check(rows, networks, release)

    def test_shared_review_network_must_contain_exactly_the_review_containers(self):
        for change in ("foreign-container", "missing-container", "missing-inventory"):
            with self.subTest(change=change):
                rows, networks, release = snapshot_fixture()
                network = networks["review-internal"]
                if change == "foreign-container":
                    network["Containers"]["another-project-container"] = {}
                elif change == "missing-container":
                    del network["Containers"][rows["frontend"]["Id"]]
                else:
                    del network["Containers"]
                with self.assertRaisesRegex(RuntimeError, "REVIEW_NETWORK_MEMBERSHIP_MISMATCH"):
                    self.check(rows, networks, release)

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

    def setUp(self):
        guard = patch.object(launcher, "require_review_host")
        guard.start()
        self.addCleanup(guard.stop)

    def volume_rows(self):
        return [
            {"Name": name, "Driver": "local", "Options": None, "Labels": {
                "com.docker.compose.project": validation.PROJECT,
                "com.docker.compose.volume": key,
            }}
            for key, name, _ in validation.VOLUMES.values()
        ]

    def test_previous_restore_marker_cannot_create_missing_volumes(self):
        runtime = self.runtime(config_fixture())
        for result in (RuntimeError("DOCKER_READ_CHECK_FAILED"), json.dumps(self.volume_rows()[:1])):
            with self.subTest(result=result), patch.object(launcher, "ROOT", self.RestoredRoot()):
                with patch.object(validation, "docker_read", side_effect=[result]):
                    with self.assertRaises(RuntimeError):
                        launcher.start_review(runtime)
                runtime.compose.assert_called_once_with("config", "--format", "json")
                runtime.compose.reset_mock()

    def test_existing_foreign_or_redirected_engine_volume_never_starts(self):
        for change in (
            {"Name": "other"}, {"Driver": "nfs"}, {"Options": {"type": "none", "o": "bind"}},
            {"Labels": {"com.docker.compose.project": "other"}},
            {"Labels": {"com.docker.compose.volume": "other"}},
        ):
            with self.subTest(change=change), patch.object(launcher, "ROOT", self.RestoredRoot()):
                rows = self.volume_rows()
                rows[0].update(change)
                runtime = self.runtime(config_fixture())
                with patch.object(validation, "docker_read", return_value=json.dumps(rows)):
                    with self.assertRaises(RuntimeError):
                        launcher.start_review(runtime)
                runtime.compose.assert_called_once_with("config", "--format", "json")

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
        rows = self.volume_rows()
        # Transfers may have created a named local volume before Compose added labels.
        rows[0]["Labels"] = None
        with (
            patch.object(launcher, "ROOT", self.RestoredRoot()),
            patch.object(validation, "docker_read", return_value=json.dumps(rows)) as read,
        ):
            launcher.start_review(runtime)
        read.assert_called_once_with(
            "volume", "inspect", "iteam_windows_transfer_windows_postgres",
            "iteam_windows_transfer_windows_redis",
        )
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


class ReviewHostTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        (self.root / "windows").mkdir()
        (self.root / "windows/database-import-PASS.json").touch()
        (self.root / "windows/release.json").write_text("{}")

    def test_deployed_profiles_block_launcher_and_standalone_bridge(self):
        for name in ("publication-runtime.json", "user-post-runtime.json"):
            with self.subTest(profile=name):
                marker = self.root / "windows" / name
                marker.touch()
                runtime = Mock()
                with (
                    patch.object(launcher, "ROOT", self.root),
                    patch.object(bridge, "ROOT", self.root),
                    patch.object(validation, "docker_read") as read,
                    patch.object(bridge, "Bridge") as listener,
                ):
                    with self.assertRaisesRegex(RuntimeError, "PUBLISHING_INSTALLATION_REFUSED"):
                        launcher.start_review(runtime)
                    with self.assertRaisesRegex(RuntimeError, "PUBLISHING_INSTALLATION_REFUSED"):
                        bridge.main()
                runtime.compose.assert_not_called()
                read.assert_not_called()
                listener.assert_not_called()
                marker.unlink()

    def test_direct_bridge_rejects_redirected_volume_before_binding(self):
        volumes = [{"Name": name, "Driver": "local", "Options": None}
                   for _, name, _ in validation.VOLUMES.values()]
        volumes[0]["Options"] = {"type": "nfs", "device": ":/other-database"}
        with (
            patch.object(bridge, "ROOT", self.root),
            patch.object(bridge, "require_review_host"),
            patch.object(validation, "docker_read", return_value=json.dumps(volumes)),
            patch.object(bridge, "Bridge") as listener,
        ):
            with self.assertRaisesRegex(RuntimeError, "REVIEW_VOLUME_IDENTITY_MISMATCH"):
                bridge.main()
        listener.assert_not_called()

    def test_existing_publisher_or_user_worker_including_stopped_blocks_start(self):
        for project in ("iteam_windows_publishing", "iteam_windows_user_posts"):
            with self.subTest(project=project):
                result = [b"npipe:////./pipe/dockerDesktopLinuxEngine", project.encode() + b"\n"]
                runtime = Mock()
                with (
                    patch.object(launcher, "ROOT", self.root),
                    patch.object(validation, "os", SimpleNamespace(name="nt", environ={})),
                    patch.object(validation, "docker_read", side_effect=result) as read,
                ):
                    with self.assertRaisesRegex(RuntimeError, "PUBLISHING_INSTALLATION_REFUSED"):
                        launcher.start_review(runtime)
                inventory = read.call_args_list[-1].args
                self.assertEqual(inventory[0], "ps")
                self.assertIn("--all", inventory)
                runtime.compose.assert_not_called()

    def test_foreign_docker_endpoint_and_failed_inventory_block_review(self):
        for replies, error in [
            ([b"npipe:////remote/pipe/dockerDesktopLinuxEngine"], "LOCAL_LINUX_ENGINE_REQUIRED"),
            ([b"tcp://localhost:2375"], "LOCAL_LINUX_ENGINE_REQUIRED"),
            ([b"npipe:////./pipe/dockerDesktopLinuxEngine", RuntimeError("DOCKER_READ_CHECK_FAILED")],
             "DOCKER_READ_CHECK_FAILED"),
        ]:
            with (
                self.subTest(error=error),
                patch.object(validation, "os", SimpleNamespace(name="nt", environ={})),
                patch.object(validation, "docker_read", side_effect=replies),
            ):
                with self.assertRaisesRegex(RuntimeError, error):
                    validation.require_review_host(self.root)

    def test_local_engine_is_pinned_for_runtime_compose(self):
        with (
            patch.object(validation, "os", SimpleNamespace(name="nt", environ={})),
            patch.object(validation, "docker_read", side_effect=[
                b"npipe:////./pipe/dockerDesktopLinuxEngine", b"iteam_windows_transfer\n", b"",
            ]),
        ):
            validation.require_review_host(self.root)
            self.assertEqual(validation.os.environ.get("DOCKER_CONTEXT"), "desktop-linux")

    def test_overrides_do_not_contact_another_engine(self):
        for override in ({"DOCKER_HOST": "tcp://forbidden"}, {"DOCKER_CONTEXT": "other"}):
            with (
                self.subTest(override=override),
                patch.object(validation, "os", SimpleNamespace(name="nt", environ=override)),
                patch.object(validation, "docker_read") as read,
            ):
                with self.assertRaisesRegex(RuntimeError, "LOCAL_WINDOWS_DOCKER_DESKTOP_REQUIRED"):
                    validation.require_review_host(self.root)
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
