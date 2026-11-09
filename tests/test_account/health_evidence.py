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
    standalone = Connection()
    budget = he.CycleBudget(10, clock=lambda: 5)
    with he.primary_cursor(10, connect=lambda deadline: standalone, clock=lambda: 5,
                           max_queries=1, budget=budget, transactional=False) as cursor:
        cursor.execute("SELECT 1")
        assert standalone.operation_deadline == 5.75, "standalone SQL lost its750ms operation bound"
        with th.assert_raises(he.EvidenceError):
            cursor.execute("SELECT 2")
    assert [sql for sql, _ in standalone.raw.calls] == ["SELECT 1"], "standalone SELECT emitted a transaction or exceeded its real query allowance"
    assert budget.counts == {"db_queries": 1} and standalone.closed and standalone.raw.closed, "standalone SQL accounting or private cleanup changed"


@th.django_unit_test()
def test_real_primary_standalone_select_is_autocommit_and_default_is_transactional(opts):
    import time
    from psycopg.pq import TransactionStatus
    from mojo.apps.account.services import health_evidence as he
    for transactional, state, count in ((False, TransactionStatus.IDLE, 1), (True, TransactionStatus.INTRANS, 3)):
        deadline = time.monotonic() + 3
        connection = he._primary_connection(deadline)
        budget = he.CycleBudget(deadline)
        with he.primary_cursor(deadline, connect=lambda end: connection, budget=budget, transactional=transactional) as cursor:
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,), "real primary SELECT did not return its result"
            assert connection.autocommit is True and connection.pgconn.transaction_status == state, "standalone/default primary SQL did not use the intended real libpq transaction state"
        assert connection.closed and budget.counts == {"db_queries": count}, "real primary cursor cleanup or actual SQL accounting changed"


@th.django_unit_test()
def test_failure_windows_keep_unread_rows_and_requery_overlap(opts):
    from mojo.apps.account.services import health_evidence as he
    calls = []
    def rows(phase, start, end, after, limit, deadline):
        calls.append((phase, start, end, after))
        if phase == "fallback":
            return []
        minimum = after[1] if after else 0
        return [{"id": number, "job_id": "c" * 32, "channel": "testit_health", "event": "failed", "at": NOW, "attempt": 1}
                for number in range(minimum + 1, 251)][:limit + 1]
    first = he.failure_page(SOURCE, deadline=10, now=NOW, reader=rows)
    assert len(first["events"]) == 200 and first["has_more"], "first failure page did not retain its unread event continuation"
    second = he.failure_page(SOURCE, first["next_cursor"], deadline=10, now=NOW, reader=rows)
    assert len(second["events"]) == 50 and not second["has_more"], "failure keyset cursor skipped or duplicated same-time event IDs"
    assert second["events"][0]["id"] == 201, "failure cursor used job UUID or max-ID watermark rather than frozen(at,id) position"
    later = NOW + timedelta(seconds=300)
    overlap_start = len(calls)
    overlap = he.failure_page(SOURCE, second["next_cursor"], deadline=10, now=later, reader=rows)
    overlap_calls = calls[overlap_start:]
    assert overlap_calls and overlap_calls[0][:3] == ("events", NOW - timedelta(seconds=600), later), "completed window did not query the new closed window with 600 seconds of overlap"
    assert overlap["events"][0]["id"] == 1 and overlap["has_more"], "overlap query skipped available prior evidence or acknowledged unread rows"
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
    assert result["has_more"] and result["next_cursor"], "failed initial database page lost its frozen window checkpoint"
    assert "durable_read_unavailable" in result["gaps"] and result["total"] is None, "failed read reported reassuring zero actual failures"
    from django.core import signing
    saved = signing.loads(result["next_cursor"], salt=he.FAILURE_CURSOR_SALT)
    retried = he.failure_page(SOURCE, result["next_cursor"], deadline=10, now=NOW + timedelta(seconds=60), reader=broken)
    resumed = signing.loads(retried["next_cursor"], salt=he.FAILURE_CURSOR_SALT)
    assert saved["start"] == resumed["start"] and saved["expires"] == resumed["expires"], "retry slid the unread lower bound or renewed its window expiry"


