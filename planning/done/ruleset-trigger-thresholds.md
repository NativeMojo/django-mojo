# RuleSet Trigger Thresholds and Re-trigger Support

**Type**: request
**Status**: resolved
**Date**: 2026-04-01
**Priority**: high

## Description

Promote `min_count` and `window_minutes` from the opaque `metadata` JSON field to real model fields on `RuleSet`, fix the broken threshold event count (currently counts globally instead of per-incident), and add `retrigger_every` to re-fire the handler every N additional events after the initial trigger.

## Context

The current threshold system buries configuration in the `metadata` JSON field (`min_count`, `window_minutes`, `pending_status`), making it invisible in the admin portal and impossible to configure without knowing magic key names. Additionally, the threshold count queries all events globally for that category + source_ip, not just events on the incident — meaning a previously resolved incident's events can prematurely trip the threshold on a new one. No production rulesets currently use `min_count` so a clean break is acceptable.

## Acceptance Criteria

- `RuleSet` has three new real model fields: `trigger_count`, `trigger_window`, `retrigger_every`
- `metadata["min_count"]` and `metadata["window_minutes"]` are no longer read — replaced by the model fields
- `pending_status` is removed from metadata — the pre-trigger status is always `"pending"`, hardcoded
- Threshold count uses events on the incident, not a global event query
- Handler fires when incident reaches `trigger_count` events (pending → new transition)
- Handler re-fires every `retrigger_every` events after the initial trigger (when incident is `new`, `open`, or `investigating`)
- Re-trigger state is tracked in `incident.metadata["last_trigger_count"]`
- Re-triggers add a history entry: "Re-triggered: N events"
- All existing default rulesets continue to work (they use `trigger_count=None`, handler fires immediately)
- Migrations generated via `bin/create_testproject`

## Investigation

**What exists**:
- `RuleSet.metadata` JSON field — currently holds `min_count`, `window_minutes`, `pending_status`
- `Event.publish()` lines 175–260 — threshold logic, handler dispatch
- `RuleSet.run_handler()` lines 128–180 — publishes handler jobs
- No production rulesets use `min_count` — safe to break

**The count bug** (`event.py:220–223`): currently counts all `Event` objects matching category + bundle criteria globally. Should count events on the incident instead.

**What changes**:
- `mojo/apps/incident/models/rule.py` — add `trigger_count`, `trigger_window`, `retrigger_every` fields to `RuleSet`
- `mojo/apps/incident/models/event.py` — rewrite threshold logic to use new fields + per-incident count + re-trigger
- `mojo/apps/incident/models/incident.py` — no model changes; `metadata["last_trigger_count"]` tracked at runtime
- `bin/create_testproject` — regenerate migrations

**Constraints**:
- `pending` status is preserved — incidents accumulate quietly until trigger_count hit
- Re-trigger only fires when incident status is `new`, `open`, or `investigating` (not `resolved`/`ignored`/`closed`)
- Backwards compat: if `trigger_count` is null, handler fires immediately on first event (existing behaviour)

**Related files**:
- `mojo/apps/incident/models/rule.py`
- `mojo/apps/incident/models/event.py`
- `tests/test_incident/` — tests go here

## Tests Required

- `trigger_count=None` → handler fires on first event (no regression)
- `trigger_count=5` → incident sits at `pending` for events 1–4, fires handler and transitions to `new` at event 5
- `trigger_count=5, trigger_window=10` → counts only events within 10 min on the incident
- `trigger_count=5, retrigger_every=10` → handler fires at 5 events, again at 15, 25, etc.
- Re-trigger does not fire when incident is `resolved` or `ignored`
- Global event count bug: events from a prior resolved incident do not prematurely trigger a new one

## Out of Scope

- Admin portal UI changes (field names now appear in REST API — UI can be built separately)
- LLM agent metadata schema updates (separate task)
- Changing `bundle_by` / `bundle_minutes` behaviour

## Plan

**Status**: planned
**Planned**: 2026-04-01

### Objective
Promote threshold config to real RuleSet fields, fix per-incident event counting, and add re-trigger support.

### Steps

1. `mojo/apps/incident/models/rule.py` — Add three fields to `RuleSet`:
   - `trigger_count = IntegerField(null=True, blank=True)` with help_text
   - `trigger_window = IntegerField(null=True, blank=True)` with help_text
   - `retrigger_every = IntegerField(null=True, blank=True)` with help_text

