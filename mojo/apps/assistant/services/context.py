"""
Build context messages for assistant conversations from any MojoModel instance.

Supports a registry of rich context builders for models that need deeper context
(e.g., tickets with notes, incidents with history/events). Falls back to generic
`to_dict(graph="detail")` serialization for any other MojoModel.
"""
from django.apps import apps

from mojo.helpers import logit

logger = logit.get_logger("assistant", "assistant.log")

MAX_NOTES = 20
MAX_HISTORY = 15
MAX_EVENTS = 10

# Sensitive field names to strip from generic serialization
SENSITIVE_SUBSTRINGS = ("password", "auth_key", "onetime_code", "secret", "token")

# Registry: "app_label.ModelName" -> builder function
_CONTEXT_BUILDERS = {}


def register_context_builder(model_string, builder_fn):
    """Register a rich context builder for a specific model."""
    _CONTEXT_BUILDERS[model_string.lower()] = builder_fn


def resolve_model(model_string):
    """Resolve 'app_label.ModelName' to a model class. Returns (model, error_dict)."""
    from mojo.models import MojoModel

    parts = model_string.split(".")
    if len(parts) != 2:
        return None, {"error": f"Invalid model format: '{model_string}'. Use 'app_label.ModelName'."}

    app_label, model_name = parts
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return None, {"error": f"Model '{model_string}' not found"}

    if not issubclass(model, MojoModel):
        return None, {"error": f"'{model_string}' is not a MojoModel"}

    if getattr(model, "RestMeta", None) is None:
        return None, {"error": f"'{model_string}' has no REST interface"}

    return model, None


def build_context(model_string, pk):
    """
    Build a context message for a model instance.

    Returns (title, message, error).
    - On success: (title_str, message_str, None)
    - On error: (None, None, error_str)
    """
    model, err = resolve_model(model_string)
    if err:
        return None, None, err["error"]

    try:
        instance = model.objects.get(pk=pk)
    except model.DoesNotExist:
        return None, None, f"{model_string} with pk={pk} not found"

    # Check for a rich builder
    key = model_string.lower()
    if key in _CONTEXT_BUILDERS:
        return _CONTEXT_BUILDERS[key](instance)

    # Generic fallback
    return _build_generic_context(model_string, instance)


def _build_generic_context(model_string, instance):
    """Generic context from to_dict serialization."""
    # Try "detail" graph first, fall back to "default"
    graphs = getattr(instance.RestMeta, "GRAPHS", {})
    graph = "detail" if "detail" in graphs else "default"

    try:
        data = instance.to_dict(graph=graph)
    except Exception:
        logger.exception("Failed to serialize %s pk=%s", model_string, instance.pk)
        return None, None, f"Failed to serialize {model_string}"

    # Strip sensitive fields
    if isinstance(data, dict):
        data = _strip_sensitive(data)

    title = _generic_title(model_string, instance, data)
    lines = [f"I need help with this {model_string.split('.')[-1]}:\n"]
    lines.append(f"## {title}\n")

    for k, v in data.items():
        if k in ("id", "pk"):
            continue
        lines.append(f"- **{k}**: {v}")

    message = "\n".join(lines)
    return title, message, None


def _strip_sensitive(data):
    """Remove fields with sensitive substrings from a dict."""
    cleaned = {}
    for k, v in data.items():
        k_lower = k.lower()
        if any(s in k_lower for s in SENSITIVE_SUBSTRINGS):
            continue
        cleaned[k] = v
    return cleaned


def _generic_title(model_string, instance, data):
    """Generate a reasonable title for a generic model."""
    model_name = model_string.split(".")[-1]
    label = getattr(instance, "title", None) or getattr(instance, "name", None) or ""
    if label:
        return f"{model_name} #{instance.pk}: {label[:100]}"
    return f"{model_name} #{instance.pk}"


# ---------------------------------------------------------------------------
# Rich context builders
# ---------------------------------------------------------------------------