@th.django_unit_test()
def test_real_durable_rows_are_json_safe_and_keep_original_terminal_fields(opts):
    import uuid
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.account.services import health_evidence as he
    job_id = uuid.uuid4().hex
    Job.objects.filter(pk=job_id).delete()
    try:
        job = Job.objects.create(id=job_id, channel="testit_health_evidence", func="testit.health.evidence",
                                 status="failed", attempt=2, finished_at=he.utcnow())
        event = JobEvent.objects.create(job=job, channel=job.channel, event="failed", attempt=2)
        now = he.utcnow() + timedelta(seconds=1)
        events = he._failure_rows("events", event.at - timedelta(microseconds=1), now, None, 200, __import__("time").monotonic() + 3)
        events = [row for row in events if str(row["job_id"]).replace("-", "") == job_id]
        page = he.failure_page(SOURCE, deadline=10, now=now, reader=lambda phase, *args: events if phase == "events" else [])
        assert page["events"][0]["job_id"] == job_id, "real durable failure UUID was not normalized to the canonical Job identifier"
        assert isinstance(he.canonical(page), str), "real durable rows or their cursor were not JSON safe"
        job.refresh_from_db()
        assert job.status == "failed" and job.attempt == 2 and job.finished_at is not None, "read-only failure evidence mutated the original terminal Job"
    finally:
        Job.objects.filter(pk=job_id).delete()


def _assembly_configuration():
    return {"source_id": SOURCE, "services": [], "services_complete": True, "channels": ["testit_health"], "channels_complete": True}


def _assembly_fleet():
    return {"rows": [], "evidence": {}, "expected": {"complete": True}, "roster_complete": True, "sources": {}}


@th.django_unit_test()
def test_queue_idle_and_drained_recover_and_database_reset_counters_are_unknown(opts):
    from mojo.apps.account.services import health_evidence as he
    config, fleet = _assembly_configuration(), _assembly_fleet()
    def native(at, due, reset, deadlocks):
        return {"observed_at": he.timestamp(at), "latency_seconds": 0, "reachable": True,
            "database": {"incarnation": "one", "stats_reset": reset, "deadlocks": deadlocks},
            "jobs": [{"channel": "testit_health", "due_pending": due, "overruns": 0}]}
    first = he.build_components(config, native(NOW, 2, "reset-one", 2), {}, fleet, {}, now=NOW)
    stalled = he.build_components(config, native(NOW+timedelta(seconds=300), 2, "reset-one", 2), {}, fleet, {}, previous={"components": first}, now=NOW+timedelta(seconds=300))
    queue = next(row for row in stalled if row["kind"] == "jobman")
    assert next(row for row in queue["conditions"] if row.get("rule") == "due_backlog_no_progress_300_seconds")["outcome"] == "fail", "two fresh due-backlog observations failed to prove stalled progress"
    drained = he.build_components(config, native(NOW+timedelta(seconds=600), 0, "reset-two", 3), {}, fleet, {}, previous={"components": stalled}, now=NOW+timedelta(seconds=600))
    queue = next(row for row in drained if row["kind"] == "jobman")
    assert next(row for row in queue["conditions"] if row.get("rule") == "due_backlog_no_progress_300_seconds")["outcome"] == "pass", "fresh drained queue could not establish a recovery pass"
    database = next(row for row in drained if row["kind"] == "database")
    assert next(row for row in database["conditions"] if row.get("rule") == "deadlocks_positive_delta")["outcome"] == "unknown", "reset PostgreSQL counter was compared across different stats epochs"
    idle = he.build_components(config, native(NOW, 0, "reset-one", 0), {}, fleet, {}, now=NOW)
    assert next(row for row in next(item for item in idle if item["kind"] == "jobman")["conditions"] if row.get("rule") == "due_backlog_no_progress_300_seconds")["outcome"] == "pass", "initial fresh idle queue remained an unknown stall predicate"


