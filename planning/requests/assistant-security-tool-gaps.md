# Assistant Security Tool Gaps

**Type**: request
**Status**: planned
**Date**: 2026-04-05
**Priority**: high

## Description

Address tool gaps identified by the live LLM assistant agent during real incident triage. The agent can query incidents/events and block IPs, but is missing critical operational tools for rule management, IP management, bulk operations, and user security actions.

## Context

During live OSSEC incident triage, the agent identified specific gaps that block its workflow. These are organized by priority based on how often the agent hits them.

## New Tools Needed

### Rule Management (highest priority — agent hit this during live triage)

**1. `get_ruleset`** — Get full details of a single RuleSet including child Rules
- Accepts: `ruleset_id`
- Returns: RuleSet fields + `rules` array with each Rule's field_name, comparator, value, value_type
- Permission: `view_security`
- Implementation: `RuleSet.objects.get(pk=id)` + `ruleset.rules.order_by("index")`

**2. `add_rule_condition`** — Add a Rule to an existing RuleSet
- Accepts: `ruleset_id`, `field`, `comparator`, `value`, `value_type`, `name` (optional)
- Creates: `Rule.objects.create(parent=ruleset, ...)`
- Permission: `manage_security`, mutates=True
- Why: Agent can create RuleSets but can't attach conditions after the fact

**3. `update_ruleset`** — Edit existing RuleSet fields
- Accepts: `ruleset_id` + optional `handler`, `bundle_by`, `bundle_minutes`, `trigger_count`, `trigger_window`, `is_active`, `name`, `priority`
- Only updates fields that are provided
- Permission: `manage_security`, mutates=True
- Critical for: enabling assistant-proposed rulesets after human review

**4. `delete_ruleset`** — Remove a RuleSet and its child Rules
- Accepts: `ruleset_id`
- CASCADE deletes child Rules automatically
- Permission: `manage_security`, mutates=True

### IP / Blocking Management

**5. `unblock_ip`** — Unblock a blocked IP
- Accepts: `ip`, `reason`
- Calls: `geo.unblock(reason=reason)`
- Permission: `manage_security`, mutates=True
- Already exists on model: `GeoLocatedIP.unblock(reason, broadcast=True)`

**6. `whitelist_ip`** — Add IP to whitelist (prevents future auto-blocks)
- Accepts: `ip`, `reason`
- Calls: `geo.whitelist(reason=reason)` — also unblocks if currently blocked
- Permission: `manage_security`, mutates=True

**7. `unwhitelist_ip`** — Remove IP from whitelist
- Accepts: `ip`
- Calls: `geo.unwhitelist()`
- Permission: `manage_security`, mutates=True

**8. `query_blocked_ips`** — List currently blocked IPs
- Accepts: `limit`, `minutes` (optional, how recently blocked)
- Queries: `GeoLocatedIP.objects.filter(is_blocked=True)`
- Returns: ip, blocked_at, blocked_until, blocked_reason, block_count, is_whitelisted
- Permission: `view_security`

**9. `query_ipsets`** — List bulk IPSet entries (country blocks, abuse lists)
- Accepts: `kind` filter (country/datacenter/abuse/custom), `is_enabled` filter
- Queries: `IPSet.objects.filter(...)`
- Returns: name, kind, is_enabled, cidr_count, source, last_synced
- Permission: `view_security`

### Incident Bulk Operations

**10. `bulk_update_incidents`** — Resolve/ignore multiple incidents at once
- Accepts: `incident_ids` (list), `status`, `note`
- Updates all matching incidents, adds history to each
- Cap at 100 per call
- Permission: `manage_security`, mutates=True
- Why: Agent had to call update_incident 50+ times to clean up OSSEC backlog

**11. `merge_incidents`** — Merge incidents (already supported via POST_SAVE_ACTIONS)
- Accepts: `target_id`, `source_ids` (list)
- Moves all events from source incidents to target, deletes sources
- Permission: `manage_security`, mutates=True

### Event & Diagnostic

**12. `get_event`** — Get full details of a single event by ID
- Accepts: `event_id`
- Returns: all fields including full metadata (no truncation)
- Permission: `view_security`

### User Security Actions

**13. `disable_user`** — Deactivate a user account + invalidate sessions
- Accepts: `user_id`, `reason`
- Sets `is_active=False`, rotates `auth_key` (invalidates all JWTs)
- Logs incident as security action
- Permission: `manage_users`, mutates=True

**14. `enable_user`** — Reactivate a disabled user account
- Accepts: `user_id`, `reason`
- Sets `is_active=True`
- Permission: `manage_users`, mutates=True

**15. `force_logout`** — Invalidate all active sessions for a user
- Accepts: `user_id`, `reason`
- Rotates `auth_key` (all JWTs fail signature validation immediately)
- Does NOT disable the account — user can log back in
- Permission: `manage_users`, mutates=True

## Existing Tool Improvements

### `query_rulesets` — fix `is_disabled` field
Currently returns `is_disabled` from metadata (legacy). Should return `is_active` from the actual model field, plus `trigger_count`, `trigger_window`, `priority`.

