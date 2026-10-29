"""The sensor's per-poll work stays bounded on an idle node (#9769)."""

import json
import os
import tempfile
from unittest import mock

from testit import helpers as th


AGGREGATION = {
    "window_seconds": 60, "flush_count": 25, "max_aggregates": 100,
    "critical_reserve_aggregates": 10,
}
DELIVERY = {
    "max_spool_events": 100, "critical_reserve_events": 10,
    "retry_min_seconds": 1, "retry_max_seconds": 2,
}
RETIRED_TABLES = (
    "audit_fragments", "process_nodes", "origin_sessions", "crond_launches",
    "audit_health_epochs", "pending_firewall", "firewall_receipts",
)
IDENTITY = {"name": "al2023-web-cost", "version": 1, "digest": "c" * 64}


def _store(root, store_class=None):
    from mojo.mojosec.store import Store

    return (store_class or Store)(
        root, "sensor-cost", AGGREGATION, DELIVERY,
        local_only_diagnostic_path=os.path.join(root, "absent.json"))


def _traced(store):
    statements = []
    store.db.set_trace_callback(statements.append)
    return statements


def _entry(size):
    return {"kind": "file", "mode": 0o644, "uid": 0, "gid": 0, "size": size,
            "sha256": f"{size:064x}"}


def _change(path):
    from mojo.mojosec.events import observation

    return observation(
        "fim.change", "high", "Protected file changed",
        attributes={"path": path, "change": "modified"},
        fingerprint_values=(path, "modified"), aggregate=False,
        recommendation="review")


def _runtime(root, store):
    from mojo.mojosec.runtime import Runtime

    runtime = Runtime.__new__(Runtime)
    runtime.config = {
        "status_path": os.path.join(root, "status.json"), "sensor_id": "sensor-cost",
        "expected_changes_path": "", "poll_seconds": 5,
    }
    runtime.identity = {}
    runtime.store = store
    runtime.journal = runtime.nginx = runtime.fim = None
    runtime.integrity_collectors = {}
    runtime.integrity_scans = {}
    runtime.profile_identity = None
    runtime.content_roots = ()
    runtime.collector_status = {}
    runtime.last_delivery = {}
    runtime.last_integrity = {}
    runtime.baselines = {}
    runtime.running = True
    runtime.diagnostic_stream = None
    runtime.sender = mock.Mock(send_once=mock.Mock(return_value={"accepted": 0}))
    return runtime


@th.django_unit_test()
def test_idle_poll_issues_bounded_store_work(opts):
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = _store(root)
        try:
            store.ingest([], cursor_key="journal", cursor="c0")
            statements = _traced(store)
            store.ingest([], cursor_key="journal", cursor="c1")
            store.db.set_trace_callback(None)
            th.assert_true(len(statements) <= 6,
                           f"an idle journal poll must issue a fixed handful of statements: "
                           f"{statements}")
            text = "\n".join(statements).lower()
            for name in RETIRED_TABLES:
                th.assert_true(name not in text,
                               f"an idle poll must not touch the provenance table {name}")
            th.assert_true(" over (" not in text and " over(" not in text,
                           "an idle poll must run no window function")
            th.assert_true("count(*)" not in text,
                           "an idle poll must count nothing")
        finally:
            store.close()


@th.django_unit_test()
def test_status_is_published_on_cadence_or_change(opts):
    from mojo.mojosec.store import Store

    class CountingStore(Store):
        stats_calls = 0

        def stats(self):
            CountingStore.stats_calls += 1
            return super().stats()

    class BrokenJournal:
        name = "journal"

        def poll(self, *args, **kwargs):
            raise RuntimeError("journal unavailable")

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = _store(root, CountingStore)
        try:
            runtime = _runtime(root, store)
            runtime.run_once()
            runtime.run_once()
            th.assert_eq(CountingStore.stats_calls, 1,
                         "two idle polls inside the cadence must publish status once")
            with open(runtime.config["status_path"]) as handle:
                th.assert_eq(json.load(handle)["sensor_id"], "sensor-cost",
                             "the first poll must still publish a status file")
            runtime.journal = BrokenJournal()
            runtime.run_once()
            th.assert_eq(CountingStore.stats_calls, 2,
                         "a collector flipping to error must publish inside the window")
            runtime.run_once()
            th.assert_eq(CountingStore.stats_calls, 2,
                         "an unchanged error must wait for the cadence")
            runtime._publish_status(force=True)
            th.assert_eq(CountingStore.stats_calls, 3,
                         "a forced publish (shutdown) must always write")
        finally:
            store.close()


