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
| `LLM_BROWSE_MAX_LENGTH` | `20000` | Max character length of content returned by `browse_url` and `read_docs` |
| `LLM_BROWSE_TIMEOUT` | `10` | HTTP request timeout in seconds for `browse_url` |
| `LLM_DOCS_BASE_URL` | `https://raw.githubusercontent.com/NativeMojo/django-mojo/refs/heads/main/docs/` | Base URL for fetching framework docs via `read_docs` |

## Built-in Tools

### Security Domain (`view_security` / `manage_security`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `query_incidents` | `view_security` | No | Filter by status, priority, category, source_ip, hostname, rule_set_id, model_name |
| `query_events` | `view_security` | No | Filter by category, IP, hostname, level, rule_id (OSSEC metadata), incident_id |
| `query_event_counts` | `view_security` | No | Aggregate counts grouped by category or rule_id |
| `query_tickets` | `view_security` | No | Filter tickets by status, priority, category |
| `query_rulesets` | `view_security` | No | List rule sets with is_active, trigger_count, priority, match_by |
| `query_ip_history` | `view_security` | No | IP reputation, block history, geo info, past incidents |
| `query_blocked_ips` | `view_security` | No | List currently blocked IPs with TTL, reason, block count |
| `query_ipsets` | `view_security` | No | List bulk IP sets (country/datacenter/abuse) — metadata only |
| `get_incident` | `view_security` | No | Full incident details including metadata and event count |
| `get_incident_timeline` | `view_security` | No | Full history/audit trail for an incident |
| `get_incident_events` | `view_security` | No | Events bundled into an incident with full metadata |
| `get_event` | `view_security` | No | Full event details including complete metadata |
| `get_ruleset` | `view_security` | No | Full rule set details including child rules (field conditions) |
| `update_incident` | `manage_security` | Yes | Change incident status with history note |
| `bulk_update_incidents` | `manage_security` | Yes | Resolve/ignore up to 100 incidents at once |
| `merge_incidents` | `manage_security` | Yes | Merge source incidents into target (moves events, deletes sources) |
| `create_rule` | `manage_security` | Yes | Create new rule set with conditions (created disabled) |
| `add_rule_condition` | `manage_security` | Yes | Add a field-level rule to an existing rule set |
| `update_ruleset` | `manage_security` | Yes | Edit rule set fields (handler, bundle, trigger, is_active, etc.) |
| `delete_ruleset` | `manage_security` | Yes | Delete a rule set and cascade-delete child rules |
| `block_ip` | `manage_security` | Yes | Block an IP fleet-wide with TTL |
| `unblock_ip` | `manage_security` | Yes | Unblock a blocked IP fleet-wide |
| `whitelist_ip` | `manage_security` | Yes | Add IP to whitelist (prevents future auto-blocks, unblocks if blocked) |
| `unwhitelist_ip` | `manage_security` | Yes | Remove IP from whitelist |
| `create_ticket` | `manage_security` | Yes | Create a ticket for human review |

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

### Users Domain (`view_admin` / `manage_users`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `query_users` | `view_admin` | No | Search/filter users by name, email, status, permission |
| `get_user_detail` | `view_admin` | No | Full user profile, permissions, group memberships |
| `get_user_activity` | `view_admin` | No | Recent security events for a user |
| `query_rate_limits` | `view_admin` | No | Currently active rate limit entries from Redis |
| `get_permission_summary` | `view_admin` | No | User permissions breakdown (user-level + group-level) |
| `update_user_permission` | `manage_users` | Yes | Add or remove a permission from a user |
| `disable_user` | `manage_users` | Yes | Disable account + rotate auth_key (invalidates all sessions). Cannot disable yourself. |
| `enable_user` | `manage_users` | Yes | Re-enable a disabled account |
| `force_logout` | `manage_users` | Yes | Rotate auth_key to invalidate all sessions (account stays active) |

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

### Web Domain (`view_admin`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `browse_url` | `view_admin` | No | Fetch a web page and return clean readable text. Supports an optional CSS selector to narrow content to a specific element. Only `http`/`https` URLs are allowed; private/internal IPs are blocked (SSRF protection). Content is truncated to `LLM_BROWSE_MAX_LENGTH` chars. |

### Docs Domain (`view_admin`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `read_docs` | `view_admin` | No | Fetch django-mojo framework documentation by path or topic keyword search. Use `path` for a specific doc (e.g. `django_developer/account/push.md`) or `topic` for keyword search (e.g. `push notifications`, `rate limiting`). Returns raw markdown content. Falls back to the index when no topic match is found. |