### `query_incidents` — add filters
- `hostname` filter
- `rule_set_id` filter
- `model_name` filter

### `query_events` — add filters
- `rule_id` metadata filter (for OSSEC rule_id drilldown): filter by `metadata__rule_id`
- `incident_id` filter (already have `get_incident_events` but a filter on query_events is more flexible)

### `query_event_counts` — add group_by option
- Allow grouping by `metadata__rule_id` in addition to `category`
- This lets the agent measure noise volume per OSSEC rule ID

## Investigation

**What exists on models (methods the tools just need to call)**:
- `GeoLocatedIP.unblock(reason, broadcast=True)` — fleet-wide unblock
- `GeoLocatedIP.whitelist(reason)` — whitelist + auto-unblock
- `GeoLocatedIP.unwhitelist()` — remove whitelist
- `User.is_active` field — disable/enable accounts
- `User.auth_key` rotation — invalidates all JWTs (force logout)
- `Incident.on_action_merge(source_ids)` — merge incidents via POST_SAVE_ACTIONS
- `IPSet` model — bulk IP blocking by country/datacenter/abuse list

**What changes**:
- `mojo/apps/assistant/services/tools/security.py` — add new handlers + tool definitions, fix existing tools
- `mojo/apps/assistant/services/tools/users.py` — add disable_user, enable_user, force_logout

## Tests Required

- unblock_ip: block then unblock, verify is_blocked=False
- whitelist_ip: whitelist, attempt block, verify block rejected
- bulk_update_incidents: create 5 incidents, bulk resolve, verify all updated
- disable_user: disable, verify is_active=False and auth_key rotated
- force_logout: rotate auth_key, verify old JWT fails validation
- get_ruleset: verify child rules included in response
- add_rule_condition: add rule to existing ruleset, verify parent FK
- All mutation tools: verify mutates=True and permission gates

## Out of Scope

- IPSet creation/management (create_ipset) — complex, needs separate request
- Job triggering (create_job/trigger_cron) — needs separate request with safety analysis
- Full RuleSet testing/simulation

## Plan

**Status**: planned
**Planned**: 2026-04-05

### Objective

Add 15 new assistant tools and improve 4 existing tools to close all operational gaps the live agent identified during incident triage.

### Steps

1. `mojo/apps/assistant/services/tools/security.py` — **Fix existing tool responses**
   - `_tool_query_rulesets` (line 128): replace `is_disabled` metadata check with `is_active` model field, add `trigger_count`, `trigger_window`, `priority`, `match_by` to response
   - `_tool_query_incidents` (line 9): add `hostname`, `rule_set_id`, `model_name` filter params + schema entries
   - `_tool_query_events` (line 43): add `rule_id` (metadata filter via `metadata__rule_id`) and `incident_id` filter params + schema entries
   - `_tool_query_event_counts` (line 78): add `group_by` param (default `"category"`, alt `"rule_id"`); when `"rule_id"`, use `.values("metadata__rule_id").annotate(count=Count("id"))`

2. `mojo/apps/assistant/services/tools/security.py` — **Add new rule management handlers**
   - `_tool_get_ruleset`: `RuleSet.objects.get(pk=id)` + `ruleset.rules.order_by("index")`, returns all RuleSet fields + child rules array
   - `_tool_add_rule_condition`: `Rule.objects.create(parent=ruleset, ...)` with auto-calculated index from `ruleset.rules.count()`
   - `_tool_update_ruleset`: selective field update — only save fields present in params via `save(update_fields=[...])`
   - `_tool_delete_ruleset`: `RuleSet.objects.get(pk=id).delete()` — CASCADE handles child Rules

3. `mojo/apps/assistant/services/tools/security.py` — **Add new IP management handlers**
   - `_tool_unblock_ip`: `GeoLocatedIP.objects.get(ip_address=ip)` → `geo.unblock(reason=f"[Admin Assistant] {reason}")`
   - `_tool_whitelist_ip`: `geo.whitelist(reason=f"[Admin Assistant] {reason}")` — auto-unblocks if currently blocked
   - `_tool_unwhitelist_ip`: `geo.unwhitelist()`
   - `_tool_query_blocked_ips`: `GeoLocatedIP.objects.filter(is_blocked=True)` with optional `blocked_at__gte` time filter, returns ip/blocked_at/blocked_until/blocked_reason/block_count/is_whitelisted
   - `_tool_query_ipsets`: `IPSet.objects.filter(...)` with `kind` and `is_enabled` filters, returns name/kind/is_enabled/cidr_count/source/last_synced (no CIDR data — too large)

4. `mojo/apps/assistant/services/tools/security.py` — **Add incident bulk operations**
   - `_tool_bulk_update_incidents`: accepts `incident_ids` (list, max 100), `status`, `note`; loops through, updates each, adds history; returns `{"updated": [...ids], "failed": [...ids], "count": N}`
   - `_tool_merge_incidents`: accepts `target_id`, `source_ids`; calls `target.on_action_merge(source_ids)` directly (reuses existing POST_SAVE_ACTIONS logic)
   - `_tool_get_event`: `Event.objects.get(pk=id)`, returns all fields including full `metadata` dict (no truncation)

