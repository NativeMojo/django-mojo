"""Submitted security events: POST /api/incident/event (maestro #4125).

Any signed-in caller (user, API key, group token) may submit an Event. The
server owns who sent it and from where: ``uid``, ``source_ip``, ``group``, the
request provenance and ``metadata.reporter``. Whatever the sender claims about
another person, address, company or incident is kept beside it as a flat
``claimed_*`` metadata key for the RuleSets to judge, and is never written to
the trusted columns.
"""
from mojo import errors as merrors
from mojo.helpers import logit
from mojo.helpers.request import restricted_identity, safe_actor_context

# Metadata keys the server writes on every submitted row. A client value under
# one of these is moved to ``claimed_<key>``, never dropped and never trusted.
RESERVED_METADATA_KEYS = frozenset({
    # reporter provenance (reporter._create_event_dict)
    "server", "request_ip", "http_path", "http_protocol", "http_method",
    "http_query_string", "http_user_agent", "http_host", "bearer",
    "user_name", "user_email", "sensitive_body", "api_key_id", "api_key_name",
    # Event.sync_metadata
    "level", "scope", "category", "source_ip", "title", "details",
    "model_name", "model_id", "group_id", "group_name", "country_code",
    "country_name", "city", "region", "latitude", "longitude", "timezone",
    # the server-authored sender
    "reporter",
})

# Body fields that name a trusted column. A sent value is kept as a claim.
_CLAIMED_FIELDS = (
    ("uid", "claimed_uid"),
    ("source_ip", "claimed_source_ip"),
    ("country_code", "claimed_country_code"),
    ("incident", "claimed_incident_id"),
)

HOSTNAME_MAX = 16  # Event.hostname column width
CATEGORY_MAX = 124  # Event.category column width
SCOPE_MAX = 64  # Event.scope column width


def trusted_group(request):
    """The request group the server may stamp on an Event, else None.

    A restricted identity (ApiKey, GroupScopedToken) keeps ``request.group``:
    the dispatcher already confined it to the credential's own tree. A
    signed-in person keeps it only as a member (or ancestor member) of that
    group or as a security operator. Anything else, including an anonymous
    caller's ``?group=``, is not trusted.
    """
    group = getattr(request, "group", None)
    if group is None:
        return None
    if restricted_identity(request) is not None:
        return group
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        if user.has_permission(["manage_security", "security"]):
            return group
        if group.get_member_for_user(user, check_parents=True) is not None:
            return group
    except Exception as exc:
        logit.warning("incident.event_intake", f"group trust check failed: {exc}")
    return None


def claimed_group_id(request):
    """The id of a routing group the server does not trust, else None."""
    group = getattr(request, "group", None)
    if group is None or trusted_group(request) is not None:
        return None
    return getattr(group, "pk", None)


def _text(data, name, maximum=None, default=None):
    value = data.get(name, None)
    if value is None:
        return default
    if not isinstance(value, str):
        raise merrors.ValueException(f"{name} must be a string")
    if maximum is not None and len(value) > maximum:
        value = value[:maximum]
    return value


def _category(data):
    value = data.get("category", None)
    if not isinstance(value, str) or not (1 <= len(value) <= CATEGORY_MAX):
        raise merrors.ValueException(
            f"category is required: a string of 1-{CATEGORY_MAX} characters")
    return value


def _level(data):
    value = data.get("level", 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise merrors.ValueException("level must be an integer")
    return value


def _model_id(data):
    value = data.get("model_id", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _metadata(data):
    value = data.get("metadata", None)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise merrors.ValueException("metadata must be an object")
    return dict(value)


def split_metadata(client):
    """Split client metadata into (kept, claims): reserved keys become claims."""
    kept, claims = {}, {}
    for key, value in client.items():
        if not isinstance(key, str):
            continue
        if key in RESERVED_METADATA_KEYS:
            claims[f"claimed_{key}"] = value
        else:
            kept[key] = value
    return kept, claims


def submit(request):
    """Create, stamp and publish one submitted Event. Returns the Event.

    Raises ValueException (400) for a malformed body. A rule or handler
    failure while publishing is logged and keeps the row.
    """
    from mojo.apps.incident import reporter
    from mojo.models.rest import _resolve_stamp_actor

    data = request.DATA
    category = _category(data)
    level = _level(data)
    scope = _text(data, "scope", SCOPE_MAX, default="global") or "global"
    title = _text(data, "title")
    details = _text(data, "details")
    if details is None:
        details = title if title is not None else category
    hostname = _text(data, "hostname", HOSTNAME_MAX)
    model_name = _text(data, "model_name")
    model_id = _model_id(data)
    client, claims = split_metadata(_metadata(data))

    for field, claim in _CLAIMED_FIELDS:
        value = data.get(field, None)
        if value is not None:
            claims[claim] = value
    routed = claimed_group_id(request)
    if routed is not None:
        # The routing group wins over a client metadata.group_id claim.
        claims["claimed_group_id"] = routed

    event_data = reporter._create_event_dict(
        details, title, category, level, request, scope,
        hostname=hostname, model_name=model_name, model_id=model_id,
        group=trusted_group(request), reporter=safe_actor_context(request))
    actor = _resolve_stamp_actor(request)
    event_data["uid"] = actor.pk if actor is not None else None
    event_data["source_ip"] = getattr(request, "ip", None)
    # Server keys win; claims are never overwritten by client keys.
    event_data["metadata"] = {**client, **claims, **event_data["metadata"]}
    event = reporter._save_event(event_data)
    try:
        event.publish(allow_default_llm=False)
    except Exception as exc:
        logit.error(
            "incident.event_intake",
            f"publish failed for submitted event {event.pk} ({category}): {exc}")
    return event
