"""Single-process MojoSec collector and delivery loop."""

import signal
import threading
import time

from .collectors import FimCollector, JournalCollector, NginxCollector, RpmCollector
from .output import emit, write_status
from .profiles import profile_identity, resolve_profile
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
        self.profile = resolve_profile(config.get("profile"))
        self.profile_identity = profile_identity(self.profile) if self.profile else None
        self.integrity_collectors = {}
        if self.profile:
            for tier, tier_config in collectors["fim"]["tiers"].items():
                self.integrity_collectors[tier] = FimCollector(
                    tier_config, config["expected_changes_path"], self.profile_identity, tier)
            self.integrity_collectors["rpm"] = RpmCollector(
                collectors["rpm"], self.profile_identity, config["expected_changes_path"])
            self.fim = None
        else:
            self.fim = (FimCollector(collectors["fim"], config["expected_changes_path"])
                        if collectors["fim"]["enabled"] else None)
        self.sender = sender or Sender(config, self.store)
        self.collector_status = {}
        self.last_delivery = {}
        self.last_fim = 0
        self.last_integrity = {}
        self.integrity_scans = {}
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

    def preview_integrity(self):
        """Scan every immutable tier without changing authoritative state."""
        if not self.profile:
            raise ValueError("legacy custom FIM has no profile activation ceremony")
        scans = {}
        fast_snapshot = None
        for tier in ("fast", "slow", "rpm"):
            collector = self.integrity_collectors[tier]
            started = time.monotonic()
            previous = self.store.load_fim_baseline(collector.baseline_key)
            if tier == "rpm":
                scan = collector.scan(previous, shared_snapshot=fast_snapshot)
            else:
                scan = collector.scan(previous)
                if tier == "fast":
                    fast_snapshot = scan["snapshot"]
            scan["duration"] = round(time.monotonic() - started, 6)
            config = collector.config
            scan["bounds"] = {
                key: config[key] for key in (
                    "max_entries", "max_file_bytes", "max_depth") if key in config
            }
            scans[tier] = scan
        return scans

    def initialize_integrity(self, scans, reason="initialize"):
        if not self.profile:
            raise ValueError("legacy custom FIM initializes on its historical first scan")
        self.store.activate_fim_profile(self.profile_identity, scans, reason=reason)

    def _poll_integrity(self):
        if not self.integrity_collectors:
            return
        active = self.store.active_fim_profile()
        if active != self.profile_identity:
            self._collector_error(
                "integrity", "profile baseline is not explicitly initialized or digest drifted")
            return
        now = time.monotonic()
        fast_snapshot = None
        for tier in ("fast", "slow", "rpm"):
            collector = self.integrity_collectors[tier]
            interval = collector.config["interval_seconds"]
            if self.last_integrity.get(tier) and now - self.last_integrity[tier] < interval:
                continue
            self.last_integrity[tier] = now
            try:
                baseline = self.store.load_fim_baseline(collector.baseline_key)
                started = time.monotonic()
                if tier == "rpm":
                    if fast_snapshot is None:
                        fast = self.integrity_collectors["fast"]
                        fast_snapshot = self.store.load_fim_baseline(fast.baseline_key)
                    scan = collector.scan(baseline, shared_snapshot=fast_snapshot)
                else:
                    scan = collector.scan(baseline)
                    if tier == "fast":
                        fast_snapshot = scan["snapshot"]
                scan["duration"] = round(time.monotonic() - started, 6)
                observations = collector.diff(baseline, scan)
                self.store.record_fim_scan(
                    collector.baseline_key, scan["snapshot"], observations, scan["complete"])
                self.integrity_scans[tier] = {
                    "ok": scan["complete"], "last_success": time.time(),
                    "complete": scan["complete"], "entries": len(scan["snapshot"]),
                    "duration": scan["duration"],
                    "packages": scan.get("packages", 0),
                    "anomalies": scan.get("anomalies", 0),
                }
                if not scan["complete"]:
                    raise RuntimeError(f"{tier} integrity scan was incomplete")
            except Exception as err:
                previous = self.integrity_scans.get(tier, {})
                self.integrity_scans[tier] = {
                    "ok": False, "complete": False,
                    "last_success": previous.get("last_success"),
                    "error": str(err)[:256],
                }
                self._collector_error(f"integrity_{tier}", err)
        self.collector_status["integrity"] = {
            "ok": all(item.get("ok") for item in self.integrity_scans.values()) and
                  set(self.integrity_scans) == set(self.integrity_collectors),
            "tiers": self.integrity_scans,
        }

    def _publish_status(self):
        status = {
            "schema": "mojosec.status", "version": 1,
            "sensor_id": self.config["sensor_id"],
            "state": "running" if self.running else "stopping",
            "config": self.config.get("config_provenance", {}),
            "collectors": self.collector_status,
            "delivery": self.last_delivery,
        }
        if self.profile_identity:
            keys = {tier: collector.baseline_key
                    for tier, collector in self.integrity_collectors.items()}
            status["integrity"] = self.store.fim_profile_health(
                self.profile_identity, keys)
            status["integrity"]["runtime"] = self.integrity_scans
        try:
            from mojo.deploy.mojosec_changes import ChangeJournal
            status["expected_changes"] = ChangeJournal().status()
        except Exception as err:
            status["expected_changes"] = {"ok": False, "error": str(err)[:256]}
        status.update(self.store.stats())
        write_status(self.config["status_path"], status)

    def run_once(self):
        self._poll_stream(self.journal)
        self._poll_stream(self.nginx)
        self._poll_fim()
        self._poll_integrity()
        try:
            self.store.annotate_pending_fim(self.config.get("expected_changes_path", ""))
        except Exception as err:
            self._collector_error("expected_changes", err)
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
