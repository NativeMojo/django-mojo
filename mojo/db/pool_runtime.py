"""PID-owned sampler and disabled-by-default local exhaustion probe."""

import json
import os
import socket
import threading
import time
from pathlib import Path

from django.conf import settings
from django.db import connections

from mojo.db import config
from mojo.db.errors import bounded_error
from mojo.db.pool_telemetry import (
    append_bounded_event,
    atomic_write,
    pool_snapshot,
    should_emit_state_event,
)
from mojo.helpers.async_db import database_connection_boundary


class PoolRuntime:
    """One sampler per ASGI worker; all output stays local and best-effort."""

    def __init__(self, interval=5, root=None):
        self.interval = max(1, int(interval))
        self.root = Path(root or os.environ.get("MOJO_POOL_TELEMETRY_ROOT", "/tmp/mojo-pool"))
        self.pid = os.getpid()
        self.stop_event = threading.Event()
        self.thread = None
        self.previous = None
        self.probe = None
        self.probe_thread = None
        self.last_event_at = None

    def enabled(self):
        plan = config.LAST_POOL_PLAN or {}
        return bool(
            plan.get("enabled") and plan.get("valid")
            and plan.get("role") == "api" and plan.get("launcher") == "asgi")

    def sample_once(self):
        if not self.enabled():
            return None
        wrapper = connections["default"]
        pool = wrapper.pool
        previous_state = (self.previous or {}).get("state")
        snapshot = pool_snapshot(pool, identity=config.LAST_POOL_PLAN.get("identity"),
                                 previous=self.previous)
        self.previous = snapshot
        atomic_write(self.root / f"worker-{self.pid}.json", snapshot)
        if should_emit_state_event(
                snapshot["state"], previous_state, self.last_event_at, snapshot["at"]):
            append_bounded_event(self.root / f"worker-{self.pid}-events.jsonl", {
                "schema": 1,
                "event": "database_pool_state",
                "at": snapshot["at"],
                "pid": snapshot["pid"],
                "process_uuid": snapshot["process_uuid"],
                "identity": snapshot["identity"],
                "state": snapshot["state"],
                "previous_state": previous_state,
                "gauges": snapshot["gauges"],
                "interval": snapshot["interval"],
                "in_use_or_preparing": snapshot["in_use_or_preparing"],
            })
            self.last_event_at = snapshot["at"]
        return snapshot

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.sample_once()
            except Exception as error:
                try:
                    atomic_write(self.root / f"worker-{self.pid}-error.json", {
                        "schema": 1, "at": time.time(), "pid": self.pid,
                        "event": "pool_sampler_error", "error": bounded_error(error),
                    })
                except Exception:
                    pass
            self.stop_event.wait(self.interval)

    def start(self):
        if not self.enabled() or (self.thread and self.thread.is_alive()):
            return False
        self.pid = os.getpid()
        self.stop_event.clear()
        atomic_write(self.root / f"worker-{self.pid}-runtime.json", {
            "schema": 1, "at": time.time(), "pid": self.pid,
            "event": "pool_runtime_starting",
        })
        if getattr(settings, "DATABASE_POOL_LAB_PROBE_ENABLED", False):
            socket_path = os.environ.get(
                "MOJO_POOL_PROBE_SOCKET", str(self.root / f"probe-{self.pid}.sock"))
            self.probe = LocalPoolProbe(socket_path)
            self.probe.bind()
        self.thread = threading.Thread(
            target=self._run, name="mojo-pool-sampler", daemon=True)
        self.thread.start()
        if self.probe:
            self.probe_thread = threading.Thread(
                target=self.probe.serve_forever,
                name="mojo-pool-lab-probe",
                daemon=True,
            )
            self.probe_thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        if self.probe:
            self.probe.stop()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=max(2, self.interval + 1))
        if self.probe_thread and self.probe_thread.is_alive():
            self.probe_thread.join(timeout=2)
        self.thread = None
        self.probe_thread = None
        self.probe = None


runtime = PoolRuntime()


