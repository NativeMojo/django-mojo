"""
REST endpoints for the admin assistant.

Endpoints:
    POST /api/assistant                    — Send message, get LLM response
    POST /api/assistant/action             — Approve or cancel a pending action
    GET  /api/assistant/action             — Owner-scoped pending/resolved actions
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
@md.requires_global_perms('view_admin', 'assistant')
@md.rate_limit("assistant", ip_limit=60, duid_limit=30)
@md.requires_params('message')
def on_assistant_message(request):
    """Send a message to the assistant and get a response."""
    from mojo.apps.assistant.services.agent import run_assistant

    message = request.DATA.message
    conversation_id = request.DATA.get("conversation_id")
    attachments_supplied = "attachments" in request.DATA

    result = run_assistant(
        request.user,
        message,
        conversation_id=conversation_id,
        request=request,
        attachments=request.DATA.get("attachments"),
        attachments_supplied=attachments_supplied,
    )

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
    pending_actions = result.get("pending_actions")
    if pending_actions:
        data["pending_actions"] = pending_actions

    return JsonResponse({"status": True, "data": data})


def _approval_refusal(refusal):
    """Map one ``ApprovalRefused`` code to its HTTP shape. Stated exactly once.

    Every unresolvable case — unknown id, wrong user, wrong conversation, wrong
    tool, expired, used, canceled, superseded, unregistered, de-mutated,
    fingerprint mismatch — arrives here as ``action_unavailable`` and returns the
    identical body, so a stolen id learns nothing. The distinguishable outcomes
    exist only for the bound owner of a live record.
    """
    from mojo import errors as merrors
    from mojo.helpers import infrastructure
    from mojo.apps.assistant.services import approvals

    if refusal.code == approvals.CODE_REAUTH:
        raise merrors.ReauthRequiredException()
    if refusal.code == approvals.CODE_PERMISSION:
        return JsonResponse({
            "status": False,
            "error": refusal.message,
            "error_code": approvals.CODE_PERMISSION,
        }, status=403)
    if refusal.code == approvals.CODE_INFRASTRUCTURE:
        return JsonResponse({
            "status": False,
            "error": refusal.message,
            "error_code": infrastructure.ERROR_CODE,
            "data": {"mode": infrastructure.EXTERNAL, "setting": infrastructure.SETTING},
        }, status=403)
    return JsonResponse({
        "status": False,
        "error": refusal.message,
        "error_code": refusal.code,
    }, status=409)


@md.POST('action')
@md.denies_key_backed_session()
@md.requires_global_perms('view_admin', 'assistant')
@md.rate_limit("assistant_action", ip_limit=60, duid_limit=30)
@md.requires_params('action_id', 'decision')
def on_assistant_action(request):
    """Approve or cancel one pending mutating action.

    The `Authorization` JWT is the sole carrier of fresh-auth evidence — the
    `auth_time` claim proves recency with no new transport, which is why an
    action whose tool declares `fresh_auth_seconds` can only resolve here. The
    step-up check runs INSIDE the body, not as a decorator, because the
    requirement is per-action; the same conditional pattern is used at
    mojo/apps/aws/rest/s3.py:122.

    A handler that ran and failed is NOT an HTTP error: it is 200 with
    `state: "failed"`, because the mutation was attempted and the operator has
    to be told so.
    """
    from mojo.apps.assistant.services import approvals

    try:
        result = approvals.resolve(
            request.user,
            request.DATA.get("action_id"),
            request.DATA.get("decision"),
            request=request,
            conversation_id=request.DATA.get("conversation_id"),
        )
    except approvals.ApprovalRefused as refusal:
        return _approval_refusal(refusal)

    return JsonResponse({"status": True, "data": {
        "action": result["block"],
        "message_id": result.get("message_id"),
    }})


@md.GET('action')
@md.denies_key_backed_session()
@md.requires_global_perms('view_admin', 'assistant')
def on_assistant_action_list(request):
    """The caller's own approval cards with their CURRENT state (50 most recent).

    Owner-scoped in the query, not by a permission: an approval belongs to the
    operator it was proposed for, and `view_admin` is not a licence to read
    someone else's. Pass `?conversation=<id>` to scope to one conversation.
    """
    from mojo.apps.assistant.services import approvals

    conversation_id = request.DATA.get("conversation")
    if conversation_id:
        conversation = Conversation.objects.filter(
            pk=conversation_id, user=request.user).first()
        if conversation is None:
            return JsonResponse({"status": False, "error": "Conversation not found"},
                                status=404)
        actions = approvals.states_for_conversation(conversation)
    else:
        actions = approvals.states_for_user(request.user)

    return JsonResponse({"status": True, "data": {"actions": actions}})


@md.POST('context')
@md.requires_global_perms('view_admin', 'assistant')
@md.requires_params('model', 'pk')
def on_assistant_context(request):
    """Create a conversation pre-loaded with context from any MojoModel instance."""
    from mojo.apps.assistant.services.context import resolve_model, build_context
    from mojo.apps.assistant.services.tools.models import check_ai_access
    from mojo.helpers.request import is_key_backed_session

    # Refused outright for any confined credential (ApiKey / GroupScopedToken).
    # The VIEW_PERMS loop below reads only the caller's GLOBAL permission dict
    # — no tenant bound anywhere — and build_context then reads arbitrary model
    # rows by pk. requires_global_perms above already denies these sessions;
    # this is the local statement of the rule so the endpoint stays closed if
    # that decorator is ever relaxed.
    if is_key_backed_session(request):
        return JsonResponse({"status": False, "error": "Permission denied"}, status=403)

    model_string = request.DATA.model
    pk = request.DATA.pk

    # Validate model exists
    model, err = resolve_model(model_string)
    if err:
        return JsonResponse({"status": False, "error": err["error"]}, status=400)

    # Apply the shared model-tool policy before permissions, object lookup,
    # serialization, duplicate lookup, or conversation/message creation.
    # DENY_AI is a structural data-boundary, not a permission the caller can
    # overcome. The gate's security event deliberately has no group stamp.
    ai_error = check_ai_access(model, "view", request.user, request=request)
    if ai_error:
        return JsonResponse({"status": False, "error": ai_error["error"]}, status=403)

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
