"""Contracts for the packaged content profile and its per-tier windows.

The content tier watches enrollment-declared tenant trees. Two properties hold
this together and are pinned here: WHICH trees a node serves never moves the
profile digest, and widening the content tier's correlation window never widens
any host tier's excuse window.
"""

import json
import os
import tempfile

from testit import helpers as th


AGGREGATION = {"window_seconds": 60, "flush_count": 25, "max_aggregates": 100,
               "critical_reserve_aggregates": 10}
DELIVERY = {"max_spool_events": 100, "critical_reserve_events": 10,
            "retry_min_seconds": 1, "retry_max_seconds": 2}
ENDPOINT = "https://incident.example/api/incident/mojosec/batch"


def _collectors(profile):
    from mojo.mojosec.collectors.fim import FimCollector
    from mojo.mojosec.profiles import profile_identity

    identity = profile_identity(profile)
    return {tier: FimCollector(config, None, identity, tier)
            for tier, config in profile["tiers"].items()}


@th.django_unit_test()
def test_content_profile_resolves_enrolled_roots_without_moving_the_digest(opts):
    from mojo.mojosec.profiles import resolve_profile

    first = resolve_profile("al2023-content-v1", ("/opt/content",))
    second = resolve_profile("al2023-content-v1", ("/srv/tenants", "/opt/content"))
    th.assert_eq(
        first["digest"], second["digest"],
        "two content nodes with different tenant roots must share one profile "
        "digest, or every node becomes its own fleet-wide policy")
    th.assert_eq(
        [target["path"] for target in first["tiers"]["content"]["targets"]],
        ["/opt/content"],
        "the content tier must watch exactly the enrolled roots")
    th.assert_true(
        all(target.get("tenant_scoped") is True
            for target in second["tiers"]["content"]["targets"]),
        "every substituted content target must be marked tenant scoped")

    first_keys = {tier: collector.baseline_key
                  for tier, collector in _collectors(first).items()}
    second_keys = {tier: collector.baseline_key
                   for tier, collector in _collectors(second).items()}
    th.assert_true(
        first_keys["content"] != second_keys["content"],
        "changing a node's roots must retire that tier's baseline, not silently "
        "diff new trees against an old one")
    for tier in ("fast", "slow"):
        th.assert_eq(first_keys[tier], second_keys[tier],
                     f"the {tier} tier baseline must not move with content roots")
        th.assert_eq(len(first_keys[tier].split(":")), 3,
                     f"the {tier} tier must keep its exact three-component key")
    th.assert_eq(len(first_keys["content"].split(":")), 4,
                 "only an enrollment-substituted tier gains the graph component")

    v1 = resolve_profile("al2023-web-v1")
    v2 = resolve_profile("al2023-web-v2")
    th.assert_eq(v1["digest"],
                 "6a45b233ae8dd87e77456d88a037ea435e6e9240d07884814fae4a23d30f3e0e",
                 "adding a content profile must not move the published v1 identity")
    th.assert_eq(v2["digest"],
                 "1cb26a911e1a2c2418d5a6428c873bc4118a39c8731c4d8442d7c4f7f3cbd14f",
                 "adding a content profile must not move the published v2 identity")


@th.django_unit_test()
def test_content_profile_refuses_blind_or_overlapping_roots(opts):
    from mojo.mojosec.profiles import ProfileError, resolve_profile

    with th.assert_raises(ProfileError):
        resolve_profile("al2023-content-v1", ())
    with th.assert_raises(ProfileError):
        resolve_profile("al2023-content-v1", tuple(f"/srv/t{n}" for n in range(9)))
    for root in ("/opt/api", "/opt/www/site", "/etc/mojosec", "/etc",
                 "/var/lib/mojosec", "/usr/local/bin", "/boot/content"):
        with th.assert_raises(ProfileError):
            resolve_profile("al2023-content-v1", (root,))
    with th.assert_raises(ProfileError):
        resolve_profile("al2023-web-v2", ("/opt/content",))