@th.django_unit_test()
def test_api_tls_context_creation_cannot_start_a_late_handshake(opts):
    from mojo.apps.account.services import health_evidence as he
    from mojo.helpers.bounded_transport import BoundedTransportError
    elapsed, handshakes = [0], []
    class Socket:
        def __init__(self):
            self.timeout, self.closed = None, False
        def settimeout(self, value):
            self.timeout = value
        def gettimeout(self):
            return self.timeout
        def close(self):
            self.closed = True
    raw = Socket()
    class Context:
        def wrap_socket(self, sock, **kwargs):
            handshakes.append(kwargs)
            return sock
    def late_context():
        elapsed[0] = 2
        return Context()
    with th.assert_raises(BoundedTransportError):
        he.probe_service({"origin": "https://example.com", "path": "/api/version"}, 10,
            resolver=lambda *args: ["8.8.8.8"], connector=lambda *args, **kwargs: raw,
            context_factory=late_context, clock=lambda: elapsed[0])
    assert not handshakes and raw.closed, "TLS began with a stale timeout after context setup consumed the HTTP deadline"


@th.django_unit_test()
def test_cluster_binding_uses_current_writer_after_failover(opts):
    import time
    from mojo.apps.account.services import health_evidence as he
    from mojo.deploy.provision.spec import Spec, names
    spec = Spec("testit-health", "production", "us-east-1")
    resolved, calls = names(spec), []
    class RDS:
        def describe_db_instances(self, **params):
            identifier = params["DBInstanceIdentifier"]
            calls.append(identifier)
            return {"DBInstances": [{"DBInstanceIdentifier": identifier, "DBClusterIdentifier": resolved["db_cluster"], "Endpoint": {"Address": identifier + ".example.com"}}]}
        def describe_db_clusters(self, **params):
            return {"DBClusters": [{"DBClusterIdentifier": resolved["db_cluster"], "Endpoint": "cluster.example.com", "DBClusterMembers": [
                {"DBInstanceIdentifier": resolved["db_writer"], "IsClusterWriter": False}, {"DBInstanceIdentifier": "new-writer", "IsClusterWriter": True}]}]}
    class Cache:
        def describe_replication_groups(self, **params):
            return {"ReplicationGroups": []}
    deadline = time.monotonic()+5
    result = he.managed_series({"expected": {"spec": spec}, "bindings": {"database_host": "cluster.example.com"}}, deadline,
        budget=he.CycleBudget(deadline), provider_factory=lambda service: RDS() if service == "rds" else Cache())
    assert result["database"]["resource"] == "new-writer" and calls == [resolved["db_writer"], "new-writer"], "stable cluster endpoint attributed failover pressure to the old configured writer"
    assert all(row["resource"] == "new-writer" for row in result["series"]), "old instance dimensions survived current writer resolution"


@th.django_unit_test()
def test_private_checked_redis_factory_clamps_actual_requested_command_timeout(opts):
    import time
    from mojo.apps.account.services import health_evidence as he
    requested = []
    def connect(**kwargs):
        requested.append(kwargs)
        return object()
    deadline = time.monotonic()+5
    he.bounded_redis_connection(budget=he.CycleBudget(deadline), connect=connect, timeout=5,
        max_connections=2, read_from_replicas=True, deadline=deadline)
    assert requested[0]["timeout"] == 0.5 and requested[0]["read_from_replicas"] is False and requested[0]["deadline"] == deadline, "checked wait budget reached the Redis pool as a longer per-command timeout or replica read"


