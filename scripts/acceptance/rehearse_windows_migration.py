#!/usr/bin/env python3
"""Stream a read-only source snapshot into a disposable Docker migration rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from uuid import uuid4

SOURCE_REVISION = "20261009_0028"
TARGET_REVISION = "20261126_0039"
TARGET_SHA = "62cd51fa744ffbc25ad5471ce5cdab6570b14eda"
TARGET_IMAGE = (
    "ghcr.io/afkochnev/iteam-ai-marketing-backend@"
    "sha256:007b28d6da66caf9276c97e853687579172c7c66af0f6025a491fe64bc7eafc8"
)
SOURCE_IMAGE_ID = "sha256:a954a0f7be0e032eaabf93b2dc769cf4aafc5557d9828bf2215a1df65b4dfc4c"
CLONE_ROLE = "iteam_rehearsal"
CLONE_DB = "iteam_rehearsal"
REVISION_SQL = "SELECT version_num FROM public.alembic_version ORDER BY version_num"
CONTAINER_FIELDS = (
    "{{.Id}}|{{.Image}}|{{.State.Running}}|"
    '{{index .Config.Labels "com.docker.compose.project"}}|'
    '{{index .Config.Labels "com.docker.compose.service"}}'
)


class RehearsalError(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RehearsalError(message)


def docker(*args: str, phase: str, report_output: bool = False) -> str:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, encoding="utf-8", errors="replace", check=False
        )
    except FileNotFoundError as exc:
        raise RehearsalError("Docker CLI was not found.") from exc
    require(result.returncode == 0, f"{phase}: Docker exited with code {result.returncode}.")
    if report_output:
        return result.stdout.strip()
    return ""


@dataclass(frozen=True)
class Container:
    id: str
    image_id: str


def running_container(name: str, project: str, service: str) -> Container:
    value = docker(
        "inspect",
        "--type=container",
        "--format",
        CONTAINER_FIELDS,
        name,
        phase=f"Inspect {service}",
        report_output=True,
    )
    fields = value.split("|")
    require(len(fields) == 5, f"Incomplete {service} container metadata.")
    container_id, image_id, running, actual_project, actual_service = fields
    require(running == "true", f"The source {service} container is stopped.")
    require(
        actual_project == project and actual_service == service,
        f"The source {service} container has unexpected Compose labels.",
    )
    return Container(container_id, image_id)


def read_env(container: Container, key: str) -> str:
    value = docker("exec", container.id, "printenv", key, phase=f"Read {key}", report_output=True)
    require(bool(value), f"The source container has an empty {key}.")
    return value


@dataclass(frozen=True)
class Database:
    container: Container
    user: str
    name: str

    def query(self, sql: str, phase: str) -> str:
        return docker(
            "exec",
            "--env=PGOPTIONS=-c default_transaction_read_only=on",
            self.container.id,
            "psql",
            "-XqAt",
            "--no-password",
            "--set=ON_ERROR_STOP=1",
            f"--username={self.user}",
            f"--dbname={self.name}",
            "--command",
            sql,
            phase=phase,
            report_output=True,
        )

    def revision(self) -> str:
        return self.query(REVISION_SQL, "Read Alembic revision")


def stream_snapshot(source: Database, clone: Database) -> None:
    dump = [
        "docker",
        "exec",
        "--env=PGOPTIONS=-c default_transaction_read_only=on",
        source.container.id,
        "pg_dump",
        "--format=custom",
        "--no-password",
        "--lock-wait-timeout=5s",
        f"--username={source.user}",
        f"--dbname={source.name}",
    ]
    restore = [
        "docker",
        "exec",
        "-i",
        clone.container.id,
        "pg_restore",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        "--exit-on-error",
        "--single-transaction",
        f"--username={clone.user}",
        f"--dbname={clone.name}",
    ]
    with subprocess.Popen(dump, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as producer:
        assert producer.stdout is not None
        try:
            consumer = subprocess.run(
                restore,
                stdin=producer.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        finally:
            producer.stdout.close()
            if producer.poll() is None:
                try:
                    producer.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    producer.terminate()
        require(
            consumer.returncode == 0 and producer.wait() == 0,
            "Read-only pg_dump or isolated pg_restore failed.",
        )


def identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def source_columns(database: Database) -> dict[str, list[str]]:
    raw = database.query(
        """
        SELECT coalesce(json_object_agg(table_name, columns), '{}'::json)
        FROM (
          SELECT c.table_name, json_agg(c.column_name ORDER BY c.ordinal_position) AS columns
          FROM information_schema.columns AS c
          JOIN information_schema.tables AS t
            ON (c.table_schema, c.table_name) = (t.table_schema, t.table_name)
          WHERE c.table_schema='public' AND c.table_name <> 'alembic_version'
            AND t.table_type='BASE TABLE'
          GROUP BY c.table_name
        ) AS source_tables
        """,
        "Read source table and column inventory",
    )
    return json.loads(raw)


def fingerprint(database: Database, columns: dict[str, list[str]]) -> dict[str, str]:
    snapshots = {}
    for table, fields in sorted(columns.items()):
        select = ", ".join(identifier(field) for field in fields)
        snapshots[table] = database.query(
            f"""
            SELECT count(*) || ':' ||
                   encode(sha256(convert_to(coalesce(string_agg(digest, '' ORDER BY digest), ''),
                                            'UTF8')), 'hex')
            FROM (
              SELECT encode(sha256(convert_to(to_jsonb(source_row)::text, 'UTF8')), 'hex') AS digest
              FROM (SELECT {select} FROM public.{identifier(table)}) AS source_row
            ) AS digests
            """,
            f"Fingerprint {table}",
        )
    return snapshots


def compare(
    database: Database,
    columns: dict[str, list[str]],
    baseline: dict[str, str],
    revision: str,
    phase: str,
) -> None:
    require(database.revision() == revision, f"{phase}: Alembic revision differs from {revision}.")
    actual = fingerprint(database, columns)
    changed = [table for table, original in baseline.items() if actual.get(table) != original]
    require(not changed, f"{phase}: original rows changed in {', '.join(changed)}.")


def run_alembic(image: str, network: str, url: str, *args: str, phase: str) -> str:
    return docker(
        "run",
        "--rm",
        "--network",
        network,
        "--env=APP_ENV=development",
        f"--env=DATABASE_URL={url}",
        "--entrypoint=alembic",
        image,
        *args,
        phase=phase,
        report_output=True,
    )


def wait_for_postgres(clone: Database) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        probe = subprocess.run(
            [
                "docker",
                "exec",
                clone.container.id,
                "pg_isready",
                "--host=127.0.0.1",
                f"--username={clone.user}",
                f"--dbname={clone.name}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return
        time.sleep(1)
    raise RehearsalError("The disposable PostgreSQL instance did not start.")


def rehearse(args: argparse.Namespace) -> dict[str, object]:
    require(
        bool(re.fullmatch(r"sha256:[a-f0-9]{64}", args.source_image_id)),
        "Expected an exact SHA-256 Docker image ID.",
    )
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
    engine = docker(
        "info", "--format", "{{.OSType}}", phase="Check Docker engine", report_output=True
    )
    require(engine == "linux", "The rehearsal requires the Linux Docker engine.")
    docker("pull", TARGET_IMAGE, phase="Pull the verified target backend by digest")

    prefix = f"iteam-m39-rehearsal-{uuid4().hex[:12]}"
    network = None
    temporary = None
    try:
        network = docker(
            "network",
            "create",
            "--internal",
            f"{prefix}-net",
            phase="Create internal rehearsal network",
            report_output=True,
        )
        password = secrets.token_hex(24)
        temporary = docker(
            "create",
            "--name",
            f"{prefix}-postgres",
            "--network",
            network,
            "--network-alias=postgres",
            f"--env=POSTGRES_USER={CLONE_ROLE}",
            f"--env=POSTGRES_PASSWORD={password}",
            f"--env=POSTGRES_DB={CLONE_DB}",
            postgres.image_id,
            phase="Create disposable PostgreSQL",
            report_output=True,
        )
        require(
            temporary not in {backend.id, postgres.id}, "The disposable container ID is unsafe."
        )
        clone = Database(Container(temporary, postgres.image_id), CLONE_ROLE, CLONE_DB)
        url = f"postgresql+asyncpg://{CLONE_ROLE}:{password}@postgres:5432/{CLONE_DB}"
        docker("start", temporary, phase="Start disposable PostgreSQL")
        wait_for_postgres(clone)
        stream_snapshot(source, clone)
        require(
            clone.revision() == SOURCE_REVISION, "The restored snapshot has a different revision."
        )
        current = run_alembic(
            backend.image_id,
            network,
            url,
            "current",
            phase="Check restored database with original backend",
        )
        require(
            SOURCE_REVISION in current.split(),
            "Original backend cannot identify the restored revision.",
        )

        columns = source_columns(clone)
        require(bool(columns), "The restored database has no source tables.")
        baseline = fingerprint(clone, columns)
        for revision, operation, phase in (
            (TARGET_REVISION, "upgrade", "Upgrade restored snapshot"),
            (SOURCE_REVISION, "downgrade", "Pre-activation rollback"),
            (TARGET_REVISION, "upgrade", "Upgrade restored snapshot again"),
        ):
            run_alembic(TARGET_IMAGE, network, url, operation, revision, phase=phase)
            if revision == TARGET_REVISION:
                run_alembic(
                    TARGET_IMAGE, network, url, "check", phase="Check model and schema alignment"
                )
            compare(clone, columns, baseline, revision, phase)
            print(f"{phase}: {revision} PASS", flush=True)

        result = {
            "status": "PASS",
            "source_schema": SOURCE_REVISION,
            "source_image_id": backend.image_id,
            "source_build_sha": args.source_build_sha,
            "target_schema": TARGET_REVISION,
            "target_build_sha": TARGET_SHA,
            "preserved_source_tables": len(baseline),
            "business_sha256": hashlib.sha256(
                json.dumps(baseline, sort_keys=True).encode()
            ).hexdigest(),
        }
    finally:
        cleanup_errors = []
        if temporary and temporary not in {backend.id, postgres.id}:
            try:
                docker("rm", "--force", "--volumes", temporary, phase=f"Remove {prefix}-postgres")
            except RehearsalError as exc:
                cleanup_errors.append(str(exc))
        if network:
            try:
                docker("network", "rm", network, phase=f"Remove {prefix}-net")
            except RehearsalError as exc:
                cleanup_errors.append(str(exc))
        require(not cleanup_errors, " ".join(cleanup_errors))
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
        print(json.dumps(rehearse(args), sort_keys=True), flush=True)
        return 0
    except (RehearsalError, OSError, ValueError) as exc:
        parser.exit(1, f"Rehearsal stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
