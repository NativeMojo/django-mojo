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
import time
import uuid
import ujson
from concurrent.futures import ThreadPoolExecutor, as_completed
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

VALID_BLOCK_TYPES = {"table", "chart", "stat", "action", "list", "alert", "progress", "file"}

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
    elif block_type == "file":
        if not block.get("url") or not block.get("filename"):
            return False
    return True


# Regex to find fenced code blocks (protect from condensing)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

# Regex to find a full markdown table: header row + separator + body rows
_MD_TABLE_RE = re.compile(
    r"^(\|.+\|)[ \t]*\n(\|[ \t]*[-:]+[-| \t:]*\|)[ \t]*\n((?:\|.+\|[ \t]*\n?)+)",
    re.MULTILINE,
)

# Regex to collapse blank lines between pipe-delimited table rows
_TABLE_BLANK_LINE_RE = re.compile(r"(\|[^\n]*\n)(\s*\n)+(\|)", re.MULTILINE)


def _condense_markdown(text, blocks):
    """
    Clean up LLM markdown: collapse excess blank lines, repair broken
    tables, and strip markdown tables that duplicate a structured block.
    """
    # Protect code fences from modification
    code_blocks = []
    def _save_code(match):
        code_blocks.append(match.group(0))
        return "\x00CODE%d\x00" % (len(code_blocks) - 1)
    text = _CODE_FENCE_RE.sub(_save_code, text)

    # Collapse 3+ consecutive blank lines to one blank line
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Repair markdown tables with blank lines between rows
    text = _TABLE_BLANK_LINE_RE.sub(r"\1\3", text)

    # Strip markdown tables that duplicate a table block
    table_blocks = [b for b in blocks if b.get("type") == "table"]
    if table_blocks:
        text = _strip_duplicate_tables(text, table_blocks)

    # Restore code fences
    for i, cb in enumerate(code_blocks):
        text = text.replace("\x00CODE%d\x00" % i, cb)

    # Final cleanup
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_duplicate_tables(text, table_blocks):
    """Remove markdown tables from text when they match a structured table block."""
    block_signatures = []
    for b in table_blocks:
        cols = set()
        for c in b.get("columns", []):
            cols.add(str(c).strip().lower())
        block_signatures.append({
            "title": (b.get("title") or "").strip().lower(),
            "columns": cols,
        })

    def _is_duplicate(match):
        header_row = match.group(1)
        # Parse column names from the markdown header row
        md_cols = set()
        for cell in header_row.split("|"):
            cell = cell.strip()
            if cell:
                md_cols.add(cell.lower())
        if not md_cols:
            return False
        for sig in block_signatures:
            # Match by 2+ overlapping column names
            overlap = md_cols & sig["columns"]
            if len(overlap) >= 2:
                return True
        return False

    def _replace_table(match):
        if not _is_duplicate(match):
            return match.group(0)
        # Also remove a title line directly above the table
        return ""

    # Check for title lines above tables and strip them too
    lines = text.split("\n")
    result = _MD_TABLE_RE.sub(_replace_table, text)

    # Remove orphaned title lines above removed tables
    # (heading or bold text followed by only whitespace where table was)
    if result != text:
        for sig in block_signatures:
            if sig["title"]:
                # Remove heading lines that match the block title
                title_re = re.compile(
                    r"^#{1,6}\s+" + re.escape(sig["title"]) + r"\s*$",
                    re.MULTILINE | re.IGNORECASE,
                )
                result = title_re.sub("", result)
                # Remove bold title lines
                bold_re = re.compile(
                    r"^\*\*" + re.escape(sig["title"]) + r"\*\*\s*$",
                    re.MULTILINE | re.IGNORECASE,
                )
                result = bold_re.sub("", result)

    return result


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
    # Condense markdown: collapse whitespace, fix tables, strip duplicates
    clean = _condense_markdown(clean, blocks)
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

## Data Strategy
- **Summaries**: Use `aggregate_model` for counts, sums, averages, min/max, and group-by breakdowns. Never pull rows just to count or summarize them.
- **Exports**: Use `export_data` when users want rows as a file. Data goes directly to storage — never returned through you. Present the download URL using a file block.
- **Inline data**: Use `query_model` only when you need to inspect specific records (small result sets, detail lookups). Keep limits low (10-50 rows).
- **Never return raw CSV**: All CSV exports go through `export_data` which writes to file storage and returns a download link.

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

