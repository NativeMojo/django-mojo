"""ApiKey behavior for ordinary and strict endpoint decorators."""
import uuid as _uuid

from testit import helpers as th


class _Anonymous:
    is_authenticated = False


def _request(marker, ip="198.51.100.45", limits=None, group=None):
    class _ApiKey:
        pk = marker
        pass

    api_key = _ApiKey()
    api_key.limits = limits or {}
    api_key.group = group

    class _Request:
        user = _Anonymous()
        DATA = {}
        muid = None
        path = "/api/limits-regression"
        method = "GET"
        bearer = None
        headers = {}
        META = {"REMOTE_ADDR": ip}

    request = _Request()
    request.api_key = api_key
    request.group = group
    request.ip = ip
    return request


def _clean(key, marker):
    from mojo.apps.incident.models import Event
    from mojo.decorators.limits import clear_rate_limits
    from mojo.helpers.redis import get_connection

    clear_rate_limits(apikey_id=marker)
    r = get_connection()
    for redis_key in r.scan_iter(f"rl:{key}:*"):
        r.delete(redis_key)
    for redis_key in r.scan_iter(f"srl:{key}:*"):
        r.delete(redis_key)
    Event.objects.filter(
        category="traffic:apikey_threshold", model_id=marker).delete()


def _limit_wrapper(func, expected_key):
    import inspect

    current = func
    while current is not None:
        closure = inspect.getclosurevars(current).nonlocals
        if closure.get("key") == expected_key:
            return current, closure
        current = getattr(current, "__wrapped__", None)
    return None, None


@th.django_unit_test()
def test_observation_option_preserves_legacy_positional_signature(opts):
    import inspect
    from mojo.decorators.limits import rate_limit

    names = list(inspect.signature(rate_limit).parameters)
    legacy_prefix = [
        "key", "ip_limit", "duid_limit", "muid_limit", "apikey_limit",
        "ip_window", "duid_window", "muid_window", "apikey_window",
        "min_granularity",
    ]
    assert names[:len(legacy_prefix)] == legacy_prefix, (
        "new options must only APPEND so existing positional ip/window "
        f"arguments keep their meaning; signature parameters were {names}"
    )
    assert "apikey_observe_limit" in names[len(legacy_prefix):], (
        "apikey_observe_limit must stay behind the legacy positional prefix; "
        f"signature parameters were {names}"
    )


@th.django_unit_test()
def test_ordinary_rate_limit_passes_apikey_by_default(opts):
    from mojo.apps.incident.models import Event
    from mojo.decorators.limits import rate_limit

    marker = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    key = f"apikey_pass_{_uuid.uuid4().hex[:10]}"
    request = _request(marker)
    _clean(key, marker)

    @rate_limit(key, ip_limit=1)
    def endpoint(req):
        return "allowed"

    try:
        results = [endpoint(request) for _ in range(5)]
        assert results == ["allowed"] * 5, (
            "ordinary rate_limit must shadow its consumer threshold for "
            f"ApiKeys instead of returning 429, got {results!r}"
        )

        events = Event.objects.filter(
            category="traffic:apikey_threshold", model_id=marker)
        assert events.count() == 1, (
            "crossing an ordinary endpoint threshold repeatedly must create "
            f"one bounded observation event, got {events.count()}"
        )
        event = events.first()
        assert event.model_name == "traffic:apikey"
        assert event.metadata.get("identity") == f"apikey:{marker}"
        assert event.metadata.get("source") == f"endpoint:{key}"
        assert event.metadata.get("threshold") == 1
        assert "token" not in event.metadata and "bearer" not in event.metadata, (
            f"observation evidence must not include credentials: {event.metadata}"
        )
    finally:
        _clean(key, marker)


