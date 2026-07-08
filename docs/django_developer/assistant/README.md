# Admin Assistant — Django Developer Reference

LLM-powered admin assistant that lets administrators query and manage the system through natural language. Uses Claude with tool-calling to access security incidents, events, jobs, users, groups, and metrics — all gated by the requesting user's permissions.

## Architecture

```
User sends message via REST
  → run_assistant(user, message, conversation_id)
    → Load/create Conversation, store user Message
    → Build tool list (two-tier):
        New conversation  → core tools only
        Resumed with active_domains → core + those domain tools
        Old conversation (pre-two-tier, has tool_use history) → all tools
    → Claude API tool-calling loop:
        → LLM selects tool + args
        → Separate meta-tools from regular tools
        → Meta-tools (load_tools, create_plan, update_plan) run first, serially
        → Regular tools run in parallel via ThreadPoolExecutor (LLM_ADMIN_MAX_PARALLEL_TOOLS)
        → Permission gate per tool: user.has_permission(tool.permission)?
          → No: return permission error to LLM
          → Yes: execute handler(params, user), return result to LLM
        → If tool == load_tools with domain arg:
            → Persist domain in conversation.metadata["active_domains"]
            → Inject new domain tools into active tool list immediately
        → If tool == create_plan:
            → Store plan in conversation.metadata["plan"]
            → Publish WS event "assistant_plan"
            → Execute parallel plan steps concurrently, publish "assistant_plan_update" per step
        → If tool == update_plan:
            → Update step status in conversation.metadata["plan"]
            → Publish WS event "assistant_plan_update"
        → Repeat until LLM stops calling tools
    → Store assistant response as Message
    → Return response + conversation_id
```

## Two-Tier Tool Loading

Tools are split into two tiers to reduce token usage on every turn. The LLM starts with a small set of core tools and loads domain-specific tools on demand.

### Core Tools (always sent)

Core tools are sent to the LLM on every turn regardless of domain. They handle universal capabilities:

| Domain | Tools |
|---|---|
| `discovery` | `load_tools` |
| `memory` | `read_memory`, `write_memory`, `delete_memory` |
| `models` | `describe_model`, `query_model`, `aggregate_model`, `export_data`, `delete_model_instance`, `save_model_instance` |
| `docs` | `read_docs` |
| `web` | `browse_url` |
| `logs` | `query_logs` |
| `files` | `query_files`, `get_file`, `analyze_image` |
| `skills` | `find_skill`, `save_skill`, `list_skills`, `delete_skill` |
| `models` (extended) | `add_context` |

### Domain Tools (loaded on demand)

All other tools (security, jobs, users, groups, metrics, full discovery) start unloaded. The LLM calls `load_tools` to activate them.

**Domains and their tool counts:**

| Domain | Description |
|---|---|
| `security` | Query and manage security incidents, events, tickets, rulesets, and IP blocking |
| `jobs` | Query, monitor, cancel, and retry background jobs |
| `users` | Query and manage users, permissions, rate limits, and activity |
| `groups` | Query and manage groups, members, and group activity |
| `metrics` | Fetch time-series metrics, system health, and incident trends |
| `discovery` | Full tool listing (`list_tools`) |

### `load_tools` — the primary gateway

`load_tools` is a core tool that acts as the discovery and loading mechanism:

- Called with **no arguments** — lists available domains with descriptions and tool counts
- Called with **`domain`** — loads that domain's tools for the rest of the conversation
- Called with **`domains`** (list) — loads multiple domains at once

Loaded domains persist in `conversation.metadata["active_domains"]`. Resumed conversations automatically restore their loaded tools.

**LLM behavior guidance (built into system prompt):**
- When the user's request clearly maps to a domain, the LLM auto-loads that domain without asking
- When the request is ambiguous, the LLM calls `load_tools()` with no args to show available domains, then asks the user
- Once a domain is loaded, dedicated domain tools are preferred over `query_model`

### Backward Compatibility

Old conversations (pre-two-tier) that have `tool_use` blocks in their message history fall back to receiving all tools. This keeps existing conversations working without any migration.

## Data Strategy

The system prompt instructs the LLM on which tool to use for data operations:

| Task | Tool |
|---|---|
| Count, sum, average, min/max, grouped breakdown | `aggregate_model` |
| Export rows as a file | `export_data` |
| Inspect specific records (small result set) | `query_model` |
| Never | Return raw CSV inline — all CSV exports use `export_data` |