@th.django_unit_test()
def test_fim_scan_without_changes_skips_baseline_rewrite(opts):
    from mojo.mojosec.store import Store

    baseline = {f"/etc/file{index}": _entry(index) for index in range(5)}
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = _store(root)
        try:
            store.record_fim_scan("cost:fast", baseline, [], True)
            statements = _traced(store)
            store.record_fim_scan("cost:fast", dict(baseline), [], True, baseline=baseline)
            th.assert_eq([item for item in statements if "fim_baseline" in item], [],
                         "a no-change complete scan must write no baseline rows")
            changed = dict(baseline)
            changed["/etc/file1"] = _entry(100)
            del changed["/etc/file2"]
            statements.clear()
            store.record_fim_scan("cost:fast", changed, [], True, baseline=baseline)
            touched = [item for item in statements if "fim_baseline" in item]
            th.assert_eq(len([item for item in touched if item.startswith("INSERT")]), 1,
                         f"one modified path must be one upsert: {touched}")
            th.assert_eq(len([item for item in touched if item.startswith("DELETE")]), 1,
                         f"one removed path must be one delete: {touched}")
            th.assert_eq(store.load_fim_baseline("cost:fast"), changed,
                         "the delta write must leave exactly the new snapshot")
            statements.clear()
            store.record_fim_scan("cost:fast", baseline, [], True)
            th.assert_true(any("DELETE FROM fim_baseline WHERE profile" in item
                               for item in statements),
                           "baseline=None must keep the full rewrite")
            th.assert_eq(store.load_fim_baseline("cost:fast"), baseline,
                         "the full rewrite must leave exactly the snapshot")
            store.db.set_trace_callback(None)
        finally:
            store.close()

    class FlakyStore(Store):
        fail_next = False
        recorded = []

        def active_fim_profile(self):
            return IDENTITY

        def record_fim_scan(self, profile, snapshot, observations, complete, baseline=None):
            if FlakyStore.fail_next:
                FlakyStore.fail_next = False
                raise RuntimeError("disk full")
            FlakyStore.recorded.append(len(observations))
            return super().record_fim_scan(
                profile, snapshot, observations, complete, baseline=baseline)

    class Collector:
        baseline_key = "cost:fast"
        config = {"interval_seconds": 60}

        def __init__(self):
            self.snapshot = dict(baseline)

        def scan(self, previous):
            return {"snapshot": dict(self.snapshot), "complete": True}

        def diff(self, previous, scan):
            return [_change(path) for path in sorted(set(previous) | set(scan["snapshot"]))
                    if previous.get(path) != scan["snapshot"].get(path)]

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = _store(root, FlakyStore)
        try:
            store.record_fim_scan("cost:fast", baseline, [], True)
            runtime = _runtime(root, store)
            collector = Collector()
            runtime.profile_identity = IDENTITY
            runtime.integrity_collectors = {"fast": collector}
            runtime._poll_integrity()
            th.assert_eq(runtime.baselines.get("cost:fast"), baseline,
                         "a fast tier keeps its committed baseline cached")
            collector.snapshot["/etc/file3"] = _entry(300)
            FlakyStore.fail_next = True
            runtime.last_integrity = {}
            runtime._poll_integrity()
            th.assert_eq(runtime.baselines.get("cost:fast"), baseline,
                         "a rolled-back write must not advance the cached baseline")
            runtime.last_integrity = {}
            runtime._poll_integrity()
            th.assert_eq(FlakyStore.recorded[-1], 1,
                         "the change must be raised on the scan after a failed write")
            th.assert_eq(store.load_fim_baseline("cost:fast")["/etc/file3"], _entry(300),
                         "the retried write must commit the change")
        finally:
            store.close()


@th.django_unit_test()
def test_manifest_is_parsed_once_per_stat_generation(opts):
    from mojo.mojosec.expected_changes import ExpectedChangeError, load_manifest

    def write(path, deployment):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"schema": "mojosec.expected_changes", "version": 1, "entries": [{
                "path": "/etc/app.conf", "change": "modified", "sha256": "a" * 64,
                "expires_at": "2099-01-01T00:00:00Z", "deployment_id": deployment,
            }]}, handle)
        os.chmod(path, 0o600)

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "expected.json")
        write(path, "deploy-1")
        first = load_manifest(path, require_root=False)
        th.assert_true(load_manifest(path, require_root=False) is first,
                       "an unchanged manifest must not be parsed again")
        write(path, "deploy-two")
        second = load_manifest(path, require_root=False)
        th.assert_true(second is not first and second[0]["deployment_id"] == "deploy-two",
                       "a rewritten manifest must be parsed again")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        for unused in range(2):
            with th.assert_raises(ExpectedChangeError):
                load_manifest(path, require_root=False)
        os.chmod(path, 0o644)
        with th.assert_raises(ExpectedChangeError):
            load_manifest(path, require_root=False)


@th.django_unit_test()
def test_annotation_is_skipped_when_nothing_awaits(opts):
    from mojo.mojosec.store import Store

    class CountingStore(Store):
        annotations = 0

        def annotate_pending_fim(self, *args, **kwargs):
            CountingStore.annotations += 1
            return super().annotate_pending_fim(*args, **kwargs)

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = _store(root, CountingStore)
        try:
            runtime = _runtime(root, store)
            th.assert_true(not store.has_pending_annotations(),
                           "an empty spool has nothing awaiting annotation")
            runtime.run_once()
            th.assert_eq(CountingStore.annotations, 0,
                         "a poll with nothing to annotate must not open the manifest")
            store.ingest([_change("/etc/app.conf")])
            th.assert_true(store.has_pending_annotations(),
                           "an unannotated fim.change awaits annotation")
            runtime.run_once()
            th.assert_eq(CountingStore.annotations, 1,
                         "a pending fim.change must be annotated")
        finally:
            store.close()
