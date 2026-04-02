"""
REST endpoints for the admin assistant.

Endpoints:
    POST /api/assistant              — Send message, get LLM response
    GET  /api/assistant/conversation — List user's conversations
    GET  /api/assistant/conversation/<pk> — Get conversation with messages
    DELETE /api/assistant/conversation/<pk> — Delete conversation
"""
from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings


@md.POST('/api/assistant')
@md.requires_perms('view_admin')
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

    return JsonResponse({
        "status": True,
        "data": {
            "response": result["response"],
            "conversation_id": result["conversation_id"],
            "tool_calls_made": result.get("tool_calls_made", []),
        },
    })


@md.GET('conversation')
@md.requires_perms('view_admin')
def on_list_conversations(request):
    """List the requesting user's conversations."""
    from mojo.apps.assistant.models import Conversation

    limit = min(request.DATA.get_typed("limit", default=20, typed=int), 50)
    conversations = Conversation.objects.filter(
        user=request.user
    ).order_by("-modified")[:limit]

    return JsonResponse({
        "status": True,
        "data": [
            {
                "id": c.pk,
                "title": c.title,
                "created": str(c.created),
                "modified": str(c.modified),
            }
            for c in conversations
        ],
    })


@md.GET('conversation/<int:pk>')
@md.requires_perms('view_admin')
def on_get_conversation(request, pk):
    """Get a conversation with its message history."""
    from mojo.apps.assistant.models import Conversation, Message

    try:
        conversation = Conversation.objects.get(pk=pk, user=request.user)
    except Conversation.DoesNotExist:
        return JsonResponse({
            "status": False,
            "error": "Conversation not found",
        }, status=404)

    messages = Message.objects.filter(
        conversation=conversation
    ).order_by("created")[:200]

    return JsonResponse({
        "status": True,
        "data": {
            "id": conversation.pk,
            "title": conversation.title,
            "created": str(conversation.created),
            "modified": str(conversation.modified),
            "messages": [
                {
                    "id": m.pk,
                    "role": m.role,
                    "content": m.content,
                    "tool_calls": m.tool_calls,
                    "created": str(m.created),
                }
                for m in messages
            ],
        },
    })


@md.DELETE('conversation/<int:pk>')
@md.requires_perms('view_admin')
def on_delete_conversation(request, pk):
    """Delete a conversation (owner only)."""
    from mojo.apps.assistant.models import Conversation

    try:
        conversation = Conversation.objects.get(pk=pk, user=request.user)
    except Conversation.DoesNotExist:
        return JsonResponse({
            "status": False,
            "error": "Conversation not found",
        }, status=404)

    conversation.delete()
    return JsonResponse({"status": True})
