import datetime
import gzip
import json
import os
import tempfile
from unittest import mock

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
        second_keys = {
            tier: f"al2023-web-v2:{'b' * 64}:{tier}"
            for tier in ("fast", "slow", "rpm")
        }
        drifted = store.fim_profile_health(second, second_keys)
        th.assert_eq(store.active_fim_profile(), first,
                     "selecting v2 in desired config must not mutate an active v1 baseline")
        th.assert_eq(drifted["active"], False,
                     "v2 must remain inactive before the explicit activation ceremony")
        th.assert_eq(drifted["digest_drift"], True,
                     "a configured-v2 versus active-v1 mismatch must fail closed as digest drift")
        second_scans = {
            tier: {"tier": tier, "baseline_key": f"al2023-web-v2:{'b' * 64}:{tier}",
                   "complete": True, "snapshot": {f"/{tier}-2": {"kind": "file"}}}
            for tier in ("fast", "slow", "rpm")
        }
        store.activate_fim_profile(second, second_scans, reason="upgrade")
        th.assert_eq(store.active_fim_profile(), second,
                     "only explicit complete activation may select the v2 generation")
        rolled_back = store.rollback_fim_profile("a" * 64)
        th.assert_eq(rolled_back, first,
                     "rollback must select an intact retained name+digest generation")
        store.close()


@th.django_unit_test()
def test_fim_evidence_is_durable_before_v2_grace_annotation(opts):
    import json

    from mojo.mojosec.events import observation
    from mojo.mojosec.expected_changes import evidence_digest
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "grace-test", AGGREGATION, DELIVERY)
        path = "/etc/example.conf"
        digest = "a" * 64
        evidence = {
            "kind": "file", "mode": 0o644, "uid": 0, "gid": 0,
            "size": 12, "mtime_ns": 100, "ctime_ns": 200,
            "device": 300, "inode": 400, "sha256": digest,
        }
        found = observation(
            "fim.change", "high", "integrity change",
            attributes={"path": path, "change": "modified", **evidence},
            fingerprint_values=(path, digest), aggregate=False, recommendation="review")
        store.ingest([found])
        th.assert_eq(store.stats()["spooled_events"], 1,
                     "an unmatched change must be durable immediately during annotation grace")
        th.assert_eq(store.pending_batch(10, 65536), [],
                     "unmatched FIM delivery should wait only for the short bounded grace")

        manifest_path = os.path.join(root, "expected.json")
        completed = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
        started = completed - datetime.timedelta(seconds=10)
        expiry = completed + datetime.timedelta(minutes=10)
        evidence_kind, evidence_sha256 = evidence_digest(evidence)
        manifest = {
            "schema": "mojosec.expected_changes", "version": 2,
            "entries": [{
                "path": path, "change": "modified", "sha256": evidence_sha256,
                "evidence_kind": evidence_kind,
                "expires_at": expiry.isoformat(), "deployment_id": "deploy-1",
                "operation_id": "deploy-1-render", "operation_kind": "rendered-config",
                "started_at": started.isoformat(),
                "completed_at": completed.isoformat(),
            }],
        }
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        os.chmod(manifest_path, 0o600)
        th.assert_eq(store.annotate_pending_fim(manifest_path), 1,
                     "a completed exact operation may enrich already-durable evidence")
        pending = store.pending_batch(10, 65536)
        th.assert_eq(pending[0]["attributes"]["expected_change"]["operation_id"],
                     "deploy-1-render",
                     "grace enrichment must preserve bounded operation identity")
        th.assert_true("sha256" not in pending[0]["attributes"],
                       "content digests must stay in the private spool, not the wire event")
        th.assert_true(all(field not in pending[0]["attributes"] for field in (
            "mtime_ns", "ctime_ns", "device", "inode")),
            "comparison-only file identity must remain out of delivered/replay events")
        stored = json.loads(store.db.execute(
            "SELECT payload FROM events WHERE id = ?", (pending[0]["id"],)
        ).fetchone()["payload"])
        th.assert_eq(stored["attributes"]["sha256"], digest,
                     "wire scrubbing must retain private evidence for later local matching")
        th.assert_eq(stored["attributes"]["inode"], 400,
                     "private comparison metadata must remain available for local matching")
        store.close()


