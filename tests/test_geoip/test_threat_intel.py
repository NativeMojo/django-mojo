"""GeoIP threat-intelligence suite (maestro items #1079, #1097).

Two rounds of work live here.

#1079 fixed threat detection being inert framework-wide:

  1. check_internal_threats() raised UnboundLocalError on every IP that had
     recorded events (`models.Avg` used a line before `from django.db import
     models`), and the blanket except turned that into a silent False/False.
  2. geolocate_ip() never fed threat-intel results into `threat_level`.
  3. perform_threat_check() wrote `is_blocklisted` only at the top level, while
     recalculate_threat_level() reads it from inside `threat_data`.

#1097 (phase B) then rewrote WHAT counts, because a working detector with the
old thresholds blocks real users. The attacker predicate is now a two-tier
ALLOWLIST over a 24-hour window: a few CONFIRMED-category events, or many
SUSPECT-category events spread across many distinct accounts from an address
that is not a shared egress. Everything unnamed counts toward nothing.

Everything here is offline: no blocklist.de fetch, no Tor exit list, no
provider HTTP. Addresses are TEST-NET-3 (203.0.113.0/24) and unique to this
file. The two geolocate_ip() end-to-end tests moved to
tests/test_geoip_extended_serial/test_threat_intel.py (item #2558), along
with every test that mock.patches the shared threat_intel/geoip modules.
"""
from testit import helpers as th

ATTACKER_IP = "203.0.113.90"
ABUSER_IP = "203.0.113.91"
CLEAN_IP = "203.0.113.92"
BLOCKLIST_IP = "203.0.113.93"
CHECK_THREATS_IP = "203.0.113.96"
EXCLUDED_IP = "203.0.113.97"
MIXED_IP = "203.0.113.98"
WINDOW_IP = "203.0.113.100"
UNLISTED_IP = "203.0.113.101"
BREADTH_IP = "203.0.113.102"
EGRESS_IP = "203.0.113.103"
FEEDBACK_IP = "203.0.113.104"
DRYRUN_IP = "203.0.113.105"
DBTUNE_IP = "203.0.113.106"
PROPS_IP = "203.0.113.107"
NOT_ABUSER_IP = "203.0.113.108"
SUSPECT_IP = "203.0.113.109"
PATCHABLE_IP = "203.0.113.110"

# Tier 1 — unambiguous. Emitted at level 9 by the bouncer honeypot.
CONFIRMED_CATEGORY = "security:bouncer:honeypot_post"
# Tier 2 — a real user can produce this by fumbling their own password.
# Emitted at level 5, which the old level-8 floor made invisible.
SUSPECT_CATEGORY = "invalid_password"
# The enumeration category a mistyped USERNAME produces. Suspect tier, but
# class-level reporting leaves model_id NULL so it can never satisfy the
# breadth gate on its own.
ENUMERATION_CATEGORY = "login:unknown"
# On neither list. Level 10, and under the old denylist that was enough.
UNLISTED_CATEGORY = "security:breach"
# The bouncer's own block decision (level 8). Counting this was a
# self-confirming loop: a block raised the score that produced the next block.
BOUNCER_BLOCK_CATEGORY = "security:bouncer:block"


