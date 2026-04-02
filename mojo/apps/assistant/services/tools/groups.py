"""Groups domain tools — query groups, members, detail, activity."""


MAX_RESULTS = 50
MAX_MINUTES = 43200  # 30 days


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

    # Only expose safe, known metadata keys (allowlist)
    safe_keys = {"timezone", "short_name", "description", "website", "industry"}
    safe_metadata = {k: v for k, v in (group.metadata or {}).items()
                     if k in safe_keys}

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
    minutes = min(params.get("minutes", 1440), MAX_MINUTES)
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


def _tool_create_group(params, user):
    from mojo.apps.account.models import Group

    name = params["name"]
    kind = params.get("kind", "group")
    parent_id = params.get("parent_id")

    parent = None
    if parent_id:
        try:
            parent = Group.objects.get(pk=parent_id)
        except Group.DoesNotExist:
            return {"error": f"Parent group {parent_id} not found"}

    group = Group.objects.create(
        name=name,
        kind=kind,
        parent=parent,
        is_active=True,
    )

    return {
        "ok": True,
        "group_id": group.pk,
        "name": group.name,
        "kind": group.kind,
        "parent_id": group.parent_id,
    }


def _tool_invite_to_group(params, user):
    from mojo.apps.account.models import Group

    group_id = params["group_id"]
    email = params["email"]
    permissions = params.get("permissions", [])

    try:
        group = Group.objects.get(pk=group_id)
    except Group.DoesNotExist:
        return {"error": f"Group {group_id} not found"}

    ms = group.invite(email)
    if ms and permissions:
        perm_dict = {p: True for p in permissions}
        ms.on_rest_update_jsonfield("permissions", perm_dict)
        ms.save()

    return {
        "ok": True,
        "group_id": group.pk,
        "group_name": group.name,
        "email": email,
        "member_id": ms.pk if ms else None,
        "permissions": permissions,
    }


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
    {
        "name": "create_group",
        "description": "Create a new group (organization, merchant, team, etc.). IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Group name"},
                "kind": {"type": "string", "description": "Group type (e.g. 'org', 'merchant', 'team', 'group')", "default": "group"},
                "parent_id": {"type": "integer", "description": "Parent group ID (for creating child groups under an org)"},
            },
            "required": ["name"],
        },
        "handler": _tool_create_group,
        "permission": "manage_groups",
        "mutates": True,
    },
    {
        "name": "invite_to_group",
        "description": "Invite a user to a group by email. Creates the user if they don't exist and sends an invite. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_id": {"type": "integer", "description": "The group to invite the user to"},
                "email": {"type": "string", "description": "Email address of the user to invite"},
                "permissions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Permission keys to grant (e.g. ['manage_users', 'view_security']). Empty for read-only access.",
                },
            },
            "required": ["group_id", "email"],
        },
        "handler": _tool_invite_to_group,
        "permission": "manage_groups",
        "mutates": True,
    },
]
