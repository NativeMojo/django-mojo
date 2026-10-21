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
        hs.validate_publication(duplicate, _identity())
    forged = copy.deepcopy(good)
    forged["components"][0]["conditions"][0]["basis"][0]["sample_id"] = "d" * 64
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(forged, _identity())
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(good, _identity("different"))
    for invalid in (True, 0, -1, "1"):
        bad = copy.deepcopy(good)
        bad["publication_generation"] = invalid
        with th.assert_raises(hs.SnapshotError):
            hs.validate_publication(bad, _identity())
    unsupported = copy.deepcopy(good)
    unsupported["schema_version"] = 1
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(unsupported, _identity())
    oversized = copy.deepcopy(good)
    oversized["extra"] = "x" * hs.MAX_BYTES
    with th.assert_raises(hs.SnapshotError):
        hs.validate_publication(oversized, _identity())


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
    from mojo.apps.account.services import health_snapshot as hs
    identity = _identity()
    cache = hs.PublicationCache(identity["source_id"], prefix="TESTIT_HEALTH_" + uuid.uuid4().hex)
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
