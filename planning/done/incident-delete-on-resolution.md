# Incident Delete-on-Resolution & Pruning

**Type**: request
**Status**: resolved
**Date**: 2026-04-03
**Priority**: medium

## Description

Add the ability to auto-delete noise incidents when they reach "resolved" or "closed" status, configured per-RuleSet. Also add an incident pruning job (90-day default) with a per-incident opt-out flag.

## Context

Currently incidents persist forever unless manually deleted. The existing `prune_events` job only deletes low-level Events older than 30 days — Incidents are never pruned. For noise categories (health checks, brute-force auth failures, etc.) where handlers fire but the incident itself has no long-term value, the database accumulates clutter. This feature gives operators two cleanup mechanisms: immediate deletion on resolution for noise, and periodic pruning for everything else.

## Acceptance Criteria

- RuleSet with `metadata.delete_on_resolution = true` causes its incidents to be hard-deleted (CASCADE) when status reaches "resolved" or "closed"
- Deletion fires from all resolution paths: REST `on_rest_saved`, BlockHandler, and LLM agent `_tool_update_incident`
- An incident with `metadata.do_not_delete = true` is never deleted (overrides both delete-on-resolution and pruning)
- New `prune_incidents` async job deletes incidents older than 90 days (configurable via `INCIDENT_PRUNE_DAYS` setting), respecting `do_not_delete`
- LLM agent's `create_rule` tool accepts a `delete_on_resolution` boolean parameter so the LLM can enable this for noise rules it proposes
- LLM agent system prompt updated to explain when to use `delete_on_resolution`

## Investigation

**What exists**:
- `prune_events` job in `asyncjobs.py:11-15` — prunes Events only, not Incidents
- `on_rest_saved` in `incident.py:112-147` — detects status changes to "resolved", records metrics
- `BlockHandler.run()` in `event_handlers.py:371-373` — sets status="resolved" directly via `save(update_fields=["status"])`
- `_tool_update_incident` in `llm_agent.py:471-489` — LLM sets status via direct save
- `_tool_create_rule` in `llm_agent.py:615-659` — LLM proposes new RuleSets with metadata
- RuleSet.metadata already stores behavioral config: `agent_prompt`, `agent_memory`, `disabled`, `llm_proposed`, `llm_reasoning`
- Incident.metadata already stores runtime data: `llm_assessment`, `analysis_in_progress`, `last_trigger_count`

**What changes**:

| File | Change |
|---|---|
| `mojo/apps/incident/models/incident.py` | Add `_check_delete_on_resolution()` helper method. Call from `on_rest_saved()` after status change to "resolved" or "closed". Checks `rule_set.metadata.delete_on_resolution` and `self.metadata.do_not_delete`. If eligible, `self.delete()` (CASCADE handles events/history). |
| `mojo/apps/incident/handlers/event_handlers.py` | After BlockHandler sets status="resolved" and saves, call `incident._check_delete_on_resolution()`. |
| `mojo/apps/incident/handlers/llm_agent.py` | In `_tool_update_incident`: after save, call `incident._check_delete_on_resolution()`. In `_tool_create_rule`: add `delete_on_resolution` param, store in metadata. Update system prompt to explain when to use it (noise patterns the LLM identifies). |
| `mojo/apps/incident/asyncjobs.py` | Add `prune_incidents(job)` — delete incidents older than `INCIDENT_PRUNE_DAYS` (default 90) where `metadata.do_not_delete` is not true. Register as cron job. |

**Constraints**:
- `_check_delete_on_resolution` must handle the case where `rule_set` is null (manually created incidents)
- CASCADE will delete Events and History — this is intentional for noise incidents
- The `do_not_delete` flag on incident metadata takes absolute precedence
- Pruning job should exclude incidents with status "open" or "investigating" (still active)
- Deletion in `on_rest_saved` happens after history is recorded — the history record will be deleted by CASCADE anyway, but the metrics recording should happen before deletion

**Related files**:
- `mojo/apps/incident/models/incident.py`
- `mojo/apps/incident/models/rule.py`
- `mojo/apps/incident/handlers/event_handlers.py`
- `mojo/apps/incident/handlers/llm_agent.py`
- `mojo/apps/incident/asyncjobs.py`
- `docs/django_developer/logging/incidents.md`
- `docs/web_developer/` (incident API docs if they exist)

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `INCIDENT_PRUNE_DAYS` | 90 | Days before resolved/closed incidents are pruned |

## Metadata Keys

