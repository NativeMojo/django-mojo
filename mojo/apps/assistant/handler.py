"""
WebSocket message handler for the admin assistant.

Handles assistant-related message types routed from the User model's
on_realtime_message hook. LLM processing runs as a background job
so the WebSocket handler returns immediately.

Message types (client → server):
  - assistant_message: Send a new message to the assistant

Response types (server → client, via realtime publish):
  - assistant:thinking:   Processing has started
  - assistant:tool_call:  A tool was called (sent per tool)
  - assistant:response:   Final LLM response
  - assistant:error:      Something went wrong
"""
from mojo.helpers import logit

logger = logit.get_logger("assistant", "assistant.log")

ASSISTANT_MESSAGE_TYPES = {
    "assistant_message",
}


def handle_assistant_message(user, data):
    """
    Main entry point called from User.on_realtime_message.

    Validates the request, stores the user message, publishes a
    background job, and returns an immediate ack.
    """
    message_type = data.get("type") or data.get("action")

    if message_type == "assistant_message":
        return _handle_message(user, data)

    return {"type": "error", "error": f"Unknown assistant message type: {message_type}"}


def _handle_message(user, data):
    """Handle a new assistant message — validate, enqueue, ack."""
    from mojo.helpers.settings import settings
    from mojo.apps.assistant.models import Conversation, Message

    # Check feature flag
    if not settings.get("LLM_ADMIN_ENABLED", False, kind="bool"):
        return {"type": "assistant:error", "error": "Assistant is not enabled"}

    # Check permission
    if not user.has_permission("view_admin"):
        return {"type": "assistant:error", "error": "Permission denied"}

    message = (data.get("message") or "").strip()
    if not message:
        return {"type": "assistant:error", "error": "Message is required"}

    conversation_id = data.get("conversation_id")

    # Load or create conversation
    conversation = None
    if conversation_id:
        conversation = Conversation.objects.filter(
            pk=conversation_id, user=user
        ).first()
        if not conversation:
            return {"type": "assistant:error", "error": "Conversation not found"}

    if not conversation:
        title = message[:100]
        conversation = Conversation.objects.create(user=user, title=title)

    # Store user message
    Message.objects.create(
        conversation=conversation,
        role="user",
        content=message,
    )

    # Publish background job for LLM processing
    from mojo.apps import jobs
    jobs.publish(
        "mojo.apps.assistant.handler.execute_assistant_job",
        {
            "user_id": user.pk,
            "conversation_id": conversation.pk,
            "message": message,
        },
        channel="default",
    )

    return {
        "type": "assistant:thinking",
        "conversation_id": conversation.pk,
    }


# ---------------------------------------------------------------------------
# Background job entry point
# ---------------------------------------------------------------------------

def execute_assistant_job(job):
    """
    Job function: run the assistant agent and publish WS events.

    Called by the job engine with a Job model instance.
    Payload keys: user_id, conversation_id, message
    """
    from mojo.apps.account.models import User
    from mojo.apps.assistant.services.agent import run_assistant_ws

    payload = job.payload
    user_id = payload.get("user_id")
    conversation_id = payload.get("conversation_id")
    message = payload.get("message")

    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("Assistant job: user %s not found", user_id)
        return

    def on_event(event_type, data=None):
        """Publish WS events back to the user during processing."""
        from mojo.apps.realtime.manager import send_to_user
        event = {
            "type": f"assistant:{event_type}",
            "conversation_id": conversation_id,
        }
        if data:
            event.update(data)
        try:
            send_to_user("user", user_id, event)
        except Exception:
            logger.warning("Failed to publish WS event %s to user %s",
                           event_type, user_id)

    try:
        result = run_assistant_ws(user, message, conversation_id, on_event=on_event)

        if "error" in result:
            on_event("error", {"error": result["error"]})
        else:
            on_event("response", {
                "response": result["response"],
                "tool_calls_made": result.get("tool_calls_made", []),
            })
    except Exception:
        logger.exception("Assistant job failed for user %s", user_id)
        on_event("error", {"error": "Assistant encountered an unexpected error"})
