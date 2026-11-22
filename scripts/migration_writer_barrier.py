"""Stop writers and fence PostgreSQL without ever reactivating a runtime.

The caller owns approval consumption, the shared operation lock and durable
state. capture() is read-only; every later lifecycle method requires the caller
to have consumed approval. Docker events are observed only in memory. A dead or
unresponsive observer makes historical barrier continuity unknowable.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any

import release_ops as ops

ERROR_CODES = frozenset({
    "WRITER_BARRIER_INVALID_OPERATION", "WRITER_BARRIER_INVALID_DEADLINE",
    "WRITER_BARRIER_INVALID_PHASE", "WRITER_BARRIER_INVALID_TOPOLOGY",
    "WRITER_BARRIER_CONTAINER_CHANGED", "WRITER_BARRIER_NETWORK_UNSUPPORTED",
    "WRITER_BARRIER_HOST_PROBE_FAILED", "WRITER_BARRIER_WRITERS_NOT_STOPPED",
    "WRITER_BARRIER_UNCLEAN_STOP", "WRITER_BARRIER_STOP_TIMEOUT",
    "WRITER_BARRIER_DRAIN_FAILED", "WRITER_BARRIER_DATABASE_NOT_FENCED",
    "WRITER_BARRIER_NAMESPACE_SHARED", "WRITER_BARRIER_CONTINUITY_LOST",
    "WRITER_BARRIER_EVENT_MONITOR_FAILED", "WRITER_BARRIER_EVENT_SYNC_FAILED",
    "WRITER_BARRIER_AUDIT_CLEANUP_FAILED",
})
_ID = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_WORKERS = tuple(name for name in ops.WORKERS if name.endswith("_worker"))

# The child only emits counts and closed error codes, never task payloads.
DRAIN_CODE = r'''
import json,sys,time
from app.workers.celery_app import celery_app
def execute():
 expected=json.loads(sys.argv[1]); deadline=time.monotonic()+int(sys.argv[2])
 inspector=celery_app.control.inspect(timeout=2)
 queues=inspector.active_queues() or {}
 if set(queues)!=set(expected): return False
 for node,queue in expected.items():
  if type(queues[node]) is not list or len(queues[node])!=1 or queues[node][0].get('name')!=queue: return False
 for node,queue in sorted(expected.items()):
  response=celery_app.control.cancel_consumer(queue,destination=[node],reply=True,timeout=3)
  if type(response) is not list or len(response)!=1 or set(response[0])!={node}: return False
  if response[0][node]!={'ok':'no longer consuming from '+queue}: return False
 while time.monotonic()<deadline:
  values=[inspector.active_queues(),inspector.active(safe=True),inspector.reserved(),inspector.scheduled()]
  if any(type(value) is not dict or set(value)!=set(expected) for value in values): return False
  if any(type(items) is not list for value in values for items in value.values()): return False
  if all(not items for value in values for items in value.values()): return True
  time.sleep(0.5)
 return False
try:
 ok=execute()
except Exception:
 ok=False
print(json.dumps({'status':'PASS' if ok else 'FAIL','drained':ok}))
'''


def _hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _valid_time(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.year > 1970 and parsed.utcoffset() is not None
    except ValueError:
        return False


class EventAudit:
    """Fail-closed, live Docker event observation with round-trip sentinels."""

    def __init__(self, operation_id: str, protected: set[str], postgres_id: str,
                 allowed_helpers: set[str]):
        self.protected = protected
        self.postgres_id = postgres_id
        self.allowed_helpers = allowed_helpers
        self.prefix = "iteam-migration-audit-" + hashlib.sha256(operation_id.encode()).hexdigest()[:12]
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.condition = threading.Condition()
        self.healthy = True
        self.sealed = False
        self.closed = False
        self.events_observed = 0
        self.seen: set[tuple[str, str]] = set()
        self.volumes: set[str] = set()

    def observe(self, value: Any) -> None:
        with self.condition:
            if not (type(value) is dict and isinstance(value.get("Type"), str)
                    and isinstance(value.get("Action"), str) and type(value.get("Actor")) is dict
                    and isinstance(value["Actor"].get("ID"), str)):
                self.healthy = False
                self.condition.notify_all()
                return
            kind, action, actor = value["Type"], value["Action"], value["Actor"]
            identifier = actor["ID"]
            self.events_observed += 1
            if kind == "volume" and identifier in self.volumes and action in {"create", "destroy"}:
                self.seen.add((identifier, action))
            if self.sealed:
                if kind == "network" and action in {"connect", "disconnect"}:
                    attributes = actor.get("Attributes") or {}
                    if type(attributes) is not dict or attributes.get("container") == self.postgres_id:
                        self.healthy = False
                if kind == "container":
                    if identifier in self.protected and action in {
                        "start", "restart", "die", "destroy", "kill", "pause", "unpause", "update", "rename"
                    }:
                        self.healthy = False
                    # Even a short-lived, subsequently removed namespace sharer
                    # must not evade an inspect-based membership check.
                    if action in {"start", "restart", "unpause"} and identifier not in self.allowed_helpers:
                        self.healthy = False
            self.condition.notify_all()

    def _read(self) -> None:
        try:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                self.observe(json.loads(line))
        except Exception:
            pass
        finally:
            with self.condition:
                if not self.closed:
                    self.healthy = False
                self.condition.notify_all()

    def start(self) -> None:
        ops.require(self.process is None, "WRITER_BARRIER_INVALID_PHASE")
        # Popen does not wait for Docker to subscribe. Replay from before spawn
        # so the first sentinel cannot race the event stream's connection. This
        # is only startup synchronization, never recovery of a lost live fence.
        since = datetime.now(UTC).isoformat()
        try:
            self.process = subprocess.Popen(
                ["docker", "events", "--since", since, "--format", "{{json .}}"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError:
            raise ops.ReleaseError("WRITER_BARRIER_EVENT_MONITOR_FAILED") from None
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        self.sync()

    def check(self) -> None:
        ops.require(
            self.healthy and not self.closed and self.process is not None
            and self.process.poll() is None,
            "WRITER_BARRIER_CONTINUITY_LOST",
        )

    def sync(self, timeout: float = 10) -> None:
        self.check()
        name = self.prefix + "-" + secrets.token_hex(6)
        with self.condition:
            self.volumes.add(name)
        removed = False
        try:
            ops.command(["docker", "volume", "create", name])
            ops.command(["docker", "volume", "rm", name])
            removed = True
            deadline = time.monotonic() + timeout
            with self.condition:
                while not {(name, "create"), (name, "destroy")} <= self.seen:
                    self.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        # Later delivery cannot retroactively prove that the
                        # observer continuously covered this operation.
                        self.healthy = False
                        raise ops.ReleaseError("WRITER_BARRIER_EVENT_SYNC_FAILED")
                    self.condition.wait(min(remaining, 0.25))
                self.check()
        finally:
            if not removed:
                try:
                    ops.command(["docker", "volume", "rm", name])
                    removed = True
                except ops.ReleaseError:
                    pass
            with self.condition:
                if removed:
                    self.volumes.discard(name)
                self.seen.discard((name, "create"))
                self.seen.discard((name, "destroy"))

    def arm(self) -> None:
        self.sync()
        with self.condition:
            self.check()
            self.sealed = True

    def close(self) -> None:
        self.closed = True
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            if self.process.stdout is not None:
                self.process.stdout.close()
        if self.thread is not None:
            self.thread.join(timeout=3)
        failed = False
        for name in tuple(self.volumes):
            try:
                ops.command(["docker", "volume", "rm", name])
                self.volumes.discard(name)
            except ops.ReleaseError:
                failed = True
        ops.require(not failed, "WRITER_BARRIER_AUDIT_CLEANUP_FAILED")


class WriterBarrier:
    def __init__(self, runtime: ops.Runtime, operation_id: str, *, deadline_seconds: int = 210):
        ops.require(
            isinstance(operation_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", operation_id),
            "WRITER_BARRIER_INVALID_OPERATION",
        )
        ops.require(type(deadline_seconds) is int and 1 <= deadline_seconds <= 300,
                    "WRITER_BARRIER_INVALID_DEADLINE")
        self.runtime = runtime
        self.operation_id = operation_id
        self.deadline_seconds = deadline_seconds
        self.allowed_helpers: set[str] = set()
        self.postgres_id = ""
        self.source_image_ids: dict[str, str] = {}
        self.safe_baseline: dict[str, Any] = {}
        self._raw: dict[str, dict[str, Any]] = {}
        self._ids: dict[str, str] = {}
        self._host_probes: list[tuple[str, int]] = []
        self._stopped: dict[str, dict[str, Any]] = {}
        self._phase = "new"
        self.audit: EventAudit | None = None

    def _phase_is(self, *phases: str) -> None:
        ops.require(self._phase in phases, "WRITER_BARRIER_INVALID_PHASE")

    def _inspect(self) -> dict[str, dict[str, Any]]:
        values = json.loads(ops.command(["docker", "inspect", *self._ids.values()]))
        ops.require(type(values) is list and len(values) == len(self._ids), "WRITER_BARRIER_CONTAINER_CHANGED")
        by_id = {value["Id"]: value for value in values}
        ops.require(set(by_id) == set(self._ids.values()), "WRITER_BARRIER_CONTAINER_CHANGED")
        return {name: by_id[identifier] for name, identifier in self._ids.items()}

    @staticmethod
    def _connects(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def capture(self) -> dict[str, Any]:
        self._phase_is("new")
        configured = json.loads(self.runtime.compose("config", "--format", "json"))
        project = configured.get("name")
        ops.require(isinstance(project, str) and _NAME.fullmatch(project), "WRITER_BARRIER_INVALID_TOPOLOGY")
        containers: dict[str, Any] = {}
        for name in sorted(ops.ALL):
            data = self.runtime.inspect(name)
            labels = data["Config"].get("Labels") or {}
            identifier, image = data["Id"], data["Image"]
            state = data["State"]
            ops.require(
                _ID.fullmatch(identifier) and _IMAGE.fullmatch(image)
                and labels.get("com.docker.compose.project") == project
                and labels.get("com.docker.compose.service") == name
                and state.get("Running") is True and state.get("Paused") is False
                and state.get("Restarting") is False and state.get("OOMKilled") is False
                and _valid_time(state.get("StartedAt"))
                and type(data.get("RestartCount")) is int and data["RestartCount"] >= 0,
                "WRITER_BARRIER_INVALID_TOPOLOGY",
            )
            policy = data["HostConfig"].get("RestartPolicy")
            ops.require(
                type(policy) is dict and set(policy) == {"Name", "MaximumRetryCount"}
                and policy["Name"] in {"", "no", "always", "unless-stopped", "on-failure"}
                and type(policy["MaximumRetryCount"]) is int,
                "WRITER_BARRIER_INVALID_TOPOLOGY",
            )
            self._raw[name], self._ids[name] = data, identifier
            self.source_image_ids[name] = image
            containers[name] = {
                "id": identifier, "image_id": image, "started_at": state["StartedAt"],
                "restart_count": data["RestartCount"], "restart_policy": policy,
            }
        ops.require(len(set(self._ids.values())) == len(ops.ALL), "WRITER_BARRIER_INVALID_TOPOLOGY")
        project_ids = ops.command([
            "docker", "ps", "--filter", "label=com.docker.compose.project=" + project,
            "--quiet", "--no-trunc",
        ]).splitlines()
        ops.require(set(project_ids) == set(self._ids.values()) and len(project_ids) == len(self._ids),
                    "WRITER_BARRIER_INVALID_TOPOLOGY")
        self.postgres_id = self._ids["postgres"]
        postgres = self._raw["postgres"]
        networks = postgres["NetworkSettings"].get("Networks")
        ops.require(type(networks) is dict and networks, "WRITER_BARRIER_NETWORK_UNSUPPORTED")
        safe_networks = {}
        for name, endpoint in networks.items():
            ops.require(
                _NAME.fullmatch(name) and name not in {"bridge", "host", "none"}
                and type(endpoint) is dict and _ID.fullmatch(endpoint.get("NetworkID", ""))
                and endpoint.get("DriverOpts") in (None, {}) and not endpoint.get("Links"),
                "WRITER_BARRIER_NETWORK_UNSUPPORTED",
            )
            network = json.loads(ops.command(["docker", "network", "inspect", endpoint["NetworkID"]]))[0]
            ops.require(network.get("Driver") == "bridge" and network.get("Scope") == "local",
                        "WRITER_BARRIER_NETWORK_UNSUPPORTED")
            safe_networks[name] = {
                key: endpoint.get(key) for key in (
                    "NetworkID", "EndpointID", "Aliases", "IPAMConfig", "IPAddress", "IPPrefixLen",
                    "Gateway", "GlobalIPv6Address", "GlobalIPv6PrefixLen", "IPv6Gateway", "MacAddress",
                )
            }
        ports = postgres["NetworkSettings"].get("Ports") or {}
        ops.require(set(ports) <= {"5432/tcp"}, "WRITER_BARRIER_NETWORK_UNSUPPORTED")
        bindings = ports.get("5432/tcp") or []
        for item in bindings:
            ops.require(type(item) is dict and set(item) == {"HostIp", "HostPort"}, "WRITER_BARRIER_NETWORK_UNSUPPORTED")
            host = item["HostIp"]
            host = "127.0.0.1" if host in {"", "0.0.0.0"} else "::1" if host == "::" else host
            port = int(item["HostPort"])
            ops.require(1 <= port <= 65535, "WRITER_BARRIER_NETWORK_UNSUPPORTED")
            self._host_probes.append((host, port))
        if not self._host_probes:
            self._host_probes = [(item["IPAddress"], 5432) for item in networks.values() if item.get("IPAddress")]
        self._host_probes = sorted(set(self._host_probes))
        ops.require(self._host_probes and all(self._connects(*probe) for probe in self._host_probes),
                    "WRITER_BARRIER_HOST_PROBE_FAILED")
        self._namespace_members()
        self.safe_baseline = {
            "version": 1, "operation_id": self.operation_id, "project": project,
            "containers": containers, "postgres_networks": safe_networks,
            "postgres_host_bindings": bindings,
        }
        self.audit = EventAudit(self.operation_id, {self._ids[name] for name in (*ops.APP, "postgres")},
                                self.postgres_id, self.allowed_helpers)
        self._phase = "captured"
        return json.loads(json.dumps(self.safe_baseline))

    def _disable_restart(self) -> None:
        current = self._inspect()
        changed = [self._ids[name] for name in (*ops.APP, "postgres")
                   if current[name]["HostConfig"]["RestartPolicy"].get("Name") != "no"]
        if changed:
            ops.command(["docker", "update", "--restart=no", *changed])

    def _stop(self, names: tuple[str, ...]) -> None:
        current = self._inspect()
        running = [self._ids[name] for name in names if current[name]["State"]["Running"]]
        if running:
            # No deadline-triggered SIGKILL: an unfinished warm shutdown is an
            # explicit failure requiring operator intervention.
            ops.command(["docker", "container", "kill", "--signal=TERM", *running])
        deadline = time.monotonic() + self.deadline_seconds
        while True:
            current = self._inspect()
            if all(not current[name]["State"]["Running"] for name in names):
                for name in names:
                    self._validate_stopped(name, current[name])
                return
            ops.require(time.monotonic() < deadline, "WRITER_BARRIER_STOP_TIMEOUT")
            time.sleep(0.5)

    def _validate_identity(self, name: str, data: dict[str, Any]) -> None:
        old = self._raw[name]
        ops.require(
            data["Id"] == old["Id"] and data["Image"] == old["Image"]
            and data["State"]["StartedAt"] == old["State"]["StartedAt"]
            and type(data["RestartCount"]) is int
            and data["RestartCount"] == old["RestartCount"]
            and data["HostConfig"]["RestartPolicy"].get("Name") == "no",
            "WRITER_BARRIER_CONTAINER_CHANGED",
        )

    def _validate_stopped(self, name: str, data: dict[str, Any]) -> None:
        self._validate_identity(name, data)
        state = data["State"]
        ops.require(state["Running"] is False and state.get("Paused") is False
                    and state.get("Restarting") is False, "WRITER_BARRIER_WRITERS_NOT_STOPPED")
        ops.require(state.get("OOMKilled") is False and type(state.get("ExitCode")) is int
                    and state["ExitCode"] in {0, 143}
                    and not state.get("Error") and _valid_time(state.get("FinishedAt")),
                    "WRITER_BARRIER_UNCLEAN_STOP")
        ops.require(datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00"))
                    >= datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00")),
                    "WRITER_BARRIER_UNCLEAN_STOP")
        result = {"started_at": state["StartedAt"], "finished_at": state["FinishedAt"],
                  "restart_count": data["RestartCount"]}
        if name in self._stopped:
            ops.require(result == self._stopped[name], "WRITER_BARRIER_CONTINUITY_LOST")
        else:
            self._stopped[name] = result

    def stop_schedulers(self) -> None:
        self._phase_is("captured")
        assert self.audit is not None
        self.audit.start()
        self._disable_restart()
        self._stop(tuple(ops.SCHEDULERS))
        self._stop(("frontend", "backend"))
        self._phase = "ingress_stopped"

    def drain_workers(self) -> None:
        self._phase_is("ingress_stopped")
        expected = {}
        for name in _WORKERS:
            hostname = self._raw[name]["Config"]["Hostname"]
            ops.require(isinstance(hostname, str) and _NAME.fullmatch(hostname), "WRITER_BARRIER_INVALID_TOPOLOGY")
            expected["celery@" + hostname] = name.removesuffix("_worker")
        ops.require(len(expected) == len(_WORKERS), "WRITER_BARRIER_INVALID_TOPOLOGY")
        report = json.loads(ops.command([
            "docker", "exec", self._ids["ai_worker"], "python", "-c", DRAIN_CODE,
            json.dumps(expected, sort_keys=True), str(self.deadline_seconds),
        ]))
        ops.require(report == {"status": "PASS", "drained": True}
                    and report.get("drained") is True, "WRITER_BARRIER_DRAIN_FAILED")
        self._phase = "drained"

    def stop_writers(self) -> None:
        self._phase_is("drained")
        self._stop(_WORKERS)
        for name, data in self._inspect().items():
            if name in ops.APP:
                self._validate_stopped(name, data)
        self._phase = "writers_stopped"

    def _namespace_members(self) -> None:
        identifiers = ops.command(["docker", "ps", "--all", "--quiet"]).splitlines()
        ops.require(identifiers, "WRITER_BARRIER_CONTAINER_CHANGED")
        fmt = '[{{json .Id}},{{json .Name}},{{json .HostConfig.NetworkMode}},{{json .HostConfig.RestartPolicy.Name}}]'
        rows = [json.loads(line) for line in ops.command(["docker", "inspect", "--format", fmt, *identifiers]).splitlines()]
        ops.require(
            len(rows) == len(identifiers) and all(
                type(row) is list and len(row) == 4 and all(isinstance(item, str) for item in row)
                and _ID.fullmatch(row[0]) for row in rows
            ), "WRITER_BARRIER_NAMESPACE_SHARED",
        )
        by_id = {row[0]: row for row in rows}
        ops.require(len(by_id) == len(rows) and set(self._ids.values()) <= set(by_id),
                    "WRITER_BARRIER_NAMESPACE_SHARED")

        def resolve(reference: str) -> str:
            matches = [identifier for identifier, row in by_id.items()
                       if identifier.startswith(reference) or row[1].lstrip("/") == reference]
            ops.require(len(matches) == 1, "WRITER_BARRIER_NAMESPACE_SHARED")
            return matches[0]

        for identifier, row in by_id.items():
            seen, cursor = set(), identifier
            while by_id[cursor][2].startswith("container:"):
                ops.require(cursor not in seen, "WRITER_BARRIER_NAMESPACE_SHARED")
                seen.add(cursor)
                cursor = resolve(by_id[cursor][2].split(":", 1)[1])
            if cursor == self.postgres_id and identifier != self.postgres_id:
                ops.require(identifier in self.allowed_helpers and row[3] == "no",
                            "WRITER_BARRIER_NAMESPACE_SHARED")

    def _fence_checks(self) -> dict[str, dict[str, Any]]:
        current = self._inspect()
        for name in ops.APP:
            self._validate_stopped(name, current[name])
        postgres = current["postgres"]
        self._validate_identity("postgres", postgres)
        ops.require(postgres["State"]["Running"] is True and not postgres["State"].get("Paused")
                    and postgres["NetworkSettings"].get("Networks") == {},
                    "WRITER_BARRIER_DATABASE_NOT_FENCED")
        interfaces = ops.command(["docker", "exec", self.postgres_id, "sh", "-c", "ls /sys/class/net"]).split()
        ops.require(interfaces == ["lo"], "WRITER_BARRIER_DATABASE_NOT_FENCED")
        routes = ops.command(["docker", "exec", self.postgres_id, "cat", "/proc/net/route"]).splitlines()
        ops.require(all(not line.split() or line.split()[0] == "lo" for line in routes[1:]),
                    "WRITER_BARRIER_DATABASE_NOT_FENCED")
        self._namespace_members()
        ops.require(all(not self._connects(*probe) for probe in self._host_probes),
                    "WRITER_BARRIER_HOST_PROBE_FAILED")
        return current

    def fence_database(self) -> None:
        self._phase_is("writers_stopped")
        assert self.audit is not None
        self.audit.sync()
        current = self._inspect()["postgres"]
        self._validate_identity("postgres", current)
        for endpoint in current["NetworkSettings"]["Networks"].values():
            ops.command(["docker", "network", "disconnect", endpoint["NetworkID"], self.postgres_id])
        self._fence_checks()
        self.audit.arm()
        self._phase = "fenced"
        self.barrier()

    def barrier(self) -> dict[str, Any]:
        self._phase_is("fenced")
        assert self.audit is not None
        self.audit.sync()
        self._fence_checks()
        self.audit.sync()
        result = {
            "version": 1, "operation_id": self.operation_id,
            "database_fenced": True, "writers_stopped": True, "barrier_continuous": True,
            "postgres_id": self.postgres_id, "baseline_sha256": _hash(self.safe_baseline),
            "stopped_writers": self._stopped,
        }
        result["barrier_sha256"] = _hash(result)
        return json.loads(json.dumps(result))

    def contain(self) -> dict[str, Any]:
        """Never restart services; failure to establish a barrier stays explicit."""
        self._phase_is("captured", "ingress_stopped", "drained", "writers_stopped", "fenced")
        assert self.audit is not None
        failures = []
        if self.audit.process is None:
            try:
                self.audit.start()
            except (ops.ReleaseError, OSError, ValueError, KeyError, TypeError):
                # Failure to prove history must not prevent best-effort physical
                # containment. It still prevents returning a successful barrier.
                failures.append(True)
        for action in (self._disable_restart, lambda: self._stop(tuple(ops.APP))):
            try:
                action()
            except (ops.ReleaseError, OSError, ValueError, KeyError, TypeError):
                failures.append(True)
        try:
            current = self._inspect()["postgres"]
            for endpoint in current["NetworkSettings"]["Networks"].values():
                ops.command(["docker", "network", "disconnect", endpoint["NetworkID"], self.postgres_id])
            self._fence_checks()
        except (ops.ReleaseError, OSError, ValueError, KeyError, TypeError):
            failures.append(True)
        ops.require(not failures, "WRITER_BARRIER_DATABASE_NOT_FENCED")
        if not self.audit.sealed:
            self.audit.arm()
        self._phase = "fenced"
        return self.barrier()

    def close(self) -> None:
        if self.audit is not None:
            self.audit.close()
