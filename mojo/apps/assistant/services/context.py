"""
Build context messages for assistant conversations from any MojoModel instance.

Supports a registry of rich context builders for models that need deeper context
(e.g., tickets with notes, incidents with history/events). Any other MojoModel
falls back to generic serialization through the server-selected assistant graph
(`RestMeta.GRAPHS["ai"]`, else `"default"`) — never the wider `detail` graph.
"""
from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import DataError, transaction
from django.db.models import Q

from mojo import errors as me
from mojo.apps.assistant.services import model_serialization
from mojo.helpers import logit

logger = logit.get_logger("assistant", "assistant.log")

# The one answer for a row that is missing and for a row the caller may not
# read: the pair must not tell a caller whether a row exists.
NOT_FOUND = "Context source not found"
MAX_MODEL_STRING = 200
MAX_PK_TEXT = 64
_NO_SELECTION = object()

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
    """Resolve 'app_label.ModelName' to a model class. Returns (model, error_dict).

    Every error is a fixed sentence: nothing the caller sent is repeated back.
    """
    from mojo.models import MojoModel

    if not isinstance(model_string, str) or len(model_string) > MAX_MODEL_STRING:
        return None, {"error": "Invalid model format. Use 'app_label.ModelName'."}
    parts = model_string.split(".")
    if len(parts) != 2 or not all(parts):
        return None, {"error": "Invalid model format. Use 'app_label.ModelName'."}

    app_label, model_name = parts
    try:
        model = apps.get_model(app_label, model_name)
    except (LookupError, ValueError):
        return None, {"error": "Model not found"}

    if not issubclass(model, MojoModel):
        return None, {"error": "Model is not a MojoModel"}

    if getattr(model, "RestMeta", None) is None:
        return None, {"error": "Model has no REST interface"}

    # NO_REST is a structural data boundary, not a permission the caller can
    # overcome — the same guard `services/tools/models.py:_resolve_model` applies.
    # Without it, `POST /api/assistant/context` was a read primitive for every
    # NO_REST model, including another operator's assistant.PendingAction.
    if getattr(model.RestMeta, "NO_REST", False):
        return None, {"error": "Model is not available for querying"}

    return model, None


