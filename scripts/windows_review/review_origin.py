"""Verify that existing core containers belong to this review transfer."""

import json
from pathlib import PureWindowsPath


def validate_core_origin(root, docker_read, *, project, services):
    """Check the Compose origin of every existing core container, including stopped ones."""
    windows = PureWindowsPath(root) / "windows"
    config_file = windows / "compose.windows.yaml"
    fields = (
        "com.docker.compose.service",
        "com.docker.compose.project.working_dir",
        "com.docker.compose.project.config_files",
    )
    template = "[" + ",".join('{{json (.Label "' + key + '")}}' for key in fields) + "]"
    rows = docker_read(
        "ps", "--all", "--filter", f"label=com.docker.compose.project={project}",
        "--format", template,
    ).splitlines()
    seen = set()
    for row in rows:
        try:
            labels = json.loads(row)
        except (ValueError, UnicodeError):
            raise RuntimeError("REVIEW_CORE_ORIGIN_MISMATCH") from None
        if not isinstance(labels, list) or len(labels) != 3 or not all(
            isinstance(label, str) and label for label in labels
        ):
            raise RuntimeError("REVIEW_CORE_ORIGIN_MISMATCH")
        service, working_dir, config_files = labels
        # Compose joins multiple config files with a comma. Match its entire label
        # so a publishing override in the same transfer cannot pass as review.
        if (
            service not in services or service in seen
            or PureWindowsPath(working_dir) != windows
            or PureWindowsPath(config_files) != config_file
        ):
            raise RuntimeError("REVIEW_CORE_ORIGIN_MISMATCH")
        seen.add(service)