## Skills

You can learn and replay reusable multi-step procedures called skills. When a user's request sounds like a stored procedure, references a skill by name, or they say something like "remember this as a skill", use the skill tools:
- `find_skill` — search for a matching skill by keywords. Returns the full step definitions so you can replay them.
- `save_skill` — store a new skill with a name, trigger phrases, and ordered steps.
- `list_skills` — see all available skills.
- `delete_skill` — remove a skill.

When replaying a skill, execute each step in order using the referenced tools. If a step has a condition, evaluate it against the previous step's result. If auto_execute is false (the default), confirm with the user before running the steps.

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

**file** — for downloadable files generated by tools (CSV exports, reports):
```assistant_block
{"type": "file", "filename": "export_users_2026-04-13.csv", "url": "https://example.com/s/Xk9mR2p", "size": 45230, "format": "csv", "row_count": 1250, "expires_in": "14 days"}
```
Use when a tool generates a downloadable file. The frontend renders this as a download card with filename, size, format icon, and download button. Include all fields returned by the export_data tool. Never fabricate URLs — only use URLs returned by export_data.

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

## Parallel Execution
When you need data from multiple independent sources (e.g., incidents AND jobs AND users), call all the tools in a single turn rather than one at a time. The system executes concurrent tool calls in parallel for faster results. Only serialize tool calls when one tool's result informs the next tool's input.
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


META_TOOLS = {"load_tools", "create_plan", "update_plan"}


def _execute_tool(block, registry, user, conversation, tools, on_event, tool_calls_made):
    """
    Execute a single tool call with permission gate, meta-tool handling,
    and event reporting.

    Returns a dict with 'tool_use_id' and 'result' for building tool_results.
    """
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

    return {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": ujson.dumps(tool_result),
    }


def _execute_tools(tool_blocks, registry, user, conversation, tools, on_event, tool_calls_made):
    """
    Execute tool calls, using parallel execution when multiple non-meta tools
    are present in a single turn.

    Meta-tools (load_tools, create_plan, update_plan) always run first serially
    since they have side effects on the agent loop state.
    """
    if not tool_blocks:
        return []

    max_workers = settings.get("LLM_ADMIN_MAX_PARALLEL_TOOLS", 4, kind="int")

    # Separate meta-tools from regular tools
    meta_blocks = [b for b in tool_blocks if b["name"] in META_TOOLS]
    regular_blocks = [b for b in tool_blocks if b["name"] not in META_TOOLS]

    results = []

    # Meta-tools run first, serially (they modify conversation state)
    for block in meta_blocks:
        result = _execute_tool(block, registry, user, conversation, tools, on_event, tool_calls_made)
        results.append(result)

    # Regular tools run in parallel if there are multiple
    if len(regular_blocks) <= 1:
        for block in regular_blocks:
            result = _execute_tool(block, registry, user, conversation, tools, on_event, tool_calls_made)
            results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(regular_blocks))) as pool:
            futures = {}
            for block in regular_blocks:
                future = pool.submit(
                    _execute_tool, block, registry, user, conversation,
                    tools, on_event, tool_calls_made,
                )
                futures[future] = block["id"]

            for future in as_completed(futures):
                try:
                    result = future.result(timeout=30)
                    results.append(result)
                except Exception:
                    tool_id = futures[future]
                    logger.exception("Parallel tool execution failed for tool_use_id %s", tool_id)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": ujson.dumps({"error": "Tool execution timed out or failed."}),
                    })

    return results