@th.django_unit_test()
def test_scheduler_lock_presence_remains_noncausal_installation_metadata(opts):
    from mojo.apps.account.services import health_evidence as he
    fleet = _assembly_fleet()
    fleet["sources"] = {"scheduler": {"status": "ok", "scope": "installation", "scheduler_active": True}}
    result = he.build_components(_assembly_configuration(), {}, {}, fleet, {}, now=NOW)
    jobman = next(row for row in result if row["kind"] == "jobman")
    lock = next(row for row in jobman["evidence"] if row["slot_id"] == "scheduler_lock")
    assert lock["provenance"]["installation_lock_present"] is True and lock["provenance"]["scope"] == "installation", "snapshot lost the actually read installation scheduler-lock presence"
    assert lock["observed_at"] is None and lock["fresh_until"] is None and not jobman["coverage"]["complete"], "lock presence fabricated independent scheduler heartbeat freshness"


@th.django_unit_test()
def test_unknown_role_keeps_named_service_obligation_and_unknown_denominator(opts):
    from mojo.apps.account.services import health_evidence as he
    fleet = _assembly_fleet()
    fleet["rows"] = [{"node_id": "unknown-role", "expected": True, "identity_status": "resolved"}]
    result = he.build_components(_assembly_configuration(), {}, {}, fleet, {}, now=NOW)
    component = next(row for row in result if row["kind"] == "fleet")
    service = next(row for row in component["evidence"] if row["source"] == "request_service")
    assert service["source_identity"]["node_id"] == "unknown-role" and service["status"] == "unknown", "unknown role silently removed or invented a current service obligation"
    assert component["coverage"]["expected_total"] is None and not component["coverage"]["complete"], "unknown role requirements established a known complete measurement denominator"


@th.django_unit_test()
def test_project_conditions_use_actual_boolean_observations_and_declared_impact(opts):
    from test_account.health_project_services import _components, _declaration, _project, _result, NOW as observed
    project = _project(_declaration({"id": "mcp", "impact": "availability"}, {"id": "sites-dns", "impact": "operational"}))
    for outcome in ("pass", "fail", "unknown"):
        rows = _components(project, {"mcp": _result(outcome), "sites-dns": _result(outcome)})
        by_id = {row["service_id"]: row for row in rows}
        mcp, dns = by_id["mcp"], by_id["sites-dns"]
        sample, proof = mcp["evidence"][0], mcp["conditions"][0]
        assert sample["slot_id"] == "check" and sample["required_grant"] == "platform" and sample["required"], "project evidence lost its required platform obligation"
        assert proof["rule"] == "service_check" and proof["outcome"] == outcome, "project callback outcome did not produce the framework condition"
        assert mcp["availability"] == {"pass": "up", "fail": "down", "unknown": "unknown"}[outcome], "proved positive/negative availability was lost or invented"
        assert dns["availability"] == "unknown" and dns["status"] == {"pass": "healthy", "fail": "degraded", "unknown": "unknown"}[outcome], "operational-only check fabricated availability or lost degradation"
        if outcome == "unknown":
            assert sample["observed_at"] is None and sample["sample_id"] is None and not proof["basis"], "Unknown callback fabricated causal observation proof"
        else:
            assert sample["value"] is (outcome == "pass") and sample["last_success_at"] == observed.isoformat(), "a successfully observed target failure became a collector failure"
            assert sample["fresh_until"] == (observed + timedelta(seconds=420)).isoformat(), "project direct observation changed the existing 420-second lifetime"
            assert proof["basis"][0]["sample_id"] == sample["sample_id"], "callback supplied evidence outside the framework-owned causal slot"
    missing = _components(project, {"mcp": _result()})
    absent = next(row for row in missing if row["service_id"] == "sites-dns")
    assert absent["status"] == "unknown" and not absent["coverage"]["complete"], "an omitted callback result silently removed or passed its obligation"