| Key | Location | Purpose |
|---|---|---|
| `delete_on_resolution` | RuleSet.metadata | When true, delete the incident on resolution/close |
| `do_not_delete` | Incident.metadata | When true, exempt this incident from all auto-deletion |

## Tests Required

- RuleSet with `delete_on_resolution=true` → incident deleted when status set to "resolved" via REST
- Same for status set to "closed"
- BlockHandler resolution path triggers deletion
- LLM agent resolution path triggers deletion
- Incident with `do_not_delete=true` survives resolution even when ruleset has `delete_on_resolution`
- Incident with no rule_set survives resolution (no crash)
- `prune_incidents` job deletes old resolved/closed incidents
- `prune_incidents` job skips incidents with `do_not_delete=true`
- `prune_incidents` job skips open/investigating incidents regardless of age
- LLM `create_rule` tool can set `delete_on_resolution` in metadata

## Out of Scope

- Soft delete / archive mechanism — this is hard delete with CASCADE
- UI changes for managing the `do_not_delete` flag (can be set via REST PATCH on metadata)
- Changing the existing `prune_events` job behavior
- Per-incident `delete_on_resolution` (it lives on the RuleSet, not the incident)

## Plan

**Status**: planned
**Planned**: 2026-04-03

### Objective

Add delete-on-resolution to incidents (via RuleSet metadata) and a periodic incident pruning job, with LLM agent awareness of both features.

### Steps

1. **`mojo/apps/incident/models/incident.py`** — Add `check_delete_on_resolution()` method
   - Checks `self.rule_set` exists and `rule_set.metadata.get("delete_on_resolution")` is truthy
   - Checks `self.metadata.get("do_not_delete")` is NOT truthy (absolute override)
   - If eligible, calls `self.delete()` and returns `True`; otherwise returns `False`
   - Call from `on_rest_saved()` after status changes to "resolved" or "closed" (after metrics recording)

2. **`mojo/apps/incident/handlers/event_handlers.py`** — BlockHandler resolution path (lines 371-376)
   - After `incident.save(update_fields=["status"])` and `add_history()`, call `incident.check_delete_on_resolution()`

3. **`mojo/apps/incident/handlers/llm_agent.py`** — Five changes:
   - **`_tool_update_incident`** (line 471-489): After both saves (status + metadata), call `incident.check_delete_on_resolution()`. Return `{"ok": True, "deleted": True}` if deleted so the LLM knows.
   - **`_tool_update_incident`**: Accept optional `do_not_delete` boolean param. When true, set `incident.metadata["do_not_delete"] = True` before saving metadata. This lets the LLM protect serious incidents from deletion and pruning.
   - **`update_incident` tool schema** (line 149+): Add `do_not_delete` boolean property: "Set true for serious incidents that should be preserved permanently — overrides delete_on_resolution and pruning."
   - **`_tool_create_rule`** (line 615-659): When `params.get("delete_on_resolution")` is truthy, add it to the metadata dict. Include it in the ticket proposal text.
   - **`create_rule` tool schema** (line 236-272): Add `delete_on_resolution` boolean property: "Set true for noise patterns (bot scanning, health blips) where the incident should be auto-deleted on resolution."

4. **`mojo/apps/incident/handlers/llm_agent.py`** — Prompt updates:
   - **`SYSTEM_PROMPT`** (line 31-63): Add section on deletion lifecycle:
     - `delete_on_resolution` on RuleSets — when to set it on proposed rules (noise patterns)
     - `do_not_delete` on Incidents — set it when you encounter real threats worth preserving
   - **`ANALYSIS_PROMPT`** (line 65-96): Mention `delete_on_resolution` in rule creation guidance — use it for noise patterns the LLM identifies during analysis.

5. **`mojo/apps/incident/asyncjobs.py`** — Add `prune_incidents(job)` function
   - Read `INCIDENT_PRUNE_DAYS` setting (default 90)
   - Delete incidents where: `created < now - INCIDENT_PRUNE_DAYS`, `status` in ("resolved", "closed", "ignored"), and NOT `metadata__do_not_delete=True`
   - Log count via `job.add_log()`
   - Register as cron job (same pattern as `prune_events`)

### Design Decisions