@th.django_unit_test()
def test_ordinary_rate_limit_supports_custom_observation_threshold(opts):
    from mojo.apps.incident.models import Event
    from mojo.decorators.limits import rate_limit

    marker = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    key = f"apikey_observe_{_uuid.uuid4().hex[:10]}"
    request = _request(marker)
    _clean(key, marker)

    @rate_limit(key, ip_limit=50, apikey_observe_limit=2, apikey_window=60)
    def endpoint(req):
        return "allowed"

    try:
        assert [endpoint(request) for _ in range(4)] == ["allowed"] * 4
        event = Event.objects.get(
            category="traffic:apikey_threshold", model_id=marker)
        assert event.metadata.get("threshold") == 2
        assert event.metadata.get("threshold_window") == 60
    finally:
        _clean(key, marker)


@th.django_unit_test()
def test_ordinary_rate_limit_keeps_explicit_per_key_limit_hard(opts):
    from mojo.decorators.limits import rate_limit

    marker = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    key = f"apikey_explicit_{_uuid.uuid4().hex[:10]}"
    request = _request(marker, limits={key: {"limit": 1, "window": 1}})
    _clean(key, marker)

    @rate_limit(key, ip_limit=50)
    def endpoint(req):
        return "allowed"

    try:
        assert endpoint(request) == "allowed"
        blocked = endpoint(request)
        assert getattr(blocked, "status_code", None) == 429, blocked
    finally:
        _clean(key, marker)


@th.django_unit_test()
def test_ordinary_rate_limit_keeps_developer_apikey_limit_hard(opts):
    from mojo.decorators.limits import rate_limit

    marker = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    key = f"apikey_developer_{_uuid.uuid4().hex[:10]}"
    request = _request(marker)
    _clean(key, marker)

    @rate_limit(key, ip_limit=50, apikey_limit=1)
    def endpoint(req):
        return "allowed"

    try:
        assert endpoint(request) == "allowed"
        blocked = endpoint(request)
        assert getattr(blocked, "status_code", None) == 429, blocked
    finally:
        _clean(key, marker)


@th.django_unit_test()
def test_apikey_hard_buckets_are_isolated_by_key_not_group(opts):
    from mojo.decorators.limits import rate_limit

    class _Group:
        pk = 123456

    key = f"apikey_isolation_{_uuid.uuid4().hex[:10]}"
    marker1 = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    marker2 = 880_000_000 + int(_uuid.uuid4().int % 10_000_000)
    request1 = _request(marker1, group=_Group())
    request2 = _request(marker2, group=_Group())
    _clean(key, marker1)
    _clean(key, marker2)

    @rate_limit(key, ip_limit=50, apikey_limit=1)
    def endpoint(req):
        return "allowed"

    try:
        assert endpoint(request1) == "allowed"
        assert endpoint(request2) == "allowed", (
            "two ApiKeys in one group must not share a hard-limit bucket"
        )
        assert getattr(endpoint(request1), "status_code", None) == 429
    finally:
        _clean(key, marker1)
        _clean(key, marker2)


@th.django_unit_test()
def test_malformed_or_nonpositive_per_key_limit_fails_open(opts):
    from mojo.decorators.limits import rate_limit

    for bad_value in (0, -1, "bad"):
        marker = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
        key = f"apikey_invalid_{_uuid.uuid4().hex[:10]}"
        request = _request(marker, limits={key: {"limit": bad_value}})
        _clean(key, marker)

        @rate_limit(key, ip_limit=1)
        def endpoint(req):
            return "allowed"

        try:
            assert [endpoint(request) for _ in range(3)] == ["allowed"] * 3
        finally:
            _clean(key, marker)


@th.django_unit_test()
def test_strict_rate_limit_remains_hard_for_apikey(opts):
    from mojo.decorators.limits import strict_rate_limit

    marker = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    key = f"apikey_strict_{_uuid.uuid4().hex[:10]}"
    request = _request(marker)
    _clean(key, marker)

    @strict_rate_limit(key, ip_limit=1)
    def endpoint(req):
        return "allowed"

    try:
        assert endpoint(request) == "allowed"
        blocked = endpoint(request)
        assert getattr(blocked, "status_code", None) == 429, blocked
    finally:
        _clean(key, marker)


