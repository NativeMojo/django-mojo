# Assistant Report Event Tool

**Type**: request
**Status**: open
**Date**: 2026-04-05
**Priority**: high

## Description

Add a `report_event` tool to the assistant that lets the LLM create security/operational events via the existing `Event.publish()` pipeline. The LLM can gather context conversationally — asking the user about severity, category, affected systems, IPs — then submit a well-structured event that flows through RuleSet matching, incident bundling, and handler execution automatically.

Also add a `list_rulesets` discovery tool so the LLM knows what categories and handlers are configured.

## Context

The incident system is event-driven: Events are created → matched against RuleSets → bundled into Incidents → handlers execute (block IP, send email, notify, create ticket, etc.). The assistant can already *query* incidents and events but can't create them. This tool lets a user say "we're seeing suspicious traffic from 203.0.113.50" and have the assistant create a properly structured event that triggers the right automated response.

Key design: the tool creates **Events**, not Incidents directly. `Event.publish()` handles RuleSet matching, incident creation/bundling, and handler execution. This preserves the existing automation pipeline.

Events have `CREATE_PERMS = ["all"]` (any authenticated user), but the tool should require `security` or `manage_security` permission since the LLM is acting with elevated context.

## Acceptance Criteria

- `report_event` tool accepts: `category` (required), `level` (0-15, required), `title`, `details`, `source_ip`, `hostname`, `scope`, `model_name`, `model_id`, `metadata` (dict)
- Creates an Event via `Event.objects.create(...)` then calls `event.publish()`
- Returns: `{"event_id": ..., "incident_id": ... or null, "incident_status": ..., "ruleset_matched": ... or null}`
- `mutates=True` — LLM confirms before creating
- Permission-gated: requires `security` or `manage_security`
- LLM is encouraged (via tool description) to ask the user clarifying questions before submitting: severity, category, affected systems
- `list_rulesets` tool: returns active RuleSets with category, handler chain, bundle config — so the LLM knows what categories exist and what will happen when an event is submitted

## Investigation

**What exists**:
- `Event.objects.create(...)` + `event.publish()` — the standard creation flow
- `Event.publish()` → RuleSet matching → `get_or_create_incident()` → handler execution
- Event fields: level, category, scope, source_ip, hostname, uid, country_code, title, details, model_name, model_id, metadata
- RuleSet matching: by scope → category → catch-all (`category="*"`)
- Handler chain: block://, email://, sms://, notify://, job://, ticket://, llm://
- Bundling: by hostname, model_name, model_id, source_ip, or combinations
- `Event.RestMeta.CREATE_PERMS = ["all"]` — any authenticated user via REST
- Existing assistant tools already query incidents/events (`services/tools/security.py`)

**What changes**:
- `mojo/apps/assistant/services/tools/incidents.py` — **new file**: `report_event` + `list_rulesets` handlers + TOOLS list
- `mojo/apps/assistant/services/tools/__init__.py` — import and register

**Constraints**:
- Event levels drive incident creation: level >= 7 (configurable via `INCIDENT_LEVEL_THRESHOLD`) creates an incident. The LLM should understand this so it sets appropriate levels.
- `event.publish()` may trigger handlers that block IPs, send emails, etc. The `mutates=True` confirmation is critical here.
- The tool description should include a severity guide so the LLM picks appropriate levels:
  - 0-3: Informational / low
  - 4-6: Warning / potential issue
  - 7-9: Significant — creates incident
  - 10-12: High severity
  - 13-15: Critical
- Category must match an existing RuleSet for automated handling. If no RuleSet matches, the event is still created but no handler fires. The `list_rulesets` tool helps the LLM pick the right category.

**Related files**:
- `mojo/apps/incident/models/event.py` — Event model + publish()
- `mojo/apps/incident/models/incident.py` — Incident model + get_or_create_incident()
- `mojo/apps/incident/models/rule.py` — RuleSet model + matching + handlers
- `mojo/apps/incident/handlers/event_handlers.py` — handler execution
- `mojo/apps/assistant/services/tools/security.py` — existing incident query tools (pattern reference)

## Example Interactions

**User: "We're seeing suspicious login attempts from 203.0.113.50"**
→ LLM asks: "What severity would you rate this? And is this brute force attempts or credential stuffing?"
→ User: "Looks like brute force, medium-high severity"
→ LLM calls `report_event(category="login:brute_force", level=8, title="Brute force login attempts", details="Multiple failed login attempts observed", source_ip="203.0.113.50")`
→ `{"event_id": 1234, "incident_id": 567, "incident_status": "new", "ruleset_matched": "SSH Brute Force"}`
→ LLM: "Created event #1234, which matched the 'SSH Brute Force' ruleset and created incident #567. The handler will auto-block that IP for 1 hour."

**User: "Log that we did planned maintenance on the auth service"**
→ LLM calls `report_event(category="maintenance", level=2, title="Planned auth service maintenance", details="Routine maintenance window completed", hostname="auth-01")`
→ `{"event_id": 1235, "incident_id": null, "ruleset_matched": null}`
→ LLM: "Logged event #1235. Level 2 is informational so no incident was created."

**LLM exploring available categories:**
→ `list_rulesets()`
→ `[{"name": "Bot/Scanner Patterns", "category": "security:bot", "handler": "block://?ttl=600", ...}, {"name": "SSH Brute Force", "category": "login:ssh", "handler": "block://?ttl=3600", ...}, ...]`

## Tests Required

- Create event with valid fields and verify Event record created
- Verify event.publish() is called and incident created when level >= threshold
- Verify incident not created when level < threshold
- Verify RuleSet matching works (event category matches ruleset)
- Verify return includes event_id, incident_id, ruleset name
- Verify mutates=True on tool definition
- Verify permission gate (user without security perm denied)
- Verify list_rulesets returns active rulesets with handler info
- Verify invalid level (>15 or <0) returns clean error
- Verify category is required

## Out of Scope

- Editing or updating existing incidents (already handled by `update_incident` tool)
- Creating or modifying RuleSets
- Direct incident creation (bypassing the event pipeline)
- Bulk event creation
