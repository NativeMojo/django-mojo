"""Moved from tests/test_mojosec/store_sender.py (maestro #2558).

These tests mock production module attributes process-globally
(`mojo.mojosec.store.diagnostic_override` / `select_health_fields` and
`mojo.deploy.audit.read_health`) — unsafe under the parallel default tier.
The store/sender spool, aggregation, FIM, and delivery contracts stay in the
default-tier tests/test_mojosec/store_sender.py.
"""

import tempfile
import time
from unittest import mock

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


def _observation(source="192.0.2.1", aggregate=True):
    from mojo.mojosec.events import observation

    return observation(
        "web.probe", "high", "Known exploit path probe",
        attributes={"source_ip": source, "path": "/wp-login.php", "status": 404},
        fingerprint_values=(source, "/wp-login.php"), aggregate=aggregate,
        recommendation="block_ip", observed_at="2026-08-08T12:00:00Z",
    )


def _local_systemd_user_observation():
    from mojo.mojosec.events import observation

    return observation(
        "auth.session_open", "info", "PAM service session opened",
        attributes={
            "attribution_provenance": "none",
            "service": "systemd-user", "target_user": "www", "target_uid": 80,
            "opener_uid": 0, "producer_uid": 0, "producer_pid": 4123,
            "producer_comm": "(systemd)",
            "producer_exe": "/usr/lib/systemd/systemd",
            "systemd_unit": "user@80.service", "boot_id": "b" * 32,
            "audit_session": 44, "audit_loginuid": 80,
        },
        fingerprint_values=("systemd-user", "www", "2026-08-08T12:00:00Z"),
        aggregate=False, recommendation="none",
        observed_at="2026-08-08T12:00:00Z",
    )


@th.django_unit_test()
def test_local_only_diagnostic_and_stale_reconciliation_are_bounded(opts):
    import copy
    import datetime

    import mojo.mojosec.store as store_module
    from mojo.mojosec.protocol import make_event

    with tempfile.TemporaryDirectory() as root:
        large_delivery = dict(DELIVERY, max_spool_events=1000, critical_reserve_events=100)
        store = store_module.Store(root, "sensor-test", AGGREGATION, large_delivery)
        active = {"active": True, "until": "2026-08-08T12:30:00Z", "error": ""}
        inactive = {"active": False, "until": "", "error": ""}
        with mock.patch.object(store_module, "diagnostic_override", return_value=active):
            try:
                store.ingest([_local_systemd_user_observation()])
                th.assert_eq(store.stats()["local_only_diagnostic_delivered"], 1,
                             "diagnostic delivery counts only an original ingestion that spooled")
            finally:
                store.close()
            reopened = store_module.Store(root, "sensor-test", AGGREGATION, large_delivery)
            try:
                reopened.ingest([_observation(aggregate=False)])
                ordinary = reopened.pending_batch(1, 65536)
                th.assert_eq([item["kind"] for item in ordinary], ["web.probe"],
                             "active diagnostics must never starve ordinary delivery")
                reopened.mark_delivery(
                    [ordinary[0]["id"]],
                    [{"id": ordinary[0]["id"], "status": "accepted"}],
                )
                th.assert_eq(len(reopened.pending_batch(10, 65536)), 1,
                             "an admitted diagnostic must survive restart while override is active")
            finally:
                reopened.close()
        reopened = store_module.Store(root, "sensor-test", AGGREGATION, large_delivery)
        try:
            with mock.patch.object(
                    store_module, "diagnostic_override", return_value=inactive):
                th.assert_eq(reopened.pending_batch(10, 65536), [],
                             "an expired override must suppress its queued diagnostic")
            th.assert_eq(reopened.stats()["local_only_suppressed"], 1,
                         "queued diagnostic reconciliation must count suppression once")
        finally:
            reopened.close()

    with tempfile.TemporaryDirectory() as root:
        store = store_module.Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            store.ingest([_local_systemd_user_observation()])
            store.set_meta("local_only_observed", 2 ** 63 - 1)
            store.set_meta("local_only_suppressed", 2 ** 63 - 1)
            fractional = _local_systemd_user_observation()
            fractional["observed_at"] = "2026-08-08T12:00:00.500000Z"
            store.ingest([fractional])
            older = _local_systemd_user_observation()
            older["observed_at"] = "2026-08-08T11:00:00Z"
            store.ingest([older])
            saturated = store.stats()
            th.assert_eq(saturated["local_only_observed"], 2 ** 63 - 1,
                         "local observed counters must saturate instead of overflowing")
            th.assert_eq(saturated["local_only_suppressed"], 2 ** 63 - 1,
                         "local suppressed counters must saturate instead of overflowing")
            th.assert_eq(
                saturated["local_only_last_seen"], "2026-08-08T12:00:00.500000Z",
                "last_seen must compare validated instants, including fractional seconds")
        finally:
            store.close()

    with tempfile.TemporaryDirectory() as root:
        large_delivery = dict(DELIVERY, max_spool_events=1000, critical_reserve_events=100)
        store = store_module.Store(root, "sensor-test", AGGREGATION, large_delivery)
        try:
            base = _local_systemd_user_observation()
            started = datetime.datetime(2026, 8, 8, 12, tzinfo=datetime.timezone.utc)
            for index in range(store_module.LOCAL_ONLY_RECONCILE_LIMIT + 44):
                found = copy.deepcopy(base)
                found["observed_at"] = (
                    started + datetime.timedelta(seconds=index)).isoformat().replace("+00:00", "Z")
                found["fingerprint"] = f"{index:064x}"
                store._enqueue(make_event("sensor-test", found), now=index + 1)
            store.db.execute("UPDATE events SET delivery_class = 'legacy'")
            for delivery_class, projection in (
                    ("local_only_diagnostic", "id"), ("legacy", "id, payload")):
                plan = store.db.execute(
                    f"EXPLAIN QUERY PLAN SELECT {projection} FROM events "
                    "WHERE delivery_class = ? ORDER BY created, id LIMIT ?",
                    (delivery_class, store_module.LOCAL_ONLY_RECONCILE_LIMIT),
                ).fetchall()
                detail = " ".join(row["detail"] for row in plan)
                th.assert_in(
                    "events_delivery_class_created", detail,
                    f"{delivery_class} reconciliation must use the class/order index")
                th.assert_in("SEARCH events", detail,
                             "reconciliation must seek to one class, not scan all events")
                th.assert_true("USE TEMP B-TREE" not in detail,
                               f"reconciliation order must not sort a whole class: {detail}")
            store._enqueue(make_event("sensor-test", _observation(aggregate=False)), now=999)
            pending = store.pending_batch(10, 65536)
            th.assert_eq([item["kind"] for item in pending], ["web.probe"],
                         "bounded stale cleanup must not starve a later ordinary event")
            th.assert_eq(store.stats()["local_only_suppressed"],
                         store_module.LOCAL_ONLY_RECONCILE_LIMIT,
                         "one sender pass must reconcile only the fixed stale-row ceiling")
        finally:
            store.close()


