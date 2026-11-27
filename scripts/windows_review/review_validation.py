"""Preflight and volume checks for the accepted, isolated Windows review stack."""

import json
import os
import subprocess

from review_origin import validate_core_origin

PROJECT = "iteam_windows_transfer"
CONTEXT = "desktop-linux"
PUBLISHING_PROJECTS = {"iteam_windows_publishing", "iteam_windows_user_posts"}
SERVICES = {"postgres", "redis", "backend", "frontend"}
# Compose keys, engine volume names and container destinations are separate identities.
VOLUMES = {
    "postgres": (
        "windows_postgres",
        "iteam_windows_transfer_windows_postgres",
        "/var/lib/postgresql/data",
    ),
    "redis": ("windows_redis", "iteam_windows_transfer_windows_redis", "/data"),
}


def docker_read(*args):
    result = subprocess.run(
        ["docker", "--context", CONTEXT, *args], capture_output=True, timeout=30
    )
    if result.returncode:
        raise RuntimeError("DOCKER_READ_CHECK_FAILED")
    return result.stdout


def require_review_host(root):
    # A configured profile still owns the installation while its workers are stopped.
    for marker in ("publication-runtime.json", "user-post-runtime.json"):
        try:
            (root / "windows" / marker).lstat()
        except FileNotFoundError:
            continue
        raise RuntimeError("PUBLISHING_INSTALLATION_REFUSED")
    if (
        os.name != "nt"
        or os.environ.get("DOCKER_HOST")
        or os.environ.get("DOCKER_CONTEXT") not in (None, "", CONTEXT)
    ):
        raise RuntimeError("LOCAL_WINDOWS_DOCKER_DESKTOP_REQUIRED")
    endpoint = docker_read("context", "inspect", CONTEXT, "--format", "{{.Endpoints.docker.Host}}")
    if endpoint.strip().lower() != b"npipe:////./pipe/dockerdesktoplinuxengine":
        raise RuntimeError("LOCAL_LINUX_ENGINE_REQUIRED")
    # The transfer's existing runtime helper invokes docker compose without --context.
    os.environ["DOCKER_CONTEXT"] = CONTEXT
    projects = docker_read(
        "ps", "--all", "--filter", "label=com.docker.compose.project",
        "--format", '{{.Label "com.docker.compose.project"}}',
    ).decode().splitlines()
    if PUBLISHING_PROJECTS.intersection(projects):
        raise RuntimeError("PUBLISHING_INSTALLATION_REFUSED")
    validate_core_origin(root, docker_read, project=PROJECT, services=SERVICES)


def require_existing_review_volumes():
    """A previous restore marker must never authorize creating replacement data volumes."""
    expected = {name: key for key, name, _target in VOLUMES.values()}
    rows = json.loads(docker_read("volume", "inspect", *expected))
    if len(rows) != len(expected) or {row.get("Name") for row in rows} != set(expected):
        raise RuntimeError("VERIFIED_REVIEW_VOLUMES_REQUIRED")
    for row in rows:
        labels = row.get("Labels") or {}
        if (
            row.get("Driver") != "local"
            or "Options" not in row
            or row["Options"] not in (None, {})
            or labels.get("com.docker.compose.project", PROJECT) != PROJECT
            or labels.get("com.docker.compose.volume", expected[row["Name"]]) != expected[row["Name"]]
        ):
            raise RuntimeError("REVIEW_VOLUME_IDENTITY_MISMATCH")


def validate_compose_mounts(config):
    """Check effective Compose JSON before any container startup is requested."""
    if config.get("name") != PROJECT or set(config.get("services", {})) != SERVICES:
        raise RuntimeError("ISOLATED_REVIEW_CONFIG_REQUIRED")
    declarations = config.get("volumes", {})
    if set(declarations) != {value[0] for value in VOLUMES.values()}:
        raise RuntimeError("REVIEW_VOLUME_CONFIG_MISMATCH")
    for key, engine_name, _destination in VOLUMES.values():
        volume = declarations[key]
        if (
            volume.get("name", f"{PROJECT}_{key}") != engine_name
            or volume.get("external")
            or volume.get("driver", "local") != "local"
            or volume.get("driver_opts")
        ):
            raise RuntimeError("REVIEW_VOLUME_CONFIG_MISMATCH")
    for service, spec in config["services"].items():
        if spec.get("volumes_from") or spec.get("tmpfs") or spec.get("secrets") or spec.get("configs"):
            raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
        mounts = spec.get("volumes", [])
        expected = VOLUMES.get(service)
        if expected is None:
            if mounts:
                raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
            continue
        if len(mounts) != 1:
            raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
        mount = mounts[0]
        if (
            mount.get("type") != "volume"
            or mount.get("source") != expected[0]
            or mount.get("target") != expected[2]
            or mount.get("read_only", False) is not False
            or mount.get("volume", {}).get("subpath")
        ):
            raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")


def validate_inspect_mounts(by_service):
    """Match actual engine mount identity and destination for every service."""
    if set(by_service) != SERVICES:
        raise RuntimeError("MISSING_REVIEW_SERVICE")
    for service, row in by_service.items():
        host = row.get("HostConfig", {})
        if host.get("VolumesFrom") or host.get("Tmpfs"):
            raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
        if any(
            mount.get("VolumeOptions", {}).get("Subpath")
            for mount in host.get("Mounts", [])
        ):
            raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
        mounts = row.get("Mounts", [])
        expected = VOLUMES.get(service)
        if expected is None:
            if mounts:
                raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
            continue
        if len(mounts) != 1:
            raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
        mount = mounts[0]
        if (
            mount.get("Type") != "volume"
            or mount.get("Name") != expected[1]
            or mount.get("Destination") != expected[2]
            or mount.get("Driver") != "local"
            or mount.get("RW") is not True
            or mount.get("SubPath")
        ):
            raise RuntimeError("UNEXPECTED_REVIEW_MOUNT")
