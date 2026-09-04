# Ticket Actions — Structured Approvals on Ticket Notes

The ticket action system turns a ticket thread into a deterministic approval
workflow. An **action note** proposes something ("Approve rule proposal?",
"Block 10.0.0.1?"); a **response note** approves or denies it; a registered
**handler** executes the outcome. No LLM round-trip, no free-text parsing —
an "approved" reply activates exactly what the action note proposed.

Actions live on **notes, not tickets**: the ticket is a conversation, and a
ticket may carry several actions over its lifecycle. Every proposal and
outcome stays in the thread as an auditable trail.

Source: `mojo/apps/incident/handlers/ticket_actions.py`,
`mojo/apps/incident/models/ticket.py`.

## The action note

A `TicketNote` whose `metadata` carries an `action` block:

```json
{
  "action": {
    "type": "approval",
    "handler": "incident.rule_approval",
    "label": "Approve rule proposal?",
    "schema": "incident.ticket_approval",
    "schema_version": 1,
    "proposal_note_id": 73,
    "state": "pending",
    "resolved": false,
    "context": {
      "target": {"model": "incident.RuleSet", "pk": 42},
      "expected_modified": "2026-09-04T17:18:19.123456+00:00",
      "confirm": "ACTIVATE RULESET 42",
      "confirm_catch_all": "ACTIVATE CATCH-ALL RULESET 42",
      "deny_confirm": "DELETE RULESET 42"
    },
    "review": {
      "target": {"model": "incident.RuleSet", "pk": 42},
      "revision": "2026-09-04T17:18:19.123456+00:00",
      "confirmation": {
        "approve": "ACTIVATE RULESET 42",
        "approve_catch_all": "ACTIVATE CATCH-ALL RULESET 42",
        "deny": "DELETE RULESET 42"
      }
    }
  }
}
```

| Key | Meaning |
|-----|---------|
| `type` | Action kind — `"approval"` renders as Approve/Deny buttons |
| `handler` | Registered handler name, `"app.handler_name"` scoped |
| `label` | Human-readable question the UI shows |
| `context` | Handler-specific payload (model refs, IPs, proposed rules) |
| `schema`, `schema_version` | Durable action contract (`incident.ticket_approval`, version 1) |
| `proposal_note_id` | Immutable identity of the saved server-authored proposal note |
| `review` | Copy of the exact target, revision, and confirmation strings rendered for review |
| `state` | `pending`, transiently `claimed`, then `resolved` after success |
| `resolved` | Stamped `true` only after a successful dispatch |

Action producers save the note, then call `bind_action_note()` to stamp these
server-owned fields. A proposal lacking them is not executable.

Tickets created around an approval also carry `metadata.requires_approval:
true` for UI filtering.

## The response note

The UI answers by creating a new note whose `metadata` carries an
`action_response`. Its object must contain exactly the pending proposal note
ID, handler name, and the operator's choice; the server reloads context from
that exact proposal note:

```
POST /api/incident/ticket/note
{
  "parent": 10,
  "note": "Approved",
  "metadata": {
    "action_response": {
      "proposal_note_id": 73,
      "handler": "incident.rule_approval",
      "action": "approve"
    }
  }
}
```

`action` is `"approve"` or `"deny"`. `TicketNote.on_rest_saved` sees the
`action_response` and dispatches it **instead of** invoking the LLM — a
structured response never triggers a conversational reply.

The response cannot replace the target, revision, confirmation, or proposed
policy. Extra keys (including response-side `context`) invalidate the response.
The server-stored action note is the authority for all of those values.

## Dispatch flow and guards

`dispatch_action(ticket, note, response_meta)`:

1. **The response is bound** — its shape is exact, the response note belongs
   to the ticket, and the named handler is registered.
2. **The ticket and exact proposal are locked** — `select_for_update()` locks
   the ticket and `proposal_note_id` on that ticket. The proposal's schema,
   version, ID, handler, and derived `review` must match its stored context.
3. **Terminal tickets are skipped** — a ticket already `closed`/`resolved`
   cannot execute an unresolved proposal.
4. **Global interactive authority is re-proved at dispatch** — the note must
   carry its active request, the actor must hold global `manage_security` or
   `security`, key-backed sessions are refused, and authentication must be
   within 600 seconds.
5. **The claim and resolution are durable** — the proposal is stamped
   `state="claimed"` with response-note and actor IDs before the handler runs.
   Success sets `state="resolved"`, `resolved=true`, and an auditable
   `resolution`; a reported failure restores `state="pending"` and removes the
   claim. Exceptions roll back the transaction and are logged, never propagated
   into the note save.

