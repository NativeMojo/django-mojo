"""GeoIP threat-intelligence regression suite (maestro item #1079).

Threat detection was inert framework-wide:

  1. check_internal_threats() raised UnboundLocalError on every IP that had
     recorded events (`models.Avg` used a line before `from django.db import
     models`), and the blanket except turned that into a silent False/False.
  2. geolocate_ip() never fed threat-intel results into `threat_level` — it
     only passed the provider-native `data['threat']` blob, which no built-in
     provider populates.
  3. perform_threat_check() wrote `is_blocklisted` only at the top level, while
     recalculate_threat_level() reads it from inside `threat_data` — so the +30
     blocklist weight never fired on the GeoLocatedIP.check_threats() path
     either (the path behind the `threat_analysis` / `refresh` REST actions).

Everything here is offline: no blocklist.de fetch, no Tor exit list, no
provider HTTP. Addresses are TEST-NET-3 (203.0.113.0/24) and unique to this
file — except the two that exercise geolocate_ip() end to end, which must be
globally routable (see GEOLOCATE_IP below).
"""
from unittest import mock
from testit import helpers as th

ATTACKER_IP = "203.0.113.90"
ABUSER_IP = "203.0.113.91"
CLEAN_IP = "203.0.113.92"
BLOCKLIST_IP = "203.0.113.93"
CHECK_THREATS_IP = "203.0.113.96"
EXCLUDED_IP = "203.0.113.97"
MIXED_IP = "203.0.113.98"

# geolocate_ip() short-circuits on private/reserved addresses BEFORE any
# provider or threat-intel work, and Python's ipaddress module reports the
# documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24) as private —
# a TEST-NET address here would take the early return and prove nothing.
# These two are globally routable and used nowhere else in the suite; nothing
# in these tests ever contacts them (every fetch is stubbed).
GEOLOCATE_IP = "102.99.113.94"
GEOLOCATE_CLEAN_IP = "102.99.113.95"

# A category that is NOT on the attacker-count exclusion list.
ATTACK_CATEGORY = "security:breach"
# A category that IS excluded — reported at level 8 for a mistyped username.
ENUMERATION_CATEGORY = "login:unknown"


def _seed_events(ip_address, count, level, category=ATTACK_CATEGORY):
    """Delete every event for this IP, then create `count` fresh ones.

    Tests run against a long-lived DB, so setup must clear its own rows first.
    A bare Event.objects.create() never calls sync_metadata(), so nothing here
    triggers a geo lookup.
    """
    from mojo.apps.incident.models.event import Event
    Event.objects.filter(source_ip=ip_address).delete()
    for i in range(count):
        Event.objects.create(
            source_ip=ip_address,
            level=level,
            category=category,
            country_code="US",
            title=f"test event {i}",
        )


def _blocklist_hit():
    """A check_all_blocklists() return value representing a listed IP."""
    return {
        'blocklist_hits': [{'source': 'blocklist.de', 'is_listed': True}],
        'is_blocklisted': True,
    }


def _stub_provider_geo(ip_address):
    """A benign provider payload — nothing that trips VPN/proxy/cloud keywords."""
    return {
        'provider': 'ipinfo',
        'country_code': 'US',
        'country_name': 'United States',
        'region': 'California',
        'city': 'San Francisco',
        'asn': 'AS64500',
        'asn_org': 'Example Residential Broadband',
        'isp': 'Example Residential Broadband',
        'connection_type': 'residential',
        'data': {},
    }


def _single_provider_patches():
    """Force geolocate_ip() through one stubbed provider, with no network."""
    from mojo.helpers.geoip import config, detection
    return [
        mock.patch.object(config, "PRIMARY_PROVIDER", "ipinfo"),
        mock.patch.object(config, "FALLBACK_PROVIDER", None),
        mock.patch.object(config, "ADDITIONAL_PROVIDERS", []),
        mock.patch.dict("mojo.helpers.geoip.PROVIDERS",
                        {"ipinfo": lambda ip: _stub_provider_geo(ip)}),
        mock.patch.object(detection, "detect_tor", return_value=False),
    ]