def _execute_parallel_plan_steps(plan, registry, user, conversation, tools, on_event, tool_calls_made):
    """
    Execute parallel plan steps concurrently.

    For plan steps with parallel=True and a tool+tool_input, execute them
    all via ThreadPoolExecutor and return the results as tool_result dicts
    that can be injected into the LLM conversation.

    Returns (tool_results, fake_tool_blocks) where fake_tool_blocks are
    synthetic tool_use blocks to pair with the results in message history.
    """
    parallel_steps = [
        s for s in plan.get("steps", [])
        if s.get("parallel") and s.get("tool") and s.get("status") == "pending"
    ]
    if not parallel_steps:
        return [], []

    max_workers = settings.get("LLM_ADMIN_MAX_PARALLEL_TOOLS", 4, kind="int")
    results = []
    fake_blocks = []

    # Build synthetic tool_use blocks for each parallel step
    step_blocks = []
    for step in parallel_steps:
        tool_name = step["tool"]
        tool_entry = registry.get(tool_name)
        # Reject mutating tools in parallel steps — they need user confirmation
        if not tool_entry or tool_entry.get("mutates"):
            _handle_plan_tool(conversation, "update_plan", {},
                              {"step_id": step["id"], "status": "skipped",
                               "summary": "Skipped: mutating tools cannot run in parallel",
                               "updated": True}, on_event)
            continue
        tool_input = step.get("tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        tool_id = f"plan_step_{step['id']}_{uuid.uuid4().hex[:8]}"
        block = {
            "type": "tool_use",
            "id": tool_id,
            "name": tool_name,
            "input": tool_input,
        }
        step_blocks.append((step, block))
        fake_blocks.append(block)

    # Mark all parallel steps as in_progress
    for step, _ in step_blocks:
        _handle_plan_tool(conversation, "update_plan", {},
                          {"step_id": step["id"], "status": "in_progress", "updated": True},
                          on_event)

    # Execute all in parallel
    if len(step_blocks) <= 1:
        for step, block in step_blocks:
            result = _execute_tool(block, registry, user, conversation, tools, on_event, tool_calls_made)
            results.append(result)
            # Parse the result to extract a summary
            try:
                parsed = ujson.loads(result["content"])
                summary = _summarize_tool_result(parsed)
            except Exception:
                summary = "Completed"
            _handle_plan_tool(conversation, "update_plan", {},
                              {"step_id": step["id"], "status": "done", "summary": summary, "updated": True},
                              on_event)
    else:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(step_blocks))) as pool:
            future_to_step = {}
            for step, block in step_blocks:
                future = pool.submit(
                    _execute_tool, block, registry, user, conversation,
                    tools, on_event, tool_calls_made,
                )
                future_to_step[future] = (step, block)

            for future in as_completed(future_to_step):
                step, block = future_to_step[future]
                try:
                    result = future.result(timeout=30)
                    results.append(result)
                    try:
                        parsed = ujson.loads(result["content"])
                        summary = _summarize_tool_result(parsed)
                    except Exception:
                        summary = "Completed"
                    _handle_plan_tool(conversation, "update_plan", {},
                                      {"step_id": step["id"], "status": "done", "summary": summary, "updated": True},
                                      on_event)
                except Exception:
                    logger.exception("Parallel plan step %d failed", step["id"])
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": ujson.dumps({"error": f"Step '{step['description']}' failed."}),
                    })
                    _handle_plan_tool(conversation, "update_plan", {},
                                      {"step_id": step["id"], "status": "skipped", "summary": "Failed", "updated": True},
                                      on_event)

    return results, fake_blocks


