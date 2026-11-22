"""Writer-fence ordering and continuous-observation tests; no real Docker."""

from __future__ import annotations

import copy
import io
import json
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migration_writer_barrier as barrier
import release_ops as ops


class FakeProcess:
    def __init__(self):
        self.status = None
        self.stdout = io.StringIO()

    def poll(self):
        return self.status

    def terminate(self):
        self.status = 0

    def kill(self):
        self.status = -9

    def wait(self, timeout=None):
        return self.status


class FakeAudit:
    def __init__(self, *args):
        self.process = None
        self.healthy = True
        self.sealed = False
        self.syncs = 0

    def start(self):
        self.process = FakeProcess()
        self.sync()

    def sync(self):
        ops.require(self.healthy, "WRITER_BARRIER_CONTINUITY_LOST")
        self.syncs += 1

    def arm(self):
        self.sync()
        self.sealed = True

    def close(self):
        pass


class Docker:
    def __init__(self):
        self.calls = []
        self.containers = {}
        self.network_id = "a" * 64
        self.project = "iteam-migration-fixture"
        self.drain_result = {"status": "PASS", "drained": True}
        self.keep_running = set()
        self.interface_override = None
        self.host_open_after_fence = False
        for index, name in enumerate(sorted(ops.ALL), 1):
            identifier = f"{index:064x}"
            self.containers[identifier] = {
                "Id": identifier, "Name": "/" + self.project + "-" + name + "-1",
                "Image": "sha256:" + f"{index:064x}", "RestartCount": 0,
                "Config": {"Hostname": name, "Env": ["SECRET=must-not-persist"], "Labels": {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.service": name,
                }},
                "State": {"Running": True, "Paused": False, "Restarting": False,
                          "OOMKilled": False, "Error": "", "ExitCode": 0,
                          "StartedAt": "2026-01-01T00:00:00.000000000Z",
                          "FinishedAt": "0001-01-01T00:00:00Z"},
                "HostConfig": {"RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                               "NetworkMode": self.project + "_default"},
                "NetworkSettings": {"Networks": {self.project + "_default": {
                    "NetworkID": self.network_id, "EndpointID": "b" * 64,
                    "Aliases": [name], "IPAMConfig": None, "IPAddress": "172.20.0.2",
                    "IPPrefixLen": 16, "Gateway": "172.20.0.1", "DriverOpts": None,
                }}, "Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "25432"}]} if name == "postgres" else {}},
            }
        self.by_service = {value["Config"]["Labels"]["com.docker.compose.service"]: key
                           for key, value in self.containers.items()}

    @property
    def postgres(self):
        return self.containers[self.by_service["postgres"]]

    @property
    def fenced(self):
        return self.postgres["NetworkSettings"]["Networks"] == {}

    def compose(self, *args):
        if args == ("config", "--format", "json"):
            return json.dumps({"name": self.project})
        raise AssertionError(args)

    def inspect(self, name):
        return copy.deepcopy(self.containers[self.by_service[name]])

    def command(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["docker", "network", "inspect"]:
            return json.dumps([{"Driver": "bridge", "Scope": "local"}])
        if args[:3] == ["docker", "ps", "--filter"]:
            return "\n".join(key for key, value in self.containers.items()
                             if value["Config"]["Labels"].get("com.docker.compose.project") == self.project
                             and value["State"]["Running"])
        if args[:2] == ["docker", "ps"]:
            return "\n".join(self.containers)
        if args[:3] == ["docker", "inspect", "--format"]:
            return "\n".join(json.dumps([value["Id"], value["Name"], value["HostConfig"]["NetworkMode"],
                                         value["HostConfig"]["RestartPolicy"]["Name"]])
                              for value in self.containers.values())
        if args[:2] == ["docker", "inspect"]:
            return json.dumps([self.containers[key] for key in args[2:]])
        if args[:3] == ["docker", "update", "--restart=no"]:
            for key in args[3:]:
                self.containers[key]["HostConfig"]["RestartPolicy"]["Name"] = "no"
            return ""
        if args[:4] == ["docker", "container", "kill", "--signal=TERM"]:
            for key in args[4:]:
                if key not in self.keep_running:
                    self.containers[key]["State"].update({"Running": False, "FinishedAt": "2026-01-01T00:01:00Z"})
            return ""
        if args[:3] == ["docker", "network", "disconnect"]:
            self.postgres["NetworkSettings"]["Networks"] = {}
            return ""
        if args[:2] == ["docker", "exec"]:
            if "-c" in args and barrier.DRAIN_CODE in args:
                return json.dumps(self.drain_result)
            if args[-1] == "ls /sys/class/net":
                return self.interface_override or ("lo\n" if self.fenced else "eth0\nlo\n")
            if args[-1] == "/proc/net/route":
                return "Iface Destination Gateway Flags\n"
        raise AssertionError(args)


class WriterBarrierTests(unittest.TestCase):
    def setUp(self):
        self.docker = Docker()
        self.command = patch.object(ops, "command", side_effect=self.docker.command).start()
        self.addCleanup(patch.stopall)
        patch.object(barrier, "EventAudit", FakeAudit).start()
        patch.object(barrier.WriterBarrier, "_connects", side_effect=lambda host, port: not self.docker.fenced or self.docker.host_open_after_fence).start()
        self.value = barrier.WriterBarrier(self.docker, "operation-123", deadline_seconds=1)

    def fenced(self):
        self.value.capture()
        self.value.stop_schedulers()
        self.value.drain_workers()
        self.value.stop_writers()
        self.value.fence_database()
        return self.value

    def test_capture_is_read_only_and_never_persists_environment(self):
        value = self.value.capture()
        self.assertNotIn("must-not-persist", json.dumps(value))
        self.assertNotIn("Env", json.dumps(value))
        self.assertEqual(set(value["containers"]), ops.ALL)
        self.assertEqual(value["postgres_host_bindings"][0]["HostPort"], "25432")
        self.assertIsNone(self.value.audit.process)
        self.assertFalse(any(call[1] in {"update", "container", "volume"} for call in self.docker.calls))

    def test_capture_rejects_wrong_project_or_extra_writer(self):
        self.docker.containers[self.docker.by_service["backend"]]["Config"]["Labels"]["com.docker.compose.project"] = "other"
        with self.assertRaisesRegex(ops.ReleaseError, "INVALID_TOPOLOGY"):
            self.value.capture()

    def test_capture_rejects_additional_running_project_container(self):
        extra = copy.deepcopy(self.docker.postgres)
        extra_id = "e" * 64
        extra.update(Id=extra_id, Name="/unexpected-writer")
        extra["Config"]["Labels"]["com.docker.compose.service"] = "unexpected-writer"
        self.docker.containers[extra_id] = extra
        with self.assertRaisesRegex(ops.ReleaseError, "INVALID_TOPOLOGY"):
            self.value.capture()

    def test_stop_order_disables_restart_and_stops_ingress_before_canceling_claims(self):
        value = self.fenced()
        actions = [call for call in self.docker.calls if call[:2] in (["docker", "update"], ["docker", "container"], ["docker", "exec"], ["docker", "network"])]
        update = next(index for index, call in enumerate(actions) if call[1] == "update")
        stops = [(index, call) for index, call in enumerate(actions) if call[1] == "container"]
        drain = next(index for index, call in enumerate(actions) if barrier.DRAIN_CODE in call)
        self.assertLess(update, stops[0][0])
        self.assertEqual(set(stops[0][1][4:]), {self.docker.by_service[name] for name in ops.SCHEDULERS})
        self.assertEqual(set(stops[1][1][4:]), {self.docker.by_service[name] for name in ("frontend", "backend")})
        self.assertLess(stops[1][0], drain)
        self.assertLess(drain, stops[2][0])
        self.assertTrue(all(call[3] == "--signal=TERM" for _, call in stops))
        self.assertTrue(value.barrier()["barrier_continuous"])

    def test_no_stage_can_skip_drain_or_activate_writers(self):
        self.value.capture()
        with self.assertRaisesRegex(ops.ReleaseError, "INVALID_PHASE"):
            self.value.fence_database()
        self.value.stop_schedulers()
        with self.assertRaisesRegex(ops.ReleaseError, "INVALID_PHASE"):
            self.value.stop_writers()
        self.assertFalse(hasattr(self.value, "activate"))

    def test_failed_or_truthy_drain_cannot_reach_writers_stopped(self):
        self.value.capture()
        self.value.stop_schedulers()
        self.docker.drain_result = {"status": "PASS", "drained": 1}
        with self.assertRaisesRegex(ops.ReleaseError, "DRAIN_FAILED"):
            self.value.drain_workers()
        self.assertTrue(self.docker.containers[self.docker.by_service["ai_worker"]]["State"]["Running"])

    def test_stop_timeout_never_escalates_to_sigkill(self):
        self.value.capture()
        self.docker.keep_running = {self.docker.by_service["ai_scheduler"]}
        with patch.object(barrier.time, "monotonic", side_effect=[0, 2]), self.assertRaisesRegex(ops.ReleaseError, "STOP_TIMEOUT"):
            self.value.stop_schedulers()
        signals = [call[3] for call in self.docker.calls if call[:3] == ["docker", "container", "kill"]]
        self.assertEqual(signals, ["--signal=TERM"])

    def test_current_stopped_state_cannot_hide_a_restart_or_bad_exit(self):
        value = self.fenced()
        target = self.docker.containers[self.docker.by_service["backend"]]
        for key, replacement in (("RestartCount", 1), ("RestartCount", True)):
            old = target[key]
            target[key] = replacement
            with self.subTest(key=key, replacement=replacement), self.assertRaises(ops.ReleaseError):
                value.barrier()
            target[key] = old
        for key, replacement in (("StartedAt", "2026-01-01T00:00:01Z"), ("FinishedAt", "2026-01-01T00:02:00Z"), ("ExitCode", 137), ("ExitCode", False), ("OOMKilled", True)):
            old = target["State"][key]
            target["State"][key] = replacement
            with self.subTest(key=key), self.assertRaises(ops.ReleaseError):
                value.barrier()
            target["State"][key] = old

    def test_fence_requires_interfaces_and_actual_host_port_unreachable(self):
        value = self.fenced()
        self.docker.interface_override = "eth0\nlo\n"
        with self.assertRaisesRegex(ops.ReleaseError, "DATABASE_NOT_FENCED"):
            value.barrier()
        self.docker.interface_override = None
        self.docker.host_open_after_fence = True
        with self.assertRaisesRegex(ops.ReleaseError, "HOST_PROBE_FAILED"):
            value.barrier()

    def test_unknown_and_indirect_namespace_sharers_fail_closed(self):
        value = self.fenced()
        helper = copy.deepcopy(self.docker.postgres)
        helper_id = "c" * 64
        helper.update(Id=helper_id, Name="/helper")
        helper["HostConfig"]["NetworkMode"] = "container:" + value.postgres_id
        self.docker.containers[helper_id] = helper
        with self.assertRaisesRegex(ops.ReleaseError, "NAMESPACE_SHARED"):
            value.barrier()
        value.allowed_helpers.add(helper_id)
        value.barrier()
        indirect = copy.deepcopy(helper)
        indirect_id = "d" * 64
        indirect.update(Id=indirect_id, Name="/indirect")
        indirect["HostConfig"]["NetworkMode"] = "container:helper"
        self.docker.containers[indirect_id] = indirect
        with self.assertRaisesRegex(ops.ReleaseError, "NAMESPACE_SHARED"):
            value.barrier()
        del self.docker.containers[indirect_id]
        del self.docker.containers[helper_id]
        value.barrier()  # Known IDs remain in the history registry after cleanup.

    def test_dead_monitor_cannot_be_replaced_by_a_current_snapshot(self):
        value = self.fenced()
        value.audit.healthy = False
        with self.assertRaisesRegex(ops.ReleaseError, "CONTINUITY_LOST"):
            value.barrier()

    def test_containment_stops_and_fences_but_never_restarts(self):
        self.value.capture()
        result = self.value.contain()
        self.assertTrue(result["database_fenced"])
        self.assertTrue(result["writers_stopped"])
        self.assertFalse(any(call[1] in {"start", "restart"} for call in self.docker.calls))

    def test_containment_still_stops_and_fences_if_monitor_cannot_start(self):
        self.value.capture()
        with patch.object(self.value.audit, "start", side_effect=ops.ReleaseError("WRITER_BARRIER_EVENT_MONITOR_FAILED")), self.assertRaisesRegex(ops.ReleaseError, "DATABASE_NOT_FENCED"):
            self.value.contain()
        self.assertTrue(self.docker.fenced)
        self.assertTrue(all(not self.docker.containers[self.docker.by_service[name]]["State"]["Running"] for name in ops.APP))
        self.assertFalse(any(call[1] in {"start", "restart"} for call in self.docker.calls))


class EventAuditTests(unittest.TestCase):
    def setUp(self):
        self.postgres_id = "a" * 64
        self.app_id = "b" * 64
        self.allowed = set()
        self.audit = barrier.EventAudit("operation-1", {self.postgres_id, self.app_id}, self.postgres_id, self.allowed)
        self.audit.process = FakeProcess()

    def event(self, kind, action, identifier, **attributes):
        return {"Type": kind, "Action": action, "Actor": {"ID": identifier, "Attributes": attributes}}

    def test_start_replays_sentinels_emitted_before_docker_subscribes(self):
        # Deterministically delay subscription until both sentinel events exist.
        # The live-only control loses them, reproducing the startup race without
        # depending on thread scheduling or a real Docker daemon.
        for live_only in (False, True):
            with self.subTest(live_only=live_only):
                audit = barrier.EventAudit("startup", {self.postgres_id}, self.postgres_id, set())
                started_at = datetime(2026, 11, 22, 15, 8, 20, 123456, tzinfo=UTC)
                history = []
                order = []
                subscribe_from = None

                def now(zone):
                    self.assertIs(zone, UTC)
                    order.append("timestamp")
                    return started_at

                def popen(args, **kwargs):
                    nonlocal subscribe_from
                    self.assertEqual(order, ["timestamp"])
                    order.append("spawn")
                    if "--since" in args and not live_only:
                        subscribe_from = datetime.fromisoformat(args[args.index("--since") + 1])
                    return FakeProcess()

                def command(args):
                    action = "create" if args[2] == "create" else "destroy"
                    order.append(action)
                    event_time = started_at + timedelta(microseconds=len(history) + 1)
                    history.append((event_time, self.event("volume", action, args[3])))
                    return args[3]

                def subscribe(timeout):
                    self.assertEqual(order, ["timestamp", "spawn", "create", "destroy"])
                    order.append("subscribe")
                    for event_time, event in history:
                        if subscribe_from is not None and event_time >= subscribe_from:
                            audit.observe(event)

                with patch.object(barrier, "datetime") as clock, \
                     patch.object(barrier.subprocess, "Popen", side_effect=popen) as spawn, \
                     patch.object(barrier.threading, "Thread"), \
                     patch.object(ops, "command", side_effect=command), \
                     patch.object(audit.condition, "wait", side_effect=subscribe), \
                     patch.object(barrier.time, "monotonic", side_effect=[0, 1, 11]):
                    clock.now.side_effect = now
                    if live_only:
                        with self.assertRaisesRegex(ops.ReleaseError, "EVENT_SYNC_FAILED"):
                            audit.start()
                    else:
                        audit.start()
                        audit.check()
                        self.assertEqual(subscribe_from, started_at)
                        self.assertEqual(audit.events_observed, 2)
                    spawn.assert_called_once()
                self.assertEqual(order[-1], "subscribe")
                self.assertFalse(audit.volumes)
                self.assertFalse(audit.seen)

    def test_fresh_volume_sentinel_proves_delivery_and_is_removed(self):
        calls = []
        def command(args):
            calls.append(args)
            self.audit.observe(self.event("volume", "create" if args[2] == "create" else "destroy", args[3]))
            return args[3]
        with patch.object(ops, "command", side_effect=command):
            self.audit.sync()
            self.audit.arm()
            self.audit.sync()
        self.assertEqual(len(calls), 6)
        self.assertTrue(self.audit.sealed)
        self.assertFalse(self.audit.volumes)
        self.assertFalse(self.audit.seen)
        self.assertEqual(self.audit.events_observed, 6)

    def test_missing_stream_ack_is_not_treated_as_continuity(self):
        with patch.object(ops, "command", return_value=""), self.assertRaisesRegex(ops.ReleaseError, "EVENT_SYNC_FAILED"):
            self.audit.sync(timeout=0)
        self.assertFalse(self.audit.volumes)
        self.assertFalse(self.audit.healthy)

    def test_sync_timeout_after_arm_cannot_be_repaired_by_later_delivery(self):
        pending = []
        delivery = True

        def command(args):
            action = "create" if args[2] == "create" else "destroy"
            event = self.event("volume", action, args[3])
            if delivery:
                self.audit.observe(event)
            else:
                pending.append(event)
            return args[3]

        with patch.object(ops, "command", side_effect=command) as run:
            self.audit.arm()
            delivery = False
            with self.assertRaisesRegex(ops.ReleaseError, "EVENT_SYNC_FAILED"):
                self.audit.sync(timeout=0)
            self.assertFalse(self.audit.healthy)
            self.assertIsNone(self.audit.process.poll())
            delivery = True
            for event in pending:
                self.audit.observe(event)
            self.assertEqual(self.audit.events_observed, 4)
            # A responsive stream and late sentinel acknowledgements cannot
            # repair the lost interval; no new synchronization is attempted.
            for operation in (self.audit.check, self.audit.sync, self.audit.arm):
                with self.assertRaisesRegex(ops.ReleaseError, "CONTINUITY_LOST"):
                    operation()
            self.assertEqual(run.call_count, 4)

    def test_closed_stream_after_arm_cannot_restart_or_rearm_the_audit(self):
        def command(args):
            action = "create" if args[2] == "create" else "destroy"
            self.audit.observe(self.event("volume", action, args[3]))
            return args[3]
        with patch.object(ops, "command", side_effect=command):
            self.audit.arm()
        self.assertTrue(self.audit.sealed)
        self.audit._read()  # EOF while the process itself still reports running.
        with patch.object(barrier.subprocess, "Popen") as spawn, patch.object(ops, "command") as run:
            for operation in (self.audit.sync, self.audit.arm):
                with self.assertRaisesRegex(ops.ReleaseError, "CONTINUITY_LOST"):
                    operation()
            with self.assertRaisesRegex(ops.ReleaseError, "INVALID_PHASE"):
                self.audit.start()
            spawn.assert_not_called()
            run.assert_not_called()

    def test_transient_reconnect_and_writer_start_remain_invalid_after_stop(self):
        self.audit.sealed = True
        self.audit.observe(self.event("network", "connect", "f" * 64, container=self.postgres_id))
        self.audit.observe(self.event("network", "disconnect", "f" * 64, container=self.postgres_id))
        with self.assertRaisesRegex(ops.ReleaseError, "CONTINUITY_LOST"):
            self.audit.check()

    def test_registered_helper_may_start_but_unregistered_transient_container_cannot(self):
        self.audit.sealed = True
        known, unknown = "c" * 64, "d" * 64
        self.allowed.add(known)
        self.audit.observe(self.event("container", "start", known))
        self.audit.observe(self.event("container", "die", known))
        self.audit.check()
        self.audit.observe(self.event("container", "start", unknown))
        self.audit.observe(self.event("container", "destroy", unknown))
        with self.assertRaisesRegex(ops.ReleaseError, "CONTINUITY_LOST"):
            self.audit.check()

    def test_pg_exec_events_allowed_but_pg_restart_and_app_update_are_not(self):
        self.audit.sealed = True
        self.audit.observe(self.event("container", "exec_start", self.postgres_id))
        self.audit.check()
        self.audit.observe(self.event("container", "update", self.app_id))
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()

    def test_process_death_or_malformed_event_invalidates_history(self):
        self.audit.process.status = 0
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()
        self.audit.process.status = None
        self.audit.observe({"unexpected": "untrusted payload"})
        with self.assertRaises(ops.ReleaseError):
            self.audit.check()


class DrainControlTests(unittest.TestCase):
    def execute(self, *, reserved=None, queues_after=None, extra_node=False, reply_ok=True):
        expected = {"celery@worker": "ai"}
        calls = []
        class Inspector:
            queue_calls = 0
            def active_queues(self):
                self.queue_calls += 1
                value = [{"name": "ai"}] if self.queue_calls == 1 else (queues_after or [])
                return {"celery@worker": value, **({"unexpected": []} if extra_node else {})}
            def active(self, safe=False):
                calls.append(("active", safe))
                return {"celery@worker": []}
            def reserved(self):
                return {"celery@worker": reserved or []}
            def scheduled(self):
                return {"celery@worker": []}
        inspector = Inspector()
        class Control:
            def inspect(self, timeout):
                return inspector
            def cancel_consumer(self, queue, **kwargs):
                calls.append((queue, kwargs))
                return [{"celery@worker": {"ok": "no longer consuming from ai"} if reply_ok else {"error": "private-task-payload"}}]
        module = types.ModuleType("app.workers.celery_app")
        module.celery_app = types.SimpleNamespace(control=Control())
        output = io.StringIO()
        with patch.dict(sys.modules, {"app.workers.celery_app": module}), patch.object(sys, "argv", ["drain", json.dumps(expected), "1"]), patch("time.monotonic", side_effect=[0, 0.1, 2]), patch("time.sleep"), redirect_stdout(output):
            exec(barrier.DRAIN_CODE, {})
        return json.loads(output.getvalue()), calls

    def test_all_consumers_and_all_inflight_categories_must_be_empty(self):
        report, calls = self.execute()
        self.assertEqual(report, {"status": "PASS", "drained": True})
        self.assertIn(("active", True), calls)
        for kwargs in ({"reserved": [{"secret": "private-task-payload"}]}, {"queues_after": [{"name": "ai"}]}, {"extra_node": True}, {"reply_ok": False}):
            with self.subTest(kwargs=kwargs):
                report, _ = self.execute(**kwargs)
                self.assertEqual(report, {"status": "FAIL", "drained": False})
                self.assertNotIn("private-task-payload", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
