import gzip
import json
import os
import tempfile

from testit import helpers as th


@th.django_unit_test()
def test_profile_activation_is_all_tier_atomic_and_keeps_rollback_state(opts):
    from mojo.mojosec.store import Store, StoreError

    with tempfile.TemporaryDirectory() as root:
        store = Store(
            root, "activation-test",
            {"window_seconds": 60, "flush_count": 25, "max_aggregates": 100,
             "critical_reserve_aggregates": 10},
            {"max_spool_events": 100, "critical_reserve_events": 10,
             "retry_min_seconds": 1, "retry_max_seconds": 2},
        )
        first = {"name": "al2023-web-v1", "version": 1, "digest": "a" * 64}
        scans = {
            tier: {"tier": tier, "baseline_key": f"al2023-web-v1:{'a' * 64}:{tier}",
                   "complete": True, "snapshot": {f"/{tier}": {"kind": "file"}}}
            for tier in ("fast", "slow", "rpm")
        }
        broken = dict(scans)
        broken["rpm"] = dict(broken["rpm"], complete=False)
        with th.assert_raises(StoreError):
            store.activate_fim_profile(first, broken)
        th.assert_eq(store.active_fim_profile(), None,
                     "one incomplete tier must make initialization a complete no-op")
        store.activate_fim_profile(first, scans)
        th.assert_eq(store.active_fim_profile(), first,
                     "one exclusive transaction must select only a complete profile generation")

        second = {"name": "al2023-web-v2", "version": 2, "digest": "b" * 64}
        second_scans = {
            tier: {"tier": tier, "baseline_key": f"al2023-web-v2:{'b' * 64}:{tier}",
                   "complete": True, "snapshot": {f"/{tier}-2": {"kind": "file"}}}
            for tier in ("fast", "slow", "rpm")
        }
        store.activate_fim_profile(second, second_scans, reason="upgrade")
        rolled_back = store.rollback_fim_profile("a" * 64)
        th.assert_eq(rolled_back, first,
                     "rollback must select an intact retained name+digest generation")
        store.close()


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


class _AckTransport:
    def __init__(self):
        self.calls = []

    def post(self, endpoint, body, headers, timeout):
        self.calls.append((endpoint, body, headers, timeout))
        batch = json.loads(gzip.decompress(body))
        response = {
            "schema": "mojosec.ack", "version": 1,
            "results": [{"id": event["id"], "status": "accepted"} for event in batch["events"]],
        }
        return 200, json.dumps(response).encode("utf-8")


class _PartialAckTransport:
    def post(self, endpoint, body, headers, timeout):
        batch = json.loads(gzip.decompress(body))
        response = {
            "schema": "mojosec.ack", "version": 1,
            "results": [{"id": batch["events"][0]["id"], "status": "accepted"}],
        }
        return 200, json.dumps(response).encode("utf-8")


@th.django_unit_test()
def test_store_aggregates_durably_and_commits_cursor_with_evidence(opts):
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            store.ingest([_observation()], cursor_key="nginx", cursor={"offset": 10})
            th.assert_eq(store.stats()["spooled_events"], 0,
                         "one noisy probe should remain in the durable aggregation window")
            store.ingest([_observation()], cursor_key="nginx", cursor={"offset": 20})
            pending = store.pending_batch(100, 256 * 1024)
            th.assert_eq(len(pending), 1,
                         "reaching the aggregation threshold should queue one event")
            th.assert_eq(pending[0]["count"], 2,
                         "the queued event should preserve the aggregate occurrence count")
            th.assert_eq(store.get_meta("cursor:nginx"), {"offset": 20},
                         "the collector cursor must advance in the same transaction as evidence")
        finally:
            store.close()


