"""Single-process MojoSec collector and delivery loop."""

import signal
import threading
import time

from .collectors import FimCollector, JournalCollector, NginxCollector
from .output import emit, write_status
from .sender import Sender
from .store import Store


class Runtime:
    def __init__(self, config, store=None, sender=None):
        self.config = config
        self.store = store or Store(
            config["state_dir"], config["sensor_id"],
            config["aggregation"], config["delivery"],
        )
        collectors = config["collectors"]
        self.journal = JournalCollector(collectors["journal"]) if collectors["journal"]["enabled"] else None
        self.nginx = NginxCollector(collectors["nginx"]) if collectors["nginx"]["enabled"] else None
        self.fim = (FimCollector(collectors["fim"], config["expected_changes_path"])
                    if collectors["fim"]["enabled"] else None)
        self.sender = sender or Sender(config, self.store)
        self.collector_status = {}
        self.last_delivery = {}
        self.last_fim = 0
        self.running = True
        self.stop_event = threading.Event()

    def _collector_ok(self, name, malformed=0):
        self.collector_status[name] = {
            "ok": True, "last_success": time.time(), "malformed_records": malformed,
        }

    def _collector_error(self, name, err):
        previous = self.collector_status.get(name, {})
        self.collector_status[name] = {
            "ok": False, "last_success": previous.get("last_success"),
            "error": str(err)[:256],
        }
        emit("error", f"{name} collector failed", collector=name, error=str(err)[:256])

    def _poll_stream(self, collector):
        if collector is None:
            return
        try:
            cursor = self.store.get_meta(f"cursor:{collector.name}")
            result = collector.poll(cursor)
            self.store.ingest(
                result["observations"], cursor_key=collector.name, cursor=result["cursor"]
            )
            self._collector_ok(collector.name, result.get("malformed", 0))
        except Exception as err:
            self._collector_error(collector.name, err)

    def _poll_fim(self):
        if self.fim is None:
            return
        interval = self.config["collectors"]["fim"]["interval_seconds"]
        if self.last_fim and time.monotonic() - self.last_fim < interval:
            return
        self.last_fim = time.monotonic()
        try:
            scan = self.fim.scan()
            initialized = self.store.fim_initialized(scan["profile"])
            baseline = self.store.load_fim_baseline(scan["profile"]) if initialized else {}
            if scan["complete"]:
                observations = self.fim.diff(baseline, scan) if initialized else []
            else:
                observations = [item for item in self.fim.diff({}, scan)
                                if item["kind"] == "fim.overflow"]
            self.store.record_fim_scan(
                scan["profile"], scan["snapshot"], observations, scan["complete"]
            )
            self._collector_ok("fim")
        except Exception as err:
            self._collector_error("fim", err)

    def _publish_status(self):
        status = {
            "schema": "mojosec.status", "version": 1,
            "sensor_id": self.config["sensor_id"],
            "state": "running" if self.running else "stopping",
            "config": self.config["config_provenance"],
            "collectors": self.collector_status,
            "delivery": self.last_delivery,
        }
        status.update(self.store.stats())
        write_status(self.config["status_path"], status)

    def run_once(self):
        self._poll_stream(self.journal)
        self._poll_stream(self.nginx)
        self._poll_fim()
        try:
            self.last_delivery = self.sender.send_once()
        except Exception as err:
            self.last_delivery = {"error": str(err)[:256]}
            emit("error", "delivery failed", error=str(err)[:256])
        self._publish_status()
        return self.last_delivery

    def stop(self, signum=None, frame=None):
        self.running = False
        self.stop_event.set()

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        emit("info", "MojoSec sensor started", sensor_id=self.config["sensor_id"])
        while self.running:
            started = time.monotonic()
            self.run_once()
            remaining = self.config["poll_seconds"] - (time.monotonic() - started)
            if remaining > 0 and self.running:
                self.stop_event.wait(remaining)
        self._publish_status()
        self.store.close()
        emit("info", "MojoSec sensor stopped", sensor_id=self.config["sensor_id"])
