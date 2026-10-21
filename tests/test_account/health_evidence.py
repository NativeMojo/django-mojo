"""Snapshot source clocks and causal proof; no shared settings or live collectors."""
from datetime import datetime, timedelta, timezone

from testit import helpers as th

NOW = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
SOURCE = "a" * 64


def _slot(slot_id="cpu", at=NOW, collected=None, **kwargs):
    from mojo.apps.account.services import health_evidence as he
    return he.slot(SOURCE, "fleet:installation", slot_id, source="cloudwatch",
                   required_grant="aws", observed_at=at, collected_at=collected or NOW,
                   value=kwargs.pop("value", 95), now=NOW, **kwargs)


@th.django_unit_test()
def test_provider_recollection_keeps_causal_identity(opts):
    from mojo.apps.account.services import health_evidence as he
    old = _slot(at=NOW - timedelta(seconds=60), period_seconds=60)
    recollected = _slot(at=NOW - timedelta(seconds=60), collected=NOW + timedelta(seconds=20), period_seconds=60)
    assert old["sample_id"] == recollected["sample_id"], "recollecting one provider sample created an independent failure observation"
    assert old["fresh_until"] == recollected["fresh_until"], "recollecting one provider sample renewed its provider expiry"
    condition = he.condition("fleet:installation", "cpu_percent", "node-a", "fail", [old], now=NOW)
    auxiliary = _slot("memory", at=NOW - timedelta(seconds=120))
    changed_auxiliary = _slot("memory", at=NOW)
    first = he.component(SOURCE, "fleet", "installation", [old, auxiliary], [condition], now=NOW)
    second = he.component(SOURCE, "fleet", "installation", [recollected, changed_auxiliary], [condition], now=NOW)
    assert first["sample_id"] != second["sample_id"], "the composite fixture did not exercise unrelated sample advancement"
    assert first["conditions"] == second["conditions"], "an unrelated memory observation changed CPU confirmation proof"
    assert first["conditions"][0]["basis"][0]["sample_id"] == old["sample_id"], "causal proof did not name the authoritative CPU slot"


@th.django_unit_test()
def test_zero_missing_stale_and_future_times(opts):
    from mojo.apps.account.services import health_evidence as he
    zero = _slot(value=0)
    assert zero["status"] == "ok" and zero["value"] == 0, "a measured zero became missing evidence"
    absent = _slot(at=None)
    assert absent["status"] == "unknown" and absent["sample_id"] is None, "an absent source time became a new observation"
    future = _slot(at=NOW + timedelta(seconds=31))
    assert future["status"] == "unknown" and future["fresh_until"] is None, "a materially future sample gained a fresh deadline"
    tolerated = _slot(at=NOW + timedelta(seconds=20))
    assert he.parse_time(tolerated["fresh_until"]) == NOW + timedelta(seconds=420), "clock tolerance granted extra health lifetime"
    stale = _slot(at=NOW - timedelta(seconds=181), period_seconds=60)
    assert stale["status"] == "stale" and stale["value"] == 95, "old real provider facts were lost or marked fresh"
    malformed = _slot(at="not a timestamp")
    assert malformed["observed_at"] is None and malformed["sample_id"] is None, "malformed source time became an observation"


@th.django_unit_test()
def test_collection_failure_retains_old_provenance_without_refresh(opts):
    from mojo.apps.account.services import health_evidence as he
    prior = _slot()
    failed = _slot(status="unavailable", reason="provider_unavailable", previous=prior)
    assert failed["observed_at"] is None and failed["value"] is None, "failed collection fabricated new source facts"
    for key in ("last_success_at", "fresh_until", "sample_id"):
        assert failed[key] == prior[key], f"failed collection advanced {key}"
    assert failed["prior"]["value"] == prior["value"], "failed collection discarded prior valid evidence"
    condition = he.condition("fleet:installation", "cpu_percent", "node-a", "fail", [failed], now=NOW)
    assert condition["outcome"] == "unknown", "carried prior facts confirmed a new failure"


