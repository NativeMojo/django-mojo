# Two-Tier Tool Loading

**Type**: request
**Status**: planned
**Date**: 2026-04-06
**Priority**: high

## Description

The assistant currently sends all 70 tool definitions to the LLM on every API call. This creates two problems:

1. **Tool selection quality degrades** — the LLM can't effectively choose between 70 tools, gravitates toward general-purpose tools like `query_model`, and incorrectly labels dedicated tools as "broken" when they return empty results.

2. **Token waste** — 70 tool schemas consume significant context on every turn, even when most tools are irrelevant to the conversation.

The fix is two-tier loading: send a small core set (~15 tools) on every call, and load domain-specific tools on demand when the LLM determines it needs them. This mirrors how Claude Code uses `ToolSearch` — start minimal, expand when needed.

## Observed Behavior

From production:
- LLM bypasses `query_job_events` and `query_job_logs` in favor of `query_model`
- LLM states tools are "broken/unsupported" when they return empty results
- LLM fails to find `write_memory` despite it being registered and available
- With 70 tools, the LLM latches onto the few it's familiar with and ignores the rest

## Goal

Reduce per-call tool count from 70 to ~15 core tools. Domain tools (security, jobs, users, etc.) load on demand via a `load_tools` tool call, persisting for the rest of the conversation.

## Plan

**Status**: planned
**Planned**: 2026-04-06

### Objective

Replace the all-at-once 70-tool payload with two-tier loading: ~13 core tools always sent, domain tools loaded on demand via `load_tools` and persisted per conversation.

### Tool Classification

**Core tools (always sent, 13 tools):**
- `memory`: `read_memory`, `write_memory`, `delete_memory` (3)
- `discovery`: `load_tools` (1) — gateway to domain tools
- `models`: `describe_model`, `query_model` (2)
- `docs`: `read_docs` (1)
- `web`: `browse_url` (1)
- `logs`: `query_logs` (1)
- `files`: `query_files`, `get_file`, `analyze_image` (3)

**Domain tools (loaded on demand, ~57 tools):**
- `security` (28 + `list_event_categories`): incidents, events, tickets, rulesets, IP management
- `jobs` (7 + `list_job_channels`): query, stats, queue health, cancel, retry
- `users` (9 + `list_permissions`): query, detail, activity, rate limits, permissions, enable/disable
- `groups` (6): query, detail, members, activity, create, invite
- `metrics` (3 + `list_metric_categories`, `list_metric_slugs`): fetch, health, trends
- `discovery` (1): `list_tools` — full tool listing, available on demand

### Steps

1. **`mojo/apps/assistant/__init__.py`** — Add `core=False` parameter to `register_tool()` and `@tool` decorator. Add it to the `_REGISTRY` entry dict. Add `get_core_tools_for_user(user)` that returns only `core=True` tools. Add `get_domain_tools_for_user(user, domains)` that returns tools for specified domains. Add `get_available_domains(user)` that returns `{domain: {count, description, tools_summary}}` for domains the user has permission to access.

2. **`mojo/apps/assistant/services/tools/memory.py`** — Add `core=True` to all three `@tool` decorators.

3. **`mojo/apps/assistant/services/tools/models.py`** — Add `core=True` to `describe_model` and `query_model`.

4. **`mojo/apps/assistant/services/tools/docs.py`** — Add `core=True` to `read_docs`.

5. **`mojo/apps/assistant/services/tools/web.py`** — Add `core=True` to `browse_url`.

6. **`mojo/apps/assistant/services/tools/logs.py`** — Add `core=True` to `query_logs`.

7. **`mojo/apps/assistant/services/tools/files.py`** — Add `core=True` to `query_files`, `get_file`, `analyze_image`.

8. **`mojo/apps/assistant/services/tools/discovery.py`** — Add `load_tools` with `core=True`. Keep `list_tools` as non-core:
   - `load_tools` (core): Accepts `domain` as a string or `domains` as a list. When called with no domain/domains, returns available domains with tool count, brief description, and 2-3 example tool names per domain. Does NOT load any tools — just shows what's available. When called with domain(s), returns the tool names + descriptions for those domains. The agent loop adds those tools to the active set for subsequent turns.
   - `list_tools` (non-core): Stays as-is — lists all tools the user has access to. Available when discovery domain is loaded. Not sent on every turn.
   - Move `list_job_channels` to `jobs.py` (domain `jobs`), `list_event_categories` to `security/events.py` (domain `security`), `list_metric_categories` and `list_metric_slugs` to `metrics.py` (domain `metrics`), `list_permissions` to `users.py` (domain `users`).

