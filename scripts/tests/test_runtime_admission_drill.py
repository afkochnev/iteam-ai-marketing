"""Contracts for the disposable controller; CI executes the actual Docker drill."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_admission_drill as drill
import runtime_admission_fixture as fixture
from migration_activation_state import TEMP_PREFIX


SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64


def snapshot(*, committed=False):
    return {
        "schema": fixture.CHECKOUT_SCHEMA, "database_identity_sha256": "sha256:" + "c" * 64,
        "all_tables_sha256": "sha256:" + ("e" if committed else "d") * 64,
        "table_counts": {"campaigns": 2 if committed else 1, "agent_runs": 3},
        "claim_status": "FAILED" if committed else "QUEUED", "claim_started": committed,
        "claim_finished": committed, "committed_canary_count": 1 if committed else 0,
        "stale_get_status": "FAILED" if committed else "QUEUED",
    }


class RedisWitnessTests(unittest.TestCase):
    def observation(self):
        result = drill.RedisObservation()
        result.feed("OK\n")
        return result

    def test_real_monitor_format_allows_only_health_and_connection_metadata(self):
        value = self.observation()
        for command in ('"CLIENT" "SETINFO" "LIB-NAME" "redis-py"', '"SELECT" "15"', '"PING"'):
            value.feed('1795362720.012345 [15 172.18.0.4:52800] ' + command + "\n")
        self.assertEqual(value.report()["allowed_readiness_commands"], 3)
        self.assertEqual(value.report()["forbidden_commands"], 0)

    def test_queued_claim_dispatch_ack_and_unknown_commands_fail_closed(self):
        for command in ('BRPOP', 'LPUSH', 'LREM', 'HSET', 'PUBLISH', 'EVAL', 'FUTURE_COMMAND'):
            with self.subTest(command=command):
                value = self.observation()
                value.feed('1795362720.012345 [15 172.18.0.4:52800] "' + command + '" "secret-payload"\n')
                with self.assertRaisesRegex(drill.DrillError, "REDIS_ACTIVITY_DETECTED"):
                    value.report()
                self.assertNotIn("secret-payload", repr(vars(value)))

    def test_connection_metadata_is_not_arbitrary_client_permission(self):
        value = self.observation()
        value.feed('1795362720.012345 [15 127.0.0.1:42] "CLIENT" "KILL" "secret"\n')
        with self.assertRaisesRegex(drill.DrillError, "REDIS_ACTIVITY_DETECTED"):
            value.report()

    def test_missing_startup_malformed_line_and_loss_are_sticky(self):
        for trigger in (lambda value: value.feed("not a monitor record\n"), lambda value: value.lost()):
            value = self.observation()
            trigger(value)
            value.feed('1795362720.012345 [0 127.0.0.1:42] "PING"\n')
            with self.assertRaisesRegex(drill.DrillError, "REDIS_WITNESS_FAILED"):
                value.report()
        value = drill.RedisObservation()
        value.feed("wrong startup\n")
        value.feed("OK\n")
        with self.assertRaises(drill.DrillError):
            value.report()

    def test_sync_timeout_cannot_be_repaired_by_late_sentinel(self):
        value = self.observation()
        sentinel = "admission-sync-" + "a" * 32
        with self.assertRaisesRegex(drill.DrillError, "REDIS_WITNESS_FAILED"):
            value.wait(sentinel=sentinel, timeout=0)
        value.feed('1795362720.012345 [0 127.0.0.1:42] "PING" "' + sentinel + '"\n')
        with self.assertRaises(drill.DrillError):
            value.report()

    def test_sentinel_requires_observed_matching_command(self):
        value = self.observation()
        sentinel = "admission-sync-" + "1" * 32
        value.feed('1795362720.012345 [0 127.0.0.1:42] "PING" "' + sentinel + '"\n')
        value.wait(sentinel=sentinel, timeout=0)
        self.assertTrue(value.report()["continuous_monitor_verified"])


class CandidateArchiveTests(unittest.TestCase):
    def archive(self, root, additions=()):
        path = root / "candidate.tar"
        with tarfile.open(path, "w") as stream:
            for name in ("backend/Dockerfile", "backend/requirements.lock"):
                value = tarfile.TarInfo(name)
                value.size = 4
                stream.addfile(value, io.BytesIO(b"test"))
            for name, kind in additions:
                value = tarfile.TarInfo(name)
                value.type = kind
                value.linkname = "/outside"
                stream.addfile(value, io.BytesIO())
        return path

    def test_only_exact_archive_backend_files_become_build_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self.archive(root)
            drill.extract_backend(archive, root / "context")
            self.assertEqual((root / "context/backend/Dockerfile").read_bytes(), b"test")
            self.assertEqual((root / "context/backend/Dockerfile").stat().st_mode & 0o777, 0o600)

    def test_traversal_links_devices_and_non_backend_paths_never_extract(self):
        cases = (("../escape", tarfile.REGTYPE), ("backend/../../escape", tarfile.REGTYPE),
                 ("/backend/escape", tarfile.REGTYPE), ("production.env", tarfile.REGTYPE),
                 ("backend/link", tarfile.SYMTYPE), ("backend/hard", tarfile.LNKTYPE),
                 ("backend/fifo", tarfile.FIFOTYPE))
        for item in cases:
            with self.subTest(item=item), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with self.assertRaisesRegex(drill.DrillError, "ARCHIVE_UNSAFE"):
                    drill.extract_backend(self.archive(root, (item,)), root / "context")
                self.assertFalse((root / "context").exists())

    def test_dirty_tracked_checkout_is_rejected_before_docker_build(self):
        class Commands:
            def run(self, args, **kwargs):
                if args[:2] == ["git", "rev-parse"]:
                    return subprocess.CompletedProcess(args, 0, SHA + "\n", "")
                if args[:2] == ["git", "status"]:
                    return subprocess.CompletedProcess(args, 0, " M backend/app/main.py\n", "")
                raise AssertionError("A dirty checkout must never build")
        with tempfile.TemporaryDirectory() as temporary:
            candidate = drill.Candidate(Commands(), Path(temporary), Path(temporary))
            with self.assertRaisesRegex(drill.DrillError, "CHECKOUT_UNSAFE"):
                candidate.build()


class EvidenceTests(unittest.TestCase):
    def test_snapshot_requires_the_exact_schema_of_each_drill(self):
        historical_schema = "20261121_0030"
        for expected, wrong in ((fixture.CHECKOUT_SCHEMA, historical_schema),
                                (historical_schema, fixture.CHECKOUT_SCHEMA)):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                candidate = {"candidate_sha": SHA, "candidate_image_id": IMAGE}
                runtime = drill.Runtime(None, candidate, Path(temporary), {}, schema=expected)
                runtime.python = Mock(return_value=dict(snapshot(), schema=expected))
                self.assertEqual(runtime.snapshot()["schema"], expected)
                runtime.python.return_value["schema"] = wrong
                with self.assertRaisesRegex(drill.DrillError, "PROBE_INVALID"):
                    runtime.snapshot()

    def test_health_is_candidate_protocol_bound_and_passive_never_ready_for_business(self):
        value = {"status": "ok", "database": "ok", "redis": "ok", "schema": "ok",
                 "build_sha": SHA, "admission_protocol": 1, "admission_mode": "passive",
                 "business_ready": False, "database_read_only": True}
        drill.validate_health(value, mode="passive", sha=SHA, ready=True)
        for key, wrong in (("build_sha", "f" * 40), ("business_ready", True),
                           ("admission_protocol", True), ("admission_mode", "active"),
                           ("database_read_only", False), ("redis", "skipped")):
            with self.subTest(key=key), self.assertRaises(drill.DrillError):
                drill.validate_health(dict(value, **{key: wrong}), mode="passive", sha=SHA, ready=True)

    def test_full_database_equality_includes_control_rows_and_identity(self):
        baseline = snapshot()
        drill.verify_unchanged(baseline, copy.deepcopy(baseline))
        for key, value in (("all_tables_sha256", IMAGE), ("database_identity_sha256", IMAGE),
                           ("stale_get_status", "FAILED"), ("claim_started", True),
                           ("committed_canary_count", 1)):
            with self.subTest(key=key), self.assertRaises(drill.DrillError):
                drill.verify_unchanged(baseline, dict(baseline, **{key: value}))

    def test_already_consumed_fixture_cannot_be_called_passive_unchanged(self):
        value = snapshot(committed=True)
        with self.assertRaisesRegex(drill.DrillError, "PASSIVE_WRITE_DETECTED"):
            drill.verify_unchanged(value, copy.deepcopy(value))

    def test_no_production_provider_or_test_memory_broker_environment(self):
        env = drill.environment(sha=SHA)
        self.assertEqual(env["APP_ENV"], "development")
        self.assertFalse(env["DATABASE_URL"].endswith("_test"))
        self.assertEqual(env["REDIS_URL"], "redis://redis:6379/15")
        for name in ("OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "VK_ACCESS_TOKEN", "VK_CREDENTIALS_FILE",
                     "TELEGRAM_METRICS_SESSION", "VK_METRICS_ACCESS_TOKEN"):
            self.assertEqual(env[name], "")
        self.assertTrue(all(value is False for value in drill.FALSE_AUTHORITY.values()))

    def test_downgrade_restore_stamp_never_reach_subprocess(self):
        commands = drill.Commands()
        with patch.object(subprocess, "run") as run:
            for token in ("downgrade", "pg_restore", "dropdb", "stamp"):
                with self.assertRaisesRegex(drill.DrillError, "COMMAND_FORBIDDEN"):
                    commands.run(["docker", "exec", "fixture", token])
            run.assert_not_called()


class WorkerCommandTests(unittest.TestCase):
    def test_every_shipped_worker_role_uses_safe_container_name_and_original_queue(self):
        class Commands:
            def __init__(self):
                self.created = {}
                self.runs = []

            def run(self, args, **kwargs):
                if args[:2] == ["docker", "run"]:
                    self.runs.append(args)
                    name = args[args.index("--name") + 1]
                    labels = dict(args[index + 1].split("=", 1)
                                  for index, token in enumerate(args) if token == "--label")
                    self.created[name] = {"Name": "/" + name, "Image": IMAGE,
                                          "Config": {"Labels": labels},
                                          "HostConfig": {"RestartPolicy": {"Name": "no"},
                                                         "PortBindings": None, "Privileged": False}}
                    return subprocess.CompletedProcess(args, 1, "", "RUNTIME_PASSIVE")
                if args[:2] == ["docker", "inspect"]:
                    return subprocess.CompletedProcess(args, 0, json.dumps([self.created[args[2]]]), "")
                if args[:2] == ["docker", "rm"]:
                    self.created.pop(args[-1])
                    return subprocess.CompletedProcess(args, 0, "", "")
                raise AssertionError("Unexpected command family")

        with tempfile.TemporaryDirectory() as temporary:
            commands = Commands()
            runtime = drill.Runtime(commands, {"candidate_sha": SHA, "candidate_image_id": IMAGE},
                                    Path(temporary), {}, schema=fixture.CHECKOUT_SCHEMA)
            roles = ("ai", "ai_control", "publication", "publication_control", "metrics", "")
            for role in roles:
                with self.subTest(role=role):
                    result = runtime.worker(role, mode="passive")
                    self.assertEqual(result.returncode, 1)
                    args = commands.runs[-1]
                    name = args[args.index("--name") + 1]
                    self.assertRegex(name, r"^iteam-admission-drill-[0-9a-f]{10}-worker-[a-z-]+$")
                    self.assertNotIn("_", name)
                    self.assertIn("WORKER_ROLE=" + role, args)
                    self.assertIn("--queues=" + (role or "ai"), args)
                    self.assertIn("--pull=never", args)
                    self.assertIn(IMAGE, args)
            self.assertEqual(len(commands.runs), len(roles))
            self.assertEqual(commands.created, {})
            self.assertEqual(runtime.containers, {})
            self.assertEqual(runtime.apps, set())


class ActivationRuntime:
    """Unit driver double; real state primitives and durable files are used."""
    def __init__(self, root):
        self.root = root
        self.schema = fixture.CHECKOUT_SCHEMA
        self.project = "iteam-admission-drill-" + "1" * 10
        self.operation = "2" * 32
        self.candidate = {"candidate_sha": SHA, "candidate_image_id": IMAGE}
        self.commands = drill.Commands()
        self.events = []
        self.after = snapshot(committed=True)
        self.physical_calls = 0
        self.fail_http = False
        self.fail_after = False

    def record(self, name, boundary=False):
        if boundary:
            marker = self.root / "state" / ("activation-" + self.operation + ".started")
            assert marker.exists(), "Runtime started before durable marker"
            assert marker.stat().st_mode & 0o777 == 0o600
            value = json.loads(marker.read_text())
            assert value["binding"]["candidate_image_id"] == IMAGE
            assert value["schema_downgrade_allowed"] is False
        self.events.append(name)

    def stop_apps(self, *, remove=False):
        self.record("stop-passive" if remove else "stop-active")
        self.physical_calls += 1

    def fence(self):
        self.record("fence")
        self.physical_calls += 1

    def reconnect(self, state):
        state.assert_activation_permitted()
        self.record("reconnect", True)

    def api(self, mode):
        assert mode == "active"
        self.record("api", True)

    def worker(self, role, *, mode, detached):
        assert (role, mode, detached) == ("ai", "active", True)
        self.record("worker", True)

    def http(self, path, **kwargs):
        self.record("http", True)
        if self.fail_http:
            raise drill.DrillError("ACTIVE_WRITE_NOT_PROVED")
        return {"http_status": 201, "body": {"name": fixture.CANARY_NAME}} if kwargs else {"http_status": 200}

    def snapshot(self, *, fenced=False):
        self.record("snapshot-contained" if fenced else "snapshot", True)
        value = copy.deepcopy(self.after)
        if fenced and self.fail_after:
            value["all_tables_sha256"] = IMAGE
        return value

    def containment(self):
        self.record("containment", True)
        value = json.loads(next((self.root / "state").glob("activation-*.json")).read_text())
        assert value["failure_code"] == "INJECTED_POST_ACTIVATION_FAILURE"
        assert value["phase"] == "CONTAINMENT_REQUESTED"
        self.physical_calls += 1
        return {"new_claims_blocked": True, "runtime_stopped": True, "data_services_fenced": True}

    def stop_and_fence(self):
        self.stop_apps()
        self.fence()


class ActivationOrderingTests(unittest.TestCase):
    def execute(self, runtime):
        return drill.activation_checks(runtime, {"status": "PASS"}, snapshot(), runtime.root / "state")

    def test_real_durable_marker_precedes_all_active_commands_and_failure_preserves_data(self):
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temporary:
            runtime = ActivationRuntime(Path(temporary))
            result = self.execute(runtime)
            self.assertEqual(runtime.events[:3], ["stop-passive", "fence", "reconnect"])
            self.assertTrue(result["http_write_committed"])
            self.assertTrue(result["real_worker_claim_committed"])
            self.assertEqual(result["activation_state"]["phase"], "CONTAINED")
            self.assertEqual(result["committed_all_tables_sha256"], result["contained_all_tables_sha256"])
            self.assertTrue(result["same_process_reactivation_blocked"])
            self.assertFalse(result["activation_state"]["schema_downgrade_allowed"])
            self.assertFalse(result["activation_state"]["production_activation_authorized"])

    def test_http_failure_still_physically_stops_and_fences(self):
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temporary:
            runtime = ActivationRuntime(Path(temporary))
            runtime.fail_http = True
            with self.assertRaisesRegex(drill.DrillError, "ACTIVE_WRITE_NOT_PROVED"):
                self.execute(runtime)
            self.assertEqual(runtime.events[-2:], ["stop-active", "fence"])
            value = json.loads(next((runtime.root / "state").glob("activation-*.json")).read_text())
            self.assertEqual(value["phase"], "OPERATOR_RECOVERY_REQUIRED")

    def test_marker_failure_forbids_any_active_command_but_still_contains(self):
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temporary:
            runtime = ActivationRuntime(Path(temporary))
            with patch.object(drill.CandidateActivationState, "begin_activation", side_effect=ops_error()):
                with self.assertRaises(RuntimeError):
                    self.execute(runtime)
            self.assertNotIn("reconnect", runtime.events)
            self.assertNotIn("api", runtime.events)
            self.assertEqual(runtime.events[-2:], ["stop-active", "fence"])

    def test_changed_committed_data_cannot_produce_successful_containment_proof(self):
        with tempfile.TemporaryDirectory(prefix=TEMP_PREFIX) as temporary:
            runtime = ActivationRuntime(Path(temporary))
            runtime.fail_after = True
            with self.assertRaisesRegex(drill.DrillError, "COMMITTED_DATA_CHANGED"):
                self.execute(runtime)
            value = json.loads(next((runtime.root / "state").glob("activation-*.json")).read_text())
            self.assertEqual(value["phase"], "OPERATOR_RECOVERY_REQUIRED")

    def test_stop_failure_still_attempts_independent_database_and_broker_fence(self):
        class BrokenRuntime:
            def __init__(self):
                self.fenced = False

            def stop_apps(self):
                raise drill.DrillError("COMMAND_FAILED")

            def fence(self):
                self.fenced = True

        runtime = BrokenRuntime()
        with self.assertRaisesRegex(drill.DrillError, "CONTAINMENT_FAILED"):
            drill.Runtime.stop_and_fence(runtime)
        self.assertTrue(runtime.fenced)


def ops_error():
    return drill.ops.ReleaseError("CANDIDATE_ACTIVATION_STATE_WRITE_FAILED")


class CliAndArtifactTests(unittest.TestCase):
    def test_cli_does_not_accept_source_state_or_authority_overrides(self):
        for option in ("--candidate-sha", "--root", "--state-dir", "--resume", "--production"):
            with self.subTest(option=option), patch.object(drill, "run_drill") as run:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = drill.main(["--output", "unused.json", option, "private-secret"])
                self.assertEqual(code, 1)
                run.assert_not_called()
                value = json.loads(output.getvalue())
                self.assertEqual(value["error_code"], "ARGUMENTS_INVALID")
                self.assertNotIn("private-secret", output.getvalue())

    def test_failure_output_redacts_subprocess_or_exception_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "proof.json"
            with patch.object(drill, "run_drill", side_effect=RuntimeError("password=very-private")):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(drill.main(["--output", str(path)]), 1)
            self.assertNotIn("very-private", output.getvalue())
            self.assertFalse(path.exists())

    def test_proof_publish_is_exclusive_private_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "proof.json"
            drill.publish(output, {"authorization": drill.AUTHORIZATION})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(drill.DrillError):
                drill.publish(output, {"changed": True})
            alias = root / "alias"
            alias.symlink_to(output)
            with self.assertRaises(drill.DrillError):
                drill.publish(alias, {})
            self.assertEqual(json.loads(output.read_text()), {"authorization": drill.AUTHORIZATION})

    def test_cleanup_failure_prevents_pass_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(drill.Candidate, "build", side_effect=drill.DrillError("COMMAND_FAILED")), \
                    patch.object(drill.Candidate, "cleanup", return_value=False):
                with self.assertRaisesRegex(drill.DrillError, "CLEANUP_FAILED"):
                    drill.run_drill(Path(temporary))


if __name__ == "__main__":
    unittest.main()
