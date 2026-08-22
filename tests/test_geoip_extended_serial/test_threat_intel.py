"""GeoIP threat-intel tests that patch shared module attributes.

Moved from tests/test_geoip/test_threat_intel.py (maestro item #2558): each
of these mock.patches a process-global surface — threat_intel threshold /
dry-run module attributes, an ip_activity_stats spy, or the geoip
config/PROVIDERS/detection bundle the geolocate_ip() end-to-end paths need —
so they run only in this opt-in serial tier. Assertions are verbatim from the
source file.

`test_thresholds_are_db_tunable` joined them for a different reason: it writes
the GLOBAL `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD` Setting row and
pushes it to cache, retuning detection for any parallel module that calls
`check_internal_threats()` inside that window.

Addresses are TEST-NET-3 and unique to this file — except the two
geolocate_ip() addresses, which must be globally routable (Python's ipaddress
module reports the documentation ranges as private, and geolocate_ip()
short-circuits on private addresses before any provider or threat-intel
work). Nothing here ever contacts them: every fetch is stubbed.
"""
from unittest import mock
from testit import helpers as th

DRYRUN_IP = "203.0.113.105"
SUSPECT_IP = "203.0.113.109"
PATCHABLE_IP = "203.0.113.110"
MEMO_IP = "203.0.113.111"
# Unique to this file: the db-tunable test writes a GLOBAL threshold row
# while it holds this address, so no other module may share it.
DBTUNE_IP = "203.0.113.112"

GEOLOCATE_IP = "102.99.113.94"
GEOLOCATE_CLEAN_IP = "102.99.113.95"

# Tier 1 — unambiguous. Emitted at level 9 by the bouncer honeypot.
CONFIRMED_CATEGORY = "security:bouncer:honeypot_post"
# Tier 2 — a real user can produce this by fumbling their own password.
SUSPECT_CATEGORY = "invalid_password"


def _seed_events(ip_address, count, level, category=CONFIRMED_CATEGORY,
                 age_hours=1, targets=0, reset=True):
    """Create `count` events for this IP, placed `age_hours` in the past.

    Same helper as tests/test_geoip/test_threat_intel.py — see there for the
    full rationale (auto_now_add forces the follow-up UPDATE; targets spreads
    events across distinct account.User model_ids).
    """
    from datetime import timedelta
    from mojo.apps.incident.models.event import Event
    from mojo.helpers import dates

    if reset:
        Event.objects.filter(source_ip=ip_address).delete()

    pks = []
    for i in range(count):
        extra = {}
        if targets:
            extra['model_name'] = 'account.User'
            extra['model_id'] = 9000 + (i % targets)
        row = Event.objects.create(
            source_ip=ip_address,
            level=level,
            category=category,
            country_code="US",
            title=f"test event {i}",
            **extra
        )
        pks.append(row.pk)

    if pks:
        Event.objects.filter(pk__in=pks).update(
            created=dates.utcnow() - timedelta(hours=age_hours))
    return pks


def _seed_devices(ip_address, count, device_age_hours=48):
    """Give `ip_address` `count` distinct bouncer devices inside the window."""
    from datetime import timedelta
    from mojo.apps.account.models.bouncer_device import BouncerDevice
    from mojo.apps.account.models.bouncer_signal import BouncerSignal
    from mojo.helpers import dates

    muids = [f"muid-{ip_address}-{i}" for i in range(count)]
    BouncerSignal.objects.filter(ip_address=ip_address).delete()
    BouncerDevice.objects.filter(muid__in=muids).delete()

    for muid in muids:
        BouncerDevice.objects.create(muid=muid)
        BouncerSignal.objects.create(ip_address=ip_address, muid=muid)

    if device_age_hours:
        BouncerDevice.objects.filter(muid__in=muids).update(
            first_seen=dates.utcnow() - timedelta(hours=device_age_hours))
    return muids


def _clear_devices(ip_address):
    from mojo.apps.account.models.bouncer_signal import BouncerSignal
    BouncerSignal.objects.filter(ip_address=ip_address).delete()


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


@th.django_unit_test("threat intel: thresholds are re-read on every call")
def test_thresholds_are_read_at_call_time(opts):
    """Guards the patching strategy the rest of this module depends on.

    The thresholds used to be captured into module constants at import via
    get_static(). If _config() ever goes back to reading a frozen value, this
    test fails — and every other test here would start passing vacuously.
    """
    from mojo.helpers.geoip import threat_intel

    _clear_devices(PATCHABLE_IP)
    _seed_events(PATCHABLE_IP, 5, level=9, category=CONFIRMED_CATEGORY)

    with mock.patch.object(threat_intel,
                           "INTERNAL_ATTACKER_CONFIRMED_THRESHOLD", 20):
        raised = threat_intel.check_internal_threats(PATCHABLE_IP)
    assert raised['is_known_attacker'] is False, (
        "raising the confirmed threshold to 20 must take effect on the next "
        f"call; got {raised!r}"
    )

    with mock.patch.object(threat_intel,
                           "INTERNAL_ATTACKER_CONFIRMED_THRESHOLD", 2):
        lowered = threat_intel.check_internal_threats(PATCHABLE_IP)
    assert lowered['is_known_attacker'] is True, (
        f"lowering it to 2 must take effect too; got {lowered!r}"
    )


