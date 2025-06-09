from mojo.apps.logit.models import Log
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse

API_PREFIX = "/".join([settings.get("MOJO_PREFIX", "api/").rstrip("/"), ""])
LOGIT_DB_ALL = settings.get("LOGIT_DB_ALL", False)
LOGIT_FILE_ALL = settings.get("LOGIT_FILE_ALL", False)
LOGIT_RETURN_REAL_ERROR = settings.get("LOGIT_RETURN_REAL_ERROR", True)
LOGGER = logit.get_logger("requests", "requests.log")
ERROR_LOGGER = logit.get_logger("error", "error.log")
LOGIT_NO_LOG_PREFIX = settings.get("LOGIT_NO_LOG_PREFIX", [])

class LoggerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Log the request before calling get_response
        self.log_request(request)
        try:
            response = self.get_response(request)
        except Exception as e:
            # Log or store the exception here
            err = ERROR_LOGGER.exception()
            Log.logit(request, err, "api_error")
            error = "system error"
            if LOGIT_RETURN_REAL_ERROR:
                error = str(e)
            response = JsonResponse(dict(status=False, error=error), status=500)
        # Log the response after get_response has been called
        self.log_response(request, response)
        return response

    def can_log(self, request):
        prefixes = LOGIT_NO_LOG_PREFIX
        if not isinstance(prefixes, (list, set, tuple)) or not prefixes:
            return True
        return not any(request.path.startswith(prefix) for prefix in prefixes)

    def log_request(self, request):
        if not self.can_log(request):
            return
        if LOGIT_DB_ALL:
            request.request_log = Log.logit(request, request.DATA.to_json(as_string=True), "request")
        if LOGIT_FILE_ALL:
            LOGGER.info(f"REQUEST - {request.method} - {request.ip} - {request.path}", request._raw_body)

    def log_response(self, request, response):
        if not self.can_log(request):
            return
        if LOGIT_DB_ALL:
            Log.logit(request, response.content, "response")
        if LOGIT_FILE_ALL:
            LOGGER.info(f"RESPONSE - {request.method} - {request.ip} - {request.path}", response.content)