9. **`mojo/apps/assistant/services/agent.py`** — Modify both `run_assistant()` and `run_assistant_ws()`:
   - **Build initial tool list**: Start with `get_core_tools_for_user(user)`. Then load active domains from `conversation.metadata.get("active_domains", [])` and merge domain tools via `get_domain_tools_for_user(user, active_domains)`.
   - **Backward compatibility**: If `active_domains` is not in metadata AND conversation history contains `tool_use` blocks, fall back to `get_tools_for_user(user)` (all tools). This ensures old conversations from before two-tier loading don't break when history references tools not in the core set. Only new conversations get the lean start.
   - **`load_tools` detection**: In the tool-call processing loop, after executing `load_tools`, check if `tool_name == "load_tools"`. If so, extract the domain(s) from `tool_input`, add to `conversation.metadata["active_domains"]`, save metadata, and merge the new domain tool definitions into the `tools` list for subsequent turns in the same loop. Extract this into a shared `_handle_load_tools(conversation, tool_input, tools, user)` helper to avoid duplicating in both `run_assistant()` and `run_assistant_ws()`.
   - **Execution always works**: Tool execution still uses the full `get_registry()` — if the LLM somehow calls an unloaded tool (e.g., from conversation history), it still executes. Loading controls what the LLM *sees*, not what it's *allowed* to call.

10. **`mojo/apps/assistant/services/agent.py`** — Update system prompt:
    - Remove the "## Tool Selection" section (no longer needed — the LLM only sees relevant tools now).
    - Replace with "## Tool Loading" guidance:
      - "You start with core tools (memory, models, docs, web, logs, files). For domain-specific work, call `load_tools` to discover and load additional tools."
      - "Loaded tools persist for the rest of this conversation."
      - Auto-load behavior: "When the user's request clearly maps to a domain (e.g., 'show me failed jobs' → jobs, 'who logged in today?' → users/security), load the domain tools without asking. When the request is ambiguous (e.g., 'something seems off'), ask the user which area to investigate before loading."
      - "If a tool returns empty results, that means no matching data exists — it does not mean the tool is broken."

### Design Decisions