@th.django_unit_test()
def test_project_failure_retains_prior_only_for_matching_actual_configuration(opts):
    from test_account.health_project_services import _components, _project, _result
    first = _project(values={"mcp": {"origin": "https://one.example"}})
    previous = {"components": _components(first, {"mcp": _result()})}
    original = previous["components"][0]["evidence"][0]
    same = _components(first, {"mcp": _result("unknown", "provider_unavailable")}, previous=previous)[0]["evidence"][0]
    assert same["observed_at"] is None and same["value"] is None and same["prior"]["sample_id"] == original["sample_id"], "failed project collection renewed or discarded matching prior facts"
    assert same["last_success_at"] == original["last_success_at"] and same["fresh_until"] == original["fresh_until"], "unknown project collection renewed prior success/expiry"
    changed = _project(values={"mcp": {"origin": "https://two.example"}})
    other = _components(changed, previous=previous)[0]["evidence"][0]
    assert "prior" not in other and other["sample_id"] is None and other["last_success_at"] is None, "changed project configuration borrowed prior health from a different route"


def _legacy_managed(*, duplicate=False, denied=False, capped=False, standalone=False, bindings=None, region="us-west-2"):
    import time
    from mojo.apps.account.services import health_evidence as he
    from mojo.helpers.aws.provider_call import ProviderCallError
    calls = []
    class Provider:
        def __getattr__(self, operation):
            def call(**params):
                calls.append((operation, params))
                if denied:
                    raise ProviderCallError(operation, "AccessDenied", denied=True)
                instance = {"DBInstanceIdentifier": "current-writer", "DBClusterIdentifier": "legacy-db", "Engine": "aurora-postgresql",
                    "Endpoint": {"Address": "writer.db.example", "Port": 5432}}
                cluster = {"DBClusterIdentifier": "legacy-db", "Endpoint": "primary.db.example", "Port": 5432,
                    "DBClusterMembers": [{"DBInstanceIdentifier": "current-writer", "IsClusterWriter": True}]}
                group = {"ReplicationGroupId": "legacy-cache", "MemberClusters": ["cache-a", "cache-b"], "NodeGroups": [{
                    "PrimaryEndpoint": {"Address": "primary.cache.example", "Port": 6379}, "NodeGroupMembers": [
                        {"CacheClusterId": "cache-a", "CacheNodeId": "0001", "CurrentRole": "primary"},
                        {"CacheClusterId": "cache-b", "CacheNodeId": "0001", "CurrentRole": "replica"}]}]}
                caches = [{"CacheClusterId": name, "Engine": "redis", "CacheNodes": [{"CacheNodeId": "0001", "Endpoint": {
                    "Address": "primary.cache.example" if standalone else name + ".cache.example", "Port": 6379}}],
                    **({} if standalone else {"ReplicationGroupId": "legacy-cache"})} for name in (["cache-a"] if standalone else ["cache-a", "cache-b"])]
                values = {"describe_db_instances": ("DBInstances", [instance] * (2 if duplicate else 1)),
                    "describe_db_clusters": ("DBClusters", [cluster]), "describe_replication_groups": ("ReplicationGroups", [] if standalone else [group]),
                    "describe_cache_clusters": ("CacheClusters", [row for row in caches if not params.get("CacheClusterId") or row["CacheClusterId"] == params["CacheClusterId"]])}
                key, rows = values[operation]
                return {key: rows, **({"Marker": "unread"} if capped else {})}
            return call
    deadline = time.monotonic() + 5
    configuration = {"expected": {"spec": None}, "region": region, "bindings": {
        "database_host": "PRIMARY.DB.EXAMPLE.", "database_port": 5432,
        "redis_host": "PRIMARY.CACHE.EXAMPLE.", "redis_port": 6379, **(bindings or {})}}
    result = he.managed_series(configuration, deadline, budget=he.CycleBudget(deadline), provider_factory=lambda service: Provider())
    return result, calls


