#!/usr/bin/env python3
"""Prepare and verify explicit, expiring migration activation approvals.

The v1 pre-activation approval is intentionally unchanged. This workflow never
connects to production: runtime identity is an operator assertion that the local
controller must compare with fresh inventory before consuming the receipt.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import migration_activation_contract as contract
import migration_release_approval as legacy
import migration_release_drill as migration
import release_ops as ops
from release_manifest import ManifestContractError, validate_migration_manifest

REPO = ops.REPO
WORKFLOW = "approve-migration-activation.yml"
REQUEST_PATH = "scripts/fixtures/migration-activation-0028-to-0030.json"
REVIEW_TYPE = "MIGRATION_ACTIVATION_REVIEW"
RECEIPT_TYPE = "MIGRATION_ACTIVATION_APPROVAL"
AUTHORIZATION = "PRODUCTION_MIGRATION_ACTIVATION"
PROOF_ARTIFACT = "migration-activation-drill-proof"
PROOF_FILE = "migration-activation-drill-proof.json"
APPROVED_ARTIFACT = "approved-migration-activation"
RECEIPT_FILE = "migration-activation.json"
BUNDLE_FILE = "migration-activation.sigstore.json"
REVIEW_FILE = "migration-activation-review.json"
MAX_BYTES = legacy.MAX_BYTES
encode = legacy.encode
sha256 = legacy.sha256
timestamp = legacy.timestamp
trusted_run = legacy.trusted_run
_OPERATION = re.compile(r"[0-9a-f]{32}\Z")

ERROR_CODES = legacy.ERROR_CODES | contract.ERROR_CODES | frozenset({
    "INVALID_ACTIVATION_RUNTIME_BINDING", "INVALID_ACTIVATION_REVIEW",
    "INVALID_ACTIVATION_RECEIPT", "INVALID_ACTIVATION_APPROVAL_WINDOW",
    "ACTIVATION_APPROVAL_EXPIRED", "ACTIVATION_ADMISSION_IMAGE_MISMATCH",
    "ACTIVATION_REHEARSAL_BINDING_MISMATCH",
})

ADMISSION_IMAGE_PROBE = r'''
import json,sys
from app.core.config import settings
from app.core.runtime_admission import admission_status,require_active_runtime,RuntimeAdmissionError
value=admission_status()
assert settings.app_env=='maintenance'
try: require_active_runtime()
except RuntimeAdmissionError as error: assert error.code=='RUNTIME_PASSIVE'
else: raise RuntimeError('ACTIVATION_ADMISSION_IMAGE_MISMATCH')
assert 'celery' not in sys.modules and 'app.api.router' not in sys.modules
print(json.dumps(value,sort_keys=True))
'''


def request_from_commit(sha: str) -> dict[str, Any]:
    ops.require(type(sha) is str and legacy.SHA.fullmatch(sha), "INVALID_TRUSTED_MIGRATION_REQUEST")
    record = ops.gh(f"contents/{REQUEST_PATH}?ref={sha}")
    ops.require(
        type(record) is dict and record.get("type") == "file"
        and record.get("path") == REQUEST_PATH and record.get("encoding") == "base64"
        and type(record.get("content")) is str and type(record.get("size")) is int
        and 0 < record["size"] <= MAX_BYTES,
        "INVALID_TRUSTED_MIGRATION_REQUEST",
    )
    try:
        data = base64.b64decode(record["content"].replace("\n", ""), validate=True)
    except ValueError:
        raise ops.ReleaseError("INVALID_TRUSTED_MIGRATION_REQUEST") from None
    blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
    ops.require(len(data) == record["size"] and blob == record.get("sha"), "INVALID_TRUSTED_MIGRATION_REQUEST")
    request = legacy.parse_json(data)
    contract.validate_activation_request(request)
    return request


def verify_admission_image(request: dict[str, Any]) -> None:
    """Independently inspect the already verified signed target, without I/O."""
    value = contract.migration_request(request)
    reference = ops.image_ref("backend", value["target_images"]["backend"])
    response = ops.command([
        "docker", "run", "--rm", "--pull=never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--env", "APP_ENV=maintenance", "--env", "RUNTIME_ADMISSION_MODE=passive",
        "--entrypoint", "python", reference, "-c", ADMISSION_IMAGE_PROBE,
    ])
    try:
        observed = legacy.parse_json(response.encode())
    except (AttributeError, UnicodeError, ops.ReleaseError):
        raise ops.ReleaseError("ACTIVATION_ADMISSION_IMAGE_MISMATCH") from None
    ops.require(contract.same_json(observed, {
        "admission_protocol": 1, "admission_mode": "passive",
        "business_ready": False, "build_sha": value["target_release_sha"],
    }), "ACTIVATION_ADMISSION_IMAGE_MISMATCH")


def load_rehearsal(
    *, rehearsal_run_id: int, rehearsal_artifact_id: int, proof_sha256: str,
    expected_source_release: str, expected_source_schema: str, target_build_run_id: int,
    cosign: str = "cosign",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return authenticated (request, proof, evidence) without runtime binding.

    The planning CLI can use this to construct the exact manifest before its
    read-only inventory. It is not an approval and does not consume anything.
    """
    legacy.digest(proof_sha256)
    legacy.positive(target_build_run_id)
    run = trusted_run(rehearsal_run_id, "ci.yml", "push")
    ops.accepted(run["head_sha"])
    request = request_from_commit(run["head_sha"])
    migration_request = contract.migration_request(request)
    ops.require(
        expected_source_release == migration_request["source_release_sha"]
        and expected_source_schema == migration_request["source_schema_head"],
        "EXPECTED_MIGRATION_SOURCE_MISMATCH",
    )
    ops.require(target_build_run_id == migration_request["target_build_run_id"], "EXPECTED_MIGRATION_BUILD_MISMATCH")
    metadata = legacy.artifact_metadata(rehearsal_artifact_id, run, PROOF_ARTIFACT)
    data = legacy.artifact_files(metadata, {PROOF_FILE})[PROOF_FILE]
    ops.require(sha256(data) == proof_sha256, "MIGRATION_PROOF_HASH_MISMATCH")
    proof = legacy.parse_json(data)
    controller = ops.gh("git/commits/" + run["head_sha"])
    ops.require(
        type(controller) is dict and controller.get("sha") == run["head_sha"]
        and type(controller.get("tree")) is dict and type(controller["tree"].get("sha")) is str
        and legacy.SHA.fullmatch(controller["tree"]["sha"]),
        "ACTIVATION_PROOF_IDENTITY_MISMATCH",
    )
    contract.validate_activation_rehearsal_proof(
        proof, request, run_started_at=run["run_started_at"], run_updated_at=run["updated_at"],
        expected_controller_sha=run["head_sha"], expected_controller_tree=controller["tree"]["sha"],
    )
    migration.verify_images(migration_request, cosign)
    verify_admission_image(request)
    target = ops.run_info(migration_request["target_build_run_id"], "release-images.yml", "workflow_run")
    ops.require(
        type(target.get("id")) is int and target["id"] == migration_request["target_build_run_id"]
        and target["head_sha"] == migration_request["target_release_sha"]
        and type(target.get("run_attempt")) is int
        and target["run_attempt"] == migration_request["target_build_attempt"],
        "TARGET_BUILD_RUN_MISMATCH",
    )
    after = trusted_run(rehearsal_run_id, "ci.yml", "push")
    ops.require(after["head_sha"] == run["head_sha"] and after["updated_at"] == run["updated_at"], "MIGRATION_WORKFLOW_CHANGED")
    ops.require(legacy.artifact_metadata(rehearsal_artifact_id, after, PROOF_ARTIFACT) == metadata, "MIGRATION_ARTIFACT_CHANGED")
    evidence = {
        "workflow_run_id": rehearsal_run_id, "artifact_id": rehearsal_artifact_id,
        "proof_sha256": proof_sha256, **contract.activation_scope(request),
    }
    return request, proof, evidence


