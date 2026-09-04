"""Exhaustive folded-status coverage that mutates process-wide seams.

The parallel-safe ``test_models`` package retains representative 403/401
coverage. These additional framework variants temporarily replace production
module attributes, so the isolation policy requires this opt-in serial sibling.
"""

import json

from testit import helpers as th


def _invoke_dispatcher_with_raise(exception):
    from mojo.decorators import http as http_decorators
    import objict

    def fake_handler(request):
        raise exception

    wrapped = http_decorators.dispatch_error_handler(fake_handler)
    request = objict.objict()
    request.user = objict.objict(is_authenticated=False, id=None)
    request.DATA = objict.objict()
    request.QUERY_PARAMS = objict.objict()
    request.method = "GET"
    request.group = None
    request.bearer = None
    request.ip = "127.0.0.1"
    request.path = "/api/test"
    request.META = {}
    request.api_key = None
    return wrapped(request)


@th.django_unit_test("folded errors retain effective 409 and 440 status")
def test_conflict_and_reauth_with_status_200_flag(opts):
    from mojo import errors
    from mojo.decorators import http as http_decorators

    original = http_decorators._status_200_on_error
    http_decorators._status_200_on_error = lambda: True
    try:
        for exception, expected, code in (
                (errors.ValueException(
                    "changed", code="stale_revision", status=409),
                 409, "stale_revision"),
                (errors.ReauthRequiredException(), 440, 440)):
            response = _invoke_dispatcher_with_raise(exception)
            body = json.loads(response.content)
            assert response.status_code == 200, (
                f"folded {expected} must use wire 200, got {response.status_code}")
            assert body["status"] is False and body["code"] == code, (
                f"folded {expected} lost its error body contract: {body!r}")
            assert body["error_status"] == expected, (
                f"folded {expected} lost its effective status: {body!r}")
    finally:
        http_decorators._status_200_on_error = original


@th.django_unit_test(
    "model REST errors retain effective status under compatibility folding")
def test_model_rest_error_response_effective_status(opts):
    import objict
    from mojo.models import rest

    original = rest.MOJO_APP_STATUS_200_ON_ERROR
    rest.MOJO_APP_STATUS_200_ON_ERROR = True
    try:
        request = objict.objict(user=objict.objict(is_authenticated=True))
        response = rest.MojoModel.rest_error_response(
            request, status=409, error="changed", code="stale_revision")
        body = json.loads(response.content)
        assert response.status_code == 200, (
            f"model REST compatibility folding returned {response.status_code}")
        assert body["status"] is False and body["error_status"] == 409, (
            f"model REST folding lost effective status: {body!r}")
    finally:
        rest.MOJO_APP_STATUS_200_ON_ERROR = original
