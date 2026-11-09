"""#4125: the crash-report route's rate limit and repeat suppression."""
import uuid as _uuid

from testit import helpers as th

ROUTE = "/api/incident/event/client"


def _clear():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="incident_client_report")


def _events(category):
    from mojo.apps.incident.models.event import Event
    return Event.objects.filter(category=category).count()


@th.django_unit_setup()
def setup_client_limits(opts):
    opts.tag = _uuid.uuid4().hex[:10]
    _clear()


@th.django_unit_test("4125 T12: the 61st report from one IP in a minute is refused")
def test_client_rate_limit(opts):
    opts.client.logout()
    _clear()
    statuses = []
    try:
        for index in range(61):
            # A cross-origin browser sends no cookie, so the per-IP limit is
            # the one that binds; drop the client's muid cookie each time.
            opts.client.session.cookies.clear()
            resp = opts.client.post(ROUTE, {
                "category": f"frontend:testit_{opts.tag}_t12", "message": f"limit {index}"})
            statuses.append(resp.status_code)
    finally:
        _clear()
    assert all(code == 200 for code in statuses[:60]), \
        f"the first 60 reports must be accepted, got {sorted(set(statuses[:60]))}"
    assert statuses[60] == 429, f"the 61st report must be 429, got {statuses[60]}"


@th.django_unit_test("4125 T12b: malformed-group reports count against the limit and file nothing")
def test_malformed_group_rate_limited(opts):
    from mojo.apps.incident.models.event import Event
    opts.client.logout()
    _clear()
    marker = f"cr4125t12b{opts.tag}"
    url = f"{ROUTE}?m={marker}&group=not-an-integer"
    body = {"category": f"frontend:testit_{opts.tag}_t12b", "message": "bad group"}
    statuses = []
    try:
        opts.client.session.cookies.clear()
        first = opts.client.post(url, body)
        filed = Event.objects.filter(metadata__http_query_string__icontains=marker).count()
        # Checked before the loop so a broken route files one event, not 61.
        assert filed == 0, f"a malformed group filed {filed} event(s) on the first request"
        statuses.append(first.status_code)
        for _ in range(60):
            opts.client.session.cookies.clear()
            statuses.append(opts.client.post(url, body).status_code)
    finally:
        _clear()
    assert all(code == 400 for code in statuses[:60]), \
        f"the first 60 malformed reports must be refused with 400, got {sorted(set(statuses[:60]))}"
    assert statuses[60] == 429, f"the 61st malformed report must hit the route limit (429), got {statuses[60]}"
    filed = Event.objects.filter(metadata__http_query_string__icontains=marker).count()
    assert filed == 0, f"malformed-group reports filed {filed} event(s)"


@th.django_unit_test("4125 T12c: an overflowing group number counts against the limit and files nothing")
def test_overflowing_group_rate_limited(opts):
    from mojo.apps.incident.models.event import Event
    opts.client.logout()
    _clear()
    marker = f"cr4125t12c{opts.tag}"
    url = f"{opts.client.host}{ROUTE.lstrip('/')}"
    raw = '{"category": "frontend:testit_%s_t12c", "message": "big group", "group": 1e309}' % opts.tag
    headers = {"Content-Type": "application/json"}
    statuses = []
    try:
        for _ in range(61):
            opts.client.session.cookies.clear()
            statuses.append(opts.client.session.post(
                url, params={"m": marker}, data=raw, headers=headers).status_code)
    finally:
        _clear()
    assert all(code == 400 for code in statuses[:60]), \
        f"the first 60 overflowing reports must be refused with 400, got {sorted(set(statuses[:60]))}"
    assert statuses[60] == 429, f"the 61st overflowing report must hit the route limit (429), got {statuses[60]}"
    filed = Event.objects.filter(metadata__http_query_string__icontains=marker).count()
    assert filed == 0, f"overflowing-group reports filed {filed} event(s)"


@th.django_unit_test("4125 T13: the same report twice files one event and two identical receipts")
def test_repeat_suppressed_same_receipt(opts):
    opts.client.logout()
    _clear()
    category = f"frontend:testit_{opts.tag}_t13"
    body = {"category": category, "message": "same crash",
            "metadata": {"stack_trace": "Error: same\n  at f (a.js:1:1)"}}
    opts.client.session.cookies.clear()
    try:
        first = opts.client.post(ROUTE, body)
        second = opts.client.post(ROUTE, body)
    finally:
        _clear()
    assert first.status_code == 200 and second.status_code == 200, \
        f"both must be accepted, got {first.status_code} and {second.status_code}"
    receipts = [{k: v for k, v in r.response.items() if k != "server"} for r in (first, second)]
    assert receipts[0] == receipts[1] and receipts[0].get("status") is True and "data" not in receipts[0], \
        f"receipts must be identical status true with no data: {first.response} / {second.response}"
    assert _events(category) == 1, f"a repeated report must file once, got {_events(category)}"


class _DownRedis:
    def set(self, *args, **kwargs):
        raise ConnectionError("redis down (test seam)")

    def incr(self, *args, **kwargs):
        raise ConnectionError("redis down (test seam)")

    def expire(self, *args, **kwargs):
        raise ConnectionError("redis down (test seam)")


@th.django_unit_test("4125 T14: suppression fails closed when Redis is down")
def test_suppression_fails_closed(opts):
    from objict import objict
    from mojo.apps.incident.services import client_report
    category = f"frontend:testit_{opts.tag}_t14"
    request = objict(
        DATA=objict(category=category, message="redis down"),
        META={"CONTENT_LENGTH": "64", "QUERY_STRING": "", "SERVER_PROTOCOL": "HTTP/1.1"},
        ip="127.0.0.1", path=ROUTE, method="POST", bearer=None, group=None,
        user=objict(is_authenticated=False, pk=None, id=None))
    resp = client_report.handle(request, connection=_DownRedis())
    assert resp.status_code == 200, f"a dropped report still gets the receipt, got {resp.status_code}"
    assert _events(category) == 0, "a report was filed while suppression could not run"