### Models Domain (`view_admin`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `describe_model` | `view_admin` | No | Describe a MojoModel's fields, graphs, permissions, and search fields. Use this to discover what data is available before querying. Requires `app_name` and `model_name`. Sensitive fields (`password`, `auth_key`, `onetime_code`, `secret`, `token_secret`) are excluded from output. Only works on MojoModels with a `RestMeta` definition and without `NO_REST = True`. |
| `query_model` | `view_admin` | No | Query any MojoModel with filters, search, ordering, and output format options. Respects the model's `RestMeta` permissions and owner/group filtering — the same rules as the REST API. Supports JSON and CSV output, count-only mode, and configurable limits (default 50, max 200). Sensitive fields are blocked as filter keys. |

`query_model` parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `app_name` | string | required | Django app label (e.g. `account`, `incident`, `jobs`) |
| `model_name` | string | required | Model class name (e.g. `User`, `Incident`, `Job`) |
| `filters` | object | `{}` | ORM filter dict (e.g. `{"status": "active", "created__gte": "2026-01-01"}`) |
| `search` | string | — | Free-text search using the model's `SEARCH_FIELDS` |
| `ordering` | string | `-pk` | Order by field, prefix with `-` for descending (e.g. `-created`) |
| `limit` | integer | `50` | Max results to return (max 200) |
| `graph` | string | `default` | Serialization graph name |
| `format` | string | `json` | Output format: `json` or `csv` |
| `count_only` | boolean | `false` | If true, return only the total count with no row data |

The tool enforces the same permission and owner/group scoping as the REST layer via `rest_check_permission` and `_apply_owner_group_filter`. Attempts to filter on sensitive fields are blocked and reported as security events.

### Logs Domain (`view_logs`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `query_logs` | `view_logs` | No | Query the `logit.Log` audit trail. Every HTTP request/response, model change, API error, and custom event is recorded here. Filter by time range, level, kind, model_name, model_id, uid, IP, path, method, or free-text search. |

`query_logs` parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `minutes` | integer | `60` | Look back N minutes (max 10080 = 7 days) |
| `level` | string | — | Filter by log level: `info`, `warn`, `error`, `debug` |
| `kind` | string | — | Filter by log kind (e.g. `request`, `response`, `api_error`, `model:created`, `model:changed`) |
| `model_name` | string | — | Filter by target model (e.g. `account.User`, `incident.Incident`) |
| `model_id` | integer | — | Filter by target model instance ID |
| `uid` | integer | — | Filter by user ID who triggered the event |
| `ip` | string | — | Filter by client IP address |
| `path` | string | — | Filter by request path (substring match) |
| `method` | string | — | Filter by HTTP method (GET, POST, etc.) |
| `search` | string | — | Free-text search in log content |
| `limit` | integer | `50` | Max results to return (max 200) |
| `count_only` | boolean | `false` | If true, return only the count with no row data |
| `verbose` | boolean | `false` | If true, include full log content, payload, and user_agent |

By default, log content is truncated at 500 characters. Pass `verbose: true` to get the full `log`, `payload`, and `user_agent` fields.

### Files Domain (`view_fileman`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `query_files` | `view_fileman` | No | List/search uploaded files by category, content type, filename, or group |
| `get_file` | `view_fileman` | No | Detailed metadata for a single file |
| `analyze_image` | `view_fileman` | No | Send an image file to Claude vision for analysis (content description, OCR, error messages, etc.) |

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

## Context Conversations

Create a conversation pre-loaded with the full context of any MojoModel instance. The UI calls this to provide an "Open in Assistant" button from any detail view (tickets, incidents, or any other model).

### Endpoint

`POST /api/assistant/context` — requires `view_admin` + the model's own `VIEW_PERMS`.

```json
{"model": "incident.Ticket", "pk": 123}
```

Returns:
```json
{"status": true, "data": {"conversation_id": 789}}
```

### How It Works

1. Resolves the model via `apps.get_model(app_label, ModelName)`
2. Checks the user has at least one of the model's `VIEW_PERMS`
3. Checks for duplicate: same user + same model + same pk returns the existing conversation (with `"existing": true`)
4. Builds a context message — rich builders for Ticket and Incident, generic `to_dict()` fallback for everything else
5. Creates a Conversation with `metadata: {"source_model": "incident.ticket", "source_pk": 123}`
6. Stores the context as the first `user` message

The admin then sends their first real message and the assistant responds with full tool access.

### Rich Context Builders

Ticket and Incident have custom builders that load related data:

- **Ticket**: title, status, priority, description, assignee, linked incident, all notes (up to 20)
- **Incident**: title, status, priority, source IP, hostname, details, LLM assessment, history (up to 15), recent events (up to 10), linked tickets

### Generic Fallback

