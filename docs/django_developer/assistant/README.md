# Admin Assistant — Django Developer Reference

LLM-powered admin assistant that lets administrators query and manage the system through natural language. Uses Claude with tool-calling to access security incidents, events, jobs, users, groups, and metrics — all gated by the requesting user's permissions.

## Architecture

```
User sends message via REST
  → run_assistant(user, message, conversation_id)
    → Load/create Conversation, store user Message
    → Build tool list filtered by user permissions
    → Claude API tool-calling loop:
        → LLM selects tool + args
        → Permission gate: user.has_permission(tool.permission)?
          → No: return permission error to LLM
          → Yes: execute handler(params, user), return result to LLM
        → Repeat until LLM stops calling tools
    → Store assistant response as Message
    → Return response + conversation_id
```

## Enabling

The assistant is disabled by default. Add to your Django settings:

```python
LLM_ADMIN_ENABLED = True
LLM_ADMIN_API_KEY = "sk-ant-..."  # or falls back to LLM_HANDLER_API_KEY
```

Add `"mojo.apps.assistant"` to `INSTALLED_APPS` and run migrations.

## Settings

| Setting | Default | Description |
|---|---|---|
| `LLM_ADMIN_ENABLED` | `False` | Feature flag — must be True for the assistant to work |
| `LLM_ADMIN_API_KEY` | `None` | Anthropic API key. Falls back to `LLM_HANDLER_API_KEY` |
| `LLM_ADMIN_MODEL` | `claude-sonnet-4-6-20250514` | Claude model to use |
| `LLM_ADMIN_MAX_TURNS` | `25` | Max tool-calling turns per request |
| `LLM_ADMIN_MAX_HISTORY` | `50` | Max messages loaded as conversation context |
| `LLM_ADMIN_SYSTEM_PROMPT` | (built-in) | Override the default system prompt |

## Built-in Tools

### Security Domain (`view_security` / `manage_security`)

| Tool | Permission | Mutates |
|---|---|---|
| `query_incidents` | `view_security` | No |
| `query_events` | `view_security` | No |
| `query_event_counts` | `view_security` | No |
| `query_tickets` | `view_security` | No |
| `query_rulesets` | `view_security` | No |
| `query_ip_history` | `view_security` | No |
| `get_incident_timeline` | `view_security` | No |
| `update_incident` | `manage_security` | Yes |
| `block_ip` | `manage_security` | Yes |
| `create_ticket` | `manage_security` | Yes |

### Jobs Domain (`view_jobs` / `manage_jobs`)

| Tool | Permission | Mutates |
|---|---|---|
| `query_jobs` | `view_jobs` | No |
| `query_job_events` | `view_jobs` | No |
| `query_job_logs` | `view_jobs` | No |
| `get_job_stats` | `view_jobs` | No |
| `get_queue_health` | `view_jobs` | No |
| `cancel_job` | `manage_jobs` | Yes |
| `retry_job` | `manage_jobs` | Yes |

### Users Domain (`view_admin`)

| Tool | Permission | Mutates |
|---|---|---|
| `query_users` | `view_admin` | No |
| `get_user_detail` | `view_admin` | No |
| `get_user_activity` | `view_admin` | No |
| `query_rate_limits` | `view_admin` | No |
| `get_permission_summary` | `view_admin` | No |

### Groups Domain (`view_groups`)

| Tool | Permission | Mutates |
|---|---|---|
| `query_groups` | `view_groups` | No |
| `get_group_detail` | `view_groups` | No |
| `get_group_members` | `view_groups` | No |
| `get_group_activity` | `view_groups` | No |

### Metrics Domain (`view_admin` / `view_security`)

| Tool | Permission | Mutates |
|---|---|---|
| `fetch_metrics` | `view_admin` | No |
| `get_system_health` | `view_admin` | No |
| `get_incident_trends` | `view_security` | No |

## Permission Enforcement

Every tool call passes through a permission gate before execution. The gate checks `user.has_permission(tool_permission)` at call time, not at conversation start. This means:

