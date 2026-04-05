"""Ticket query, detail, create, update, and note tools."""
from mojo.apps.assistant import tool

MAX_RESULTS = 50


@tool(
    name="query_tickets",
    domain="security",
    permission="view_security",
    description="Filter tickets by status, priority, category.",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status (open, closed, etc.)"},
            "priority_gte": {"type": "integer", "description": "Minimum priority"},
            "category": {"type": "string", "description": "Filter by category"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
def _tool_query_tickets(params, user):
    from mojo.apps.incident.models import Ticket

    criteria = {}
    if params.get("status"):
        criteria["status"] = params["status"]
    if params.get("priority_gte"):
        criteria["priority__gte"] = params["priority_gte"]
    if params.get("category"):
        criteria["category"] = params["category"]

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    tickets = Ticket.objects.filter(**criteria).order_by("-modified")[:limit]

    return [
        {
            "id": t.pk,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "category": t.category,
            "created": str(t.created),
            "modified": str(t.modified),
            "assignee_id": t.assignee_id,
            "incident_id": t.incident_id,
        }
        for t in tickets
    ]


@tool(
    name="get_ticket",
    domain="security",
    permission="view_security",
    description="Get full details of a ticket including all notes, assignee, and metadata.",
    input_schema={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "integer", "description": "The ticket ID"},
        },
        "required": ["ticket_id"],
    },
)
def _tool_get_ticket(params, user):
    from mojo.apps.incident.models import Ticket, TicketNote

    ticket_id = params["ticket_id"]
    try:
        t = Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {"error": f"Ticket {ticket_id} not found"}

    notes = (
        TicketNote.objects.filter(parent=t)
        .select_related("user")
        .order_by("created")[:MAX_RESULTS]
    )
    note_list = []
    for n in notes:
        entry = {
            "id": n.pk, "note": n.note, "created": str(n.created),
            "has_media": n.media_id is not None,
        }
        if n.user_id:
            entry["user_id"] = n.user_id
            entry["user_email"] = n.user.email if n.user else None
        note_list.append(entry)

    return {
        "id": t.pk, "title": t.title, "description": (t.description or "")[:2000],
        "status": t.status, "priority": t.priority, "category": t.category,
        "assignee_id": t.assignee_id, "incident_id": t.incident_id,
        "metadata": t.metadata or {}, "created": str(t.created),
        "modified": str(t.modified), "notes": note_list,
    }


@tool(
    name="create_ticket",
    domain="security",
    permission="manage_security",
    description="Create a ticket for human review. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Ticket title"},
            "description": {"type": "string", "description": "Ticket description / analysis"},
            "priority": {"type": "integer", "description": "1-10 priority (default 5)", "default": 5},
            "incident_id": {"type": "integer", "description": "Associated incident ID (optional)"},
        },
        "required": ["title", "description"],
    },
    mutates=True,
)
def _tool_create_ticket(params, user):
    from mojo.apps.incident.models import Ticket

    incident = None
    if params.get("incident_id"):
        try:
            from mojo.apps.incident.models import Incident
            incident = Incident.objects.get(pk=params["incident_id"])
        except Exception:
            pass

    ticket = Ticket.objects.create(
        title=params["title"],
        description=params["description"],
        priority=params.get("priority", 5),
        category="assistant_review",
        incident=incident,
        user=user,
        metadata={"assistant_created": True},
    )
    return {"ok": True, "ticket_id": ticket.pk}


@tool(
    name="update_ticket",
    domain="security",
    permission="manage_security",
    description="Update a ticket's status, priority, category, or assignee. Adds an audit note. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "integer", "description": "The ticket ID to update"},
            "status": {"type": "string", "description": "New status"},
            "priority": {"type": "integer", "description": "New priority (1-10)"},
            "category": {"type": "string", "description": "New category"},
            "assignee_id": {"type": "integer", "description": "New assignee user ID (null to unassign)", "nullable": True},
        },
        "required": ["ticket_id"],
    },
    mutates=True,
)
def _tool_update_ticket(params, user):
    from mojo.apps.incident.models import Ticket

    ticket_id = params["ticket_id"]
    try:
        t = Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {"error": f"Ticket {ticket_id} not found"}

    updatable = ["status", "priority", "category"]
    update_fields = []
    changes = []
    for field in updatable:
        if field in params:
            old_val = getattr(t, field)
            new_val = params[field]
            if old_val != new_val:
                setattr(t, field, new_val)
                update_fields.append(field)
                changes.append(f"{field}: {old_val} -> {new_val}")

    if "assignee_id" in params:
        from mojo.apps.account.models import User as AccountUser
        new_assignee_id = params["assignee_id"]
        if new_assignee_id is None:
            if t.assignee_id is not None:
                t.assignee_id = None
                update_fields.append("assignee_id")
                changes.append("assignee: unassigned")
        else:
            try:
                target_user = AccountUser.objects.get(pk=new_assignee_id)
            except AccountUser.DoesNotExist:
                return {"error": f"User {new_assignee_id} not found"}
            if not target_user.is_active:
                return {"error": f"User {new_assignee_id} is not active"}
            if t.assignee_id != new_assignee_id:
                t.assignee_id = new_assignee_id
                update_fields.append("assignee_id")
                changes.append(f"assignee: -> {target_user.email}")

    if not update_fields:
        return {"error": "No fields to update. Provide at least one of: status, priority, category, assignee_id"}

    t.save(update_fields=update_fields)
    note_text = f"[Admin Assistant] Updated: {', '.join(changes)}"
    t.add_note(note_text, user=user)
    return {
        "ok": True, "ticket_id": t.pk, "updated_fields": update_fields,
        "status": t.status, "priority": t.priority, "category": t.category,
        "assignee_id": t.assignee_id,
    }


@tool(
    name="add_ticket_note",
    domain="security",
    permission="manage_security",
    description="Add a note to a ticket. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "ticket_id": {"type": "integer", "description": "The ticket ID"},
            "note": {"type": "string", "description": "Note text to add"},
        },
        "required": ["ticket_id", "note"],
    },
    mutates=True,
)
def _tool_add_ticket_note(params, user):
    from mojo.apps.incident.models import Ticket, TicketNote

    ticket_id = params["ticket_id"]
    try:
        t = Ticket.objects.get(pk=ticket_id)
    except Ticket.DoesNotExist:
        return {"error": f"Ticket {ticket_id} not found"}

    note = TicketNote.objects.create(
        parent=t, note=params["note"], user=user, group=t.group,
    )
    return {"ok": True, "note_id": note.pk, "ticket_id": t.pk}