@th.django_unit_test()
def test_fim_grace_only_reconciles_virgin_time_correlated_rows(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.expected_changes import evidence_digest
    from mojo.mojosec.store import Store

    def found(path, digest):
        return observation(
            "fim.change", "high", "integrity change",
            attributes={
                "path": path, "change": "modified", "kind": "file",
                "mode": 0o644, "uid": 0, "gid": 0, "size": 12,
                "sha256": digest,
            },
            fingerprint_values=(path, digest), aggregate=False,
            recommendation="review",
        )

    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "grace-eligibility", AGGREGATION, DELIVERY)
        now = datetime.datetime.now(datetime.timezone.utc)
        retry_path = "/etc/retry.conf"
        old_path = "/etc/old.conf"
        retry_digest = "b" * 64
        old_digest = "c" * 64
        store.ingest([found(retry_path, retry_digest), found(old_path, old_digest)])
        rows = store.db.execute("SELECT id, payload FROM events ORDER BY created").fetchall()
        ids = {json.loads(row["payload"])["attributes"]["path"]: row["id"] for row in rows}

        # Simulate a previously attempted event even if a later bug or migration
        # were to restore a grace deadline. It must never be relabelled.
        store.db.execute(
            "UPDATE events SET attempts=1, last_error='offline', annotation_deadline=? "
            "WHERE id=?", (now.timestamp() + 60, ids[retry_path]))
        # A row observed well before this operation must not match merely because
        # the exact path and resulting bytes happen to agree.
        store.db.execute(
            "UPDATE events SET created=?, annotation_deadline=? WHERE id=?",
            (now.timestamp() - 600, now.timestamp() + 60, ids[old_path]))

        manifest_path = os.path.join(root, "expected.json")
        entries = []
        for path, digest in ((retry_path, retry_digest), (old_path, old_digest)):
            evidence = {
                "kind": "file", "mode": 0o644, "uid": 0, "gid": 0,
                "size": 12, "sha256": digest,
            }
            kind, evidence_sha256 = evidence_digest(evidence)
            entries.append({
                "path": path, "change": "modified", "sha256": evidence_sha256,
                "evidence_kind": kind,
                "expires_at": (now + datetime.timedelta(minutes=10)).isoformat(),
                "deployment_id": "deploy-eligibility",
                "operation_id": "deploy-eligibility-render",
                "operation_kind": "rendered-config",
                "started_at": (now - datetime.timedelta(seconds=2)).isoformat(),
                "completed_at": (now - datetime.timedelta(seconds=1)).isoformat(),
            })
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump({
                "schema": "mojosec.expected_changes", "version": 2,
                "entries": entries,
            }, handle)
        os.chmod(manifest_path, 0o600)

        th.assert_eq(store.annotate_pending_fim(manifest_path, now=now.timestamp()), 0,
                     "retry-delayed and pre-operation rows must remain unexplained")
        payloads = [json.loads(row["payload"]) for row in store.db.execute(
            "SELECT payload FROM events").fetchall()]
        th.assert_true(all("expected_change" not in event["attributes"] for event in payloads),
                       "ineligible rows must not acquire expected-change annotations")
        store.close()


@th.django_unit_test()
def test_active_exact_path_extends_only_the_bounded_local_grace(opts):
    from mojo.mojosec.events import observation
    from mojo.deploy.mojosec_changes import MAX_TTL_SECONDS
    from mojo.mojosec.expected_changes import MAX_OPERATION_CORRELATION_SECONDS
    from mojo.mojosec.store import ANNOTATION_MAX_GRACE_SECONDS, Store

    th.assert_eq(
        ANNOTATION_MAX_GRACE_SECONDS,
        MAX_TTL_SECONDS + MAX_OPERATION_CORRELATION_SECONDS,
        "the active hold must cover the supported operation and post-completion window",
    )

    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "grace-extension", AGGREGATION, DELIVERY)
        path = "/etc/active.conf"
        digest = "d" * 64
        store.ingest([observation(
            "fim.change", "high", "integrity change",
            attributes={
                "path": path, "change": "modified", "kind": "file",
                "mode": 0o644, "uid": 0, "gid": 0, "size": 12,
                "sha256": digest,
            },
            fingerprint_values=(path, digest), aggregate=False,
            recommendation="review",
        )])
        row = store.db.execute(
            "SELECT id, created, annotation_deadline FROM events").fetchone()
        original = row["annotation_deadline"]
        observed = row["created"]

        th.assert_eq(store.annotate_pending_fim(
            "", active_paths=[path], now=observed + 110), 0,
            "an active operation may extend grace without fabricating an annotation")
        extended = store.db.execute(
            "SELECT annotation_deadline FROM events WHERE id=?", (row["id"],)
        ).fetchone()["annotation_deadline"]
        th.assert_true(extended > original,
                       "only an exact active path should keep its virgin event local longer")
        th.assert_true(extended <= observed + ANNOTATION_MAX_GRACE_SECONDS,
                       "active-operation grace must remain bounded from original observation time")
        store.close()