@th.django_unit_test()
def test_direct_target_failure_survives_unrelated_collection_gap(opts):
    from mojo.apps.account.services import health_evidence as he
    direct = he.slot(SOURCE, "database:default", "select1", source="database", required_grant="platform",
                     observed_at=NOW, collected_at=NOW, value={"reachable": False}, now=NOW)
    cloud = _slot(status="unavailable", reason="provider_denied")
    failure = he.condition("database:default", "reachability", "default", "fail", [direct], impact="availability", now=NOW)
    result = he.component(SOURCE, "database", "default", [direct, cloud], [failure], now=NOW)
    assert result["availability"] == "down" and result["status"] == "unhealthy", "an unrelated provider gap erased a proved database failure"
    assert not result["coverage"]["complete"], "proved outage hid the unrelated required source gap"
    assert result["observed_at"] is None, "partial component fabricated an all-source observation time"
    with th.assert_raises(he.EvidenceError):
        he.condition("database:default", "reachability", "default", "fail", [direct] * 9, now=NOW)


@th.django_unit_test()
def test_sql_deadline_and_cleanup_are_per_operation(opts):
    from mojo.apps.account.services import health_evidence as he
    class RawCursor:
        def __init__(self):
            self.calls, self.closed = [], False
        def execute(self, sql, params):
            self.calls.append((sql, params))
        def close(self):
            self.closed = True
    class Connection:
        def __init__(self):
            self.raw, self.closed = RawCursor(), False
        def cursor(self):
            return self.raw
        def close(self):
            self.closed = True
    connection = Connection()
    with he.primary_cursor(10, connect=lambda deadline: connection, clock=lambda: 5, max_queries=3) as cursor:
        cursor.execute("SELECT 1")
        assert connection.operation_deadline == 5.75, "SQL operation did not receive the750ms wall-clock bound"
    assert connection.closed and connection.raw.closed, "private DB resources were retained after the transaction"
    assert [sql for sql, _ in connection.raw.calls] == ["BEGIN", "SELECT 1", "COMMIT"], "bounded transaction omitted its commit boundary"
    limited = Connection()
    with th.assert_raises(he.EvidenceError):
        with he.primary_cursor(10, connect=lambda deadline: limited, clock=lambda: 5, max_queries=2) as cursor:
            cursor.execute("SELECT 1")
    assert limited.closed, "query-budget refusal leaked its private connection"


@th.django_unit_test()
def test_failure_windows_keep_unread_rows_and_requery_overlap(opts):
    from mojo.apps.account.services import health_evidence as he
    calls = []
    def rows(phase, start, end, after, limit, deadline):
        calls.append((phase, start, end, after))
        if phase == "fallback":
            return []
        minimum = after[1] if after else 0
        return [{"id": number, "job_id": "job", "channel": "testit_health", "event": "failed", "at": NOW, "attempt": 1}
                for number in range(minimum + 1, 251)][:limit + 1]
    first = he.failure_page(SOURCE, deadline=10, now=NOW, reader=rows)
    assert len(first["events"]) == 200 and first["has_more"], "first failure page did not retain its unread event continuation"
    second = he.failure_page(SOURCE, first["next_cursor"], deadline=10, now=NOW, reader=rows)
    assert len(second["events"]) == 50 and not second["has_more"], "failure keyset cursor skipped or duplicated same-time event IDs"
    assert second["events"][0]["id"] == 201, "failure cursor used job UUID or max-ID watermark rather than frozen(at,id) position"
    later = NOW + timedelta(seconds=300)
    he.failure_page(SOURCE, second["next_cursor"], deadline=10, now=later, reader=rows)
    assert calls[-2][1] == NOW - timedelta(seconds=600), "completed window did not reread600seconds of overlap"
    assert first["capture_status"] == "best_effort" and "retention_unproven" in first["limitations"], "available failure rows overstated capture or retention completeness"
    assert not second["gaps"], "standing best-effort limitations became permanent observed gaps"
    with th.assert_raises(he.EvidenceError):
        he.failure_page("b" * 64, first["next_cursor"], deadline=10, now=NOW, reader=rows)


@th.django_unit_test()
def test_failure_database_error_never_advances_cursor_or_claims_zero(opts):
    from mojo.apps.account.services import health_evidence as he
    def broken(*args):
        raise RuntimeError("database unavailable")
    result = he.failure_page(SOURCE, deadline=10, now=NOW, reader=broken)
    assert result["has_more"] and result["next_cursor"] is None, "failed database page advanced its window"
    assert "durable_read_unavailable" in result["gaps"] and result["total"] is None, "failed read reported reassuring zero actual failures"