def _summarize_tool_result(result):
    """Create a brief summary from a tool result for plan step updates."""
    if isinstance(result, dict):
        if "error" in result:
            return f"Error: {result['error'][:80]}"
        if "message" in result:
            return str(result["message"])[:80]
        if "total" in result:
            return f"{result['total']} results"
    if isinstance(result, list):
        return f"{len(result)} results"
    return "Completed"


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
    t_start = time.time()

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
                duration_ms = int((time.time() - t_start) * 1000)

                Message.objects.create(
                    conversation=conversation,
                    role="assistant",
                    content=response_text,
                    blocks=blocks or None,
                    duration_ms=duration_ms,
                    tool_calls=result["content"] if any(
                        b.get("type") == "tool_use" for b in result["content"]
                    ) else None,
                )

                return {
                    "response": response_text,
                    "blocks": blocks,
                    "conversation_id": conversation.pk,
                    "tool_calls_made": tool_calls_made,
                    "duration_ms": duration_ms,
                }

            # Process tool calls — parallel when multiple non-meta tools
            tool_blocks = [b for b in result["content"] if b.get("type") == "tool_use"]
            tool_results = _execute_tools(
                tool_blocks, registry, user, conversation, tools, on_event, tool_calls_made,
            )

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

            # Plan-aware parallel execution: if create_plan just ran and
            # the plan has parallel steps with tools, execute them now
            plan = (conversation.metadata or {}).get("plan")
            if plan and any(b["name"] == "create_plan" for b in tool_blocks):
                plan_results, plan_blocks = _execute_parallel_plan_steps(
                    plan, registry, user, conversation, tools, on_event, tool_calls_made,
                )
                if plan_results:
                    # Inject parallel results as if the LLM had called them
                    Message.objects.create(
                        conversation=conversation,
                        role="assistant",
                        content="",
                        tool_calls=plan_blocks,
                    )
                    Message.objects.create(
                        conversation=conversation,
                        role="tool_result",
                        content="",
                        tool_calls=plan_results,
                    )
                    # Add to messages so the LLM sees the results
                    messages.append({"role": "assistant", "content": plan_blocks})
                    messages.append({"role": "user", "content": plan_results})

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
        duration_ms = int((time.time() - t_start) * 1000)
        Message.objects.create(
            conversation=conversation,
            role="assistant",
            content=response_text,
            duration_ms=duration_ms,
        )
        return {
            "response": response_text,
            "conversation_id": conversation.pk,
            "tool_calls_made": tool_calls_made,
            "duration_ms": duration_ms,
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
    t_start = time.time()

    try:
        for _ in range(max_turns):
            result = llm.call(messages, system=system_prompt, tools=tools)
            stop_reason = result.get("stop_reason")
            messages.append({"role": "assistant", "content": result["content"]})

            if stop_reason != "tool_use":
                text_parts = [b["text"] for b in result["content"] if b.get("type") == "text"]
                raw_text = "\n".join(text_parts) if text_parts else ""
                response_text, blocks = _parse_blocks(raw_text)
                duration_ms = int((time.time() - t_start) * 1000)
                msg = Message.objects.create(
                    conversation=conversation, role="assistant",
                    content=response_text, blocks=blocks or None,
                    duration_ms=duration_ms,
                )
                return {
                    "response": response_text,
                    "blocks": blocks,
                    "message_id": msg.pk,
                    "created": msg.created.isoformat(),
                    "conversation_id": conversation.pk,
                    "tool_calls_made": tool_calls_made,
                    "duration_ms": duration_ms,
                }

            # Process tool calls — parallel when multiple non-meta tools
            tool_blocks = [b for b in result["content"] if b.get("type") == "tool_use"]
            tool_results = _execute_tools(
                tool_blocks, registry, user, conversation, tools, on_event, tool_calls_made,
            )

            Message.objects.create(
                conversation=conversation, role="assistant", content="", tool_calls=result["content"],
            )
            Message.objects.create(
                conversation=conversation, role="tool_result", content="", tool_calls=tool_results,
            )
            messages.append({"role": "user", "content": tool_results})

            # Plan-aware parallel execution: if create_plan just ran and
            # the plan has parallel steps with tools, execute them now
            plan = (conversation.metadata or {}).get("plan")
            if plan and any(b["name"] == "create_plan" for b in tool_blocks):
                plan_results, plan_blocks = _execute_parallel_plan_steps(
                    plan, registry, user, conversation, tools, on_event, tool_calls_made,
                )
                if plan_results:
                    Message.objects.create(
                        conversation=conversation, role="assistant", content="", tool_calls=plan_blocks,
                    )
                    Message.objects.create(
                        conversation=conversation, role="tool_result", content="", tool_calls=plan_results,
                    )
                    messages.append({"role": "assistant", "content": plan_blocks})
                    messages.append({"role": "user", "content": plan_results})

        logger.warning("WS max turns reached for user %s, conv %s", user.pk, conversation.pk)
        _report_event(
            "assistant:error", 5,
            "Max tool turns exhausted",
            f"WS agent hit {max_turns} turn limit for user {user.email} (id={user.pk}). "
            f"conv={conversation.pk}. Tools called: {len(tool_calls_made)}",
            user=user,
        )
        response_text = "I've reached the maximum number of tool calls for this request. Please try a more specific query."
        duration_ms = int((time.time() - t_start) * 1000)
        Message.objects.create(conversation=conversation, role="assistant", content=response_text, duration_ms=duration_ms)
        return {"response": response_text, "conversation_id": conversation.pk, "tool_calls_made": tool_calls_made, "duration_ms": duration_ms}

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
