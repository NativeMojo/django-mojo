"""The one-tier re-baseline ceremony, and what it is not allowed to do.

Re-enrolling a node's content roots produces a content baseline key with
nothing behind it. Seeding it must be silent, must never touch the host tiers,
and must never be able to overwrite a baseline that already exists.
"""

import tempfile

from testit import helpers as th


AGGREGATION = {"window_seconds": 60, "flush_count": 25, "max_aggregates": 100,
               "critical_reserve_aggregates": 10}
DELIVERY = {"max_spool_events": 100, "critical_reserve_events": 10,
            "retry_min_seconds": 1, "retry_max_seconds": 2}
IDENTITY = {"name": "al2023-content-v1", "version": 1, "digest": "e" * 64}


def _scan(tier, key, snapshot=None):
    return {"tier": tier, "baseline_key": key, "complete": True,
            "snapshot": snapshot if snapshot is not None else {f"/{tier}": {"kind": "file"}}}


def _activated(store):
    keys = {tier: f"{IDENTITY['name']}:{IDENTITY['digest']}:{tier}"
            for tier in ("fast", "slow", "rpm")}
    store.activate_fim_profile(
        IDENTITY, {tier: _scan(tier, key) for tier, key in keys.items()})
    return keys


@th.django_unit_test()
def test_new_content_root_seeds_without_alarming_and_never_relaunders(opts):
    from mojo.mojosec.store import Store, StoreError

    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "content-seed", AGGREGATION, DELIVERY)
        host_keys = _activated(store)
        before = store.stats()["spooled_events"]

        first = f"{IDENTITY['name']}:{IDENTITY['digest']}:content:0123456789abcdef"
        result = store.initialize_fim_tier(
            IDENTITY, "content", _scan("content", first, {
                "/opt/content/acme/index.html": {"kind": "file", "sha256": "a" * 64},
                "/opt/content/acme": {"kind": "directory"},
            }))
        th.assert_eq(result["entries"], 2, "the whole declared estate must be seeded")
        th.assert_eq(store.stats()["spooled_events"], before,
                     "seeding a new content baseline must emit no observations at all")
        th.assert_true(store.fim_initialized(first),
                       "the new content tier must be initialized after seeding")
        th.assert_eq(len(store.load_fim_baseline(first)), 2,
                     "the seeded baseline must hold the scanned snapshot")

        with th.assert_raises(StoreError):
            store.initialize_fim_tier(
                IDENTITY, "content", _scan("content", first, {
                    "/opt/content/acme/index.html": {"kind": "file", "sha256": "b" * 64},
                }))
        th.assert_eq(
            store.load_fim_baseline(first)["/opt/content/acme/index.html"]["sha256"],
            "a" * 64,
            "a refused re-seed must not launder a change into the known-good state")

        for tier, key in host_keys.items():
            th.assert_true(store.fim_initialized(key),
                           f"the {tier} tier baseline must survive a content re-seed")
            th.assert_eq(len(store.load_fim_baseline(key)), 1,
                         f"the {tier} tier snapshot must be untouched")

        second = f"{IDENTITY['name']}:{IDENTITY['digest']}:content:fedcba9876543210"
        result = store.initialize_fim_tier(
            IDENTITY, "content", _scan("content", second, {"/srv/x": {"kind": "file"}}))
        th.assert_eq(result["superseded"], [first],
                     "the retired root set's baseline must be pruned by the same transaction")
        th.assert_eq(store.load_fim_baseline(first), {},
                     "a superseded content baseline must not retain the old estate")
        th.assert_true(not store.fim_initialized(first),
                       "a superseded content tier must no longer read as initialized")
        for tier, key in host_keys.items():
            th.assert_true(store.fim_initialized(key),
                           f"pruning content baselines must never reach the {tier} tier")

        drifted = dict(IDENTITY, digest="f" * 64)
        with th.assert_raises(StoreError):
            store.initialize_fim_tier(
                drifted, "content", _scan("content", "other:x:content:1"))
        store.close()


