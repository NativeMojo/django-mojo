"""
Core LLM agent for the admin assistant.

Entry point: ``run_assistant(user, message, conversation_id=None)``

Flow:
    1. Check LLM_ADMIN_ENABLED
    2. Load or create Conversation, append user Message
    3. Build system prompt + tool list filtered by user perms
    4. Run Claude tool-calling loop with permission gate
    5. Store assistant response as Message(s)
    6. Return response dict
"""
import re
import ujson
from mojo.helpers.settings import settings
from mojo.helpers import logit

logger = logit.get_logger(__name__, "assistant.log")

# Regex to extract ```assistant_block ... ``` fences from LLM output
_BLOCK_RE = re.compile(
    r"```assistant_block\s*\n(.+?)\n\s*```",
    re.DOTALL,
)

VALID_BLOCK_TYPES = {"table", "chart", "stat"}


def _parse_blocks(text):
    """
    Extract structured data blocks from LLM response text.

    Returns (clean_text, blocks) where clean_text has the fences removed
    and blocks is a list of parsed dicts.
    """
    blocks = []
    for match in _BLOCK_RE.finditer(text):
        raw = match.group(1).strip()
        try:
            block = ujson.loads(raw)
            if isinstance(block, dict) and block.get("type") in VALID_BLOCK_TYPES:
                blocks.append(block)
        except Exception:
            logger.warning("Failed to parse assistant_block: %s", raw[:200])

    # Remove the fences from the text
    clean = _BLOCK_RE.sub("", text).strip()
    # Collapse multiple blank lines left by removed blocks
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, blocks


SYSTEM_PROMPT = """You are an admin assistant for a web application platform. You help administrators query and manage their system through natural language.

You have access to tools that let you query security incidents, events, jobs, users, groups, and metrics. Each tool call is checked against the requesting user's permissions.

## Guidelines
- Answer questions clearly and concisely using the data from your tools.
- When presenting data, summarize key findings and highlight anything unusual.
- For mutating operations (blocking IPs, canceling jobs, updating incidents), always confirm with the user before executing.
- If a tool call fails with a permission error, explain what permission the user needs.
- Bound your queries: use reasonable time ranges and limits. Don't query everything at once.
- Never expose passwords, auth keys, or other secrets — the tools already filter these out.
- If you don't have a tool for what the user is asking, say so clearly.

## Structured Data Blocks

When your response includes data that would be better shown as a table, chart, or stat card, include it as a structured JSON block using this exact format:

```assistant_block
{"type": "table", "title": "Failed Jobs", "columns": ["ID", "Function", "Error"], "rows": [["abc", "send_email", "timeout"]]}
```

Write your narrative text around the blocks normally. The blocks are extracted and rendered as visual components by the frontend.

### Block Types

**table** — for lists of records, query results, comparisons:
```assistant_block
{"type": "table", "title": "Open Incidents", "columns": ["ID", "Category", "Priority", "Status"], "rows": [[1, "auth", 8, "new"], [2, "ossec", 3, "investigating"]]}
```

**chart** — for time-series, trends, distributions:
```assistant_block
{"type": "chart", "chart_type": "line", "title": "Events (24h)", "labels": ["00:00", "06:00", "12:00", "18:00"], "series": [{"name": "events", "values": [12, 45, 32, 18]}]}
```
Supported chart_type values: line, bar, pie, area.

**stat** — for single key metrics, counts, rates:
```assistant_block
{"type": "stat", "items": [{"label": "Open Incidents", "value": 42}, {"label": "Failed Jobs (24h)", "value": 7}, {"label": "Active Users", "value": 156}]}
```

### Rules
- Always include narrative text — blocks supplement the text, they don't replace it.
- Use tables for 3+ rows of data. For 1-2 items, just describe them in text.
- Use stat blocks for dashboard-style overviews (system health, summaries).
- Use chart blocks when the user asks about trends or when time-series data is available.
- Keep table rows bounded — show the most relevant 20 rows max, mention the total if there are more.
- Column names should be human-readable (Title Case).
- The JSON must be valid and on a single line within the code fence.
"""


def _get_api_key():
    key = settings.get("LLM_ADMIN_API_KEY", None)
    if not key:
        key = settings.get("LLM_HANDLER_API_KEY", None)
    return key


def _get_model():
    return settings.get("LLM_ADMIN_MODEL", "claude-sonnet-4-6-20250514")


def _get_system_prompt():
    custom = settings.get("LLM_ADMIN_SYSTEM_PROMPT", None)
    return custom if custom else SYSTEM_PROMPT


def _call_claude(messages, system_prompt, tools):
    import anthropic

    client = anthropic.Anthropic(api_key=_get_api_key())
    response = client.messages.create(
        model=_get_model(),
        max_tokens=4096,
        system=system_prompt,
        tools=tools,
        messages=messages,
    )
    return response.model_dump()


