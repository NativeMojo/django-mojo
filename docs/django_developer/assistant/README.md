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
| `LLM_ADMIN_MODEL` | (auto-detect) | Claude model to use. If unset, auto-detects latest Sonnet via `mojo.helpers.llm.get_model()` |
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

### Discovery Domain (`view_admin` / `view_security` / `view_jobs`)

| Tool | Permission | Mutates |
|---|---|---|
| `list_tools` | `view_admin` | No |
| `list_metric_categories` | `view_admin` | No |
| `list_metric_slugs` | `view_admin` | No |
| `list_job_channels` | `view_jobs` | No |
| `list_event_categories` | `view_security` | No |
| `list_permissions` | `view_admin` | No |

Discovery tools let the LLM explore the system. When a user asks "what metrics do we track?" or "what can you do?", the LLM calls these tools to find valid slugs, categories, channels, and permissions — rather than guessing.

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

#### RestMeta

| Setting | Value | Effect |
|---|---|---|
| `VIEW_PERMS` | `["view_admin", "owner"]` | Admins see all; owners see only their own conversations |
| `OWNER_FIELD` | `"user"` | List auto-filters to `user=request.user` for non-admins |
| `CAN_DELETE` | `True` | Owner or admin may delete via `DELETE /api/assistant/conversation/<pk>` |
| `NO_REST_SAVE` | `True` | Conversations are created by the agent service, not via direct POST |

The `detail` graph includes nested messages using the message `default` graph:

```python
"detail": {
    "fields": ["id", "title", "created", "modified", "messages"],
    "graphs": {"messages": "default"},
}
```

Request it with `GET /api/assistant/conversation/<pk>?graph=detail`.

The REST handler is standard:

```python
@md.URL('conversation')
@md.URL('conversation/<int:pk>')
@md.uses_model_security(Conversation)
def on_conversation(request, pk=None):
    return Conversation.on_rest_request(request, pk)
```

### Message

A single message in a conversation.

| Field | Type | Description |
|---|---|---|
| `conversation` | FK(Conversation) | Parent conversation |
| `role` | CharField | `user`, `assistant`, `tool_use`, or `tool_result` |
| `content` | TextField | Message text content (block fences stripped for assistant messages) |
| `tool_calls` | JSONField | Tool call details (for assistant/tool_result messages) |
| `blocks` | JSONField | Pre-parsed structured data blocks extracted at write time. `null` when none present. |
| `created` | DateTimeField | When the message was created |

`blocks` is populated when the agent saves an assistant message — `_parse_blocks()` runs once at write time and the result is stored. Reading the conversation detail never re-parses content.

## WebSocket Interface

The assistant also supports a WebSocket transport for real-time chat UIs. This uses the existing realtime system — no new WebSocket endpoint needed.

### Architecture

```
Client sends WS message {type: "assistant_message", message: "...", conversation_id: N}
  → User.on_realtime_message() dispatches to assistant handler
  → Handler validates, stores message, returns {type: "assistant_thinking"} immediately
  → Background job runs run_assistant_ws() with on_event callback
  → Callback publishes WS events back to the user:
      assistant_tool_call  (per tool)
      assistant_response   (final answer)
      assistant_error      (on failure)
```

### Key Files

- `mojo/apps/assistant/handler.py` — WS message handler + background job function
- `mojo/apps/assistant/services/agent.py` — `run_assistant_ws()` variant with event callbacks
- `mojo/apps/account/models/user.py` — `on_realtime_message` dispatches `assistant_*` types

### How Messages Are Routed

The `User.on_realtime_message` method checks if the message type starts with `assistant_` and delegates to `handle_assistant_message()` — identical to how `chat_*` messages are routed to the chat handler.

### Background Processing

LLM calls are too slow to block the WebSocket handler. The handler publishes a job via `mojo.apps.jobs` and returns immediately. The job function (`execute_assistant_job`) runs the agent loop and uses `send_to_user()` from the realtime manager to push events back to the user's WebSocket connections.

### `assistant_response` Event Payload

`run_assistant_ws()` returns a dict that becomes the WS event payload. As of v1.1.8 it always includes:

```python
{
    "message_id": msg.pk,          # int — PK of the saved Message record
    "created": msg.created.isoformat(),  # ISO 8601 string
    "response": response_text,     # narrative text, block fences stripped
    "blocks": blocks or None,      # parsed blocks, or None when absent
    "tool_calls_made": [...],       # list of {tool, input} dicts
    "conversation_id": conversation.pk,
}
```

`blocks` is always included in the dict (`None` when empty) rather than conditionally omitted, making the shape consistent with the REST detail graph and easier for clients to handle.

## Structured Data Blocks

Responses can include a `blocks` array with structured data for frontend rendering. The LLM decides when to include blocks based on the data — tables for query results, charts for trends, stat cards for key metrics.

### How It Works

1. The system prompt defines three block types: `table`, `chart`, `stat`
2. The LLM wraps structured data in ` ```assistant_block ` code fences within its text response
3. `_parse_blocks()` in `agent.py` extracts valid blocks and strips the fences from the text
4. The response includes both clean `response` text and a `blocks` array
5. Clean text and parsed blocks are stored on the `Message` — no re-parsing happens at read time

### System Prompt Behavior — Narrative Text

The system prompt instructs the LLM to keep narrative text brief when blocks carry the data. The text provides interpretation (key takeaways, context, warnings); the blocks carry the detail. The LLM should not repeat in prose what is already shown in a table, chart, or stat block.

This is enforced via the system prompt, not code. Override `LLM_ADMIN_SYSTEM_PROMPT` in settings to change this behavior.

### Block Types

- **`table`** — `{type, title, columns, rows}` — query results, comparisons
- **`chart`** — `{type, chart_type, title, labels, series}` — line/bar/pie/area for trends
- **`stat`** — `{type, items: [{label, value}]}` — dashboard key metrics

### Customizing

Override `LLM_ADMIN_SYSTEM_PROMPT` to change the block format instructions. The parser (`_parse_blocks`) only requires valid JSON with a recognized `type` field inside ` ```assistant_block ` fences.

See [web_developer/assistant/README.md](../../web_developer/assistant/README.md#structured-data-blocks) for the full block schema reference and frontend rendering examples.

## Tests

```bash
./bin/run_tests -t test_assistant
```

Test files:
- `tests/test_assistant/1_test_permissions.py` — Registry, permission gate, feature flag, sensitive field exclusion
- `tests/test_assistant/2_test_conversations.py` — Conversation CRUD, owner-only access, message ordering
