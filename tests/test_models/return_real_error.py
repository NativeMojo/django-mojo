"""Maestro item 51 — the dispatcher's 500 handler honors LOGIT_RETURN_REAL_ERROR.

mojo/middleware/logging.py has always consulted the flag before putting
`str(e)` in a 500 body, but the REST dispatcher's `except Exception` branch
returned `str(err)` unconditionally. So a deployment that set the flag to False
still leaked exception text — including whatever a raised message happened to
contain — through every endpoint routed by the decorator.

The default stays True: this is a bug fix that makes the flag work at all, not
a change to the framework default.

Tests run in-process and patch `_return_real_error` on the dispatcher module —
the flag is read per request, so the patch is sufficient without a server
restart. Deliberately NOT th.server_settings: that rewrites var/django.conf and
triggers the uvicorn reload that maestro item 275 exists to keep away from
in-flight connections.

The DB-Setting-row test, which overrides django.conf.settings in-process, lives
in tests/test_models_extended_serial/return_real_error.py (maestro item #1839).
"""
import json
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "bug"  # #2792 tier curation

SECRET_TEXT = "psycopg2 OperationalError: password=hunter2 host=10.0.0.7"


@th.django_unit_setup()
def setup_return_real_error(opts):
    pass


def _invoke_500():
    """Run a handler that raises a plain Exception through the real dispatcher."""
    from mojo.decorators import http as http_decorators
    import objict

    def fake_handler(request):
        raise Exception(SECRET_TEXT)

    wrapped = http_decorators.dispatch_error_handler(fake_handler)
    req = objict.objict()
    req.user = objict.objict()
    req.user.is_authenticated = False
    req.user.id = None
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = "GET"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/test"
    req.META = {}
    req.api_key = None
    return wrapped(req)


def _body(resp):
    return json.loads(resp.content.decode("utf-8"))


@th.django_unit_test("500 body hides the exception text when LOGIT_RETURN_REAL_ERROR is False")
def test_500_hides_real_error_when_flag_false(opts):
    from unittest.mock import patch
    from mojo.decorators import http as http_decorators

    with patch.object(http_decorators, "_return_real_error", return_value=False), \
            patch.object(http_decorators, "_events_on_errors", return_value=False):
        resp = _invoke_500()

    assert_eq(resp.status_code, 500, f"an unhandled exception must still be a 500, got {resp.status_code}")
    body = _body(resp)
    assert_eq(body.get("error"), "system error",
              f"the 500 body must carry the generic message, got {body.get('error')!r}")
    assert_true(SECRET_TEXT not in resp.content.decode("utf-8"),
                "the exception text leaked into the 500 response despite the flag being False")
    assert_eq(body.get("code"), 500, f"the envelope's code field must be preserved, got {body.get('code')!r}")
    assert_eq(body.get("status"), False, f"the envelope's status field must be preserved, got {body.get('status')!r}")


@th.django_unit_test("500 body returns the exception text when LOGIT_RETURN_REAL_ERROR is True")
def test_500_returns_real_error_when_flag_true(opts):
    from unittest.mock import patch
    from mojo.decorators import http as http_decorators

    with patch.object(http_decorators, "_return_real_error", return_value=True), \
            patch.object(http_decorators, "_events_on_errors", return_value=False):
        resp = _invoke_500()

    assert_eq(resp.status_code, 500, f"an unhandled exception must still be a 500, got {resp.status_code}")
    body = _body(resp)
    assert_true(SECRET_TEXT in body.get("error", ""),
                f"with the flag True the real error must be returned, got {body.get('error')!r}")


@th.django_unit_test("LOGIT_RETURN_REAL_ERROR defaults to True (framework default unchanged)")
def test_return_real_error_default_is_true(opts):
    from mojo.decorators import http as http_decorators

    assert_true(http_decorators._return_real_error() is True,
                "the framework default must remain True — flipping it is a separate, "
                "deliberate decision, not a side effect of this fix")


@th.django_unit_test("4xx responses still carry their real text when the flag is False")
def test_4xx_still_returns_real_text_when_flag_false(opts):
    """Only the unhandled-500 path is gated. Much 4xx text is deliberate client
    feedback ('Invalid code', 'permission denied: X') and must not be swept up
    by a consistency-minded refactor."""
    from unittest.mock import patch
    from mojo.decorators import http as http_decorators
    import mojo.errors as merrors
    import objict

    def fake_handler(request):
        raise merrors.ValueException("Invalid code")

    wrapped = http_decorators.dispatch_error_handler(fake_handler)
    req = objict.objict()
    req.user = objict.objict()
    req.user.is_authenticated = False
    req.user.id = None
    req.DATA = objict.objict()
    req.QUERY_PARAMS = objict.objict()
    req.method = "GET"
    req.group = None
    req.bearer = None
    req.ip = "127.0.0.1"
    req.path = "/api/test"
    req.META = {}
    req.api_key = None

    with patch.object(http_decorators, "_return_real_error", return_value=False), \
            patch.object(http_decorators, "_events_on_errors", return_value=False):
        resp = wrapped(req)

    body = json.loads(resp.content.decode("utf-8"))
    assert_true("Invalid code" in body.get("error", ""),
                f"4xx client feedback must survive the flag being False, got {body.get('error')!r}")


@th.django_unit_test("incident reporting keeps the REAL error even when the client sees the generic one")
def test_incident_still_records_real_error(opts):
    """The flag must gate only what reaches the CLIENT. Losing server-side
    detail would be a worse regression than the leak being fixed — an operator
    would be left with 'system error' and nothing to debug from.

    The other tests in this module patch _events_on_errors to False, so without
    this one nothing pins that the incident keeps the real text; a refactor
    that moved the generic string upstream would silently gut reporting.
    """
    from unittest.mock import patch
    from mojo.decorators import http as http_decorators
    from mojo.models import rest as rest_models

    with patch.object(http_decorators, "_return_real_error", return_value=False), \
            patch.object(http_decorators, "_events_on_errors", return_value=True), \
            patch.object(rest_models.MojoModel, "class_report_incident_for_user") as report:
        resp = _invoke_500()

    body = _body(resp)
    assert_eq(body.get("error"), "system error",
              f"the client must still see the generic message, got {body.get('error')!r}")
    assert_true(report.called,
                "an incident must still be reported when _events_on_errors is on")
    details = report.call_args.kwargs.get("details", "")
    assert_true(SECRET_TEXT in details,
                f"the incident must record the REAL error for the operator, got details={details!r}")
    assert_true(report.call_args.kwargs.get("stack_trace"),
                "the incident must still carry a stack trace")
