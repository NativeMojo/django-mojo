"""
WebSocket message handler for the admin assistant.

Handles assistant-related message types routed from the User model's
on_realtime_message hook. LLM processing runs as a background job
so the WebSocket handler returns immediately.

Message types (client → server):
  - assistant_message: Send a new message to the assistant

Response types (server → client, via realtime publish):
  - assistant_thinking:   Processing has started
  - assistant_tool_call:  A tool was called (sent per tool)
  - assistant_response:   Final LLM response
  - assistant_error:      Something went wrong

Reliability guarantees:
  - The user ALWAYS receives either assistant_response or assistant_error.
  - Every exception path publishes assistant_error back to the user.
  - All stages are logged to assistant.log for debugging.
"""
from mojo.helpers import logit

logger = logit.get_logger("assistant", "assistant.log")

ASSISTANT_MESSAGE_TYPES = {
    "assistant_message",
}


def _send_ws_event(user_id, event_type, conversation_id, data=None):
    """
    Publish a WS event to the user. Never raises — logs failures instead.
    This is the single point through which all WS messages to the client flow.

    Uses send_event_to_user so the client receives the event directly
    (e.g., {"type": "assistant_response", ...}) without the
    {"type": "message", "data": ...} wrapper that send_to_user adds.
    This ensures background-thread events arrive in the same format
    as the immediate handler return (assistant_thinking).
    """
    from mojo.apps.realtime.manager import send_event_to_user
    event = {
        "type": f"assistant_{event_type}",
        "conversation_id": conversation_id,
    }
    if data:
        event.update(data)
    try:
        send_event_to_user("user", user_id, event)
    except Exception:
        logger.exception("Failed to send WS event '%s' to user %s (conv %s)",
                         event_type, user_id, conversation_id)


def handle_assistant_message(user, data):
    """
    Main entry point called from User.on_realtime_message.

    Validates the request, stores the user message, publishes a
    background job, and returns an immediate ack.
    """
    message_type = data.get("type") or data.get("action")

    if message_type == "assistant_message":
        try:
            return _handle_message(user, data)
        except Exception:
            logger.exception("assistant handler crashed for user %s", user.pk)
            return {"type": "assistant_error", "error": "Failed to process message. Please try again."}

    return {"type": "assistant_error", "error": f"Unknown assistant message type: {message_type}"}


def _handle_message(user, data):
    """Handle a new assistant message — validate, enqueue, ack."""
    from mojo.helpers.settings import settings
    from mojo.apps.assistant.models import Conversation, Message

    # Check feature flag
    if not settings.get("LLM_ADMIN_ENABLED", False, kind="bool"):
        logger.info("assistant: feature disabled, user %s", user.pk)
        return {"type": "assistant_error", "error": "Assistant is not enabled. Set LLM_ADMIN_ENABLED=True in settings."}

    # Check permission
    if not user.has_permission("view_admin"):
        logger.info("assistant: permission denied for user %s", user.pk)
        return {"type": "assistant_error", "error": "Permission denied. You need 'view_admin' permission."}

    message = (data.get("message") or "").strip()
    if not message:
        return {"type": "assistant_error", "error": "Message is required"}

    conversation_id = data.get("conversation_id")

    # Load or create conversation
    conversation = None
    if conversation_id:
        conversation = Conversation.objects.filter(
            pk=conversation_id, user=user
        ).first()
        if not conversation:
            return {"type": "assistant_error", "error": "Conversation not found"}

    if not conversation:
        title = message[:100]
        conversation = Conversation.objects.create(user=user, title=title)

    # Store user message
    Message.objects.create(
        conversation=conversation,
        role="user",
        content=message,
    )

    # Pre-flight check: API key configured?
    from mojo.helpers import llm
    if not llm.get_api_key():
        logger.error("assistant: no API key configured")
        return {
            "type": "assistant_error",
            "conversation_id": conversation.pk,
            "error": "LLM API key is not configured. Set LLM_ADMIN_API_KEY or LLM_HANDLER_API_KEY.",
        }

    # Run the agent in a background thread — no job engine dependency.
    # The handler returns assistant_thinking immediately, and the thread
    # publishes assistant_response or assistant_error when done.
    import threading
    thread = threading.Thread(
        target=_run_agent_thread,
        args=(user.pk, conversation.pk, message),
        daemon=True,
    )
    thread.start()
    logger.info("assistant: thread started for user %s, conv %s",
                user.pk, conversation.pk)

    return {
        "type": "assistant_thinking",
        "conversation_id": conversation.pk,
    }


# ---------------------------------------------------------------------------
# Background thread — runs the LLM agent and publishes WS events
# ---------------------------------------------------------------------------

def _run_agent_thread(user_id, conversation_id, message):
    """
    Run the assistant agent in a background thread.

    RELIABILITY CONTRACT: This function ALWAYS publishes either
    assistant_response or assistant_error back to the user's WS.
    Every code path ends with a WS event — no silent failures.
    """
    import django
    # Ensure Django is ready (thread may not have it set up)
    try:
        django.setup()
    except Exception:
        pass

    from mojo.apps.account.models import User
    from mojo.apps.assistant.services.agent import run_assistant_ws

    logger.info("assistant thread started: user=%s conv=%s", user_id, conversation_id)

    # --- Load user ---
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        logger.error("assistant thread: user %s not found", user_id)
        _send_ws_event(user_id, "error", conversation_id,
                       {"error": "Your user account was not found."})
        return
    except Exception:
        logger.exception("assistant thread: failed to load user %s", user_id)
        _send_ws_event(user_id, "error", conversation_id,
                       {"error": "Failed to load user account."})
        return

    def on_event(event_type, data=None):
        """Publish WS events back to the user during processing."""
        _send_ws_event(user_id, event_type, conversation_id, data)

    # --- Run agent ---
    try:
        result = run_assistant_ws(user, message, conversation_id, on_event=on_event)
    except Exception:
        logger.exception("assistant thread: agent crashed for user %s conv %s",
                         user_id, conversation_id)
        on_event("error", {"error": "The assistant encountered an unexpected error. Please try again."})
        return

    # --- Deliver result ---
    try:
        if "error" in result:
            logger.warning("assistant thread: agent returned error for user %s: %s",
                           user_id, result["error"])
            on_event("error", {"error": result["error"]})
        else:
            response_data = {
                "message_id": result.get("message_id"),
                "created": result.get("created"),
                "response": result.get("response", ""),
                "blocks": result.get("blocks") or None,
                "tool_calls_made": result.get("tool_calls_made", []),
            }
            on_event("response", response_data)
            logger.info("assistant thread completed: user=%s conv=%s tools=%d",
                        user_id, conversation_id, len(response_data["tool_calls_made"]))
    except Exception:
        logger.exception("assistant thread: failed to deliver result to user %s", user_id)
        on_event("error", {"error": "Failed to deliver response. Please check conversation history."})
