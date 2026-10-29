"""Per-poll work of the sensor is bounded (maestro #9769 regression tests)."""

import json
import os
import tempfile
import time

from testit import helpers as th


AGGREGATION = {
    "window_seconds": 60, "flush_count": 2, "max_aggregates": 100,
    "critical_reserve_aggregates": 10,
}
DELIVERY = {
    "batch_events": 100, "batch_bytes": 256 * 1024, "timeout_seconds": 10,
    "retry_min_seconds": 1, "retry_max_seconds": 30, "gzip": True,
    "max_spool_events": 100, "critical_reserve_events": 10,
}
RETIRED = ("process_nodes", "audit_fragments", "crond_launches", "origin_sessions",
           "audit_health_epochs", "pending_firewall", "firewall_receipts")


def _store(root, cls=None):
    from mojo.mojosec.store import Store

    return (cls or Store)(root, "sensor", AGGREGATION, DELIVERY,
                          local_only_diagnostic_path=os.path.join(root, "missing"))


class _Trace:
    def __init__(self, store):
        self.statements = []
        store.db.set_trace_callback(self.statements.append)

    def touching(self, *needles):
        return [sql for sql in self.statements
                if any(needle in sql for needle in needles)]


@th.django_unit_test()
def test_idle_poll_issues_bounded_store_work(opts):
    with tempfile.TemporaryDirectory() as root:
        store = _store(root)
        try:
            trace = _Trace(store)
            store.ingest([], cursor_key="journal", cursor="c1")
            store.ingest([], cursor_key="nginx", cursor="n1")
            th.assert_true(len(trace.statements) <= 12,
                           f"two idle polls must be a fixed handful of statements: {trace.statements}")
            th.assert_eq(trace.touching(*RETIRED), [],
                         "no per-poll statement may touch a retired provenance table")
            th.assert_eq(trace.touching("OVER (", "COUNT(*)", "LIMIT -1 OFFSET"), [],
                         "no per-poll window scan, table count or offset prune may remain")
            th.assert_eq(store.get_meta("cursor:journal"), "c1",
                         "the idle poll must still commit its cursor")
        finally:
            store.close()


@th.django_unit_test()
def test_status_is_published_on_cadence_or_change(opts):
    from mojo.mojosec import runtime as runtime_module
    from mojo.mojosec.store import Store

    class CountingStore(Store):
        stats_calls = 0

        def stats(self):
            self.stats_calls += 1
            return super().stats()

    class IdleSender:
        def send_once(self):
            return {"sent": 0, "accepted": 0, "retry": 0}

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        status_path = os.path.join(root, "run", "status.json")
        config = {
            "sensor_id": "sensor", "state_dir": os.path.join(root, "state"),
            "status_path": status_path, "poll_seconds": 5,
            "aggregation": AGGREGATION, "delivery": DELIVERY,
            "collectors": {"journal": {"enabled": False}, "nginx": {"enabled": False},
                           "fim": {"enabled": False}},
        }
        store = CountingStore(os.path.join(root, "state"), "sensor", AGGREGATION, DELIVERY,
                              local_only_diagnostic_path=os.path.join(root, "missing"))
        runtime = runtime_module.Runtime(config, store=store, sender=IdleSender())
        try:
            runtime.run_once()
            runtime.run_once()
            runtime.run_once()
            th.assert_eq(store.stats_calls, 1,
                         "an unchanged runtime inside the cadence must not recount the spool")
            with open(status_path, encoding="utf-8") as handle:
                first = json.load(handle)["updated_at"]
            runtime._collector_error("nginx", RuntimeError("tail failed"))
            runtime.run_once()
            th.assert_eq(store.stats_calls, 2,
                         "a collector health transition must publish at once")
            runtime.run_once()
            th.assert_eq(store.stats_calls, 2,
                         "a repeated error state inside the cadence is not a change")
            runtime.last_status_at -= runtime_module.STATUS_INTERVAL_SECONDS
            runtime.run_once()
            th.assert_eq(store.stats_calls, 3,
                         "the cadence must publish once the interval has elapsed")
            runtime.running = False
            runtime._publish_status(force=True)
            with open(status_path, encoding="utf-8") as handle:
                final = json.load(handle)
            th.assert_eq(final["state"], "stopping",
                         "a forced publish must write regardless of cadence")
            th.assert_true(final["updated_at"] >= first,
                           "status timestamps must move forward across publishes")
            th.assert_true("provenance" not in final,
                           "the status file must no longer carry a provenance block")
        finally:
            store.close()


