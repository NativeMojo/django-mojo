"""
REST endpoints for the admin assistant.

Endpoints:
    POST /api/assistant              — Send message, get LLM response
    GET  /api/assistant/conversation — List user's conversations
    GET  /api/assistant/conversation/<pk> — Conversation detail (use ?graph=detail for messages)
    DELETE /api/assistant/conversation/<pk> — Delete conversation (owner or admin)
"""
from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.assistant.models import Conversation


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

    data = {
        "response": result["response"],
        "conversation_id": result["conversation_id"],
        "tool_calls_made": result.get("tool_calls_made", []),
    }
    blocks = result.get("blocks")
    if blocks:
        data["blocks"] = blocks

    return JsonResponse({"status": True, "data": data})


@md.URL('conversation')
@md.URL('conversation/<int:pk>')
@md.uses_model_security(Conversation)
def on_conversation(request, pk=None):
    return Conversation.on_rest_request(request, pk)
