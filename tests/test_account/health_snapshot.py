"""Version2 identity, durable counter, fencing and strict cache contract."""
import copy
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

from testit import helpers as th

NOW = datetime(2026, 10, 21, 2, 0, tzinfo=timezone.utc)
CONFIG = "b" * 64


def _identity(environment="production"):
    from mojo.apps.account.services import health_snapshot as hs
    return hs.resolve_identity(identity_reader=lambda: {"uuid": "d496818a-06c7-45f5-b165-8993d93b3a47", "slug": "testit"},
                               get_static=lambda key, default=None: environment if key == "MOJO_ENVIRONMENT" else default)


def _seal(value):
    from mojo.apps.account.services import health_evidence as he
    value["publication_digest"] = he.digest({key: item for key, item in value.items() if key != "publication_digest"})
    return value


def _publication(generation=1, with_cloud=False):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    identity = _identity()
    direct = he.slot(identity["source_id"], "redis:default", "ping", source="redis", required_grant="platform",
                     observed_at=NOW, collected_at=NOW, value={"reachable": True}, now=NOW)
    slots = [direct]
    conditions = [he.condition("redis:default", "reachability", "default", "pass", [direct], impact="availability", now=NOW)]
    if with_cloud:
        cloud = he.slot(identity["source_id"], "redis:default", "cache-private-node-cpu", source="cloudwatch", required_grant="aws",
                        source_identity={"node_id": "private-cache-node"}, observed_at=NOW, collected_at=NOW,
                        value=95, period_seconds=60, now=NOW)
        slots.append(cloud)
        conditions.append(he.condition("redis:default", "cpu_percent", "private-cache-node", "fail", [cloud], now=NOW))
    component = he.component(identity["source_id"], "redis", "default", slots, conditions, now=NOW)
    return hs.publication(identity, generation, CONFIG, [component], generated_at=he.timestamp(NOW),
                          published_at=he.timestamp(NOW), roster_revision="c" * 64)


def _worker_publication(heartbeat_at):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    from mojo.apps.aws.services import fleet_health
    from mojo.helpers.settings import settings
    identity = _identity()
    interval = max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5)))
    runners = [] if heartbeat_at is None else [{"hostname": "worker-one", "runner_id": "worker-runner",
        "started": he.timestamp(NOW - timedelta(hours=1)), "last_heartbeat": he.timestamp(heartbeat_at), "alive": True}]
    rows = fleet_health.reconcile([{"node_id": "worker-one", "hostname": "worker-one", "source": "configuration"}],
                                  runners=runners, now=NOW, heartbeat_ttl=interval * 3)
    fleet = {"rows": rows, "runners": runners, "evidence": {}, "sources": {},
             "expected": {"complete": True}, "roster_complete": True}
    configuration = {**identity, "services": [], "channels": [], "channels_complete": True}
    components = he.build_components(configuration, {}, {}, fleet, {}, now=NOW)
    jobman = next(row for row in components if row["kind"] == "jobman")
    value = hs.publication(identity, 1, CONFIG, [jobman], generated_at=he.timestamp(NOW),
                           published_at=he.timestamp(NOW), roster_revision="c" * 64)
    return value, rows


def _service_publication(healthy, required=True):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    identity = _identity()
    observed = NOW - timedelta(seconds=2)
    service = {"status": "ok", "observed_at": he.timestamp(observed), "healthy": healthy,
               "node": "api_private.example", "loaded_revision": "a" * 32, "pid": 123,
               "profile": "opt_api_asgi_v1", "source": "signed_request_proof", "required": required}
    if healthy is None:
        service = {"status": "unknown", "reason": "current_service_proof_unavailable", "required": required}
    if required is False:
        service = {"status": "not_applicable", "reason": "bound_role_excludes_request_service", "required": False}
    fleet = {"rows": [{"node_id": "api-private-example", "expected": True, "request_service_required": required}],
             "evidence": {"api-private-example": {"services": {"request_service": service}}},
             "expected": {"complete": True}, "roster_complete": True}
    configuration = {**identity, "services": [], "channels": [], "channels_complete": True}
    components = he.build_components(configuration, {}, {}, fleet, {}, now=NOW)
    component = next(row for row in components if row["kind"] == "fleet")
    return hs.publication(identity, 1, CONFIG, [component], generated_at=he.timestamp(NOW),
                          published_at=he.timestamp(NOW), roster_revision="c" * 64)