Any MojoModel without a registered builder gets `to_dict(graph="detail")` serialization with sensitive fields stripped. This means the endpoint works for RuleSets, Jobs, Users, or any other model — the context is less rich but still useful.

### Registering Custom Builders

```python
from mojo.apps.assistant.services.context import register_context_builder

def build_order_context(instance):
    title = f"Order #{instance.pk}: {instance.status}"
    message = f"I need help with this order:\n\n## {title}\n..."
    return title, message, None  # (title, message, error)

register_context_builder("myapp.Order", build_order_context)
```

### Key Files

- `mojo/apps/assistant/services/context.py` — context builder registry and rich builders
- `mojo/apps/assistant/rest/assistant.py` — `on_assistant_context` endpoint

## Permission Enforcement

Every tool call passes through a permission gate before execution. The gate checks `user.has_permission(tool_permission)` at call time, not at conversation start. This means:

- Users only see tools they have permission for (filtered tool list sent to Claude)
- Even if Claude somehow requests a tool the user can't use, execution is blocked
- Permission changes mid-conversation are reflected immediately
- Mutating tools require `manage_*` permissions, never just `view_*`

Sensitive fields (`password`, `auth_key`, `onetime_code`) are never included in tool results.

## Adding Custom Tools

There are two ways to register tools: the `@tool` decorator (preferred) and the `register_tool()` function.

### The `@tool` Decorator (Preferred)

The decorator registers the function as an assistant tool immediately on import. All built-in tools use this pattern.

```python
from mojo.apps.assistant import tool


@tool(
    name="query_orders",
    domain="orders",
    permission="view_orders",
    description="Query orders by status and date range. Returns up to 50 orders.",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status"},
            "limit": {"type": "integer", "description": "Max results", "default": 50},
        },
    },
)
def _tool_query_orders(params, user):
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
```

#### `@tool` Parameters

| Parameter | Required | Description |
|---|---|---|
| `name` | Yes | Unique tool name. Raises `ValueError` if duplicate. |
| `domain` | Yes | Logical grouping (e.g. `"security"`, `"jobs"`, `"orders"`) |
| `permission` | Yes | Permission string checked via `user.has_permission()` |
| `description` | Yes | Human-readable description shown to the LLM |
| `input_schema` | Yes | JSON Schema dict for the tool's parameters |
| `mutates` | No | Default `False`. If True, LLM is told to confirm before executing |

#### Organizing Tools in Modules

For external projects, create an `assistant_tools.py` file in any installed Django app. The assistant auto-discovers these on startup. Each `@tool`-decorated function in that module self-registers on import — no additional wiring needed.

For apps with many tools, split into a `tools/` package with submodules per domain and import them from `__init__.py`:

```
myapp/
  assistant_tools/
    __init__.py      # from . import orders, shipping
    orders.py        # @tool-decorated functions
    shipping.py      # @tool-decorated functions
```

This is how the built-in security tools are organized — `mojo/apps/assistant/services/tools/security/` contains submodules for incidents, events, tickets, rules, and IPs.

### `register_tool()` Function

The imperative alternative — useful when tool configuration is dynamic or loaded from data.

```python
from mojo.apps.assistant import register_tool


def query_orders(params, user):
    ...

register_tool(
    name="query_orders",
    description="Query orders by status and date range.",
    input_schema={...},
    handler=query_orders,
    permission="view_orders",
    mutates=False,
    domain="orders",
)
```

#### `register_tool()` Parameters

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
- **Signature**: Always `(params, user)` — even if you don't use `user` today.

### Other Registry Functions

```python
from mojo.apps.assistant import (
    tool,                # @tool decorator (preferred)
    register_tool,       # Register a single tool imperatively
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
- `tests/test_assistant/4_test_security_tools.py` — Security tool handlers, IP management, rule management, bulk operations, user security actions
- `tests/test_assistant/5_test_web_tools.py` — Web domain tool: URL fetching, SSRF protection, CSS selector filtering, error handling
- `tests/test_assistant/6_test_docs_tools.py` — Docs domain tool: path fetch, topic lookup, unknown topic fallback, truncation, path traversal rejection, registration
- `tests/test_assistant/7_test_model_tools.py` — Models domain tools: describe_model, query_model, permission enforcement, owner/group filtering, sensitive field blocking, count-only mode, CSV export
- `tests/test_assistant/8_test_log_tools.py` — Logs domain tool: query_logs, time range, filter combinations, count-only mode, verbose mode, log truncation
- `tests/test_assistant/9_test_ticket_tools.py` — Ticket management tools (get, update, add note)
- `tests/test_assistant/10_test_file_tools.py` — File domain tools: query, metadata, image analysis
- `tests/test_assistant/11_test_context.py` — Context conversations: ticket, incident, generic model, duplicate prevention, permission checks