def clean_pk(model, value):
    """The caller's pk as ``model``'s own key type, or None when it cannot be one."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, str) and (not value.strip() or len(value) > MAX_PK_TEXT):
        return None
    try:
        return model._meta.pk.to_python(value)
    except (ValidationError, ValueError, TypeError):
        return None


def authorize_source(request, model, instance):
    """Decide whether the caller may read ``instance``, as a REST read would.

    Returns ``(allowed, graph_override)``. The check is the framework's own
    (`rest_check_permission`): the owner match, the row's own tenant and the
    instance's view hook all apply.

    That check changes the request by design: it re-points ``request.group``
    at the row's tenant, and a view hook may select a narrower graph
    (`account.Group` sets `basic` for a plain member). Both are put back here,
    whatever the outcome. A graph the hook selected is returned for
    `build_context`; one the model does not declare as an explicit mapping
    refuses the read rather than fall back to a wider graph.
    """
    # The REST framework reads "no VIEW_PERMS declared" as open to everyone.
    # This endpoint has always refused such a model, and still does.
    if not model.get_rest_meta_prop("VIEW_PERMS", None):
        return False, None

    group = getattr(request, "group", None)
    # The endpoint has already refused a caller's graph. Anything on the
    # request is set aside anyway, so what is found after the check is the
    # hook's choice and nothing else.
    sent = request.DATA.pop("graph", _NO_SELECTION)
    selected = _NO_SELECTION
    try:
        allowed = model.rest_check_permission(request, "VIEW_PERMS", instance)
        if "graph" in request.DATA:
            selected = request.DATA.get("graph")
    except me.PermissionDeniedException:
        # a view hook that refuses by raising: the same answer as any refusal,
        # not a 403 that would tell the caller the row exists
        allowed = False
    finally:
        request.group = group
        request.DATA.pop("graph", None)
        if sent is not _NO_SELECTION:
            request.DATA["graph"] = sent
    if not allowed:
        return False, None
    if selected is _NO_SELECTION:
        return True, None
    try:
        if not isinstance(selected, str) or not selected:
            raise model_serialization.AssistantGraphError("the selected graph is not a name")
        model_serialization.select_graph(model, graph_override=selected)
    except model_serialization.AssistantGraphError as err:
        logger.warning("context: %s refused, its permission check selected an unusable graph: %s",
                       model_serialization.model_label(model), err)
        return False, None
    return True, selected


def _report_denied(request, label, pk):
    """File a level-4 event for a refused read: one per caller and model an hour.

    Suppressed because a caller can loop over ids: the first refusal of the
    hour is reported, the rest are only logged. No group stamp, like the
    DENY_AI gate's event. Reporting never raises, so it cannot turn the 404
    into a 500.
    """
    from mojo.apps.incident import reporter

    user_id = getattr(request.user, "id", None)
    details = f"Assistant context denied: {label} pk={pk} by user {user_id}"
    logger.info(details)
    extra = {}
    ip = getattr(request, "ip", None)
    if ip and ip != "assistant":
        extra["source_ip"] = ip
    reporter.report_event_suppressed(
        details, f"{user_id}:{label}", title=details[:80],
        category="assistant_context_denied", level=4, scope="assistant",
        uid=user_id, model_name=label, **extra)


def build_context(model_string, instance, request=None, graph_override=None):
    """
    Build a context message for an already-resolved, already-authorized row.

    ``model_string`` is the row's own label ('app_label.ModelName'), not what a
    caller typed. Nothing is looked up here: the caller of this function has
    resolved ``instance`` once and checked it may be read.

    ``request`` is the caller's request. The generic fallback uses it to check
    ``GRAPH_PERMISSIONS`` on the graph it serializes through; without one, a
    gated graph is refused.

    ``graph_override`` is a graph an instance permission hook selected. A rich
    builder reads the row directly and cannot honor it, so a row with a
    selected graph always takes the generic path through that graph.

    Returns (title, message, error).
    - On success: (title_str, message_str, None)
    - On error: (None, None, error_str)
    """
    if not model_serialization.is_mojo_model(instance):
        return None, None, f"{model_string} context needs a model instance"

    key = model_string.lower()
    if graph_override is None and key in _CONTEXT_BUILDERS:
        return _CONTEXT_BUILDERS[key](instance)

    return _build_generic_context(
        model_string, instance, request=request, graph_override=graph_override)


def create_conversation(user, group, title, message, metadata):
    """Store the conversation and its first message together, or neither."""
    from mojo.apps.assistant.models import Conversation, Message

    with transaction.atomic():
        conversation = Conversation.objects.create(
            user=user, group=group, title=title[:255], metadata=metadata)
        Message.objects.create(conversation=conversation, role="user", content=message)
    return conversation


def open_context(request):
    """Find or create the caller's conversation about one model row.

    The body of `POST /api/assistant/context`. Returns ``(data, error, status)``:
    ``data`` on success, otherwise a fixed error sentence and its HTTP status.

    Order matters. Shape and model policy are settled before any row is read;
    the row is read once; the caller's right to read it is checked before the
    duplicate lookup, so a retry is checked again; and everything recorded
    about the source comes from the row, not from what the caller sent.
    """
    from mojo.apps.account.models import Group
    from mojo.apps.assistant.models import Conversation
    from mojo.apps.assistant.services.tools.models import check_ai_access

    # The graph is the server's choice. A caller cannot name one, and with
    # none sent, a graph found on the request after the permission check can
    # only be a hook's.
    if "graph" in request.DATA:
        return None, "A context conversation does not take a graph", 400

    model, err = resolve_model(request.DATA.get("model"))
    if err:
        return None, err["error"], 400

    # DENY_AI is a structural data-boundary, not a permission the caller can
    # overcome. The gate's security event deliberately has no group stamp.
    ai_error = check_ai_access(model, "view", request.user, request=request)
    if ai_error:
        return None, ai_error["error"], 403

    pk = clean_pk(model, request.DATA.get("pk"))
    if pk is None:
        return None, "Invalid pk", 400
    try:
        instance = model._default_manager.filter(pk=pk).first()
    except (ValidationError, ValueError, TypeError, OverflowError, DataError):
        # a value the key type accepts and the column does not
        return None, "Invalid pk", 400
    if instance is None:
        return None, NOT_FOUND, 404

    label = model_serialization.model_label(model)
    allowed, graph_override = authorize_source(request, model, instance)
    if not allowed:
        _report_denied(request, label, instance.pk)
        return None, NOT_FOUND, 404

    # The row names itself: its own label and its own key, so an id sent as a
    # number and as text find the same conversation.
    source_model = instance._meta.label_lower
    source_pk = instance.pk if isinstance(instance.pk, int) else str(instance.pk)
    existing = Conversation.objects.filter(
        user=request.user, metadata__source_model=source_model,
    ).filter(
        Q(metadata__source_pk=source_pk) | Q(metadata__source_pk=str(source_pk)),
    ).first()
    if existing:
        return {"conversation_id": existing.pk, "existing": True}, None, 200

    title, message, error = build_context(
        label, instance, request=request, graph_override=graph_override)
    if error or not isinstance(message, str) or not message:
        logger.warning("context: no message for %s pk=%s: %s", label, instance.pk,
                       error or "the builder returned no text")
        return None, NOT_FOUND, 404
    if not isinstance(title, str) or not title.strip():
        title = f"{model.__name__} #{instance.pk}"

    # The conversation belongs to the row's tenant, never to a group the
    # caller sent. A row with no tenant gives a conversation with none.
    group = model._instance_group(instance)
    if not isinstance(group, Group):
        group = None
    conversation = create_conversation(
        request.user, group, title, message,
        {"source_model": source_model, "source_pk": source_pk})
    return {"conversation_id": conversation.pk}, None, 200


def _build_generic_context(model_string, instance, request=None, graph_override=None):
    """Generic context through the server-selected assistant graph."""
    try:
        data = model_serialization.serialize_instance(
            instance, request=request, graph_override=graph_override)
    except Exception:
        logger.exception("Failed to serialize %s pk=%s", model_string, instance.pk)
        return None, None, f"Failed to serialize {model_string}"

    # Strip sensitive fields — defense in depth behind the graph
    if isinstance(data, dict):
        data = _strip_sensitive(data)

    title = _generic_title(model_string, instance.pk, data)
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


def _generic_title(model_string, pk, data):
    """Title for a generic model: its `title` or `name` as the graph serialized it.

    It takes the serialized ``data`` and not the instance, so it cannot read a
    field the graph left out. The title heads the context message and names
    the conversation, so a name the graph omits must be missing from both.
    """
    model_name = model_string.split(".")[-1]
    label = data.get("title") or data.get("name")
    if isinstance(label, str) and label:
        return f"{model_name} #{pk}: {label[:100]}"
    return f"{model_name} #{pk}"


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
