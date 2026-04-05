"""Users domain tools — query users, activity, permissions, rate limits."""
from mojo.apps.assistant import tool


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


@tool(
    name="query_users",
    domain="users",
    permission="view_admin",
    description="Search/filter users by name, email, status, or permission. Returns up to 50 users.",
    input_schema={
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Search by username, email, or display name"},
            "is_active": {"type": "boolean", "description": "Filter by active status"},
            "permission": {"type": "string", "description": "Filter to users who have this permission (e.g. 'manage_users')"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
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

    # Filter by permission key — find users who have this permission set to True
    if params.get("permission"):
        perm = params["permission"]
        q &= Q(**{f"permissions__{perm}": True})

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    users = User.objects.filter(q).order_by("-created")[:limit]
    return [_safe_user_dict(u) for u in users]


@tool(
    name="get_user_detail",
    domain="users",
    permission="view_admin",
    description="Get full user profile, permissions, and group memberships. Sensitive fields (password, auth_key) are never included.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "The user ID"},
        },
        "required": ["user_id"],
    },
)
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


@tool(
    name="get_user_activity",
    domain="users",
    permission="view_admin",
    description="Get recent security events for a specific user.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "The user ID"},
            "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
        },
        "required": ["user_id"],
    },
)
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


@tool(
    name="query_rate_limits",
    domain="users",
    permission="view_admin",
    description="Show currently active rate limit entries from Redis.",
    input_schema={
        "type": "object",
        "properties": {},
    },
)
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


@tool(
    name="get_permission_summary",
    domain="users",
    permission="view_admin",
    description="Get a user's permissions and where they come from (user-level and group-level).",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "The user ID"},
        },
        "required": ["user_id"],
    },
)
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


@tool(
    name="disable_user",
    domain="users",
    permission="manage_users",
    description="Disable a user account and invalidate all active sessions. Cannot disable yourself. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "The user ID to disable"},
            "reason": {"type": "string", "description": "Reason for disabling the account"},
        },
        "required": ["user_id", "reason"],
    },
    mutates=True,
)
def _tool_disable_user(params, user):
    from mojo.apps.account.models import User
    import uuid

    user_id = int(params["user_id"])
    if user_id == user.pk:
        return {"error": "Cannot disable your own account"}

    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"error": f"User {user_id} not found"}

    if target.is_superuser and not user.is_superuser:
        return {"error": "Cannot disable a superuser account"}

    if not target.is_active:
        return {"error": f"User {user_id} is already disabled"}

    target.is_active = False
    target.auth_key = uuid.uuid4().hex
    target.save(update_fields=["is_active", "auth_key", "modified"])

    reason = params.get("reason", "Disabled by admin assistant")
    User.class_logit(None, f"[Admin Assistant] Disabled user {target.username}: {reason}",
                     kind="security:user_disabled", model_id=target.pk, level="warn")

    return {
        "ok": True,
        "user_id": target.pk,
        "username": target.username,
        "is_active": False,
        "sessions_invalidated": True,
    }


@tool(
    name="enable_user",
    domain="users",
    permission="manage_users",
    description="Re-enable a disabled user account. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "The user ID to enable"},
            "reason": {"type": "string", "description": "Reason for re-enabling the account"},
        },
        "required": ["user_id", "reason"],
    },
    mutates=True,
)
def _tool_enable_user(params, user):
    from mojo.apps.account.models import User

    user_id = int(params["user_id"])
    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"error": f"User {user_id} not found"}

    if target.is_superuser and not user.is_superuser:
        return {"error": "Cannot modify a superuser account"}

    if target.is_active:
        return {"error": f"User {user_id} is already active"}

    target.is_active = True
    target.save(update_fields=["is_active", "modified"])

    reason = params.get("reason", "Enabled by admin assistant")
    User.class_logit(None, f"[Admin Assistant] Enabled user {target.username}: {reason}",
                     kind="security:user_enabled", model_id=target.pk, level="info")

    return {
        "ok": True,
        "user_id": target.pk,
        "username": target.username,
        "is_active": True,
    }


@tool(
    name="force_logout",
    domain="users",
    permission="manage_users",
    description="Invalidate all active sessions for a user by rotating their auth key. Account stays active — user can log back in. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "The user ID to force logout"},
            "reason": {"type": "string", "description": "Reason for force logout"},
        },
        "required": ["user_id", "reason"],
    },
    mutates=True,
)
def _tool_force_logout(params, user):
    from mojo.apps.account.models import User
    import uuid

    user_id = int(params["user_id"])
    if user_id == user.pk:
        return {"error": "Cannot force-logout your own session via assistant"}

    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"error": f"User {user_id} not found"}

    if target.is_superuser and not user.is_superuser:
        return {"error": "Cannot force-logout a superuser account"}

    target.auth_key = uuid.uuid4().hex
    target.save(update_fields=["auth_key", "modified"])

    reason = params.get("reason", "Force logout by admin assistant")
    User.class_logit(None, f"[Admin Assistant] Force logout user {target.username}: {reason}",
                     kind="security:force_logout", model_id=target.pk, level="warn")

    return {
        "ok": True,
        "user_id": target.pk,
        "username": target.username,
        "sessions_invalidated": True,
        "account_active": target.is_active,
    }


@tool(
    name="update_user_permission",
    domain="users",
    permission="manage_users",
    description="Add or remove a permission from a user. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "integer", "description": "The user ID"},
            "permission": {"type": "string", "description": "Permission key (e.g. 'manage_users', 'view_security')"},
            "action": {"type": "string", "enum": ["add", "remove"], "description": "Whether to add or remove the permission"},
        },
        "required": ["user_id", "permission", "action"],
    },
    mutates=True,
)
def _tool_update_user_permission(params, user):
    from mojo.apps.account.models import User

    user_id = params["user_id"]
    perm_key = params["permission"]
    action = params.get("action", "add")

    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return {"error": f"User {user_id} not found"}

    if action == "remove":
        target.add_permission(perm_key, False)
        return {
            "ok": True,
            "user_id": target.pk,
            "username": target.username,
            "permission": perm_key,
            "action": "removed",
        }
    else:
        target.add_permission(perm_key, True)
        return {
            "ok": True,
            "user_id": target.pk,
            "username": target.username,
            "permission": perm_key,
            "action": "added",
        }
