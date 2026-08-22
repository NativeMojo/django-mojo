"""Unit coverage for `incident.report_event_suppressed` — the reusable
Redis-suppressed reporter introduced in #1098.

The helper is exercised DIRECTLY (not via `opts.client`), so these are true
in-process unit tests. The fail-closed test simulates the Redis outage by
injecting a test-owned failing client through the keyword-only `connection=`
seam on `report_event_suppressed` (item #2558) — no rebinding of
`mojo.helpers.redis.get_connection`, which every parallel test thread in the
process reads. A throwaway category keeps the assertions isolated from real
incident traffic; the DB and Redis are long-lived, so every test clears its own
events and Redis keys before it runs.
"""
from testit import helpers as th

CATEGORY = "test:suppression_probe"
WINDOW = 3600


def _clear(keys):
    """Delete this category's events and the given Redis keys (best-effort)."""
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    Event.objects.filter(category=CATEGORY).delete()
    try:
        redis = get_connection()
    except Exception:
        return
    for k in keys:
        try:
            redis.delete(k)
        except Exception:
            pass


@th.django_unit_setup()
def setup_incident_suppression(opts):
    from mojo.apps.incident import budget_key

    # Broad initial sweep of the throwaway category + its budget bucket. Each
    # test additionally clears the specific notice keys it will use.
    _clear([budget_key(CATEGORY, WINDOW)])


@th.django_unit_test("suppressed report files once per key per window")
def test_suppressed_report_files_once_per_key_per_window(opts):
    from mojo.apps import incident
    from mojo.apps.incident import notice_key
    from mojo.apps.incident.models import Event

    key_a = "probe-once-a"
    key_b = "probe-once-b"
    _clear([notice_key(CATEGORY, key_a), notice_key(CATEGORY, key_b)])

    first = incident.report_event_suppressed(
        "first for A", key=key_a, category=CATEGORY, level=1)
    assert first is True, (
        f"the first report for a key must file and return True, got {first!r}")

    second = incident.report_event_suppressed(
        "second for A", key=key_a, category=CATEGORY, level=1)
    assert second is False, (
        f"a second report for the same key inside the window must be suppressed "
        f"and return False, got {second!r}")
    assert Event.objects.filter(category=CATEGORY).count() == 1, (
        "two reports for the same key must file exactly one event")

    third = incident.report_event_suppressed(
        "first for B", key=key_b, category=CATEGORY, level=1)
    assert third is True, (
        f"a report for a DIFFERENT key must file its own event, got {third!r}")
    assert Event.objects.filter(category=CATEGORY).count() == 2, (
        "a distinct key files its own event — expected two events total")

    _clear([notice_key(CATEGORY, key_a), notice_key(CATEGORY, key_b)])


@th.django_unit_test("suppression budget caps distinct keys and files a marker")
def test_budget_caps_distinct_keys_and_says_so(opts):
    from mojo.apps import incident
    from mojo.apps.incident import notice_key, budget_key
    from mojo.apps.incident.models import Event

    keys = [f"probe-budget-{i}" for i in range(4)]
    _clear([notice_key(CATEGORY, k) for k in keys] + [budget_key(CATEGORY, WINDOW)])

    results = [
        incident.report_event_suppressed(
            f"budget probe {k}", key=k, category=CATEGORY, level=1, budget=2)
        for k in keys]

    assert results[:2] == [True, True], (
        f"the first two distinct keys are within budget and must file, got "
        f"{results[:2]!r}")
    assert results[2] is False and results[3] is False, (
        f"distinct keys past the budget must be dropped and return False, got "
        f"{results[2:]!r}")

    events = list(Event.objects.filter(category=CATEGORY))
    assert len(events) == 3, (
        f"budget=2 over four distinct keys must file exactly three events — two "
        f"real plus one budget-exhausted marker — got {len(events)}")
    markers = [e for e in events if e.level == 4]
    assert len(markers) == 1, (
        f"exactly one level-4 budget-exhausted marker must be filed, got "
        f"{len(markers)}")
    assert "budget" in (markers[0].details or "").lower(), (
        f"the marker must SAY the budget was exhausted, got {markers[0].details!r}")

    _clear([notice_key(CATEGORY, k) for k in keys] + [budget_key(CATEGORY, WINDOW)])


@th.django_unit_test("suppressed report never raises")
def test_suppressed_report_never_raises(opts):
    from mojo.apps import incident
    from mojo.apps.incident import notice_key
    from mojo.apps.incident.models import Event

    key = "probe-raise"
    _clear([notice_key(CATEGORY, key)])

    # A truthy non-request object forces `report_event` to raise while building
    # the event dict (it reads `request.ip`), BEFORE any DB write — so this
    # exercises the report-stage except path without poisoning the DB connection.
    try:
        result = incident.report_event_suppressed(
            "forced failure", key=key, category=CATEGORY, level=1,
            request=object())
    except Exception as exc:
        assert False, (
            f"report_event_suppressed must never raise, but it raised {exc!r}")
    assert result is False, (
        f"a report that fails to file must return False, got {result!r}")
    assert Event.objects.filter(category=CATEGORY).count() == 0, (
        "a forced failure must not leave a partial event row behind")

    _clear([notice_key(CATEGORY, key)])


class _DownConnection:
    """A Redis-like client whose every round-trip fails, as during an outage.

    Injected through the keyword-only ``connection=`` seam on
    ``report_event_suppressed`` (item #2558) so the outage is simulated with a
    test-owned object instead of rebinding ``mojo.helpers.redis.get_connection``
    for every parallel test thread in the process.
    """

    def set(self, *args, **kwargs):
        raise RuntimeError("redis is down")

    def incr(self, *args, **kwargs):
        raise RuntimeError("redis is down")

    def expire(self, *args, **kwargs):
        raise RuntimeError("redis is down")


@th.django_unit_test("fail-closed drops the event when redis is unreachable")
def test_fail_closed_skips_report_when_redis_down(opts):
    from mojo.apps import incident
    from mojo.apps.incident import notice_key
    from mojo.apps.incident.models import Event

    key = "probe-failclosed"
    _clear([notice_key(CATEGORY, key)])

    down = _DownConnection()

    closed = incident.report_event_suppressed(
        "fail closed", key=key, category=CATEGORY, level=1, fail_open=False,
        connection=down)
    assert closed is False, (
        f"with redis down and fail_open=False the event must be DROPPED and "
        f"return False, got {closed!r}")
    assert Event.objects.filter(category=CATEGORY).count() == 0, (
        "fail-closed must not file when redis is unreachable — a public, "
        "amplifiable category must not become an open floodgate on an outage")

    opened = incident.report_event_suppressed(
        "fail open", key=key, category=CATEGORY, level=1, fail_open=True,
        connection=down)
    assert Event.objects.filter(category=CATEGORY).count() == 1, (
        "fail-open must still file the event when redis is unreachable "
        "(logged once, unsuppressed)")
    assert opened is True, (
        f"fail-open must return True when the event is filed, got {opened!r}")

    _clear([notice_key(CATEGORY, key)])
