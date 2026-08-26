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
from mojo.db.pool_telemetry import atomic_write, pool_snapshot
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
        snapshot = pool_snapshot(pool, identity=config.LAST_POOL_PLAN.get("identity"),
                                 previous=self.previous)
        self.previous = snapshot
        atomic_write(self.root / f"worker-{self.pid}.json", snapshot)
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
        self.thread = threading.Thread(
            target=self._run, name="mojo-pool-sampler", daemon=True)
        self.thread.start()
        if getattr(settings, "DATABASE_POOL_LAB_PROBE_ENABLED", False):
            socket_path = os.environ.get(
                "MOJO_POOL_PROBE_SOCKET", str(self.root / f"probe-{self.pid}.sock"))
            self.probe = LocalPoolProbe(socket_path)
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
        try:
            pool = self.connections["default"].pool
            if pool is None:
                return {"ok": False, "error": "default connection is not pooled"}
            maximum = int(pool.get_stats().get("pool_max", 0))
            if maximum <= 0:
                return {"ok": False, "error": "pool maximum is unavailable"}
            requested = max(1, min(int(leases), maximum))
            for _index in range(requested):
                if self.cancelled.is_set() or time.monotonic() - started > self.deadline:
                    break
                owned.append(pool.getconn(timeout=min(2, self.deadline)))
            self.cancelled.wait(max(0, min(float(hold_seconds), self.deadline)))
        finally:
            for connection in reversed(owned):
                try:
                    self.connections["default"].pool.putconn(connection)
                except Exception:
                    pass
            self.lock.release()
        recovered = False
        try:
            recovered = self._query() == 1
        except Exception:
            recovered = False
        return {
            "ok": recovered,
            "leases_acquired": len(owned),
            "cancelled": self.cancelled.is_set(),
            "recovered_without_restart": recovered,
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
        with client:
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
                client.sendall((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
            except OSError:
                pass

    def serve_forever(self):
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
            while not self.stopped.is_set():
                try:
                    client, _address = server.accept()
                except socket.timeout:
                    continue
                threading.Thread(
                    target=self._handle,
                    args=(client,),
                    name="mojo-pool-lab-probe-client",
                    daemon=True,
                ).start()
        finally:
            server.close()
            try:
                self.socket_path.unlink()
            except FileNotFoundError:
                pass
