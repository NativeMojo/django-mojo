# LLM Review Loop Creates Duplicate Rule Proposals and Fails to Auto-Activate on Approval

**Type**: bug + enhancement
**Status**: planned
**Date**: 2026-05-06
**Severity**: critical

## Description

Three related issues in the LLM security agent (`mojo/apps/incident/handlers/llm_agent.py`):

1. **Duplicate rule proposals**: When multiple security incidents of the same type arrive in quick succession, each independent LLM triage call generates a new rule proposal. The existing signature-based dedup (`_find_matching_proposed_ruleset`) only catches exact matches — when the LLM generates slightly different rule definitions (different regex, different fields) for the same attack pattern, the signatures differ and duplicates are created. Additionally, the LLM has no visibility into existing pending proposals, so it can't avoid proposing duplicates.

2. **Approval not auto-activating rules**: When an admin replies "approved" to a rule proposal ticket, the LLM agent re-invokes but has no `activate_rule` or `close_ticket` tool. It can only respond with text or create yet another ticket, resulting in extra manual steps and ticket noise.

3. **`metadata.disabled` vs `is_active`**: Rules were created with `metadata.disabled=True` but the rule matching engine (`RuleSet.find_match` at `rule.py:700`) gates on `is_active=True` — it never checks `metadata.disabled`. So LLM-proposed rules went live immediately without human review.

## Context

This is a critical security workflow issue. The intent is that LLM-proposed rules are created in a disabled/inactive state, reviewed by a human via a ticket, and only activated on approval. All three bugs undermined this safety workflow: rules went live immediately (bug 3), approvals didn't actually activate anything (bug 2), and the review queue was flooded with duplicates (bug 1). Observed in production: 6 near-identical rulesets (#78-#83) and 7 duplicate tickets (#271-#277) for the same credential/config file harvesting scan pattern.

## Acceptance Criteria

- One rule proposal per unique attack pattern; subsequent incidents link to the existing open proposal
- LLM-proposed rules are genuinely inactive (`is_active=False`) until approved
- An "approved" reply on a proposal ticket deterministically activates the linked ruleset and closes the ticket
- Non-approval replies (questions, rejections) still go through the normal LLM conversation flow

## Investigation

**Likely root cause**: Three distinct code gaps — (1) no cross-incident context for the LLM, no fallback dedup beyond exact signature; (2) missing approval-handling capability; (3) wrong field used for rule gating.

**Confidence**: confirmed

**Code path**:
- Rule matching: `mojo/apps/incident/models/rule.py:700` — filters `is_active=True`
- Rule creation: `mojo/apps/incident/handlers/llm_agent.py:_tool_create_rule` — was setting `metadata.disabled=True` instead of `is_active=False`
- Dedup (exact): `llm_agent.py:_find_matching_proposed_ruleset` — signature-based, no fuzzy fallback
- Dedup (ticket): `llm_agent.py:_tool_create_ticket` — per-incident only, not cross-incident
- Ticket reply: `llm_agent.py:execute_llm_ticket_reply` — no approval detection, no rule activation tool
- Incident prompt: `llm_agent.py:_build_incident_message` — no pending proposal context

**Regression tests**: `tests/test_incident/llm_agent.py` — 10 tests total, 3 new:
- `test_llm_agent_create_rule_deduplicates_variant` — two triage jobs with different signatures, same category
- `test_llm_ticket_approval_activates_rule` — approval reply activates rule, closes ticket, no LLM call
- `test_llm_ticket_non_approval_still_invokes_llm` — question reply still goes through LLM

**Related files**:
- `mojo/apps/incident/handlers/llm_agent.py` (all fixes)
- `mojo/apps/incident/models/rule.py` (reference — `is_active` field, `find_match` query)
- `tests/test_incident/llm_agent.py` (new and updated tests)

## Initial Fix (already committed)

1. `is_active=False` instead of `metadata.disabled` — rules are genuinely inactive now
2. Prompt-level pending proposal context — LLM sees existing proposals
3. Category-level time-windowed dedup fallback — catches variant signatures
4. Regex-based approval detection — temporary, replaced by the plan below

## Plan

**Status**: planned
**Planned**: 2026-05-06

### Objective

Replace the fragile regex-based approval and time-windowed dedup with a robust ticket action system — structured action blocks on TicketNote metadata, app-scoped handlers, generic LLM opt-in per ticket, and existing-rule awareness to prevent duplicate proposals.

### Steps

#### Phase 1: TicketNote action infrastructure

1. `mojo/apps/incident/models/ticket.py` — Add `metadata = models.JSONField(default=dict, blank=True)` to TicketNote. Migration required.