@th.django_unit_test()
def test_signed_service_slot_preserves_failure_expiry_and_revoked_jobs(opts):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    observed = NOW - timedelta(seconds=2)
    for healthy in (True, False):
        value = _service_publication(healthy)
        original = copy.deepcopy(value)
        sample = next(slot for slot in value["components"][0]["evidence"] if slot["source"] == "request_service")
        for at in (NOW, NOW + timedelta(seconds=60), NOW + timedelta(seconds=300), observed + timedelta(seconds=419)):
            result = hs.project_publication(value, {"platform": True, "jobs": True, "aws": False}, now=at)
            component = result["components"][0]
            service = next(slot for slot in component["evidence"] if slot["source"] == "request_service")
            condition = next(row for row in component["conditions"] if row.get("rule") == "request_service_availability")
            assert service["status"] == "ok" and service["value"]["healthy"] is healthy, "Cached polling must preserve a known positive or negative service observation"
            assert service["observed_at"] == he.timestamp(observed) and service["fresh_until"] == he.timestamp(observed + timedelta(seconds=420)), "Collection/publication/read times cannot renew request proof"
            assert service["sample_id"] == sample["sample_id"] and service["collected_at"] == sample["collected_at"], "Polling cannot fabricate another current request sample"
            assert condition["outcome"] == ("pass" if healthy else "fail") and condition["impact"] == "availability", "The existing service slot must cause the correct availability predicate"
            assert component["availability"] == ("up" if healthy else "down"), "A proved service failure must survive independent source gaps"
            assert not component["coverage"]["complete"], "Current service proof cannot erase unrelated missing fleet evidence"
        expired = hs.project_publication(value, {"platform": True, "jobs": True, "aws": False}, now=observed + timedelta(seconds=420))
        service = next(slot for slot in expired["components"][0]["evidence"] if slot["source"] == "request_service")
        condition = next(row for row in expired["components"][0]["conditions"] if row.get("rule") == "request_service_availability")
        assert service["status"] == "stale" and condition["outcome"] == "unknown", "Service availability must expire independently at its actual 420-second deadline"
        revoked = hs.project_publication(value, {"platform": True, "jobs": False, "aws": False}, now=NOW)
        assert "request_service" not in json.dumps(revoked) and "api_private.example" not in json.dumps(revoked), "Revoked jobs authority must remove the service slot and its causal identities"
        assert revoked["coverage"]["expected_total"] is None and not revoked["coverage"]["complete"], "Revoked service authority cannot preserve complete health"
        assert value == original, "Expiry and permission projection must preserve the immutable service publication"


@th.django_unit_test()
def test_excluded_or_unknown_service_cannot_fabricate_availability_pass(opts):
    from mojo.apps.account.services import health_snapshot as hs
    for healthy, required in ((None, True), (None, False)):
        result = hs.project_publication(_service_publication(healthy, required), {"platform": True, "jobs": True, "aws": False}, now=NOW)
        component = result["components"][0]
        conditions = [row for row in component["conditions"] if row.get("rule") == "request_service_availability"]
        assert not conditions or all(row["outcome"] == "unknown" for row in conditions), "An unknown or excluded request service cannot fabricate a causal availability pass"
        assert component["availability"] == "unknown" and not component["coverage"]["complete"], "Unknown/excluded service evidence cannot hide independent coverage gaps"


