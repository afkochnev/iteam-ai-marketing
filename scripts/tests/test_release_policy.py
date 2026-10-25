from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_ops as ops
from test_release_ops import FakeRuntime, NEW, OLD, manifest


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_env_update_preserves_unrelated_crlf_bytes(self):
        original = (
            'SECRET="do not expand $value"\r\nBUILD_SHA='
            + OLD
            + "\r\nVK_PUBLISHING_ENABLED=true\r\n"
        )
        updated = ops.set_env(original, {"BUILD_SHA": NEW})
        self.assertEqual(updated, original.replace(OLD, NEW))

    def gate_data(self):
        return {
            "branches/main/protection": {
                "enforce_admins": {"enabled": True},
                "required_status_checks": {
                    "strict": True,
                    "contexts": ["backend", "frontend", "release-tools"],
                },
                "required_pull_request_reviews": {"required_approving_review_count": 0},
                "required_conversation_resolution": {"enabled": True},
            },
            "environments/production": {
                "deployment_branch_policy": {"protected_branches": True},
                "protection_rules": [
                    {
                        "type": "required_reviewers",
                        "reviewers": [
                            {"type": "User", "reviewer": {"login": ops.REVIEWER}}
                        ],
                    }
                ],
            },
        }

    def test_live_gate_rules_are_verified_not_just_environment_name(self):
        good = self.gate_data()
        with patch.object(ops, "gh", side_effect=lambda path: good[path]):
            ops.gates()
        for change in (
            "missing-reviewer",
            "extra-team",
            "missing-check",
            "admin-bypass",
            "force-push",
        ):
            data = copy.deepcopy(good)
            main, env = (
                data["branches/main/protection"],
                data["environments/production"],
            )
            if change == "missing-reviewer":
                env["protection_rules"] = []
            elif change == "extra-team":
                env["protection_rules"][0]["reviewers"].append(
                    {"type": "Team", "reviewer": {"slug": "other"}}
                )
            elif change == "missing-check":
                main["required_status_checks"]["contexts"].remove("release-tools")
            elif change == "admin-bypass":
                main["enforce_admins"]["enabled"] = False
            else:
                main["allow_force_pushes"] = {"enabled": True}
            with (
                self.subTest(change=change),
                patch.object(ops, "gh", side_effect=lambda path: data[path]),
                self.assertRaises(ops.ReleaseError),
            ):
                ops.gates()

    def test_latest_exact_commit_ci_must_be_green(self):
        def api(path):
            if path.startswith("compare/"):
                return {"status": "ahead"}
            return {
                "workflow_runs": [
                    {"head_sha": NEW, "conclusion": "failure", "status": "completed"},
                    {"head_sha": NEW, "conclusion": "success", "status": "completed"},
                ]
            }

        with (
            patch.object(ops, "gh", side_effect=api),
            self.assertRaises(ops.ReleaseError),
        ):
            ops.accepted(NEW)

    def test_valid_signed_receipt_is_bound_to_run_and_source(self):
        info = {
            "run_attempt": 1,
            "head_sha": OLD,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        ops.save(
            self.root / "release.json",
            {
                "approval_run_id": 99,
                "approval_attempt": 1,
                "approval_workflow_sha": OLD,
                "expected_source_release": OLD,
                "release": manifest(),
            },
        )
        reviews = [
            {
                "state": "approved",
                "user": {"login": ops.REVIEWER, "type": "User"},
                "environments": [{"name": "production"}],
            }
        ]
        with (
            patch.object(ops, "gates"),
            patch.object(ops, "run_info", return_value=info),
            patch.object(ops, "accepted"),
            patch.object(ops, "gh", return_value=reviews),
            patch.object(ops, "command", return_value=""),
            patch.object(ops, "prepare", return_value=manifest()),
        ):
            value, source = ops.approved(99, self.root, "cosign")
            self.assertEqual(source, OLD)
            self.assertEqual(value, manifest())

    def test_another_source_release_is_blocked_before_consuming_approval(self):
        runtime = FakeRuntime(self.root)
        state = self.root / "state"
        with (
            patch.object(ops, "approved", return_value=(manifest(), NEW)),
            patch.object(ops, "Runtime", return_value=runtime),
            patch.object(ops, "switch") as switch,
            self.assertRaisesRegex(ops.ReleaseError, "SOURCE_RELEASE_CHANGED"),
        ):
            ops.apply(self.root, state, 18, self.root / "proof", "cosign")
        switch.assert_not_called()
        self.assertFalse((state / "approval-18.consumed").exists())

    def test_provenance_requires_both_platforms_and_exact_sha_endpoint(self):
        ref = manifest()["images"]["frontend"]
        descriptor = {
            "digest": "sha256:" + "a" * 64,
            "manifests": [
                {"platform": {"os": "linux", "architecture": arch}}
                for arch in ("amd64", "arm64")
            ],
        }
        sbom = {
            platform: {"SPDX": {"packages": ["fixture"]}}
            for platform in ("linux/amd64", "linux/arm64")
        }
        args = {
            "build-arg:BUILD_SHA": NEW,
            "label:org.opencontainers.image.revision": NEW,
            "label:org.opencontainers.image.source": "https://github.com/" + ops.REPO,
            "build-arg:NEXT_PUBLIC_API_URL": ops.API_URL,
        }
        provenance = {
            platform: {
                "SLSA": {
                    "buildDefinition": {
                        "externalParameters": {"request": {"args": dict(args)}}
                    }
                }
            }
            for platform in sbom
        }

        def run(argv, **kwargs):
            if argv[0] == "cosign":
                return "{}"
            return json.dumps(
                {
                    "{{json .Manifest}}": descriptor,
                    "{{json .SBOM}}": sbom,
                    "{{json .Provenance}}": provenance,
                }[argv[-1]]
            )

        with patch.object(ops, "command", side_effect=run):
            ops.verify_image("frontend", ref, NEW, "cosign")
            for key in ("build-arg:BUILD_SHA", "build-arg:NEXT_PUBLIC_API_URL"):
                with self.subTest(key=key):
                    provenance["linux/arm64"]["SLSA"]["buildDefinition"][
                        "externalParameters"
                    ]["request"]["args"][key] = "wrong"
                    with self.assertRaises(ops.ReleaseError):
                        ops.verify_image("frontend", ref, NEW, "cosign")
                    provenance["linux/arm64"]["SLSA"]["buildDefinition"][
                        "externalParameters"
                    ]["request"]["args"][key] = args[key]
            descriptor["manifests"].pop()
            with self.assertRaises(ops.ReleaseError):
                ops.verify_image("frontend", ref, NEW, "cosign")


if __name__ == "__main__":
    unittest.main()