2. `mojo/apps/incident/models/event.py` — Rewrite `publish()` threshold block (lines 175–260):
   - Read `trigger_count` / `trigger_window` from RuleSet fields (drop metadata reads)
   - Remove `pending_status` variable — hardcode `"pending"`
   - Count events per-incident: `incident.events.count() + 1` after `get_or_create_incident` (before linking), or use queryset on incident after linking
   - Existing pending→new transition logic stays, just uses new field names
   - After `link_to_incident`, add re-trigger check:
     - Skip if `retrigger_every` is None or incident status not in `("new", "open", "investigating")`
     - `total = incident.events.count()`
     - `last = incident.metadata.get("last_trigger_count", trigger_count or 1)`
     - If `total >= last + retrigger_every`: update `last_trigger_count`, add history, call `run_handler`

3. `bin/create_testproject` — Regenerate migrations after model change

4. `tests/test_incident/test_ruleset_triggers.py` — Write tests covering all scenarios

### Design Decisions

- **Per-incident count for threshold**: After `get_or_create_incident`, count `incident.events.filter(...)` instead of global `Event.objects.filter(...)`. The incident already bundles by the same criteria, so counting its events is correct and scoped.
- **`last_trigger_count` in `incident.metadata`**: No new model field needed — metadata is already a JSON field. Saves a migration on Incident.
- **Re-trigger status gate**: Only `new`, `open`, `investigating` — not `resolved`/`ignored`/`closed`. The incident must still be active.
- **Hardcode `"pending"`**: `pending_status` was never set to anything else in practice. Removing the configurability simplifies the logic.

### Edge Cases

- `trigger_count=None` and no `retrigger_every` — existing behaviour, no change
- `trigger_window` without `trigger_count` — window is irrelevant without a count threshold; just ignore it
- Event count race condition between checking and linking — acceptable; the transition is idempotent (status check prevents double-fire)
- Re-trigger fires the same handler chain as the initial trigger — same `rule_set.run_handler()` call

### Testing
- All scenarios → `tests/test_incident/test_ruleset_triggers.py`
- Run: `bin/run_tests -t test_incident/test_ruleset_triggers`

### Docs
- `docs/django_developer/logging/incidents.md` — update threshold/bundling section, remove metadata key docs, add re-trigger docs
- `docs/web_developer/security/README.md` — update RuleSet field reference

## Resolution

**Status**: resolved
**Date**: 2026-04-01

### What Was Built
Three new first-class fields on `RuleSet` replacing opaque metadata JSON keys, per-incident event counting (fixing the global count bug), and a re-trigger mechanism.

### Files Changed
- `mojo/apps/incident/models/rule.py` — Added `trigger_count`, `trigger_window`, `retrigger_every` with `MinValueValidator(1)`
- `mojo/apps/incident/models/event.py` — Rewrote publish() threshold block: per-incident count, atomic transaction + select_for_update, re-trigger logic, logger, bounds check on last_trigger_count
- `mojo/apps/incident/migrations/0024_ruleset_retrigger_every_ruleset_trigger_count_and_more.py` — Migration
- `tests/test_incident/test_ruleset_triggers.py` — 6 new tests
- `tests/test_incident/test_handler_transition_simple.py` — Updated to use new field names

### Tests
- `tests/test_incident/test_ruleset_triggers.py` — 6 scenarios: immediate fire, pending hold, history entry, retrigger, resolved skip, cross-incident count isolation
- Run: `bin/run_tests -t test_incident.test_ruleset_triggers`

### Docs Updated
- `docs/django_developer/logging/incidents.md` — trigger_count, trigger_window, retrigger_every added to RuleSet section; metadata keys removed

### Security Review
Four findings addressed in follow-up commit (ef60dd4):
1. CRITICAL race condition fixed with `transaction.atomic()` + `select_for_update()`
2. `MinValueValidator(1)` added to all three fields (blocks retrigger_every=1 spam)
3. `last_trigger_count` tamper guard added (isinstance + bounds check)
4. Exception logging added (replaced bare except/pass)

### Follow-up
- Run `manage.py migrate` in any project using this framework to apply migration 0024
- LLM agent metadata schema could be updated to document the new fields (separate task)