- **Explicit method calls, not signals**: `check_delete_on_resolution()` is called at each resolution site (3 places) rather than via a Django signal or `save()` override. Keeps deletion visible and predictable.
- **Public method name**: Handlers and the LLM agent call it from outside the model, so not prefixed with `_`.
- **Metrics before delete**: In `on_rest_saved()`, metrics recording for "resolved" already runs before the delete check — no ordering issue.
- **`do_not_delete` on Incident, not RuleSet**: This is a per-incident flag set by humans or the LLM for specific serious incidents. The RuleSet controls the category-level behavior (`delete_on_resolution`); the incident flag is the per-instance override.
- **Pruning excludes active statuses**: Only prunes "resolved", "closed", "ignored" — never "new", "open", "pending", "investigating".
- **JSONField queries**: Django JSONField supports `metadata__do_not_delete=True` lookups natively.

### Edge Cases

- **Null `rule_set`**: Manually created incidents have no rule_set. `check_delete_on_resolution()` returns `False` — no crash, no delete.
- **Race in LLM agent**: `_tool_update_incident` does two saves (status, then metadata). Delete check runs after both to avoid saving to a deleted object.
- **BlockHandler skip**: BlockHandler only sets status when `not in ("resolved", "ignored")`. If status doesn't change, no delete check fires.
- **Cascade cleanup**: Django CASCADE on Event→Incident and IncidentHistory→Incident deletes all child records. Intentional for noise.
- **LLM sets `do_not_delete` on same call as resolve**: If the LLM resolves an incident AND sets `do_not_delete=True` in the same `update_incident` call, the `do_not_delete` flag is written first, so `check_delete_on_resolution()` correctly skips deletion.

### Testing

- REST resolution with `delete_on_resolution` triggers delete → `tests/test_incident/test_delete_on_resolution.py`
- "closed" status also triggers delete → same file
- BlockHandler resolution triggers delete → same file
- LLM agent resolution triggers delete → same file
- `do_not_delete` on incident overrides delete → same file
- Null `rule_set` doesn't crash → same file
- LLM `update_incident` can set `do_not_delete` → same file
- `prune_incidents` deletes old resolved/closed/ignored → `tests/test_incident/test_prune_incidents.py`
- Pruning skips `do_not_delete=True` incidents → same file
- Pruning skips active statuses regardless of age → same file
- LLM `create_rule` can set `delete_on_resolution` in metadata → same file

### Docs

- `docs/django_developer/logging/incidents.md` — Add section on incident deletion lifecycle (delete-on-resolution, `do_not_delete`, pruning job, `INCIDENT_PRUNE_DAYS` setting)
- `CHANGELOG.md` — Document new feature

## Resolution

**Status**: resolved
**Date**: 2026-04-03

### What Was Built
Incident delete-on-resolution via RuleSet `metadata.delete_on_resolution`, per-incident `metadata.do_not_delete` override, `prune_incidents` async job (90-day default), and LLM agent awareness of both flags.

### Files Changed
- `mojo/apps/incident/models/incident.py` — Added `check_delete_on_resolution()` method, hooked into `on_rest_saved()`
- `mojo/apps/incident/handlers/event_handlers.py` — BlockHandler calls `check_delete_on_resolution()` after resolving
- `mojo/apps/incident/handlers/llm_agent.py` — Updated prompts, tool schemas, `_tool_update_incident` (do_not_delete + delete check), `_tool_create_rule` (delete_on_resolution)
- `mojo/apps/incident/asyncjobs.py` — Added `prune_incidents()` job with `INCIDENT_PRUNE_DAYS` setting

### Tests
- `tests/test_incident/test_delete_on_resolution.py` — 12 tests covering all resolution paths, do_not_delete override, null ruleset, cascade
- `tests/test_incident/test_prune_incidents.py` — 4 tests covering prune job, do_not_delete skip, active status skip, recent skip
- Run: `bin/run_tests -t test_incident.test_delete_on_resolution -t test_incident.test_prune_incidents`

### Docs Updated
- `docs/django_developer/logging/incidents.md` — Deletion lifecycle section, INCIDENT_PRUNE_DAYS setting
- `docs/web_developer/logging/incidents.md` — do_not_delete flag, 404-after-resolution behavior
- `CHANGELOG.md` — Feature entry

### Security Review
No auth bypass or injection risks. Two advisory items: broad `security` SAVE_PERM allows clearing `do_not_delete` (existing permission model), and `prune_incidents` has no batch limit (same pattern as existing `prune_events`). Both acceptable for current use.

### Follow-up
- Consider minimum retention window (e.g. 24h) before delete-on-resolution fires, to give humans time to set `do_not_delete`
- Consider batch-limiting `prune_incidents` for large backlogs
- Register `prune_incidents` as a cron job in the deployment config