@th.django_unit_test()
def test_symlink_target_digest_is_private_to_the_local_spool(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "link-privacy", AGGREGATION, DELIVERY)
        target_digest = "e" * 64
        store.ingest([observation(
            "fim.change", "high", "integrity change",
            attributes={
                "path": "/etc/current", "change": "modified", "kind": "symlink",
                "mode": 0o777, "uid": 0, "gid": 0, "size": 6,
                "target_sha256": target_digest,
            },
            fingerprint_values=("/etc/current", target_digest), aggregate=False,
            recommendation="review",
        )])
        store.db.execute("UPDATE events SET next_attempt=0, annotation_deadline=0")
        pending = store.pending_batch(10, 65536)
        th.assert_true("target_sha256" not in pending[0]["attributes"],
                       "symlink target digests must not be delivered or replayed")
        stored = json.loads(store.db.execute("SELECT payload FROM events").fetchone()["payload"])
        th.assert_eq(stored["attributes"]["target_sha256"], target_digest,
                     "private symlink evidence must remain available for local matching")
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
def test_exact_systemd_user_lifecycle_is_local_only_and_cursor_atomic(opts):
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            store.ingest(
                [_local_systemd_user_observation()],
                cursor_key="journal", cursor="cursor-local-only",
            )
            stats = store.stats()
            th.assert_eq(stats["spooled_events"], 0,
                         "the exact trusted systemd-user lifecycle must stay off the fleet spool")
            th.assert_eq(stats["local_only_observed"], 1,
                         "local suppression must remain measurable as an observed occurrence")
            th.assert_eq(stats["local_only_suppressed"], 1,
                         "default local-only handling must count one suppressed occurrence")
            th.assert_eq(store.get_meta("cursor:journal"), "cursor-local-only",
                         "local counters and the collector cursor must commit atomically")
        finally:
            store.close()


@th.django_unit_test()
def test_local_only_counter_and_cursor_transaction_rolls_back_on_cursor_failure(opts):
    import mojo.mojosec.store as store_module

    with tempfile.TemporaryDirectory() as root:
        store = store_module.Store(root, "sensor-test", AGGREGATION, DELIVERY)
        original = store.set_meta

        def fail_cursor(key, value):
            if key == "cursor:journal":
                raise RuntimeError("injected cursor failure")
            return original(key, value)

        try:
            with mock.patch.object(store, "set_meta", side_effect=fail_cursor):
                with th.assert_raises(RuntimeError):
                    store.ingest(
                        [_local_systemd_user_observation()],
                        cursor_key="journal", cursor="must-not-commit")
            stats = store.stats()
            th.assert_eq(stats["local_only_observed"], 0,
                         "cursor failure must roll back the observed counter")
            th.assert_eq(stats["local_only_suppressed"], 0,
                         "cursor failure must roll back the suppressed counter")
            th.assert_eq(stats["local_only_last_seen"], None,
                         "cursor failure must roll back local-only last_seen")
            th.assert_eq(store.get_meta("cursor:journal"), None,
                         "cursor failure must leave the collector cursor unchanged")
        finally:
            store.close()