@th.django_unit_test()
def test_fim_scan_without_changes_skips_baseline_rewrite(opts):
    from mojo.mojosec.events import observation

    def change(path):
        return observation("fim.change", "high", "Targeted filesystem integrity change",
                           attributes={"path": path, "change": "modified", "kind": "file",
                                       "expected_change": {"deployment_id": "d"}},
                           fingerprint_values=(path, "modified", str(time.time())),
                           aggregate=False, recommendation="review")

    with tempfile.TemporaryDirectory() as root:
        store = _store(root)
        try:
            baseline = {f"/etc/{index}": {"kind": "file", "sha256": "a" * 64}
                        for index in range(50)}
            store.record_fim_scan("fast", baseline, [], True)
            th.assert_eq(store.load_fim_baseline("fast"), baseline,
                         "the first complete scan must seed the whole baseline")

            trace = _Trace(store)
            store.record_fim_scan("fast", dict(baseline), [], True, baseline=baseline)
            th.assert_eq(trace.touching("fim_baseline"), [],
                         "a scan that changed nothing must not touch the baseline rows")
            th.assert_true(any("fim_scan:fast" in sql for sql in trace.statements),
                           "the scan health marker must still be refreshed")

            snapshot = dict(baseline)
            snapshot["/etc/7"] = {"kind": "file", "sha256": "b" * 64}
            del snapshot["/etc/9"]
            trace = _Trace(store)
            store.record_fim_scan("fast", snapshot, [change("/etc/7")], True, baseline=baseline)
            writes = trace.touching("fim_baseline")
            th.assert_eq(len(writes), 2,
                         f"one modified and one deleted path must be two row writes: {writes}")
            th.assert_true(any("ON CONFLICT" in sql for sql in writes) and
                           any("DELETE FROM fim_baseline WHERE profile = " in sql and
                               " AND path = " in sql for sql in writes),
                           f"the delta must be an upsert and a per-path delete: {writes}")
            th.assert_eq(store.load_fim_baseline("fast"), snapshot,
                         "the delta write must leave the stored baseline equal to the snapshot")

            trace = _Trace(store)
            store.record_fim_scan("fast", snapshot, [], True)
            th.assert_true(any("DELETE FROM fim_baseline WHERE profile = " in sql and
                               "path" not in sql for sql in trace.touching("fim_baseline")),
                           "without a baseline the full rewrite remains available")
        finally:
            store.close()