@th.django_unit_test("threat intel: the deprecated exclusion list is still honored")
def test_deprecated_excluded_categories_still_honored(opts):
    """Nobody's config may silently invert. A deployment that excluded a
    category keeps excluding it — with a deprecation warning."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(SUSPECT_IP)
    _seed_events(SUSPECT_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=10)

    assert threat_intel.check_internal_threats(SUSPECT_IP)['is_known_attacker'] is True, (
        "precondition: this IP is an attacker with no exclusions configured"
    )

    with mock.patch.object(threat_intel, "INTERNAL_ATTACKER_EXCLUDED_CATEGORIES",
                           [SUSPECT_CATEGORY]):
        result = threat_intel.check_internal_threats(SUSPECT_IP)

    assert result['is_known_attacker'] is False, (
        f"an operator who excluded {SUSPECT_CATEGORY!r} must keep that "
        f"exclusion after the denylist became an allowlist; got {result!r}"
    )
    assert result['internal_stats']['suspect_events'] == 0, (
        "the excluded category must drop out of the suspect tier entirely; got "
        f"{result['internal_stats']!r}"
    )


@th.django_unit_test("threat intel: dry run computes the verdict but acts on nothing")
def test_dry_run_returns_false_but_records_the_verdict(opts):
    """Lets a busy deployment watch a week of real traffic before a retune can
    block anyone."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(DRYRUN_IP)
    _seed_events(DRYRUN_IP, 3, level=9, category=CONFIRMED_CATEGORY)

    live = threat_intel.check_internal_threats(DRYRUN_IP)
    assert live['is_known_attacker'] is True, (
        f"precondition: this IP is an attacker with dry run off; got {live!r}"
    )

    with mock.patch.object(threat_intel, "INTERNAL_THREAT_DRY_RUN", True):
        result = threat_intel.check_internal_threats(DRYRUN_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is False, (
        f"dry run must never return a positive verdict; got {result!r}"
    )
    assert result['is_known_abuser'] is False, (
        f"dry run must never return a positive verdict; got {result!r}"
    )
    assert stats.get('dry_run') is True, (
        f"the stats must say the verdict was withheld; got {stats!r}"
    )
    assert stats.get('dry_run_is_known_attacker') is True, (
        "the withheld verdict must be recorded so an operator can see what "
        f"would have happened; got {stats!r}"
    )
    assert stats['confirmed_events'] == 3, (
        "dry run must still compute the driving counts; got "
        f"{stats.get('confirmed_events')!r}"
    )


@th.django_unit_test("incident: Event IP rate properties memoize the stats read")
def test_ip_rate_properties_memoized(opts):
    """Split from test_event_ip_rate_properties (which stays in the default
    tier): the memoization assertion needs a spy on
    threat_intel.ip_activity_stats, a mock.patch of the shared module.

    check_by_category() walks every RuleSet in the category, so a property
    can be read many times per event — memoization is required, not an
    optimisation.
    """
    from mojo.apps.incident.models.event import Event
    from mojo.helpers.geoip import threat_intel

    _seed_devices(MEMO_IP, 4)
    _seed_events(MEMO_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=10)

    fresh = Event.objects.filter(source_ip=MEMO_IP).first()
    with mock.patch.object(threat_intel, "ip_activity_stats",
                           wraps=threat_intel.ip_activity_stats) as spy:
        fresh.ip_recent_attack_events
        fresh.ip_recent_distinct_targets
        fresh.ip_recent_distinct_devices
        fresh.ip_recent_attack_events
    assert spy.call_count == 1, (
        "four property reads on one event must hit the DB once; got "
        f"{spy.call_count} calls"
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


# ---------------------------------------------------------------------------
# Tunability — a DB-backed Setting row, visible to every parallel module
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: a DB-backed Setting retunes detection")
def test_thresholds_are_db_tunable(opts):
    """The point of the rewrite: each deployment tunes its own rules.

    get_static() reads file-based settings only, so before this nothing was
    tunable without a code edit and a restart.
    """
    from mojo.apps.account.models.setting import Setting
    from mojo.helpers.geoip import threat_intel

    key = "GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD"
    _clear_devices(DBTUNE_IP)
    _seed_events(DBTUNE_IP, 3, level=9, category=CONFIRMED_CATEGORY)

    baseline = threat_intel.check_internal_threats(DBTUNE_IP)
    assert baseline['is_known_attacker'] is True, (
        f"3 confirmed events flag an attacker at the shipped default; got {baseline!r}"
    )

    Setting.objects.filter(key=key, group=None).delete()
    row = Setting.objects.create(key=key, value="25", group=None)
    try:
        row.push_to_cache()
        tuned = threat_intel.check_internal_threats(DBTUNE_IP)
        assert tuned['is_known_attacker'] is False, (
            "a DB-backed Setting of 25 must raise the bar without a restart; "
            f"got {tuned!r}"
        )
    finally:
        row.remove_from_cache()
        row.delete()

    restored = threat_intel.check_internal_threats(DBTUNE_IP)
    assert restored['is_known_attacker'] is True, (
        f"deleting the Setting must restore the shipped default; got {restored!r}"
    )