5. `mojo/apps/assistant/services/tools/security.py` — **Add TOOLS list entries for all 12 new security tools**
   - Each with `input_schema`, `permission`, `mutates` flag
   - Read-only tools: `permission="view_security"`
   - Mutation tools: `permission="manage_security"`, `mutates=True`

6. `mojo/apps/assistant/services/tools/users.py` — **Add user security action handlers**
   - `_tool_disable_user`: guard `user_id != calling_user.pk` (cannot disable yourself), set `is_active=False`, rotate `auth_key = uuid.uuid4().hex` (immediate JWT invalidation), `save(update_fields=["is_active", "auth_key", "modified"])`, log via `User.class_logit()`
   - `_tool_enable_user`: set `is_active=True`, `save(update_fields=["is_active", "modified"])`
   - `_tool_force_logout`: rotate `auth_key = uuid.uuid4().hex` only (account stays active, user can log back in), `save(update_fields=["auth_key", "modified"])`

7. `mojo/apps/assistant/services/tools/users.py` — **Add TOOLS list entries for 3 new user tools**
   - `disable_user`: `permission="manage_users"`, `mutates=True`
   - `enable_user`: `permission="manage_users"`, `mutates=True`
   - `force_logout`: `permission="manage_users"`, `mutates=True`

8. `tests/test_assistant/4_test_security_tools.py` — **New test file**
   - Setup: create test users, RuleSets, Events, Incidents, GeoLocatedIP records
   - Test `get_ruleset` returns child rules in response
   - Test `add_rule_condition` creates Rule with correct `parent` FK and auto-index
   - Test `update_ruleset` selective field update (only provided fields change)
   - Test `delete_ruleset` cascades to child Rules
   - Test `bulk_update_incidents` updates all, caps at 100, reports failures
   - Test `merge_incidents` moves events and deletes sources
   - Test `get_event` returns full metadata without truncation
   - Test `unblock_ip` / `whitelist_ip` / `unwhitelist_ip` round-trip
   - Test `query_blocked_ips` returns correct set
   - Test `query_event_counts` with `group_by="rule_id"`
   - Test `disable_user` sets `is_active=False` and rotates `auth_key`
   - Test `disable_user` rejects self-disable with error
   - Test `force_logout` rotates `auth_key` without disabling account
   - Test `enable_user` reactivates disabled account
   - All handlers called directly with `(params, user)` — no LLM needed

9. `docs/django_developer/assistant/README.md` — **Update built-in tools documentation**
   - Add new tools to the built-in tools table under security and users domains
   - Document user security actions and permission requirements

### Design Decisions

- **All new tools in existing files**: rule/IP/incident tools in `security.py`, user security actions in `users.py`. No new domain modules — keeps `__init__.py` registration simple.
- **`merge_incidents` calls `on_action_merge` directly**: reuses existing POST_SAVE_ACTIONS logic (event migration, history entries, cleanup) rather than reimplementing.
- **`bulk_update_incidents` caps at 100**: prevents runaway bulk operations. Agent can call multiple times if needed.
- **`disable_user` rotates `auth_key`**: `is_active=False` alone isn't enough — existing JWTs remain valid until expiry. Rotating `auth_key` provides immediate cryptographic session invalidation.
- **`force_logout` vs `disable_user`**: separate tools because intent differs. Force logout is temporary (user can log back in), disable is persistent. Both rotate `auth_key`.
- **`query_event_counts` group_by `rule_id`**: uses `metadata__rule_id` via Django JSONField lookups. Events without the field group under `null`.
- **`query_ipsets` excludes CIDR data**: CIDR lists can be enormous. Returns metadata only (name, kind, count, source). Full data would blow LLM context.
- **Cannot disable yourself**: `disable_user` hard-blocks `user_id == calling_user.pk` and returns an error. Non-negotiable safety guard.

### Edge Cases

- **unblock_ip on non-blocked IP**: `unblock()` is idempotent — safe to call, returns success
- **whitelist_ip on blocked IP**: `whitelist()` auto-calls `unblock()` — correct behavior
- **bulk_update_incidents with invalid IDs**: skip missing IDs, report which succeeded and which failed in response
- **merge_incidents where source has no events**: `on_action_merge` handles empty incidents (deletes them)
- **disable_user on self**: returns `{"error": "Cannot disable your own account"}` — hard block
- **query_event_counts group_by `rule_id` on non-OSSEC events**: events without `metadata.rule_id` group under `null` — acceptable

### Testing

- Rule management tools → `tests/test_assistant/4_test_security_tools.py`
- IP management tools → `tests/test_assistant/4_test_security_tools.py`
- Bulk incident operations → `tests/test_assistant/4_test_security_tools.py`
- User security actions → `tests/test_assistant/4_test_security_tools.py`
- Existing tool improvements → `tests/test_assistant/4_test_security_tools.py`

### Docs

- `docs/django_developer/assistant/README.md` — add all new tools to built-in tools table, document user security action tools and permissions
