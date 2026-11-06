"""Browser crash and debug reports: POST /api/incident/event/client (#4125).

A public route, so everything here is bounded and allowlisted, and the
sender's identity, address and group are the server's, never the body's.
``handle`` NEVER raises: every refusal is a returned 400 or 413, so a bad
report cannot file a second error Event about itself.
"""
import hashlib
import re

from mojo import JsonResponse
from mojo.helpers import logit

MAX_BODY = 64 * 1024
MAX_MESSAGE = 2000
MAX_TITLE = 200
MAX_STACK = 16 * 1024
MAX_COMPONENT_STACK = 8 * 1024
MAX_SHORT = 64
MAX_REFERENCE = 128
MAX_PATH = 512
MAX_LINE = 10_000_000
SUPPRESS_WINDOW = 300
SUPPRESS_CATEGORY = "incident_client_report"

CATEGORY_PREFIX = "frontend:"
CATEGORY_MAX = 64
_CATEGORY_RE = re.compile(r"frontend:[a-z0-9_.:-]+")

SEVERITY_LEVELS = {"debug": 1, "info": 2, "warning": 4, "error": 5, "fatal": 6}
DEFAULT_LEVEL = 5
MAX_LEVEL = 6

# metadata key -> maximum length; anything else in metadata is dropped.
_TEXT_FIELDS = {
    "stack_trace": MAX_STACK,
    "component_stack": MAX_COMPONENT_STACK,
    "app": MAX_SHORT,
    "environment": MAX_SHORT,
    "build": MAX_SHORT,
    "reference": MAX_REFERENCE,
}
_PATH_FIELDS = ("page_path", "filename")
_LINE_FIELDS = ("lineno", "colno")


def _refuse(message, status=400):
    return JsonResponse({"status": False, "error": message, "code": status}, status=status)


def _receipt():
    return JsonResponse({"status": True})


def normalize_level(value, threshold=None):
    """Return (stored_level, claim): the claim is the sent value when it differs.

    Named severities and integers are accepted. The stored level is clamped to
    1..min(6, threshold - 1), so a browser can never open an incident by level
    alone; a bad or missing value is stored as 5.
    """
    if threshold is None:
        from mojo.apps.incident.models.event import INCIDENT_LEVEL_THRESHOLD
        threshold = INCIDENT_LEVEL_THRESHOLD
    ceiling = max(1, min(MAX_LEVEL, int(threshold) - 1))
    if value is None:
        return min(DEFAULT_LEVEL, ceiling), None
    if isinstance(value, str) and value.strip().lower() in SEVERITY_LEVELS:
        level = SEVERITY_LEVELS[value.strip().lower()]
    elif isinstance(value, int) and not isinstance(value, bool):
        level = value
    else:
        return min(DEFAULT_LEVEL, ceiling), value
    stored = max(1, min(level, ceiling))
    return stored, (None if stored == level else value)


def _clean_path(value):
    if not isinstance(value, str):
        return None
    value = value.split("#", 1)[0].split("?", 1)[0]
    return value[:MAX_PATH]


def _clean_metadata(raw):
    if not isinstance(raw, dict):
        return {}
    clean = {}
    for key, maximum in _TEXT_FIELDS.items():
        value = raw.get(key)
        if isinstance(value, str):
            clean[key] = value[:maximum]
    for key in _PATH_FIELDS:
        value = _clean_path(raw.get(key))
        if value is not None:
            clean[key] = value
    for key in _LINE_FIELDS:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_LINE:
            clean[key] = value
    return clean


def _message(data):
    for name in ("message", "details", "title"):
        value = data.get(name)
        if isinstance(value, str) and value:
            return value[:MAX_MESSAGE]
    return ""


def _body_too_large(request):
    try:
        return int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY
    except (TypeError, ValueError):
        return False


def suppression_key(category, message, stack, who):
    first_line = (stack or "").split("\n", 1)[0]
    raw = "|".join((category, message[:MAX_TITLE], first_line, str(who)))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def handle(request, connection=None):
    """Validate, normalise and file one crash report. Never raises.

    ``connection`` is the reporter's keyword-only Redis seam for tests.
    """
    try:
        return _handle(request, connection)
    except Exception as exc:
        logit.error("incident.client_report", f"crash report not filed: {exc}")
        return _receipt()


def _handle(request, connection):
    from mojo.apps.incident import reporter
    from mojo.apps.incident.services import event_intake
    from mojo.helpers.request import safe_actor_context
    from mojo.models.rest import _resolve_stamp_actor

    if _body_too_large(request):
        return _refuse("report is too large", 413)
    if getattr(request, "invalid_group", False):
        return _refuse("Invalid group ID")
    data = request.DATA
    category = data.get("category")
    if (not isinstance(category, str) or len(category) > CATEGORY_MAX
            or not _CATEGORY_RE.fullmatch(category)):
        return _refuse(f"category must start with {CATEGORY_PREFIX!r} and use a-z 0-9 _ . : -")
    severity = data.get("severity", data.get("level"))
    level, claimed_level = normalize_level(severity)
    message = _message(data)
    metadata = _clean_metadata(data.get("metadata"))

    extras = dict(metadata)
    if claimed_level is not None:
        extras["claimed_level"] = claimed_level
    claimed_group = event_intake.claimed_group_id(request)
    if claimed_group is not None:
        extras["claimed_group_id"] = claimed_group

    actor = _resolve_stamp_actor(request)
    who = actor.pk if actor is not None else getattr(request, "ip", None)
    key = suppression_key(category, message, metadata.get("stack_trace"), who)
    reporter.report_event_suppressed(
        message or category, key,
        title=(message or category)[:MAX_TITLE], category=category, level=level,
        request=request, scope="web", window=SUPPRESS_WINDOW, fail_open=False,
        connection=connection, group=event_intake.trusted_group(request),
        reporter=safe_actor_context(request), **extras)
    return _receipt()
