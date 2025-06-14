from mojo.helpers import request as rhelper
import time
from objict import objict
from mojo.helpers.settings import settings

ANONYMOUS_USER = objict(is_authenticated=False)


class MojoMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.started_at = time.time()
        request.user = ANONYMOUS_USER
        request.group = None
        request.request_log = None
        request.ip = rhelper.get_remote_ip(request)
        request.user_agent = rhelper.get_user_agent(request)
        request.duid = rhelper.get_device_id(request)
        if settings.LOGIT_REQUEST_BODY:
            request._raw_body = str(request.body)
        else:
            request._raw_body = None
        request.DATA = rhelper.parse_request_data(request)
        return self.get_response(request)