@th.django_unit_test()
def test_runtime_requires_post_poll_audit_health_bracket(opts):
    from mojo.mojosec.runtime import Runtime
    from mojo.deploy import audit

    base = {
        "schema": "mojosec.audit-health", "version": 1, "boot_id": "a" * 32,
        "generation": "b" * 64, "rules_sha256": "c" * 64,
        "sequence": 4, "enabled": 1, "failure": 1, "rate_limit": 0,
        "backlog_limit": 8192, "backlog": 0, "lost": 0,
        "updated_at": time.time(),
    }

    class FakeStore:
        def __init__(self):
            self.health = []

        def get_meta(self, key):
            return None

        def load_ssh_sessions(self):
            return []

        def load_audit_fragments(self):
            return {}

        def ingest(self, observations, **values):
            self.health.append(values["audit_health"])

    class FakeJournal:
        name = "journal"

        def poll(self, *args, **kwargs):
            return {"observations": [], "cursor": "c1", "malformed": 0}

    runtime = Runtime.__new__(Runtime)
    runtime.store = FakeStore()
    runtime.collector_status = {}
    for changed in (dict(base, sequence=5, lost=1),
                    dict(base, sequence=5, rules_sha256="d" * 64)):
        with mock.patch.object(audit, "read_health", side_effect=[base, changed]):
            runtime._poll_stream(FakeJournal())
        th.assert_true(runtime.store.health[-1]["healthy"] is False,
                       "loss or rule drift during collection must fail open ordinary")

    healthy_post = dict(base, sequence=5, updated_at=base["updated_at"] + 0.01)
    with mock.patch.object(audit, "read_health", side_effect=[base, healthy_post]):
        runtime._poll_stream(FakeJournal())
    th.assert_true(runtime.store.health[-1]["healthy"] is True,
                   "only a healthy matching post-poll sidecar may authorize proof")


@th.django_unit_test()
def test_store_uses_the_audit_contract_for_previous_health(opts):
    from mojo.deploy import audit
    from mojo.mojosec import store as store_module

    health = {
        "schema": audit.HEALTH_SCHEMA, "version": 1,
        "boot_id": "a" * 32, "generation": "b" * 64,
        "rules_sha256": "c" * 64, "sequence": 4,
        "enabled": 1, "failure": 1, "rate_limit": 0,
        "backlog_limit": 8192, "backlog": 0, "lost": 0,
        "updated_at": time.time(), "healthy": True, "reason": "",
    }
    with tempfile.TemporaryDirectory() as root, \
            mock.patch.object(
                store_module, "select_health_fields",
                wraps=audit.select_health_fields) as selector:
        store = store_module.Store(root, "health-contract-test", AGGREGATION, DELIVERY)
        try:
            store.ingest([], audit_health=health)
            previous = store.get_meta("audit_health")
            th.assert_eq(set(previous), set(audit.HEALTH_FIELDS),
                         "durable previous-health state must use the publisher selector")
            th.assert_true("healthy" not in previous and "reason" not in previous,
                           "runtime annotations must not become prior external health")
            selector.assert_called_once_with(health)
        finally:
            store.close()