@th.django_unit_test()
def test_effective_config_round_trips_a_content_node(opts):
    from mojo.mojosec.config import (
        ConfigError, build_config, validate_effective_config,
    )

    base = {"sensor_id": "i-content", "endpoint": ENDPOINT}
    config = build_config(dict(
        base, profile="al2023-content-v1", content_roots=["/opt/content"]))
    th.assert_eq(sorted(config["collectors"]["fim"]["tiers"]),
                 ["content", "fast", "slow"],
                 "a content node must resolve the content tier alongside the host tiers")
    validated = validate_effective_config(json.loads(json.dumps(config)))
    th.assert_eq(validated["content_roots"], ["/opt/content"],
                 "the effective artifact must round trip its enrolled roots")

    with th.assert_raises(ConfigError):
        build_config(dict(base, profile="al2023-content-v1"))
    with th.assert_raises(ConfigError):
        build_config(dict(base, content_roots=["/opt/content"]))

    host = build_config(dict(base, profile="al2023-web-v2"))
    previous_generation = json.loads(json.dumps(host))
    previous_generation.pop("content_roots")
    loaded = validate_effective_config(previous_generation)
    th.assert_eq(
        loaded["content_roots"], [],
        "an artifact written by the previous package generation must still start "
        "the sensor; refusing it would fail the whole fleet deploy")


@th.django_unit_test()
def test_correlation_is_per_tier_and_leaves_deploy_tiers_untouched(opts):
    from mojo.mojosec.config import ConfigError, build_config
    from mojo.mojosec.expected_changes import MAX_OPERATION_CORRELATION_SECONDS
    from mojo.mojosec.profiles import resolve_profile

    th.assert_eq(MAX_OPERATION_CORRELATION_SECONDS, 300,
                 "the shared deploy correlation window must stay exactly 300s")
    content = _collectors(resolve_profile("al2023-content-v1", ("/opt/content",)))
    for tier in ("fast", "slow"):
        th.assert_eq(content[tier].correlation_seconds, 300,
                     f"the {tier} tier must keep the shared 300s deploy window")
    th.assert_eq(content["content"].correlation_seconds, 900,
                 "only the content tier may carry the wider publish window")
    for tier, collector in _collectors(resolve_profile("al2023-web-v2")).items():
        th.assert_eq(collector.correlation_seconds, 300,
                     f"a host-only profile's {tier} tier must not gain a window")

    base = {"sensor_id": "i-1", "endpoint": ENDPOINT}
    for bad in (299, 1801, "900", True):
        poisoned = build_config(dict(base, profile="al2023-web-v2"))
        poisoned["collectors"]["fim"]["tiers"]["fast"]["correlation_seconds"] = bad
        with th.assert_raises(ConfigError):
            from mojo.mojosec.config import validate_config
            validate_config(poisoned)