def _seed_events(ip_address, count, level, category=CONFIRMED_CATEGORY,
                 age_hours=1, targets=0, reset=True):
    """Create `count` events for this IP, placed `age_hours` in the past.

    Tests run against a long-lived DB, so setup clears its own rows first
    (reset=False appends instead, for mixed-category cases).

    Event.created is auto_now_add, so the only way to place an event in time is
    a follow-up UPDATE — without it every seeded event lands inside the window
    and the window tests would prove nothing.

    targets spreads the events across N distinct account.User model_ids. 0
    leaves model_id NULL, which is what class-level reporting
    (User.class_report_incident, used by login:unknown) actually produces.

    A bare Event.objects.create() never calls sync_metadata(), so nothing here
    triggers a geo lookup.
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
    """Give `ip_address` `count` distinct bouncer devices inside the window.

    BouncerDevice.first_seen is auto_now_add and the suppressor only counts
    devices that predate the window, so first_seen needs the same follow-up
    UPDATE treatment. device_age_hours=0 leaves them brand new — the
    attacker-cycling-fresh-muids case the hardening is there to reject.
    """
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


def _blocklist_hit():
    """A check_all_blocklists() return value representing a listed IP."""
    return {
        'blocklist_hits': [{'source': 'blocklist.de', 'is_listed': True}],
        'is_blocklisted': True,
    }


# _stub_provider_geo and _single_provider_patches moved to
# tests/test_geoip_extended_serial/test_threat_intel.py with the
# geolocate_ip() tests (item #2558).


# ---------------------------------------------------------------------------
# Tier 1 — confirmed
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: confirmed-category events flag a known attacker")
def test_internal_threats_flags_known_attacker(opts):
    """A handful of honeypot POSTs is enough — no human trips a hidden field."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(ATTACKER_IP)
    _seed_events(ATTACKER_IP, 3, level=9, category=CONFIRMED_CATEGORY)

    result = threat_intel.check_internal_threats(ATTACKER_IP)
    stats = result['internal_stats']

    assert 'error' not in stats, (
        "check_internal_threats must complete without swallowing an exception; "
        f"got error={stats.get('error')!r}"
    )
    assert result['is_known_attacker'] is True, (
        "3 confirmed-category events must set is_known_attacker at the "
        f"confirmed threshold of 3; got {result!r}"
    )
    assert stats['confirmed_events'] == 3, (
        f"expected 3 confirmed events, got {stats.get('confirmed_events')!r}"
    )
    assert stats['high_severity_events'] == 3, (
        "high_severity_events is the in-window attack count that "
        f"recalculate_threat_level() scores; got {stats.get('high_severity_events')!r}"
    )
    assert stats['total_events'] == 3, (
        f"expected total_events=3, got {stats.get('total_events')!r}"
    )


@th.django_unit_test("threat intel: the confirmed tier ignores the shared-egress suppressor")
def test_confirmed_tier_not_suppressed_by_device_count(opts):
    """A confirmed attacker behind a corporate NAT is still an attacker."""
    from mojo.helpers.geoip import threat_intel

    _seed_devices(EGRESS_IP, 50)
    _seed_events(EGRESS_IP, 3, level=9, category=CONFIRMED_CATEGORY)

    result = threat_intel.check_internal_threats(EGRESS_IP)

    assert result['is_known_attacker'] is True, (
        "50 distinct devices must NOT suppress a confirmed-category verdict — "
        f"suppression is suspect-tier only; got {result!r}"
    )
    assert result['internal_stats']['shared_egress_suppressed'] is False, (
        "the suppressor must not even run on the confirmed path; got "
        f"{result['internal_stats']!r}"
    )


