"""Groups domain tools — query groups, members, detail, activity."""


MAX_RESULTS = 50


def _tool_query_groups(params, user):
    from mojo.apps.account.models import Group

    criteria = {}
    if params.get("name"):
        criteria["name__icontains"] = params["name"]
    if params.get("kind"):
        criteria["kind"] = params["kind"]
    if params.get("is_active") is not None:
        criteria["is_active"] = params["is_active"]

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    groups = Group.objects.filter(**criteria).order_by("-modified")[:limit]

    return [
        {
            "id": g.pk,
            "name": g.name,
            "kind": g.kind,
            "is_active": g.is_active,
            "parent_id": g.parent_id,
            "created": str(g.created),
            "modified": str(g.modified),
            "last_activity": str(g.last_activity) if g.last_activity else None,
        }
        for g in groups
    ]


def _tool_get_group_detail(params, user):
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.group import GroupMember

    group_id = params["group_id"]
    group = Group.objects.get(pk=group_id)

    member_count = GroupMember.objects.filter(group=group).count()
    children_count = Group.objects.filter(parent=group).count()

    # Exclude secrets from metadata
    safe_metadata = {k: v for k, v in (group.metadata or {}).items()
                     if "secret" not in k.lower() and "key" not in k.lower()}

    return {
        "id": group.pk,
        "name": group.name,
        "kind": group.kind,
        "is_active": group.is_active,
        "parent_id": group.parent_id,
        "member_count": member_count,
        "children_count": children_count,
        "metadata": safe_metadata,
        "created": str(group.created),
        "modified": str(group.modified),
    }


def _tool_get_group_members(params, user):
    from mojo.apps.account.models.group import GroupMember

    group_id = params["group_id"]
    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)

    members = (
        GroupMember.objects.filter(group_id=group_id)
        .select_related("user")
        .order_by("-created")[:limit]
    )

    return [
        {
            "id": m.pk,
            "user_id": m.user_id,
            "username": m.user.username if m.user else None,
            "display_name": m.user.display_name if m.user else None,
            "is_active": m.user.is_active if m.user else None,
            "permissions": m.permissions if hasattr(m, "permissions") else {},
            "created": str(m.created),
        }
        for m in members
    ]


def _tool_get_group_activity(params, user):
    from mojo.apps.incident.models import Event
    from mojo.helpers import dates

    group_id = params["group_id"]
    minutes = params.get("minutes", 1440)
    since = dates.subtract(minutes=minutes)

    # Events related to the group via model reference
    events = Event.objects.filter(
        model_name="Group", model_id=group_id, created__gte=since
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


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "query_groups",
        "description": "Filter groups by name, kind (type), active status. Returns up to 50 groups.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Search by group name (partial match)"},
                "kind": {"type": "string", "description": "Filter by group kind/type"},
                "is_active": {"type": "boolean", "description": "Filter by active status"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_groups,
        "permission": "view_groups",
    },
    {
        "name": "get_group_detail",
        "description": "Get group info including member count, children count, and metadata.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "The group ID"},
            },
            "required": ["group_id"],
        },
        "handler": _tool_get_group_detail,
        "permission": "view_groups",
    },
    {
        "name": "get_group_members",
        "description": "List members of a group with their roles and permissions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "The group ID"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
            "required": ["group_id"],
        },
        "handler": _tool_get_group_members,
        "permission": "view_groups",
    },
    {
        "name": "get_group_activity",
        "description": "Get recent security events related to a specific group.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "The group ID"},
                "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
            },
            "required": ["group_id"],
        },
        "handler": _tool_get_group_activity,
        "permission": "view_groups",
    },
]