@th.django_unit_test()
def test_rpm_diff_helper_survives_per_tier_correlation(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    identity = {"name": "al2023-content-v1", "version": 1, "digest": "d" * 64}
    # Exactly how rpm.py builds its diff helper and its system-site walker:
    # neither config carries interval_seconds or correlation_seconds.
    helper = FimCollector(
        {"targets": [], "max_entries": 1, "max_file_bytes": 0, "max_depth": 1},
        None, identity, "rpm")
    walker = FimCollector(
        {"targets": [{"path": "/usr/lib/python3", "recursive": True}],
         "max_entries": 10, "max_file_bytes": 0, "max_depth": 2},
        None, identity, "rpm")
    for collector in (helper, walker):
        th.assert_eq(collector.correlation_seconds, 300,
                     "an rpm-built collector must resolve the shared window, not crash")
        th.assert_eq(collector.content_roots, (),
                     "an rpm-built collector must declare no tenant roots")
        th.assert_eq(len(collector.baseline_key.split(":")), 3,
                     "the rpm tier must keep its exact three-component key")
    th.assert_eq(helper.diff({}, {"snapshot": {}, "complete": True}), [],
                 "the rpm diff helper must still difference an empty scan")


@th.django_unit_test()
def test_mixed_tier_pending_events_each_use_their_own_window(opts):
    from mojo.deploy.mojosec_changes import MAX_TTL_SECONDS
    from mojo.mojosec.events import observation
    from mojo.mojosec.store import (
        ANNOTATION_MAX_GRACE_SECONDS, ANNOTATION_MAX_OPERATION_SECONDS, Store,
    )

    th.assert_eq(ANNOTATION_MAX_OPERATION_SECONDS, MAX_TTL_SECONDS,
                 "the store's operation ceiling must track the producer's TTL cap")
    with tempfile.TemporaryDirectory() as root:
        store = Store(root, "mixed-window", AGGREGATION, DELIVERY)
        events = []
        for name, attributes in (
                ("/etc/host.conf", {}),
                ("/opt/content/acme/index.html", {"correlation_seconds": 900})):
            events.append(observation(
                "fim.change", "high", "integrity change",
                attributes={"path": name, "change": "modified", "kind": "file",
                            "mode": 0o644, "uid": 0, "gid": 0, "size": 4,
                            "sha256": "c" * 64, **attributes},
                fingerprint_values=(name, "modified"), aggregate=False,
                recommendation="review"))
        store.ingest(events)
        rows = {json.loads(row["payload"])["attributes"]["path"]: row
                for row in store.db.execute(
                    "SELECT id, payload, created FROM events").fetchall()}
        host = rows["/etc/host.conf"]
        content = rows["/opt/content/acme/index.html"]
        th.assert_eq(
            store._event_grace(json.loads(host["payload"])["attributes"]),
            ANNOTATION_MAX_GRACE_SECONDS,
            "a deploy-tier change must hold for exactly the historical grace")
        th.assert_eq(
            store._event_grace(json.loads(content["payload"])["attributes"]),
            MAX_TTL_SECONDS + 900,
            "a content change must hold for the producer TTL plus its own window")
        th.assert_eq(
            store._event_grace({"correlation_seconds": 99999}),
            ANNOTATION_MAX_GRACE_SECONDS,
            "an out-of-range window must fall back, never extend the hold")

        # Both are held for their annotation grace on ingest; deliver them as
        # the sender would once that grace has elapsed.
        store.db.execute("UPDATE events SET next_attempt=0, annotation_deadline=0")
        delivered = store.pending_batch(10, 1024 * 1024)
        th.assert_eq(len(delivered), 2, "both changes must be deliverable")
        th.assert_true(
            all("correlation_seconds" not in event["attributes"]
                for event in delivered),
            "the private tier window must never reach the wire")
        th.assert_true(
            all("sha256" not in event["attributes"] for event in delivered),
            "the existing private FIM fields must still be stripped")
        store.close()


@th.django_unit_test()
def test_unannotated_content_change_alarms(opts):
    """The acceptance criterion: only a declared publish is ever explained."""
    from mojo.deploy.mojosec_changes import ChangeJournal
    from mojo.deploy.publish_broker import execute
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        content = os.path.join(base, "content")
        tenant = os.path.join(content, "acme")
        current = os.path.join(tenant, "current")
        os.makedirs(current)
        page = os.path.join(current, "index.html")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write("published\n")

        manifest = os.path.join(base, "expected.json")
        journal = ChangeJournal(
            journal_path=os.path.join(base, "journal.json"),
            lock_path=os.path.join(base, "journal.lock"),
            manifest_path=manifest, allowed_roots=(content,))
        config = {"targets": [{"path": content, "recursive": True,
                               "tenant_scoped": True}],
                  "max_entries": 1000, "max_file_bytes": 1 << 20, "max_depth": 16,
                  "correlation_seconds": 900}
        collector = FimCollector(config, manifest, None, "content")
        baseline = collector.scan()["snapshot"]

        with open(page, "w", encoding="utf-8") as handle:
            handle.write("defaced by an intruder\n")
        found = [item for item in collector.diff(baseline, collector.scan())
                 if item["attributes"].get("path") == page]
        th.assert_eq(len(found), 1, "an undeclared content write must be reported once")
        th.assert_eq(found[0]["severity"], "high",
                     "an unexplained content change must stay a high-severity alarm")
        th.assert_true("expected_change" not in found[0]["attributes"],
                       "nothing may explain a write nobody declared")

        baseline = collector.scan()["snapshot"]
        begun = execute(
            {"operation": "begin", "root": tenant, "subtrees": ["current"]},
            journal=journal, content_roots=(content,))
        with open(page, "w", encoding="utf-8") as handle:
            handle.write("the next legitimate release\n")
        execute({"operation": "end", "operation_id": begun["operation_id"]},
                journal=journal, content_roots=(content,))
        explained = [item for item in collector.diff(baseline, collector.scan())
                     if item["attributes"].get("path") == page]
        th.assert_eq(len(explained), 1,
                     "an explained change is still reported, never suppressed")
        annotation = explained[0]["attributes"].get("expected_change")
        th.assert_true(annotation is not None,
                       "a declared publish must carry its expected-change annotation")
        th.assert_eq(annotation["operation_kind"], "content-publish",
                     "the receiver must see which producer explained the change")
        th.assert_eq(annotation["deployment_id"], "content-publish:acme",
                     "the annotation must name the publishing tenant")


@th.django_unit_test()
def test_directory_metadata_relaxation_is_scoped_to_content_roots(opts):
    from mojo.deploy.mojosec_changes import _snapshot
    from mojo.mojosec.expected_changes import annotation, relaxed_directory_digest

    now = "2026-08-23T12:00:00Z"
    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        inside = os.path.join(base, "content", "acme", "current")
        outside = os.path.join(base, "etc")
        os.makedirs(inside)
        os.makedirs(outside)
        roots = (os.path.join(base, "content"),)

        def entry(path):
            kind, digest = relaxed_directory_digest(_snapshot(path))
            return {
                "path": path, "change": "modified", "sha256": digest,
                "evidence_kind": kind, "expires_at": "2030-01-01T00:00:00Z",
                "deployment_id": "content-publish:acme",
                "operation_id": "content-publish-" + "a" * 32,
                "operation_kind": "content-publish", "started_at": now,
                "completed_at": now,
            }

        import datetime
        started = datetime.datetime(2026, 8, 23, 12, tzinfo=datetime.timezone.utc)
        entries = []
        for path in (inside, outside):
            item = entry(path)
            item.update({"_expiry": datetime.datetime(
                2030, 1, 1, tzinfo=datetime.timezone.utc),
                "_started": started, "_completed": started})
            entries.append(item)

        os.utime(inside, (1600000000, 1600000000))
        os.utime(outside, (1600000000, 1600000000))
        moved = _snapshot(inside)
        th.assert_true(
            annotation(entries, inside, "modified", None, moved, now=started,
                       observed_at=started, directory_metadata_roots=roots)
            is not None,
            "a publish moves its directory's mtime; that must stay explainable")
        th.assert_true(
            annotation(entries, outside, "modified", None, _snapshot(outside),
                       now=started, observed_at=started,
                       directory_metadata_roots=roots) is None,
            "a directory outside every content root must never be relaxed")

        os.chmod(inside, 0o777)
        th.assert_true(
            annotation(entries, inside, "modified", None, _snapshot(inside),
                       now=started, observed_at=started,
                       directory_metadata_roots=roots) is None,
            "ownership and mode are exactly what a publish may not change")


@th.django_unit_test()
def test_content_tier_symlink_escape_is_tenant_scoped(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        content = os.path.join(base, "content")
        acme = os.path.join(content, "acme")
        other = os.path.join(content, "other")
        os.makedirs(os.path.join(acme, "rev-7"))
        os.makedirs(os.path.join(other, "rev-1"))
        os.symlink(os.path.join(acme, "rev-7"), os.path.join(acme, "current"))
        os.symlink(os.path.join(other, "rev-1"), os.path.join(acme, "stolen"))

        def scan(tenant_scoped):
            target = {"path": content, "recursive": True}
            if tenant_scoped:
                target["tenant_scoped"] = True
            return FimCollector(
                {"targets": [target], "max_entries": 500,
                 "max_file_bytes": 1 << 20, "max_depth": 16}).scan()["snapshot"]

        scoped = scan(True)
        th.assert_true("anomaly" not in scoped[os.path.join(acme, "current")],
                       "a tenant's own release alias is ordinary in-tenant movement")
        th.assert_eq(scoped[os.path.join(acme, "stolen")].get("anomaly"),
                     "symlink_escape",
                     "a link into a sibling tenant must read as an escape")
        flat = scan(False)
        th.assert_true("anomaly" not in flat[os.path.join(acme, "stolen")],
                       "an unscoped target must keep its historical whole-root rule")