`export_data` writes data directly to file storage and returns a download URL. The LLM presents this URL in a `file` structured block so the frontend can render a download card. The file is owned by the requesting user/group and expires after `FILEMAN_EXPORT_EXPIRES_DAYS` days.

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
| `LLM_ADMIN_MAX_PARALLEL_TOOLS` | `4` | Max concurrent threads for parallel tool execution |
| `LLM_ADMIN_SYSTEM_PROMPT` | (built-in) | Override the default system prompt |
| `LLM_ADMIN_PROMPT_CACHE_ENABLED` | `True` | Enable Anthropic prompt caching on assistant LLM calls (see [Prompt Caching](#prompt-caching)) |
| `LLM_BROWSE_MAX_LENGTH` | `20000` | Max character length of content returned by `browse_url` and `read_docs` |
| `LLM_BROWSE_TIMEOUT` | `10` | HTTP request timeout in seconds for `browse_url` |
| `LLM_DOCS_BASE_URL` | `https://raw.githubusercontent.com/NativeMojo/django-mojo/refs/heads/main/docs/` | Base URL for fetching framework docs via `read_docs` |
| `FILEMAN_EXPORT_EXPIRES_DAYS` | `14` | Days until assistant `export_data` files expire and are deleted by the cleanup job |

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
| `delete_rule` | `manage_security` | Yes | Delete a single rule condition from a rule set |
| `block_ip` | `manage_security` | Yes | Block an IP fleet-wide with TTL |
| `unblock_ip` | `manage_security` | Yes | Unblock a blocked IP fleet-wide |
| `whitelist_ip` | `manage_security` | Yes | Add IP to whitelist (prevents future auto-blocks, unblocks if blocked) |
| `unwhitelist_ip` | `manage_security` | Yes | Remove IP from whitelist |
| `create_ticket` | `manage_security` | Yes | Create a ticket for human review |

### Jobs Domain (`view_jobs` / `manage_jobs`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `query_jobs` | `view_jobs` | No | Search/filter jobs by status, channel, func, time range |
| `query_job_events` | `view_jobs` | No | Events attached to a specific job |
| `query_job_logs` | `view_jobs` | No | Log lines written by a specific job |
| `get_job_stats` | `view_jobs` | No | Aggregate counts by status and channel |
| `get_queue_health` | `view_jobs` | No | Per-channel queue depth and worker status |
| `list_scheduled_tasks` | `view_jobs` | No | List the user's scheduled tasks |
| `cancel_job` | `manage_jobs` | Yes | Cancel a pending or running job |
| `retry_job` | `manage_jobs` | Yes | Requeue a failed, canceled, or expired job |
| `create_scheduled_task` | `manage_jobs` | Yes | Create a new recurring scheduled task |
| `update_scheduled_task` | `manage_jobs` | Yes | Edit a scheduled task (name, schedule, payload, enabled state) |
| `delete_scheduled_task` | `manage_jobs` | Yes | Delete a scheduled task |
| `run_job` | `manage_jobs` | Yes | Publish a new job by func+payload (fresh run) or by cloning an existing job as a template |
| `run_scheduled_task_now` | `manage_jobs` | Yes | Immediately execute a scheduled task regardless of schedule or enabled state |

### Users Domain (`view_admin` / `manage_users`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `query_users` | `view_admin` | No | Search/filter users by name, email, status, permission |
| `get_user_detail` | `view_admin` | No | Full user profile, permissions, group memberships |
| `get_user_activity` | `view_admin` | No | Recent security events for a user |
| `query_rate_limits` | `view_admin` | No | Currently active rate limit entries from Redis |
| `get_permission_summary` | `view_admin` | No | User permissions breakdown (user-level + group-level) |
| `update_user_permission` | `manage_users` | Yes | Add or remove a permission from a user |
| `disable_user` | `manage_users` | Yes | Disable account + rotate auth_key (invalidates all sessions). Cannot disable yourself. Does not write `metadata.protected.disable.*` — use the REST `disable` action for audited disables. |
| `enable_user` | `manage_users` | Yes | Re-enable a disabled account. Does not update `metadata.protected.disable.*` history. |
| `force_logout` | `manage_users` | Yes | Rotate auth_key to invalidate all sessions (account stays active) |

### Groups Domain (`view_groups`)

| Tool | Permission | Mutates |
|---|---|---|
| `query_groups` | `view_groups` | No |
| `get_group_detail` | `view_groups` | No |
| `get_group_members` | `view_groups` | No |
| `get_group_activity` | `view_groups` | No |

### Metrics Domain (`view_metrics` / `write_metrics` / `view_admin` / `view_security`)

Full reference: [metrics_tools.md](metrics_tools.md).

| Tool | Permission | Mutates | Purpose |
|---|---|---|---|
| `list_metric_accounts` | `view_metrics` | No | Discover every account the user can view (unions configured + data-inferred) |
| `list_metric_categories` | `view_metrics` | No | Categories on a given account |
| `list_metric_slugs` | `view_metrics` | No | Time-series slugs on an account; supports category and prefix filters |
| `list_metric_gauges` | `view_metrics` | No | Gauge (non-time-series) slug names on an account |
| `describe_metric_slug` | `view_metrics` | No | Grep the codebase for `metrics.record()` call sites matching a slug |
| `resolve_group_account` | `view_metrics` | No | Turn a group name/id into the matching `group-<id>` account string |
| `fetch_metrics` | `view_metrics` | No | Time-series fetch; auto-granularity and retention notes |
| `fetch_metric_values` | `view_metrics` | No | Point-in-time snapshot across many slugs |
| `fetch_metrics_by_category` | `view_metrics` | No | Fetch every slug in a category at once, capped for token budget |
| `get_metric_gauge` | `view_metrics` | No | Read one or more gauge values |
| `set_metric_gauge` | `write_metrics` | Yes | Write a gauge (maintenance_mode, feature flags). Mutates — confirm first. |
| `get_system_health` | `view_admin` | No | Cross-domain roll-up: active users, jobs, incidents, events |
| `get_incident_trends` | `view_security` | No | Incident/event trends over 1h/6h/24h/7d |

Every read tool calls `mojo.apps.metrics.rest.helpers.check_view_permissions(request, account)`; the write tool calls `check_write_permissions`. This matches the REST layer exactly: per-account gating for `public`, `global`, `group-<id>`, `user-<id>`, and custom accounts. Denials return `{"error": ...}` and fire a level-5 security event.

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
| `query_model` | `view_admin` | No | Query any MojoModel and return results inline as JSON. Best for small result sets (detail lookups, spot-checking). Respects `RestMeta` permissions and owner/group filtering. Max 200 rows. For exports use `export_data`; for counts/sums use `aggregate_model`. |
| `aggregate_model` | `view_admin` | No | Run aggregate queries (count, sum, avg, min, max, count_distinct) on any MojoModel, with optional `group_by`. Use for summaries — never pull rows just to count or sum them. |
| `export_data` | `view_admin` | Yes | Export query results to a CSV file in file storage (S3). Data is written directly to a `fileman.File` record — not returned inline. Returns a download URL. Use for any export request, especially large result sets. |
| `delete_model_instance` | `view_admin` | Yes | Delete a single model instance by primary key. The model must have `CAN_DELETE = True` in its `RestMeta`, and the user must pass the model's full delete permission chain (`DELETE_PERMS` → `SAVE_PERMS` → `VIEW_PERMS`), including owner and group checks — identical to the REST layer's `on_rest_handle_delete`. Calls `on_rest_pre_delete()` and deletes inside `transaction.atomic()`. Reports a security event on permission denial. Writes `assistant:model:deleted` audit log on success. |
| `save_model_instance` | `view_admin` | Yes | Create or update a single MojoModel instance. Pass `pk` to update an existing row; omit `pk` to create a new one. Creates require `CAN_CREATE=True` plus the `CREATE_PERMS`/`SAVE_PERMS`/`VIEW_PERMS` chain. Updates require `SAVE_PERMS`/`VIEW_PERMS` on the target instance. FK fields can be set by pk in `data`. Runs via `on_rest_save()`. Writes `assistant:model:created` or `assistant:model:updated` audit log on success, `assistant:model:save_failed` on exception. |

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
| `count_only` | boolean | `false` | If true, return only the total count with no row data |

`aggregate_model` parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `app_name` | string | required | Django app label |
| `model_name` | string | required | Model class name |
| `aggregations` | array | required | List of `{field, func, alias?}` objects. `func` is one of `count`, `sum`, `avg`, `min`, `max`, `count_distinct`. `alias` defaults to `{func}_{field}`. Use `id` as the field for row counts. |
| `filters` | object | `{}` | ORM filter dict |
| `group_by` | array | — | Fields to group by (e.g. `["status"]` or `["status", "category"]`). Forward FK fields are accepted by either the relation name or the column name and resolve to the column attname (e.g. `"group"` → `"group_id"`); the resolved column is the key used in result rows. Reverse relations and many-to-many fields are rejected. |
| `having` | object | — | Post-aggregation filter applied after `group_by` + `annotate` (SQL HAVING semantics). Keys must reference an aggregation alias and may use scalar lookup suffixes (`gte`, `gt`, `lte`, `lt`, `exact`, `in`, `isnull`, `range`). Requires `group_by`. Example: `{"total__gte": 2}`. |
| `ordering` | string | — | Order grouped results. Must reference a `group_by` column or an aggregation alias (e.g. `-total` or `group_id`). Relational ordering is not supported. |
| `limit` | integer | `50` | Max grouped rows to return (max 200) |

`export_data` parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `app_name` | string | required | Django app label |
| `model_name` | string | required | Model class name |
| `filters` | object | `{}` | ORM filter dict |
| `search` | string | — | Free-text search using the model's `SEARCH_FIELDS` |
| `ordering` | string | `-pk` | Order by field |
| `limit` | integer | `5000` | Max rows to export (max 50000) |
| `fields` | array | — | Specific fields to include. Defaults to the model's graph config. |
| `graph` | string | `default` | Serialization graph name |

`export_data` requires `fileman` to be installed and a `FileManager` configured for the user/group. Files are stored with `metadata.expires_at` set to `FILEMAN_EXPORT_EXPIRES_DAYS` days from creation (default 14). If `mojo.apps.shortlink` is installed, the returned URL is a shortlink. The assistant should present the URL using a `file` block (see structured block types in the system prompt).

#### `DENY_AI_*` RestMeta flags

Every tool in the Models Domain honors per-model opt-out flags on `RestMeta`: `DENY_AI_VIEW` (describe/query/aggregate/export), `DENY_AI_CREATE`, `DENY_AI_UPDATE`, `DENY_AI_DELETE`, plus `DENY_AI` as a shorthand for all four. All default `False`.

The AI gate runs **before** the REST permission check, so denied requests return a distinct error — `"<app>.<Model> is not available to the assistant"` — to signal that the block is policy, not permission. Denials emit a level-4 `assistant_ai_denied` incident event for operator visibility. REST continues to work unchanged for humans — only the assistant tools honor the flags. See [docs/django_developer/rest/permissions.md](../rest/permissions.md#assistant-access-flags) for details.

`save_model_instance` parameters:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `app_name` | string | required | Django app label |
| `model_name` | string | required | Model class name |
| `pk` | integer | — | Primary key of an existing instance to update. Omit to create. |
| `data` | object | required | Dict of field names to values. FK fields accept the related instance's pk. |

The tool delegates to `instance.on_rest_save(request, data)` so all model-level save hooks, validators, and `POST_SAVE_ACTIONS` fire exactly as they would through the REST API. The `action_response` from `POST_SAVE_ACTIONS` is included in the return dict when present. Setting `CAN_CREATE = False` in `RestMeta` blocks creates; setting `CAN_UPDATE = False` blocks updates to existing instances.

All five tools enforce the same permission and owner/group scoping as the REST layer via `rest_check_permission` and `_apply_owner_group_filter`. Attempts to filter or aggregate on sensitive fields are blocked and reported as security events.

#### `add_context` — Clickable model references

`add_context` is a core tool that lets the LLM attach validated clickable model references to its response. When the LLM mentions specific records (users, jobs, incidents, rulesets, etc.) it can call `add_context` to give admins a direct link to each record instead of having to search for it.

| Attribute | Value |
|---|---|
| Name | `add_context` |
| Domain | `models` |
| Permission | `view_admin` |
| Core | Yes (always sent) |
| Mutates | No |

**Input schema:**

```python
{
    "type": "object",
    "properties": {
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "app_name":   {"type": "string"},   # Django app label, e.g. "incident"
                    "model_name": {"type": "string"},   # Model class name, e.g. "RuleSet"
                    "pk":         {"type": "integer"},  # Primary key of the instance
                    "label":      {"type": "string"},   # Display label, e.g. "SSH brute force blocker"
                },
                "required": ["app_name", "model_name", "pk"],
            },
        },
    },
    "required": ["references"],
}
```

**Validation pipeline** (per reference, silent filter on failure):

1. `app_name`, `model_name`, and `pk` must be present and non-empty
2. `_resolve_model(app_name, model_name)` must succeed (model exists, has RestMeta, no `NO_REST`)
3. `_check_ai_access(model, "view", user)` must pass (honors `DENY_AI_VIEW` / `DENY_AI` flags)
4. `model.objects.filter(pk=pk).exists()` must return True

Invalid references are silently dropped. The tool never errors — if all references fail validation it returns `{"references": []}` and no block is injected.

Maximum 20 references per call. Multiple `add_context` calls in one agent turn are merged into a single `context` block.

**Agent loop behavior:**

- After each tool turn, `_extract_context_refs()` collects validated refs from any `add_context` results
- Refs are accumulated across all turns in `context_refs`
- Before saving the final assistant `Message`, if `context_refs` is non-empty, the agent appends `{"type": "context", "references": context_refs}` to the `blocks` list
- The context block is stored on the `Message.blocks` field alongside any other structured blocks

**System prompt guidance (built in):**

```
When you reference specific records in your responses (users, jobs, incidents, rulesets, etc.),
use add_context to attach clickable links. This lets admins click through directly instead of
having to search for the record you're discussing. Call add_context alongside your final
response — invalid references are silently filtered.
```

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

### Discovery Domain (`view_admin`)

| Tool | Permission | Core | Description |
|---|---|---|---|
| `load_tools` | `view_admin` | Yes | List available domains or load domain tools for this conversation |
| `list_tools` | `view_admin` | No | Full listing of all tools grouped by domain. Loaded via `load_tools(domain="discovery")` |

The domain-specific discovery tools have been moved to their parent domains so they load together with the tools they support:

| Tool | Domain | Permission |
|---|---|---|
| `list_metric_categories` | `metrics` | `view_metrics` |
| `list_metric_slugs` | `metrics` | `view_metrics` |
| `list_job_channels` | `jobs` | `view_jobs` |
| `list_event_categories` | `security` | `view_security` |
| `list_permissions` | `users` | `view_admin` |

`load_tools` (core) is the primary entry point. The LLM calls it to discover what's available and activate domain tools before using them.

### Planning Domain (`view_admin`)

| Tool | Permission | Core | Mutates | Description |
|---|---|---|---|---|
| `create_plan` | `view_admin` | Yes | No | Create a multi-step execution plan shown to the user as a progress tracker |
| `update_plan` | `view_admin` | Yes | No | Update the status of a plan step (`pending`, `in_progress`, `done`, `skipped`) |

Both planning tools are **meta-tools** — they have side effects managed by the agent loop rather than by their handlers. The handler validates input and returns a result dict; the agent loop applies the actual side effect (storing the plan, publishing WS events).

`create_plan` parameters:

| Parameter | Type | Required | Description |
|---|---|---|---|
| `title` | string | Yes | Short plan title shown to the user (e.g. `"Security Audit (24h)"`) |
| `steps` | array | Yes | List of step objects |

Each step object:

| Field | Type | Required | Description |
|---|---|---|---|
| `description` | string | Yes | What this step does |
| `parallel` | boolean | No | If true, run concurrently with other parallel steps. Default false. |
| `tool` | string | No | Tool name to execute. Required when `parallel=true` for auto-execution. |
| `tool_input` | object | No | Input params for the tool. Only used when `tool` is specified. |

`update_plan` parameters:

| Parameter | Type | Required | Description |
|---|---|---|---|
| `step_id` | integer | Yes | Step ID (1-indexed, assigned by `create_plan`) |
| `status` | string | Yes | One of: `pending`, `in_progress`, `done`, `skipped` |
| `summary` | string | No | Brief summary of what was found/done. Include when marking `done`. |

### Memory Domain (`assistant`)

| Tool | Permission | Mutates | Description |
|---|---|---|---|
| `read_memory` | `assistant` | No | Read stored memory entries grouped by tier. The system prompt already injects memories, but this tool lets the LLM check raw keys before updating or deleting. |
| `write_memory` | `assistant` | Yes | Store or update a memory entry. Requires `tier`, `key`, `value`. Enforces key format, size limits, and secret detection. |
| `delete_memory` | `assistant` | Yes | Delete a memory entry by tier and key. |

### Skills Domain (`assistant`)

All four tools are `core=True` — always available without calling `load_tools`.

| Tool | Permission | Core | Mutates | Description |
|---|---|---|---|---|
| `find_skill` | `assistant` | Yes | No | Search for a skill by keywords. Returns matching skills with full step definitions for replay. |
| `save_skill` | `assistant` | Yes | Yes | Create or update a skill with name, description, trigger phrases, and ordered steps. Upserts on name within the same scope. |
| `list_skills` | `assistant` | Yes | No | List all accessible skills grouped by tier. Returns summaries (no step details). |
| `delete_skill` | `assistant` | Yes | Yes | Delete a skill by ID. Owner or admin only. |

See [skills.md](skills.md) for the full model reference, service API, step format, and settings.

## Incident Event Reporting

The assistant reports security-relevant actions and errors to the incident system via `incident.report_event()`. Events flow through the rule engine for automated response (blocking, ticketing, notifications).

### Event Categories

| Category | Level | Trigger |
|---|---|---|
| `assistant:permission_denied` | 5 | User's tool call blocked by permission gate |
| `assistant:permission_denied` | 6 | LLM requested a tool not in the registry |
| `assistant:tool:<name>` | 5 | Successful mutating tool execution (block_ip, disable_user, etc.) |
| `assistant:error` | 6 | Tool handler raised an unhandled exception |
| `assistant:error` | 7 | Agent loop crashed |
| `assistant:error` | 5 | Max tool turns exhausted |
| `assistant:error:api` | 7 | LLM API auth failure or model not found |
| `assistant:error:api` | 5 | LLM API rate limit hit |
| `assistant:error:serialize` | 7 | Tool result could not be serialized to JSON (datetime/Decimal/UUID/Model fallback failed) |
| `assistant:error:parallel` | 6 | A parallel tool call or plan step raised an exception |
| `assistant:error:unhandled` | 8 | Catch-all agent-loop exception not covered by a more specific category |

### Design

- **Permission denied = always an event.** These are security signals for probing/brute-force detection.
- **Mutating tools = event on success only.** No event when the tool returns an error dict (operation failed).
- **Read-only tools = no events.** Too high volume, low signal.
- **Events supplement, not replace, file logging.** Existing `logger` calls remain for debug.
- **`_report_event()` never raises** — wrapped in try/except to avoid breaking the assistant if the incident system is down.

### Audit Trail (`logit.Log`)

The model mutation tools (`save_model_instance`, `delete_model_instance`) write per-row audit entries to `logit.Log` via `_audit_user_log()` in addition to the incident events above. These entries are queryable with the `query_logs` assistant tool (filter by `kind`).

| `kind` | When written |
|---|---|
| `assistant:model:created` | `save_model_instance` — new row created successfully |
| `assistant:model:updated` | `save_model_instance` — existing row updated successfully |
| `assistant:model:deleted` | `delete_model_instance` — row deleted successfully |
| `assistant:model:save_failed` | `save_model_instance` — `on_rest_save()` raised an exception |

**What is recorded:**

- The `logit.Log` message includes the action, model label (`app.Model`), `pk`, and changed field **names** (never values).
- The `payload` JSON carries `conversation_id` (when available) so the audit trail ties back to the specific assistant turn.
- Sensitive field names (`password`, `auth_key`, etc.) are not filtered from the field name list — the field names themselves are considered safe metadata; only values are withheld.
- Entries are written against the target model (`model_name`, `model_id`) so they surface when querying logs for that model.

### Suggested RuleSets

| Name | Category | Bundle | Trigger | Handler |
|---|---|---|---|---|
| Permission Probing | `assistant:permission_denied` | SOURCE_IP | 5 in 10 min | `ticket://?priority=7` |
| Rapid Permission Changes | `assistant:tool:update_permission` | SOURCE_IP | 3 in 5 min | `ticket://?priority=8,notify://perm@manage_security` |
| Error Spike | `assistant:error` | HOSTNAME | 10 in 30 min | `notify://perm@manage_security` |

### Key Files

- `mojo/apps/assistant/services/agent.py` — `_report_event()` helper, events in both REST and WS agent loops
- `mojo/apps/assistant/handler.py` — events for WS permission denied and handler/thread crashes

## Memory System

The assistant has persistent three-tier memory stored in Redis. Memories are injected into the system prompt at the start of every conversation so the LLM has continuous context across sessions.

### Tiers

| Tier | Redis Key | Who Reads | Who Writes | Injected When |
|---|---|---|---|---|
| **Global** | `assistant:memory:global` | `assistant` perm or superuser | `assistant` perm or superuser | Every conversation |
| **User** | `assistant:memory:user:<user_id>` | The user themselves, or superuser | The user themselves, or superuser | Conversations by that user |
| **Group** | `assistant:memory:group:<group_id>` | Any member of the group | Members with `assistant` perm on their Member record | Conversations in group context |

Groups are resolved from the Conversation's `group` FK. The `group` field is set when a conversation is created in a group context (via REST or WebSocket with `group_id`).

### System Prompt Injection

At conversation start, `_get_system_prompt(user, group)` calls `build_memory_prompt()`, which loads all applicable memory tiers and formats them as a `## Memory` section:

```
## Memory

### Platform
- platform: Healthcare SaaS (HIPAA-compliant) on AWS us-east-1
- internal_ips: Never block 10.0.0.0/8 or 172.16.0.0/12

### Your Notes
- preferred_channel: Prefers Slack notifications for non-critical alerts

### Group: Acme Corp
- deploy_window: Deploys run Tuesdays 9-11 PM UTC only
```

Empty tiers are omitted. When global memory is empty and the user has `assistant` permission, an onboarding prompt is injected instead — it instructs the LLM to ask the user about their platform and store answers as global memories.

### Memory Limits

Configurable per tier:

| Setting | Default | Description |
|---|---|---|
| `LLM_ADMIN_MEMORY_ENABLED` | `True` | Feature flag — disables all memory when False |
| `LLM_ADMIN_MEMORY_GLOBAL_MAX` | `50` | Max entries in the global tier |
| `LLM_ADMIN_MEMORY_USER_MAX` | `30` | Max entries per user tier |
| `LLM_ADMIN_MEMORY_GROUP_MAX` | `40` | Max entries per group tier |
| `LLM_ADMIN_MEMORY_ENTRY_MAX_CHARS` | `500` | Max character length per entry value |

### Key Format

Keys must be lowercase alphanumeric with colons, underscores, or hyphens, max 64 characters. Examples: `platform`, `rule:internal_ips`, `preferred_channel`. The reserved field `_meta` is used internally.

### Secret Detection

The write path rejects values matching known secret patterns (API keys, passwords, tokens, connection strings). Writes containing these patterns return an error dict.

### LLM Tools

Three tools in the `memory` domain (all require `assistant` permission):

| Tool | Mutates | Description |
|---|---|---|
| `read_memory` | No | Read raw entries grouped by tier. The system prompt already has memories, but this is useful before updating or deleting. Optional `tier` param. |
| `write_memory` | Yes | Create or update an entry. Params: `tier`, `key`, `value`. Enforces size limits, key format, and secret detection. |
| `delete_memory` | Yes | Remove an entry. Params: `tier`, `key`. |

The group context is passed via `user._assistant_group` — set on the user object by the agent loop before tools are called.

### Nightly Cleanup Job

Register `mojo.apps.assistant.jobs.assistant_memory_cleanup` as a scheduled job. Two phases run nightly:

**Phase 1 — Mechanical** (always runs, no LLM):
- Orphan cleanup: delete memory hashes for users/groups that no longer exist
- Size enforcement: prune oldest-touched entries if a tier is over its limit
- Suspicious pattern scan: log warnings for entries matching secret patterns

**Phase 2 — Dreaming** (conditional, uses LLM):
- Only runs per tier if memory was modified since the last dream pass, or if `LLM_ADMIN_MEMORY_DREAM_INTERVAL` days have elapsed
- LLM reviews all entries and proposes `keep`, `delete`, `rewrite`, or `merge` actions
- Changes are logged before application (original values preserved in log)
- `LLM_ADMIN_MEMORY_DREAM_AUTO_APPLY = False` switches to log-only mode

| Setting | Default | Description |
|---|---|---|
| `LLM_ADMIN_MEMORY_DREAM_ENABLED` | `True` | Enable/disable the dreaming phase |
| `LLM_ADMIN_MEMORY_DREAM_AUTO_APPLY` | `True` | If False, proposed changes are logged but not applied |
| `LLM_ADMIN_MEMORY_DREAM_INTERVAL` | `7` | Days between forced dream passes even without changes |

### Service API

```python
from mojo.apps.assistant.services.memory import (
    read_memories,      # Read all applicable tiers for a user/group context
    write_memory,       # Write a single entry (validates key, value, secrets, limits)
    delete_memory,      # Delete a single entry
    build_memory_prompt,  # Build the ## Memory section string for the system prompt
    is_global_empty,    # Check if global tier has zero entries (used for onboarding)
)
```

All functions degrade gracefully when Redis is unavailable — they return empty results or error dicts rather than raising.

### Key Files

- `mojo/apps/assistant/services/memory.py` — all memory operations, cleanup, dreaming
- `mojo/apps/assistant/services/tools/memory.py` — `read_memory`, `write_memory`, `delete_memory` tool handlers
- `mojo/apps/assistant/rest/memory.py` — REST endpoints for memory management
- `mojo/apps/assistant/jobs.py` — nightly cleanup job entry point

## Context Conversations

Create a conversation pre-loaded with the full context of any MojoModel instance. The UI calls this to provide an "Open in Assistant" button from any detail view (tickets, incidents, or any other model).

### Endpoint

`POST /api/assistant/context` — requires `view_admin` + the model's own `VIEW_PERMS`. Like `POST /api/assistant`, this is gated with `@md.requires_global_perms('view_admin', 'assistant')` — the grant must be global on the User, not a group/member-scoped permission.

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
| `core` | No | Default `False`. If `True`, tool is always sent to the LLM (two-tier tier 1). Set this for tools that should be available in every conversation without loading a domain. |

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
| `core` | No | Default `False`. If `True`, always included in every conversation (tier 1). |

### Tool Handler Guidelines

- **Bound results**: Always cap query results (50 is the convention). Never return unbounded querysets.
- **Exclude secrets**: Never include passwords, auth keys, tokens, or other sensitive fields.
- **Return simple types**: Return dicts and lists with JSON-serializable values. Dates as strings.
- **Handle errors gracefully**: Return `{"error": "message"}` instead of raising exceptions.
- **Signature**: `(params, user)` is the baseline. Handlers that need HTTP context or the conversation can opt in to additional keyword-only arguments.

### Optional Handler Kwargs

The dispatcher inspects handler signatures at first call (result is cached). Handlers declare the kwargs they need; the dispatcher only passes them when present. Handlers that stick to `(params, user)` are untouched.

| Kwarg | Type | Description |
|---|---|---|
| `request_meta` | `objict` or `None` | Slim HTTP context from the originating request: `ip`, `user_agent`, `path`, `method`. `None` when there is no HTTP request (e.g. WS path or programmatic call). |
| `conversation` | `Conversation` instance | The active Conversation model instance. Use to read metadata or to associate audit records with the conversation. |

Both kwargs are keyword-only (`*` in the signature):

```python
@tool(name="my_tool", domain="myapp", permission="view_admin", mutates=True,
      description="...", input_schema={...})
def _tool_my_tool(params, user, *, request_meta=None, conversation=None):
    # request_meta.ip is the originating client IP (or None)
    # conversation.pk links audit records to the turn
    ...
```

`run_assistant()` now accepts an optional `request=None` parameter. When the REST endpoint passes the Django request through, `request_meta` is populated automatically for all tool handlers that opt in. WebSocket calls do not pass a request; `request_meta` is `None` in that path.

### Other Registry Functions

```python
from mojo.apps.assistant import (
    tool,                        # @tool decorator (preferred)
    register_tool,               # Register a single tool imperatively
    register_tools,              # Register multiple tools from a list of dicts
    get_registry,                # Get the full registry dict (all tools, unfiltered)
    get_tools_for_user,          # All tools the user has permission for (backward compat)
    get_core_tools_for_user,     # Core tools only (tier 1, always sent)
    get_domain_tools_for_user,   # Tools for specific domains, filtered by perms
    get_available_domains,       # Domains the user has access to, with tool counts
)
```

`get_domain_tools_for_user(user, domains)` accepts a list or single string. Returns only tools where `entry["domain"] in domains` and the user has permission.

`get_available_domains(user)` returns a dict keyed by domain name, each with `count`, `description`, and `examples` (first 3 tool names). Domains where every tool is already core are excluded — they're always loaded and don't need to be "activated".

## Models

### Conversation

Tracks a multi-turn conversation between a user and the assistant.

| Field | Type | Description |
|---|---|---|
| `user` | FK(User) | Owner of the conversation |
| `group` | FK(Group, nullable) | Optional group context. Set when the conversation is started in a group context. Used for group-tier memory injection. |
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
| `tool_calls` | JSONField | For `assistant` role: only `tool_use` blocks (`null` when none). For `tool_result` role: tool result payloads. `null` for user messages. |
| `blocks` | JSONField | Pre-parsed structured data blocks extracted at write time. `null` when none present. |
| `duration_ms` | IntegerField | Wall-clock time for the full agent loop in milliseconds. `null` for non-assistant messages. |
| `usage` | JSONField | Summed token counts across all `llm.call()` turns for this exchange: `{cache_read_input_tokens, cache_creation_input_tokens, input_tokens, output_tokens}`. Only on the final assistant `Message` per exchange; `null` otherwise. See [Prompt Caching](#prompt-caching). |
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
      assistant_text       (intermediate prose from a turn that also calls tools)
      assistant_tool_call  (per tool)
      assistant_plan       (when LLM creates a task plan)
      assistant_plan_update (per plan step status change)
      assistant_response   (final answer, terminal)
      assistant_error      (on failure, terminal)
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

1. The system prompt defines seven LLM-authored block types: `table`, `chart`, `stat`, `action`, `list`, `alert`, `progress`. The agent loop also injects a `context` block when `add_context` was called during the turn.
2. The LLM wraps structured data in ` ```assistant_block ` code fences within its text response
3. `_parse_blocks()` in `agent.py` extracts valid blocks, runs `_validate_block()`, and strips the fences from the text
4. The response includes both clean `response` text and a `blocks` array
5. Clean text and parsed blocks are stored on the `Message` — no re-parsing happens at read time

### System Prompt Behavior — Narrative Text

The system prompt instructs the LLM to keep narrative text brief when blocks carry the data. The text provides interpretation (key takeaways, context, warnings); the blocks carry the detail. The LLM should not repeat in prose what is already shown in a table, chart, or stat block.

This is enforced via the system prompt, not code. Override `LLM_ADMIN_SYSTEM_PROMPT` in settings to change this behavior.

### Block Types

- **`table`** — `{type, title, columns, rows}` — query results, comparisons
- **`chart`** — `{type, chart_type, title, labels, series}` — line/bar/pie/area for trends. Optional render hints: `stacked`, `grouped`, `crosshair_tracking` (line/area), `cutout`, `show_labels`, `show_percentages` (pie), `colors` (palette), per-series `color`, `show_legend`, `legend_position`
- **`stat`** — `{type, items: [{label, value}]}` — dashboard key metrics
- **`action`** — `{type, title, description, actions: [{label, value}], action_id}` — user confirmation dialogs for mutating operations
- **`list`** — `{type, title, items: [{label, value}]}` — single-record key/value summaries
- **`alert`** — `{type, level, title, message}` — important warnings, errors, success notices
- **`context`** — `{type, references: [{app_name, model_name, pk, label}]}` — injected by the agent loop when `add_context` was called; not authored directly by the LLM in block fences

### Block Validation

`_validate_block()` in `agent.py` enforces structural requirements beyond just the type check. Invalid blocks are silently dropped.

| Block type | Validation rules |
|---|---|
| `action` | `actions` must be a non-empty list. The block is tagged with a unique `action_id` (UUID string) on parse. |
| `alert` | `level` must be one of `info`, `success`, `warning`, `error`. `message` must be non-empty. |
| `list` | `items` must be a non-empty list. |
| `chart` | `chart_type` must be one of `line`, `bar`, `pie`, `area`. `labels` must be a non-empty list. `series` must be a non-empty list of `{name, values}` dicts; every `series[i].values` length must equal `len(labels)`. Recoverable fields are coerced rather than dropping the chart: `cutout` is clamped to `[0, 1]`; `stacked` is stripped if not in `{True, False, "auto"}`; `crosshair_tracking` is coerced to `bool`; `colors` is stripped if non-list and non-null. Unknown top-level fields pass through unchanged for forward compatibility. |
| `progress` | No extra validation beyond type membership. |
| `context` | `references` must be a non-empty list. The block is injected by the agent loop, not parsed from LLM text. |

### `action` Block

Used for mutating operations that need user confirmation. The LLM presents an action card; the user clicks a button; the choice is sent back as a message.

```json
{
    "type": "action",
    "title": "Block IP",
    "description": "Block 1.2.3.4 on all firewall sets for 24 hours",
    "actions": [
        {"label": "Confirm", "value": "confirm"},
        {"label": "Cancel", "value": "cancel"}
    ],
    "action_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

`action_id` is injected by the parser — the LLM does not set it. The frontend uses it to correlate the button click with the block.

The system prompt instructs the LLM to use action blocks instead of asking "type yes to confirm". Always include a Cancel option.

### `list` Block

Used for single-record key/value summaries instead of a one-row table.

```json
{
    "type": "list",
    "title": "User Detail",
    "items": [
        {"label": "Email", "value": "admin@example.com"},
        {"label": "Role", "value": "Admin"},
        {"label": "Last Login", "value": "2026-04-01 09:30 UTC"}
    ]
}
```

### `alert` Block

Used for permission denials, rate limit warnings, error conditions, and success confirmations that need visual distinction from narrative text.

```json
{
    "type": "alert",
    "level": "warning",
    "title": "Rate Limited",
    "message": "User exceeded 100 req/min threshold. Current rate: 142 req/min."
}
```

Valid levels: `info`, `success`, `warning`, `error`.

### Customizing

Override `LLM_ADMIN_SYSTEM_PROMPT` to change the block format instructions. The parser (`_parse_blocks`) only requires valid JSON with a recognized `type` field inside ` ```assistant_block ` fences, validated by `_validate_block()`.

See [web_developer/assistant/README.md](../../web_developer/assistant/README.md#structured-data-blocks) for the full block schema reference and frontend rendering examples.

## Task Planning

For complex requests requiring 3+ tool calls across different areas, the LLM can create an explicit execution plan using the `create_plan` / `update_plan` meta-tools. The plan is stored in `conversation.metadata["plan"]` and displayed to the user as a live progress tracker.

### How It Works

1. The LLM calls `create_plan` with a title and list of steps
2. The agent loop stores the plan in `conversation.metadata["plan"]` and publishes a `"plan"` WS event
3. Steps marked `parallel=True` with a `tool` and `tool_input` are executed concurrently by `_execute_parallel_plan_steps()` immediately after `create_plan` returns — the LLM does not need to call those tools itself
4. Each parallel step is marked `in_progress` at start, then `done` (with a brief summary) on completion
5. Sequential steps (no `tool` field) are handled by the LLM itself using `update_plan` calls
6. After all steps complete, the LLM synthesizes findings into a final response

### Plan Structure

```python
# Stored in conversation.metadata["plan"]
{
    "plan_id": "f47ac10b-...",   # UUID string
    "title": "Security Audit (24h)",
    "steps": [
        {
            "id": 1,                    # 1-indexed, assigned by create_plan
            "description": "Check open incidents",
            "status": "done",           # pending | in_progress | done | skipped
            "summary": "14 open",       # set when done
            "parallel": True,
            "tool": "query_incidents",
            "tool_input": {"status": "open"},
        },
        {
            "id": 2,
            "description": "Synthesize findings",
            "status": "pending",
            "summary": None,
            "parallel": False,
        },
    ],
}
```

### When the LLM Should Plan

The system prompt provides guidance — do not plan for simple queries. Examples where planning is appropriate:
- "Give me a security audit" — parallel steps for incidents, events, blocked IPs, etc.
- "What's the system health?" — parallel steps for jobs, metrics, incidents
- "Investigate this user" — steps for user detail, activity, rate limits, related incidents

Single-tool queries, follow-ups, and simple mutations do not need a plan.

### WS Events

| Event | When | Key Payload Fields |
|---|---|---|
| `assistant_plan` | After `create_plan` succeeds | `{plan: {plan_id, title, steps}}` |
| `assistant_plan_update` | After each step status change | `{plan_id, step_id, status, summary}` |

### Key Implementation Files

- `mojo/apps/assistant/services/agent.py` — `_handle_plan_tool()`, `_execute_parallel_plan_steps()`, plan injection in both `run_assistant` and `run_assistant_ws`
- `mojo/apps/assistant/services/tools/planning.py` — `create_plan` and `update_plan` tool handlers

## Parallel Tool Execution

When the LLM requests multiple tool calls in a single turn, they are executed in parallel using `ThreadPoolExecutor`.

### Execution Order

1. **Meta-tools run first, serially** — `load_tools`, `create_plan`, `update_plan` have side effects on the conversation state (modifying `metadata`, injecting tools, publishing WS events). These always run before regular tools.
2. **Regular tools run in parallel** — when the LLM requests 2+ non-meta tools in one turn, they are submitted to a `ThreadPoolExecutor`.
3. Results are returned to the LLM in completion order.

### Configuration

```python
LLM_ADMIN_MAX_PARALLEL_TOOLS = 4  # default
```

The pool size is `min(LLM_ADMIN_MAX_PARALLEL_TOOLS, number_of_tools_in_batch)`.

### Thread Safety

- Each tool receives its own `user` reference. Tools do not share mutable state.
- `tool_calls_made` is a shared list — appended from threads. This is safe for CPython due to the GIL, but the append order is non-deterministic with parallel execution.
- The `on_event` callback (WS event publisher) may be called from multiple threads simultaneously. The underlying `send_to_user()` call is safe because it publishes to a queue; but `assistant_tool_call` events may arrive out of order when tools run in parallel.

### Timeouts

Each parallel tool call has a 30-second `future.result(timeout=30)` timeout. A timed-out or failed tool returns `{"error": "Tool execution timed out or failed."}` — the rest of the batch continues normally.

### Key Implementation Files

- `mojo/apps/assistant/services/agent.py` — `_execute_tool()`, `_execute_tools()`, `_execute_parallel_plan_steps()`

## Prompt Caching

The assistant uses Anthropic's automatic prompt caching to cut the cost and latency of multi-turn agent loops. Cache hits cost ~10% of base input tokens; cache writes cost 25% more. Across a typical 25-turn loop the reads dominate by a wide margin.

### How It Works

`mojo.helpers.llm.call()` adds `cache_control={"type": "ephemeral"}` at the top level of every Anthropic request. Anthropic walks back from the last cacheable block looking for a previously written cache entry; on hit it reuses the cached prefix (system + tools + prior messages), on miss it writes a new entry. The cache breakpoint advances automatically as the conversation grows — no manual breakpoint management.

Enable / disable via the `LLM_ADMIN_PROMPT_CACHE_ENABLED` setting (default `True`).

### What Invalidates the Cache

Anthropic's cache hierarchy is `tools → system → messages`. A change at any level invalidates that level and everything below.

| Change | Tools | System | Messages | When it happens |
|---|---|---|---|---|
| `load_tools` fires mid-conversation | invalid | invalid | invalid | Domain tools added → new tool definitions array |
| `save_skill` / `update_skill` / `delete_skill` | valid | invalid | invalid | Skill catalog re-injected into system prompt next turn |
| `write_memory` / `update_memory` / `delete_memory` | valid | invalid | invalid | Memory section re-injected into system prompt next turn |
| New user message in same conversation | valid | valid | (extends) | Normal turn — previous prefix reads from cache |

The cache TTL is 5 minutes; idle conversations resumed after that miss the cache and re-write on the first new turn.

### Minimum Cacheable Prefix

Anthropic silently ignores `cache_control` below a per-model threshold:

- Sonnet: 1024 tokens
- Opus: 4096 tokens

If the first call after process start returns both `cache_creation_input_tokens == 0` and `cache_read_input_tokens == 0`, `llm.call()` logs a one-time WARN to `llm.log` so operators know their prompt is too small to cache.

### Observing Cache Effectiveness

`mojo.apps.assistant.models.Message.usage` (new JSONField) carries the summed token counts for each user-message exchange:

```json
{
  "cache_read_input_tokens": 8200,
  "cache_creation_input_tokens": 350,
  "input_tokens": 42,
  "output_tokens": 510
}
```

Stored only on the final assistant `Message` of each user-message exchange, mirroring how `duration_ms` is recorded. Sum across turns. Exposed in the `default` REST graph for the frontend.

Per-turn detail (one line per `llm.call()`) is written to `assistant.log` at INFO:

```
llm turn conv=42 turn=3 cache_read=8200 cache_write=0 input=42 output=180
```

### Total Input Tokens

`usage.input_tokens` represents only the tokens **after the last cache breakpoint**, not the total input. Compute the total via:

```text
total = cache_read_input_tokens + cache_creation_input_tokens + input_tokens
```

### Data Retention

Prompt caching is ZDR-eligible per Anthropic. Cache entries are key-value representations held in memory, isolated per organization (and per workspace on the Claude API as of 2026-02-05), and expire after the TTL. No additional data exposure surface.

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
- `tests/test_assistant/12_test_incident_reporting.py` — Incident event reporting: permission denied events, mutating tool events, error events, category conventions
- `tests/test_assistant/13_test_memory.py` — Memory service: read/write/delete, tier permissions, key validation, secret detection, size limits, build_memory_prompt, onboarding flag
- `tests/test_assistant/14_test_memory_cleanup.py` — Nightly cleanup: mechanical phase (orphans, size prune, suspicious scan), dreaming phase (should_dream, dream_tier, apply_dream_actions)
- `tests/test_assistant/32_test_context_refs.py` — `add_context` tool: valid refs, invalid model/app/pk filtering, DENY_AI_VIEW filtering, mixed refs, empty input, `_validate_block` for context type, `_extract_context_refs` helper
- `tests/test_assistant/33_test_prompt_caching.py` — Prompt caching: `cache_control` injection/omission, usage round-trip, `_accumulate_usage` summation and null tolerance, `run_assistant()` usage persistence and per-turn logging, `Message.usage` graph exposure and nullability, zero-cache one-time warning