@th.django_unit_test("threat intel: one confirmed event is below the threshold (guard)")
def test_internal_threats_mixed_categories_count_only_included(opts):
    """Unlisted categories cannot make up the difference, whatever their level."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(MIXED_IP)
    # 1 confirmed + 20 unlisted level-10 events. Under the old denylist the 20
    # alone were four times the attacker threshold.
    _seed_events(MIXED_IP, 1, level=9, category=CONFIRMED_CATEGORY)
    _seed_events(MIXED_IP, 20, level=10, category=UNLISTED_CATEGORY, reset=False)

    result = threat_intel.check_internal_threats(MIXED_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is False, (
        "1 confirmed event is below the threshold of 3, and 20 unlisted "
        f"level-10 events count for nothing; got {result!r}"
    )
    assert stats['confirmed_events'] == 1, (
        f"expected 1 confirmed event, got {stats.get('confirmed_events')!r}"
    )
    assert stats['total_events'] == 21, (
        "total_events is a display stat and must still count every event; got "
        f"{stats.get('total_events')!r}"
    )

    # Two more confirmed events cross the line.
    _seed_events(MIXED_IP, 2, level=9, category=CONFIRMED_CATEGORY, reset=False)

    result = threat_intel.check_internal_threats(MIXED_IP)

    assert result['is_known_attacker'] is True, (
        f"3 confirmed events must reach the confirmed threshold; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Allowlist — the actual false-positive bug
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: unlisted categories never flag an attacker, at any level")
def test_unlisted_categories_never_flag_attacker(opts):
    """The false positive this rewrite exists to kill.

    `rest_error` is emitted at level 12 by mojo/decorators/http.py for ANY
    unhandled 500 and is attributed to the client's IP. Under the old
    level-8-floor denylist, five server bugs branded the caller an attacker.
    """
    from mojo.helpers.geoip import threat_intel

    _clear_devices(UNLISTED_IP)

    for category, level, why in [
        ("rest_error", 12, "an unhandled 500 is the server's fault, not the caller's"),
        ("assistant:error", 8, "an LLM handler failure is not an attack"),
        ("auth", 8, "a realtime socket that never sent a token is not an attack"),
        ("user_permission_denied", 4, "asking for something you cannot have is not an attack"),
    ]:
        _seed_events(UNLISTED_IP, 50, level=level, category=category)
        result = threat_intel.check_internal_threats(UNLISTED_IP)

        assert result['is_known_attacker'] is False, (
            f"50 {category!r} events at level {level} must not flag an "
            f"attacker — {why}; got {result!r}"
        )
        assert result['internal_stats']['high_severity_events'] == 0, (
            f"{category!r} is on neither allowlist and must contribute nothing "
            f"to the attack count; got {result['internal_stats']!r}"
        )


@th.django_unit_test("threat intel: the bouncer's own blocks are not evidence")
def test_bouncer_blocks_do_not_feed_back(opts):
    """Breaks the self-confirming loop.

    A bouncer block emits a level-8 security:bouncer:block event. Counting it
    made is_known_attacker true, which added +40 to the next risk score, which
    made the next block more likely — an address could convict itself.
    """
    from mojo.helpers.geoip import threat_intel

    _clear_devices(FEEDBACK_IP)
    _seed_events(FEEDBACK_IP, 20, level=8, category=BOUNCER_BLOCK_CATEGORY)

    result = threat_intel.check_internal_threats(FEEDBACK_IP)

    assert result['is_known_attacker'] is False, (
        "20 of the bouncer's own block decisions must not prove there is an "
        f"attacker — that is circular; got {result!r}"
    )
    assert result['internal_stats']['high_severity_events'] == 0, (
        "security:bouncer:block must contribute nothing to the attack count; "
        f"got {result['internal_stats']!r}"
    )


# ---------------------------------------------------------------------------
# Tier 2 — suspect: volume AND breadth AND not a shared egress
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: suspect volume across many accounts flags an attacker")
def test_suspect_tier_volume_plus_breadth_flags_attacker(opts):
    from mojo.helpers.geoip import threat_intel

    _clear_devices(SUSPECT_IP)
    _seed_events(SUSPECT_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=10)

    result = threat_intel.check_internal_threats(SUSPECT_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is True, (
        "30 invalid_password attempts against 10 distinct accounts in 24h is "
        f"credential stuffing; got {result!r}"
    )
    assert stats['suspect_events'] == 30, (
        f"expected 30 suspect events, got {stats.get('suspect_events')!r}"
    )
    assert stats['distinct_targets'] == 10, (
        f"expected 10 distinct targets, got {stats.get('distinct_targets')!r}"
    )


@th.django_unit_test("threat intel: suspect volume against ONE account is not an attack")
def test_suspect_tier_needs_breadth(opts):
    """The breadth gate is what separates an attacker from a locked-out user.

    Someone hammering their own password is throttled by the per-account
    sliding window (mojo/decorators/limits.py check_account_attempt), which is
    keyed on the account and survives IP rotation. It is the correct control
    there — this predicate must not double as it.
    """
    from mojo.helpers.geoip import threat_intel

    _clear_devices(BREADTH_IP)
    _seed_events(BREADTH_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=1)

    result = threat_intel.check_internal_threats(BREADTH_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is False, (
        "30 failed passwords against a SINGLE account is one frustrated user, "
        f"not a stuffing run; got {result!r}"
    )
    assert stats['suspect_events'] == 30, (
        "the volume is real — it is the breadth that is missing; got "
        f"{stats.get('suspect_events')!r}"
    )
    assert stats['distinct_targets'] == 1, (
        f"expected 1 distinct target, got {stats.get('distinct_targets')!r}"
    )


@th.django_unit_test("threat intel: username enumeration alone cannot satisfy the breadth gate")
def test_enumeration_without_targets_is_not_an_attack(opts):
    """login:unknown carries no model_id — an unknown username is nobody.

    A shared office egress produces these all day from mistyped usernames.
    """
    from mojo.helpers.geoip import threat_intel

    _clear_devices(EXCLUDED_IP)
    _seed_events(EXCLUDED_IP, 60, level=8, category=ENUMERATION_CATEGORY, targets=0)

    result = threat_intel.check_internal_threats(EXCLUDED_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is False, (
        "60 mistyped usernames name no account, so the breadth gate cannot be "
        f"satisfied; got {result!r}"
    )
    assert stats['suspect_events'] == 60, (
        "the events are still suspect-tier and still counted; got "
        f"{stats.get('suspect_events')!r}"
    )
    assert stats['distinct_targets'] == 0, (
        "an event with a NULL model_id targets nobody; got "
        f"{stats.get('distinct_targets')!r}"
    )


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: the attacker predicate only looks at the last 24 hours")
def test_predicate_window_boundary(opts):
    """90 days answered 'has anything ever gone wrong here'. On a shared egress
    that is always yes, and the enforcement it feeds lasts minutes."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(WINDOW_IP)

    _seed_events(WINDOW_IP, 30, level=5, category=SUSPECT_CATEGORY,
                 targets=10, age_hours=25)
    result = threat_intel.check_internal_threats(WINDOW_IP)

    assert result['is_known_attacker'] is False, (
        "events 25 hours old are outside the 24-hour predicate window; got "
        f"{result!r}"
    )
    assert result['internal_stats']['suspect_events'] == 0, (
        "nothing outside the window may be counted; got "
        f"{result['internal_stats']!r}"
    )
    assert result['internal_stats']['total_events'] == 30, (
        "the 90-day display stats must still see them; got "
        f"{result['internal_stats']!r}"
    )

    _seed_events(WINDOW_IP, 30, level=5, category=SUSPECT_CATEGORY,
                 targets=10, age_hours=1)
    result = threat_intel.check_internal_threats(WINDOW_IP)

    assert result['is_known_attacker'] is True, (
        f"the same 30 events one hour old are inside the window; got {result!r}"
    )


