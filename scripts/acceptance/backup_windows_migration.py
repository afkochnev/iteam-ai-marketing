#!/usr/bin/env python3
"""Keep an encrypted backup of the Windows source database and verify its restore."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from rehearse_windows_migration import (
    CLONE_DB,
    CLONE_ROLE,
    SOURCE_IMAGE_ID,
    SOURCE_REVISION,
    Container,
    Database,
    RehearsalError,
    docker,
    fingerprint,
    read_env,
    require,
    running_container,
    source_columns,
    wait_for_postgres,
)


def find_openssl() -> str:
    installed = shutil.which("openssl")
    if installed:
        return installed
    git_openssl = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / (
        "Git/usr/bin/openssl.exe"
    )
    require(os.name == "nt" and git_openssl.is_file(), "OpenSSL was not found.")
    return str(git_openssl)


@contextmanager
def exported_snapshot(source: Database) -> Iterator[str]:
    options = [
        "docker",
        "exec",
        "-i",
        "--env=PGCONNECT_TIMEOUT=5",
        source.container.id,
        "psql",
        "-XqAt",
        "--no-password",
        "--set=ON_ERROR_STOP=1",
        f"--username={source.user}",
        f"--dbname={source.name}",
    ]
    with subprocess.Popen(
        options,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        encoding="utf-8",
    ) as session:
        assert session.stdin is not None and session.stdout is not None
        try:
            session.stdin.write(
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n"
            )
            session.stdin.flush()
            identifier = session.stdout.readline().strip()
            require(
                bool(re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", identifier)),
                "Could not export a PostgreSQL snapshot.",
            )
            yield identifier
        finally:
            session.stdin.close()
            try:
                session.wait(timeout=15)
            except subprocess.TimeoutExpired:
                session.terminate()
                session.wait()


class SnapshotDatabase(Database):
    def __init__(self, source: Database, snapshot: str):
        super().__init__(source.container, source.user, source.name)
        self.snapshot = snapshot

    def query(self, sql: str, phase: str) -> str:
        return super().query(
            "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
            f"SET TRANSACTION SNAPSHOT '{self.snapshot}';\n{sql};\nCOMMIT;",
            phase,
        )


def run_pipeline(source: list[str], target: list[str], output: BinaryIO | int, phase: str) -> None:
    with subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as producer:
        assert producer.stdout is not None
        try:
            result = subprocess.run(
                target, stdin=producer.stdout, stdout=output, stderr=subprocess.DEVNULL, check=False
            )
        finally:
            producer.stdout.close()
            if producer.poll() is None:
                try:
                    producer.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    producer.terminate()
        require(result.returncode == 0 and producer.wait() == 0, phase)


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_directory(root: Path, name: str) -> Path:
    directory = root / name
    if not directory.exists():
        directory.mkdir(mode=0o700)
    require(
        directory.is_dir() and not directory.is_symlink() and not directory.is_junction(),
        f"Unsafe backup directory: {directory}",
    )
    return directory


def verify_restore(
    postgres: Container,
    archive: Path,
    cipher: list[str],
    columns: dict[str, list[str]],
    baseline: dict[str, str],
    checksum: str,
    name: str,
) -> None:
    temporary = docker(
        "create",
        "--name",
        f"{name}-restore",
        "--network",
        "none",
        f"--env=POSTGRES_USER={CLONE_ROLE}",
        f"--env=POSTGRES_DB={CLONE_DB}",
        f"--env=POSTGRES_PASSWORD={secrets.token_hex(24)}",
        postgres.image_id,
        phase="Create network-disabled restore",
        report_output=True,
    )
    require(temporary != postgres.id, "Unexpected restore container ID.")
    clone = Database(Container(temporary, postgres.image_id), CLONE_ROLE, CLONE_DB)
    try:
        docker("start", temporary, phase="Start isolated restore")
        wait_for_postgres(clone)
        require(file_hash(archive) == checksum, "The encrypted archive changed.")
        run_pipeline(
            [*cipher, "-d", "-in", str(archive)],
            [
                "docker",
                "exec",
                "-i",
                temporary,
                "pg_restore",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--no-password",
                "--exit-on-error",
                "--single-transaction",
                f"--username={CLONE_ROLE}",
                f"--dbname={CLONE_DB}",
            ],
            subprocess.DEVNULL,
            "Decryption or isolated restore failed.",
        )
        require(clone.revision() == SOURCE_REVISION, "The restored Alembic revision changed.")
        require(source_columns(clone) == columns, "The restored table or column inventory changed.")
        restored = fingerprint(clone, columns)
        changed = sorted(table for table in baseline if baseline[table] != restored[table])
        require(not changed, f"The restored data changed in {', '.join(changed)}.")
        require(
            file_hash(archive) == checksum, "The encrypted archive changed during verification."
        )
    finally:
        docker("rm", "--force", "--volumes", temporary, phase="Remove isolated restore")


def backup(args: argparse.Namespace) -> dict[str, object]:
    openssl = find_openssl()
    backend = running_container(args.backend, args.project, "backend")
    postgres = running_container(args.postgres, args.project, "postgres")
    source = Database(
        postgres, read_env(postgres, "POSTGRES_USER"), read_env(postgres, "POSTGRES_DB")
    )
    require(backend.image_id == args.source_image_id, "The working backend image ID changed.")
    require(
        read_env(backend, "BUILD_SHA") == args.source_build_sha, "The working BUILD_SHA changed."
    )
    require(source.revision() == SOURCE_REVISION, "The working database revision changed.")

    home = Path(os.environ["LOCALAPPDATA"]) if os.name == "nt" else Path.home()
    archive_root = safe_directory(home, "iTeamBackups")
    key_root = safe_directory(home, "iTeamBackupKeys")
    timestamp = datetime.now(UTC)
    name = f"iteam-0028-{timestamp:%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    archive_dir = safe_directory(archive_root, name)
    key_dir = safe_directory(key_root, name)
    archive = archive_dir / "database.dump.enc"
    key = key_dir / "database.key"
    partial = archive_dir / "database.dump.enc.partial"
    proof = archive_dir / "restore-proof.json"

    with key.open("x", encoding="ascii") as stream:
        stream.write(secrets.token_urlsafe(48) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    cipher = [
        openssl,
        "enc",
        "-aes-256-cbc",
        "-salt",
        "-pbkdf2",
        "-iter",
        "200000",
        "-md",
        "sha256",
        "-pass",
        f"file:{key}",
    ]
    try:
        with exported_snapshot(source) as identifier:
            snapshot = SnapshotDatabase(source, identifier)
            require(snapshot.revision() == SOURCE_REVISION, "The exported source revision changed.")
            columns = source_columns(snapshot)
            require(bool(columns), "The exported database has no source tables.")
            baseline = fingerprint(snapshot, columns)
            with partial.open("xb") as output:
                run_pipeline(
                    [
                        "docker",
                        "exec",
                        "--env=PGOPTIONS=-c default_transaction_read_only=on",
                        postgres.id,
                        "pg_dump",
                        "--format=custom",
                        "--no-password",
                        "--lock-wait-timeout=5s",
                        f"--snapshot={identifier}",
                        f"--username={source.user}",
                        f"--dbname={source.name}",
                    ],
                    cipher,
                    output,
                    "Read-only pg_dump or encryption failed.",
                )
                output.flush()
                os.fsync(output.fileno())
        require(partial.stat().st_size > 0, "The encrypted archive is empty.")
        partial.rename(archive)
    finally:
        partial.unlink(missing_ok=True)
    checksum = file_hash(archive)
    print(f"Encrypted archive saved: {archive}", flush=True)
    verify_restore(postgres, archive, cipher, columns, baseline, checksum, name)

    result = {
        "status": "PASS",
        "created_at_utc": timestamp.isoformat(),
        "source_schema": SOURCE_REVISION,
        "source_image_id": backend.image_id,
        "source_build_sha": args.source_build_sha,
        "archive": str(archive),
        "archive_sha256": "sha256:" + checksum,
        "archive_bytes": archive.stat().st_size,
        "restore_network": "none",
        "preserved_source_tables": len(baseline),
        "proof": str(proof),
    }
    staged = proof.with_suffix(".json.partial")
    try:
        with staged.open("x", encoding="utf-8") as output:
            print(json.dumps(result, indent=2, sort_keys=True), file=output)
            output.flush()
            os.fsync(output.fileno())
        staged.rename(proof)
    finally:
        staged.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="iteam_windows_transfer")
    parser.add_argument("--backend", default="iteam_windows_transfer-backend-1")
    parser.add_argument("--postgres", default="iteam_windows_transfer-postgres-1")
    parser.add_argument("--source-build-sha", default="local-20261121-publication-history")
    parser.add_argument("--source-image-id", default=SOURCE_IMAGE_ID)
    args = parser.parse_args()
    try:
        print(json.dumps(backup(args), sort_keys=True), flush=True)
        return 0
    except (RehearsalError, OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Backup verification stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
