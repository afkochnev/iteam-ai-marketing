"""Encrypted backup and isolated, network-disabled restore; no plaintext dump on disk."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from release_ops import Runtime, SCHEMA, ReleaseError, command, load, require, save

# A repeatable-read snapshot stays open until the host closes stdin.
EXPORT = """
import asyncio,json,sys
from sqlalchemy import text
from app.core.database import engine
async def main():
 async with engine.connect() as c:
  c=await c.execution_options(isolation_level='REPEATABLE READ')
  async with c.begin():
   await c.execute(text('SET TRANSACTION READ ONLY'))
   snapshot=await c.scalar(text('SELECT pg_export_snapshot()'))
   names=(await c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars().all()
   counts={name:await c.scalar(text('SELECT count(*) FROM "'+name.replace('"','""')+'"')) for name in names}
   print(json.dumps({'snapshot':snapshot,'counts':counts}),flush=True)
   await asyncio.to_thread(sys.stdin.readline)
 await engine.dispose()
asyncio.run(main())
"""


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def check_proof(path: Path, source_sha: str) -> dict:
    require(
        not path.is_symlink()
        and path.stat().st_uid == os.getuid()
        and stat.S_IMODE(path.stat().st_mode) == 0o600,
        "UNSAFE_BACKUP_PROOF",
    )
    proof = load(path)
    age = (
        datetime.now(UTC) - datetime.fromisoformat(proof["created_at"])
    ).total_seconds()
    require(
        proof["status"] == "PASS"
        and proof["source_release"] == source_sha
        and proof["alembic_head"] == SCHEMA
        and 0 <= age <= 3600
        and proof["isolated_network"] == "none"
        and proof["table_counts_match"] is True,
        "BACKUP_RESTORE_GATE_FAILED",
    )
    backup = Path(proof["backup"])
    require(
        backup.is_absolute()
        and not backup.is_symlink()
        and backup.stat().st_uid == os.getuid()
        and stat.S_IMODE(backup.stat().st_mode) == 0o600
        and backup.is_file()
        and file_hash(backup) == proof["checksum"],
        "BACKUP_FILE_MISSING_OR_CHANGED",
    )
    key = Path(proof["key_path"])
    require(
        key.is_absolute()
        and not key.is_symlink()
        and key.is_file()
        and key.stat().st_uid == os.getuid()
        and stat.S_IMODE(key.stat().st_mode) == 0o600,
        "BACKUP_KEY_UNAVAILABLE",
    )
    return proof


def backup_restore(
    runtime: Runtime, directory: Path, key_directory: Path, proof_path: Path
) -> dict:
    for path in (directory, key_directory):
        require(path.is_absolute() and not path.is_symlink(), "UNSAFE_BACKUP_DIRECTORY")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(
            path.stat().st_uid == os.getuid()
            and stat.S_IMODE(path.stat().st_mode) == 0o700,
            "UNSAFE_BACKUP_DIRECTORY_MODE",
        )
    require(
        directory.resolve() != key_directory.resolve()
        and directory.resolve() not in key_directory.resolve().parents
        and key_directory.resolve() not in directory.resolve().parents,
        "BACKUP_KEYS_MUST_BE_SEPARATE",
    )
    from release_ops import env_value

    if env_value(runtime.env_path.read_text(), "APP_ENV") in {"production", "prod"}:
        require(
            all(
                path.resolve() != runtime.root
                and runtime.root not in path.resolve().parents
                for path in (directory, key_directory)
            ),
            "BACKUP_OUTSIDE_CHECKOUT_REQUIRED",
        )
    baseline = runtime.snapshot()
    name = (
        "iteam-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(4)
    )
    backup = directory / (name + ".dump.enc")
    key = key_directory / (name + ".key")
    with key.open("x", opener=lambda p, flags: os.open(p, flags, 0o600)) as stream:
        stream.write(secrets.token_urlsafe(48) + "\n")
    exporter = None
    restore_name = name + "-restore"
    created = False
    try:
        exporter = subprocess.Popen(
            runtime.base + ["exec", "-T", "backend", "python", "-c", EXPORT],
            cwd=runtime.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        exported_line = exporter.stdout.readline()
        require(exported_line, "SNAPSHOT_EXPORT_FAILED")
        exported = json.loads(exported_line)
        require(exported["counts"] and exported["snapshot"], "EMPTY_BACKUP_SNAPSHOT")
        database = runtime.inspect("postgres")
        env = dict(
            line.split("=", 1) for line in database["Config"]["Env"] if "=" in line
        )
        user, dbname = env["POSTGRES_USER"], env["POSTGRES_DB"]
        # Both subprocesses are argv-only; no shell expansion, plaintext file or token output.
        with backup.open(
            "xb", opener=lambda p, flags: os.open(p, flags, 0o600)
        ) as output:
            dump = subprocess.Popen(
                runtime.base
                + [
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    user,
                    "-d",
                    dbname,
                    "-Fc",
                    "--snapshot=" + exported["snapshot"],
                ],
                cwd=runtime.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            encryption = subprocess.Popen(
                [
                    "openssl",
                    "enc",
                    "-aes-256-cbc",
                    "-salt",
                    "-pbkdf2",
                    "-iter",
                    "200000",
                    "-pass",
                    "file:" + str(key),
                ],
                stdin=dump.stdout,
                stdout=output,
                stderr=subprocess.DEVNULL,
            )
            dump.stdout.close()
            encrypted_status, dump_status = encryption.wait(), dump.wait()
            require(encrypted_status == dump_status == 0, "ENCRYPTED_DUMP_FAILED")
            output.flush()
            os.fsync(output.fileno())
        exporter.stdin.close()
        require(exporter.wait(timeout=30) == 0, "SNAPSHOT_RELEASE_FAILED")
        command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                restore_name,
                "--network",
                "none",
                "-e",
                "POSTGRES_USER=restore",
                "-e",
                "POSTGRES_DB=restore_test",
                "-e",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                database["Image"],
            ]
        )
        created = True
        for _ in range(60):
            try:
                command(
                    [
                        "docker",
                        "exec",
                        restore_name,
                        "sh",
                        "-c",
                        'test "$(cat /proc/1/comm)" = postgres && pg_isready -U restore -d restore_test',
                    ]
                )
                break
            except ReleaseError:
                time.sleep(1)
        else:
            raise ReleaseError("ISOLATED_RESTORE_START_TIMEOUT")
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
            [
                "docker",
                "exec",
                "-i",
                restore_name,
                "pg_restore",
                "-U",
                "restore",
                "-d",
                "restore_test",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
            ],
            stdin=decrypted.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        decrypted.stdout.close()
        restore_status, decrypt_status = restore.wait(), decrypted.wait()
        require(restore_status == decrypt_status == 0, "ISOLATED_RESTORE_FAILED")
        pieces = [
            "SELECT '"
            + name.replace("'", "''")
            + "' AS name,count(*) AS n FROM \""
            + name.replace('"', '""')
            + '"'
            for name in exported["counts"]
        ]
        sql = (
            "SELECT json_object_agg(name,n) FROM ("
            + " UNION ALL ".join(pieces)
            + ") snapshot"
        )
        restored = json.loads(
            command(
                [
                    "docker",
                    "exec",
                    restore_name,
                    "psql",
                    "-U",
                    "restore",
                    "-d",
                    "restore_test",
                    "-At",
                    "-c",
                    sql,
                ]
            )
        )
        require(restored == exported["counts"], "RESTORED_ROW_COUNTS_DIFFER")
        proof = {
            "status": "PASS",
            "created_at": datetime.now(UTC).isoformat(),
            "backup": str(backup),
            "key_path": str(key),
            "checksum": file_hash(backup),
            "source_release": baseline["build_sha"],
            "alembic_head": SCHEMA,
            "isolated_network": "none",
            "table_counts_match": True,
            "tables": len(restored),
        }
        save(proof_path, proof)
        return proof
    finally:
        if exporter is not None and exporter.poll() is None:
            exporter.stdin.close()
            try:
                exporter.wait(timeout=30)
            except subprocess.TimeoutExpired:
                exporter.terminate()
                exporter.wait(timeout=10)
        if created:
            command(["docker", "rm", "-f", "-v", restore_name])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--key-dir", type=Path, required=True)
    parser.add_argument("--proof", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = backup_restore(
            Runtime(args.root), args.backup_dir, args.key_dir, args.proof
        )
        print(json.dumps(result))
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        print(json.dumps({"status": "FAIL", "automatic_retry": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