def _build_ticket_context(instance):
    """Rich context for incident.Ticket — includes notes."""
    from mojo.apps.incident.models import TicketNote

    t = instance
    title = f"Ticket #{t.pk}: {t.title}"

    lines = [
        "I need help with this ticket:\n",
        f"## {title}",
        f"- **Status**: {t.status}",
        f"- **Priority**: {t.priority}",
        f"- **Category**: {t.category}",
        f"- **Created**: {t.created}",
    ]

    if t.assignee_id:
        try:
            lines.append(f"- **Assignee**: {t.assignee.email}")
        except Exception:
            lines.append(f"- **Assignee ID**: {t.assignee_id}")

    if t.incident_id:
        lines.append(f"- **Linked Incident**: #{t.incident_id}")

    if t.description:
        lines.append(f"\n## Description\n{t.description[:3000]}")

    # Load notes
    notes = (
        TicketNote.objects.filter(parent=t)
        .select_related("user")
        .order_by("-created")[:MAX_NOTES]
    )
    if notes:
        lines.append(f"\n## Notes ({len(notes)} entries, newest first)")
        for n in notes:
            user_label = n.user.email if n.user_id and n.user else "System"
            lines.append(f"- [{n.created}] {user_label}: {(n.note or '')[:500]}")

    # LLM metadata
    meta = t.metadata or {}
    if meta.get("llm_linked"):
        lines.append("\n*This ticket was created by the LLM agent.*")

    message = "\n".join(lines)
    return title, message, None


def _build_incident_context(instance):
    """Rich context for incident.Incident — includes history and events."""
    from mojo.apps.incident.models import IncidentHistory, Event

    i = instance
    title = f"Incident #{i.pk}"
    if i.title:
        title = f"Incident #{i.pk}: {i.title[:100]}"

    lines = [
        "I need help with this incident:\n",
        f"## {title}",
        f"- **Status**: {i.status}",
        f"- **Priority**: {i.priority}",
        f"- **Category**: {i.category}",
        f"- **Created**: {i.created}",
    ]

    if i.source_ip:
        lines.append(f"- **Source IP**: {i.source_ip}")
    if i.hostname:
        lines.append(f"- **Hostname**: {i.hostname}")
    if i.scope and i.scope != "global":
        lines.append(f"- **Scope**: {i.scope}")
    if i.rule_set_id:
        lines.append(f"- **RuleSet**: #{i.rule_set_id}")

    if i.details:
        lines.append(f"\n## Details\n{i.details[:3000]}")

    # LLM assessment from metadata
    meta = i.metadata or {}
    if meta.get("llm_assessment"):
        lines.append(f"\n## LLM Assessment\n{str(meta['llm_assessment'])[:2000]}")

    # History
    history = (
        IncidentHistory.objects.filter(parent=i)
        .select_related("user")
        .order_by("-created")[:MAX_HISTORY]
    )
    if history:
        lines.append(f"\n## History ({len(history)} entries, newest first)")
        for h in history:
            user_label = h.user.email if h.user_id and h.user else "System"
            note_text = (h.note or "")[:300]
            lines.append(f"- [{h.created}] {h.kind} — {user_label}: {note_text}")

    # Recent events
    events = Event.objects.filter(incident=i).order_by("-created")[:MAX_EVENTS]
    if events:
        lines.append(f"\n## Recent Events ({len(events)} shown)")
        for e in events:
            lines.append(
                f"- [evt-{e.pk}] {e.created} | level={e.level} | {(e.title or '')[:200]}"
            )

    # Linked tickets
    ticket_count = i.tickets.count()
    if ticket_count:
        tickets = i.tickets.order_by("-created")[:5]
        lines.append(f"\n## Linked Tickets ({ticket_count} total)")
        for t in tickets:
            lines.append(f"- Ticket #{t.pk}: {t.title} (status={t.status})")

    message = "\n".join(lines)
    return title, message, None


# Register rich builders
register_context_builder("incident.Ticket", _build_ticket_context)
register_context_builder("incident.Incident", _build_incident_context)