@th.django_unit_test()
def test_cached_worker_sample_survives_poll_without_renewing_heartbeat(opts):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    heartbeat_at = NOW - timedelta(seconds=2)
    value, rows = _worker_publication(heartbeat_at)
    assert rows[0]["runner_id"] == "worker-runner", "a fresh real heartbeat was not admitted by the fleet reconciler"
    worker = next(slot for slot in value["components"][0]["evidence"] if slot["source"] == "heartbeat")
    original = copy.deepcopy(value)
    deadline = he.timestamp(heartbeat_at + timedelta(seconds=420))
    for at, expected in ((NOW + timedelta(seconds=60), "ok"), (NOW + timedelta(seconds=61), "ok"),
                         (NOW + timedelta(seconds=300), "ok"), (heartbeat_at + timedelta(seconds=419), "ok"),
                         (heartbeat_at + timedelta(seconds=420), "stale")):
        report = hs.project_publication(value, {"platform": True, "jobs": True, "aws": False}, now=at)
        observed = next(slot for slot in report["components"][0]["evidence"] if slot["source"] == "heartbeat")
        assert observed["status"] == expected, "cached worker evidence expired before the supported poll or survived its 420-second deadline"
        assert observed["observed_at"] == he.timestamp(heartbeat_at), "cached projection replaced the actual heartbeat instant"
        assert observed["fresh_until"] == deadline, "cached projection renewed or shortened the original worker sample deadline"
        assert observed["sample_id"] == worker["sample_id"] and observed["collected_at"] == worker["collected_at"], "repeated reads fabricated a new worker sample or collection"
        assert not report["components"][0]["coverage"]["complete"], "worker freshness erased the unrelated missing scheduler detector"
    assert value == original, "repeated worker projections mutated the immutable cached publication"


@th.django_unit_test()
def test_stale_or_missing_worker_is_not_admitted_as_cached_health(opts):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    from mojo.helpers.settings import settings
    live_window = max(1, int(settings.get_static("JOBS_RUNNER_HEARTBEAT_SEC", 5))) * 3
    for heartbeat_at in (NOW - timedelta(seconds=live_window + 1), None):
        value, rows = _worker_publication(heartbeat_at)
        assert rows[0]["last_heartbeat"] is None and rows[0]["runner_id"] is None, "the snapshot fixture admitted an expired or absent live runner"
        report = hs.project_publication(value, {"platform": True, "jobs": True, "aws": False}, now=NOW + timedelta(seconds=60))
        worker = next(slot for slot in report["components"][0]["evidence"] if slot["source"] == "heartbeat")
        assert worker["status"] == "unknown", "a stale or missing heartbeat became a healthy cached observation"
        assert worker["observed_at"] is None and worker["fresh_until"] is None and worker["sample_id"] is None, "collection or read time fabricated evidence for an unadmitted heartbeat"


@th.django_unit_test()
def test_source_identity_is_explicit_stable_and_never_created(opts):
    from mojo.apps.account.services import health_snapshot as hs
    first, second = _identity(), _identity()
    assert first == second and first["source_id"] != _identity("staging")["source_id"], "source identity was not stable and environment-scoped"
    assert set(first["source_identity"]) == {"installation_id", "environment_id"}, "source identity acquired release, host or workspace aliases"
    with th.assert_raises(hs.SnapshotError):
        hs.resolve_identity(identity_reader=lambda: None, get_static=lambda key, default=None: "production")
    with th.assert_raises(hs.SnapshotError):
        hs.resolve_identity(identity_reader=lambda: {"uuid": "d496818a-06c7-45f5-b165-8993d93b3a47"},
                            get_static=lambda key, default=None: {"MOJO_ENVIRONMENT": "production", "HEALTH_SNAPSHOT_ENVIRONMENT_ID": "staging"}.get(key, default))
    with th.assert_raises(hs.SnapshotError):
        hs.resolve_identity(identity_reader=lambda: {"uuid": "d496818a-06c7-45f5-b165-8993d93b3a47"}, get_static=lambda key, default=None: None)


