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
import uuid
import ujson
from mojo.helpers.settings import settings
from mojo.helpers import logit, llm

logger = logit.get_logger(__name__, "assistant.log")


def _report_event(category, level, title, details, user=None, **kwargs):
    """Report an incident event. Never raises — logs failures instead."""
    try:
        from mojo.apps.incident import report_event
        extra = {}
        if user:
            extra["uid"] = user.pk
            extra["model_name"] = "account.User"
            extra["model_id"] = user.pk
        extra.update(kwargs)
        report_event(details, title=title, category=category, level=level, **extra)
    except Exception:
        logger.exception("Failed to report event: %s / %s", category, title)


# Regex to extract ```assistant_block ... ``` fences from LLM output
_BLOCK_RE = re.compile(
    r"```assistant_block\s*\n(.+?)\n\s*```",
    re.DOTALL,
)

VALID_BLOCK_TYPES = {"table", "chart", "stat", "action", "list", "alert", "progress"}

VALID_ALERT_LEVELS = {"info", "success", "warning", "error"}


def _validate_block(block):
    """
    Validate a parsed block dict beyond just type membership.

    Returns True if the block is valid and should be included,
    False if it should be silently dropped.
    """
    block_type = block.get("type")
    if block_type not in VALID_BLOCK_TYPES:
        return False
    if block_type == "action":
        actions = block.get("actions")
        if not isinstance(actions, list) or not actions:
            return False
        # Tag with a unique action_id for frontend tracking
        block["action_id"] = str(uuid.uuid4())
    elif block_type == "alert":
        if block.get("level") not in VALID_ALERT_LEVELS:
            return False
        if not block.get("message"):
            return False
    elif block_type == "list":
        items = block.get("items")
        if not isinstance(items, list) or not items:
            return False
    return True


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
            if isinstance(block, dict) and _validate_block(block):
                blocks.append(block)
        except Exception:
            logger.warning("Failed to parse assistant_block: %s", raw[:200])

    # Remove the fences from the text
    clean = _BLOCK_RE.sub("", text).strip()
    # Collapse multiple blank lines left by removed blocks
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return clean, blocks