@th.django_unit_test()
def test_strict_per_key_limit_overrides_developer_fallback(opts):
    from mojo.decorators.limits import strict_rate_limit

    marker = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    key = f"apikey_strict_override_{_uuid.uuid4().hex[:10]}"
    request = _request(marker, limits={key: {"limit": 1, "window": 1}})
    _clean(key, marker)

    @strict_rate_limit(key, ip_limit=100, apikey_limit=50)
    def endpoint(req):
        return "allowed"

    try:
        assert endpoint(request) == "allowed"
        blocked = endpoint(request)
        assert getattr(blocked, "status_code", None) == 429, (
            "the explicit per-key limit of 1 must override developer fallback 50"
        )
    finally:
        _clean(key, marker)


@th.django_unit_test()
def test_incident_event_route_is_strict_for_apikey(opts):
    from mojo.apps.incident.rest import event as event_rest

    wrapper, closure = _limit_wrapper(event_rest.on_event, "incident_event")
    assert wrapper is not None, "/api/event is missing its incident_event limiter"
    assert "_check_sliding" in wrapper.__code__.co_names, (
        "/api/event must use strict_rate_limit, not the ordinary ApiKey pass"
    )
    assert closure.get("ip_limit") == 240
    assert closure.get("muid_limit") == 120


@th.django_unit_test()
def test_audited_safety_routes_use_strict_limits(opts):
    from mojo.apps.account.rest import device, oauth_server, user
    from mojo.apps.account.rest.bouncer import assess, event as bouncer_event
    from mojo.apps.docit.rest import search
    from mojo.apps.fileman.rest import qrcode
    from mojo.apps.incident.rest import maestro_webhook

    routes = (
        (qrcode.on_qrcode, "qrcode"),
        (qrcode.on_qrcode_vcard, "qrcode_vcard"),
        (oauth_server.on_authorize, "oauth_authorize"),
        (oauth_server.on_token, "oauth_token"),
        (oauth_server.on_revoke, "oauth_revoke"),
        (device.on_geo_located_ip_lookup, "geoip_lookup"),
        (device.on_geo_located_ip_sync, "geoip_sync"),
        (user.on_refresh_token, "refresh_token"),
        (user.on_auth_handoff, "auth_handoff"),
        (user.on_sessions_revoke, "sessions_revoke"),
        (user.on_account_deactivate, "account_deactivate"),
        (assess.on_bouncer_assess, "bouncer_assess"),
        (bouncer_event.on_bouncer_event, "bouncer_event"),
        (maestro_webhook.on_maestro_webhook, "maestro_webhook"),
        (search.on_search, "docit_search"),
    )

    for func, key in routes:
        wrapper, _ = _limit_wrapper(func, key)
        assert wrapper is not None, f"{func.__module__}.{func.__name__} is missing {key}"
        assert "_check_sliding" in wrapper.__code__.co_names, (
            f"{func.__module__}.{func.__name__} must remain strict for ApiKeys"
        )


@th.django_unit_test()
def test_observation_events_share_a_global_distinct_key_budget(opts):
    from mojo.decorators import limits
    from mojo.helpers.redis import get_connection

    marker1 = 870_000_000 + int(_uuid.uuid4().int % 10_000_000)
    marker2 = 880_000_000 + int(_uuid.uuid4().int % 10_000_000)
    source = f"endpoint:budget_{_uuid.uuid4().hex[:10]}"
    window = 3600
    r = get_connection()
    calls = []

    def _capture(*args, **kwargs):
        calls.append((args, kwargs))
        return True

    for marker in (marker1, marker2):
        limits._report_apikey_threshold(
            _request(marker), r, marker, source, 2, 1, 60,
            event_window=window, event_budget=1, report=_capture)

    assert len(calls) == 2
    assert {call[1]["key"] for call in calls} == {
        f"{marker1}:{source}", f"{marker2}:{source}",
    }
    assert all(call[1]["category"] == "traffic:apikey_threshold" for call in calls)
    assert all(call[1]["budget"] == 1 for call in calls), (
        "every distinct ApiKey/source must enter the reporter's shared global budget"
    )
    assert all(call[1]["fail_open"] is False for call in calls), (
        "the attacker-amplifiable observation path must fail closed on reporter failure"
    )
