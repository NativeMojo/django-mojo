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


@th.django_unit_test()
def test_observation_option_preserves_legacy_positional_signature(opts):
    import inspect
    from mojo.decorators.limits import rate_limit

    names = list(inspect.signature(rate_limit).parameters)
    assert names[-1] == "apikey_observe_limit", (
        "the new option must remain last so existing positional ip/window "
        f"arguments keep their meaning; signature parameters were {names}"
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
    import inspect
    from mojo.apps.incident.rest import event as event_rest

    current = event_rest.on_event
    found = None
    while current is not None:
        closure = inspect.getclosurevars(current).nonlocals
        if closure.get("key") == "incident_event":
            found = (current, closure)
            break
        current = getattr(current, "__wrapped__", None)

    assert found is not None, "/api/event is missing its incident_event limiter"
    wrapper, closure = found
    assert "_check_sliding" in wrapper.__code__.co_names, (
        "/api/event must use strict_rate_limit, not the ordinary ApiKey pass"
    )
    assert closure.get("ip_limit") == 240
    assert closure.get("muid_limit") == 120


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