# ---------------------------------------------------------------------------
# Shared-egress suppressor
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: many distinct devices suppress the suspect tier")
def test_shared_egress_suppresses_suspect_tier(opts):
    """muid is a server-set HttpOnly cookie present pre-auth — JS cannot forge
    it. An address fronting 50 independent browsers is a NAT."""
    from mojo.helpers.geoip import threat_intel

    _seed_devices(EGRESS_IP, 50)
    _seed_events(EGRESS_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=10)

    result = threat_intel.check_internal_threats(EGRESS_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is False, (
        "an address fronting 50 pre-existing devices is a shared egress — "
        f"blocking it blocks everyone behind it; got {result!r}"
    )
    assert stats['shared_egress_suppressed'] is True, (
        f"the suppressor must record that it fired; got {stats!r}"
    )
    assert stats['distinct_devices'] == 50, (
        f"expected 50 distinct devices, got {stats.get('distinct_devices')!r}"
    )


@th.django_unit_test("threat intel: freshly minted muids cannot fake a shared egress")
def test_shared_egress_ignores_devices_born_inside_the_window(opts):
    """Without the first_seen hardening, cycling cookies defeats detection."""
    from mojo.helpers.geoip import threat_intel

    _seed_devices(EGRESS_IP, 50, device_age_hours=0)
    _seed_events(EGRESS_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=10)

    result = threat_intel.check_internal_threats(EGRESS_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is True, (
        "50 muids that all appeared inside the window are one attacker "
        f"cycling cookies, not 50 browsers; got {result!r}"
    )
    assert stats['distinct_devices'] == 0, (
        "only devices whose first_seen predates the window may be counted; got "
        f"{stats.get('distinct_devices')!r}"
    )


@th.django_unit_test("threat intel: no bouncer telemetry means the suppressor is skipped")
def test_no_bouncer_signals_skips_suppressor(opts):
    """A deployment that does not run the bouncer JS must not have every
    address read as 'zero devices, definitely one attacker' — nor as 'unknown,
    therefore suppressed'. The suppressor simply does not apply."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(EGRESS_IP)
    _seed_events(EGRESS_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=10)

    result = threat_intel.check_internal_threats(EGRESS_IP)
    stats = result['internal_stats']

    assert result['is_known_attacker'] is True, (
        "with no signals at all the suppressor must be skipped, leaving the "
        f"suspect verdict intact; got {result!r}"
    )
    assert stats['distinct_devices'] is None, (
        "'no telemetry' must be reported as None, never as 0 — 0 is an answer; "
        f"got {stats.get('distinct_devices')!r}"
    )


# ---------------------------------------------------------------------------
# Abuser
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: sustained rate-limit tripping flags a known abuser")
def test_internal_threats_flags_known_abuser(opts):
    """Each rate_limit:* event is deduped to one per key+IP per 60s in Redis,
    so 70 of them is over an hour of the day spent above the limits."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(ABUSER_IP)
    _seed_events(ABUSER_IP, 70, level=5, category="rate_limit:login")

    result = threat_intel.check_internal_threats(ABUSER_IP)
    stats = result['internal_stats']

    assert 'error' not in stats, (
        "check_internal_threats must complete without swallowing an exception; "
        f"got error={stats.get('error')!r}"
    )
    assert result['is_known_abuser'] is True, (
        f"70 rate_limit:* events in 24h must set is_known_abuser; got {result!r}"
    )
    assert stats['abuse_events'] == 70, (
        f"expected 70 abuse events, got {stats.get('abuse_events')!r}"
    )
    assert result['is_known_attacker'] is False, (
        "rate_limit:* is not on either attacker allowlist — being noisy is not "
        f"the same as attacking; got {result!r}"
    )


@th.django_unit_test("threat intel: ordinary API traffic is not abuse")
def test_ordinary_traffic_is_not_abuse(opts):
    """The old predicate ('>=10 events, mean level in [4,8), 90 days') counted
    'has this IP ever used the API'. Any busy client tripped it."""
    from mojo.helpers.geoip import threat_intel

    _clear_devices(NOT_ABUSER_IP)
    _seed_events(NOT_ABUSER_IP, 70, level=4, category="user_permission_denied")

    result = threat_intel.check_internal_threats(NOT_ABUSER_IP)

    assert result['is_known_abuser'] is False, (
        "70 permission-denied events are a misconfigured client, not abuse — "
        f"the abuser predicate is rate-limit tripping only; got {result!r}"
    )
    assert result['internal_stats']['abuse_events'] == 0, (
        "nothing outside the rate_limit: prefix may count toward abuse; got "
        f"{result['internal_stats']!r}"
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


# ---------------------------------------------------------------------------
# Tunability (thresholds must be reachable at runtime, not frozen at import)
# ---------------------------------------------------------------------------

# test_thresholds_are_read_at_call_time moved to
# tests/test_geoip_extended_serial/test_threat_intel.py (item #2558) — it
# mock.patches a threat_intel module attribute.


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


# test_deprecated_excluded_categories_still_honored moved to
# tests/test_geoip_extended_serial/test_threat_intel.py (item #2558) — it
# mock.patches a threat_intel module attribute.


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

# test_dry_run_returns_false_but_records_the_verdict moved to
# tests/test_geoip_extended_serial/test_threat_intel.py (item #2558) — it
# mock.patches a threat_intel module attribute.


# ---------------------------------------------------------------------------
# Event rate properties (rule-engine fields)
# ---------------------------------------------------------------------------

@th.django_unit_test("incident: Event exposes IP rate fields for rule matching")
def test_event_ip_rate_properties(opts):
    from mojo.apps.incident.models.event import Event

    _seed_devices(PROPS_IP, 4)
    _seed_events(PROPS_IP, 30, level=5, category=SUSPECT_CATEGORY, targets=10)

    event = Event.objects.filter(source_ip=PROPS_IP).first()

    assert event.ip_recent_attack_events == 30, (
        "ip_recent_attack_events must count the allowlisted events in the "
        f"window; got {event.ip_recent_attack_events!r}"
    )
    assert event.ip_recent_distinct_targets == 10, (
        "ip_recent_distinct_targets must count the distinct accounts hit; got "
        f"{event.ip_recent_distinct_targets!r}"
    )
    assert event.ip_recent_distinct_devices == 4, (
        "ip_recent_distinct_devices must count the pre-existing bouncer "
        f"devices; got {event.ip_recent_distinct_devices!r}"
    )

    # An event with no IP must not query anything.
    orphan = Event(source_ip=None, level=1, category="info")
    assert orphan.ip_recent_attack_events == 0, (
        f"an event with no source_ip scores 0; got {orphan.ip_recent_attack_events!r}"
    )

    # The memoization assertion (spying on threat_intel.ip_activity_stats)
    # moved to tests/test_geoip_extended_serial/test_threat_intel.py
    # (item #2558) — the spy is a mock.patch of the shared threat_intel module.


@th.django_unit_test("incident: a rule field name starting with _ is refused (guard)")
def test_rule_cannot_address_the_ip_stats_cache(opts):
    """The memo lives on Event._ip_stats. check_rule() refuses underscore-
    prefixed field names, so the cache is not itself a matchable field."""
    from mojo.apps.incident.models.event import Event
    from mojo.apps.incident.models.rule import Rule

    _seed_events(PROPS_IP, 1, level=5, category=SUSPECT_CATEGORY, targets=1)
    event = Event.objects.filter(source_ip=PROPS_IP).first()

    rule = Rule(field_name="_ip_stats", comparator="==", value="anything")
    assert rule.check_rule(event) is False, (
        "an underscore-prefixed field name must never match; got True"
    )


# ---------------------------------------------------------------------------
# Blocklist plumbing (#1079 regressions — unchanged by the retune)
# ---------------------------------------------------------------------------

@th.django_unit_test("threat intel: perform_threat_check exposes is_blocklisted in threat_data")
def test_perform_threat_check_exposes_blocklisted(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.helpers.geoip import threat_intel

    Event.objects.filter(source_ip=BLOCKLIST_IP).delete()

    # The blocklist hit rides in through perform_threat_check's check_external
    # seam (item #2558) instead of patching the shared threat_intel module.
    result = threat_intel.perform_threat_check(
        BLOCKLIST_IP, check_external=lambda ip: _blocklist_hit())

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


# test_geolocate_ip_escalates_threat_level_on_blocklist_hit moved to
# tests/test_geoip_extended_serial/test_threat_intel.py (item #2558) — it
# patches the shared geoip config/PROVIDERS/detection surfaces.


# test_geolocate_ip_check_threats_false_unchanged moved to
# tests/test_geoip_extended_serial/test_threat_intel.py (item #2558) — it
# patches the shared geoip config/PROVIDERS/detection surfaces.


@th.django_unit_test("geoip: GeoLocatedIP.check_threats raises threat_level on a blocklist hit")
def test_check_threats_raises_threat_level_on_blocklist_hit(opts):
    """End-to-end on the path production actually uses.

    `threat_analysis` and `refresh` REST actions land here. Before the fix,
    perform_threat_check() stored a threat_data blob with no is_blocklisted
    key, so recalculate_threat_level() scored 0 and the level stayed 'low'.
    """
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.incident.models.event import Event

    Event.objects.filter(source_ip=CHECK_THREATS_IP).delete()
    GeoLocatedIP.objects.filter(ip_address=CHECK_THREATS_IP).delete()
    geo = GeoLocatedIP.objects.create(
        ip_address=CHECK_THREATS_IP,
        provider="ipinfo",
        threat_level="low",
        data={},
    )

    # Injected through check_threats' check_external seam (item #2558) —
    # no patch of the shared threat_intel module.
    threat_results = geo.check_threats(
        check_external=lambda ip: _blocklist_hit())

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


@th.django_unit_test("geoip: skipping the external check must not erase a known blocklist hit")
def test_check_threats_skip_external_preserves_blocklist_flag(opts):
    """The daily decay pass re-scores on local evidence with skip_external=True.

    An unrun lookup is not a clean lookup — dropping the flag would both lose
    data and hand the address a free downgrade every night.
    """
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.apps.incident.models.event import Event

    Event.objects.filter(source_ip=CHECK_THREATS_IP).delete()
    GeoLocatedIP.objects.filter(ip_address=CHECK_THREATS_IP).delete()
    geo = GeoLocatedIP.objects.create(
        ip_address=CHECK_THREATS_IP,
        provider="ipinfo",
        threat_level="low",
        data={},
    )

    # Both external checks ride in through check_threats' check_external seam
    # (item #2558) instead of patching the shared threat_intel module.
    geo.check_threats(check_external=lambda ip: _blocklist_hit())
    assert geo.threat_level == 'medium', (
        f"precondition: the blocklist hit escalated the level; got {geo.threat_level!r}"
    )

    def must_not_run(ip):
        raise AssertionError("external check must be skipped")

    geo.check_threats(skip_external=True, check_external=must_not_run)

    assert geo.data['threat_data']['is_blocklisted'] is True, (
        "a recorded blocklist hit must survive a skip_external re-check; got "
        f"{geo.data.get('threat_data')!r}"
    )
    assert geo.threat_level == 'medium', (
        f"and so must the level it produced; got {geo.threat_level!r}"
    )
