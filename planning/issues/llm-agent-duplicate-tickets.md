# LLM agent creates duplicate tickets for the same incident

**Type**: bug
**Status**: planned
**Date**: 2026-04-20
**Severity**: medium

## Description
`_tool_create_ticket` in `mojo/apps/incident/handlers/llm_agent.py` unconditionally calls `Ticket.objects.create(...)` every time the LLM invokes the `create_ticket` tool. When the agent is re-invoked on the same incident (new events bundled into the same incident, rule refires, or analysis reruns), it has no way to notice an existing ticket and keeps creating fresh ones, splintering the conversation across multiple parallel tickets.

The agent should first look for an existing open, LLM-linked ticket for the same incident and, if found, append a `TicketNote` to that ticket instead of creating a new `Ticket`.

## Context
- The LLM agent is designed to hold a conversation with humans through a ticket (see `execute_llm_ticket_reply` — a human reply re-invokes the agent via `TicketNote.on_rest_saved` at `mojo/apps/incident/models/ticket.py:89-99`).
- That conversation flow assumes **one** ticket per incident/topic. When the agent creates a second ticket, the human sees duplicate notifications, and replies on the "wrong" ticket won't carry the full conversation context because `execute_llm_ticket_reply` only loads notes for `ticket_id` it was fired with.
- This also amplifies noise in `category="llm_review"` queues and inflates the count of open tickets that admins must triage.
- `_tool_create_rule` at `mojo/apps/incident/handlers/llm_agent.py:683` calls `_tool_create_ticket` internally for rule-approval tickets, so the same duplication can happen whenever the analysis tool re-proposes a similar rule.

## Acceptance Criteria
- When `_tool_create_ticket` is called with an `incident_id` that already has an open LLM-linked ticket (`metadata.llm_linked == True`, `status` not closed/resolved), it must reuse that ticket:
  - Append a `TicketNote` containing the new `note` (same `[LLM Agent] ...` prefix used today).
  - Return `{"ok": True, "ticket_id": <existing_pk>, "deduplicated": True}` (or equivalent) so the LLM/telemetry can see the reuse.
  - Do not create a new `Ticket` row.
- When no matching open ticket exists, behavior stays unchanged — a new ticket is created as today.
- Closed/resolved tickets must NOT suppress new ticket creation — if the prior ticket is closed, a fresh ticket is allowed.
- Tickets created with no `incident_id` (e.g. general rule proposals) continue to create new tickets; dedup only applies when `incident_id` is present.
- `add_history("handler:llm", ...)` on the incident records the note-append event (parallel to the existing "Created ticket #N" history entry).
- Regression test: calling the tool twice in a row for the same incident yields one `Ticket` and two `TicketNote` rows.

## Investigation
**Likely root cause**: `_tool_create_ticket` at `mojo/apps/incident/handlers/llm_agent.py:540-578` has no lookup step — it goes straight to `Ticket.objects.create(...)` every time.

**Confidence**: confirmed (behavior is visible in the code path; no dedup guard exists anywhere upstream or in the tool).

**Code path**:
- `mojo/apps/incident/handlers/llm_agent.py:540` — `_tool_create_ticket` entry.
- `mojo/apps/incident/handlers/llm_agent.py:551-558` — unconditional `Ticket.objects.create(...)`.
- `mojo/apps/incident/handlers/llm_agent.py:683` — `_tool_create_rule` also goes through this path for its approval tickets.
- `mojo/apps/incident/models/ticket.py:41` — `Ticket.incident` FK with `related_name="tickets"` (makes the lookup `incident.tickets.filter(...)` straightforward).
- `mojo/apps/incident/models/ticket.py:54-56` — `Ticket.add_note(note, user)` helper that already exists and wraps `TicketNote.objects.create`.
- `mojo/apps/incident/models/ticket.py:89-99` — human-reply hook; only one ticket per conversation is assumed by this design.

