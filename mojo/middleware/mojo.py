import uuid

from mojo.helpers import request as rhelper
import time
from objict import objict
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.models import rest

logger = logit.get_logger("debug", "debug.log")

ANONYMOUS_USER = objict(
    display_name="Anonymous",
    username="anonymous",
    email="anonymous@example.com",
    is_authenticated=False,
    has_permission=lambda: False)

# Cookie TTL for _muid: 2 years in seconds
_MUID_MAX_AGE = 63072000


class MojoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.started_at = time.time()
        request.bearer = None
        request.user = ANONYMOUS_USER
        request.group = None
        request.device = None
        request.request_log = None
        request.ip = rhelper.get_remote_ip(request)
        request.user_agent = rhelper.get_user_agent(request)
        request.duid = rhelper.get_device_id(request)

        # Server-controlled device and session identity (HttpOnly cookies).
        # _muid: persistent device identity (2yr), unforgeable by JS.
        # _msid: browser session identity (no Expires = dies on browser close).
        # These are set on every request so all framework code can use
        # request.muid and request.msid without depending on bouncer.
        request.muid = request.COOKIES.get('_muid', '')
        request.msid = request.COOKIES.get('_msid', '')
        _muid_is_new = not request.muid
        _msid_is_new = not request.msid
        if _muid_is_new:
            request.muid = uuid.uuid4().hex
        if _msid_is_new:
            request.msid = uuid.uuid4().hex

        if settings.LOGIT_REQUEST_BODY:
            request._raw_body = str(request.body)
        else:
            request._raw_body = None
        request.DATA = rhelper.parse_request_data(request)

        # Tab session ID from client JS (sessionStorage, tab-scoped).
        # Only present when mojo-bouncer.js is active on the page.
        request.mtab = request.DATA.get('_mtab', '')

        token = rest.ACTIVE_REQUEST.set(request)
        try:
            resp = self.get_response(request)
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"

            # Set server identity cookies on the response when newly generated.
            if _muid_is_new:
                resp.set_cookie(
                    '_muid', request.muid,
                    max_age=_MUID_MAX_AGE,
                    httponly=True,
                    secure=not settings.DEBUG,
                    samesite='Lax',
                )
            if _msid_is_new:
                # No max_age / expires → session cookie (dies on browser close)
                resp.set_cookie(
                    '_msid', request.msid,
                    httponly=True,
                    secure=not settings.DEBUG,
                    samesite='Lax',
                )

            return resp
        finally:
            rest.ACTIVE_REQUEST.reset(token)
