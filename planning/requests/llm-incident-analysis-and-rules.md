# LLM Incident Analysis & Rule Generation Endpoint

**Type**: request
**Status**: planned
**Date**: 2026-04-01
**Priority**: high

## Description
Add a REST endpoint (POST_SAVE_ACTION on Incident) that lets an admin click on an incident in the Admin Portal and trigger the LLM agent to:
1. Analyze the incident and its events
2. Find and analyze related incidents (same category, IP, pattern)
3. Merge related incidents that represent the same underlying issue
4. Create or propose rulesets that would auto-resolve these incidents going forward

The goal: every recurring incident pattern gets a rule so future occurrences are handled automatically — no new or open incidents pile up.

## Context
The LLM agent already exists (`llm_agent.py`) and can triage individual incidents reactively (when an event triggers a rule with an `llm://` handler). But there's no way for an admin to proactively invoke the LLM on an existing incident to analyze patterns and generate rules. The building blocks are all there — this feature wires them together with a new entry point and a specialized prompt.

## Acceptance Criteria
- Admin can trigger LLM analysis on any incident via REST (POST_SAVE_ACTION `analyze`)
- LLM investigates the incident's events, finds related incidents, and identifies patterns
- LLM can merge related incidents into one (new `merge_incidents` tool)
- LLM proposes or creates rulesets to auto-handle the pattern (using existing `create_rule` tool)
- LLM resolves/closes incidents that the new rule would cover
- Analysis result is stored in `incident.metadata["llm_analysis"]` and recorded in history
- Works async via the job queue (non-blocking for the admin)
- Requires `manage_security` permission

## Investigation

**What exists**:
- `mojo/apps/incident/handlers/llm_agent.py` — full LLM agent with 12 tools, agent loop, Claude API integration
- `Incident.on_action_merge(value)` — merges events from other incidents (REST action already exists)
- `_tool_create_rule()` — creates disabled rulesets with human-approval tickets
- `_tool_query_related_incidents()` — finds incidents by IP/category
- `_tool_query_incident_events()` — gets all events for an incident
- `_tool_update_incident()` — changes incident status
- `execute_llm_handler(job)` — existing job entry point (event-triggered only)
- Handler chain supports `llm://` already

**What changes**:

| File | Change |
|---|---|
| `mojo/apps/incident/models/incident.py` | Add `analyze` to `POST_SAVE_ACTIONS`, implement `on_action_analyze` to publish the async job |
| `mojo/apps/incident/handlers/llm_agent.py` | Add `merge_incidents` tool, add `execute_llm_analysis` job entry point, add `ANALYSIS_PROMPT` (specialized prompt for pattern analysis + rule creation), add `_build_analysis_message` (richer context builder that includes related incidents inline) |

**Constraints**:
- Requires `LLM_HANDLER_API_KEY` to be configured — fail gracefully with clear error if not set
- Must run async (job queue) — LLM calls take 30-60s
- Rules created by this flow should still be disabled/proposed (human approves via ticket) unless we add explicit auto-enable
- Merge should only combine incidents the LLM is confident are the same pattern — don't blindly merge everything
- `manage_security` permission required (same as existing SAVE_PERMS)

**Related files**:
- `mojo/apps/incident/models/incident.py`
- `mojo/apps/incident/handlers/llm_agent.py`
- `mojo/apps/incident/handlers/event_handlers.py`
- `mojo/apps/incident/models/rule.py`
- `mojo/apps/incident/models/event.py`
- `mojo/apps/incident/rest/event.py`

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `/api/incident/incident/<int:pk>` (action=`analyze`) | Trigger LLM analysis on an incident | `manage_security` |

This uses the existing `POST_SAVE_ACTIONS` pattern — the frontend POSTs `{"action": "analyze"}` to the incident endpoint.

## New LLM Tools

| Tool | Description |
|---|---|
| `merge_incidents` | Merge a list of related incident IDs into the target incident (delegates to `Incident.on_action_merge`) |

## Specialized Prompt (ANALYSIS_PROMPT)

The analysis prompt should instruct the LLM to:
1. Set the incident to "investigating"
2. Query all events in the incident to understand the pattern
3. Query related incidents by category and source_ip
4. For each related incident, query its events to confirm it's the same pattern
5. Merge incidents that are clearly the same issue
6. Identify what rule (category match, field comparators, bundling, handler chain) would auto-handle this pattern
7. Create the rule (disabled, for human approval) via `create_rule`
8. Resolve/close the merged incidents with a note explaining the new rule
9. Summarize what was done: incidents merged, rule proposed, pattern description

## Tests Required
- Test `on_action_analyze` publishes the correct job with correct payload
- Test `execute_llm_analysis` loads incident, builds analysis prompt, calls agent loop
- Test `merge_incidents` tool correctly delegates to `Incident.on_action_merge`
- Test that `analyze` action requires `manage_security` permission
- Test graceful failure when `LLM_HANDLER_API_KEY` is not configured