@th.django_unit_test()
def test_poll_integrity_scans_every_tier_including_content(opts):
    """The scan roster must be derived, or a content tier is silently skipped."""
    from mojo.mojosec.collectors.fim import FimCollector
    from mojo.mojosec.profiles import profile_identity, resolve_profile
    from mojo.mojosec.runtime import Runtime

    profile = resolve_profile("al2023-content-v1", ("/opt/content",))
    identity = profile_identity(profile)
    runtime = Runtime.__new__(Runtime)
    runtime.profile = profile
    runtime.profile_identity = identity
    runtime.integrity_collectors = {
        tier: FimCollector(config, None, identity, tier)
        for tier, config in profile["tiers"].items()
    }
    runtime.integrity_collectors["rpm"] = FimCollector(
        {"targets": [], "max_entries": 1, "max_file_bytes": 0, "max_depth": 1},
        None, identity, "rpm")

    roster = runtime.integrity_roster()
    th.assert_eq(set(roster), {"fast", "slow", "content", "rpm"},
                 "every configured tier must be scanned, not a hard-coded triple")
    th.assert_eq(roster[0], "fast",
                 "fast must run first; rpm verifies against its shared traversal")
    th.assert_eq(roster[-1], "rpm",
                 "rpm must run last so the shared fast snapshot already exists")
    host_only = Runtime.__new__(Runtime)
    host_only.integrity_collectors = dict.fromkeys(("slow", "rpm", "fast"), None)
    th.assert_eq(host_only.integrity_roster(), ("fast", "slow", "rpm"),
                 "a host-only node must keep exactly its historical scan order")


@th.django_unit_test()
def test_rollback_validates_every_tier_on_a_content_node(opts):
    from mojo.mojosec.store import Store, StoreError

    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "content-rollback", AGGREGATION, DELIVERY)
        _activated(store)
        content_key = f"{IDENTITY['name']}:{IDENTITY['digest']}:content:0123456789abcdef"
        store.initialize_fim_tier(
            IDENTITY, "content", _scan("content", content_key))

        successor = {"name": "al2023-content-v1", "version": 1, "digest": "9" * 64}
        store.activate_fim_profile(successor, {
            tier: _scan(tier, f"{successor['name']}:{successor['digest']}:{tier}")
            for tier in ("fast", "slow", "rpm")})

        rolled = store.rollback_fim_profile(IDENTITY["digest"])
        th.assert_eq(rolled, IDENTITY,
                     "a fully baselined generation must remain rollback eligible")
        th.assert_eq(store.active_fim_profile(), IDENTITY,
                     "rollback must select the previous generation")
        keys = store.initialized_baseline_keys(
            f"{IDENTITY['name']}:{IDENTITY['digest']}:")
        th.assert_true(content_key in keys,
                       "the content tier must be part of the derived tier set")
        th.assert_eq(len(keys), 4,
                     "a content generation carries four seeded tiers, not three")

        partial = {"name": "al2023-content-v1", "version": 1, "digest": "7" * 64}
        store.set_meta("fim_profile_history", [partial] + store.get_meta(
            "fim_profile_history", []))
        store.set_meta(f"fim_initialized:{partial['name']}:{partial['digest']}:fast", True)
        with th.assert_raises(StoreError):
            store.rollback_fim_profile(partial["digest"])
        th.assert_eq(store.active_fim_profile(), IDENTITY,
                     "a generation missing host tiers must never become active")

        # Every tier the NAMED profile configures must be baselined, not just
        # the host triple: a content profile whose content tier was never
        # checked is exactly the generation the guard exists to refuse.
        hosts_only = {"name": "al2023-content-v1", "version": 1, "digest": "3" * 64}
        store.set_meta("fim_profile_history", [hosts_only] + store.get_meta(
            "fim_profile_history", []))
        for tier in ("fast", "slow", "rpm"):
            store.set_meta(
                f"fim_initialized:{hosts_only['name']}:{hosts_only['digest']}:{tier}",
                True)
        with th.assert_raises(StoreError):
            store.rollback_fim_profile(hosts_only["digest"])
        th.assert_eq(store.active_fim_profile(), IDENTITY,
                     "a content generation with no content baseline must never "
                     "become active")
        store.close()