@th.django_unit_test("threat intel: high-severity events flag a known attacker")
def test_internal_threats_flags_known_attacker(opts):
    from mojo.helpers.geoip import threat_intel

    _seed_events(ATTACKER_IP, 6, level=10)

    result = threat_intel.check_internal_threats(ATTACKER_IP)
    stats = result['internal_stats']

    assert 'error' not in stats, (
        "check_internal_threats must complete without swallowing an exception; "
        f"got error={stats.get('error')!r}"
    )
    assert result['is_known_attacker'] is True, (
        "6 level-10 events over the 90-day window must set is_known_attacker "
        f"(threshold is 5); got {result!r}"
    )
    assert stats['high_severity_events'] == 6, (
        f"expected 6 high-severity events, got {stats.get('high_severity_events')!r}"
    )
    assert stats['total_events'] == 6, (
        f"expected total_events=6, got {stats.get('total_events')!r}"
    )
    assert float(stats['avg_level']) == 10.0, (
        f"expected avg_level 10.0, got {stats.get('avg_level')!r}"
    )


@th.django_unit_test("threat intel: many mid-level events flag a known abuser")
def test_internal_threats_flags_known_abuser(opts):
    from mojo.helpers.geoip import threat_intel

    _seed_events(ABUSER_IP, 12, level=5)

    result = threat_intel.check_internal_threats(ABUSER_IP)
    stats = result['internal_stats']

    assert 'error' not in stats, (
        "check_internal_threats must complete without swallowing an exception; "
        f"got error={stats.get('error')!r}"
    )
    assert result['is_known_abuser'] is True, (
        "12 level-5 events must set is_known_abuser (>=10 events, 4 <= avg < 8); "
        f"got {result!r}"
    )
    assert result['is_known_attacker'] is False, (
        "level-5 events are below the attacker threshold (8) — is_known_attacker "
        f"must stay False; got {result!r}"
    )
    assert stats['high_severity_events'] == 0, (
        f"expected 0 high-severity events, got {stats.get('high_severity_events')!r}"
    )