A retry or concurrent response with the same choice observes the recorded
success without redispatching. A conflicting choice cannot rewrite the
committed resolution.

## Built-in handlers

| Handler | Approve | Deny |
|---------|---------|------|
| `incident.rule_approval` | Governed `ruleset.activate` against the stored revision/confirmations, ticket `resolved`. Refuses targets not flagged `metadata.llm_proposed`. | Governed, revision-bound RuleSet delete; ticket `closed` |
| `incident.rule_update` | Governed complete inactive replacement from stored `context.ruleset`; ticket `closed` so activation remains a separate review | No changes, ticket `closed` |
| `incident.block_confirm` | Validate and execute the bounded manual block through `mojosec_actions`; resolve only for `applied` or verifiably `pre_existing`, otherwise leave open | Ticket `closed` |
| `incident.escalate` | Email `context.message` to `context.targets` (same target grammar as `notify://` handlers), ticket `resolved` | Ticket `closed` |

Built-in handlers write their operator-visible outcomes — including failure
paths such as "ruleset was deleted before approval" — back to the thread as an
`[LLM Agent]` system note. Responses rejected before handler dispatch are
logged and leave the proposal pending.

## Model references

Handlers resolve targets from a self-describing reference:

```json
{"target": {"model": "incident.RuleSet", "pk": 42, "label": "SSH brute force blocker"}}
```

Resolution is **whitelisted** (`ALLOWED_MODEL_REFS` — currently
`incident.RuleSet` only); any other model path is rejected and logged. The
same shape lets a UI render links/cards generically by mapping `model` to a
REST URL.

## Registering a handler

```python
from mojo.apps.incident.handlers.ticket_actions import register_handler

def _handler_deploy_confirm(ticket, note, action, context):
    if action == "approve":
        ...
        ticket.status = "resolved"
        ticket.save(update_fields=["status"])
    elif action == "deny":
        ticket.status = "closed"
        ticket.save(update_fields=["status"])

register_handler("myapp.deploy_confirm", _handler_deploy_confirm)
```

Handler names are app-scoped (`"app.handler_name"`) — each app owns its
handlers. A handler receives `(ticket, note, action, context)` and is
responsible for setting the ticket's terminal status. Actions work with or
without the LLM: any pipeline can create a ticket with an action note and
get a deterministic approve/deny workflow.

## LLM integration

The LLM security agent composes actions through two tools (see the
[LLM Security Agent](README.md#6-llm-security-agent) tool table):

- **`request_approval(ticket_id, handler, label, context, reasoning)`** —
  the generic path: instead of executing a destructive action directly, the
  agent posts an action note and waits for a human. One tool, any registered
  handler.
- **`suggest_rule_update(ruleset_id, proposed_rules, reasoning)`** — when an
  existing active rule almost covers a pattern, the agent proposes widening
  it rather than creating a duplicate: a ticket with an
  `incident.rule_update` action carrying a prevalidated complete inactive
  replacement, the source revision, and typed confirmation. Deduplicated — an
  open update-suggestion ticket for the same ruleset collects follow-up notes
  instead of spawning a new one.

`create_rule` proposals follow the same shape automatically: the RuleSet is
created `is_active=False` and its review ticket's first note carries an
`incident.rule_approval` action block.

### LLM opt-in per ticket

The conversational LLM is **opt-in** via `Ticket.metadata.llm_enabled`
(legacy `llm_linked` is honored as an alias). Two `POST_SAVE_ACTIONS` toggle
it:

```
POST /api/incident/ticket/<id>   {"enable_llm": 1}    # also invokes the LLM
POST /api/incident/ticket/<id>   {"disable_llm": 1}
```

`enable_llm` immediately queues the agent with the full thread — it reads
and responds, not just waits for the next reply. On an enabled ticket, a
plain note (no `action_response`, not authored by the agent) re-invokes the
LLM; a structured `action_response` always dispatches instead.

## Security notes

- Creating notes requires `manage_security`/`security` (`TicketNote`
  `SAVE_PERMS`), and dispatch separately requires a fresh, global, interactive
  grant. A programmatic note without `active_request` cannot execute an action.
- Model resolution is whitelist-only. `incident.rule_approval` additionally
  refuses any RuleSet not flagged `metadata.llm_proposed`, so *that* handler
  cannot activate an arbitrary ruleset. `incident.rule_update` has no
  `llm_proposed` guard, but both target and complete replacement are bound to
  the server-authored proposal and stale revisions fail closed.
- Approvals are bound to an immutable proposal-note ID and serialized under
  row locks. Same-choice retries converge, conflicting choices fail closed,
  and every RuleSet action also binds the aggregate revision.
