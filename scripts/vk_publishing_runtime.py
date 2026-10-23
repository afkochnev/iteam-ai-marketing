#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from uuid import UUID

REPO_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_DIR / ".env"
OWNER_ID = -150574411
RUNTIME_SERVICES = (
    "backend",
    "publication_worker",
    "publication_control_worker",
    "publication_scheduler",
)


class RuntimeErrorSafe(RuntimeError):
    pass


def run(*args: str, capture: bool = False, check: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=REPO_DIR,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (
            result.stderr.strip().splitlines()[-1]
            if result.stderr.strip()
            else "command failed"
        )
        raise RuntimeErrorSafe(f"{args[0]} failed: {detail}")
    return result.stdout.strip() if capture else ""


def env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip("'\"")
    return values


def update_env(changes: dict[str, str]) -> str:
    original = ENV_PATH.read_text(encoding="utf-8")
    lines = original.splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key = line.split("=", 1)[0]
        if key in changes:
            output.append(f"{key}={changes[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in changes.items():
        if key not in seen:
            output.append(f"{key}={value}")
    mode = stat.S_IMODE(ENV_PATH.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=ENV_PATH.parent,
        prefix=".env.vk.",
        delete=False,
    ) as handle:
        temp = Path(handle.name)
        handle.write("\n".join(output) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, mode)
    os.replace(temp, ENV_PATH)
    return original


def restore_env(original: str) -> None:
    mode = stat.S_IMODE(ENV_PATH.stat().st_mode)
    ENV_PATH.write_text(original, encoding="utf-8")
    os.chmod(ENV_PATH, mode)


def postgres_identity() -> tuple[str, str]:
    user = run(
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "sh",
        "-lc",
        'printf %s "$POSTGRES_USER"',
        capture=True,
    )
    database = run(
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "sh",
        "-lc",
        'printf %s "$POSTGRES_DB"',
        capture=True,
    )
    if not user or not database:
        raise RuntimeErrorSafe("Could not determine PostgreSQL identity.")
    return user, database


def sql(query: str) -> str:
    user, database = postgres_identity()
    return run(
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-U",
        user,
        "-d",
        database,
        "-qAt",
        "-F",
        "\t",
        "-c",
        "BEGIN READ ONLY; " + query + " COMMIT;",
        capture=True,
    )


def publication(publication_id: UUID) -> list[str]:
    value = sql(
        "SELECT channel::text, status::text, "
        "COALESCE(scheduled_at::text,''), "
        "COALESCE(approved_for_publish_at::text,''), "
        "COALESCE(approved_for_publish_by::text,''), "
        "COALESCE(failure_code,''), COALESCE(provider_target_id,''), "
        "COALESCE(external_id,'') "
        f"FROM publications WHERE id = '{publication_id}'::uuid;"
    )
    if not value:
        raise RuntimeErrorSafe("Publication not found.")
    return value.split("\t")


def count(query: str) -> int:
    value = sql(query)
    try:
        return int(value)
    except ValueError as error:
        raise RuntimeErrorSafe("Unexpected PostgreSQL result.") from error


def ensure_release() -> str:
    head = run("git", "rev-parse", "HEAD", capture=True)
    if run("git", "status", "--porcelain", capture=True):
        raise RuntimeErrorSafe("Repository contains local changes.")
    with urllib.request.urlopen(
        "http://localhost:8000/health/ready", timeout=10
    ) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or any(
        payload.get(key) != "ok" for key in ("status", "database", "redis", "schema")
    ):
        raise RuntimeErrorSafe("Backend readiness checks did not pass.")
    # Readiness deliberately contains no release identity. Verify the actual
    # runtime settings for every process affected by activation instead.
    for service in RUNTIME_SERVICES:
        build_sha = run(
            "docker",
            "compose",
            "exec",
            "-T",
            service,
            "python",
            "-c",
            "from app.core.config import settings; print(settings.build_sha)",
            capture=True,
        )
        if build_sha != head:
            raise RuntimeErrorSafe(
                f"{service} BUILD_SHA {build_sha or 'missing'} does not match HEAD {head}."
            )
    return head


def credential_status() -> None:
    run("./scripts/vk-publishing-credentials.sh", "status")


def stop_dispatch() -> None:
    run(
        "docker",
        "compose",
        "--profile",
        "publishing",
        "stop",
        "publication_scheduler",
        "publication_control_worker",
    )


def restart_runtime() -> None:
    run(
        "docker",
        "compose",
        "--profile",
        "publishing",
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        *RUNTIME_SERVICES,
    )
    for _ in range(60):
        try:
            with urllib.request.urlopen(
                "http://localhost:8000/health/ready", timeout=5
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(2)
    raise RuntimeErrorSafe("Backend readiness did not recover.")


def validate_base_config() -> dict[str, str]:
    values = env_values()
    expected = {
        "VK_CLIENT_ID": "54815172",
        "VK_EXPECTED_USER_ID": "176199961",
        "VK_OWNER_ID": str(OWNER_ID),
    }
    mismatches = [key for key, value in expected.items() if values.get(key) != value]
    if mismatches:
        raise RuntimeErrorSafe(
            "Set exact VK configuration first: " + ", ".join(mismatches)
        )
    if values.get("VK_ACCESS_TOKEN", ""):
        raise RuntimeErrorSafe("Legacy VK_ACCESS_TOKEN must be empty.")
    return values


def no_inflight() -> None:
    inflight = count(
        "SELECT count(*) FROM publications "
        "WHERE channel = 'VK' AND status = 'PUBLISHING';"
    )
    if inflight:
        raise RuntimeErrorSafe(f"VK publications currently PUBLISHING: {inflight}")


def apply_runtime(changes: dict[str, str]) -> None:
    original = ENV_PATH.read_text(encoding="utf-8")
    stop_dispatch()
    try:
        update_env(changes)
        restart_runtime()
    except Exception:
        restore_env(original)
        restart_runtime()
        raise


def status() -> None:
    release = ensure_release()
    values = validate_base_config()
    credential_status()
    publishing = count(
        "SELECT count(*) FROM publications WHERE channel = 'VK' AND status = 'PUBLISHING';"
    )
    due = count(
        "SELECT count(*) FROM publications WHERE channel = 'VK' "
        "AND status = 'SCHEDULED' AND scheduled_at <= now() "
        f"AND scheduled_at >= now() - make_interval(secs => {dispatch_window()});"
    )
    print("VK PUBLISHING RUNTIME STATUS")
    print(f"- release: {release}")
    print(f"- enabled: {values.get('VK_PUBLISHING_ENABLED', 'false')}")
    print(f"- canary publication: {values.get('VK_CANARY_PUBLICATION_ID') or 'none'}")
    print(f"- PUBLISHING rows: {publishing}")
    print(f"- due rows in auto window: {due}")
    print("- credential values were not printed")


def dispatch_window() -> int:
    raw = env_values().get("PUBLICATION_AUTO_DISPATCH_MAX_LATENESS_SECONDS", "3600")
    if not raw.isdigit() or int(raw) <= 0:
        raise RuntimeErrorSafe("Invalid automatic dispatch window.")
    return int(raw)


def inventory() -> None:
    values = validate_base_config()
    if values.get("VK_PUBLISHING_ENABLED", "").lower() != "false":
        raise RuntimeErrorSafe("VK must be explicitly disabled before inventory.")
    window = dispatch_window()
    print("VK PUBLICATION INVENTORY (read-only)")
    print(
        "id / status / scheduled_at / content_version_id / content_item_id / "
        "content_channel / target / external_id / failure / auto_dispatch_eligible_now"
    )
    print(
        sql(
            "SELECT p.id, p.status, p.scheduled_at, p.content_version_id, p.content_item_id, "
            "c.channel, p.provider_target_id, p.external_id, p.failure_code, "
            "(p.status = 'SCHEDULED' AND p.approved_for_publish_at IS NOT NULL "
            "AND p.approved_for_publish_by IS NOT NULL AND p.execution_token IS NULL "
            "AND p.failure_code IS NULL AND p.scheduled_at <= now() "
            f"AND p.scheduled_at >= now() - make_interval(secs => {window})) "
            "FROM publications p LEFT JOIN content_items c ON c.id = p.content_item_id "
            "WHERE p.channel = 'VK' ORDER BY p.scheduled_at NULLS FIRST, p.created_at, p.id;"
        )
    )
    no_inflight()
    print(f"INVENTORY: PASS; dispatch window={window}s; expected owner={OWNER_ID}")


def arm_canary(publication_id: UUID) -> None:
    release = ensure_release()
    inventory()
    credential_status()
    no_inflight()
    row = publication(publication_id)
    (
        channel,
        state,
        _scheduled,
        approved_at,
        approved_by,
        failure,
        target,
        external_id,
    ) = row
    if channel != "VK":
        raise RuntimeErrorSafe("Canary publication is not VK.")
    if state not in {"APPROVED", "SCHEDULED"}:
        raise RuntimeErrorSafe(
            f"Canary publication status is {state}, expected APPROVED/SCHEDULED."
        )
    if not approved_at or not approved_by:
        raise RuntimeErrorSafe("Canary publication is not approved for delivery.")
    if failure or target or external_id:
        raise RuntimeErrorSafe("Canary publication already has delivery/failure state.")
    apply_runtime(
        {
            "VK_PUBLISHING_ENABLED": "true",
            "VK_CANARY_PUBLICATION_ID": str(publication_id),
            "VK_ACCESS_TOKEN": "",
        }
    )
    print("VK CANARY: ARMED")
    print(f"- release: {release}")
    print(f"- publication_id: {publication_id}")
    print(f"- owner_id: {OWNER_ID}")
    print("- only this VK publication can be claimed or dispatched")
    print("- other VK publications remain blocked before provider call")


def promote() -> None:
    release = ensure_release()
    values = validate_base_config()
    credential_status()
    no_inflight()
    raw_id = values.get("VK_CANARY_PUBLICATION_ID", "")
    if not raw_id:
        raise RuntimeErrorSafe("No VK canary publication is configured.")
    canary_id = UUID(raw_id)
    row = publication(canary_id)
    (
        channel,
        state,
        _scheduled,
        _approved_at,
        _approved_by,
        failure,
        target,
        external_id,
    ) = row
    if channel != "VK" or state != "PUBLISHED" or failure:
        raise RuntimeErrorSafe("VK canary is not durably PUBLISHED.")
    if target != str(OWNER_ID) or not external_id:
        raise RuntimeErrorSafe("VK canary target/external id is not verified.")
    due_other = count(
        "SELECT count(*) FROM publications WHERE channel = 'VK' "
        "AND status = 'SCHEDULED' AND scheduled_at <= now() "
        f"AND scheduled_at >= now() - make_interval(secs => {dispatch_window()}) "
        f"AND id <> '{canary_id}'::uuid;"
    )
    if due_other:
        raise RuntimeErrorSafe(
            f"Refusing promotion: {due_other} other VK publication(s) are immediately due."
        )
    apply_runtime(
        {
            "VK_PUBLISHING_ENABLED": "true",
            "VK_CANARY_PUBLICATION_ID": "",
            "VK_ACCESS_TOKEN": "",
        }
    )
    print("VK AUTOMATIC PUBLISHING: ENABLED")
    print(f"- release: {release}")
    print(f"- verified canary: {canary_id}")
    print(f"- exact owner_id: {OWNER_ID}")
    print("- immediately due unintended rows: 0")


def disable() -> None:
    release = ensure_release()
    validate_base_config()
    no_inflight()
    apply_runtime(
        {
            "VK_PUBLISHING_ENABLED": "false",
            "VK_CANARY_PUBLICATION_ID": "",
            "VK_ACCESS_TOKEN": "",
        }
    )
    print("VK AUTOMATIC PUBLISHING: DISABLED")
    print(f"- release: {release}")
    print("- no Publication business state was changed")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("inventory")
    canary = sub.add_parser("canary")
    canary.add_argument("publication_id", type=UUID)
    sub.add_parser("promote")
    sub.add_parser("disable")
    args = parser.parse_args()
    try:
        if args.command == "status":
            status()
        elif args.command == "inventory":
            ensure_release()
            inventory()
        elif args.command == "canary":
            arm_canary(args.publication_id)
        elif args.command == "promote":
            promote()
        else:
            disable()
    except (RuntimeErrorSafe, ValueError, OSError, subprocess.SubprocessError) as error:
        print("VK RUNTIME CHANGE: FAIL", file=sys.stderr)
        print(f"- Reason: {error}", file=sys.stderr)
        print("- Token values were not printed", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