@th.django_unit_test()
def test_schema_rejects_duplicates_wrong_source_and_forged_causal_basis(opts):
    from mojo.apps.account.services import health_snapshot as hs
    good = _publication()
    assert hs.validate_publication(good, _identity()) is good, "valid version2 publication was refused"
    duplicate = copy.deepcopy(good)
    duplicate["components"].append(copy.deepcopy(duplicate["components"][0]))
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(_seal(duplicate), _identity())
    forged = copy.deepcopy(good)
    forged["components"][0]["conditions"][0]["basis"][0]["sample_id"] = "d" * 64
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(_seal(forged), _identity())
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(good, _identity("different"))
    for invalid in (True, 0, -1, "1"):
        bad = copy.deepcopy(good)
        bad["publication_generation"] = invalid
        with th.assert_raises(hs.SnapshotError):
            hs.validate_publication(_seal(bad), _identity())
    unsupported = copy.deepcopy(good)
    unsupported["schema_version"] = 1
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(_seal(unsupported), _identity())
    oversized = copy.deepcopy(good)
    oversized["extra"] = "x" * hs.MAX_BYTES
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(_seal(oversized), _identity())


@th.django_unit_test()
def test_repeated_cached_read_expires_and_revoked_source_is_removed(opts):
    from mojo.apps.account.services import health_snapshot as hs
    value = _publication(with_cloud=True)
    original = copy.deepcopy(value)
    allowed = hs.project_publication(value, {"platform": True, "aws": True, "jobs": False}, now=NOW)
    assert allowed["components"][0]["status"] == "degraded", "real pressure did not survive the permitted cached projection"
    expired = hs.project_publication(value, {"platform": True, "aws": True, "jobs": False}, now=NOW + timedelta(seconds=421))
    assert expired["components"][0]["status"] == "stale" and not expired["coverage"]["complete"], "served_at renewed expired source evidence"
    assert expired["components"][0]["conditions"][0]["outcome"] == "unknown", "an expired pass retained causal recovery proof"
    revoked = hs.project_publication(value, {"platform": True, "aws": False, "jobs": False}, now=NOW)
    text = json.dumps(revoked)
    assert "private-cache-node" not in text and "cache-private-node-cpu" not in text, "revoked cloud authority leaked resource identity through slots or causal proof"
    assert revoked["coverage"]["expected_total"] is None and not revoked["coverage"]["complete"], "denied required slots retained a reassuring denominator or complete result"
    assert revoked["components"][0]["sample_id"] is None, "privileged composite sample identity survived source denial"
    assert value == original, "a response projection mutated the immutable cached publication"


@th.django_unit_test()
def test_denied_component_identity_does_not_survive_projection(opts):
    from mojo.apps.account.services import health_evidence as he, health_snapshot as hs
    identity = _identity()
    private = he.slot(identity["source_id"], "database:secret-rds", "private-slot", source="cloudwatch", required_grant="aws",
                      observed_at=NOW, collected_at=NOW, source_identity={"resource": "secret-rds"}, value=3, now=NOW)
    component = he.component(identity["source_id"], "database", "secret-rds", [private], [],
                             service_id="secret-service", resource_scope="secret-scope", resource_owner_source_id="secret-owner", now=NOW)
    value = hs.publication(identity, 1, CONFIG, [component], generated_at=he.timestamp(NOW), published_at=he.timestamp(NOW), roster_revision="c" * 64)
    result = hs.project_publication(value, {"platform": True, "aws": False, "jobs": False}, now=NOW)
    text = json.dumps(result)
    for secret in ("secret-rds", "secret-service", "secret-scope", "secret-owner", "private-slot"):
        assert secret not in text, f"denied owning grant leaked component identity {secret}"
    assert result["coverage"]["expected_total"] is None, "denied component count leaked through the denominator"


