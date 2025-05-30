from mojo.apps.logit.models import Log
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse

LOGIT_DB_ALL = settings.get("LOGIT_DB_ALL", False)
LOGIT_FILE_ALL = settings.get("LOGIT_FILE_ALL", False)
LOGIT_RETURN_REAL_ERROR = settings.get("LOGIT_RETURN_REAL_ERROR", True)
LOGGER = logit.get_logger("requests", "requests.log")
ERROR_LOGGER = logit.get_logger("error", "error.log")

class LoggerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
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
        # Only log if the endpoint starts with '/api'
        if request.path.startswith('/api'):
            # Log Request and Response details with data
            if LOGIT_DB_ALL:
                Log.logit(request, request.body, "api_request")
                Log.logit(request, response.content, "api_response")
            if LOGIT_FILE_ALL:
                LOGGER.info(f"REQUEST - {request.method} - {request.ip} - {request.path}", request.body)
                LOGGER.info(f"RESPONSE - {request.method} - {request.ip} - {request.path}", response.content)
        return response
