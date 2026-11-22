#!/usr/bin/env python3
"""Prepare and verify migration approvals without connecting to production.

The only image execution is the existing network-isolated schema/inventory
inspection. This module has no apply, restore, migration, or consumption path.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import re
import stat
import subprocess
import tempfile
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import migration_release_drill as drill
import release_ops as ops
from migration_rehearsal_proof import ERROR_CODES as PROOF_ERROR_CODES
from migration_rehearsal_proof import validate_rehearsal_proof
from release_manifest import ManifestContractError, validate_migration_manifest

REPO = ops.REPO
WORKFLOW = "approve-migration-release.yml"
REQUEST_PATH = "scripts/fixtures/migration-0028-to-0030.json"
REVIEW_TYPE = "MIGRATION_RELEASE_REVIEW"
RECEIPT_TYPE = "MIGRATION_RELEASE_APPROVAL"
PROOF_ARTIFACT = "migration-drill-proof"
APPROVED_ARTIFACT = "approved-migration-release"
PROOF_FILE = "migration-drill-proof.json"
RECEIPT_FILE = "migration-release.json"
BUNDLE_FILE = "migration-release.sigstore.json"
MAX_BYTES = 4 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
ERROR_CODES = frozenset({
    "INVALID_MIGRATION_APPROVAL_ID", "INVALID_MIGRATION_APPROVAL_HASH",
    "UNTRUSTED_MIGRATION_WORKFLOW", "MIGRATION_WORKFLOW_NOT_SUCCESSFUL",
    "MIGRATION_WORKFLOW_RERUN_FORBIDDEN", "MIGRATION_WORKFLOW_CHANGED",
    "INVALID_MIGRATION_APPROVAL_TIME", "MIGRATION_ARTIFACT_MISMATCH",
    "MIGRATION_ARTIFACT_EXPIRED", "MIGRATION_ARTIFACT_CHANGED",
    "MIGRATION_ARTIFACT_DOWNLOAD_FAILED", "MIGRATION_ARTIFACT_DIGEST_MISMATCH",
    "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE", "INVALID_MIGRATION_APPROVAL_JSON",
    "INVALID_TRUSTED_MIGRATION_REQUEST", "MIGRATION_PROOF_HASH_MISMATCH",
    "EXPECTED_MIGRATION_SOURCE_MISMATCH", "EXPECTED_MIGRATION_BUILD_MISMATCH",
    "INVALID_MIGRATION_REVIEW", "INVALID_MIGRATION_RECEIPT",
    "MIGRATION_REVIEW_HASH_MISMATCH", "MIGRATION_REVIEW_CHANGED",
    "MIGRATION_APPROVAL_CONTEXT_MISMATCH", "MIGRATION_APPROVAL_EXPIRED",
    "MIGRATION_HUMAN_APPROVAL_MISSING", "MIGRATION_APPROVAL_OUTPUT_EXISTS",
    "INVALID_MIGRATION_APPROVAL_FILE", "MIGRATION_APPROVAL_NOT_RUNNING",
})


def positive(value: Any) -> int:
    ops.require(type(value) is int and value > 0, "INVALID_MIGRATION_APPROVAL_ID")
    return value


def digest(value: Any) -> str:
    ops.require(
        isinstance(value, str) and DIGEST.fullmatch(value),
        "INVALID_MIGRATION_APPROVAL_HASH",
    )
    return value


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def parse_json(data: bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            ops.require(key not in result, "INVALID_MIGRATION_APPROVAL_JSON")
            result[key] = value
        return result

    def constant(_value: str) -> None:
        raise ops.ReleaseError("INVALID_MIGRATION_APPROVAL_JSON")

    def finite_number(value: str) -> float:
        number = float(value)
        ops.require(math.isfinite(number), "INVALID_MIGRATION_APPROVAL_JSON")
        return number

    ops.require(0 < len(data) <= MAX_BYTES, "INVALID_MIGRATION_APPROVAL_JSON")
    try:
        return json.loads(
            data.decode("utf-8"), object_pairs_hook=pairs,
            parse_constant=constant, parse_float=finite_number,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ops.ReleaseError("INVALID_MIGRATION_APPROVAL_JSON") from None


def timestamp(value: Any) -> datetime:
    ops.require(isinstance(value, str), "INVALID_MIGRATION_APPROVAL_TIME")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        ops.require(result.utcoffset() is not None, "INVALID_MIGRATION_APPROVAL_TIME")
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        raise ops.ReleaseError("INVALID_MIGRATION_APPROVAL_TIME") from None


def trusted_run(
    run_id: int, workflow: str, event: str, *, completed: bool = True
) -> dict[str, Any]:
    positive(run_id)
    info = ops.gh(f"actions/runs/{run_id}")
    ops.require(type(info) is dict, "UNTRUSTED_MIGRATION_WORKFLOW")
    repository, head_repository = info.get("repository"), info.get("head_repository")
    ops.require(
        type(info.get("id")) is int and info["id"] == run_id
        and info.get("path") == ".github/workflows/" + workflow
        and info.get("event") == event and info.get("head_branch") == "main"
        and isinstance(info.get("head_sha"), str) and SHA.fullmatch(info["head_sha"])
        and type(repository) is dict and repository.get("full_name") == REPO
        and type(repository.get("id")) is int and repository["id"] > 0
        and type(head_repository) is dict and head_repository.get("full_name") == REPO
        and type(head_repository.get("id")) is int
        and head_repository["id"] == repository["id"],
        "UNTRUSTED_MIGRATION_WORKFLOW",
    )
    ops.require(
        type(info.get("run_attempt")) is int and info["run_attempt"] == 1,
        "MIGRATION_WORKFLOW_RERUN_FORBIDDEN",
    )
    if completed:
        ops.require(
            info.get("status") == "completed" and info.get("conclusion") == "success",
            "MIGRATION_WORKFLOW_NOT_SUCCESSFUL",
        )
    else:
        ops.require(
            info.get("status") == "in_progress" and info.get("conclusion") is None,
            "MIGRATION_APPROVAL_NOT_RUNNING",
        )
    start, finish = timestamp(info.get("run_started_at")), timestamp(info.get("updated_at"))
    ops.require(start <= finish <= datetime.now(UTC), "INVALID_MIGRATION_APPROVAL_TIME")
    return info


def artifact_metadata(
    artifact_id: int, run: dict[str, Any], name: str
) -> dict[str, Any]:
    positive(artifact_id)
    item = ops.gh(f"actions/artifacts/{artifact_id}")
    ops.require(type(item) is dict, "MIGRATION_ARTIFACT_MISMATCH")
    source = item.get("workflow_run")
    ops.require(
        type(item.get("id")) is int and item["id"] == artifact_id
        and item.get("name") == name and type(source) is dict
        and type(source.get("id")) is int and source["id"] == run["id"]
        and source.get("head_sha") == run["head_sha"]
        and source.get("head_branch") == "main"
        and type(source.get("repository_id")) is int
        and type(source.get("head_repository_id")) is int
        and source["repository_id"] == source["head_repository_id"] == run["repository"]["id"]
        and type(item.get("size_in_bytes")) is int
        and 0 < item["size_in_bytes"] <= MAX_BYTES,
        "MIGRATION_ARTIFACT_MISMATCH",
    )
    digest(item.get("digest"))
    ops.require(
        item.get("expired") is False and timestamp(item.get("expires_at")) > datetime.now(UTC),
        "MIGRATION_ARTIFACT_EXPIRED",
    )
    ops.require(
        timestamp(run["run_started_at"]) <= timestamp(item.get("created_at"))
        <= timestamp(item.get("updated_at")) <= timestamp(run["updated_at"]),
        "MIGRATION_ARTIFACT_MISMATCH",
    )
    return item


def download_archive(artifact_id: int, destination: Path) -> None:
    """Keep binary ZIP bytes intact; neither stderr nor signed URLs are logged."""
    positive(artifact_id)
    try:
        with destination.open("xb") as output:
            result = subprocess.run(
                ["gh", "api", f"repos/{REPO}/actions/artifacts/{artifact_id}/zip"],
                stdout=output, stderr=subprocess.PIPE, check=False, timeout=120,
            )
        ops.require(result.returncode == 0, "MIGRATION_ARTIFACT_DOWNLOAD_FAILED")
    except (OSError, subprocess.SubprocessError):
        raise ops.ReleaseError("MIGRATION_ARTIFACT_DOWNLOAD_FAILED") from None


def unpack_archive(data: bytes, metadata: dict[str, Any], names: set[str]) -> dict[str, bytes]:
    ops.require(
        len(data) == metadata["size_in_bytes"] <= MAX_BYTES
        and sha256(data) == metadata["digest"],
        "MIGRATION_ARTIFACT_DIGEST_MISMATCH",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            ops.require(
                len(entries) == len(names) and {item.filename for item in entries} == names,
                "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE",
            )
            for item in entries:
                kind = stat.S_IFMT(item.external_attr >> 16)
                ops.require(
                    not item.is_dir() and kind in {0, stat.S_IFREG}
                    and not item.flag_bits & 1 and 0 < item.file_size <= MAX_BYTES,
                    "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE",
                )
            ops.require(sum(item.file_size for item in entries) <= MAX_BYTES, "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE")
            return {item.filename: archive.read(item) for item in entries}
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, zlib.error, EOFError):
        raise ops.ReleaseError("UNSAFE_MIGRATION_ARTIFACT_ARCHIVE") from None


def artifact_files(metadata: dict[str, Any], names: set[str]) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="iteam-migration-approval-") as directory:
        path = Path(directory) / "artifact.zip"
        download_archive(metadata["id"], path)
        ops.require(path.stat().st_size <= MAX_BYTES, "UNSAFE_MIGRATION_ARTIFACT_ARCHIVE")
        return unpack_archive(path.read_bytes(), metadata, names)


def request_from_commit(sha: str) -> dict[str, Any]:
    ops.require(isinstance(sha, str) and SHA.fullmatch(sha), "INVALID_TRUSTED_MIGRATION_REQUEST")
    record = ops.gh(f"contents/{REQUEST_PATH}?ref={sha}")
    ops.require(
        type(record) is dict and record.get("type") == "file"
        and record.get("path") == REQUEST_PATH and record.get("encoding") == "base64"
        and isinstance(record.get("content"), str)
        and type(record.get("size")) is int and 0 < record["size"] <= MAX_BYTES,
        "INVALID_TRUSTED_MIGRATION_REQUEST",
    )
    try:
        data = base64.b64decode(record["content"].replace("\n", ""), validate=True)
    except ValueError:
        raise ops.ReleaseError("INVALID_TRUSTED_MIGRATION_REQUEST") from None
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    ops.require(len(data) == record["size"] and blob == record.get("sha"), "INVALID_TRUSTED_MIGRATION_REQUEST")
    request = parse_json(data)
    drill.validate_rehearsal_request(request)
    return request


def validate_review(review: Any) -> None:
    ops.require(
        type(review) is dict and set(review) == {
            "version", "review_type", "repository", "expected_current_release_sha",
            "expected_current_schema_head", "migration_manifest",
        }
        and type(review.get("version")) is int and review["version"] == 1
        and review.get("review_type") == REVIEW_TYPE and review.get("repository") == REPO,
        "INVALID_MIGRATION_REVIEW",
    )
    manifest = review["migration_manifest"]
    validate_migration_manifest(manifest)
    ops.require(
        review["expected_current_release_sha"] == manifest["source_release_sha"]
        and review["expected_current_schema_head"] == manifest["source_schema_head"],
        "EXPECTED_MIGRATION_SOURCE_MISMATCH",
    )


def prepare(
    *, rehearsal_run_id: int, rehearsal_artifact_id: int, proof_sha256: str,
    expected_source_release: str, expected_source_schema: str, target_build_run_id: int,
    cosign: str = "cosign",
) -> dict[str, Any]:
    """Read trusted evidence, independently inspect signed images, build review."""
    digest(proof_sha256)
    positive(target_build_run_id)
    run = trusted_run(rehearsal_run_id, "ci.yml", "push")
    ops.accepted(run["head_sha"])
    request = request_from_commit(run["head_sha"])
    ops.require(
        expected_source_release == request["source_release_sha"]
        and expected_source_schema == request["source_schema_head"],
        "EXPECTED_MIGRATION_SOURCE_MISMATCH",
    )
    ops.require(target_build_run_id == request["target_build_run_id"], "EXPECTED_MIGRATION_BUILD_MISMATCH")
    metadata = artifact_metadata(rehearsal_artifact_id, run, PROOF_ARTIFACT)
    data = artifact_files(metadata, {PROOF_FILE})[PROOF_FILE]
    ops.require(sha256(data) == proof_sha256, "MIGRATION_PROOF_HASH_MISMATCH")
    proof = parse_json(data)
    validate_rehearsal_proof(
        proof, request, run_started_at=run["run_started_at"], run_updated_at=run["updated_at"],
    )
    drill.verify_images(request, cosign)
    # The target build can be rerun while the four image signatures are being
    # checked. Its completed attempt must still be the one in the proof.
    target = ops.run_info(request["target_build_run_id"], "release-images.yml", "workflow_run")
    ops.require(
        type(target.get("id")) is int and target["id"] == request["target_build_run_id"]
        and target["head_sha"] == request["target_release_sha"]
        and type(target.get("run_attempt")) is int
        and target["run_attempt"] == request["target_build_attempt"],
        "TARGET_BUILD_RUN_MISMATCH",
    )
    # A rerun or replacement while registry verification was running must fail.
    after = trusted_run(rehearsal_run_id, "ci.yml", "push")
    ops.require(after["head_sha"] == run["head_sha"] and after["updated_at"] == run["updated_at"], "MIGRATION_WORKFLOW_CHANGED")
    ops.require(artifact_metadata(rehearsal_artifact_id, after, PROOF_ARTIFACT) == metadata, "MIGRATION_ARTIFACT_CHANGED")
    manifest = {key: value for key, value in request.items() if key not in {"version", "request_type"}}
    manifest.update({
        "version": 2, "release_type": "MIGRATION",
        "rehearsal_proof": {
            "workflow_run_id": rehearsal_run_id, "artifact_id": rehearsal_artifact_id,
            "proof_sha256": proof_sha256, **drill.rehearsal_scope(request),
        },
    })
    review = {
        "version": 1, "review_type": REVIEW_TYPE, "repository": REPO,
        "expected_current_release_sha": expected_source_release,
        "expected_current_schema_head": expected_source_schema,
        "migration_manifest": manifest,
    }
    validate_review(review)
    return review


def revalidate_review(review: dict[str, Any], cosign: str) -> None:
    validate_review(review)
    manifest = review["migration_manifest"]
    proof = manifest["rehearsal_proof"]
    rebuilt = prepare(
        rehearsal_run_id=proof["workflow_run_id"], rehearsal_artifact_id=proof["artifact_id"],
        proof_sha256=proof["proof_sha256"],
        expected_source_release=review["expected_current_release_sha"],
        expected_source_schema=review["expected_current_schema_head"],
        target_build_run_id=manifest["target_build_run_id"], cosign=cosign,
    )
    ops.require(encode(review) == encode(rebuilt), "MIGRATION_REVIEW_CHANGED")


def read_file(path: Path) -> bytes:
    ops.require(path.is_file() and not path.is_symlink(), "INVALID_MIGRATION_APPROVAL_FILE")
    ops.require(0 < path.stat().st_size <= MAX_BYTES, "INVALID_MIGRATION_APPROVAL_FILE")
    return path.read_bytes()


def seal(review_path: Path, review_sha256: str, cosign: str) -> dict[str, Any]:
    """Create unsigned receipt in the guarded signing job; Cosign signs next."""
    digest(review_sha256)
    data = read_file(review_path)
    ops.require(sha256(data) == review_sha256, "MIGRATION_REVIEW_HASH_MISMATCH")
    review = parse_json(data)
    validate_review(review)
    # Canonical bytes let the consumer recompute exactly the reviewed-file hash.
    ops.require(data == encode(review), "MIGRATION_REVIEW_HASH_MISMATCH")
    expected_ref = f"{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main"
    ops.require(
        os.environ.get("GITHUB_REPOSITORY") == REPO
        and os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and os.environ.get("GITHUB_RUN_ATTEMPT") == "1"
        and os.environ.get("GITHUB_WORKFLOW_REF") == expected_ref,
        "MIGRATION_APPROVAL_CONTEXT_MISMATCH",
    )
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    run = trusted_run(run_id, WORKFLOW, "workflow_dispatch", completed=False)
    ops.require(run["head_sha"] == os.environ.get("GITHUB_SHA"), "MIGRATION_APPROVAL_CONTEXT_MISMATCH")
    ops.accepted(run["head_sha"])
    revalidate_review(review, cosign)
    after = trusted_run(run_id, WORKFLOW, "workflow_dispatch", completed=False)
    ops.require(after["head_sha"] == run["head_sha"], "MIGRATION_WORKFLOW_CHANGED")
    receipt = {
        "version": 1, "receipt_type": RECEIPT_TYPE, "repository": REPO,
        "approval_run_id": run_id, "approval_attempt": 1,
        "approval_workflow_sha": run["head_sha"], "review_sha256": review_sha256,
        "review": review,
    }
    validate_receipt(receipt, run)
    return receipt


def validate_receipt(receipt: Any, run: dict[str, Any]) -> None:
    ops.require(
        type(receipt) is dict and set(receipt) == {
            "version", "receipt_type", "repository", "approval_run_id", "approval_attempt",
            "approval_workflow_sha", "review_sha256", "review",
        }
        and type(receipt.get("version")) is int and receipt["version"] == 1
        and receipt.get("receipt_type") == RECEIPT_TYPE and receipt.get("repository") == REPO
        and type(receipt.get("approval_run_id")) is int and receipt["approval_run_id"] == run["id"]
        and type(receipt.get("approval_attempt")) is int and receipt["approval_attempt"] == 1
        and receipt.get("approval_workflow_sha") == run["head_sha"],
        "INVALID_MIGRATION_RECEIPT",
    )
    validate_review(receipt["review"])
    ops.require(receipt["review_sha256"] == sha256(encode(receipt["review"])), "MIGRATION_REVIEW_HASH_MISMATCH")


def approval_fresh(run: dict[str, Any]) -> None:
    age = (datetime.now(UTC) - timestamp(run["updated_at"])).total_seconds()
    ops.require(0 <= age <= 3600, "MIGRATION_APPROVAL_EXPIRED")


def verify_approved(approval_run_id: int, artifact_id: int, cosign: str) -> dict[str, Any]:
    """Read-only verification. Future apply must additionally consume under lock."""
    ops.gates()
    run = trusted_run(approval_run_id, WORKFLOW, "workflow_dispatch")
    approval_fresh(run)
    ops.accepted(run["head_sha"])
    history = ops.gh(f"actions/runs/{approval_run_id}/approvals")
    ops.require(type(history) is list, "MIGRATION_HUMAN_APPROVAL_MISSING")
    reviews = [
        item for item in history
        if type(item) is dict and type(item.get("environments")) is list
        and any(type(env) is dict and env.get("name") == "production" for env in item["environments"])
    ]
    ops.require(
        reviews and all(
            item.get("state") == "approved" and type(item.get("user")) is dict
            and item["user"].get("login") == ops.REVIEWER and item["user"].get("type") == "User"
            for item in reviews
        ),
        "MIGRATION_HUMAN_APPROVAL_MISSING",
    )
    metadata = artifact_metadata(artifact_id, run, APPROVED_ARTIFACT)
    files = artifact_files(metadata, {RECEIPT_FILE, BUNDLE_FILE})
    with tempfile.TemporaryDirectory(prefix="iteam-migration-signature-") as directory:
        root = Path(directory)
        for name, data in files.items():
            path = root / name
            path.write_bytes(data)
            path.chmod(0o600)
        ops.command([
            cosign, "verify-blob", "--bundle", str(root / BUNDLE_FILE),
            "--certificate-identity", f"https://github.com/{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main",
            "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
            str(root / RECEIPT_FILE),
        ])
    receipt = parse_json(files[RECEIPT_FILE])
    validate_receipt(receipt, run)
    revalidate_review(receipt["review"], cosign)
    after = trusted_run(approval_run_id, WORKFLOW, "workflow_dispatch")
    approval_fresh(after)
    ops.require(after["head_sha"] == run["head_sha"] and after["updated_at"] == run["updated_at"], "MIGRATION_WORKFLOW_CHANGED")
    ops.require(artifact_metadata(artifact_id, after, APPROVED_ARTIFACT) == metadata, "MIGRATION_ARTIFACT_CHANGED")
    return receipt


def review_summary(review: dict[str, Any]) -> str:
    validate_review(review)
    value = review["migration_manifest"]
    proof = value["rehearsal_proof"]
    return (
        "## Migration release review\n\n"
        f"Source: `{value['source_release_sha']}` / `{value['source_schema_head']}`\n\n"
        f"Target: `{value['target_release_sha']}` / `{value['target_schema_head']}`\n\n"
        f"Build: `{value['target_build_run_id']}`, attempt `{value['target_build_attempt']}`\n\n"
        f"Compatibility: `{value['compatibility_class']}`; rollback policy: `{value['pre_activation_rollback_policy']}`.\n\n"
        f"Rehearsal: [run {proof['workflow_run_id']}](https://github.com/{REPO}/actions/runs/{proof['workflow_run_id']}), "
        f"artifact `{proof['artifact_id']}`, raw proof `{proof['proof_sha256']}`.\n\n"
        "Expected production source is an operator assertion; this workflow does not connect to production.\n\n"
        "Review the ordered revisions, script hashes, all four image digests and full manifest below.\n\n"
        "```json\n" + encode(review).decode() + "```\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cosign", default="cosign")
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--rehearsal-run", type=int, required=True)
    prep.add_argument("--rehearsal-artifact", type=int, required=True)
    prep.add_argument("--proof-sha256", required=True)
    prep.add_argument("--expected-source-release", required=True)
    prep.add_argument("--expected-source-schema", required=True)
    prep.add_argument("--target-build-run", type=int, required=True)
    prep.add_argument("--summary", type=Path)
    signing = sub.add_parser("seal")
    signing.add_argument("--review", type=Path, required=True)
    signing.add_argument("--review-sha256", required=True)
    verify = sub.add_parser("verify-approved")
    verify.add_argument("--approval-run", type=int, required=True)
    verify.add_argument("--approval-artifact", type=int, required=True)
    for command in (prep, signing, verify):
        command.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    os.umask(0o077)
    try:
        ops.require(not args.output.exists() and not args.output.is_symlink(), "MIGRATION_APPROVAL_OUTPUT_EXISTS")
        if args.action == "prepare":
            result = prepare(
                rehearsal_run_id=args.rehearsal_run, rehearsal_artifact_id=args.rehearsal_artifact,
                proof_sha256=args.proof_sha256, expected_source_release=args.expected_source_release,
                expected_source_schema=args.expected_source_schema, target_build_run_id=args.target_build_run,
                cosign=args.cosign,
            )
            if args.summary is not None:
                with args.summary.open("a") as stream:
                    stream.write(review_summary(result))
        elif args.action == "seal":
            result = seal(args.review, args.review_sha256, args.cosign)
        else:
            result = verify_approved(args.approval_run, args.approval_artifact, args.cosign)
        ops.save(args.output, result)
        print(json.dumps({"status": "PASS", "action": args.action, "sha256": sha256(encode(result))}))
    except (
        ops.ReleaseError, ManifestContractError, OSError, ValueError, KeyError,
        TypeError, subprocess.SubprocessError,
    ) as error:
        code = drill.diagnostic_error_code(error)
        if isinstance(error, ops.ReleaseError) and str(error) in ERROR_CODES | PROOF_ERROR_CODES:
            code = str(error)
        print(json.dumps({"status": "FAIL", "error_code": code, "automatic_retry": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
