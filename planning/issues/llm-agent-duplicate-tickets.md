# LLM Agent Creates Duplicate Tickets for Same Pattern Across Incidents

**Type**: bug
**Status**: planned
**Date**: 2026-05-07
**Severity**: high

## Description

The LLM security agent creates multiple tickets for the same underlying issue when separate incidents arrive for the same pattern. Example: 4 tickets (IDs 279-282) were created for the same "duplicate ATM transaction" pattern — one per incident — because the dedup logic only checks tickets linked to the *same* incident, not tickets about the same category/pattern across different incidents.

## Context

Each `atm:dup` incident fires the LLM agent independently. The agent investigates, decides it needs human review, and calls `create_ticket`. The dedup check in `_tool_create_ticket` (line 649-664) only looks at `incident.tickets` — tickets linked to *that specific incident*. When incident #7042 arrives 6 minutes after #7040, it has no tickets of its own, so a new ticket is created even though an identical review ticket for #7040 already exists.

This creates noise for operators: they see 3-4 tickets about the same pattern and must mentally de-duplicate.

The same problem affects `_tool_suggest_rule_update` to a lesser degree — it deduplicates by `metadata__ruleset_id` which is narrower but at least cross-incident.

## Root Cause (Two Gaps)

### Gap 1: `_tool_create_ticket` dedup is per-incident only
`llm_agent.py:649-664` — The dedup check queries `incident.tickets.filter(metadata__llm_linked=True)`. This only finds tickets already linked to the same incident object. Different incidents for the same pattern each get their own ticket.

### Gap 2: No existing-ticket context in the LLM prompt
`llm_agent.py:1304-1355` (`_build_incident_message`) — The prompt includes sections for active rules and pending rule proposals, but does NOT include any information about existing open tickets. The LLM has no way to know that a ticket already exists for this category/pattern — it can only see the current incident's data.

By contrast, the analysis prompt (`_build_analysis_message`, line 1433) *does* pre-load related open incidents, which is why the analysis flow can merge things together. The triage flow lacks this awareness.

### Why it's worse than it looks
The `triage_new_incidents` cron (asyncjobs.py:413) can queue multiple incidents in the same batch. If two `atm:dup` incidents arrive within the same sweep window, both LLM agents run concurrently with zero visibility into each other's ticket creation — a classic TOCTOU race even if prompt-level dedup existed.

## Acceptance Criteria

- When the LLM agent is about to create a ticket, it should first check for existing open LLM-linked tickets in the same category (not just the same incident)
- If a matching open ticket exists, append a note to it (with the new incident details) and link the new incident to the existing ticket
- The LLM prompt should include a section showing existing open tickets for this category, so the LLM can make intelligent decisions about whether to create vs. update
- The `create_ticket` tool's dedup should be a code-level safety net (not reliant on the LLM choosing correctly)
- After the fix, the scenario from the report (3 `atm:dup` incidents within minutes) should produce 1 ticket with 3 notes, not 3 separate tickets

## Investigation

**Likely root cause**: `_tool_create_ticket` dedup is scoped to `incident.tickets` (per-incident) instead of checking all open LLM tickets in the same category. Additionally, the triage prompt lacks existing-ticket context, so the LLM doesn't know a ticket already exists.

**Confidence**: confirmed (code analysis — the dedup query is clearly per-incident, and no broader check exists)

**Code path**:
- `llm_agent.py:637-685` — `_tool_create_ticket` with per-incident-only dedup
- `llm_agent.py:1304-1355` — `_build_incident_message` (no existing ticket section)
- `llm_agent.py:1256-1279` — `_build_pending_proposals_section` (model for how to add ticket section)
- `asyncjobs.py:413-465` — `triage_new_incidents` (concurrent dispatch, no cross-incident coordination)

**Regression test**: not feasible — requires LLM API mocking and a running incident pipeline

**Related files**:
- `mojo/apps/incident/handlers/llm_agent.py` — primary fix location
- `mojo/apps/incident/models/ticket.py` — Ticket model (for queryset patterns)
- `mojo/apps/incident/asyncjobs.py` — triage_new_incidents cron (potential batching improvement)

## Suggested Fix Approach

1. **Add `_build_open_tickets_section(category)`** — similar to `_build_pending_proposals_section`, query open `llm_review` tickets in the same category and include them in the prompt. This lets the LLM call `add_ticket_note` on an existing ticket instead of `create_ticket`.

2. **Broaden `_tool_create_ticket` dedup** — after the per-incident check, add a category-level check: query `Ticket.objects.filter(category="llm_review", incident__category=<category>, metadata__llm_linked=True).exclude(status__in=TICKET_CLOSED_STATUSES)`. If found, append a note, link the new incident, and return `deduplicated=True`.

3. **Optionally**: pass existing ticket IDs into the `create_ticket` tool description so the LLM is guided to prefer `add_ticket_note` when duplicates exist.

## Plan

**Status**: planned
**Planned**: 2026-05-07

### Objective

Prevent the LLM agent from creating duplicate tickets for the same incident pattern by adding both prompt-level awareness of existing open tickets and a code-level category-based dedup safety net in `_tool_create_ticket`.

### Steps