**Regression test**: not written — feasible but would require the LLM tool dispatch be exercised against a live DB with an `Incident`, system superuser, and `Ticket` fixture. Recommend adding it as part of the fix under `tests/test_incident/` using the `testit` harness.

**Related files**:
- `mojo/apps/incident/handlers/llm_agent.py` (the fix)
- `mojo/apps/incident/models/ticket.py` (possible helper addition — e.g., a `Ticket.find_open_for_incident(incident)` classmethod, if the lookup is reused elsewhere)
- `tests/test_incident/` (new regression test)

## Plan

**Status**: planned
**Planned**: 2026-04-20

### Objective
Add two dedup layers in the LLM agent handler: (A) reuse an existing open, LLM-linked ticket for an incident instead of creating a duplicate, and (B) reuse an existing `llm_proposed` RuleSet + its approval ticket instead of spawning hundreds of identical rule-approval tickets.

### Steps
1. **[mojo/apps/incident/handlers/llm_agent.py](mojo/apps/incident/handlers/llm_agent.py)** — add module-level helpers:
   - `_rule_signature(category, handler, rules_list)` — canonical string from category, handler, and sorted `(field_name, comparator, value, value_type)` tuples of child rules.
   - `_rule_signature_from_ruleset(ruleset)` — same signature computed from a persisted `RuleSet` (uses its `rules` related manager).
