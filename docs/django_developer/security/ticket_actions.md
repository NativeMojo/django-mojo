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
    "context": {
      "target": {"model": "incident.RuleSet", "pk": 42}
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
| `resolved` | Stamped `true` by the dispatcher after a successful dispatch |

Tickets created around an approval also carry `metadata.requires_approval:
true` for UI filtering.

## The response note

The UI (or any REST caller with `manage_security`) answers by creating a new
note whose `metadata` carries an `action_response`, copying `handler` and
`context` from the action note — the backend does not have to look up the
original:

```
POST /api/incident/ticket/note
{
  "parent": 10,
  "note": "Approved",
  "metadata": {
    "action_response": {
      "handler": "incident.rule_approval",
      "action": "approve",
      "context": {"target": {"model": "incident.RuleSet", "pk": 42}}
    }
  }
}
```

`action` is `"approve"` or `"deny"`. `TicketNote.on_rest_saved` sees the
`action_response` and dispatches it **instead of** invoking the LLM — a
structured response never triggers a conversational reply.

## Dispatch flow and guards

`dispatch_action(ticket, note, response_meta)`:

1. **Handler must be registered** — unknown names are logged and rejected.
2. **A matching unresolved action note must exist** on the ticket for that
   handler — a response cannot conjure an action that was never proposed.
3. **Terminal tickets are skipped** — a ticket already `closed`/`resolved`
   dispatches nothing (double-click / replay guard, alongside the
   `resolved` stamp on the action note).
4. The handler runs; on success the action note is stamped
   `action.resolved = true`. Handler exceptions are logged and reported as a
   failed dispatch — never propagated into the note save.

## Built-in handlers

| Handler | Approve | Deny |
|---------|---------|------|
| `incident.rule_approval` | `RuleSet.is_active = True`, ticket `resolved`. Refuses targets not flagged `metadata.llm_proposed`; an already-active ruleset is a no-op with a note. | RuleSet **deleted**, ticket `closed` |
| `incident.rule_update` | Replace the target RuleSet's child `Rule` rows with `context.proposed_rules`, ticket `resolved` | No changes, ticket `closed` |
| `incident.block_confirm` | Validate `context.ip` (must parse as an IP address), `IPSet.block_ip(ip, reason)`, ticket `resolved` | Ticket `closed` |
| `incident.escalate` | Email `context.message` to `context.targets` (same target grammar as `notify://` handlers), ticket `resolved` | Ticket `closed` |

Every outcome — including failure paths like "ruleset was deleted before
approval" — is written back to the thread as an `[LLM Agent]` system note.

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
  `incident.rule_update` action carrying the proposed and current rules (for
  a diff view). Deduplicated — an open update-suggestion ticket for the same
  ruleset collects follow-up notes instead of spawning a new one.

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
  `SAVE_PERMS`) — the approval surface is admin-gated.
- Model resolution is whitelist-only; `incident.rule_approval` additionally
  refuses any RuleSet not flagged `llm_proposed`, so the approval path
  cannot flip arbitrary rulesets.
- Approvals are idempotent at three layers: the `resolved` stamp, the
  terminal-status skip, and per-handler no-ops ("already active").