@th.django_unit_test()
def test_legacy_endpoint_binding_uses_current_writer_and_applicable_cache_metrics(opts):
    result, calls = _legacy_managed()
    assert result["database"].get("resource") == "current-writer", "non-provisioned primary endpoint did not resolve the provider's current writer"
    assert result["redis"]["status"] == "ok", "normalized replication-group primary endpoint did not bind legacy cache observations"
    database = [row for row in result["series"] if row["kind"] == "database"]
    assert "FreeLocalStorage" in {row["metric"] for row in database} and "FreeStorageSpace" not in {row["metric"] for row in database}, "Aurora was required to publish the inapplicable RDS free-storage metric"
    lag = [row for row in result["series"] if row["metric"] == "ReplicationLag"]
    assert [row["resource"] for row in lag] == ["cache-b:0001"], "cache primary was required to publish replica-only ReplicationLag"
    standalone, _ = _legacy_managed(standalone=True)
    assert standalone["redis"]["status"] == "ok" and not any(row["metric"] == "ReplicationLag" for row in standalone["series"]), "exact standalone Redis endpoint failed binding or acquired a replica obligation"


@th.django_unit_test()
def test_legacy_binding_ambiguity_denial_and_budget_remain_named_unknown(opts):
    for options, reason in (({"duplicate": True}, "managed_endpoint_ambiguous"), ({"denied": True}, "provider_denied"), ({"capped": True}, "not_collected_budget")):
        result, calls = _legacy_managed(**options)
        assert result["database"]["status"] == "unknown" and result["database"]["reason"] == reason, "unproved legacy binding lost its ambiguity/denial/budget reason"
        assert not any(row["kind"] == "database" for row in result["series"]), "incomplete endpoint discovery selected a candidate anyway"
        assert len(calls) <= 7 and sum("MaxRecords" in params for _, params in calls) <= 4, "legacy endpoint discovery exceeded its shared page/call bounds including exact current-writer revalidation"
    for binding in ({"database_host": "unproved-cname.example"}, {"database_port": 5433}):
        result, _ = _legacy_managed(bindings=binding)
        assert result["database"]["reason"] == "managed_endpoint_unresolved", "a CNAME guess or different port crossed the actual primary endpoint boundary"
    result, calls = _legacy_managed(region=None)
    assert not calls and result["database"]["reason"] == result["redis"]["reason"] == "provider_region_unresolved", "undeclared region guessed us-east-1 or queried provider resources"


def _authoritative_host(cloud=None):
    from mojo.apps.account.services import health_evidence as he
    at = he.timestamp(NOW - timedelta(seconds=2))
    sample = {"status": "ok", "observed_at": at, "percent": 2, "total": 100, "free": 98}
    current = {"cpu": sample, "memory": sample, "filesystems": {"status": "ok", "observed_at": at,
        "mounts": [{**sample, "path": "/", "inodes": {**sample, "total": 100, "free": 98}}]}}
    fleet = {**_assembly_fleet(), "inventory": {"observed_at": at}, "rows": [{"node_id": "host", "expected": True,
        "request_service_required": False, "identity_status": "resolved", "proof_observed_at": at}],
        "evidence": {"host": {"current": current, "trends": cloud or {}, "services": {"request_service": {"status": "not_applicable"}}}}}
    rows = he.build_components(_assembly_configuration(), {}, {}, fleet, {}, now=NOW)
    return next(row for row in rows if row["kind"] == "fleet")


@th.django_unit_test()
def test_host_metrics_complete_without_optional_cloud_and_real_cloud_failure_survives(opts):
    good = _authoritative_host()
    assert good["coverage"]["complete"] and good["status"] == "healthy", "fresh authoritative host metrics still required absent CWAgent or an EC2 mapping"
    presence = next(row for row in good["evidence"] if row["source"] == "ec2")
    assert presence["reason"] == "provider_identity_unresolved" and presence["observed_at"] is None and not presence["required"], "missing EC2 identity became a timestamp error or fabricated presence"
    failed = _authoritative_host({"cpu": {"status": "ok", "observed_at": NOW.isoformat(), "value": 95, "period_seconds": 300}})
    assert failed["status"] == "degraded" and any(row["outcome"] == "fail" for row in failed["conditions"]), "optional cloud policy erased a real fresh observed pressure condition"
    required = _authoritative_host({"cpu": {"status": "unknown", "reason": "no_datapoints", "required": True}})
    assert not required["coverage"]["complete"], "an explicitly required CloudWatch obligation became optional"