@th.django_unit_test()
def test_aggregate_capacity_reserves_and_evicts_for_high_signals(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import Store

    aggregation = {
        "window_seconds": 600, "flush_count": 100, "max_aggregates": 3,
        "critical_reserve_aggregates": 1,
    }
    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor-test", aggregation, DELIVERY)
        try:
            warnings = [observation(
                "web.denied", "warning", "denied", {"path": f"/path-{index}"},
                fingerprint_values=(f"warning-{index}",), aggregate=True,
            ) for index in range(3)]
            store.ingest(warnings)
            th.assert_eq(store.stats()["pending_aggregates"], 2,
                         "low-priority cardinality must not consume the high-signal aggregate reserve")
            th.assert_eq(store.stats()["dropped_aggregate_capacity"], 1,
                         "a low-priority aggregate beyond its partition must be counted as dropped")

            high = [observation(
                "web.probe", "high", "probe", {"path": f"/probe-{index}"},
                fingerprint_values=(f"high-{index}",), aggregate=True,
            ) for index in range(2)]
            store.ingest(high)
            stats = store.stats()
            th.assert_eq(stats["pending_aggregates"], 3,
                         "high signals should occupy the reserve without exceeding the hard cap")
            th.assert_eq(stats["aggregate_evicted_for_priority"], 1,
                         "a high signal at the hard cap should flush one low-priority victim")
            th.assert_eq(stats["spooled_events"], 1,
                         "the evicted low-priority aggregate should be preserved in the durable spool")
        finally:
            store.close()


@th.django_unit_test()
def test_fim_baseline_and_event_are_updated_atomically(opts):
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            first = {"/etc/example": {"kind": "file", "sha256": "a" * 64}}
            store.record_fim_scan("profile", first, [], complete=True)
            th.assert_true(store.fim_initialized("profile"),
                           "a complete first scan should establish the persistent FIM baseline")
            th.assert_eq(store.load_fim_baseline("profile"), first,
                         "the complete FIM baseline should survive a store readback")

            changed = {"/etc/example": {"kind": "file", "sha256": "b" * 64}}
            store.record_fim_scan("profile", changed, [_observation(aggregate=False)], complete=True)
            th.assert_eq(store.load_fim_baseline("profile"), changed,
                         "the changed baseline must commit with its queued evidence")
            th.assert_eq(store.stats()["spooled_events"], 1,
                         "the FIM transaction should durably queue its immediate evidence")
        finally:
            store.close()


@th.django_unit_test()
def test_sender_gzips_batches_and_removes_only_acknowledged_events(opts):
    from mojo.mojosec.sender import Sender
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        credential = os.path.join(root, "credential")
        with open(credential, "w", encoding="utf-8") as handle:
            handle.write("secret-api-key\n")
        os.chmod(credential, 0o600)
        store = Store(os.path.join(root, "state"), "sensor-test", AGGREGATION, DELIVERY)
        store.ingest([_observation(aggregate=False)])
        transport = _AckTransport()
        config = {
            "sensor_id": "sensor-test", "policy_revision": "one",
            "endpoint": "https://incident.example/api/incident/mojosec/batch",
            "credential_path": credential, "delivery": DELIVERY,
        }
        try:
            result = Sender(config, store, transport=transport).send_once()
            th.assert_eq(result["accepted"], 1,
                         "a per-item accepted acknowledgement should drain one event")
            th.assert_eq(store.stats()["spooled_events"], 0,
                         "only an acknowledged event may be removed from the durable spool")
            headers = transport.calls[0][2]
            th.assert_eq(headers["Authorization"], "apikey secret-api-key",
                         "delivery must use the existing per-installation API-key scheme")
            th.assert_eq(headers["Content-Encoding"], "gzip",
                         "bounded batches should be compressed when configured")
        finally:
            store.close()


@th.django_unit_test()
def test_partial_ack_retries_only_the_unacknowledged_event(opts):
    from mojo.mojosec.sender import Sender
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        credential = os.path.join(root, "credential")
        with open(credential, "w", encoding="utf-8") as handle:
            handle.write("secret-api-key\n")
        os.chmod(credential, 0o600)
        store = Store(os.path.join(root, "state"), "sensor-test", AGGREGATION, DELIVERY)
        store.ingest([_observation("192.0.2.1", aggregate=False),
                      _observation("192.0.2.2", aggregate=False)])
        config = {
            "sensor_id": "sensor-test", "policy_revision": "one",
            "endpoint": "https://incident.example/api/incident/mojosec/batch",
            "credential_path": credential, "delivery": DELIVERY,
        }
        try:
            result = Sender(config, store, transport=_PartialAckTransport()).send_once()
            th.assert_eq(result["accepted"], 1,
                         "the explicit accepted result should drain exactly one event")
            th.assert_eq(result["retry"], 1,
                         "an omitted per-item acknowledgement must schedule only that event for retry")
            th.assert_eq(store.stats()["spooled_events"], 1,
                         "the unacknowledged event must remain durable in SQLite")
        finally:
            store.close()


@th.django_unit_test()
def test_runtime_publishes_secret_free_status_with_collectors_disabled(opts):
    from mojo.mojosec.runtime import Runtime

    class IdleSender:
        def send_once(self):
            return {"sent": 0, "accepted": 0, "retry": 0}

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        status_path = os.path.join(root, "run", "status.json")
        config = {
            "sensor_id": "sensor-test", "state_dir": os.path.join(root, "state"),
            "status_path": status_path, "poll_seconds": 5,
            "aggregation": AGGREGATION, "delivery": DELIVERY,
            "collectors": {
                "journal": {"enabled": False}, "nginx": {"enabled": False},
                "fim": {"enabled": False},
            },
        }
        runtime = Runtime(config, sender=IdleSender())
        try:
            runtime.run_once()
            with open(status_path, encoding="utf-8") as handle:
                status_text = handle.read()
            status = json.loads(status_text)
            th.assert_eq(status["schema"], "mojosec.status",
                         "the service status file should carry a versioned public schema")
            th.assert_eq(status["spooled_events"], 0,
                         "status should expose spool health without reading the private database")
            th.assert_true("credential" not in status_text and "secret" not in status_text,
                           "the public status snapshot must not expose credentials or secrets")
        finally:
            runtime.store.close()
