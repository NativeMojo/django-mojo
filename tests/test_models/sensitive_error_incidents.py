"""Maestro item #5960 — error incidents on a host app's listed sensitive paths.

A failed REST request copies `request.DATA`, the query string and the
exception text into the Event (and, at level >= 7, the Incident). For a path a
host app lists in MOJO_SENSITIVE_BODY_PATHS none of that may be stored: the
body becomes {"sensitive_body": "host_sensitive"}, the query string is dropped,
and the exception is recorded by type and stack frames only. Unlisted paths and
framework-labelled paths store exactly what they stored before.

Fake requests as in return_real_error.py, with `_host_sensitive` set directly
so no setting is changed; the settings-driven matching is covered in
tests/test_account_admin_extended_serial/test_sensitive_body_paths.py.
"""
import json
import uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

MARKER = {"sensitive_body": "host_sensitive"}


@th.django_unit_setup()
def setup_sensitive_error_incidents(opts):
    pass


def _values():
    token = uuid.uuid4().hex[:12]
    return {
        "email": f"payer-{token}@example.com",
        "phone": f"+1555{token[:7]}",
        "payerId": f"PAYER{token.upper()}",
    }


def _request(path, values, host_sensitive):
    import objict
    req = objict.objict()
    req.user = objict.objict()
    req.user.is_authenticated = False
    req.user.id = None
    req.DATA = objict.objict(values)
    req.QUERY_PARAMS = objict.objict()
    req.method = "POST"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = path
    req.META = {"QUERY_STRING": f"email={values['email']}&phone={values['phone']}"}
    req.api_key = None
    req._host_sensitive = host_sensitive
    return req


def _invoke(req, exc):
    from mojo.decorators import http as http_decorators

    def fake_handler(request):
        raise exc

    return http_decorators.dispatch_error_handler(fake_handler)(req)


# A routine 4xx queues its Event to the jobs app instead of writing it on the
# request thread (#6565); the 500 paths still write inline. The facts — and so
# the masking this module asserts — are captured on the request thread either
# way. Run the anonymous queued writes of each 4xx category before reading.
QUEUED_CATEGORIES = ("api_denied", "mojo_rest_error", "rest_value_error")


def _events(path):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.reporter import QUEUED_EVENT_JOB
    for category in QUEUED_CATEGORIES:
        th.run_pending_jobs(channel="incident_handlers", func=QUEUED_EVENT_JOB,
                            payload={"category": category, "uid": None})
    return list(Event.objects.filter(metadata__http_path=path).order_by("pk"))


def _leaks(record, values):
    text = json.dumps(record.metadata, default=str) + " " + str(record.details or "")
    return [v for v in values.values() if v in text]


@th.django_unit_test("host-sensitive path: 403, 404, 400, ValueError and 500 store no body, query or message")
def test_host_sensitive_error_incidents_store_no_body(opts):
    from django.http import Http404
    import mojo.errors as merrors

    values = _values()
    cases = [
        ("permission_error", PermissionError(f"not allowed for {values['email']}")),
        ("http404", Http404(f"no payout for {values['payerId']}")),
        # A MojoException's reason is app-authored and is kept (documented);
        # the body and query string must still be masked.
        ("value_exception", merrors.ValueException("bad payer")),
        ("value_error", ValueError(f"invalid literal for int(): '{values['payerId']}'")),
        ("runtime_error", RuntimeError(f"crash on {values['email']} {values['phone']}")),
    ]
    for name, exc in cases:
        path = f"/api/test_host_sensitive/{uuid.uuid4().hex[:8]}/{name}"
        _invoke(_request(path, values, True), exc)
        events = _events(path)
        assert_eq(len(events), 1, f"{name}: expected one Event for {path}, got {len(events)}")
        event = events[0]
        meta = event.metadata
        assert_eq(meta.get("request_data"), MARKER,
                  f"{name}: request_data must be the marker, got {meta.get('request_data')!r}")
        assert_eq(meta.get("http_query_string"), "",
                  f"{name}: the query string must be dropped, got {meta.get('http_query_string')!r}")
        assert_eq(meta.get("sensitive_body"), "host_sensitive",
                  f"{name}: the metadata must carry the sensitive_body marker")
        leaked = _leaks(event, values)
        assert_true(not leaked, f"{name}: Event metadata/details leaked {leaked}")
        if name == "runtime_error":
            assert_true(event.incident_id is not None, "a re-raised 500 (level 12) must file an Incident")
            leaked = _leaks(event.incident, values)
            assert_true(not leaked, f"runtime_error: Incident metadata/details leaked {leaked}")
            assert_true("RuntimeError" in (meta.get("stack_trace") or ""),
                        "the stack trace must still name the exception type")


@th.django_unit_test("unlisted path: a 500 still stores the body, query string and message as today")
def test_unlisted_path_stores_body_as_today(opts):
    values = _values()
    path = f"/api/test_not_sensitive/{uuid.uuid4().hex[:8]}"
    _invoke(_request(path, values, False), RuntimeError(f"crash on {values['email']}"))
    events = _events(path)
    assert_eq(len(events), 1, f"expected one Event for {path}, got {len(events)}")
    meta = events[0].metadata
    assert_eq(meta.get("request_data"), values, f"request_data must be stored as today, got {meta.get('request_data')!r}")
    assert_true(values["email"] in meta.get("http_query_string", ""), "the query string must be stored as today")
    assert_true(values["email"] in events[0].details, "the exception message must be in details as today")
    assert_true("sensitive_body" not in meta, "an unlisted path must not carry the marker")


@th.django_unit_test("framework-labelled path without a host listing stores the body as today")
def test_framework_label_alone_does_not_mask(opts):
    values = _values()
    path = f"/api/auth/test_sensitive_label/{uuid.uuid4().hex[:8]}"
    req = _request(path, values, False)
    req._sensitive_body_label = "account_auth"
    _invoke(req, RuntimeError(f"crash on {values['email']}"))
    events = _events(path)
    assert_eq(len(events), 1, f"expected one Event for {path}, got {len(events)}")
    meta = events[0].metadata
    assert_eq(meta.get("request_data"), values,
              f"a framework label alone must not mask the incident body, got {meta.get('request_data')!r}")
    assert_true(values["email"] in events[0].details, "a framework label alone must keep the message")
