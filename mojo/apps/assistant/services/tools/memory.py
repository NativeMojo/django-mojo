"""Memory domain tools — read, write, delete memories across tiers."""
from mojo.apps.assistant import tool


@tool(
    name="read_memory",
    domain="memory",
    permission="assistant",
    description=(
        "Read stored memories for the current context. Memories are already injected "
        "into the system prompt, but this tool lets you see raw keys for reference "
        "before updating or deleting. Returns entries grouped by tier."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["global", "user", "group"],
                "description": "Read a specific tier only. Omit to read all tiers.",
            },
        },
    },
)
def _tool_read_memory(params, user):
    from mojo.apps.assistant.services.memory import read_memories

    group = getattr(user, "_assistant_group", None)
    tier = params.get("tier")
    result = read_memories(user, group=group, tier=tier)
    if not result:
        return {"message": "No memories stored"}
    return result


@tool(
    name="write_memory",
    domain="memory",
    permission="assistant",
    mutates=True,
    description=(
        "Store or update a memory entry. Memories persist across conversations.\n\n"
        "Tiers:\n"
        "- global: Platform-wide facts (tech stack, infra rules, safety constraints). "
        "Visible to all assistant users.\n"
        "- user: Your personal notes (preferences, focus areas, shorthand).\n"
        "- group: Group/tenant-specific rules (deploy windows, SLAs, team conventions).\n\n"
        "Guidelines:\n"
        "- One fact per entry, 1-2 sentences max.\n"
        "- Don't memorize what you can look up with a tool.\n"
        "- Check existing memories before writing — update rather than duplicate.\n"
        "- Never store passwords, API keys, tokens, or credentials.\n"
        "- For user/group tiers, confirm with the user before storing observations."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["global", "user", "group"],
                "description": "Which memory tier to write to.",
            },
            "key": {
                "type": "string",
                "description": "Short slug key (lowercase, alphanumeric, colons/underscores/hyphens). Example: 'platform', 'rule:internal_ips', 'preferred_channel'.",
            },
            "value": {
                "type": "string",
                "description": "The memory content. Plain text, 1-2 sentences, max 500 characters.",
            },
        },
        "required": ["tier", "key", "value"],
    },
)
def _tool_write_memory(params, user):
    from mojo.apps.assistant.services.memory import write_memory

    group = getattr(user, "_assistant_group", None)
    return write_memory(
        user,
        tier=params["tier"],
        key=params["key"],
        value=params["value"],
        group=group,
    )


@tool(
    name="delete_memory",
    domain="memory",
    permission="assistant",
    mutates=True,
    description=(
        "Delete a memory entry. Use this to remove outdated, incorrect, or "
        "duplicate memories. Check existing entries with read_memory first."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tier": {
                "type": "string",
                "enum": ["global", "user", "group"],
                "description": "Which memory tier to delete from.",
            },
            "key": {
                "type": "string",
                "description": "The key of the memory entry to delete.",
            },
        },
        "required": ["tier", "key"],
    },
)
def _tool_delete_memory(params, user):
    from mojo.apps.assistant.services.memory import delete_memory

    group = getattr(user, "_assistant_group", None)
    return delete_memory(
        user,
        tier=params["tier"],
        key=params["key"],
        group=group,
    )
