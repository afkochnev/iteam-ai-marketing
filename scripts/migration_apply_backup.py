"""Consistent, encrypted backup evidence for a pre-activation migration.

The source hash and pg_dump use the same exported repeatable-read snapshot.
Only a separate network-disabled PostgreSQL is restored. Business rows, keys,
database URLs and host paths never enter the returned proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import select
import stat
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import release_ops as ops
from release_backup import file_hash, private_file

SOURCE_SCHEMA = "20261009_0028"
EXCLUDED_TABLES = frozenset({"alembic_version", "workspaces", "workspace_memberships"})
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
_OPERATION = re.compile(r"[0-9a-f]{32}\Z")
_CONTAINER = re.compile(r"[0-9a-f]{64}\Z")
_SNAPSHOT = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{8}-[0-9]+\Z")
_BINDING_FIELDS = {
    "operation_id", "manifest_sha256", "database_identity_sha256",
    "source_release_sha", "source_schema_head", "source_backend_image",
}
_SNAPSHOT_FIELDS = {
    "build_sha", "schema", "database_identity_sha256", "business_state", "table_counts",
}
ERROR_CODES = frozenset({
    "INVALID_MIGRATION_BACKUP_BINDING", "INVALID_MIGRATION_DATA_SNAPSHOT",
    "INVALID_MIGRATION_BACKUP_PROOF", "MIGRATION_BACKUP_SOURCE_MISMATCH",
    "MIGRATION_BACKUP_DATABASE_CHANGED", "MIGRATION_BACKUP_DIRECTORIES_UNSAFE",
    "MIGRATION_BACKUP_OUTPUT_EXISTS", "MIGRATION_BACKUP_EXPORT_FAILED",
    "MIGRATION_BACKUP_PIPELINE_FAILED", "MIGRATION_BACKUP_PROCESS_CLEANUP_FAILED",
    "MIGRATION_BACKUP_RESTORE_START_TIMEOUT", "MIGRATION_BACKUP_RESTORE_MISMATCH",
    "MIGRATION_BACKUP_CLEANUP_FAILED", "MIGRATION_BACKUP_CIPHERTEXT_CHANGED",
    "MIGRATION_BACKUP_CONTAINER_EXISTS",
})

# This payload executes inside the exact signed backend image. The algorithm is
# identical to migration_release_drill.canonical_business_state; no rows leave
# the image. The same code is used for the source snapshot and restored clone.
_SNAPSHOT_BODY = r'''
import asyncio,hashlib,json,os,re,sys
from sqlalchemy import text
from app.core.database import engine
from app.core.config import settings
EXCLUDED={'alembic_version','workspaces','workspace_memberships'}
async def inspect(connection):
 names=(await connection.execute(text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname='public' ORDER BY tablename"))).scalars().all()
 assert names and all(re.fullmatch(r'[a-z_][a-z0-9_]{0,62}',n) for n in names)
 legacy=sorted(set(names)-EXCLUDED)
 assert legacy
 if len(sys.argv)>1:
  requested=json.loads(sys.argv[1])
  assert type(requested) is list and len(requested)==len(set(requested))
  assert all(type(n) is str for n in requested) and sorted(requested)==legacy
 counts={}
 tables={}
 for name in names:
  quoted='"'+name.replace('"','""')+'"'
  counts[name]=await connection.scalar(text('SELECT count(*) FROM public.'+quoted))
  if name in legacy:
   rows=(await connection.execute(text('SELECT to_jsonb(t)::text FROM public.'+quoted+' AS t'))).scalars().all()
   assert len(rows)==counts[name]
   tables[name]=sorted(rows)
 schema=(await connection.execute(text('SELECT version_num FROM public.alembic_version'))).scalars().all()
 assert len(schema)==1
 identity={
  'system_identifier':str(await connection.scalar(text('SELECT system_identifier::text FROM pg_catalog.pg_control_system()'))),
  'database_oid':int(await connection.scalar(text('SELECT oid::bigint FROM pg_catalog.pg_database WHERE datname=current_database()'))),
  'database_name':str(await connection.scalar(text('SELECT current_database()'))),
 }
 def hashed(value):
  return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()
 return {
  'build_sha':settings.build_sha,
  'schema':schema[0],
  'database_identity_sha256':hashed(identity),
  'business_state':{'sha256':hashed(tables),'table_counts':{n:counts[n] for n in legacy}},
  'table_counts':counts,
 }
async def main():
 async with engine.connect() as connection:
  connection=await connection.execution_options(isolation_level='REPEATABLE READ')
  async with connection.begin():
   await connection.execute(text('SET TRANSACTION READ ONLY'))
   await connection.execute(text("SET LOCAL statement_timeout='60s'"))
   snapshot=await connection.scalar(text('SELECT pg_export_snapshot()')) if HOLD else None
   result=await asyncio.wait_for(inspect(connection),timeout=60)
   if HOLD: result['snapshot']=snapshot
   print(json.dumps(result,sort_keys=True),flush=True)
   if HOLD:
    loop=asyncio.get_running_loop()
    released=loop.create_future()
    def readable():
     if not released.done():
      os.read(sys.stdin.fileno(),1)
      released.set_result(None)
    loop.add_reader(sys.stdin.fileno(),readable)
    try: await asyncio.wait_for(released,timeout=720)
    finally: loop.remove_reader(sys.stdin.fileno())
 await engine.dispose()
asyncio.run(main())
'''
SNAPSHOT_CODE = "HOLD=False\n" + _SNAPSHOT_BODY
EXPORT_CODE = "HOLD=True\n" + _SNAPSHOT_BODY


def _hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def database_identity_hash(identity: Any) -> str:
    """Hash the safe fields returned by source and PostgreSQL identity probes."""
    ops.require(
        type(identity) is dict
        and set(identity) == {"system_identifier", "database_oid", "database_name"}
        and type(identity["system_identifier"]) is str
        and re.fullmatch(r"[0-9]{1,20}", identity["system_identifier"])
        and type(identity["database_oid"]) is int and identity["database_oid"] > 0
        and type(identity["database_name"]) is str
        and _IDENTIFIER.fullmatch(identity["database_name"]),
        "INVALID_MIGRATION_DATA_SNAPSHOT",
    )
    return _hash(identity)


def validate_binding(binding: Any) -> None:
    ops.require(
        type(binding) is dict and set(binding) == _BINDING_FIELDS
        and type(binding["operation_id"]) is str and _OPERATION.fullmatch(binding["operation_id"])
        and type(binding["source_release_sha"]) is str and _SHA.fullmatch(binding["source_release_sha"])
        and binding["source_schema_head"] == SOURCE_SCHEMA
        and all(type(binding[key]) is str and _DIGEST.fullmatch(binding[key])
                for key in ("manifest_sha256", "database_identity_sha256"))
        and type(binding["source_backend_image"]) is str
        and binding["source_backend_image"].startswith(f"ghcr.io/{ops.REPO}-backend@")
        and _DIGEST.fullmatch(binding["source_backend_image"].split("@", 1)[1]),
        "INVALID_MIGRATION_BACKUP_BINDING",
    )


def _counts(value: Any) -> bool:
    return (
        type(value) is dict and bool(value)
        and all(type(name) is str and _IDENTIFIER.fullmatch(name)
                and type(count) is int and count >= 0 for name, count in value.items())
    )


def validate_business_state(value: Any) -> None:
    ops.require(
        type(value) is dict and set(value) == {"sha256", "table_counts"}
        and type(value["sha256"]) is str and _DIGEST.fullmatch(value["sha256"])
        and _counts(value["table_counts"])
        and not (set(value["table_counts"]) & EXCLUDED_TABLES),
        "INVALID_MIGRATION_DATA_SNAPSHOT",
    )


def validate_snapshot(value: Any, *, exported: bool = False) -> None:
    fields = _SNAPSHOT_FIELDS | ({"snapshot"} if exported else set())
    ops.require(
        type(value) is dict and set(value) == fields
        and type(value["build_sha"]) is str and _SHA.fullmatch(value["build_sha"])
        and type(value["schema"]) is str and re.fullmatch(r"[A-Za-z0-9_]{1,64}", value["schema"])
        and type(value["database_identity_sha256"]) is str
        and _DIGEST.fullmatch(value["database_identity_sha256"])
        and _counts(value["table_counts"])
        and value["table_counts"].get("alembic_version") == 1,
        "INVALID_MIGRATION_DATA_SNAPSHOT",
    )
    validate_business_state(value["business_state"])
    ops.require(
        value["business_state"]["table_counts"] == {
            name: count for name, count in value["table_counts"].items() if name not in EXCLUDED_TABLES
        },
        "INVALID_MIGRATION_DATA_SNAPSHOT",
    )
    if exported:
        ops.require(
            type(value["snapshot"]) is str and _SNAPSHOT.fullmatch(value["snapshot"]),
            "INVALID_MIGRATION_DATA_SNAPSHOT",
        )


def _private_directory(path: Path, *, create: bool = True) -> None:
    ops.require(
        path.is_absolute() and path.resolve() == path and not path.is_symlink(),
        "MIGRATION_BACKUP_DIRECTORIES_UNSAFE",
    )
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    ops.require(
        path.is_dir() and path.stat().st_uid == os.getuid()
        and stat.S_IMODE(path.stat().st_mode) == 0o700,
        "MIGRATION_BACKUP_DIRECTORIES_UNSAFE",
    )


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_proof(path: Path, proof: dict[str, Any]) -> None:
    """Publish complete bytes atomically without replacing a concurrent file."""
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(proof, sort_keys=True, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            raise ops.ReleaseError("MIGRATION_BACKUP_OUTPUT_EXISTS") from None
        _fsync_directory(path.parent)
    finally:
        os.unlink(temporary)


def _read_export(process: subprocess.Popen, *, timeout: float = 60) -> dict[str, Any]:
    """Bound both waiting and bytes; readline alone could hang before newline."""
    deadline = time.monotonic() + timeout
    data = bytearray()
    fd = process.stdout.fileno()
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        ops.require(remaining > 0, "MIGRATION_BACKUP_EXPORT_FAILED")
        ready, _, _ = select.select([fd], [], [], remaining)
        ops.require(ready, "MIGRATION_BACKUP_EXPORT_FAILED")
        chunk = os.read(fd, 65536)
        ops.require(chunk and len(data) + len(chunk) <= 1024 * 1024, "MIGRATION_BACKUP_EXPORT_FAILED")
        data.extend(chunk)
    line, tail = bytes(data).split(b"\n", 1)
    ops.require(not tail, "MIGRATION_BACKUP_EXPORT_FAILED")
    from migration_release_approval import parse_json

    value = parse_json(line)
    validate_snapshot(value, exported=True)
    return value


def _reap(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    try:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise ops.ReleaseError("MIGRATION_BACKUP_PROCESS_CLEANUP_FAILED") from None
    finally:
        for attribute in ("stdin", "stdout", "stderr"):
            stream = getattr(process, attribute, None)
            if stream is not None and not stream.closed:
                stream.close()


def _pipeline(source: list[str], target: list[str], *, cwd: Path | None = None,
              output: Any = subprocess.DEVNULL) -> None:
    producer = consumer = None
    try:
        producer = subprocess.Popen(source, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        consumer = subprocess.Popen(target, cwd=cwd, stdin=producer.stdout, stdout=output,
                                    stderr=subprocess.DEVNULL)
        producer.stdout.close()
        result = consumer.wait(timeout=600)
        source_result = producer.wait(timeout=30)
        ops.require(result == source_result == 0, "MIGRATION_BACKUP_PIPELINE_FAILED")
    except (OSError, subprocess.SubprocessError):
        raise ops.ReleaseError("MIGRATION_BACKUP_PIPELINE_FAILED") from None
    finally:
        failures = []
        for process in (consumer, producer):
            try:
                _reap(process)
            except ops.ReleaseError as error:
                failures.append(error)
        if failures:
            raise ops.ReleaseError("MIGRATION_BACKUP_PROCESS_CLEANUP_FAILED") from None


def _remove_helper(name: str, operation_id: str) -> None:
    """Remove only our labelled temporary container, including anonymous data."""
    ids = ops.command(["docker", "ps", "--all", "--quiet", "--no-trunc", "--filter", f"name=^/{name}$"]).splitlines()
    if not ids:
        return
    ops.require(len(ids) == 1 and _CONTAINER.fullmatch(ids[0]), "MIGRATION_BACKUP_CLEANUP_FAILED")
    value = json.loads(ops.command(["docker", "inspect", ids[0]]))[0]
    labels = value["Config"].get("Labels") or {}
    ops.require(
        labels.get("iteam.migration.operation") == operation_id
        and labels.get("iteam.migration.purpose") in {"backup-restore", "backup-inspection"},
        "MIGRATION_BACKUP_CLEANUP_FAILED",
    )
    volumes = [item["Name"] for item in value.get("Mounts", []) if item["Type"] == "volume"]
    ops.command(["docker", "rm", "--force", "--volumes", ids[0]])
    ops.require(
        not ops.command(["docker", "ps", "--all", "--quiet", "--filter", f"id={ids[0]}"]),
        "MIGRATION_BACKUP_CLEANUP_FAILED",
    )
    for volume in volumes:
        ops.require(
            _CONTAINER.fullmatch(volume)
            and not ops.command(["docker", "volume", "ls", "--quiet", "--filter", f"name=^{volume}$"]),
            "MIGRATION_BACKUP_CLEANUP_FAILED",
        )


def validate_proof(proof: Any, binding: dict[str, Any], *, now: datetime | None = None) -> None:
    validate_binding(binding)
    fields = _BINDING_FIELDS | {
        "proof_version", "status", "created_at", "encrypted_backup_sha256",
        "source_business_state", "restored_business_state", "table_counts_match",
        "isolated_restore_network", "restore_container_removed",
    }
    ops.require(
        type(proof) is dict and set(proof) == fields
        and type(proof["proof_version"]) is int and proof["proof_version"] == 1
        and proof["status"] == "PASS"
        and all(proof[key] == value for key, value in binding.items())
        and type(proof["encrypted_backup_sha256"]) is str
        and _DIGEST.fullmatch(proof["encrypted_backup_sha256"])
        and proof["table_counts_match"] is True
        and proof["isolated_restore_network"] == "none"
        and proof["restore_container_removed"] is True,
        "INVALID_MIGRATION_BACKUP_PROOF",
    )
    validate_business_state(proof["source_business_state"])
    validate_business_state(proof["restored_business_state"])
    ops.require(proof["source_business_state"] == proof["restored_business_state"], "INVALID_MIGRATION_BACKUP_PROOF")
    try:
        created = datetime.fromisoformat(proof["created_at"])
        ops.require(created.utcoffset() is not None, "INVALID_MIGRATION_BACKUP_PROOF")
        age = ((now or datetime.now(UTC)) - created).total_seconds()
        ops.require(0 <= age <= 3600, "INVALID_MIGRATION_BACKUP_PROOF")
    except (ValueError, TypeError, OverflowError):
        raise ops.ReleaseError("INVALID_MIGRATION_BACKUP_PROOF") from None


def backup_restore(runtime: ops.Runtime, directory: Path, key_directory: Path,
                   proof_path: Path, binding: dict[str, Any]) -> dict[str, Any]:
    """Retain encrypted backup/key separately, publish proof after clone cleanup."""
    validate_binding(binding)
    for path in (directory, key_directory):
        _private_directory(path)
    _private_directory(proof_path.parent, create=False)
    ops.require(
        directory != key_directory and directory not in key_directory.parents
        and key_directory not in directory.parents,
        "MIGRATION_BACKUP_DIRECTORIES_UNSAFE",
    )
    ops.require(
        proof_path.is_absolute() and proof_path.resolve() == proof_path
        and not proof_path.exists() and not proof_path.is_symlink(),
        "MIGRATION_BACKUP_OUTPUT_EXISTS",
    )
    if ops.env_value(runtime.env_path.read_text(), "APP_ENV").strip("\"'") in {"production", "prod"}:
        ops.require(
            all(path != runtime.root and runtime.root not in path.parents
                for path in (directory, key_directory, proof_path)),
            "MIGRATION_BACKUP_DIRECTORIES_UNSAFE",
        )
    name = "migration-" + binding["operation_id"]
    backup, key = directory / (name + ".dump.enc"), key_directory / (name + ".key")
    restore_name, helper_name = "iteam-" + name + "-restore", "iteam-" + name + "-inspect"
    for path in (backup, key):
        ops.require(not path.exists() and not path.is_symlink(), "MIGRATION_BACKUP_OUTPUT_EXISTS")
    for container_name in (restore_name, helper_name):
        ops.require(
            not ops.command(["docker", "ps", "--all", "--quiet", "--filter", f"name=^/{container_name}$"]),
            "MIGRATION_BACKUP_CONTAINER_EXISTS",
        )
    database = runtime.inspect("postgres")
    ops.require(
        type(database.get("Id")) is str and _CONTAINER.fullmatch(database["Id"])
        and type(database.get("Image")) is str and _DIGEST.fullmatch(database["Image"]),
        "MIGRATION_BACKUP_DATABASE_CHANGED",
    )
    environment = dict(item.split("=", 1) for item in database["Config"]["Env"] if "=" in item)
    user, dbname = environment.get("POSTGRES_USER"), environment.get("POSTGRES_DB")
    ops.require(
        type(user) is str and _IDENTIFIER.fullmatch(user)
        and type(dbname) is str and _IDENTIFIER.fullmatch(dbname),
        "MIGRATION_BACKUP_DATABASE_CHANGED",
    )
    exporter = None
    created = False
    proof = None
    try:
        exporter = subprocess.Popen(
            runtime.base + ["exec", "-T", "backend", "python", "-c", EXPORT_CODE],
            cwd=runtime.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        exported = _read_export(exporter)
        ops.require(
            exported["build_sha"] == binding["source_release_sha"]
            and exported["schema"] == binding["source_schema_head"]
            and exported["database_identity_sha256"] == binding["database_identity_sha256"],
            "MIGRATION_BACKUP_SOURCE_MISMATCH",
        )
        with private_file(key) as stream:
            stream.write(secrets.token_urlsafe(48) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(key_directory)
        with private_file(backup, binary=True) as stream:
            _pipeline(
                runtime.base + ["exec", "-T", "postgres", "pg_dump", "-U", user, "-d", dbname,
                                "-Fc", "--snapshot=" + exported["snapshot"]],
                ["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000", "-pass", "file:" + str(key)],
                cwd=runtime.root, output=stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(directory)
        checksum = file_hash(backup)
        ops.require(backup.stat().st_size > 0, "MIGRATION_BACKUP_PIPELINE_FAILED")
        exporter.stdin.close()
        ops.require(exporter.wait(timeout=30) == 0, "MIGRATION_BACKUP_EXPORT_FAILED")
        created = True  # Also clean up a successful daemon create with a lost CLI reply.
        restore_id = ops.command([
            "docker", "run", "-d", "--name", restore_name,
            "--label", "iteam.migration.operation=" + binding["operation_id"],
            "--label", "iteam.migration.purpose=backup-restore",
            "--restart", "no", "--pull", "never", "--network", "none",
            "-e", "POSTGRES_USER=restore", "-e", "POSTGRES_DB=restore_test",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust", database["Image"],
        ])
        ops.require(_CONTAINER.fullmatch(restore_id), "MIGRATION_BACKUP_RESTORE_START_TIMEOUT")
        for _ in range(60):
            try:
                ops.command([
                    "docker", "exec", restore_id, "sh", "-c",
                    'test "$(cat /proc/1/comm)" = postgres && pg_isready -U restore -d restore_test',
                ])
                break
            except ops.ReleaseError:
                time.sleep(1)
        else:
            raise ops.ReleaseError("MIGRATION_BACKUP_RESTORE_START_TIMEOUT")
        ops.require(file_hash(backup) == checksum, "MIGRATION_BACKUP_CIPHERTEXT_CHANGED")
        _pipeline(
            ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-pass", "file:" + str(key), "-in", str(backup)],
            ["docker", "exec", "-i", restore_id, "pg_restore", "-U", "restore", "-d", "restore_test",
             "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges"],
        )
        from migration_release_approval import parse_json

        restored = parse_json(ops.command([
            "docker", "run", "--rm", "--name", helper_name,
            "--label", "iteam.migration.operation=" + binding["operation_id"],
            "--label", "iteam.migration.purpose=backup-inspection",
            "--restart", "no", "--pull", "never", "--network", "container:" + restore_id,
            "--read-only", "--entrypoint", "python",
            "-e", "APP_ENV=maintenance", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "DATABASE_URL=postgresql+asyncpg://restore@127.0.0.1:5432/restore_test",
            binding["source_backend_image"], "-c", SNAPSHOT_CODE,
            json.dumps(sorted(exported["business_state"]["table_counts"])),
        ]).encode())
        validate_snapshot(restored)
        ops.require(
            restored["schema"] == exported["schema"]
            and restored["build_sha"] == binding["source_release_sha"]
            and restored["table_counts"] == exported["table_counts"]
            and restored["business_state"] == exported["business_state"],
            "MIGRATION_BACKUP_RESTORE_MISMATCH",
        )
        current = runtime.inspect("postgres")
        ops.require(current["Id"] == database["Id"] and current["Image"] == database["Image"], "MIGRATION_BACKUP_DATABASE_CHANGED")
        ops.require(file_hash(backup) == checksum, "MIGRATION_BACKUP_CIPHERTEXT_CHANGED")
        proof = {
            **binding, "proof_version": 1, "status": "PASS",
            "created_at": datetime.now(UTC).isoformat(),
            "encrypted_backup_sha256": "sha256:" + checksum,
            "source_business_state": exported["business_state"],
            "restored_business_state": restored["business_state"],
            "table_counts_match": True, "isolated_restore_network": "none",
            "restore_container_removed": True,
        }
    except (OSError, subprocess.SubprocessError):
        raise ops.ReleaseError("MIGRATION_BACKUP_PIPELINE_FAILED") from None
    finally:
        failures = []
        if exporter is not None:
            try:
                if exporter.stdin is not None and not exporter.stdin.closed:
                    exporter.stdin.close()
                if exporter.poll() is None:
                    try:
                        exporter.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                _reap(exporter)
            except (OSError, ops.ReleaseError):
                failures.append("process")
        if created:
            for container_name in (helper_name, restore_name):
                try:
                    _remove_helper(container_name, binding["operation_id"])
                except (ops.ReleaseError, OSError, ValueError, KeyError, TypeError):
                    failures.append("container")
        if failures:
            raise ops.ReleaseError("MIGRATION_BACKUP_CLEANUP_FAILED") from None
    validate_proof(proof, binding)
    _publish_proof(proof_path, proof)
    return proof