SYSTEM_PROMPT = """You are an admin assistant for a web application platform. You help administrators query and manage their system through natural language.

You have access to tools for querying and managing the system. Each tool call is checked against the requesting user's permissions.

## Guidelines
- Answer questions clearly and concisely using the data from your tools.
- When presenting data, summarize key findings and highlight anything unusual.
- For mutating operations (blocking IPs, canceling jobs, updating incidents), always confirm with the user before executing.
- If a tool call fails with a permission error, explain what permission the user needs.
- Bound your queries: use reasonable time ranges and limits. Don't query everything at once.
- Never expose passwords, auth keys, or other secrets — the tools already filter these out.
- If you don't have a tool for what the user is asking, say so clearly.
- If a tool returns empty results, that means there is no matching data — it does NOT mean the tool is broken.

## Tool Loading

You start with core tools (memory, models, docs, web, logs, files). For domain-specific work, call `load_tools` to discover and load additional tools.

- **Discover domains**: Call `load_tools()` with no arguments to see available domains (security, jobs, users, groups, metrics) with descriptions and tool counts.
- **Load a domain**: Call `load_tools(domain="security")` or `load_tools(domains=["security", "jobs"])` to load domain tools. Loaded tools persist for the rest of this conversation.
- **Auto-load when clear**: When the user's request clearly maps to a domain (e.g., "show me failed jobs" → jobs, "who logged in today?" → users), load the domain tools without asking.
- **Ask when ambiguous**: When the request is vague (e.g., "something seems off"), ask the user which area to investigate before loading.
- **Prefer dedicated tools**: Once a domain is loaded, prefer its dedicated tools over query_model. Dedicated tools return curated, optimized output — query_model returns raw data that is noisier and uses more tokens.

## Memory

You have persistent memory that carries across conversations. Memories are shown above in the ## Memory section (if any exist). You can store, update, and delete memories using the memory tools.

### When to Store Memories

**Global tier** (platform-wide, visible to all assistant users):
- Platform identity: what this application is, cloud provider, regions
- Infrastructure rules: IP ranges to never block, critical services
- Escalation rules: who handles what, required response procedures
- Only store facts the user explicitly states. These are slow-changing, universal truths.

**User tier** (personal, follows the user):
- Communication preferences, focus areas, working patterns
- Personal shorthand or terminology
- Ask before storing implicit observations — confirm with the user first.

**Group tier** (tenant-specific, visible to group members):
- Operational rules: deploy windows, maintenance schedules
- Compliance requirements, SLAs, team conventions
- Only store when a group member states something specific to their group.

### Memory Rules
- One fact per entry, 1-2 sentences max.
- If you can look it up with a tool, don't memorize it.
- Check existing memories before writing — update rather than duplicate.
- Memories are hints, not commands. When acting on a memory for a critical decision, verify with a tool first.
- Never store passwords, API keys, tokens, or credentials.

## Structured Data Blocks

When your response includes data that would be better shown as a table, chart, or stat card, include it as a structured JSON block using this exact format:

```assistant_block
{"type": "table", "title": "Failed Jobs", "columns": ["ID", "Function", "Error"], "rows": [["abc", "send_email", "timeout"]]}
```

The blocks are extracted and rendered as rich visual components by the frontend alongside your text.

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

**action** — for mutating operations that need user confirmation:
```assistant_block
{"type": "action", "title": "Block IP", "description": "Block 1.2.3.4 on all firewall sets for 24 hours", "actions": [{"label": "Confirm", "value": "confirm"}, {"label": "Cancel", "value": "cancel"}]}
```
Use when you need user confirmation before executing a mutating operation (blocking IPs, disabling users, canceling jobs, etc.). Always include a Cancel option. The user clicks a button and their choice is sent back as a message. Do not execute the operation until you receive confirmation.

**list** — for single-record details, key/value summaries:
```assistant_block
{"type": "list", "title": "User Detail", "items": [{"label": "Email", "value": "admin@example.com"}, {"label": "Role", "value": "Admin"}, {"label": "Last Login", "value": "2024-01-15 09:30 UTC"}]}
```
Use for single-record summaries instead of a 1-row table. Prefer this for user profiles, incident details, job info, and any single object with multiple fields.

**alert** — for warnings, errors, and important notices:
```assistant_block
{"type": "alert", "level": "warning", "title": "Rate Limited", "message": "User exceeded 100 req/min threshold. Current rate: 142 req/min."}
```
Supported level values: info, success, warning, error. Use for permission denials, important warnings, error conditions, and success confirmations that need visual distinction from narrative text. Don't overuse — reserve for genuinely important information.

### Rules
- Always include brief narrative text — a sentence or two of context, key takeaways, or warnings. Do NOT repeat the data that is already in the blocks. The blocks carry the detail; the text provides interpretation.
- Use tables for 3+ rows of data. For 1-2 items, just describe them in text or use a list block.
- Use list blocks for single-record details — never use a table with 1 row.
- Use stat blocks for dashboard-style overviews (system health, summaries).
- Use chart blocks when the user asks about trends or when time-series data is available.
- Use action blocks for confirmations — never ask "type yes to confirm" when an action block is appropriate.
- Use alert blocks sparingly — only for genuinely important warnings, errors, or status changes.
- Keep table rows bounded — show the most relevant 20 rows max, mention the total if there are more.
- Column names should be human-readable (Title Case).
- The JSON must be valid and on a single line within the code fence.

## Task Planning

For complex requests that require 3+ tool calls across different areas, create a plan first using the create_plan tool. This shows the user a progress tracker so they can see what you're doing.

1. Call create_plan with a title and list of steps. Mark independent steps as parallel=true with their tool name and tool_input — the system will execute them concurrently for faster results.
2. For sequential steps you handle yourself: call update_plan(step_id, "in_progress") before starting, then update_plan(step_id, "done", summary="...") when complete.
3. After all steps complete, synthesize your findings into a final response.

Don't create a plan for simple queries — if the user asks "how many open incidents?" just call the tool directly.

### When to Plan
- "Give me a security audit" — plan with parallel steps for incidents, events, blocked IPs, etc.
- "What's the system health?" — plan with parallel steps for jobs, metrics, incidents.
- "Investigate this user" — plan with steps for user detail, activity, rate limits, related incidents.

### When NOT to Plan
- Single-tool queries: "show me open incidents"
- Follow-up questions in a conversation: "what about the last 7 days?"
- Simple mutations: "block this IP"

### Parallel Steps
When steps are independent (no data dependencies), mark them parallel=true and include the tool and tool_input. The system executes these concurrently — you don't need to call them yourself. Only the final synthesis step should be sequential (parallel=false, no tool field).
"""