- **`core=True` on the decorator**: Minimal change to registration — each tool self-declares whether it's core. No separate config file to maintain. External project tools default to `core=False` (domain tools), which is the right default.
- **Conversation-scoped, not turn-scoped**: Active domains persist in `conversation.metadata["active_domains"]`. Resuming a conversation auto-loads previously activated domains. The LLM doesn't have to re-load tools when continuing a multi-turn conversation.
- **Full registry still used for execution**: Loading controls visibility (what tool schemas the LLM sees), not permission (what it's allowed to call). If the LLM somehow names an unloaded tool, it still works — the permission gate is the real security boundary. This prevents weird edge cases with conversation history containing tool calls from unloaded domains.
- **`load_tools` is the primary tool, `list_tools` stays as non-core**: `load_tools` is the core gateway — no-arg lists domains, with-arg loads a domain. `list_tools` remains available as a non-core tool (loads with discovery domain) for users who explicitly ask "list all your tools." The LLM learns `load_tools` as the one it uses routinely.
- **`load_tools` accepts single or multiple domains**: `domain` (string) for single, `domains` (list) for multi-domain loading in one call. The LLM can load `["users", "security"]` in a single tool call instead of two sequential calls. Both parameters are optional — omitting both triggers the domain listing mode.
- **Auto-load when obvious, ask when ambiguous**: "Show me failed jobs" → auto-load jobs. "Something seems off" → ask the user. The LLM shouldn't ask permission for clear domain matches — that's friction. But ambiguous requests deserve clarification to avoid loading 3 domains and flooding the user with irrelevant data.
- **Discovery tools move to their parent domains**: `list_job_channels` is only useful when working with jobs — it should load with the jobs domain. Same for `list_event_categories` (security), `list_metric_*` (metrics), and `list_permissions` (users). This keeps core clean and domain bundles self-contained.
- **Domain descriptions include example tool names**: When listing domains, include a one-sentence description + tool count + 2-3 example tool names per domain. The LLM can pattern-match from examples without seeing all schemas.
- **Backward compatibility for old conversations**: If `active_domains` is missing from conversation metadata AND the history contains `tool_use` blocks, fall back to sending all tools. Old conversations from before two-tier loading work unchanged. Only new conversations (no history, or metadata has `active_domains`) get the lean core-only start.
- **Shared `_handle_load_tools` helper**: The meta-tool logic (detect `load_tools` call → update metadata → inject tools) is extracted into a helper function shared between `run_assistant()` and `run_assistant_ws()` to avoid code duplication.

### User Interaction Patterns

How domain loading happens in practice — the LLM decides based on user intent:

| User message | LLM behavior | Why |
|---|---|---|
| "Show me failed jobs" | Auto-load `jobs`, query immediately | Clear domain match — no friction |
| "Load the security tools" | Load `security` | Explicit request |
| "What can you help with?" | Call `load_tools()` (no domain), describe available domains | Discovery mode |
| "Something seems off" | Ask: "I can check security incidents, jobs, users, or system metrics — which area?" | Ambiguous — ask first |
| "Why is this user getting rate limited?" | Auto-load `users` + `security` | Multi-domain but clear intent |
| "What happened at 2am?" | Ask what area to investigate, or load `jobs` + `security` if context suggests ops | Depends on conversation context |
| "Check the incident queue and any stuck jobs" | Auto-load `security` + `jobs` | Two clear domains in one request |
| "Show me everything" | Call `load_tools()`, describe domains, ask which to start with | Too broad — guide the user |

**Rule**: auto-load when the domain is obvious from the request. Ask when it's genuinely ambiguous. Never ask for permission on a clear match — that's unnecessary friction.

### Edge Cases

- **Conversation with pre-loaded domains resumes**: Agent reads `conversation.metadata["active_domains"]` and loads those domain tools automatically on the first turn. No re-discovery needed.
- **Old conversation from before two-tier loading**: `active_domains` is missing from metadata. If history contains `tool_use` blocks, fall back to all tools — backward compatible. If no history (shouldn't happen for old conversations), use core only.
- **User loses permission mid-conversation**: `get_domain_tools_for_user` filters by permission, so even if a domain is in `active_domains`, tools the user can't access won't be in the tool list. Execution also checks permission.
- **External project tools**: Default to `core=False`, domain `"custom"`. Loaded via `load_tools(domain="custom")`. If a project wants a tool always available, they set `core=True` in their `register_tool` call.
- **Empty domain**: If a user has no permission for any tools in a domain, `load_tools` returns an empty list with a note. The domain is not added to `active_domains`.
- **All domains loaded**: If the LLM loads every domain, we're back to 70 tools — but this is fine because it's intentional and the LLM has context about why it needs each domain. The problem was 70 tools on turn 1 with no context.
- **Multi-domain load**: `load_tools(domains=["security", "jobs"])` loads both in one call, one turn. Domains already loaded are skipped (no duplicate tools in the list).

### Testing

- `tests/test_assistant/15_test_two_tier_tools.py`:
  - `test_core_tools_only_on_first_turn` — verify only core tools (13) returned by `get_core_tools_for_user`, not the full 70
  - `test_load_tools_lists_domains` — `load_tools()` with no domain returns domain list with counts, descriptions, and example tool names
  - `test_load_tools_loads_single_domain` — `load_tools(domain="jobs")` returns job tool definitions
  - `test_load_tools_loads_multiple_domains` — `load_tools(domains=["jobs", "users"])` returns tools from both domains
  - `test_loaded_domain_persists_in_metadata` — after loading, `conversation.metadata["active_domains"]` includes the domain
  - `test_resume_conversation_loads_active_domains` — building tools for a conversation with existing `active_domains` in metadata includes those domain tools
  - `test_backward_compat_old_conversation` — conversation with tool_use history but no `active_domains` in metadata falls back to all tools
  - `test_unloaded_tool_still_executes` — calling a tool from an unloaded domain via registry still works (execution != visibility)
  - `test_permission_filter_on_domain_load` — user without `view_security` gets no security tools even when loading that domain
  - `test_core_flag_on_registration` — tools registered with `core=True` appear in core set, others don't
  - `test_discovery_tools_moved_to_domains` — `list_job_channels` is in jobs domain, `list_event_categories` is in security domain, `list_permissions` is in users domain
  - `test_duplicate_domain_load_no_duplicates` — loading the same domain twice doesn't duplicate tools in the list

### Docs

- `docs/django_developer/assistant/README.md` — Update tools section: document two-tier loading, `core=True` parameter on `@tool` decorator, `load_tools` behavior, conversation-scoped domain persistence. Update tool domain table.
- `docs/web_developer/assistant/README.md` — Note that tool availability is now progressive — the assistant loads domain tools as needed rather than having all tools on every turn.
