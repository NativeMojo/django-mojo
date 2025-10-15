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


class MojoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.started_at = time.time()
        request.user = ANONYMOUS_USER
        request.group = None
        request.device = None
        request.request_log = None
        request.ip = rhelper.get_remote_ip(request)
        request.user_agent = rhelper.get_user_agent(request)
        request.duid = rhelper.get_device_id(request)
        # logger.info(f"duid: {request.duid}", request.META)
        if settings.LOGIT_REQUEST_BODY:
            request._raw_body = str(request.body)
        else:
            request._raw_body = None
        request.DATA = rhelper.parse_request_data(request)
        token = rest.ACTIVE_REQUEST.set(request)
        try:
            resp = self.get_response(request)
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
            return resp
        finally:
            rest.ACTIVE_REQUEST.reset(token)
