"""Restore the encrypted fixture into its disposable rehearsal database.

This helper has no CLI and accepts only the generated migration-drill runtime.
The original fixture database is dropped before restoration, so subsequent
migrations necessarily exercise data recovered from the encrypted backup.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from release_backup import check_proof
from release_ops import ReleaseError, Runtime, require

DATABASE_NAME = "iteam_migration_drill"
_PROJECT = re.compile(r"iteam-migration-drill-[0-9a-f]{10}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _private_directory(path: Path) -> None:
    require(
        path.is_absolute()
        and not path.is_symlink()
        and path.is_dir()
        and path.resolve() == path
        and path.stat().st_uid == os.getuid()
        and stat.S_IMODE(path.stat().st_mode) == 0o700,
        "UNSAFE_MIGRATION_FIXTURE_DIRECTORY",
    )


def _restore_inputs(runtime: Runtime, proof: dict[str, Any]) -> tuple[Path, Path]:
    root = runtime.root
    project = getattr(runtime, "project", None)
    require(
        isinstance(project, str) and _PROJECT.fullmatch(project),
        "UNSAFE_MIGRATION_FIXTURE_PROJECT",
    )
    _private_directory(root)
    for directory in (root / "backups", root / "keys"):
        _private_directory(directory)

    # The decrypt/restore subprocess must use exactly the generated Compose
    # configuration. Environment-derived Compose paths are never sufficient.
    base = runtime.base
    require(
        type(base) is list
        and base[:2] == ["docker", "compose"]
        and len(base[2:]) % 2 == 0,
        "UNSAFE_MIGRATION_FIXTURE_COMPOSE",
    )
    options: dict[str, list[str]] = {}
    for flag, value in zip(base[2::2], base[3::2]):
        require(
            flag in {"--project-directory", "--project-name", "--env-file", "-f"}
            and isinstance(value, str),
            "UNSAFE_MIGRATION_FIXTURE_COMPOSE",
        )
        options.setdefault(flag, []).append(value)
    require(
        options
        == {
            "--project-directory": [str(root)],
            "--project-name": [project],
            "--env-file": [str(root / ".env")],
            "-f": [str(root / "compose.json"), str(root / "source-images.json")],
        },
        "UNSAFE_MIGRATION_FIXTURE_COMPOSE",
    )

    require(
        type(proof) is dict
        and isinstance(proof.get("source_release"), str)
        and _SHA.fullmatch(proof["source_release"]),
        "INVALID_MIGRATION_FIXTURE_PROOF",
    )
    # Recheck the generated proof and ciphertext immediately before deletion.
    checked = check_proof(root / "backup-proof.json", proof["source_release"])
    require(checked == proof, "MIGRATION_FIXTURE_PROOF_CHANGED")
    backup, key = Path(checked["backup"]), Path(checked["key_path"])
    require(
        backup.parent == root / "backups" and key.parent == root / "keys",
        "MIGRATION_FIXTURE_BACKUP_OUTSIDE_PROJECT",
    )

    for service in ("postgres", "backend", "frontend"):
        container = runtime.inspect(service)
        labels = container["Config"].get("Labels", {})
        require(
            labels.get("com.docker.compose.project") == project
            and labels.get("com.docker.compose.service") == service,
            "MIGRATION_FIXTURE_CONTAINER_OUTSIDE_PROJECT",
        )
        require(
            container["State"]["Running"] is (service == "postgres"),
            "MIGRATION_FIXTURE_WRITERS_NOT_STOPPED",
        )
        if service == "postgres":
            environment = dict(
                entry.split("=", 1)
                for entry in container["Config"]["Env"]
                if "=" in entry
            )
            require(
                environment.get("POSTGRES_USER") == "iteam"
                and environment.get("POSTGRES_DB") == DATABASE_NAME,
                "UNSAFE_MIGRATION_FIXTURE_DATABASE",
            )
    return backup, key


def _reap(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            # A child may exit between poll and kill; it still must be reaped.
            pass
    process.wait(timeout=10)


def restore_fixture(runtime: Runtime, proof: dict[str, Any]) -> None:
    """Replace the stopped, disposable fixture DB from its checked ciphertext.

    Both pipeline exit codes must succeed. Any failure leaves the enclosing
    rehearsal responsible for removing its project; this function never retries
    restoration or writes a success proof.
    """
    decrypted = None
    restore = None
    try:
        backup, key = _restore_inputs(runtime, proof)
        # No --force: an unexpected connection prevents deletion.
        runtime.compose(
            "exec", "-T", "postgres", "dropdb", "-U", "iteam", DATABASE_NAME
        )
        runtime.compose(
            "exec",
            "-T",
            "postgres",
            "createdb",
            "-U",
            "iteam",
            "-O",
            "iteam",
            DATABASE_NAME,
        )
        decrypted = subprocess.Popen(
            [
                "openssl",
                "enc",
                "-d",
                "-aes-256-cbc",
                "-pbkdf2",
                "-iter",
                "200000",
                "-pass",
                "file:" + str(key),
                "-in",
                str(backup),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        restore = subprocess.Popen(
            runtime.base
            + [
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "-U",
                "iteam",
                "-d",
                DATABASE_NAME,
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
            ],
            cwd=runtime.root,
            stdin=decrypted.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        decrypted.stdout.close()
        restore_status = restore.wait(timeout=300)
        decrypt_status = decrypted.wait(timeout=30)
        require(restore_status == decrypt_status == 0, "MIGRATION_FIXTURE_RESTORE_FAILED")
    except (OSError, subprocess.SubprocessError):
        raise ReleaseError("MIGRATION_FIXTURE_RESTORE_FAILED") from None
    finally:
        if decrypted is not None and decrypted.stdout is not None:
            decrypted.stdout.close()
        cleanup_failed = False
        for process in (restore, decrypted):
            try:
                _reap(process)
            except (OSError, subprocess.SubprocessError):
                cleanup_failed = True
        if cleanup_failed:
            raise ReleaseError("MIGRATION_FIXTURE_RESTORE_CLEANUP_FAILED") from None