2. **`_tool_create_ticket`** at [mojo/apps/incident/handlers/llm_agent.py:540](mojo/apps/incident/handlers/llm_agent.py:540):
   - When `params.get("incident_id")` resolves to an incident, query `incident.tickets.filter(metadata__llm_linked=True).exclude(status__in=["closed", "resolved"]).order_by("-modified").first()`. If found: append a `TicketNote` with `f"[LLM Agent] {params['note']}"` via the existing superuser-lookup pattern, record `incident.add_history("handler:llm", note=f"[LLM Agent] Appended to existing ticket #{ticket.pk}: {params['title']}")`, and return `{"ok": True, "ticket_id": existing.pk, "deduplicated": True}`.
   - Accept an optional `params["ruleset_id"]` (internal use — not exposed in Claude's tool schema). When present on the create path, merge `{"ruleset_id": <id>}` into the new ticket's `metadata` alongside `llm_linked=True`.
3. **`_tool_create_rule`** at [mojo/apps/incident/handlers/llm_agent.py:640](mojo/apps/incident/handlers/llm_agent.py:640):
   - Compute signature from the incoming `params` up front.
   - Scan `RuleSet.objects.filter(category=params["category"], metadata__llm_proposed=True)` and compare signatures.
   - **Pending match** (`metadata.disabled == True`): do NOT create a new `RuleSet`. Bump `metadata.occurrence_count` on the existing RuleSet (initialize to 1 + new = 2 if absent). Find the approval ticket via `Ticket.objects.filter(metadata__ruleset_id=existing.pk).exclude(status__in=["closed", "resolved"]).order_by("-modified").first()`. If found, append a note summarizing the new sighting (`"Pattern seen again — total observations: N"`). If no open ticket exists (e.g., human closed/rejected the original), fall through to `_tool_create_ticket` to create a fresh approval ticket for the **existing** `ruleset`, passing `ruleset_id=existing.pk`. Return `{"ok": True, "ruleset_id": existing.pk, "ticket_id": ..., "deduplicated": True, "occurrence_count": N}`.
   - **Active match** (`metadata.disabled` not True): no action — rule is already live. Return `{"ok": True, "ruleset_id": existing.pk, "deduplicated": True, "already_active": True}`.
   - **No match**: fall through to today's create path. When calling `_tool_create_ticket` for the approval ticket, pass `ruleset_id=ruleset.pk` through params.
4. **[tests/test_incident/llm_agent.py](tests/test_incident/llm_agent.py)** — three new tests:
   - `test_llm_agent_create_ticket_deduplicates` — two `create_ticket` tool calls in one agent loop for the same incident → one `Ticket`, two `TicketNote` rows, second tool result contains `deduplicated=True`.
   - `test_llm_agent_create_rule_deduplicates_pending` — two `create_rule` calls with identical payloads → one `RuleSet`, one approval `Ticket`, RuleSet's `metadata.occurrence_count == 2`, second tool result contains `deduplicated=True`.
   - `test_llm_agent_create_rule_deduplicates_active` — pre-seed an enabled (`metadata.disabled == False`) `llm_proposed` RuleSet matching the payload; one `create_rule` call → no new RuleSet, no new Ticket, result contains `already_active=True`.

### Design Decisions
- **Inline lookup for ticket dedup**: 3-line query, no second caller — a model helper would be premature (KISS).
- **Signature scoped to `llm_proposed` RuleSets in the same category**: bounds the scan (categories are the natural partition) and keeps human-authored rules out of the match set so the agent can't accidentally treat a human rule as "already proposed".
- **Signature includes `handler`**: `block://` vs `notify://` for the same conditions are treated as distinct proposals.
- **Child Rules sorted before signing**: rule authorship order doesn't change equivalence.
- **`ruleset_id` passed internally, not in Claude's tool schema**: the LLM doesn't need to know about the link; the glue lives in our handler.
- **`occurrence_count` on RuleSet metadata, not on Ticket**: humans reviewing the RuleSet approval UI see how often the pattern has reoccurred, which is the decision-relevant signal.
- **Closed approval ticket + pending RuleSet re-proposes the ticket**: if a human closed the original approval ticket without disabling the RuleSet metadata flag, the agent should surface the pattern again rather than go silent.
- **Status exclude set = `{"closed", "resolved"}`**: `Ticket.status` is a free `CharField` with no enum; excluding those two values treats any other status (`open`, `investigating`, etc.) as reusable.

### Edge Cases
- **`metadata` JSON queries on null columns**: `metadata__llm_linked=True` and `metadata__llm_proposed=True` filters return false for missing keys — safe across rows without those metadata flags.
- **Incident not found when `incident_id` provided**: existing code leaves `incident=None` and swallows the exception; dedup simply doesn't trigger and a new ticket is created, matching today's behavior.
- **RuleSet with zero child Rules**: signature becomes `category|handler`; still deterministic and comparable.
- **Race between two concurrent agent invocations creating the same ticket/rule**: no DB-level uniqueness constraint, so a narrow window for duplicates remains. Acceptable — the job engine serializes per-channel, and the cost of a rare duplicate is low vs. the added complexity of transaction-level locks.
- **Pre-fix RuleSets whose approval tickets lack `metadata.ruleset_id`**: first re-proposal after the fix ships creates a fresh approval ticket for the existing RuleSet (legacy tickets are simply not discoverable). No migration needed.
- **Signature drift across bundle_by / bundle_minutes / min_count / window_minutes**: these are NOT in the signature. Two proposals with identical match conditions but different bundling are deduped as the same pattern — intentional, since the semantic "what this matches" is what matters for dedup; the first-proposed bundling settings win and humans can edit on approval.

### Testing
- `test_llm_agent_create_ticket_deduplicates` → `tests/test_incident/llm_agent.py`
- `test_llm_agent_create_rule_deduplicates_pending` → `tests/test_incident/llm_agent.py`
- `test_llm_agent_create_rule_deduplicates_active` → `tests/test_incident/llm_agent.py`
- Existing `test_llm_agent_create_ticket` must continue to pass (single-call path unchanged).

### Docs
- `CHANGELOG.md` — one-line entry under the next release: LLM agent deduplicates incident tickets and rule proposals; re-observations of a pending rule bump `occurrence_count` instead of spawning new tickets.
- No `docs/django_developer/` or `docs/web_developer/` updates needed (internal handler behavior, no REST/model surface change).