2. `mojo/apps/incident/handlers/ticket_actions.py` (new) — Action handler registry and dispatch logic:
   - `ACTION_HANDLERS` dict: `"app.handler_name" → callable`
   - `dispatch_action(ticket, note, response_meta)` — validates handler exists, calls it
   - Auto-discovery: each app with `ticket_actions.py` registers handlers at startup

3. `mojo/apps/incident/models/ticket.py` — Update `TicketNote.on_rest_saved`:
   - If `metadata.action_response` is present → call `dispatch_action()`
   - If `metadata.action_response` is NOT present and ticket has `llm_enabled` → invoke LLM
   - Replaces the current `_is_llm_ticket()` check

4. `mojo/apps/incident/handlers/ticket_actions.py` — Initial handlers:
   - `incident.rule_approval` — approve: `is_active=True` + close ticket; deny: delete ruleset + close ticket
   - `incident.rule_update` — approve: apply proposed changes to existing ruleset + close; deny: close
   - `incident.block_confirm` — approve: execute block; deny: close
   - `incident.merge_confirm` — approve: merge incidents; deny: close
   - `incident.whitelist` — approve: add IP to allowlist; deny: close
   - `incident.escalate` — approve: send SMS/notify to on-call; deny: close

#### Phase 2: LLM as opt-in per ticket

5. `mojo/apps/incident/models/ticket.py` — Add `enable_llm` / `disable_llm` to Ticket `POST_SAVE_ACTIONS`:
   - `on_action_enable_llm`: set `metadata.llm_enabled = True`, immediately invoke LLM with full conversation
   - `on_action_disable_llm`: set `metadata.llm_enabled = False`

6. `mojo/apps/incident/handlers/llm_agent.py` — Update `execute_llm_ticket_reply`:
   - Remove `_try_handle_rule_approval` and `_APPROVAL_RE` (replaced by action system)
   - LLM invocation now gated on `metadata.llm_enabled` instead of `metadata.llm_linked`
   - Backward compat: treat `llm_linked=True` as `llm_enabled=True`

7. `mojo/apps/incident/handlers/llm_agent.py` — Add `request_approval` tool to TOOLS list:
   ```
   request_approval(action_type, handler, label, options?, context)
   ```
   Creates a TicketNote with `metadata.action` block. The LLM uses this instead of executing destructive actions directly when uncertain.

#### Phase 3: Dedup simplification

8. `mojo/apps/incident/handlers/llm_agent.py` — Replace `_find_recent_proposed_ruleset` + time window with:
   - Check for any open rule-proposal ticket in this category (no time limit)
   - Query: `Ticket.objects.filter(category="llm_review", status="open", metadata__action__handler="incident.rule_approval", metadata__action__context__category=<category>)` or simpler: open ticket with `metadata.ruleset_id` pointing to an inactive `llm_proposed` ruleset in same category
   - If found → bump occurrence count, append note, return deduped
   - Remove `DEDUP_WINDOW_MINUTES` constant

9. `mojo/apps/incident/handlers/llm_agent.py` — Keep exact-signature check against ACTIVE rules only (to detect "rule already live, nothing to do"). Remove signature check against pending rules (open-ticket check handles that).

#### Phase 4: Existing rule awareness + suggest_rule_update

10. `mojo/apps/incident/handlers/llm_agent.py` — Expand `_build_incident_message` to include active rules for the category:
    - Query active rulesets for this category (limit 5)
    - Show their names, conditions, handlers
    - System prompt guidance: "If an existing rule covers a similar pattern but missed this event, suggest modifying it instead of creating a new rule"

11. `mojo/apps/incident/handlers/llm_agent.py` — Add `suggest_rule_update` tool:
    ```
    suggest_rule_update(ruleset_id, proposed_rules, reasoning)
    ```
    - Creates a ticket with an action note: `handler: "incident.rule_update"`
    - Context includes target ruleset (model ref), proposed changes, and original rules for diff view
    - Deduplicates: if open update-suggestion ticket exists for same ruleset, appends note

12. `mojo/apps/incident/handlers/ticket_actions.py` — `incident.rule_update` handler:
    - On approve: replace ruleset's child Rule objects with proposed rules, add history
    - On deny: close ticket, no changes

#### Phase 5: Action note schema for _tool_create_rule

13. `mojo/apps/incident/handlers/llm_agent.py` — Update `_tool_create_rule` and `_tool_create_ticket`:
    - When creating a rule-proposal ticket, the first note carries an action block:
      ```python
      metadata = {
          "action": {
              "type": "approval",
              "handler": "incident.rule_approval",
              "label": "Approve rule proposal?",
              "context": {
                  "target": {"model": "incident.RuleSet", "pk": <id>},
              }
          }
      }
      ```
    - Ticket metadata gets `requires_approval: True` for UI filtering

