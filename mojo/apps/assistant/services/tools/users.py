"""Users domain tools — query users, activity, permissions, rate limits."""


MAX_RESULTS = 50
MAX_MINUTES = 43200  # 30 days

# Fields to never expose in tool results
SENSITIVE_FIELDS = {"password", "auth_key", "onetime_code"}


def _safe_user_dict(user):
    """Return a safe dict representation of a user, excluding sensitive fields."""
    return {
        "id": user.pk,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_active": user.is_active,
        "is_superuser": user.is_superuser,
        "is_email_verified": user.is_email_verified,
        "is_phone_verified": user.is_phone_verified,
        "created": str(user.created),
        "last_activity": str(user.last_activity) if user.last_activity else None,
    }


def _tool_query_users(params, user):
    from mojo.apps.account.models import User
    from django.db.models import Q

    criteria = {}
    if params.get("is_active") is not None:
        criteria["is_active"] = params["is_active"]

    q = Q(**criteria)
    if params.get("search"):
        search = params["search"]
        q &= (
            Q(username__icontains=search)
            | Q(email__icontains=search)
            | Q(display_name__icontains=search)
        )

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    users = User.objects.filter(q).order_by("-created")[:limit]
    return [_safe_user_dict(u) for u in users]


def _tool_get_user_detail(params, user):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import GroupMember

    user_id = params["user_id"]
    target = User.objects.get(pk=user_id)

    result = _safe_user_dict(target)

    # Add permissions (safe to show to admins)
    result["permissions"] = target.permissions or {}

    # Add group memberships
    memberships = GroupMember.objects.filter(user=target).select_related("group")[:20]
    result["groups"] = [
        {
            "group_id": m.group_id,
            "group_name": m.group.name if m.group else None,
            "role": m.role if hasattr(m, "role") else None,
        }
        for m in memberships
    ]
    return result


def _tool_get_user_activity(params, user):
    from mojo.apps.incident.models import Event

    user_id = params["user_id"]
    minutes = min(params.get("minutes", 1440), MAX_MINUTES)

    from mojo.helpers import dates
    since = dates.subtract(minutes=minutes)

    events = Event.objects.filter(
        uid=user_id, created__gte=since
    ).order_by("-created")[:MAX_RESULTS]

    return [
        {
            "id": e.pk,
            "created": str(e.created),
            "category": e.category,
            "level": e.level,
            "title": e.title,
            "source_ip": e.source_ip,
        }
        for e in events
    ]


def _tool_query_rate_limits(params, user):
    """Query currently rate-limited keys from Redis."""
    from mojo.helpers.settings import settings
    try:
        from mojo.helpers.redis import get_redis
        r = get_redis()
        if r is None:
            return {"error": "Redis not available"}

        pattern = "ratelimit:*"
        keys = []
        for key in r.scan_iter(match=pattern, count=100):
            ttl = r.ttl(key)
            if ttl > 0:
                val = r.get(key)
                keys.append({
                    "key": key.decode() if isinstance(key, bytes) else key,
                    "count": int(val) if val else 0,
                    "ttl_seconds": ttl,
                })
            if len(keys) >= MAX_RESULTS:
                break

        return {"rate_limits": keys, "count": len(keys)}
    except Exception as e:
        return {"error": str(e)}


def _tool_get_permission_summary(params, user):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import GroupMember

    user_id = params["user_id"]
    target = User.objects.get(pk=user_id)

    result = {
        "user_id": target.pk,
        "username": target.username,
        "is_superuser": target.is_superuser,
        "user_permissions": target.permissions or {},
        "group_permissions": [],
    }

    memberships = GroupMember.objects.filter(user=target).select_related("group")[:20]
    for m in memberships:
        result["group_permissions"].append({
            "group_id": m.group_id,
            "group_name": m.group.name if m.group else None,
            "permissions": m.permissions if hasattr(m, "permissions") else {},
        })

    return result


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "query_users",
        "description": "Search/filter users by name, email, status. Returns up to 50 users.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string", "description": "Search by username, email, or display name"},
                "is_active": {"type": "boolean", "description": "Filter by active status"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_users,
        "permission": "view_admin",
    },
    {
        "name": "get_user_detail",
        "description": "Get full user profile, permissions, and group memberships. Sensitive fields (password, auth_key) are never included.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "The user ID"},
            },
            "required": ["user_id"],
        },
        "handler": _tool_get_user_detail,
        "permission": "view_admin",
    },
    {
        "name": "get_user_activity",
        "description": "Get recent security events for a specific user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "The user ID"},
                "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
            },
            "required": ["user_id"],
        },
        "handler": _tool_get_user_activity,
        "permission": "view_admin",
    },
    {
        "name": "query_rate_limits",
        "description": "Show currently active rate limit entries from Redis.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "handler": _tool_query_rate_limits,
        "permission": "view_admin",
    },
    {
        "name": "get_permission_summary",
        "description": "Get a user's permissions and where they come from (user-level and group-level).",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "description": "The user ID"},
            },
            "required": ["user_id"],
        },
        "handler": _tool_get_permission_summary,
        "permission": "view_admin",
    },
]
