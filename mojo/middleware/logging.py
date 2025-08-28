import json
import threading
from queue import Queue, Empty
from mojo.apps.logit.models import Log
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.helpers.response import JsonResponse

API_PREFIX = "/".join([settings.get("MOJO_PREFIX", "api/").rstrip("/"), ""])
LOGIT_DB_ALL = settings.get("LOGIT_DB_ALL", False)
LOGIT_FILE_ALL = settings.get("LOGIT_FILE_ALL", False)
LOGIT_RETURN_REAL_ERROR = settings.get("LOGIT_RETURN_REAL_ERROR", True)
LOGIT_MAX_RESPONSE_SIZE = settings.get("LOGIT_MAX_RESPONSE_SIZE", 1024)  # 1KB default
LOGGER = logit.get_logger("requests", "requests.log")
ERROR_LOGGER = logit.get_logger("error", "error.log")
LOGIT_NO_LOG_PREFIX = settings.get("LOGIT_NO_LOG_PREFIX", [])

# Async logging setup
log_queue = Queue()
background_thread = None

def background_logger():
    """Background thread to process logs without blocking responses."""
    while True:
        try:
            log_item = log_queue.get(timeout=30)  # 30s timeout
            if log_item is None:  # Shutdown signal
                break

            log_type, request, content, log_kind = log_item

            if log_type == "db":
                Log.logit(request, content, log_kind)
            elif log_type == "file":
                method = request.method if request else "SYSTEM"
                ip = getattr(request, 'ip', 'unknown') if request else 'system'
                path = getattr(request, 'path', 'unknown') if request else 'system'
                LOGGER.info(f"{log_kind.upper()} - {method} - {ip} - {path}", content)

            log_queue.task_done()
        except Empty:
            continue
        except Exception as e:
            ERROR_LOGGER.exception(f"Background logging error: {e}")

def start_background_logger():
    global background_thread
    if background_thread is None or not background_thread.is_alive():
        background_thread = threading.Thread(target=background_logger, daemon=True)
        background_thread.start()

# Start the background logger
start_background_logger()

class LoggerMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self.log_request(request)
        try:
            response = self.get_response(request)
        except Exception as e:
            err = ERROR_LOGGER.exception()
            Log.logit(request, err, "api_error")  # Keep errors synchronous
            error = "system error"
            if LOGIT_RETURN_REAL_ERROR:
                error = str(e)
            response = JsonResponse(dict(status=False, error=error), status=500)

        self.log_response(request, response)
        return response

    def can_log(self, request):
        prefixes = LOGIT_NO_LOG_PREFIX
        if not isinstance(prefixes, (list, set, tuple)) or not prefixes:
            return True
        return not any(request.path.startswith(prefix) for prefix in prefixes)

    def should_log_full_content(self, request, response):
        """Fast conditional checks to decide logging strategy."""
        # Always log errors fully (but still async)
        if response.status_code >= 400:
            return True

        # Quick size check
        content_length = len(response.content)
        if content_length > LOGIT_MAX_RESPONSE_SIZE:
            return False

        # Path-based decisions
        if request.path.endswith('/list/') or '/list?' in request.path:
            return False

        return True

    def get_response_log_content(self, request, response):
        """Extract log content - prioritize log_context if available."""

        # Check for log_context first (fastest path)
        if hasattr(response, 'log_context') and response.log_context:
            return json.dumps(response.log_context)

        # Conditional processing based on fast checks
        if not self.should_log_full_content(request, response):
            return f"Response: {response.status_code}, Size: {len(response.content)} bytes"

        # For small responses, log full content
        return response.content

    def queue_log(self, log_type, request, content, log_kind):
        """Queue log for background processing."""
        try:
            log_queue.put((log_type, request, content, log_kind), block=False)
        except:
            # If queue is full, just skip this log to avoid blocking
            pass

    def log_request(self, request):
        if not self.can_log(request):
            return
        if LOGIT_DB_ALL:
            self.queue_log("db", request, request.DATA.to_json(as_string=True), "request")
        if LOGIT_FILE_ALL:
            self.queue_log("file", request, request._raw_body, "request")

    def log_response(self, request, response):
        if not self.can_log(request):
            return

        log_content = self.get_response_log_content(request, response)

        if LOGIT_DB_ALL:
            self.queue_log("db", request, log_content, "response")
        if LOGIT_FILE_ALL:
            self.queue_log("file", request, log_content, "response")