@th.django_unit_test()
def test_protocol_valid_local_only_near_match_stays_spooled(opts):
    import copy

    from mojo.mojosec.store import Store

    near_match = copy.deepcopy(_local_systemd_user_observation())
    near_match["attributes"]["source_ip"] = None
    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            store.ingest([near_match])
            pending = store.pending_batch(10, 65536)
            th.assert_eq(len(pending), 1,
                         "a protocol-valid explicit-null near match must remain spooled")
            th.assert_eq(pending[0]["attributes"]["source_ip"], None,
                         "ordinary near-match raw evidence must remain unchanged on the wire")
        finally:
            store.close()


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
def test_incomplete_fim_scan_never_advances_baseline_and_queues_one_overflow(opts):
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import Store

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        store = Store(root, "sensor-test", AGGREGATION, DELIVERY)
        try:
            baseline = {"/etc/example": {"kind": "file", "sha256": "a" * 64}}
            store.record_fim_scan("profile", baseline, [], complete=True)
            th.assert_true(store.fim_initialized("profile"),
                           "a complete first scan must initialize the FIM baseline")

            partial = {"/etc/example": {"kind": "file", "sha256": "b" * 64}}
            for index in range(3):
                overflow = observation(
                    "fim.overflow", "critical", "Filesystem integrity scan was incomplete",
                    attributes={"profile": "profile", "tier": "fast", "entries": 1},
                    fingerprint_values=("profile", "fast"), aggregate=False,
                    observed_at=f"2026-08-11T00:00:0{index}Z",
                )
                store.record_fim_scan("profile", partial, [overflow], complete=False)

            th.assert_eq(store.load_fim_baseline("profile"), baseline,
                         "an incomplete scan must never advance the persistent baseline")
            th.assert_true(store.fim_initialized("profile"),
                           "incomplete scans must not clear the initialized marker")
            th.assert_eq(store.stats()["spooled_events"], 3,
                         "each incomplete interval must queue exactly its one overflow event")

            changed = {"/etc/example": {"kind": "file", "sha256": "c" * 64}}
            change = observation(
                "fim.change", "high", "Targeted filesystem integrity change",
                attributes={"path": "/etc/example", "change": "modified"},
                fingerprint_values=("/etc/example", "modified", "c" * 64),
                aggregate=False, observed_at="2026-08-11T00:01:00Z",
            )
            store.record_fim_scan("profile", changed, [change], complete=True)
            th.assert_eq(store.load_fim_baseline("profile"), changed,
                         "the next complete scan must advance the baseline with its evidence")
            th.assert_eq(store.stats()["spooled_events"], 4,
                         "the complete reconcile scan must queue its change event")
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


@th.django_unit_test()
def test_runtime_reconciles_pending_even_when_journal_fails(opts):
    from mojo.mojosec.runtime import Runtime

    class FakeStore:
        def __init__(self):
            self.reconciled = 0

        def get_meta(self, key):
            return None

        def load_ssh_sessions(self):
            return []

        def load_audit_fragments(self):
            return {}

        def annotate_pending_fim(self, *args, **kwargs):
            return 0

        def reconcile_pending_firewall(self):
            self.reconciled += 1

    class BrokenJournal:
        name = "journal"

        def poll(self, *args, **kwargs):
            raise RuntimeError("journal unavailable")

    runtime = Runtime.__new__(Runtime)
    runtime.config = {"expected_changes_path": ""}
    runtime.store = FakeStore()
    runtime.journal = BrokenJournal()
    runtime.nginx = None
    runtime.fim = None
    runtime.integrity_collectors = {}
    runtime.collector_status = {}
    runtime.sender = mock.Mock(send_once=mock.Mock(return_value={"accepted": 0}))
    runtime._publish_status = mock.Mock()
    runtime.run_once()
    th.assert_eq(runtime.store.reconciled, 1,
                 "pending deadlines must reconcile on every cycle despite collector failure")