## Out of Scope
- Auto-enabling rules without human approval (keep the ticket-based approval flow)
- Admin Portal UI changes (frontend will call the action endpoint)
- Batch analysis of multiple incidents at once (start with single-incident trigger)
- Modifying existing rules (only creating new ones)

## Plan

**Status**: planned
**Planned**: 2026-04-01

### Objective
Add an `analyze` POST_SAVE_ACTION on Incident that triggers the LLM agent to investigate the incident, merge related incidents, and propose rulesets to auto-handle the pattern.

### Steps
1. `mojo/apps/incident/models/incident.py` — Add `"analyze"` to `POST_SAVE_ACTIONS`. Implement `on_action_analyze(self, value)`: validate `LLM_HANDLER_API_KEY` is configured (return error if not), check `metadata["analysis_in_progress"]` to prevent double-runs, publish `execute_llm_analysis` job with `incident_id`, record "analysis requested" history entry.

2. `mojo/apps/incident/handlers/llm_agent.py` — Add the following:
   - **`ANALYSIS_PROMPT`**: Specialized system prompt instructing the LLM to focus on pattern identification, merging, and rule creation. Steps: investigate events → find related open incidents → merge duplicates (same category only) → check existing rulesets before proposing new ones → create rule (disabled, for human approval) → resolve affected incidents → summarize.
   - **`_tool_merge_incidents(params)`**: Takes `target_incident_id` + list of `incident_ids`. Loads target Incident, delegates to `Incident.on_action_merge(incident_ids)`. Returns merge count.
   - **`_tool_query_open_incidents(params)`**: Queries `Incident.objects.filter(status__in=["new", "open", "investigating"])` with optional category filter. Returns list with event counts. Distinct from `query_related_incidents` (which doesn't filter by status).
   - **`_build_analysis_message(incident)`**: Richer context builder that pre-loads the incident's events (up to 50) and up to 20 related open incidents inline, saving the LLM initial tool calls.
   - **`execute_llm_analysis(job)`**: Job entry point. Loads incident (no event needed). Builds `ANALYSIS_PROMPT` + `_build_analysis_message`. Runs agent loop with extended tool set (all 12 existing tools + `merge_incidents` + `query_open_incidents`). Stores result in `incident.metadata["llm_analysis"]`. Clears `analysis_in_progress` flag. Records summary in history.
   - Add `merge_incidents` and `query_open_incidents` to `TOOLS` list and `TOOL_DISPATCH`.

3. `tests/test_incident/test_analyze_action.py` — Test `on_action_analyze` publishes correct job payload. Test rejection when `LLM_HANDLER_API_KEY` not set. Test `_tool_merge_incidents` delegates to `Incident.on_action_merge`. Test `_tool_query_open_incidents` filters by status correctly.

4. `docs/django_developer/security/README.md` — Document the `analyze` action, `execute_llm_analysis` job entry point, and new LLM tools.

5. `docs/web_developer/security/README.md` — Document the `analyze` POST_SAVE_ACTION for frontend consumers (POST `{"action": "analyze"}` to incident endpoint).

### Design Decisions
- **POST_SAVE_ACTION, not a custom endpoint**: Consistent with existing `merge` action and REST conventions. No new URL needed.
- **Separate prompt from triage prompt**: Analysis (pattern identification + rule creation) is fundamentally different from reactive triage. Separate prompts keep both clean.
- **Pre-load context in the user message**: Include incident events and related open incidents inline. Saves 2-3 tool calls per run and gives better initial context.
- **Reuse `Incident.on_action_merge`**: Merge logic already handles event reassignment and cleanup. The LLM tool just delegates.
- **`query_open_incidents` as a new tool**: `query_related_incidents` doesn't filter by status. The LLM needs to see specifically what's open and piling up.
- **`analysis_in_progress` guard**: Prevents double-runs if admin clicks the button twice before the first run completes.

### Edge Cases
- **No LLM API key**: `on_action_analyze` returns error dict immediately, no job published.
- **Double-click**: `metadata["analysis_in_progress"]` flag checked on action and on job entry, cleared on exit (including errors).
- **Duplicate rule proposal**: Prompt instructs LLM to check existing rulesets for the category before creating a new one. Rules are always created disabled for human approval.
- **Cross-category merge**: Prompt instructs LLM to only merge incidents with the same category.
- **LLM agent error**: Catch-all in `execute_llm_analysis` clears `analysis_in_progress` flag and records failure in history.

### Testing
- `on_action_analyze` publishes job with correct payload → `tests/test_incident/test_analyze_action.py`
- `on_action_analyze` rejects when no API key → `tests/test_incident/test_analyze_action.py`
- `on_action_analyze` rejects when analysis already in progress → `tests/test_incident/test_analyze_action.py`
- `_tool_merge_incidents` delegates to `Incident.on_action_merge` → `tests/test_incident/test_analyze_action.py`
- `_tool_query_open_incidents` filters by status → `tests/test_incident/test_analyze_action.py`

### Docs
- `docs/django_developer/security/README.md` — Document `analyze` action, `execute_llm_analysis` job, new LLM tools
- `docs/web_developer/security/README.md` — Document `analyze` POST_SAVE_ACTION for frontend