@th.django_unit_test()
def test_frozen_source_configuration_includes_actual_primary_binding_region_and_policy(opts):
    from contextlib import contextmanager
    import time
    from mojo.apps.account.services import health_evidence as he, system_settings
    @contextmanager
    def cursor(end):
        class Cursor:
            def execute(self, *args):
                pass
            def fetchone(self):
                return {system_settings.INSTALLATION_UUID: "d496818a-06c7-45f5-b165-8993d93b3a47",
                    system_settings.INSTALLATION_SLUG: "testit-health"}, []
        yield Cursor()
    def configuration(region="us-west-2", host="PRIMARY.CACHE.EXAMPLE.", port=6380):
        values = {"HEALTH_SNAPSHOT_ENVIRONMENT_ID": "testit", "JOBS_CHANNELS": ["testit_health"], "AWS_REGION": region,
            "REDIS_URL": "rediss://private-user:private-password@%s:%s/1" % (host, port), "REDIS_READER_URL": "rediss://reader.example:6379/1"}
        return he.source_configuration(time.monotonic()+5, get_static=lambda key, default=None: values.get(key, default),
            cursor_context=cursor, project_reader=lambda end: None)
    first = configuration()
    assert first["bindings"]["redis_host"] == "primary.cache.example" and first["bindings"]["redis_port"] == 6380, "snapshot fingerprint did not use normalized primary Redis URL parts"
    assert first["region"] == "us-west-2" and first["source_policy"]["cloudwatch_host_metrics"] == "optional", "source region or authoritative host policy was not frozen"
    assert "private-password" not in he.canonical(first) and "reader.example" not in he.canonical(first), "source configuration retained credentials or bound the reader endpoint"
    for changed in (configuration(region="us-east-1"), configuration(host="other.cache.example"), configuration(port=6379)):
        assert changed["config_revision"] != first["config_revision"], "actual region/endpoint/port change reused a publication fingerprint"


@th.django_unit_test()
def test_declared_resource_fences_never_fall_back_to_legacy_account_search(opts):
    import time
    from mojo.apps.account.services import health_evidence as he
    from mojo.deploy.provision.spec import Spec, names
    spec = Spec("testit-fenced", "production", "us-east-1")
    resolved, calls = names(spec), []
    class Provider:
        def describe_db_instances(self, **params):
            calls.append(params)
            return {"DBInstances": [{"DBInstanceIdentifier": "outside-declaration", "Endpoint": {"Address": "actual.db.example", "Port": 5432}}]}
        def describe_replication_groups(self, **params):
            calls.append(params)
            return {"ReplicationGroups": [{"ReplicationGroupId": "outside-declaration", "NodeGroups": [{"PrimaryEndpoint": {"Address": "actual.cache.example", "Port": 6379}}]}]}
    deadline = time.monotonic()+5
    result = he.managed_series({"expected": {"spec": spec}, "bindings": {"database_host": "actual.db.example", "redis_host": "actual.cache.example"}},
        deadline, budget=he.CycleBudget(deadline), provider_factory=lambda service: Provider())
    assert not result["series"] and result["database"]["status"] == result["redis"]["status"] == "unknown", "an exact endpoint outside declared resource identities crossed the provision fence"
    assert calls == [{"DBInstanceIdentifier": resolved["db_writer"]}, {"ReplicationGroupId": resolved["cache_group"]}], "failed declared binding widened into account discovery"