### Design Decisions

- **Actions live on notes, not tickets**: The ticket is a conversation; actions are proposed within notes. This keeps the audit trail clean and allows multiple actions per ticket lifecycle.
- **Handler registry is app-scoped**: `"incident.rule_approval"` pattern. Each app owns its handlers. Prevents coupling.
- **Model references in context**: `{"model": "incident.RuleSet", "pk": 123}` — self-describing for the UI to resolve REST URLs and render links/cards generically.
- **LLM opt-in, not opt-out**: `llm_enabled` must be explicitly set. Incidents that create tickets set it automatically. Manual tickets start without LLM unless toggled.
- **`enable_llm` triggers immediate invocation**: When you toggle LLM on, it reads the full thread and responds. Not just silently waiting for the next reply.
- **Dedup by open ticket, not time window**: Simpler, no arbitrary constants. "Is there an open proposal for this category?" is the only question.
- **`request_approval` as a single generic LLM tool**: The LLM composes action blocks through one tool. The handler registry validates and executes. No need for N separate approval tools.
- **Response note carries full context back**: The UI copies `handler` + `context` from the action into the response. Backend doesn't need to look up the original note.

### User Cases

1. **Incident triage → new rule proposal**: LLM triages, no similar rules exist, calls `create_rule` → inactive ruleset created + ticket with approval action → admin clicks Approve → rule goes live, ticket closes
2. **Incident triage → similar rule exists**: LLM sees existing rules in prompt context, recognizes coverage gap, calls `suggest_rule_update` → ticket with approval action showing diff → admin approves → existing rule widened
3. **Incident triage → proposal already pending**: LLM calls `create_rule`, dedup finds open proposal ticket for category → bumps count, appends note, no new rule/ticket
4. **Admin denies proposal**: Clicks Deny → ruleset deleted, ticket closed
5. **Admin asks question on proposal**: Types free text (no action_response) → LLM re-invokes, answers in conversation
6. **LLM uncertain about blocking**: Calls `request_approval` with `block_confirm` handler → admin sees "Block 10.0.0.0/24?" with Approve/Deny → on approve, block executes
7. **Admin enables LLM on manual ticket**: Creates a ticket about a problem, clicks "Enable AI" → LLM reads thread, investigates with tools, responds with findings and possibly action blocks
8. **Non-LLM approval workflow**: Deploy pipeline creates ticket with approval action → admin clicks Approve → handler triggers deploy. No LLM involved.

### Edge Cases

- **Double-click on action**: `on_rest_saved` checks if action is already resolved (original note `metadata.action.resolved = True`) before dispatching. UI disables buttons after first click.
- **Ruleset deleted before approval**: Handler checks `RuleSet.DoesNotExist`, closes ticket with "Rule no longer exists" note.
- **LLM creates action on wrong ticket**: Handler validates context (e.g., ruleset belongs to the right category). Returns error note if invalid.
- **Concurrent approvals on same ruleset**: `is_active` flip is idempotent. Second approval is a no-op, note says "already active".
- **No system user for notes**: `_get_llm_system_user()` returns None → handler logs warning but still executes the action. Note creation is best-effort.
- **Backward compat**: `llm_linked=True` in existing tickets treated as `llm_enabled=True`. Old tickets continue working.

### Testing

- Approval action dispatches correctly → `tests/test_incident/ticket_actions.py`
- Denial deletes ruleset and closes ticket → `tests/test_incident/ticket_actions.py`
- Double-action prevention → `tests/test_incident/ticket_actions.py`
- Open-ticket dedup (no time window) → `tests/test_incident/llm_agent.py`
- `suggest_rule_update` creates correct action note → `tests/test_incident/llm_agent.py`
- `request_approval` tool creates action note → `tests/test_incident/llm_agent.py`
- `enable_llm` triggers immediate LLM invocation → `tests/test_incident/llm_agent.py`
- Existing-rule context in prompt → `tests/test_incident/llm_agent.py`
- Non-LLM action workflow (no LLM involved) → `tests/test_incident/ticket_actions.py`
- Model reference resolution in context → `tests/test_incident/ticket_actions.py`

### Docs

- `docs/django_developer/incident/ticket_actions.md` (new) — action system: schema, handler registration, built-in handlers
- `docs/django_developer/incident/llm_agent.md` — update: `request_approval` tool, `suggest_rule_update` tool, existing-rule context
- `docs/web_developer/incident/tickets.md` — update: action block rendering, response submission, model reference resolution