@th.django_unit_test()
def test_system_python_tier_is_independent_of_incomplete_fast_snapshot(opts):
    from mojo.mojosec.runtime import Runtime

    identity = {"name": "al2023-web-test", "version": 1, "digest": "f" * 64}
    fast_key = f"al2023-web-test:{'f' * 64}:fast"
    slow_key = f"al2023-web-test:{'f' * 64}:slow"
    rpm_key = f"al2023-web-test:{'f' * 64}:rpm"
    kept = "/usr/lib/python3.11/site-packages/kept.py"
    fast_baseline = {
        kept: {"kind": "file", "sha256": "1" * 64},
        "/etc/example.conf": {"kind": "file", "sha256": "2" * 64},
    }
    slow_baseline = {"/usr/bin/example": {"kind": "file", "sha256": "3" * 64}}
    rpm_baseline = {kept: {"kind": "file", "sha256": "1" * 64}}
    partial_fast = {"/etc/example.conf": {"kind": "file", "sha256": "2" * 64}}

    class FakeTier:
        def __init__(self, baseline_key, result):
            self.config = {"interval_seconds": 0}
            self.baseline_key = baseline_key
            self.result = result

        def scan(self, previous):
            return dict(self.result, snapshot=dict(self.result["snapshot"]))

        def diff(self, baseline, scan):
            return []

    class FakeSystemPython:
        def __init__(self):
            self.config = {"interval_seconds": 0}
            self.baseline_key = rpm_key
            self.seen_previous = []

        def scan(self, previous):
            self.seen_previous.append(previous)
            return {
                "profile": identity["digest"], "baseline_key": rpm_key,
                "tier": "rpm", "snapshot": dict(rpm_baseline), "complete": True,
                "descriptor_safe": True, "entries": len(rpm_baseline),
            }

        def diff(self, baseline, scan):
            return []

    class IdleSender:
        def send_once(self):
            return {"sent": 0, "accepted": 0, "retry": 0}

    with tempfile.TemporaryDirectory() as root:
        os.chmod(root, 0o700)
        config = {
            "sensor_id": "sensor-test", "state_dir": os.path.join(root, "state"),
            "status_path": os.path.join(root, "run", "status.json"),
            "poll_seconds": 5, "aggregation": AGGREGATION, "delivery": DELIVERY,
            "collectors": {
                "journal": {"enabled": False}, "nginx": {"enabled": False},
                "fim": {"enabled": False},
            },
        }
        runtime = Runtime(config, sender=IdleSender())
        try:
            store = runtime.store
            store.activate_fim_profile(identity, {
                "fast": {"tier": "fast", "baseline_key": fast_key,
                         "complete": True, "snapshot": fast_baseline},
                "slow": {"tier": "slow", "baseline_key": slow_key,
                         "complete": True, "snapshot": slow_baseline},
                "rpm": {"tier": "rpm", "baseline_key": rpm_key,
                        "complete": True, "snapshot": rpm_baseline},
            })
            runtime.profile = {"name": identity["name"]}
            runtime.profile_identity = identity
            system_python = FakeSystemPython()
            incomplete_fast = FakeTier(fast_key, {
                "profile": identity["digest"], "baseline_key": fast_key,
                "tier": "fast", "snapshot": partial_fast, "complete": False,
                "descriptor_safe": True, "entries": len(partial_fast),
            })
            runtime.integrity_collectors = {
                "fast": incomplete_fast,
                "slow": FakeTier(slow_key, {
                    "profile": identity["digest"], "baseline_key": slow_key,
                    "tier": "slow", "snapshot": dict(slow_baseline), "complete": True,
                    "descriptor_safe": True, "entries": len(slow_baseline),
                }),
                "rpm": system_python,
            }
            runtime.last_integrity = {}
            runtime.integrity_scans = {}
            spooled_before = store.stats()["spooled_events"]

            runtime._poll_integrity()

            th.assert_eq(system_python.seen_previous, [rpm_baseline],
                         "the system-Python tier must use only its own prior baseline")
            th.assert_true(kept in store.load_fim_baseline(rpm_key),
                         "an incomplete fast scan must not alter the independent "
                         "system-Python baseline")
            th.assert_eq(store.stats()["spooled_events"], spooled_before,
                         "no deleted/created events may be derived from a truncated "
                         "fast snapshot")

            previews = runtime.preview_integrity()
            th.assert_eq(previews["rpm"]["complete"], True,
                         "system-Python preview must run independently of fast FIM")
            th.assert_eq(previews["fast"]["complete"], False,
                         "the fast preview row must keep naming the actually faulty tier")
        finally:
            runtime.store.close()
