"""
REST endpoints for the admin assistant.

Endpoints:
    POST /api/assistant                    — Send message, get LLM response
    POST /api/assistant/context            — Create conversation with model context
    GET  /api/assistant/conversation       — List user's conversations
    GET  /api/assistant/conversation/<pk>  — Conversation detail (?graph=detail for messages)
    DELETE /api/assistant/conversation/<pk> — Delete conversation (owner or admin)
    GET  /api/assistant/skill              — List user's skills
    GET  /api/assistant/skill/<pk>         — Skill detail (?graph=detail for steps/triggers)
    DELETE /api/assistant/skill/<pk>       — Delete skill (owner or admin)

Memory endpoints are in memory.py.
"""
from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.assistant.models import Conversation, Message, Skill


@md.POST('')
@md.requires_perms('view_admin', 'assistant')
@md.rate_limit("assistant", ip_limit=60, duid_limit=30)
@md.requires_params('message')
def on_assistant_message(request):
    """Send a message to the assistant and get a response."""
    from mojo.apps.assistant.services.agent import run_assistant

    message = request.DATA.message
    conversation_id = request.DATA.get("conversation_id")

    result = run_assistant(request.user, message, conversation_id=conversation_id)

    if "error" in result:
        status_code = result.get("status_code", 400)
        return JsonResponse({
            "status": False,
            "error": result["error"],
            "conversation_id": result.get("conversation_id"),
        }, status=status_code)

    data = {
        "response": result["response"],
        "conversation_id": result["conversation_id"],
        "tool_calls_made": result.get("tool_calls_made", []),
        "duration_ms": result.get("duration_ms"),
    }
    blocks = result.get("blocks")
    if blocks:
        data["blocks"] = blocks

    return JsonResponse({"status": True, "data": data})


@md.POST('context')
@md.requires_perms('view_admin', 'assistant')
@md.requires_params('model', 'pk')
def on_assistant_context(request):
    """Create a conversation pre-loaded with context from any MojoModel instance."""
    from mojo.apps.assistant.services.context import resolve_model, build_context

    model_string = request.DATA.model
    pk = request.DATA.pk

    # Validate model exists
    model, err = resolve_model(model_string)
    if err:
        return JsonResponse({"status": False, "error": err["error"]}, status=400)

    # Check user has VIEW_PERMS for this model
    view_perms = getattr(model.RestMeta, "VIEW_PERMS", [])
    has_access = False
    for perm in view_perms:
        if perm == "owner":
            continue
        if request.user.has_permission(perm):
            has_access = True
            break
    if not has_access:
        return JsonResponse({"status": False, "error": "Permission denied"}, status=403)

    # Duplicate prevention: same user + same model + same pk
    existing = Conversation.objects.filter(
        user=request.user,
        metadata__source_model=model_string.lower(),
        metadata__source_pk=pk,
    ).first()
    if existing:
        return JsonResponse({"status": True, "data": {"conversation_id": existing.pk, "existing": True}})

    # Build the context message
    title, message, error = build_context(model_string, pk)
    if error:
        return JsonResponse({"status": False, "error": error}, status=404)

    # Create conversation + first message
    conversation = Conversation.objects.create(
        user=request.user,
        group=getattr(request, "group", None),
        title=title[:255],
        metadata={
            "source_model": model_string.lower(),
            "source_pk": pk,
        },
    )
    Message.objects.create(
        conversation=conversation,
        role="user",
        content=message,
    )

    return JsonResponse({"status": True, "data": {"conversation_id": conversation.pk}})


@md.URL('conversation')
@md.URL('conversation/<int:pk>')
@md.uses_model_security(Conversation)
def on_conversation(request, pk=None):
    return Conversation.on_rest_request(request, pk)


@md.URL('skill')
@md.URL('skill/<int:pk>')
@md.uses_model_security(Skill)
def on_skill(request, pk=None):
    return Skill.on_rest_request(request, pk)


