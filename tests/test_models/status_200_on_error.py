"""
MOJO_APP_STATUS_200_ON_ERROR honored uniformly by the dispatcher.

When the flag is True, the dispatcher must return wire status 200 with
`status:false` body for any MojoException — including PermissionDeniedException.
This was previously only honored by `rest_error_response`. Now that 403 sites
raise instead of returning a JsonResponse, the dispatcher carries the contract.

Tests run in-process and patch `_status_200_on_error` on the dispatcher
module — the flag is read each request, so the patch is sufficient
without a server restart.
"""
import json
from testit import helpers as th


@th.django_unit_setup()
def setup_status_200(opts):
    pass


def _invoke_dispatcher_with_raise(exception):
    """Run a fake handler that raises ``exception`` through the real
    dispatcher decorator so we exercise the same code path REST views use.
    """
    from mojo.decorators import http as http_decorators
    import objict

    def fake_handler(request):
        raise exception

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


@th.django_unit_test()
def test_403_default_wire_status(opts):
    """Default flag (False) → 403 returned on the wire."""
    from mojo.errors import PermissionDeniedException
    from mojo.decorators import http as http_decorators

    original = http_decorators._status_200_on_error
    http_decorators._status_200_on_error = lambda: False
    try:
        resp = _invoke_dispatcher_with_raise(
            PermissionDeniedException(
                reason="denied",
                branch="user.has_permission",
                model_name="X",
                event_type="user_permission_denied",
            )
        )
        assert resp.status_code == 403, (
            f"With flag False, expected wire status 403, got {resp.status_code}"
        )
        body = json.loads(resp.content)
        assert body.get("status") is False, f"Expected status:false in body, got {body!r}"
        assert body.get("code") == 403, f"Expected code:403 in body, got {body!r}"
    finally:
        http_decorators._status_200_on_error = original


@th.django_unit_test()
def test_403_with_status_200_flag(opts):
    """Flag True → 200 on the wire, but body still carries the real code."""
    from mojo.errors import PermissionDeniedException
    from mojo.decorators import http as http_decorators

    original = http_decorators._status_200_on_error
    http_decorators._status_200_on_error = lambda: True
    try:
        resp = _invoke_dispatcher_with_raise(
            PermissionDeniedException(
                reason="denied",
                branch="user.has_permission",
                model_name="X",
                event_type="user_permission_denied",
            )
        )
        assert resp.status_code == 200, (
            f"With flag True, expected wire status 200, got {resp.status_code}"
        )
        body = json.loads(resp.content)
        assert body.get("status") is False, f"Expected status:false in body, got {body!r}"
        assert body.get("code") == 403, (
            f"Body must still carry the real code 403, got {body!r}"
        )
    finally:
        http_decorators._status_200_on_error = original


@th.django_unit_test()
def test_401_with_status_200_flag(opts):
    """401 (unauthenticated) also rewrites to 200 when flag is True."""
    from mojo.errors import PermissionDeniedException
    from mojo.decorators import http as http_decorators

    original = http_decorators._status_200_on_error
    http_decorators._status_200_on_error = lambda: True
    try:
        resp = _invoke_dispatcher_with_raise(
            PermissionDeniedException(
                reason="unauth",
                status=401, code=401,
                branch="unauthenticated",
                event_type="unauthenticated",
            )
        )
        assert resp.status_code == 200, (
            f"401 with flag True must return 200 on the wire, got {resp.status_code}"
        )
        body = json.loads(resp.content)
        assert body.get("code") == 401, f"Body code must remain 401, got {body!r}"
    finally:
        http_decorators._status_200_on_error = original