def _build_conversation_messages(conversation, max_history):
    """Load previous messages from the conversation into Claude message format."""
    from mojo.apps.assistant.models import Message

    messages = []
    recent = Message.objects.filter(
        conversation=conversation
    ).order_by("-created")[:max_history]

    # Reverse to chronological order
    recent = list(reversed(recent))

    for msg in recent:
        if msg.role == "user":
            messages.append({"role": "user", "content": msg.content})
        elif msg.role == "assistant":
            content = []
            if msg.content:
                content.append({"type": "text", "text": msg.content})
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    content.append(tc)
            if content:
                messages.append({"role": "assistant", "content": content})
        elif msg.role == "tool_result":
            if msg.tool_calls:
                messages.append({"role": "user", "content": msg.tool_calls})

    return messages


def run_assistant(user, message, conversation_id=None, on_event=None):
    """
    Main entry point for the admin assistant.

    Args:
        user:            The requesting User instance.
        message:         The user's natural language message.
        conversation_id: Optional existing conversation to continue.
        on_event:        Optional callback ``(event_type, data_dict)`` for
                         live progress events (used by the WS handler).
                         Events: ``tool_call``, ``thinking``.

    Returns:
        dict with keys: response, conversation_id, tool_calls_made, error
    """
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant import get_registry, get_tools_for_user

    # Check feature flag
    if not settings.get("LLM_ADMIN_ENABLED", False, kind="bool"):
        return {"error": "Assistant is not enabled", "status_code": 404}

    # Check API key
    api_key = _get_api_key()
    if not api_key:
        return {"error": "LLM API key not configured", "status_code": 503}

    # Load or create conversation
    conversation = None
    if conversation_id:
        try:
            conversation = Conversation.objects.get(pk=conversation_id, user=user)
        except Conversation.DoesNotExist:
            return {"error": "Conversation not found", "status_code": 404}

    if not conversation:
        title = message[:100] if message else "New conversation"
        conversation = Conversation.objects.create(user=user, title=title)

    # Store user message
    Message.objects.create(
        conversation=conversation,
        role="user",
        content=message,
    )

    # Build messages from history
    max_history = settings.get("LLM_ADMIN_MAX_HISTORY", 50, kind="int")
    messages = _build_conversation_messages(conversation, max_history)

    # Get tools the user has permission for
    tools = get_tools_for_user(user)
    if not tools:
        response_text = "You don't have permissions for any assistant tools."
        Message.objects.create(
            conversation=conversation,
            role="assistant",
            content=response_text,
        )
        return {
            "response": response_text,
            "conversation_id": conversation.pk,
            "tool_calls_made": [],
        }

    # Run agent loop
    system_prompt = _get_system_prompt()
    max_turns = settings.get("LLM_ADMIN_MAX_TURNS", 25, kind="int")
    registry = get_registry()
    tool_calls_made = []

    try:
        for _ in range(max_turns):
            result = _call_claude(messages, system_prompt, tools)
            stop_reason = result.get("stop_reason")

            # Add assistant response to messages
            messages.append({"role": "assistant", "content": result["content"]})

            if stop_reason != "tool_use":
                # Agent is done — extract text response
                text_parts = []
                for block in result["content"]:
                    if block.get("type") == "text":
                        text_parts.append(block["text"])

                raw_text = "\n".join(text_parts) if text_parts else ""
                response_text, blocks = _parse_blocks(raw_text)

                # Store full response (with block fences) for audit
                Message.objects.create(
                    conversation=conversation,
                    role="assistant",
                    content=raw_text,
                    tool_calls=result["content"] if any(
                        b.get("type") == "tool_use" for b in result["content"]
                    ) else None,
                )

                return {
                    "response": response_text,
                    "blocks": blocks,
                    "conversation_id": conversation.pk,
                    "tool_calls_made": tool_calls_made,
                }

            # Process tool calls with permission gate
            tool_results = []
            for block in result["content"]:
                if block.get("type") != "tool_use":
                    continue

                tool_name = block["name"]
                tool_input = block["input"]
                tool_id = block["id"]

                tool_entry = registry.get(tool_name)
                if not tool_entry:
                    tool_result = {"error": f"Unknown tool: {tool_name}"}
                    logger.warning("LLM requested unknown tool: %s", tool_name)
                elif not user.has_permission(tool_entry["permission"]):
                    perm = tool_entry["permission"]
                    tool_result = {
                        "error": f"Permission denied. You need '{perm}' to use {tool_name}."
                    }
                    logger.info("Permission denied for tool %s, user %s needs %s",
                                tool_name, user.pk, perm)
                else:
                    try:
                        if on_event:
                            on_event("tool_call", {
                                "tool": tool_name,
                                "input": tool_input,
                            })
                        tool_result = tool_entry["handler"](tool_input, user)
                        tool_calls_made.append({
                            "tool": tool_name,
                            "input": tool_input,
                        })
                    except Exception:
                        logger.exception("Tool %s failed", tool_name)
                        tool_result = {"error": f"Tool '{tool_name}' encountered an internal error."}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": ujson.dumps(tool_result),
                })

            # Store tool interaction messages
            Message.objects.create(
                conversation=conversation,
                role="assistant",
                content="",
                tool_calls=result["content"],
            )
            Message.objects.create(
                conversation=conversation,
                role="tool_result",
                content="",
                tool_calls=tool_results,
            )

            messages.append({"role": "user", "content": tool_results})

        # Hit max turns
        response_text = "I've reached the maximum number of tool calls for this request. Please try a more specific query."
        Message.objects.create(
            conversation=conversation,
            role="assistant",
            content=response_text,
        )
        return {
            "response": response_text,
            "conversation_id": conversation.pk,
            "tool_calls_made": tool_calls_made,
        }

    except Exception as e:
        logger.exception("Assistant agent failed for user %s", user.pk)
        return {
            "error": f"Assistant encountered an error: {str(e)}",
            "conversation_id": conversation.pk,
            "status_code": 500,
        }