ONBOARDING_PROMPT = """

## Getting Started

This is a new deployment — no platform memories have been stored yet. To serve you better in future conversations, ask the user about:
- What kind of application/platform this is (e.g., "Healthcare SaaS", "E-commerce marketplace")
- Infrastructure and environment (cloud provider, regions, key services)
- Any critical safety rules ("never block these IP ranges", "always escalate PCI events")
- Key operational patterns worth remembering

Store their answers as global memories using the write_memory tool. This only needs to happen once — future conversations will have this context automatically."""


def _get_system_prompt(user=None, group=None):
    custom = settings.get("LLM_ADMIN_SYSTEM_PROMPT", None)
    base = custom if custom else SYSTEM_PROMPT

    # Inject memory or onboarding prompt
    if not settings.get("LLM_ADMIN_MEMORY_ENABLED", True, kind="bool"):
        return base

    try:
        from mojo.apps.assistant.services.memory import build_memory_prompt, is_global_empty
        memory_section = build_memory_prompt(user, group=group) if user else ""
        if memory_section:
            return base + "\n\n" + memory_section
        # If global memory is empty, inject onboarding
        if user and is_global_empty():
            return base + ONBOARDING_PROMPT
    except Exception:
        logger.exception("Failed to build memory prompt")

    return base


def _build_tools_for_conversation(user, conversation, messages):
    """
    Build the tool list for a conversation.

    - New conversations: core tools only.
    - Resumed conversations with active_domains: core + those domains.
    - Old conversations (pre-two-tier) with tool_use in history: all tools.
    """
    from mojo.apps.assistant import (
        get_core_tools_for_user, get_domain_tools_for_user, get_tools_for_user,
    )

    active_domains = (conversation.metadata or {}).get("active_domains", [])

    if active_domains:
        # Resumed conversation with previously loaded domains
        tools = get_core_tools_for_user(user)
        domain_tools = get_domain_tools_for_user(user, active_domains)
        # Deduplicate (core tools might overlap with domain)
        core_names = {t["name"] for t in tools}
        for dt in domain_tools:
            if dt["name"] not in core_names:
                tools.append(dt)
        return tools

    # Check if history contains tool_use blocks (old conversation, pre-two-tier)
    has_tool_use = any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_use" for b in m["content"] if isinstance(b, dict))
        for m in messages
        if m.get("role") == "assistant"
    )
    if has_tool_use:
        # Backward compat — send all tools so history references resolve
        return get_tools_for_user(user)

    # New conversation — core tools only
    return get_core_tools_for_user(user)


def _handle_load_tools(conversation, tool_input, tools, user):
    """
    Handle a load_tools call: update conversation metadata and inject
    domain tools into the active tools list.

    Returns the list of newly added tool names (for logging).
    """
    from mojo.apps.assistant import get_domain_tools_for_user, get_registry

    # Collect requested domains
    requested = []
    if tool_input.get("domain"):
        requested.append(tool_input["domain"])
    if tool_input.get("domains"):
        requested.extend(tool_input["domains"])

    if not requested:
        return []  # Listing mode, no domains to load

    # Validate against known domains in the registry
    known_domains = {entry["domain"] for entry in get_registry().values()}
    requested = [d for d in requested if d in known_domains]
    if not requested:
        return []  # No valid domains

    # Update conversation metadata
    metadata = conversation.metadata or {}
    active = metadata.get("active_domains", [])
    new_domains = [d for d in set(requested) if d not in active]
    if not new_domains:
        return []  # Already loaded

    active.extend(new_domains)
    metadata["active_domains"] = active
    conversation.metadata = metadata
    conversation.save(update_fields=["metadata"])

    # Inject new domain tools into the active tools list
    domain_tools = get_domain_tools_for_user(user, new_domains)
    existing_names = {t["name"] for t in tools}
    added = []
    for dt in domain_tools:
        if dt["name"] not in existing_names:
            tools.append(dt)
            added.append(dt["name"])

    return added


