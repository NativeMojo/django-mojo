"""Threat-list cache tests (ITEM-021) — IPSet-backed cache-only rows for the
Tor exit list and blocklist.de, consumed by mojo.helpers.geoip detection.

All in-process (no opts.client, no network): the readers short-circuit on a
seeded cache row, so nothing here ever issues a live fetch. Lives in
test_geofence (not test_incident) because test_incident is an opt-in module
the default suite skips.

The two rows (tor_exits / blocklist_de) are global singletons in the shared
test DB — each test (re)seeds the state it needs. Seeding them is strictly
safer for parallel modules than deleting them: a populated row PREVENTS
detect_tor from attempting a live network fetch.
"""
from testit import helpers as th

LISTED_IP = "198.51.100.7"
UNLISTED_IP = "198.51.100.200"


def _ensure_rows():
    from mojo.apps.incident.models import IPSet
    return {row.name: row for row in IPSet.ensure_threat_caches()}


@th.django_unit_test("threat cache: ensure_threat_caches creates disabled rows, idempotent")
def test_ensure_threat_caches(opts):
    from mojo.apps.incident.models import IPSet
    IPSet.objects.filter(name__in=["tor_exits", "blocklist_de"]).delete()

    rows = _ensure_rows()
    assert set(rows) == {"tor_exits", "blocklist_de"}, \
        f"both cache rows must be created, got {sorted(rows)}"
    for name, row in rows.items():
        assert row.is_enabled is False, \
            f"{name} must be created DISABLED (enabled rows reach the kernel firewall)"
        assert "do NOT enable" in (row.description or ""), \
            f"{name} must carry the do-not-enable warning: {row.description!r}"
    assert rows["tor_exits"].source == "tor", \
        f"tor_exits source wrong: {rows['tor_exits'].source!r}"
    assert rows["blocklist_de"].source == "blocklist_de", \
        f"blocklist_de source wrong: {rows['blocklist_de'].source!r}"

    # Idempotent — second call must not duplicate or reset anything.
    again = _ensure_rows()
    assert IPSet.objects.filter(name="tor_exits").count() == 1, \
        "ensure must not duplicate rows"

    # get_or_create only applies defaults on create: an operator's explicit
    # flag change survives (we don't fight the operator, docs warn them).
    row = again["tor_exits"]
    row.is_enabled = True
    row.save(update_fields=["is_enabled"])
    _ensure_rows()
    row.refresh_from_db()
    assert row.is_enabled is True, "ensure must not overwrite operator changes"
    row.is_enabled = False
    row.save(update_fields=["is_enabled"])


@th.django_unit_test("threat cache: _parse_tor_exit_list extracts ExitAddress IPs")
def test_parse_tor_exit_list(opts):
    from mojo.apps.incident.models.ipset import _parse_tor_exit_list
    text = (
        "ExitNode ABCDEF0123456789\n"
        "Published 2026-07-08 01:00:00\n"
        "LastStatus 2026-07-08 02:00:00\n"
        f"ExitAddress {LISTED_IP} 2026-07-08 02:00:00\n"
        "ExitNode FEDCBA9876543210\n"
        "ExitAddress 203.0.113.44 2026-07-08 02:00:00\n"
        "# stray comment\n"
        "ExitAddress\n"  # malformed — no IP, must be skipped
    )
    ips = _parse_tor_exit_list(text)
    assert ips == [LISTED_IP, "203.0.113.44"], \
        f"parser must extract exactly the exit IPs, got {ips}"


@th.django_unit_test("threat cache: detect_tor reads the seeded cache, no network")
def test_detect_tor_cached(opts):
    from mojo.helpers.geoip import detection
    rows = _ensure_rows()
    row = rows["tor_exits"]
    row.set_data([LISTED_IP, "203.0.113.44"])
    row.save(update_fields=["data", "cidr_count"])

    assert detection.detect_tor(LISTED_IP) is True, \
        "listed IP must be detected via the cache"
    assert detection.detect_tor(UNLISTED_IP) is False, \
        "unlisted IP must not be flagged"


@th.django_unit_test("threat cache: check_blocklist_de reads the seeded cache, no network")
def test_check_blocklist_de_cached(opts):
    from mojo.helpers.geoip import threat_intel
    rows = _ensure_rows()
    row = rows["blocklist_de"]
    row.set_data([LISTED_IP])
    row.save(update_fields=["data", "cidr_count"])

    result = threat_intel.check_blocklist_de(LISTED_IP)
    assert result == {"source": "blocklist.de", "is_listed": True}, \
        f"listed IP must report is_listed=true, got {result}"
    result = threat_intel.check_blocklist_de(UNLISTED_IP)
    assert result == {"source": "blocklist.de", "is_listed": False}, \
        f"unlisted IP must report is_listed=false, got {result}"


@th.django_unit_test("threat cache: missing/empty row signals fallback (None)")
def test_cached_ip_set_fallback_signal(opts):
    from mojo.apps.incident.models import IPSet
    from mojo.helpers.geoip.detection import _cached_ip_set

    IPSet.objects.filter(name="tor_exits").delete()
    assert _cached_ip_set("tor_exits") is None, \
        "missing row must return None (live-fetch fallback)"

    rows = _ensure_rows()
    row = rows["tor_exits"]
    row.data = ""
    row.cidr_count = 0
    row.save(update_fields=["data", "cidr_count"])
    assert _cached_ip_set("tor_exits") is None, \
        "empty row must return None (cache not warmed yet)"

    row.set_data([LISTED_IP])
    row.save(update_fields=["data", "cidr_count"])
    assert _cached_ip_set("tor_exits") == {LISTED_IP}, \
        "populated row must return the IP set"


@th.django_unit_test("threat cache: excluded from the weekly refresh_ipsets selection")
def test_weekly_cron_excludes_threat_caches(opts):
    from mojo.apps.incident.models import IPSet
    _ensure_rows()
    # The refresh_ipsets asyncjob refreshes AND sync()s this exact selection
    # into the kernel firewall — the cache-only rows must never be in it.
    selected = set(
        IPSet.objects.filter(is_enabled=True).exclude(source="manual")
        .values_list("name", flat=True))
    assert "tor_exits" not in selected, \
        "tor_exits must be excluded from the firewall-sync cron (is_enabled=False)"
    assert "blocklist_de" not in selected, \
        "blocklist_de must be excluded from the firewall-sync cron (is_enabled=False)"