- Users only see tools they have permission for (filtered tool list sent to Claude)
- Even if Claude somehow requests a tool the user can't use, execution is blocked
- Permission changes mid-conversation are reflected immediately
- Mutating tools require `manage_*` permissions, never just `view_*`

Sensitive fields (`password`, `auth_key`, `onetime_code`) are never included in tool results.

## Adding Custom Tools

External projects can register tools by creating an `assistant_tools.py` file in any installed Django app. The assistant auto-discovers these on startup.

### Example: `myapp/assistant_tools.py`

```python
from mojo.apps.assistant import register_tool


def query_orders(params, user):
    """Tool handler receives (params, user) and returns a dict or list."""
    from myapp.models import Order

    criteria = {}
    if params.get("status"):
        criteria["status"] = params["status"]

    limit = min(params.get("limit", 50), 50)
    orders = Order.objects.filter(
        group__in=user.get_groups(), **criteria
    ).order_by("-created")[:limit]

    return [
        {
            "id": o.pk,
            "status": o.status,
            "total": str(o.total),
            "created": str(o.created),
        }
        for o in orders
    ]


register_tool(
    name="query_orders",
    description="Query orders by status and date range. Returns up to 50 orders.",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status"},
            "limit": {"type": "integer", "description": "Max results", "default": 50},
        },
    },
    handler=query_orders,
    permission="view_orders",
    mutates=False,
    domain="orders",
)
```

### `register_tool()` Parameters

| Parameter | Required | Description |
|---|---|---|
| `name` | Yes | Unique tool name. Raises `ValueError` if duplicate. |
| `description` | Yes | Human-readable description shown to the LLM |
| `input_schema` | Yes | JSON Schema dict for the tool's parameters |
| `handler` | Yes | Callable `(params, user) -> dict or list` |
| `permission` | Yes | Permission string checked via `user.has_permission()` |
| `mutates` | No | Default `False`. If True, LLM is told to confirm before executing |
| `domain` | No | Default `"custom"`. Logical grouping for the tool |

### Tool Handler Guidelines

- **Bound results**: Always cap query results (50 is the convention). Never return unbounded querysets.
- **Exclude secrets**: Never include passwords, auth keys, tokens, or other sensitive fields.
- **Return simple types**: Return dicts and lists with JSON-serializable values. Dates as strings.
- **Handle errors gracefully**: Return `{"error": "message"}` instead of raising exceptions.
- **Accept `user` param**: Even if you don't need it now, the signature is `(params, user)`.

### Other Registry Functions

```python
from mojo.apps.assistant import (
    register_tool,       # Register a single tool
    register_tools,      # Register multiple tools from a list of dicts
    get_registry,        # Get the full registry dict
    get_tools_for_user,  # Get Claude tool definitions filtered by user perms
)
```

## Models

### Conversation

Tracks a multi-turn conversation between a user and the assistant.

| Field | Type | Description |
|---|---|---|
| `user` | FK(User) | Owner of the conversation |
| `title` | CharField | Set from the first 100 chars of the first message on creation (field max 255) |
| `metadata` | JSONField | Extensible metadata |
| `created` | DateTimeField | When the conversation started |
| `modified` | DateTimeField | Last activity |

### Message

A single message in a conversation.

| Field | Type | Description |
|---|---|---|
| `conversation` | FK(Conversation) | Parent conversation |
| `role` | CharField | `user`, `assistant`, `tool_use`, or `tool_result` |
| `content` | TextField | Message text content |
| `tool_calls` | JSONField | Tool call details (for assistant/tool_result messages) |
| `created` | DateTimeField | When the message was created |

## Tests

```bash
./bin/run_tests -t test_assistant
```

Test files:
- `tests/test_assistant/1_test_permissions.py` — Registry, permission gate, feature flag, sensitive field exclusion
- `tests/test_assistant/2_test_conversations.py` — Conversation CRUD, owner-only access, message ordering
