# Approvals — resolving a mutating assistant action

Every assistant tool that changes data is gated. When the model calls one, the
server does **not** run it. It creates a pending action, sends you an `approval`
block, and waits for a real human decision.

Your job as a client is small and exact:

1. Render the `approval` block with fixed **Approve** / **Cancel** controls.
2. Send the operator's decision to `POST /api/assistant/action` (or over the
   WebSocket, when the card does not require step-up auth).
3. Render the resolved block you get back.

Nothing else in the chat can cause a mutation. In particular, an `action`
quick-reply block cannot — see [Not the `action` block](#not-the-action-block).

---

## The `approval` block

Arrives in three places, always the same shape:

- `pending_actions` on a REST assistant response,
- the `assistant_approval_required` WebSocket event,
- the `blocks` array of the turn's message in conversation history.

```json
{
  "type": "approval",
  "action_id": "9f1c6a2e-3d47-4b8a-9c11-8f0d2e5b7a34",
  "conversation_id": 812,
  "tool": "block_ip",
  "title": "Block Ip",
  "description": "block_ip will run with the arguments below.",
  "args": {"ip": "203.0.113.10", "reason": "brute force", "ttl": 3600},
  "preview": {"summary": "3 nodes -> 4 nodes", "details": {"…": "…"}},
  "requires_fresh_auth": false,
  "requires_superuser": false,
  "expires_at": "2026-08-21T18:30:00Z",
  "state": "pending",
  "result": null,
  "failure_code": ""
}
```

| Field | Type | Notes |
|---|---|---|
| `action_id` | string | Opaque UUID. The only identifier you ever send back. It is **not** a bearer capability — it is resolvable only by the authenticated session it was issued to. |
| `conversation_id` | integer | The conversation the action belongs to. |
| `tool` | string | The server-side tool name. |
| `title`, `description` | string | Human-readable header and one-sentence summary. |
| `args` | object | The exact arguments that will run, **redacted**. |
| `preview` | object or null | `{summary, details, revision}` when the tool provides one. |
| `requires_fresh_auth` | boolean | `true` ⇒ resolve over **REST only**, after a step-up. |
| `requires_superuser` | boolean | Informational — the server enforces it regardless. |
| `expires_at` | string | ISO 8601. After this the card is dead. |
| `state` | string | `pending` \| `executing` \| `completed` \| `failed` \| `canceled` \| `expired` \| `superseded` |
| `result` | object or null | Bounded, redacted outcome. Present only in terminal states. |
| `failure_code` | string | Machine token when `state` is `failed` (e.g. `handler_error`, `permission_lost`, `unknown_outcome`, or a service's own code). |

### Rendering rules

- **Render `args`, `description`, `preview.summary` and `preview.details` as
  text, never as HTML.** Those values originate from a language model and are
  untrusted. Escape them.
- There is **no `actions` array**. The panel draws its own fixed Approve /
  Cancel controls for `type: "approval"`, which is what keeps it from ever being
  confused with the legacy `action` quick-reply block.
- Show `args` — the operator is approving *these arguments*, and showing them is
  the whole point of the card.
- When `requires_fresh_auth` is `true`, expect to be asked to re-authenticate.
  Say so on the card so the prompt is not a surprise.
- Only `state: "pending"` is actionable. Every other state renders **inert** —
  disabled controls plus a status line. Re-check `expires_at` on render: a card
  can be past it before you get a chance to touch it.

---

## Resolving over REST

The only path for a card with `requires_fresh_auth: true`, and a perfectly good
path for all the others.

```http
POST /api/assistant/action
Authorization: Bearer <jwt>
Content-Type: application/json

{"action_id": "9f1c6a2e-…", "decision": "approve"}
```

`decision` is `"approve"` or `"cancel"`. `conversation_id` is optional; send it
and the server verifies the binding.

```json
200 {
  "status": true,
  "data": {
    "action": { …the block, with its resolved state… },
    "message_id": 4471
  }
}
```

`message_id` is the server-authored assistant message that now carries the
outcome in the conversation. Append it to the transcript, or refetch the
conversation.

The `Authorization` JWT is the **sole** carrier of recent-authentication
evidence. There is no body field for a token or a password; do not invent one.

### Listing

```http
GET /api/assistant/action                       → your 50 most recent cards
GET /api/assistant/action?conversation=812      → that conversation's cards
```

```json
200 {"status": true, "data": {"actions": [ …blocks with current state… ]}}
```

Owner-scoped: you only ever see your own. Requesting another operator's
conversation returns `404`.

Conversation detail also carries them:
`GET /api/assistant/conversation/812?graph=detail` includes a `pending_actions`
array. **Use it when re-loading history** — the block embedded in an old message
is the card *as proposed*, so without this you would offer Approve on something
that expired an hour ago.

---

## Resolving over the WebSocket

Available for cards with `requires_fresh_auth: false`. The socket authenticates
once at connect and holds no per-message token, so it cannot prove recent
authentication — and a token in a message body is exactly what this design
refuses to do.

**Client → server**

```json
{
  "type": "assistant_approval",
  "conversation_id": 812,
  "action_id": "9f1c6a2e-…",
  "decision": "approve",
  "request_id": "<canonical uuid>"
}
```

Never put a token in this message. If one is present it is ignored, not honoured.

**Server → client**

| Event | When | Payload |
|---|---|---|
| `assistant_approval_required` | A mutating tool produced a card | `{conversation_id, request_id, action_id, block}` |
| `assistant_approval_ack` | Decision accepted for processing | `{conversation_id, request_id, action_id}` |
| `assistant_approval_result` | Terminal for that action | `{conversation_id, request_id, action_id, block, message_id}` |
| `assistant_error` | Refused | `{conversation_id, request_id, code, error}` (plus `action_id` for `reauth_required`) |

`request_id` is echoed on every event for the turn, as with every other assistant
message.

### The step-up handoff

```json
{"type": "assistant_error", "code": "reauth_required",
 "action_id": "9f1c6a2e-…",
 "error": "Re-authenticate, then approve this action over POST /api/assistant/action."}
```

Run your normal re-authentication flow, then re-submit the **same `action_id`**
to `POST /api/assistant/action`. The card is untouched and still approvable.

---

## The failure contract

Every unresolvable case returns one identical body. Unknown id, someone else's
id, wrong conversation, expired, already used, canceled, superseded, a tool that
no longer exists — all the same:

```json
409 {"status": false,
     "error": "This action is no longer available.",
     "error_code": "action_unavailable"}
```

That is deliberate: a stolen `action_id` must not learn whether it is real.
Distinguishable outcomes exist only for the operator the card belongs to.

| `error_code` | HTTP | WebSocket `code` | What to do |
|---|---|---|---|
| `action_unavailable` | 409 | `action_unavailable` | Render the card inert and refresh from `GET /api/assistant/action`. |
| `reauth_required` | 440 | `reauth_required` | Step up, then re-submit over REST. |
| `permission_denied` | 403 | `permission_denied` | The operator lost the grant, or their account/group was deactivated. Render inert. |
| `infrastructure_external` | 403 | `infrastructure_external` | This installation's infrastructure is managed elsewhere. Show the message; do not retry. |
| `precondition_failed` | 409 | `precondition_failed` | The system changed since the card was made. Refresh and ask again. |

**A failed operation is not an HTTP error.** If the tool ran and failed, you get
`200` with `state: "failed"`, a `failure_code`, and a safe message in `result`.
The mutation was attempted; the operator has to be told so. Show it as a failure
card, not as a network error.

---

## Not the `action` block

The `action` block still exists. It is a **quick reply** — buttons whose `value`
is replayed to the assistant as an ordinary chat message.

- It carries **no authority**. Its `action_id` is accepted in the
  `assistant_action` message and discarded server-side; it has never been able to
  execute anything, and now nothing can execute through it.
- Never use it, or build UI around it, as a confirmation for a mutation. The
  server issues its own `approval` card for those, and the model cannot emit one
  — a model-generated block claiming `type: "approval"` is dropped before it ever
  reaches you.

`assistant_action` is unchanged, so existing clients keep working exactly as
before.

---

## A procedure pauses at the card

The approval path never calls the language model. When you approve, the server
runs the operation and writes its **own** message into the conversation — one
line of text plus the resolved block. The model reads that from history on its
next turn.

So a multi-step procedure (a skill, a plan) stops at the first mutating step and
does not resume by itself. That is by design. After the outcome lands, the
operator's next message is what continues the procedure. Build the UI to make
that obvious — show the outcome, keep the input enabled, and do not display a
spinner waiting for a continuation that is not coming.

---

## Minimal wiring

```javascript
function renderApproval(block) {
  const live = block.state === 'pending' && new Date(block.expires_at) > new Date();

  const card = el('div', 'assistant-approval-card');
  card.appendChild(el('h4', null, block.title));                 // textContent
  card.appendChild(el('p', null, block.description));            // textContent
  card.appendChild(renderArgsAsText(block.args));                // never innerHTML

  if (block.requires_fresh_auth) {
    card.appendChild(el('p', 'hint', 'You will be asked to confirm your password.'));
  }

  if (!live) {
    card.appendChild(el('p', `status status-${block.state}`, statusLine(block)));
    return card;
  }

  const approve = el('button', 'btn-primary', 'Approve');
  const cancel  = el('button', 'btn-outline', 'Cancel');
  for (const [btn, decision] of [[approve, 'approve'], [cancel, 'cancel']]) {
    btn.onclick = async () => {
      approve.disabled = cancel.disabled = true;      // no double-submit
      await resolveAction(block, decision);
    };
  }
  card.append(approve, cancel);
  return card;
}

async function resolveAction(block, decision) {
  const resp = await fetch('/api/assistant/action', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}`},
    body: JSON.stringify({action_id: block.action_id, decision}),
  });

  if (resp.status === 440) {          // step-up, then retry the SAME action_id
    await stepUpAuth();
    return resolveAction(block, decision);
  }

  const body = await resp.json();
  if (!body.status) {
    showInert(block, body.error);     // one message covers every refusal
    return;
  }
  replaceCard(body.data.action);      // resolved state, including "failed"
  if (body.data.message_id) appendServerMessage(body.data.message_id);
}
```

The same `resolveAction` works for a card delivered over the WebSocket. Using
REST for every decision is a legitimate simplification — the WebSocket path only
saves a round trip, and it cannot serve step-up cards at all.

---

## Cards can also come from a remote AI client

A third transport can *propose*: an AI client connected over
[MCP](mcp.md) calls the same tools, and a mutating one produces the same
`PendingAction` and the same `approval` block, bound to the operator whose
account authorized that connection. Those cards live in their own conversation
(titled `MCP: <client name>`) and reach the Admin the same way any other card
does.

**Only an interactive session can resolve one.** An MCP token is refused at
`POST /api/assistant/action` with a `401`, so a remote client can never approve
its own proposal — it can only poll its own cards and wait. Nothing changes for
your client: render and resolve them exactly as above.

---

## See also

- [Block Rendering Guide](blocks.md) — every block type, including `approval`
- [Assistant REST + WebSocket reference](README.md)
- [Connecting an AI client over MCP](mcp.md) — the third proposing transport
- [Server-side protocol](../../django_developer/assistant/approvals.md) — the
  gates, the audit trail, and how to declare a mutating tool