@th.django_unit_test()
def test_runtime_keeps_its_cached_baseline_until_the_store_commits(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.runtime import Runtime
    from mojo.mojosec.store import Store, StoreError

    class FlakyStore(Store):
        fail_next = False

        def record_fim_scan(self, *args, **kwargs):
            if self.fail_next:
                self.fail_next = False
                raise StoreError("busy")
            return super().record_fim_scan(*args, **kwargs)

    class FakeTier:
        baseline_key = "al2023-web-v3:d:fast"
        config = {"interval_seconds": 60}

        def __init__(self):
            self.snapshot = {"/etc/a": {"kind": "file", "sha256": "1" * 64}}

        def scan(self, previous=None):
            return {"complete": True, "snapshot": dict(self.snapshot), "tier": "fast",
                    "baseline_key": self.baseline_key}

        def diff(self, baseline, scan):
            return [observation(
                "fim.change", "high", "Targeted filesystem integrity change",
                attributes={"path": path, "change": "modified", "kind": "file",
                            "expected_change": {"deployment_id": "d"}},
                fingerprint_values=(path, "modified", json.dumps(scan["snapshot"][path])),
                aggregate=False, recommendation="review")
                for path in scan["snapshot"] if baseline.get(path) != scan["snapshot"][path]]

    with tempfile.TemporaryDirectory() as root:
        store = _store(root, FlakyStore)
        identity = {"name": "al2023-web-v3", "version": 3, "digest": "d"}
        tier = FakeTier()
        store.activate_fim_profile(identity, {"fast": {
            "tier": "fast", "baseline_key": tier.baseline_key, "complete": True,
            "snapshot": dict(tier.snapshot)}})
        runtime = Runtime.__new__(Runtime)
        runtime.store = store
        runtime.profile = {"name": "al2023-web-v3"}
        runtime.profile_identity = identity
        runtime.integrity_collectors = {"fast": tier}
        runtime.last_integrity = {}
        runtime.integrity_scans = {}
        runtime.collector_status = {}
        runtime.baselines = {}
        errors = []
        runtime._collector_error = lambda name, err: errors.append(name)
        try:
            runtime._poll_integrity()
            th.assert_eq(store.pending_batch(10, 65536), [], "an unchanged scan raises nothing")
            th.assert_eq(runtime.baselines[tier.baseline_key][0], tier.snapshot,
                         "a committed scan must populate the fast-tier cache")

            tier.snapshot["/etc/a"] = {"kind": "file", "sha256": "2" * 64}
            store.fail_next = True
            runtime.last_integrity.clear()
            runtime._poll_integrity()
            th.assert_eq(errors, ["integrity_fast"], "the failed commit must be reported")
            th.assert_eq(runtime.baselines[tier.baseline_key][0]["/etc/a"]["sha256"], "1" * 64,
                         "a rolled-back write must leave the cache equal to SQLite")

            runtime.last_integrity.clear()
            runtime._poll_integrity()
            events = store.pending_batch(10, 65536)
            th.assert_eq([event["attributes"]["path"] for event in events], ["/etc/a"],
                         "the change must be raised on the next scan, not lost to a stale cache")
            th.assert_eq(store.load_fim_baseline(tier.baseline_key)["/etc/a"]["sha256"],
                         "2" * 64, "the committed delta must reach SQLite")

            # Another process (the baseline CLI) re-seeds the baseline underneath
            # the daemon: the next scan must diff against what it seeded, not the
            # daemon's memory of its own last scan.
            time.sleep(0.01)
            reseeded = {"/etc/a": {"kind": "file", "sha256": "3" * 64}}
            store.activate_fim_profile(identity, {"fast": {
                "tier": "fast", "baseline_key": tier.baseline_key, "complete": True,
                "snapshot": dict(reseeded)}}, reason="re-initialize")
            tier.snapshot = dict(reseeded)
            runtime.last_integrity.clear()
            runtime._poll_integrity()
            th.assert_eq(len(store.pending_batch(10, 65536)), 1,
                         "a scan equal to the re-seeded baseline must raise nothing new")
            th.assert_eq(runtime.baselines[tier.baseline_key][0], reseeded,
                         "the cache must have been rebuilt from the re-seeded baseline")
        finally:
            store.close()


@th.django_unit_test()
def test_runtime_cache_drops_a_reseed_committed_right_after_its_own_scan(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.runtime import Runtime
    from mojo.mojosec.store import Store

    identity = {"name": "al2023-web-v3", "version": 3, "digest": "d"}
    key = "al2023-web-v3:d:fast"
    reseeded = {"/etc/a": {"kind": "file", "sha256": "4" * 64}}

    class FakeTier:
        baseline_key = key
        config = {"interval_seconds": 60}

        def __init__(self):
            self.snapshot = {"/etc/a": {"kind": "file", "sha256": "1" * 64}}

        def scan(self, previous=None):
            return {"complete": True, "snapshot": dict(self.snapshot), "tier": "fast",
                    "baseline_key": key}

        def diff(self, baseline, scan):
            return [observation(
                "fim.change", "high", "Targeted filesystem integrity change",
                attributes={"path": path, "change": "modified", "kind": "file",
                            "expected_change": {"deployment_id": "d"}},
                fingerprint_values=(path, "modified", json.dumps(scan["snapshot"][path])),
                aggregate=False, recommendation="review")
                for path in scan["snapshot"] if baseline.get(path) != scan["snapshot"][path]]

    class RacedStore(Store):
        """The baseline CLI's own connection commits right after the daemon's
        scan COMMIT and before the daemon caches that scan."""
        other = None

        def record_fim_scan(self, *args, **kwargs):
            stamp = super().record_fim_scan(*args, **kwargs)
            if self.other is not None:
                other, self.other = self.other, None
                time.sleep(0.01)
                other.activate_fim_profile(identity, {"fast": {
                    "tier": "fast", "baseline_key": key, "complete": True,
                    "snapshot": dict(reseeded)}}, reason="re-initialize")
            return stamp

    with tempfile.TemporaryDirectory() as root:
        tier = FakeTier()
        store = _store(root, RacedStore)
        other = _store(root)
        store.activate_fim_profile(identity, {"fast": {
            "tier": "fast", "baseline_key": key, "complete": True,
            "snapshot": dict(tier.snapshot)}})
        runtime = Runtime.__new__(Runtime)
        runtime.store = store
        runtime.profile = {"name": "al2023-web-v3"}
        runtime.profile_identity = identity
        runtime.integrity_collectors = {"fast": tier}
        runtime.last_integrity = {}
        runtime.integrity_scans = {}
        runtime.collector_status = {}
        runtime.baselines = {}
        try:
            time.sleep(0.01)
            store.other = other
            runtime._poll_integrity()
            th.assert_eq(store.load_fim_baseline(key), reseeded,
                         "the second connection's re-seed must be what SQLite holds")
            th.assert_eq(store.pending_batch(10, 65536), [], "the raced scan itself raises nothing")

            # The file reverts to the content the re-seed replaced: against the
            # re-seeded baseline that is a change; against a stale cache it is not.
            runtime.last_integrity.clear()
            runtime._poll_integrity()
            events = store.pending_batch(10, 65536)
            th.assert_eq([event["attributes"]["path"] for event in events], ["/etc/a"],
                         "a re-seed committed between the scan COMMIT and the cache write "
                         "must not leave the daemon diffing against its old snapshot")
        finally:
            other.close()
            store.close()


@th.django_unit_test()
def test_manifest_is_parsed_once_per_stat_generation(opts):
    from mojo.mojosec.expected_changes import ExpectedChangeError, load_manifest

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "expected.json")
        manifest = {"schema": "mojosec.expected_changes", "version": 1, "entries": [{
            "path": "/etc/motd", "change": "modified", "sha256": "a" * 64,
            "expires_at": "2099-01-01T00:00:00Z", "deployment_id": "deploy-1"}]}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        os.chmod(path, 0o600)
        first = load_manifest(path, require_root=False)
        second = load_manifest(path, require_root=False)
        th.assert_true(first is second,
                       "an unchanged manifest must be served from the stat cache, not re-parsed")
        th.assert_eq(len(first), 1, "the cached entries must be the parsed entries")

        manifest["entries"][0]["deployment_id"] = "deploy-2"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        third = load_manifest(path, require_root=False)
        th.assert_true(third is not second and third[0]["deployment_id"] == "deploy-2",
                       "a rewritten manifest must be parsed again")

        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        for unused in range(2):
            with th.assert_raises(ExpectedChangeError):
                load_manifest(path, require_root=False)
        os.unlink(path)
        th.assert_eq(load_manifest(path, require_root=False), [],
                     "an absent manifest means no annotations and clears the cache")


@th.django_unit_test()
def test_annotation_is_skipped_when_nothing_awaits(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.runtime import Runtime

    with tempfile.TemporaryDirectory() as root:
        store = _store(root)
        try:
            th.assert_true(not store.has_pending_annotations(),
                           "an empty spool has nothing awaiting annotation")
            trace = _Trace(store)
            store.has_pending_annotations()
            th.assert_true(all("LIMIT 1" in sql for sql in trace.statements),
                           f"the probe must be a bounded indexed lookup: {trace.statements}")
            plan = store.db.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM events WHERE annotation_deadline > 0 "
                "AND attempts = 0 AND last_error = '' LIMIT 1").fetchall()
            th.assert_true(any("events_annotation_pending" in str(tuple(row)) for row in plan),
                           f"the pending probe must use the partial index: {plan}")
            store.ingest([observation(
                "fim.change", "high", "Targeted filesystem integrity change",
                attributes={"path": "/etc/motd", "change": "modified", "kind": "file"},
                fingerprint_values=("/etc/motd", "modified", "x"), aggregate=False,
                recommendation="review")])
            th.assert_true(store.has_pending_annotations(),
                           "an unexplained FIM change must await its annotation pass")
            store.ingest([observation(
                "auth.ssh_failure", "warning", "SSH authentication failed",
                attributes={"source_ip": "192.0.2.9"}, fingerprint_values=("192.0.2.9",),
                aggregate=False, recommendation="review")])
            th.assert_true(store.has_pending_annotations(),
                           "unrelated spool traffic must not hide a pending annotation")
        finally:
            store.close()

    class FakeStore:
        def __init__(self, pending):
            self.pending = pending
            self.annotated = 0

        def has_pending_annotations(self):
            return self.pending

        def annotate_pending_fim(self, *args, **kwargs):
            self.annotated += 1
            return 0

    for pending, expected in ((False, 0), (True, 1)):
        runtime = Runtime.__new__(Runtime)
        runtime.config = {"expected_changes_path": ""}
        runtime.store = FakeStore(pending)
        runtime.journal = runtime.nginx = runtime.fim = None
        runtime.integrity_collectors = {}
        runtime.content_roots = ()
        runtime.collector_status = {}
        runtime.sender = type("Sender", (), {"send_once": lambda self: {"accepted": 0}})()
        runtime._publish_status = lambda force=False: True
        runtime.run_once()
        th.assert_eq(runtime.store.annotated, expected,
                     f"annotation must run only when something awaits (pending={pending})")
