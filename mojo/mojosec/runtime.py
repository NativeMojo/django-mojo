"""Single-process MojoSec collector and delivery loop."""

import signal
import threading
import time

from .collectors import (
    FimCollector, JournalCollector, NginxCollector, SystemPythonCollector,
)
from .output import emit, emit_error, write_status, runtime_identity, publish_identity
from .profiles import profile_identity, resolve_profile
from .sender import Sender
from .store import Store


# The status file is for operators and check_node (which tolerates 600 s of
# age); a write per 5 s poll was five COUNT(*)s, a profile-health read, a
# change-journal read and two fsyncs for nothing. Health transitions still
# publish at once.
STATUS_INTERVAL_SECONDS = 60
# Tiers that scan at least this often keep their baseline in memory between
# scans instead of decoding it from SQLite each time; the six-hour tiers hold
# 30-64k entries and reload twice a day, which is free.
BASELINE_CACHE_MAX_INTERVAL_SECONDS = 600


class Runtime:
    def __init__(self, config, store=None, sender=None, identity=None):
        self.config = config
        self.identity = dict(identity if identity is not None else runtime_identity())
        collectors = config["collectors"]
        self.store = store or Store(
            config["state_dir"], config["sensor_id"],
            config["aggregation"], config["delivery"],
            expected_app_uid=(collectors["journal"].get("app_uid")
                              if collectors["journal"]["enabled"] else None),
        )
        self.journal = JournalCollector(collectors["journal"]) if collectors["journal"]["enabled"] else None
        self.nginx = NginxCollector(collectors["nginx"]) if collectors["nginx"]["enabled"] else None
        self.profile = resolve_profile(config.get("profile"))
        self.profile_identity = profile_identity(self.profile) if self.profile else None
        self.integrity_collectors = {}
        if self.profile:
            for tier, tier_config in collectors["fim"]["tiers"].items():
                self.integrity_collectors[tier] = FimCollector(
                    tier_config, config["expected_changes_path"], self.profile_identity, tier)
            self.integrity_collectors["rpm"] = SystemPythonCollector(
                collectors["rpm"], self.profile_identity, config["expected_changes_path"],
                covered_roots=tuple(
                    target["path"]
                    for tier in ("fast", "slow") if tier in collectors["fim"]["tiers"]
                    for target in collectors["fim"]["tiers"][tier]["targets"]))
            self.fim = None
        else:
            self.fim = (FimCollector(collectors["fim"], config["expected_changes_path"])
                        if collectors["fim"]["enabled"] else None)
        # Only enrollment-substituted tiers declare tenant roots, so this is
        # empty on every host-only node and the store relaxes nothing there.
        self.content_roots = tuple(sorted({
            root for collector in self.integrity_collectors.values()
            for root in getattr(collector, "content_roots", ())
        }))
        self.sender = sender or Sender(config, self.store)
        self.collector_status = {}
        self.last_delivery = {}
        self.last_fim = 0
        self.last_integrity = {}
        self.integrity_scans = {}
        self.baselines = {}
        self.last_status_at = 0
        self.last_status_signature = None
        self.running = True
        self.stop_event = threading.Event()
        self.diagnostic_stream = None

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
        emit_error(
            f"{name} collector failed", err,
            stream=getattr(self, "diagnostic_stream", None), collector=name,
        )

    def _poll_stream(self, collector):
        if collector is None:
            return
        try:
            cursor = self.store.get_meta(f"cursor:{collector.name}")
            if collector.name == "journal":
                result = collector.poll(
                    cursor, ssh_sessions=self.store.load_ssh_sessions())
            else:
                result = collector.poll(cursor)
            self.store.ingest(
                result["observations"], cursor_key=collector.name, cursor=result["cursor"],
                ssh_sessions=result.get("ssh_sessions"),
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
        cacheable = interval <= BASELINE_CACHE_MAX_INTERVAL_SECONDS
        try:
            scan = self.fim.scan()
            initialized = self.store.fim_initialized(scan["profile"])
            baseline = self.baselines.get(scan["profile"]) if cacheable else None
            if baseline is None:
                baseline = self.store.load_fim_baseline(scan["profile"]) if initialized else {}
            if scan["complete"]:
                observations = self.fim.diff(baseline, scan) if initialized else []
            else:
                observations = [item for item in self.fim.diff({}, scan)
                                if item["kind"] == "fim.overflow"]
            self.store.record_fim_scan(
                scan["profile"], scan["snapshot"], observations, scan["complete"],
                baseline=baseline if initialized else None,
            )
            if scan["complete"] and cacheable:
                self.baselines[scan["profile"]] = scan["snapshot"]
            self._collector_ok("fim")
        except Exception as err:
            self._collector_error("fim", err)

    def integrity_roster(self):
        """Every configured tier in stable scan order, preserving legacy order."""
        names = set(self.integrity_collectors)
        roster = ["fast"] if "fast" in names else []
        roster.extend(sorted(names - {"fast", "rpm"}))
        if "rpm" in names:
            roster.append("rpm")
        return tuple(roster)

    def preview_integrity(self):
        """Scan every immutable tier without changing authoritative state."""
        if not self.profile:
            raise ValueError("legacy custom FIM has no profile activation ceremony")
        scans = {}
        for tier in self.integrity_roster():
            collector = self.integrity_collectors[tier]
            started = time.monotonic()
            previous = self.store.load_fim_baseline(collector.baseline_key)
            scan = collector.scan(previous)
            scan["duration"] = round(time.monotonic() - started, 6)
            config = collector.config
            scan["bounds"] = {
                key: config[key] for key in (
                    "max_entries", "max_file_bytes", "max_depth") if key in config
            }
            scans[tier] = scan
        return scans

    def preview_integrity_tier(self, tier):
        """Scan exactly one enrollment-substituted tier, changing no state."""
        if not self.profile:
            raise ValueError("legacy custom FIM has no profile activation ceremony")
        collector = self.integrity_collectors.get(tier)
        if collector is None:
            raise ValueError(f"unknown integrity tier: {tier}")
        if not getattr(collector, "content_roots", ()):
            raise ValueError(
                "only an enrollment-substituted tier may be baselined on its own; "
                "the immutable host tiers are seeded by the whole-profile ceremony")
        started = time.monotonic()
        scan = collector.scan(self.store.load_fim_baseline(collector.baseline_key))
        scan["duration"] = round(time.monotonic() - started, 6)
        scan["bounds"] = {
            key: collector.config[key] for key in (
                "max_entries", "max_file_bytes", "max_depth")
            if key in collector.config
        }
        return scan

    def initialize_integrity_tier(self, tier, scan, reason="re-enrollment"):
        if not self.profile:
            raise ValueError("legacy custom FIM has no profile activation ceremony")
        self.baselines.clear()
        return self.store.initialize_fim_tier(
            self.profile_identity, tier, scan, reason=reason)

    def initialize_integrity(self, scans, reason="initialize"):
        if not self.profile:
            raise ValueError("legacy custom FIM initializes on its historical first scan")
        self.baselines.clear()
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
        for tier in self.integrity_roster():
            collector = self.integrity_collectors[tier]
            interval = collector.config["interval_seconds"]
            if self.last_integrity.get(tier) and now - self.last_integrity[tier] < interval:
                continue
            self.last_integrity[tier] = now
            cacheable = interval <= BASELINE_CACHE_MAX_INTERVAL_SECONDS
            try:
                baseline = self.baselines.get(collector.baseline_key) if cacheable else None
                if baseline is None:
                    baseline = self.store.load_fim_baseline(collector.baseline_key)
                started = time.monotonic()
                scan = collector.scan(baseline)
                scan["duration"] = round(time.monotonic() - started, 6)
                observations = collector.diff(baseline, scan)
                self.store.record_fim_scan(
                    collector.baseline_key, scan["snapshot"], observations, scan["complete"],
                    baseline=baseline)
                # Only after the commit: a rolled-back write must leave the
                # cache equal to what SQLite still holds, or the next scan
                # would diff against the new snapshot and never raise it.
                if scan["complete"] and cacheable:
                    self.baselines[collector.baseline_key] = scan["snapshot"]
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

    def _status_signature(self):
        """What a status write is for: health transitions, never timestamps."""
        return (
            self.running,
            tuple(sorted(
                (name, bool(status.get("ok")), str(status.get("error", "")))
                for name, status in self.collector_status.items())),
            tuple(sorted(
                (tier, bool(scan.get("ok"))) for tier, scan in self.integrity_scans.items())),
            bool(self.last_delivery.get("error")),
            bool(self.last_delivery.get("retry")),
        )

    def _publish_status(self, force=False):
        now = time.monotonic()
        signature = self._status_signature()
        if (not force and self.last_status_at and
                now - self.last_status_at < STATUS_INTERVAL_SECONDS and
                signature == self.last_status_signature):
            return False
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
        status.update(self.identity)
        status["running"] = self.running
        write_status(self.config["status_path"], status)
        self.last_status_at = now
        self.last_status_signature = signature
        return True

    def run_once(self):
        self._poll_stream(self.journal)
        self._poll_stream(self.nginx)
        self._poll_fim()
        self._poll_integrity()
        try:
            # Nothing to annotate is the steady state; the manifest and the
            # change journal are not opened at all on such a poll.
            if self.store.has_pending_annotations():
                expected_path = self.config.get("expected_changes_path", "")
                active_paths = ()
                if expected_path == "/etc/mojosec/expected_changes.json":
                    from mojo.deploy.mojosec_changes import ChangeJournal
                    active_paths = ChangeJournal().active_paths()
                self.store.annotate_pending_fim(
                    expected_path, active_paths=active_paths,
                    directory_metadata_roots=self.content_roots)
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

    def run(self, signal_installer=None):
        install_signal = signal_installer or signal.signal
        install_signal(signal.SIGTERM, self.stop)
        install_signal(signal.SIGINT, self.stop)
        publish_identity(self.config, self.identity, running=True)
        emit("info", "MojoSec sensor started", sensor_id=self.config["sensor_id"])
        while self.running:
            started = time.monotonic()
            self.run_once()
            remaining = self.config["poll_seconds"] - (time.monotonic() - started)
            if remaining > 0 and self.running:
                self.stop_event.wait(remaining)
        self._publish_status(force=True)
        self.store.close()
        emit("info", "MojoSec sensor stopped", sensor_id=self.config["sensor_id"])