class LocalPoolProbe:
    """One-worker Unix-socket probe; no HTTP surface and no automatic retry."""

    def __init__(self, socket_path, deadline=15, connection_handler=None):
        self.socket_path = Path(socket_path)
        self.deadline = max(1, min(60, int(deadline)))
        self.connections = connection_handler if connection_handler is not None else connections
        self.cancelled = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.client_slots = threading.BoundedSemaphore(4)
        self.server = None

    def _query(self):
        with database_connection_boundary():
            with self.connections["default"].cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone()[0]

    def exercise(self, leases, hold_seconds=1):
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "error": "probe already running"}
        self.cancelled.clear()
        started = time.monotonic()
        owned = []
        exercise_error = None
        waiter_timeout = False
        return_errors = []
        try:
            pool = self.connections["default"].pool
            if pool is None:
                return {"ok": False, "error": "default connection is not pooled"}
            maximum = int(pool.get_stats().get("pool_max", 0))
            if maximum <= 0:
                return {"ok": False, "error": "pool maximum is unavailable"}
            requested = int(leases)
            if requested != maximum:
                return {
                    "ok": False,
                    "error": f"exhaustion exercise requires exactly pool_max={maximum} leases",
                }
            for _index in range(requested):
                remaining = self.deadline - (time.monotonic() - started)
                if self.cancelled.is_set() or remaining <= 0:
                    break
                owned.append(pool.getconn(timeout=max(0.1, min(2, remaining))))
            if len(owned) == requested and not self.cancelled.is_set():
                remaining = self.deadline - (time.monotonic() - started)
                if remaining > 0:
                    try:
                        unexpected = pool.getconn(timeout=max(0.1, min(1, remaining)))
                    except Exception as error:
                        from mojo.db.errors import is_pool_acquisition_error
                        if is_pool_acquisition_error(error):
                            waiter_timeout = True
                        else:
                            exercise_error = bounded_error(error)
                    else:
                        try:
                            pool.putconn(unexpected)
                        except Exception as error:
                            return_errors.append(bounded_error(error))
                            try:
                                unexpected.close()
                            except Exception:
                                pass
                        exercise_error = "extra waiter unexpectedly acquired a lease"
            remaining = self.deadline - (time.monotonic() - started)
            self.cancelled.wait(max(0, min(float(hold_seconds), remaining)))
        except Exception as error:
            exercise_error = bounded_error(error)
        finally:
            for connection in reversed(owned):
                try:
                    self.connections["default"].pool.putconn(connection)
                except Exception as error:
                    return_errors.append(bounded_error(error))
                    try:
                        connection.close()
                    except Exception:
                        pass
            self.lock.release()
        pool_recovered = False
        pool_stats = {}
        pool = self.connections["default"].pool
        while time.monotonic() - started <= self.deadline:
            try:
                pool_stats = pool.get_stats()
                pool_recovered = bool(
                    pool_stats.get("pool_size", 0) > 0
                    and pool_stats.get("pool_available", 0) == pool_stats.get("pool_size", 0)
                    and pool_stats.get("requests_waiting", 0) == 0
                )
            except Exception as error:
                exercise_error = exercise_error or bounded_error(error)
                break
            if pool_recovered:
                break
            time.sleep(0.05)
        recovered = False
        if pool_recovered and not return_errors:
            try:
                recovered = self._query() == 1
            except Exception as error:
                exercise_error = exercise_error or bounded_error(error)
        return {
            "ok": bool(
                recovered and pool_recovered and waiter_timeout
                and not exercise_error and not return_errors),
            "leases_acquired": len(owned),
            "waiter_timeout": waiter_timeout,
            "cancelled": self.cancelled.is_set(),
            "recovered_without_restart": recovered,
            "pool_stats_recovered": pool_recovered,
            "return_errors": return_errors[:4],
            "error": exercise_error,
        }

    def cancel(self):
        self.cancelled.set()

    def stop(self):
        self.stopped.set()
        self.cancel()
        try:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(0.2)
            client.connect(str(self.socket_path))
            client.close()
        except OSError:
            pass

    def _handle(self, client):
        try:
            with client:
                client.settimeout(2)
                try:
                    request = json.loads(client.recv(4096).decode("utf-8"))
                    if request.get("command") == "cancel":
                        self.cancel()
                        response = {"ok": True, "cancelled": True}
                    elif request.get("command") == "exercise":
                        response = self.exercise(
                            request.get("leases", 1), request.get("hold_seconds", 1))
                    else:
                        response = {"ok": False, "error": "unsupported command"}
                except Exception as error:
                    response = {"ok": False, "error": bounded_error(error)}
                try:
                    client.sendall(
                        (json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
                except OSError:
                    pass
        finally:
            self.client_slots.release()

    def bind(self):
        if self.server is not None:
            return
        self.socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(4)
            server.settimeout(0.5)
        except Exception:
            server.close()
            raise
        self.server = server

    def serve_forever(self):
        self.bind()
        server = self.server
        try:
            while not self.stopped.is_set():
                try:
                    client, _address = server.accept()
                except socket.timeout:
                    continue
                if not self.client_slots.acquire(blocking=False):
                    client.close()
                    continue
                threading.Thread(
                    target=self._handle,
                    args=(client,),
                    name="mojo-pool-lab-probe-client",
                    daemon=True,
                ).start()
        finally:
            server.close()
            self.server = None
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