@th.django_unit_test()
def test_durable_generation_uses_one_test_owned_primary_row(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import health_snapshot as hs, system_settings
    Setting.objects.filter(key="TESTIT_HEALTH_PUBLICATION_STATE").delete()
    deadline = time.monotonic() + 8
    try:
        one = hs.reserve_generation(_identity()["source_id"], CONFIG, deadline, key="TESTIT_HEALTH_PUBLICATION_STATE")
        two = hs.reserve_generation(_identity()["source_id"], "d" * 64, deadline, key="TESTIT_HEALTH_PUBLICATION_STATE")
        assert (one, two) == (1, 2), "durable generation failed to advance independently of Redis"
        assert Setting.objects.filter(key="TESTIT_HEALTH_PUBLICATION_STATE", group=None).count() == 1, "reservation created duplicate global counter rows"
        assert system_settings.is_protected_setting(hs.STATE_KEY), "production counter key is not protected from generic writers"
        candidate = Setting(key=hs.STATE_KEY, value="{}")
        with th.assert_raises(me.PermissionDeniedException):
            candidate._reject_protected_write()
    finally:
        Setting.objects.filter(key="TESTIT_HEALTH_PUBLICATION_STATE").delete()


@th.django_unit_test()
def test_atomic_publication_fences_late_token_and_survives_cache_key_loss(opts):
    from contextlib import nullcontext
    from mojo.apps.account.services import health_snapshot as hs
    identity = _identity()
    cache = hs.PublicationCache(identity["source_id"], prefix="TESTIT_HEALTH_" + uuid.uuid4().hex, guard=lambda *args: nullcontext())
    keys, deadline = cache.keys(), time.monotonic() + 10
    generation = iter((1, 2, 3))
    reserve = lambda source, revision, end: next(generation)
    try:
        old = cache.acquire(CONFIG, deadline)
        with th.assert_raises(hs.SnapshotError):
            cache.reserve(old, "e" * 64, deadline, reserve=reserve)
        first, old_reservation = cache.reserve(old, CONFIG, deadline, reserve=reserve)
        with cache.connection(deadline) as client:
            client.delete(keys["lease"])
        new = cache.acquire(CONFIG, deadline)
        second, new_reservation = cache.reserve(new, CONFIG, deadline, reserve=reserve)
        cache.publish(_publication(second), new, new_reservation, deadline, current_identity=identity, current_config_revision=CONFIG)
        with th.assert_raises(hs.SnapshotError):
            cache.publish(_publication(first), old, old_reservation, deadline, current_identity=identity, current_config_revision=CONFIG)
        assert cache.read(identity, deadline)["publication_generation"] == 2, "late generation overwrote the newer publication"
        third_lease = cache.acquire(CONFIG, deadline)
        third, third_reservation = cache.reserve(third_lease, CONFIG, deadline, reserve=reserve)
        cache.publish(_publication(third), third_lease, third_reservation, deadline, current_identity=identity, current_config_revision=CONFIG)
        with cache.connection(deadline) as client:
            previous = json.loads(client.get(keys["previous"]))
            assert previous["publication_generation"] == 2, "atomic replacement did not retain the actual prior publication"
            client.delete(*keys.values())
        with th.assert_raises(hs.SnapshotError):
            cache.publish(_publication(first), old, old_reservation, deadline, current_identity=identity, current_config_revision=CONFIG)
        with th.assert_raises(hs.SnapshotError):
            cache.read(identity, deadline)
    finally:
        with cache.connection(time.monotonic() + 2) as client:
            client.delete(*keys.values())


@th.django_unit_test()
def test_cache_missing_malformed_oversize_and_forged_marker_fail_closed(opts):
    from mojo.apps.account.services import health_snapshot as hs
    cache = hs.PublicationCache(_identity()["source_id"], prefix="TESTIT_HEALTH_" + uuid.uuid4().hex)
    keys, deadline = cache.keys(), time.monotonic() + 8
    try:
        for raw in ("{", "x" * (hs.MAX_BYTES + 1), json.dumps(_publication())):
            with cache.connection(deadline) as client:
                client.set(keys["current"], raw)
                client.set(keys["published"], "999")
            with th.assert_raises(hs.SnapshotError):
                cache.read(_identity(), deadline)
    finally:
        with cache.connection(time.monotonic() + 2) as client:
            client.delete(*keys.values())


@th.django_unit_test()
def test_cached_service_has_no_collection_fallback_and_checks_entry_grant(opts):
    from mojo.apps.account.services import health_snapshot as hs
    class Actor:
        is_active = True
        is_authenticated = True
        is_request_user = True
        def __init__(self, *perms):
            self.perms = set(perms)
        def has_permission(self, perms):
            return bool(self.perms.intersection(perms))
    calls = []
    class Client:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def eval(self, *args):
            calls.append("cache")
            return [json.dumps(_publication()), "ok", "1"]
    def connect(**kwargs):
        assert kwargs["timeout"] == 0.5 and kwargs["read_from_replicas"] is False, "fast cache read inherited an unbounded/replica client"
        return Client()
    report = hs.read_snapshot(Actor("view_platform"), connect=connect, identity_reader=_identity,
                              actor_loader=lambda actor, deadline: actor, now=NOW)
    assert report["components"][0]["status"] == "healthy" and calls == ["cache"], "cached read performed more than its one bounded cache acquisition"
    with th.assert_raises(hs.SnapshotError):
        hs.read_snapshot(Actor("manage_aws"), connect=connect, identity_reader=_identity,
                         actor_loader=lambda actor, deadline: actor, now=NOW)
    assert calls == ["cache"], "entry denial touched cached protected source data"
    def unavailable(**kwargs):
        raise RuntimeError("bounded_cluster_startup_unsupported")
    failed = hs.read_snapshot(Actor("view_platform"), connect=unavailable, identity_reader=_identity,
                              actor_loader=lambda actor, deadline: actor, now=NOW)
    assert failed["collection_status"] == "unknown" and not failed["components"], "Redis failure reused an in-process green snapshot"
    assert failed["source_id"] == _identity()["source_id"], "cache failure discarded independently validated configured identity"


@th.django_unit_test()
def test_mcp_compaction_preserves_unread_failure_cursor(opts):
    from mojo.apps.account.services import health_snapshot as hs
    value = _publication()
    value["failure_evidence"] = {"events": [{"channel": "x" * 250, "id": number} for number in range(200)],
                                 "terminal_fallback": [], "next_cursor": "advanced", "checkpoint_cursor": "page-start", "gaps": []}
    result = hs.mcp_projection(value)
    assert len(json.dumps(result).encode()) < 24 * 1024, "MCP compaction exceeded its projection byte boundary"
    assert result["failure_evidence"]["next_cursor"] == "page-start", "MCP dropped initial failure rows but lost the frozen page-start checkpoint"
    assert not result["coverage"]["complete"] and result["rest_reference"], "MCP omission was reported as a complete report"
    extra = _publication()
    extra["extra"] = "x" * 300000
    hs.validate_publication(_seal(extra), _identity())
    result = hs.mcp_projection(extra)
    assert len(json.dumps(result).encode()) < 24 * 1024 and "extra" not in result, "unbounded extension metadata escaped MCP output allowlist"
    extra["coverage"]["reasons"] = ["x" * 300000]
    result = hs.mcp_projection(extra)
    assert len(json.dumps(result).encode()) < 24 * 1024 and result["collection_status"] == "unknown", "MCP final fallback exceeded the hard byte ceiling"


@th.django_unit_test()
def test_well_shaped_cache_cannot_extend_old_source_deadline(opts):
    from mojo.apps.account.services import health_snapshot as hs
    value = _publication()
    slot = value["components"][0]["evidence"][0]
    slot["fresh_until"] = (NOW + timedelta(days=1)).isoformat()
    value["components"][0]["conditions"][0]["basis"][0]["fresh_until"] = slot["fresh_until"]
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(_seal(value), _identity())
    forged_sample = _publication()
    forged_sample["components"][0]["evidence"][0]["sample_id"] = "e" * 64
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(_seal(forged_sample), _identity())
    for field in ("omitted_total", "truncated", "omitted_conditions"):
        omitted = _publication()
        omitted["components"][0]["coverage"][field] = True if field == "truncated" else 1
        with th.assert_raises(hs.SnapshotError):
            hs.validate_publication(_seal(omitted), _identity())
        projected = hs.project_publication(omitted, {"platform": True}, now=NOW)
        assert not projected["coverage"]["complete"], f"required {field} omission survived as complete coverage"


@th.django_unit_test()
def test_restored_old_redis_token_cannot_cross_durable_generation_head(opts):
    from functools import partial
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import health_snapshot as hs
    Setting.objects.filter(key="TESTIT_HEALTH_RESTORE_STATE").delete()
    identity, deadline = _identity(), time.monotonic() + 12
    cache = hs.PublicationCache(identity["source_id"], prefix="TESTIT_HEALTH_" + uuid.uuid4().hex,
                                guard=partial(hs.publication_guard, key="TESTIT_HEALTH_RESTORE_STATE"))
    keys = cache.keys()
    reserve = partial(hs.reserve_generation, key="TESTIT_HEALTH_RESTORE_STATE")
    try:
        old = cache.acquire(CONFIG, deadline)
        one, old_reservation = cache.reserve(old, CONFIG, deadline, reserve=reserve)
        with cache.connection(deadline) as client:
            backup = {name: client.get(key) for name, key in keys.items()}
            client.delete(*keys.values())
        new = cache.acquire(CONFIG, deadline)
        two, reservation = cache.reserve(new, CONFIG, deadline, reserve=reserve)
        cache.publish(_publication(two), new, reservation, deadline, current_identity=identity, current_config_revision=CONFIG)
        with cache.connection(deadline) as client:
            client.delete(*keys.values())
            for name, raw in backup.items():
                if raw is not None:
                    client.set(keys[name], raw, ex=75)
        with th.assert_raises(hs.SnapshotError):
            cache.publish(_publication(one), old, old_reservation, deadline, current_identity=identity, current_config_revision=CONFIG)
        with cache.connection(deadline) as client:
            assert client.get(keys["current"]) is None, "restored old token published an old generation over durable newer state"
    finally:
        with cache.connection(time.monotonic() + 2) as client:
            client.delete(*keys.values())
        Setting.objects.filter(key="TESTIT_HEALTH_RESTORE_STATE").delete()


@th.django_unit_test()
def test_real_user_revocation_rereads_primary_before_cache_or_history(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import health_snapshot as hs
    name = "testit-health-" + uuid.uuid4().hex
    User.objects.filter(username=name).delete()
    user = User.objects.create_user(username=name, email=name + "@example.test", password=None)
    user.is_active = True
    user.permissions = {"view_platform": True, "manage_aws": True, "view_jobs": True}
    user.save()
    calls = []
    def forbidden(*args, **kwargs):
        calls.append("forbidden")
        raise AssertionError("revoked entry touched a protected source")
    try:
        User.objects.filter(pk=user.pk).update(permissions={})
        with th.assert_raises(hs.SnapshotError):
            hs.read_snapshot(user, connect=forbidden, identity_reader=forbidden, failure_reader=forbidden)
        assert not calls, "stale User grants opened cache/history after primary revocation"
        assert user.has_permission("manage_aws"), "fixture did not retain a stale privileged User copy"
    finally:
        User.objects.filter(pk=user.pk).delete()
