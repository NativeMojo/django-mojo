"""DM-042: traffic-concentration detector (incident cron job).

Drives run_concentration_check() with isolated Redis accounting keys and also
exercises those keys end to end through check_api_throttle(). A synthetic time
base far from the real clock keeps parallel test modules from colliding.
"""
import time
import uuid as _uuid

from testit import helpers as th

TESTIT_TIER = "extended"

BUCKET = 300


@th.django_unit_setup()
def setup_concentration(opts):
    # Synthetic "now", bucket-aligned, ~2 years in the past — unique key space
    # per run via a random bucket offset.
    base = (int(time.time()) - 63_072_000) // BUCKET * BUCKET
    opts.now = base - (int(_uuid.uuid4().int % 10_000) * BUCKET)
    opts.b1 = opts.now // BUCKET * BUCKET - BUCKET       # newest complete bucket
    opts.b2 = opts.b1 - BUCKET
    opts.user_pk = 990_000_000 + int(_uuid.uuid4().int % 1_000_000)
    opts.member = f"user:{opts.user_pk}"


def _clean(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.incident.models import Event
    r = get_connection()
    r.delete(f"traffic:top:{opts.b1}", f"traffic:top:{opts.b2}",
             f"traffic:total:{opts.b1}", f"traffic:alerted:{opts.member}")
    Event.objects.filter(category="traffic:concentration",
                         details__contains=opts.member).delete()


@th.django_unit_test()
def test_sustained_offender_alerts_once(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.incident.asyncjobs import run_concentration_check
    from mojo.apps.incident.models import Event

    _clean(opts)
    r = get_connection()
    # 150 rpm in both complete buckets (threshold default 120 sustained x2).
    r.zadd(f"traffic:top:{opts.b1}", {opts.member: 750, "ip:203.0.113.9": 750})
    r.zadd(f"traffic:top:{opts.b2}", {opts.member: 750})
    r.set(f"traffic:total:{opts.b1}", 800)

    alerts = run_concentration_check(now=opts.now)
    ours = [a for a in alerts if a["identity"] == opts.member]
    assert len(ours) == 1, f"sustained 150rpm identity must alert exactly once, got {alerts}"

    events = Event.objects.filter(category="traffic:concentration",
                                  details__contains=opts.member)
    assert events.count() == 1, (
        f"expected 1 concentration event for {opts.member}, got {events.count()}"
    )
    event = events.first()
    assert event.metadata.get("top_ips") == ["ip:203.0.113.9"], (
        f"event metadata should carry the bucket's top IPs, got {event.metadata.get('top_ips')}"
    )

    # Second run inside the dedup hour: no new alert, no new event.
    alerts = run_concentration_check(now=opts.now)
    ours = [a for a in alerts if a["identity"] == opts.member]
    assert not ours, f"identity is deduped for an hour — second run must not re-alert, got {ours}"
    assert Event.objects.filter(category="traffic:concentration",
                                details__contains=opts.member).count() == 1, \
        "second run must not create another event inside the dedup window"
    _clean(opts)


@th.django_unit_test()
def test_unlimited_apikey_accounting_reaches_concentration_detector(opts):
    """A quiet-stop ApiKey burst must be visible without a later request to
    flush an enforcement window."""
    from mojo.apps.incident.asyncjobs import run_concentration_check
    from mojo.apps.incident.models import Event
    from mojo.decorators import limits
    from mojo.helpers.redis import get_connection

    marker = 860_000_000 + int(_uuid.uuid4().int % 10_000_000)
    member = f"apikey:{marker}"
    ip_member = "ip:203.0.113.88"

    class _ApiKey:
        pk = marker
        limits = {}
        group = None

    class _Anonymous:
        is_authenticated = False

    class _Request:
        api_key = _ApiKey()
        user = _Anonymous()
        group = None
        path = "/api/concentration-regression"
        method = "GET"
        ip = "203.0.113.88"
        bearer = None
        headers = {}
        META = {"REMOTE_ADDR": ip}

    r = get_connection()
    cleanup_keys = (
        f"traffic:top:{opts.b1}", f"traffic:top:{opts.b2}",
        f"traffic:total:{opts.b1}", f"traffic:total:{opts.b2}",
        f"traffic:alerted:{member}",
    )
    r.delete(*cleanup_keys)
    Event.objects.filter(
        category="traffic:concentration", model_id=marker).delete()

    config = {
        "enabled": True,
        "user_limit": 240,
        "apikey_limit": 0,
        "apikey_observe_limit": 100_000,
        "window": 60,
        "exempt_prefixes": [],
        "report_floor": 60,
        "config_ttl": 30,
    }
    try:
        for bucket in (opts.b2, opts.b1):
            for _ in range(750):
                blocked = limits.check_api_throttle(
                    _Request(), now=bucket + 1, config=config)
                assert blocked is None

        assert float(r.zscore(f"traffic:top:{opts.b1}", member) or 0) == 750
        assert float(r.zscore(f"traffic:top:{opts.b2}", member) or 0) == 750
        assert float(r.zscore(f"traffic:top:{opts.b1}", ip_member) or 0) == 750

        alerts = run_concentration_check(now=opts.now)
        ours = [alert for alert in alerts if alert["identity"] == member]
        assert len(ours) == 1, (
            "unlimited ApiKey traffic written by check_api_throttle must reach "
            f"the sustained-concentration alert, got {alerts}"
        )
        event = Event.objects.get(
            category="traffic:concentration", model_id=marker)
        assert event.model_name == "traffic:apikey"
        assert event.metadata.get("identity") == member
        assert event.metadata.get("top_ips") == [ip_member]
    finally:
        limits.clear_rate_limits(apikey_id=marker)
        r.delete(*cleanup_keys)
        Event.objects.filter(
            category="traffic:concentration", model_id=marker).delete()


@th.django_unit_test()
def test_single_bucket_spike_does_not_alert(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.incident.asyncjobs import run_concentration_check

    _clean(opts)
    r = get_connection()
    # 150 rpm in the newest bucket only — not sustained; share below threshold.
    r.zadd(f"traffic:top:{opts.b1}", {opts.member: 750})
    r.set(f"traffic:total:{opts.b1}", 100_000)

    alerts = run_concentration_check(now=opts.now)
    ours = [a for a in alerts if a["identity"] == opts.member]
    assert not ours, f"one-bucket spike below share threshold must not alert, got {ours}"
    _clean(opts)


@th.django_unit_test()
def test_share_trigger_requires_min_total(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.incident.asyncjobs import run_concentration_check

    _clean(opts)
    r = get_connection()
    # 60 rpm (below the rpm threshold) but 60% of the bucket's traffic.
    r.zadd(f"traffic:top:{opts.b1}", {opts.member: 300})

    # Below the floor: one user being most of a quiet box's traffic is normal.
    r.set(f"traffic:total:{opts.b1}", 500)
    alerts = run_concentration_check(now=opts.now)
    ours = [a for a in alerts if a["identity"] == opts.member]
    assert not ours, f"share trigger must respect the MIN_TOTAL floor, got {ours}"

    # At the floor: 300/1000 = 30% share >= 20% default -> alert.
    r.set(f"traffic:total:{opts.b1}", 1000)
    alerts = run_concentration_check(now=opts.now)
    ours = [a for a in alerts if a["identity"] == opts.member]
    assert len(ours) == 1, (
        f"30% share of 1000 requests must alert via the share trigger, got {alerts}"
    )
    _clean(opts)


@th.django_unit_test()
def test_ip_members_never_alert(opts):
    from mojo.helpers.redis import get_connection
    from mojo.apps.incident.asyncjobs import run_concentration_check

    _clean(opts)
    r = get_connection()
    ip_member = "ip:198.51.100.77"
    r.delete(f"traffic:alerted:{ip_member}")
    r.zadd(f"traffic:top:{opts.b1}", {ip_member: 5000})
    r.zadd(f"traffic:top:{opts.b2}", {ip_member: 5000})
    r.set(f"traffic:total:{opts.b1}", 5000)

    alerts = run_concentration_check(now=opts.now)
    assert not [a for a in alerts if a["identity"].startswith("ip:")], (
        f"ip: members are informational and must never be the alert unit, got {alerts}"
    )
    _clean(opts)


@th.django_unit_test()
def test_traffic_ruleset_bootstrapped(opts):
    from mojo.apps.incident.models import RuleSet
    rs = RuleSet.objects.filter(category="traffic:concentration").first()
    assert rs is not None, (
        "run_concentration_check must bootstrap the traffic:concentration ruleset"
    )
    assert "notify://" in (rs.handler or ""), (
        f"traffic ruleset must notify security staff, got handler {rs.handler!r}"
    )
    assert "block://" not in (rs.handler or ""), (
        "traffic ruleset must NEVER IP-block — authenticated abusers have valid "
        "credentials and CGNAT collateral is the report's own warning"
    )