@th.django_unit_test("threat intel: an IP with no events is clean (guard)")
def test_internal_threats_no_events_is_clean(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.helpers.geoip import threat_intel

    Event.objects.filter(source_ip=CLEAN_IP).delete()

    result = threat_intel.check_internal_threats(CLEAN_IP)

    assert result['is_known_attacker'] is False, (
        f"an IP with no events must not be an attacker; got {result!r}"
    )
    assert result['is_known_abuser'] is False, (
        f"an IP with no events must not be an abuser; got {result!r}"
    )
    assert result['internal_stats'] == {'total_events': 0}, (
        f"clean IPs take the early return; got {result['internal_stats']!r}"
    )


@th.django_unit_test("threat intel: excluded categories never flag an attacker")
def test_internal_threats_excluded_categories_not_attacker(opts):
    """Enumeration/typo categories are reported at level 8 but must not count.

    On a shared NAT egress a handful of mistyped usernames would otherwise
    brand the address a known attacker for every user behind it.
    """
    from mojo.helpers.geoip import threat_intel

    _seed_events(EXCLUDED_IP, 6, level=10, category=ENUMERATION_CATEGORY)

    result = threat_intel.check_internal_threats(EXCLUDED_IP)
    stats = result['internal_stats']

    assert 'error' not in stats, (
        "check_internal_threats must complete without swallowing an exception; "
        f"got error={stats.get('error')!r}"
    )
    assert result['is_known_attacker'] is False, (
        f"{ENUMERATION_CATEGORY!r} events must be excluded from the attacker "
        f"count no matter their level; got {result!r}"
    )
    assert stats['high_severity_events'] == 0, (
        "excluded categories must not appear in high_severity_events; got "
        f"{stats.get('high_severity_events')!r}"
    )
    assert stats['total_events'] == 6, (
        "the exclusion applies to the attacker count ONLY — total_events must "
        f"still count every event; got {stats.get('total_events')!r}"
    )
    assert float(stats['avg_level']) == 10.0, (
        "avg_level must still average every event, excluded or not; got "
        f"{stats.get('avg_level')!r}"
    )


@th.django_unit_test("threat intel: mixed categories count only the non-excluded ones")
def test_internal_threats_mixed_categories_count_only_included(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.helpers.geoip import threat_intel

    # 3 excluded + 6 counted -> over the threshold on the counted ones alone.
    _seed_events(MIXED_IP, 3, level=10, category=ENUMERATION_CATEGORY)
    for i in range(6):
        Event.objects.create(
            source_ip=MIXED_IP, level=10, category=ATTACK_CATEGORY,
            country_code="US", title=f"mixed attack {i}",
        )

    result = threat_intel.check_internal_threats(MIXED_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is True, (
        "6 counted level-10 events (plus 3 excluded) must set is_known_attacker; "
        f"got {result!r}"
    )
    assert stats['high_severity_events'] == 6, (
        "only the non-excluded events may be counted; expected 6, got "
        f"{stats.get('high_severity_events')!r}"
    )
    assert stats['total_events'] == 9, (
        f"total_events must count all 9 events, got {stats.get('total_events')!r}"
    )

    # Now drop to 3 counted events — below the threshold of 5.
    Event.objects.filter(source_ip=MIXED_IP, category=ATTACK_CATEGORY).delete()
    for i in range(3):
        Event.objects.create(
            source_ip=MIXED_IP, level=10, category=ATTACK_CATEGORY,
            country_code="US", title=f"mixed attack below {i}",
        )

    result = threat_intel.check_internal_threats(MIXED_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is False, (
        "3 counted + 3 excluded level-10 events is below the attacker threshold "
        f"of 5 — the excluded ones must not make up the difference; got {result!r}"
    )
    assert stats['high_severity_events'] == 3, (
        f"expected 3 counted high-severity events, got {stats.get('high_severity_events')!r}"
    )
    assert stats['total_events'] == 6, (
        f"total_events must count all 6 events, got {stats.get('total_events')!r}"
    )


@th.django_unit_test("threat intel: perform_threat_check exposes is_blocklisted in threat_data")
def test_perform_threat_check_exposes_blocklisted(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.helpers.geoip import threat_intel

    Event.objects.filter(source_ip=BLOCKLIST_IP).delete()

    with mock.patch.object(threat_intel, "check_all_blocklists",
                           return_value=_blocklist_hit()):
        result = threat_intel.perform_threat_check(BLOCKLIST_IP)

    assert result['is_blocklisted'] is True, (
        f"top-level is_blocklisted must reflect the blocklist hit; got {result!r}"
    )
    assert result['threat_data']['is_blocklisted'] is True, (
        "threat_data.is_blocklisted is the copy that gets persisted and that "
        "recalculate_threat_level() reads — it must be present and True; got "
        f"{result['threat_data']!r}"
    )
    assert result['threat_data']['blocklists'] == [
        {'source': 'blocklist.de', 'is_listed': True}
    ], (
        "the per-source hit list must be preserved alongside the flag; got "
        f"{result['threat_data']['blocklists']!r}"
    )


@th.django_unit_test("threat intel: recalculate_threat_level scores a blocklist hit (guard)")
def test_recalculate_threat_level_counts_blocklist_hit(opts):
    """Guard, not a regression — this read path already worked.

    What was broken was the WRITE side: nothing ever put is_blocklisted where
    this function looks. This pins the reader so a future refactor can't move
    the key out from under it.
    """
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.helpers.geoip import threat_intel

    # Unsaved instance — recalculate_threat_level only reads attributes.
    geo = GeoLocatedIP(
        ip_address="203.0.113.99",
        is_known_attacker=False,
        is_known_abuser=False,
        is_tor=False,
        is_vpn=False,
        is_proxy=False,
        data={'threat_data': {
            'internal': {},
            'blocklists': [{'source': 'blocklist.de', 'is_listed': True}],
            'is_blocklisted': True,
        }},
    )

    level = threat_intel.recalculate_threat_level(geo)

    assert level == 'medium', (
        f"a bare blocklist hit scores 30 -> 'medium'; got {level!r}"
    )


@th.django_unit_test("threat intel: escalate_threat_level never downgrades")
def test_escalate_threat_level_never_downgrades(opts):
    from mojo.helpers.geoip import threat_intel

    assert threat_intel.escalate_threat_level('high', 'medium') == 'high', (
        "escalate must keep the higher level when the candidate is lower"
    )
    assert threat_intel.escalate_threat_level('low', 'critical') == 'critical', (
        "escalate must take the candidate when it is higher"
    )
    assert threat_intel.escalate_threat_level('medium', 'medium') == 'medium', (
        "equal levels must be a no-op"
    )
    assert threat_intel.escalate_threat_level(None, 'low') == 'low', (
        "an unset current level must be replaced by any real level"
    )


@th.django_unit_test("geoip: geolocate_ip escalates threat_level on a blocklist hit")
def test_geolocate_ip_escalates_threat_level_on_blocklist_hit(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.helpers import geoip
    from mojo.helpers.geoip import threat_intel

    Event.objects.filter(source_ip=GEOLOCATE_IP).delete()

    blocklisted = {
        'is_known_attacker': False,
        'is_known_abuser': False,
        'is_blocklisted': True,
        'threat_data': {
            'internal': {'total_events': 0},
            'blocklists': [{'source': 'blocklist.de', 'is_listed': True}],
            'is_blocklisted': True,
        },
    }

    patches = _single_provider_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
         mock.patch.object(threat_intel, "perform_threat_check",
                           return_value=blocklisted):
        result = geoip.geolocate_ip(GEOLOCATE_IP, check_threats=True)

    assert result is not None, "the stubbed provider must return geo data"
    # Break 2 first: threat intel has to reach threat_level at all.
    assert result['threat_level'] != 'low', (
        "a blocklisted IP must not come back as 'low' — threat intel has to "
        f"reach threat_level; got {result['threat_level']!r}"
    )
    assert result['threat_level'] == 'medium', (
        "a bare blocklist hit scores 30 -> 'medium', the same as the "
        f"check_threats() path; got {result['threat_level']!r}"
    )
    assert result.get('is_blocklisted') is True, (
        "geolocate_ip must promote the blocklist flag to the top level; got "
        f"{result.get('is_blocklisted', '<missing>')!r}"
    )
    assert result['data']['threat_data']['is_blocklisted'] is True, (
        "the persisted threat_data blob must carry the flag; got "
        f"{result['data'].get('threat_data')!r}"
    )


@th.django_unit_test("geoip: geolocate_ip(check_threats=False) is unchanged (guard)")
def test_geolocate_ip_check_threats_false_unchanged(opts):
    """No over-escalation: without threat checks nothing may move threat_level."""
    from mojo.helpers import geoip

    patches = _single_provider_patches()
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = geoip.geolocate_ip(GEOLOCATE_CLEAN_IP, check_threats=False)

    assert result is not None, "the stubbed provider must return geo data"
    assert result['threat_level'] == 'low', (
        "a benign residential IP with check_threats=False must stay 'low'; got "
        f"{result['threat_level']!r}"
    )
    assert result['is_known_attacker'] is False, (
        f"check_threats=False must leave is_known_attacker False; got {result!r}"
    )
    assert result['is_known_abuser'] is False, (
        f"check_threats=False must leave is_known_abuser False; got {result!r}"
    )
    assert result.get('is_blocklisted') is False, (
        "is_blocklisted must be present and False on every branch; got "
        f"{result.get('is_blocklisted', '<missing>')!r}"
    )


@th.django_unit_test("geoip: GeoLocatedIP.check_threats raises threat_level on a blocklist hit")
def test_check_threats_raises_threat_level_on_blocklist_hit(opts):
    """End-to-end on the path production actually uses.

    `threat_analysis` and `refresh` REST actions land here. Before the fix,
    perform_threat_check() stored a threat_data blob with no is_blocklisted
    key, so recalculate_threat_level() scored 0 and the level stayed 'low'.
    """
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.incident.models.event import Event
    from mojo.helpers.geoip import threat_intel

    Event.objects.filter(source_ip=CHECK_THREATS_IP).delete()
    GeoLocatedIP.objects.filter(ip_address=CHECK_THREATS_IP).delete()
    geo = GeoLocatedIP.objects.create(
        ip_address=CHECK_THREATS_IP,
        provider="ipinfo",
        threat_level="low",
        data={},
    )

    with mock.patch.object(threat_intel, "check_all_blocklists",
                           return_value=_blocklist_hit()):
        threat_results = geo.check_threats()

    assert threat_results['is_blocklisted'] is True, (
        f"check_threats must report the blocklist hit; got {threat_results!r}"
    )
    assert geo.data['threat_data']['is_blocklisted'] is True, (
        "the stored threat_data blob must carry is_blocklisted — this is the "
        f"key recalculate_threat_level() reads; got {geo.data.get('threat_data')!r}"
    )
    assert geo.threat_level == 'medium', (
        "a blocklisted IP must escalate off 'low' (30 points -> 'medium'); got "
        f"{geo.threat_level!r}"
    )

    geo.refresh_from_db()
    assert geo.threat_level == 'medium', (
        f"the escalated threat_level must be persisted; got {geo.threat_level!r}"
    )