1. `mojo/apps/incident/handlers/llm_agent.py` — **Add `_build_open_tickets_section(category)`**
   New function modeled on `_build_pending_proposals_section` (line 1256). Queries open LLM-linked tickets whose incident shares the same category:
   ```
   Ticket.objects.filter(
       category="llm_review",
       metadata__llm_linked=True,
       incident__category=category,
       incident__isnull=False,
   ).exclude(status__in=TICKET_CLOSED_STATUSES)
   .select_related("incident")
   .order_by("-modified")[:10]
   ```
   Returns a prompt section listing each ticket's ID, title, linked incident ID, and creation time. Includes instruction: "If an existing ticket covers the same pattern, use `add_ticket_note` with the ticket ID instead of calling `create_ticket`."

2. `mojo/apps/incident/handlers/llm_agent.py` — **Wire into `_build_incident_message`**
   After the `pending_section` block (line 1348-1350), call `_build_open_tickets_section(event.category)` and append to `parts`.

3. `mojo/apps/incident/handlers/llm_agent.py` — **Update SYSTEM_PROMPT**
   Add a bullet to the Guidelines section (around line 43-55):
   ```
   - Before creating a ticket, check the "Open Tickets for This Category" section in the incident context. If a ticket already covers this pattern, use add_ticket_note to append your findings to it instead of creating a duplicate ticket.
   ```

4. `mojo/apps/incident/handlers/llm_agent.py` — **Update `create_ticket` tool description**
   Change the description (line 206) to mention dedup behavior:
   ```
   "Create a ticket for human review. IMPORTANT: Check the 'Open Tickets' section first — if an existing ticket covers this pattern, use add_ticket_note instead. The tool auto-deduplicates within the same incident category, but preferring add_ticket_note avoids unnecessary API calls."
   ```

5. `mojo/apps/incident/handlers/llm_agent.py` — **Broaden `_tool_create_ticket` dedup** (line 648-664)
   After the existing per-incident dedup block, add a category-level fallback:
   - Get the incident's category: `incident_category = incident.category if incident else None`
   - If `incident_category` is set, query for any open LLM-linked ticket in that category:
     ```
     Ticket.objects.filter(
         category="llm_review",
         metadata__llm_linked=True,
         incident__category=incident_category,
         incident__isnull=False,
     ).exclude(status__in=TICKET_CLOSED_STATUSES)
     .order_by("-modified").first()
     ```
   - If found: append a note to the existing ticket (include the new incident ID and title in the note text), add a reference metadata entry pointing to the new incident (`{"model": "incident.Incident", "pk": incident.pk}`), record in the new incident's history that it was folded into the existing ticket, and return `{"ok": True, "ticket_id": existing.pk, "deduplicated": True}`

6. `mojo/apps/incident/handlers/llm_agent.py` — **Add `incident_id` to `add_ticket_note` tool schema**
   Currently `add_ticket_note` (line 306-329) doesn't accept an `incident_id`. Add an optional `incident_id` property so the LLM can explicitly link an incident reference when appending notes. The tool implementation should auto-add the incident as a reference if provided.

### Design Decisions

- **Category-level dedup, no time window**: If a ticket is open, it absorbs new incidents in the same category. Closing the ticket resets dedup naturally. Simpler than time-windowed logic and matches how operators actually work (one open ticket per active pattern).
- **Prompt awareness is the primary fix, code dedup is the safety net**: The LLM seeing existing tickets and choosing `add_ticket_note` produces better results (richer context in notes, smarter grouping). The code dedup catches the concurrent race case from `triage_new_incidents` batching.
- **No schema change (no M2M)**: Use note references to link additional incidents to an existing ticket rather than adding a M2M relationship. Operators can click through to each incident from the ticket notes. This avoids a migration for a dedup fix.
- **`select_related("incident")` on the ticket query**: Avoids N+1 when building the prompt section (need incident.category and incident.pk).

### Edge Cases

- **Concurrent LLM agents**: Two agents run simultaneously for the same category. First one creates a ticket; second one hits the code-level dedup and appends a note. The DB query in the dedup check is atomic — no race window.
- **Ticket with no incident**: Some tickets may be created without an `incident_id` (e.g., manually created). The category dedup filters on `incident__isnull=False`, so these are ignored.
- **Different subcategories**: `atm:dup` and `atm:pin_anomaly` are different categories. Category matching is exact, so they get separate tickets. This is correct.
- **Very old open tickets**: A ticket left open for weeks would still absorb new incidents. This is acceptable — if operators don't want that, they close the ticket. Could add a staleness cutoff later if needed, but YAGNI for now.
- **No incident provided to create_ticket**: LLM calls `create_ticket` without `incident_id`. The category dedup is skipped (no incident to derive category from). This is fine — rare path, and the prompt awareness still helps.

### Testing

- Not feasible as automated tests — requires LLM API mocking and a running incident pipeline with real DB state
- Manual verification: create 3 incidents with the same category, run `triage_new_incidents`, confirm only 1 ticket is created with notes from all 3

### Docs

- `docs/django_developer/security/README.md` — update the LLM agent section to mention ticket dedup behavior (if this section exists; otherwise note in the incident handlers doc)