def build_migration_manifest(request: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    value = contract.migration_request(request)
    scope = contract.activation_scope(request)
    ops.require(
        type(evidence) is dict and set(evidence) == {"workflow_run_id", "artifact_id", "proof_sha256"} | set(scope)
        and all(contract.same_json(evidence[key], item) for key, item in scope.items()),
        "ACTIVATION_REHEARSAL_BINDING_MISMATCH",
    )
    legacy.positive(evidence["workflow_run_id"])
    legacy.positive(evidence["artifact_id"])
    legacy.digest(evidence["proof_sha256"])
    manifest = {key: copy.deepcopy(item) for key, item in value.items() if key not in {"version", "request_type"}}
    manifest.update({
        "version": 2, "release_type": "MIGRATION",
        "rehearsal_proof": {
            **{key: evidence[key] for key in ("workflow_run_id", "artifact_id", "proof_sha256")},
            **migration.rehearsal_scope(value),
        },
    })
    validate_migration_manifest(manifest)
    return manifest


def validate_runtime_binding(operation_id: Any, runtime_identity: Any) -> None:
    ops.require(type(operation_id) is str and _OPERATION.fullmatch(operation_id), "INVALID_ACTIVATION_RUNTIME_BINDING")
    try:
        legacy.digest(runtime_identity)
    except ops.ReleaseError:
        raise ops.ReleaseError("INVALID_ACTIVATION_RUNTIME_BINDING") from None


def validate_review(review: Any) -> None:
    ops.require(
        type(review) is dict and set(review) == {
            "version", "review_type", "repository", "expected_current_release_sha",
            "expected_current_schema_head", "migration_manifest", "operation_id",
            "expected_runtime_identity_sha256", "activation_policy", "activation_rehearsal",
        }
        and type(review["version"]) is int and review["version"] == 2
        and review["review_type"] == REVIEW_TYPE and review["repository"] == REPO,
        "INVALID_ACTIVATION_REVIEW",
    )
    validate_runtime_binding(review["operation_id"], review["expected_runtime_identity_sha256"])
    contract.validate_activation_policy(review["activation_policy"])
    manifest = review["migration_manifest"]
    validate_migration_manifest(manifest)
    ops.require(
        review["expected_current_release_sha"] == manifest["source_release_sha"]
        and review["expected_current_schema_head"] == manifest["source_schema_head"],
        "EXPECTED_MIGRATION_SOURCE_MISMATCH",
    )
    evidence = review["activation_rehearsal"]
    old = manifest["rehearsal_proof"]
    ops.require(
        type(evidence) is dict and set(evidence) == set(old) | {
            "admission_protocol", "activation_policy", "request_sha256",
        }
        and all(contract.same_json(evidence[key], item) for key, item in old.items())
        and type(evidence["admission_protocol"]) is int and evidence["admission_protocol"] == 1,
        "ACTIVATION_REHEARSAL_BINDING_MISMATCH",
    )
    contract.validate_activation_policy(evidence["activation_policy"])
    legacy.digest(evidence["request_sha256"])


def prepare(
    *, operation_id: str, expected_runtime_identity_sha256: str, **rehearsal_parameters: Any,
) -> dict[str, Any]:
    validate_runtime_binding(operation_id, expected_runtime_identity_sha256)
    request, _proof, evidence = load_rehearsal(**rehearsal_parameters)
    manifest = build_migration_manifest(request, evidence)
    review = {
        "version": 2, "review_type": REVIEW_TYPE, "repository": REPO,
        "expected_current_release_sha": manifest["source_release_sha"],
        "expected_current_schema_head": manifest["source_schema_head"],
        "migration_manifest": manifest, "operation_id": operation_id,
        "expected_runtime_identity_sha256": expected_runtime_identity_sha256,
        "activation_policy": contract.activation_policy(), "activation_rehearsal": evidence,
    }
    validate_review(review)
    return review


def revalidate_review(review: dict[str, Any], cosign: str) -> None:
    validate_review(review)
    evidence = review["activation_rehearsal"]
    rebuilt = prepare(
        operation_id=review["operation_id"],
        expected_runtime_identity_sha256=review["expected_runtime_identity_sha256"],
        rehearsal_run_id=evidence["workflow_run_id"], rehearsal_artifact_id=evidence["artifact_id"],
        proof_sha256=evidence["proof_sha256"],
        expected_source_release=review["expected_current_release_sha"],
        expected_source_schema=review["expected_current_schema_head"],
        target_build_run_id=review["migration_manifest"]["target_build_run_id"], cosign=cosign,
    )
    ops.require(encode(review) == encode(rebuilt), "MIGRATION_REVIEW_CHANGED")


def _receipt_window(receipt: Any) -> tuple[datetime, datetime]:
    ops.require(type(receipt) is dict and receipt.get("authorization") == AUTHORIZATION,
                "INVALID_ACTIVATION_RECEIPT")
    try:
        issued, expires = timestamp(receipt.get("issued_at")), timestamp(receipt.get("expires_at"))
    except ops.ReleaseError:
        raise ops.ReleaseError("INVALID_ACTIVATION_APPROVAL_WINDOW") from None
    ops.require(0 < (expires - issued).total_seconds() <= contract.APPROVAL_TTL_SECONDS,
                "INVALID_ACTIVATION_APPROVAL_WINDOW")
    return issued, expires


def assert_receipt_fresh(receipt: dict[str, Any], *, now: datetime | None = None) -> None:
    issued, expires = _receipt_window(receipt)
    now = datetime.now(UTC) if now is None else now
    ops.require(isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None,
                "INVALID_ACTIVATION_APPROVAL_WINDOW")
    ops.require(issued <= now.astimezone(UTC) <= expires, "ACTIVATION_APPROVAL_EXPIRED")


def validate_receipt(receipt: Any, run: dict[str, Any], *, sealing: bool = False) -> None:
    ops.require(
        type(receipt) is dict and set(receipt) == {
            "version", "receipt_type", "repository", "authorization", "approval_run_id",
            "approval_attempt", "approval_workflow_sha", "review_sha256", "review", "issued_at", "expires_at",
        }
        and type(receipt["version"]) is int and receipt["version"] == 2
        and receipt["receipt_type"] == RECEIPT_TYPE and receipt["repository"] == REPO
        and receipt["authorization"] == AUTHORIZATION
        and type(receipt["approval_run_id"]) is int and receipt["approval_run_id"] == run["id"]
        and type(receipt["approval_attempt"]) is int and receipt["approval_attempt"] == 1
        and receipt["approval_workflow_sha"] == run["head_sha"],
        "INVALID_ACTIVATION_RECEIPT",
    )
    validate_review(receipt["review"])
    ops.require(receipt["review_sha256"] == sha256(encode(receipt["review"])), "MIGRATION_REVIEW_HASH_MISMATCH")
    issued, _expires = _receipt_window(receipt)
    finish = datetime.now(UTC) if sealing else timestamp(run["updated_at"])
    ops.require(timestamp(run["run_started_at"]) <= issued <= finish, "INVALID_ACTIVATION_APPROVAL_WINDOW")


def seal(review_path: Path, review_sha256: str, cosign: str) -> dict[str, Any]:
    legacy.digest(review_sha256)
    data = legacy.read_file(review_path)
    ops.require(sha256(data) == review_sha256, "MIGRATION_REVIEW_HASH_MISMATCH")
    review = legacy.parse_json(data)
    validate_review(review)
    ops.require(data == encode(review), "MIGRATION_REVIEW_HASH_MISMATCH")
    expected_ref = f"{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main"
    ops.require(
        os.environ.get("GITHUB_REPOSITORY") == REPO and os.environ.get("GITHUB_REF") == "refs/heads/main"
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
    now = datetime.now(UTC)
    receipt = {
        "version": 2, "receipt_type": RECEIPT_TYPE, "repository": REPO, "authorization": AUTHORIZATION,
        "approval_run_id": run_id, "approval_attempt": 1, "approval_workflow_sha": run["head_sha"],
        "review_sha256": review_sha256, "review": review, "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=contract.APPROVAL_TTL_SECONDS)).isoformat(),
    }
    validate_receipt(receipt, run, sealing=True)
    return receipt


def verify_approved(approval_run_id: int, artifact_id: int, cosign: str) -> dict[str, Any]:
    """Authenticate only. The controller must bind, consume and keep a live lock."""
    ops.gates()
    run = trusted_run(approval_run_id, WORKFLOW, "workflow_dispatch")
    legacy.approval_fresh(run)
    ops.accepted(run["head_sha"])
    history = ops.gh(f"actions/runs/{approval_run_id}/approvals")
    ops.require(type(history) is list, "MIGRATION_HUMAN_APPROVAL_MISSING")
    reviews = [item for item in history if type(item) is dict and type(item.get("environments")) is list
               and any(type(env) is dict and env.get("name") == "production" for env in item["environments"])]
    ops.require(reviews and all(
        item.get("state") == "approved" and type(item.get("user")) is dict
        and item["user"].get("login") == ops.REVIEWER and item["user"].get("type") == "User"
        for item in reviews
    ), "MIGRATION_HUMAN_APPROVAL_MISSING")
    metadata = legacy.artifact_metadata(artifact_id, run, APPROVED_ARTIFACT)
    files = legacy.artifact_files(metadata, {RECEIPT_FILE, BUNDLE_FILE})
    with tempfile.TemporaryDirectory(prefix="iteam-activation-signature-") as directory:
        root = Path(directory)
        for name, data in files.items():
            path = root / name
            path.write_bytes(data)
            path.chmod(0o600)
        ops.command([
            cosign, "verify-blob", "--bundle", str(root / BUNDLE_FILE),
            "--certificate-identity", f"https://github.com/{REPO}/.github/workflows/{WORKFLOW}@refs/heads/main",
            "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com", str(root / RECEIPT_FILE),
        ])
    receipt = legacy.parse_json(files[RECEIPT_FILE])
    validate_receipt(receipt, run)
    assert_receipt_fresh(receipt)
    revalidate_review(receipt["review"], cosign)
    after = trusted_run(approval_run_id, WORKFLOW, "workflow_dispatch")
    legacy.approval_fresh(after)
    ops.require(after["head_sha"] == run["head_sha"] and after["updated_at"] == run["updated_at"], "MIGRATION_WORKFLOW_CHANGED")
    ops.require(legacy.artifact_metadata(artifact_id, after, APPROVED_ARTIFACT) == metadata, "MIGRATION_ARTIFACT_CHANGED")
    assert_receipt_fresh(receipt)
    return receipt


def review_summary(review: dict[str, Any]) -> str:
    validate_review(review)
    value = review["migration_manifest"]
    proof = review["activation_rehearsal"]
    return (
        "## Migration and activation review\n\n"
        f"Source: `{value['source_release_sha']}` / `{value['source_schema_head']}`\n\n"
        f"Target: `{value['target_release_sha']}` / `{value['target_schema_head']}`; admission protocol `1`.\n\n"
        f"Operation: `{review['operation_id']}`; runtime identity: `{review['expected_runtime_identity_sha256']}`.\n\n"
        f"Build: `{value['target_build_run_id']}`, attempt `{value['target_build_attempt']}`.\n\n"
        f"Signed-image rehearsal: [run {proof['workflow_run_id']}](https://github.com/{REPO}/actions/runs/{proof['workflow_run_id']}), "
        f"artifact `{proof['artifact_id']}`, raw proof `{proof['proof_sha256']}`.\n\n"
        "This approval explicitly allows business writes, queue claims and OpenAI requests using the bound existing configuration. "
        "Publication and metrics requests remain forbidden. It authorizes cold recreation only after the durable boundary "
        "within the same live operation; post-activation failures require stop/fence and target preservation.\n\n"
        "Receipt validity is at most one hour. Automatic retry, post-activation schema downgrade and backup restore are forbidden.\n\n"
        "After successful active postflight, the controller restores the exact bound service restart policies before recording success. "
        "This does not authorize retrying the migration operation.\n\n"
        "The expected source and runtime identity are operator assertions from a read-only inventory plan; "
        "this workflow does not connect to production. The controller must compare fresh inventory before consuming this receipt.\n\n"
        "Review the full manifest, fixed capability policy and runtime binding below.\n\n"
        "```json\n" + encode(review).decode() + "```\n"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--cosign", default="cosign")
    sub = parser.add_subparsers(dest="action", required=True)
    prepare_parser = sub.add_parser("prepare", allow_abbrev=False)
    for flag in ("rehearsal-run", "rehearsal-artifact", "target-build-run"):
        prepare_parser.add_argument("--" + flag, type=int, required=True)
    for flag in ("proof-sha256", "expected-source-release", "expected-source-schema", "operation-id", "expected-runtime-identity-sha256"):
        prepare_parser.add_argument("--" + flag, required=True)
    prepare_parser.add_argument("--summary", type=Path)
    signing = sub.add_parser("seal", allow_abbrev=False)
    signing.add_argument("--review", type=Path, required=True)
    signing.add_argument("--review-sha256", required=True)
    verify = sub.add_parser("verify-approved", allow_abbrev=False)
    verify.add_argument("--approval-run", type=int, required=True)
    verify.add_argument("--approval-artifact", type=int, required=True)
    for command in (prepare_parser, signing, verify):
        command.add_argument("--output", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    os.umask(0o077)
    try:
        ops.require(not args.output.exists() and not args.output.is_symlink(), "MIGRATION_APPROVAL_OUTPUT_EXISTS")
        if args.action == "prepare":
            result = prepare(
                operation_id=args.operation_id, expected_runtime_identity_sha256=args.expected_runtime_identity_sha256,
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
    except (ops.ReleaseError, ManifestContractError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, ops.ReleaseError) and str(error) in ERROR_CODES else "ACTIVATION_APPROVAL_FAILED"
        print(json.dumps({"status": "FAIL", "error_code": code, "automatic_retry": False}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
