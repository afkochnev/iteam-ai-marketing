"""Pure volume checks for the accepted, isolated Windows review stack."""

PROJECT = "iteam_windows_transfer"
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