def _handle_plan_tool(conversation, tool_name, tool_input, tool_result, on_event):
    """
    Handle create_plan and update_plan meta-tool calls.

    create_plan: stores plan in conversation metadata, publishes WS event.
    update_plan: updates a step in the stored plan, publishes WS event.

    Returns True if the tool was handled as a plan tool, False otherwise.
    """
    if tool_name == "create_plan":
        if isinstance(tool_result, dict) and "plan_id" in tool_result:
            metadata = conversation.metadata or {}
            metadata["plan"] = tool_result
            conversation.metadata = metadata
            conversation.save(update_fields=["metadata"])
            if on_event:
                on_event("plan", {"plan": tool_result})
            logger.info("Plan created: %s (conv=%s)", tool_result["plan_id"], conversation.pk)
        return True

    if tool_name == "update_plan":
        if isinstance(tool_result, dict) and tool_result.get("updated"):
            metadata = conversation.metadata or {}
            plan = metadata.get("plan")
            if plan:
                step_id = tool_result["step_id"]
                for step in plan.get("steps", []):
                    if step["id"] == step_id:
                        step["status"] = tool_result["status"]
                        if tool_result.get("summary"):
                            step["summary"] = tool_result["summary"]
                        break
                metadata["plan"] = plan
                conversation.metadata = metadata
                conversation.save(update_fields=["metadata"])
                if on_event:
                    on_event("plan_update", {
                        "plan_id": plan["plan_id"],
                        "step_id": step_id,
                        "status": tool_result["status"],
                        "summary": tool_result.get("summary"),
                    })
        return True

    return False


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
    from mojo.apps.assistant import get_registry

    # Check feature flag
    if not settings.get("LLM_ADMIN_ENABLED", False, kind="bool"):
        return {"error": "Assistant is not enabled", "status_code": 404}

    # Check API key
    if not llm.get_api_key():
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

    # Build tool list — two-tier: core + active domains, or all for old conversations
    tools = _build_tools_for_conversation(user, conversation, messages)
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

    # Resolve group from conversation for memory injection
    conv_group = getattr(conversation, "group", None)

    # Attach group to user for tool access (tools read user._assistant_group)
    user._assistant_group = conv_group

    # Run agent loop
    system_prompt = _get_system_prompt(user=user, group=conv_group)
    max_turns = settings.get("LLM_ADMIN_MAX_TURNS", 25, kind="int")
    registry = get_registry()
    tool_calls_made = []

    try:
        for _ in range(max_turns):
            result = llm.call(messages, system=system_prompt, tools=tools)
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

                Message.objects.create(
                    conversation=conversation,
                    role="assistant",
                    content=response_text,
                    blocks=blocks or None,
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
                    _report_event(
                        "assistant:permission_denied", 6,
                        f"Unknown tool requested: {tool_name}",
                        f"LLM requested tool '{tool_name}' which is not in the registry. "
                        f"User: {user.email} (id={user.pk}), conv={conversation.pk}",
                        user=user,
                    )
                elif not user.has_permission(tool_entry["permission"]):
                    perm = tool_entry["permission"]
                    tool_result = {
                        "error": f"Permission denied. You need '{perm}' to use {tool_name}."
                    }
                    logger.info("Permission denied for tool %s, user %s needs %s",
                                tool_name, user.pk, perm)
                    _report_event(
                        "assistant:permission_denied", 5,
                        f"Permission denied: {tool_name}",
                        f"User {user.email} (id={user.pk}) denied access to tool '{tool_name}' "
                        f"(requires '{perm}'). conv={conversation.pk}",
                        user=user,
                    )
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
                        # Handle meta-tools — side effects in the agent loop
                        if tool_name == "load_tools":
                            added = _handle_load_tools(
                                conversation, tool_input, tools, user,
                            )
                            if added:
                                logger.info("Loaded %d tools for domains via load_tools, conv=%s",
                                            len(added), conversation.pk)
                        _handle_plan_tool(
                            conversation, tool_name, tool_input, tool_result, on_event,
                        )
                        # Report events for successful mutating tool calls
                        if tool_entry.get("mutates") and (
                            not isinstance(tool_result, dict) or "error" not in tool_result
                        ):
                            _report_event(
                                f"assistant:tool:{tool_name}", 5,
                                f"Assistant tool: {tool_name}",
                                f"User {user.email} (id={user.pk}) executed mutating tool "
                                f"'{tool_name}'. conv={conversation.pk}",
                                user=user,
                            )
                    except Exception:
                        logger.exception("Tool %s failed", tool_name)
                        tool_result = {"error": f"Tool '{tool_name}' encountered an internal error."}
                        _report_event(
                            "assistant:error", 6,
                            f"Tool exception: {tool_name}",
                            f"Tool '{tool_name}' raised an exception for user {user.email} "
                            f"(id={user.pk}). conv={conversation.pk}",
                            user=user,
                        )

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
        logger.warning("Max turns reached for user %s, conv %s", user.pk, conversation.pk)
        _report_event(
            "assistant:error", 5,
            "Max tool turns exhausted",
            f"Agent hit {max_turns} turn limit for user {user.email} (id={user.pk}). "
            f"conv={conversation.pk}. Tools called: {len(tool_calls_made)}",
            user=user,
        )
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
        err_str = str(e)
        if "not_found_error" in err_str or "404" in err_str:
            error = f"LLM model not found. Check LLM_ADMIN_MODEL setting. ({err_str[:200]})"
            _report_event("assistant:error:api", 7, "LLM model not found", err_str[:500], user=user)
        elif "authentication_error" in err_str or "401" in err_str:
            error = "LLM API key is invalid. Check LLM_ADMIN_API_KEY setting."
            _report_event("assistant:error:api", 7, "LLM API auth failure", err_str[:500], user=user)
        elif "rate_limit" in err_str.lower() or "429" in err_str:
            error = "LLM API rate limit reached. Please wait a moment and try again."
            _report_event("assistant:error:api", 5, "LLM API rate limit", err_str[:500], user=user)
        else:
            error = f"Assistant error: {err_str[:200]}"
            _report_event(
                "assistant:error", 7,
                "Agent loop exception",
                f"Agent crashed for user {user.email} (id={user.pk}). "
                f"conv={conversation.pk}. Error: {err_str[:500]}",
                user=user,
            )
        return {
            "error": error,
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
    from mojo.apps.assistant import get_registry

    # Check feature flag
    if not settings.get("LLM_ADMIN_ENABLED", False, kind="bool"):
        return {"error": "Assistant is not enabled"}

    if not llm.get_api_key():
        return {"error": "LLM API key not configured"}

    try:
        conversation = Conversation.objects.get(pk=conversation_id, user=user)
    except Conversation.DoesNotExist:
        return {"error": "Conversation not found"}

    # Build messages from history (user message already stored by handler)
    max_history = settings.get("LLM_ADMIN_MAX_HISTORY", 50, kind="int")
    messages = _build_conversation_messages(conversation, max_history)

    # Build tool list — two-tier: core + active domains, or all for old conversations
    tools = _build_tools_for_conversation(user, conversation, messages)
    if not tools:
        response_text = "You don't have permissions for any assistant tools."
        Message.objects.create(
            conversation=conversation, role="assistant", content=response_text,
        )
        return {"response": response_text, "conversation_id": conversation.pk, "tool_calls_made": []}

    # Resolve group from conversation for memory injection
    conv_group = getattr(conversation, "group", None)

    # Attach group to user for tool access
    user._assistant_group = conv_group

    system_prompt = _get_system_prompt(user=user, group=conv_group)
    max_turns = settings.get("LLM_ADMIN_MAX_TURNS", 25, kind="int")
    registry = get_registry()
    tool_calls_made = []

    try:
        for _ in range(max_turns):
            result = llm.call(messages, system=system_prompt, tools=tools)
            stop_reason = result.get("stop_reason")
            messages.append({"role": "assistant", "content": result["content"]})

            if stop_reason != "tool_use":
                text_parts = [b["text"] for b in result["content"] if b.get("type") == "text"]
                raw_text = "\n".join(text_parts) if text_parts else ""
                response_text, blocks = _parse_blocks(raw_text)
                msg = Message.objects.create(
                    conversation=conversation, role="assistant",
                    content=response_text, blocks=blocks or None,
                )
                return {
                    "response": response_text,
                    "blocks": blocks,
                    "message_id": msg.pk,
                    "created": msg.created.isoformat(),
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
                    _report_event(
                        "assistant:permission_denied", 6,
                        f"Unknown tool requested: {tool_name}",
                        f"LLM requested tool '{tool_name}' not in registry. "
                        f"User: {user.email} (id={user.pk}), conv={conversation.pk}",
                        user=user,
                    )
                elif not user.has_permission(tool_entry["permission"]):
                    perm = tool_entry["permission"]
                    tool_result = {"error": f"Permission denied. You need '{perm}' to use {tool_name}."}
                    _report_event(
                        "assistant:permission_denied", 5,
                        f"Permission denied: {tool_name}",
                        f"User {user.email} (id={user.pk}) denied access to tool '{tool_name}' "
                        f"(requires '{perm}'). conv={conversation.pk}",
                        user=user,
                    )
                else:
                    try:
                        if on_event:
                            on_event("tool_call", {"tool": tool_name, "input": tool_input})
                        tool_result = tool_entry["handler"](tool_input, user)
                        tool_calls_made.append({"tool": tool_name, "input": tool_input})
                        # Handle meta-tools — side effects in the agent loop
                        if tool_name == "load_tools":
                            added = _handle_load_tools(
                                conversation, tool_input, tools, user,
                            )
                            if added:
                                logger.info("WS loaded %d tools for domains via load_tools, conv=%s",
                                            len(added), conversation.pk)
                        _handle_plan_tool(
                            conversation, tool_name, tool_input, tool_result, on_event,
                        )
                        # Report events for successful mutating tool calls
                        if tool_entry.get("mutates") and (
                            not isinstance(tool_result, dict) or "error" not in tool_result
                        ):
                            _report_event(
                                f"assistant:tool:{tool_name}", 5,
                                f"Assistant tool: {tool_name}",
                                f"User {user.email} (id={user.pk}) executed mutating tool "
                                f"'{tool_name}'. conv={conversation.pk}",
                                user=user,
                            )
                    except Exception:
                        logger.exception("Tool %s failed", tool_name)
                        tool_result = {"error": f"Tool '{tool_name}' encountered an internal error."}
                        _report_event(
                            "assistant:error", 6,
                            f"Tool exception: {tool_name}",
                            f"Tool '{tool_name}' raised an exception for user {user.email} "
                            f"(id={user.pk}). conv={conversation.pk}",
                            user=user,
                        )

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

        logger.warning("WS max turns reached for user %s, conv %s", user.pk, conversation.pk)
        _report_event(
            "assistant:error", 5,
            "Max tool turns exhausted",
            f"WS agent hit {max_turns} turn limit for user {user.email} (id={user.pk}). "
            f"conv={conversation.pk}. Tools called: {len(tool_calls_made)}",
            user=user,
        )
        response_text = "I've reached the maximum number of tool calls for this request. Please try a more specific query."
        Message.objects.create(conversation=conversation, role="assistant", content=response_text)
        return {"response": response_text, "conversation_id": conversation.pk, "tool_calls_made": tool_calls_made}

    except Exception as e:
        logger.exception("Assistant WS agent failed for user %s", user.pk)
        err_str = str(e)
        if "not_found_error" in err_str or "404" in err_str:
            _report_event("assistant:error:api", 7, "LLM model not found", err_str[:500], user=user)
            return {"error": f"LLM model not found. Check LLM_ADMIN_MODEL setting. ({err_str[:200]})"}
        if "authentication_error" in err_str or "401" in err_str:
            _report_event("assistant:error:api", 7, "LLM API auth failure", err_str[:500], user=user)
            return {"error": "LLM API key is invalid. Check LLM_ADMIN_API_KEY setting."}
        if "rate_limit" in err_str.lower() or "429" in err_str:
            _report_event("assistant:error:api", 5, "LLM API rate limit", err_str[:500], user=user)
            return {"error": "LLM API rate limit reached. Please wait a moment and try again."}
        _report_event(
            "assistant:error", 7,
            "WS agent loop exception",
            f"WS agent crashed for user {user.email} (id={user.pk}). "
            f"conv={conversation.pk}. Error: {err_str[:500]}",
            user=user,
        )
        return {"error": f"Assistant error: {err_str[:200]}"}
