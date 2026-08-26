"""Database-pool acquisition failures that must never recurse through the ORM."""

import json
import os
import threading
import time
from pathlib import Path


_LOCAL = threading.local()
_MAX_MESSAGE = 160


def is_pool_acquisition_error(error):
    """Return True only for psycopg-pool acquisition/queue failures."""
    try:
        from psycopg_pool import PoolTimeout, TooManyRequests
    except ImportError:
        pool_errors = ()
    else:
        pool_errors = (PoolTimeout, TooManyRequests)
    current = error
    seen = set()
    for _depth in range(4):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if pool_errors and isinstance(current, pool_errors):
            return True
        if current.__class__.__module__.startswith("psycopg_pool") and (
                "timeout" in current.__class__.__name__.lower()
                or "queue" in current.__class__.__name__.lower()
                or "request" in current.__class__.__name__.lower()):
            return True
        current = current.__cause__ or current.__context__
    return False


def mark_pool_error_reported(error):
    """Deduplicate one exception while it crosses framework error layers."""
    if getattr(error, "_mojo_pool_reported", False):
        return False
    try:
        error._mojo_pool_reported = True
    except Exception:
        marker = id(error)
        if getattr(_LOCAL, "last_error", None) == marker:
            return False
        _LOCAL.last_error = marker
    return True


def bounded_error(error):
    value = " ".join(str(error).split())[:_MAX_MESSAGE]
    return value or error.__class__.__name__[:_MAX_MESSAGE]


def emit_pool_error(error, path=None, sink_path=None):
    """Best-effort atomic JSON signal with no Django/Redis/AWS dependency."""
    if not mark_pool_error_reported(error):
        return False
    payload = {
        "schema": 1,
        "event": "database_pool_acquisition_error",
        "error_type": error.__class__.__name__[:80],
        "error": bounded_error(error),
        "path": str(path or "")[:160],
        "pid": os.getpid(),
        "at": time.time(),
    }
    target = sink_path or os.environ.get("MOJO_POOL_ERROR_FILE", "")
    if not target:
        try:
            os.write(2, (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))
        except Exception:
            pass
        return True
    try:
        path_obj = Path(target)
        path_obj.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path_obj.with_name(f".{path_obj.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path_obj)
    except Exception:
        pass
    return True


def http_pool_error_response(request, error):
    """Return the bounded retryable HTTP response without touching the ORM."""
    from django.http import JsonResponse

    request._mojo_pool_acquisition_error = True
    emit_pool_error(error, path=getattr(request, "path", None))
    response = JsonResponse(
        {
            "status": False,
            "error": "Database temporarily unavailable",
            "code": 503,
        },
        status=503,
    )
    response["Retry-After"] = "1"
    return response