def run_assistant_ws(user, message, conversation_id, on_event=None):
    """
    WebSocket variant — conversation already exists and user message
    is already stored by the WS handler.  Skips conversation creation
    and message storage, delegates to the core loop.
    """
    from mojo.apps.assistant.models import Conversation, Message
    from mojo.apps.assistant import get_registry, get_tools_for_user

    # Check feature flag
    if not settings.get("LLM_ADMIN_ENABLED", False, kind="bool"):
        return {"error": "Assistant is not enabled"}

    api_key = _get_api_key()
    if not api_key:
        return {"error": "LLM API key not configured"}

    try:
        conversation = Conversation.objects.get(pk=conversation_id, user=user)
    except Conversation.DoesNotExist:
        return {"error": "Conversation not found"}

    # Build messages from history (user message already stored by handler)
    max_history = settings.get("LLM_ADMIN_MAX_HISTORY", 50, kind="int")
    messages = _build_conversation_messages(conversation, max_history)

    tools = get_tools_for_user(user)
    if not tools:
        response_text = "You don't have permissions for any assistant tools."
        Message.objects.create(
            conversation=conversation, role="assistant", content=response_text,
        )
        return {"response": response_text, "conversation_id": conversation.pk, "tool_calls_made": []}

    system_prompt = _get_system_prompt()
    max_turns = settings.get("LLM_ADMIN_MAX_TURNS", 25, kind="int")
    registry = get_registry()
    tool_calls_made = []

    try:
        for _ in range(max_turns):
            result = _call_claude(messages, system_prompt, tools)
            stop_reason = result.get("stop_reason")
            messages.append({"role": "assistant", "content": result["content"]})

            if stop_reason != "tool_use":
                text_parts = [b["text"] for b in result["content"] if b.get("type") == "text"]
                raw_text = "\n".join(text_parts) if text_parts else ""
                response_text, blocks = _parse_blocks(raw_text)
                Message.objects.create(
                    conversation=conversation, role="assistant", content=raw_text,
                )
                return {
                    "response": response_text,
                    "blocks": blocks,
                    "conversation_id": conversation.pk,
                    "tool_calls_made": tool_calls_made,
                }

            # Process tool calls with permission gate
            tool_results = []
            for block in result["content"]:
                if block.get("type") != "tool_use":
                    continue

                tool_name = block["name"]
                tool_input = block["input"]
                tool_id = block["id"]

                tool_entry = registry.get(tool_name)
                if not tool_entry:
                    tool_result = {"error": f"Unknown tool: {tool_name}"}
                elif not user.has_permission(tool_entry["permission"]):
                    perm = tool_entry["permission"]
                    tool_result = {"error": f"Permission denied. You need '{perm}' to use {tool_name}."}
                else:
                    try:
                        if on_event:
                            on_event("tool_call", {"tool": tool_name, "input": tool_input})
                        tool_result = tool_entry["handler"](tool_input, user)
                        tool_calls_made.append({"tool": tool_name, "input": tool_input})
                    except Exception:
                        logger.exception("Tool %s failed", tool_name)
                        tool_result = {"error": f"Tool '{tool_name}' encountered an internal error."}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": ujson.dumps(tool_result),
                })

            Message.objects.create(
                conversation=conversation, role="assistant", content="", tool_calls=result["content"],
            )
            Message.objects.create(
                conversation=conversation, role="tool_result", content="", tool_calls=tool_results,
            )
            messages.append({"role": "user", "content": tool_results})

        response_text = "I've reached the maximum number of tool calls for this request. Please try a more specific query."
        Message.objects.create(conversation=conversation, role="assistant", content=response_text)
        return {"response": response_text, "conversation_id": conversation.pk, "tool_calls_made": tool_calls_made}

    except Exception:
        logger.exception("Assistant WS agent failed for user %s", user.pk)
        return {"error": "Assistant encountered an unexpected error"}
