# Approvals — the boundary in front of every mutating assistant tool

`mutates=True` is no longer a hint to the model. It is an enforced contract:

> A mutating tool cannot run until **this exact operator** approves **this exact
> argument set**, over an **authenticated transport**, within a **bounded
> window**, **once**.

A tool call from the model is a *proposal*. The server stores a `PendingAction`,
renders an operator-facing card, and returns a tool result that says plainly that
nothing happened. Only `approvals.resolve()` — driven by a human decision arriving
over REST or the WebSocket — ever calls a mutating handler.

- Service: `mojo/apps/assistant/services/approvals.py`
- Record: `mojo/apps/assistant/models/pending_action.py`
- Gate: `_execute_tool()` in `mojo/apps/assistant/services/agent.py`
- Client-facing contract: [web_developer/assistant/approvals.md](../../web_developer/assistant/approvals.md)

---

## Why the gate is where it is

`_execute_tool()` is the single dispatch function both agent loops
(`run_assistant` and `run_assistant_ws`) funnel through, including parallel tool
batches and parallel plan steps. Putting the branch there — after the permission
check, before `_call_handler` — means there is exactly one place a mutating tool
could reach its handler, and it does not.

Consequences worth stating explicitly, because they are the point:

- **No opt-out flag exists.** A per-tool exemption is the first thing a future
  tool author would reach for, and it would undo the guarantee. `write_memory`,
  `delete_memory` and the three skill writers are gated too: a global memory is a
  standing instruction and a skill is a stored program, so a model that can
  rewrite either without an operator is the prompt-injection amplifier this
  boundary exists to close. `export_data` is gated because writing arbitrary
  model rows to a downloadable URL is exfiltration.
- **Prompt wording cannot bypass it.** Neither can a skill marked AUTO-EXECUTE,
  nor a multi-step plan. Parallel plan steps refuse mutating tools outright
  (`_execute_parallel_plan_steps`), and every other route lands in the gate.
- **The model cannot forge a card.** `approval` is deliberately absent from
  `agent.VALID_BLOCK_TYPES`, so a model-emitted ` ```assistant_block ` claiming
  `type: "approval"` is dropped by `_validate_block()`.

### MCP is a third PROPOSING transport, and it can never resolve

`mojo/apps/assistant/mcp/server.py` dispatches a remote AI client's `tools/call`
through the same `_execute_tool`, so a mutating tool called over MCP produces an
ordinary `PendingAction` — bound to the calling operator, in that grant's own
conversation — and returns `approvals.proposal_result()` as the tool result.
Nothing runs. That is the whole reason remote access is safe to offer: the
external model proposes, and the operator still resolves in the Admin.

**Resolution stays REST/WS with an interactive session.** An MCP access token's
audience is the MCP path and nothing else, so presenting it at
`POST /api/assistant/action` is a 401 at the framework chokepoint — there is no
code path that lets a remote client approve its own proposal. The client can
only *watch*, through the two MCP-only read tools (`list_pending_actions`,
`get_pending_action`), and only for cards it proposed itself: both are scoped to
its conversation, because `render_block` ships redacted `args` and up to 8 KB of
`preview`. See [MCP transport](mcp.md).

---

## The state machine

```
                 propose()
                     │
                     ▼
                 ┌────────┐  approve + every gate passes   ┌───────────┐
                 │pending │ ─────────────────────────────► │ executing │
                 └────────┘                                └───────────┘
                  │  │  │                                    │       │
     cancel ──────┘  │  └────── a newer proposal with the    │       │
       │             │          same fingerprint arrives     │       │
       ▼             ▼                    │                  ▼       ▼
  ┌──────────┐  ┌─────────┐        ┌────────────┐     ┌──────────┐ ┌────────┐
  │ canceled │  │ expired │        │ superseded │     │completed │ │ failed │
  └──────────┘  └─────────┘        └────────────┘     └──────────┘ └────────┘
```

`expired` and the unknown-outcome form of `failed` are decided **lazily** by
`PendingAction.effective_state()`, not by the sweep. A `pending` row past
`expires_at` already reads as expired; an `executing` row older than
`EXECUTING_TIMEOUT_SECONDS` (900) already reads as failed. The atomic claim
carries the same expiry predicate, which is what stops the sweep from ever
racing a live resolution.

`executing` is terminal for the operator. A process that dies mid-handler leaves
a row that is never re-executable — the sweep marks it `failed` with
`failure_code="unknown_outcome"`. That is the same reconcile-don't-retry contract
the ambiguous infrastructure writes already carry; nothing is retried
automatically.

### `PendingAction` fields

| Field | Purpose |
|---|---|
| `uuid` | The only identifier that leaves the server. The sequential pk is never exposed. |
| `user`, `conversation`, `group` | The binding, snapshotted at proposal. |
| `tool_name`, `permission` | What was proposed, and under which grant. |
| `args` | The **normalized** argument set — the only thing execution reads. |
| `args_fingerprint` | `sha256` over the canonical JSON of `{tool, redact(args)}` — the **masked** form, never the raw values. |
| `summary`, `preview` | Redacted, operator-facing card content. |
| `fresh_auth_seconds`, `requires_superuser`, `requires_managed_infrastructure` | Gate snapshot, for audit and rendering. |
| `revision` | A `preview`'s bound revision, re-checked before execution. |
| `state`, `expires_at`, `resolved_at` | Lifecycle. |
| `result`, `failure_code` | Bounded, redacted outcome. |

**The snapshot never outranks the registry — but it can only make a gate
stricter, never looser.** Execution re-reads `get_registry()[tool_name]` and
re-checks every gate against it; for `requires_superuser`,
`requires_managed_infrastructure` and `fresh_auth_seconds` the live entry is
combined with the snapshot (OR, and the shorter window), so removing a gate from
the registry cannot silently un-gate a pending card that already told the
operator it would ask. A tool that was
unregistered, or whose `mutates` flag was removed, resolves to the generic
failure; a tool whose permission changed is checked against the *live* value.
Trusting the snapshot would let a downgrade at deploy time execute under
yesterday's rules.

`RestMeta.NO_REST = True` is load-bearing. No generic CRUD plane may write
`state`.

---

## Declaring a mutating tool

All six arguments are accepted by both `register_tool()` and `@tool(...)`.

| Argument | Meaning |
|---|---|
| `fresh_auth_seconds=None` | Recency window in seconds; mirrors `@md.requires_fresh_auth(seconds=N)` on the matching Admin endpoint. When set, the action can be resolved **over REST only**. |
| `requires_superuser=False` | AND-check for a live literal `User.is_superuser`, mirroring the hand-written superuser checks in the Admin REST layer (`aws/rest/capacity.py:79`). Enforced inside `user_can_use_tool`, so a non-superuser never sees the tool listed, never has the model call it, and never receives a card they could not approve. |
| `requires_managed_infrastructure=False` | Tool is hidden from the model and refused at proposal and execution when `infrastructure.is_external()`. |
| `summarize=None` | `(params, user) -> str`. One operator-facing sentence for the card. Must contain no secret. Default: `"<tool_name> will run with the arguments below."` |
| `preview=None` | `(params, user) -> {"summary": str, "details": <json>, "revision": str}`. Read-only. See below. |
| `authorize=None` | `(user) -> bool`, allowed on **any** tool, mutating or not. See below. |

**Passing any of the first five without `mutates=True` raises `ValueError` at
import time.** A misdeclared tool breaks the import rather than degrading
silently. `fresh_auth_seconds` must be a positive `int` or `None`;
`summarize`/`preview`/`authorize` must be callable or `None`.

### `authorize(user)`

`user.has_permission()` is global-only. `authorize` is the ADDITIONAL gate for
rules it cannot express — "holds WebApp authority in at least one effectively
active group", or "`manage_aws` AND any of `manage_platform`/`admin`/superuser".

- It is evaluated everywhere `has_permission` is: the four listing functions,
  `_execute_tool`, proposal, and execution against the **re-read** `User`.
- A `False` result is indistinguishable from a missing permission — same tool
  error, same `assistant:permission_denied` event.
- It is never a substitute for `permission`, which stays required.
- An `authorize` that raises is treated as a refusal. A broken predicate must not
  open a tool.

```python
def _can_manage_fleet(user):
    return user.has_permission("manage_platform") or user.is_superuser


@tool(name="scale_fleet", domain="fleet", permission="manage_aws",
      description="...", input_schema={...},
      mutates=True, requires_superuser=True, requires_managed_infrastructure=True,
      fresh_auth_seconds=600, authorize=_can_manage_fleet,
      summarize=_summarize_scale, preview=_preview_scale)
def _tool_scale_fleet(params, user, *, approval=None):
    return capacity.apply(params["pool"], params["delta"],
                          idempotency_key=str(approval.uuid))
```

### `preview(params, user)`

Read-only. Called at proposal time; its `details` render on the card and its
`revision` is bound into the record. Called again at execution: if the revision
moved, the resolution refuses with `precondition_failed` — the same rule as
"this installation is offering X, reload and try again".

**A raising `preview` is a refusal, not a crash.** It is the supported place for
a per-object or per-group authority check to fail closed:

| When it raises | Outcome |
|---|---|
| At proposal | Ordinary tool error `{"error": "<safe message>"}`. **No record, no card.** |
| At execution | `ApprovalRefused("precondition_failed")`. |

A `ValueError` or `PermissionError` has its message forwarded to the model
(bounded and masked), so tool authors keep those messages **non-oracular** — say
"that is not available", not "fleet 7 belongs to another tenant". Any other
exception type is reported generically and logged.

### What the handler is called with

At execution the handler is invoked exactly as it is today —
`handler(args, user)` — with `request_meta`, `conversation` and the new
`approval` passed **only** when the handler's signature declares them.

| | |
|---|---|
| `user` | The freshly re-read **active** `User` row — never the object the socket authenticated with hours ago. |
| `args` | `PendingAction.args`. Never anything the client or the model sent at approve time. |
| `approval.uuid` | Use `str(approval.uuid)` as the idempotency key for the underlying service — `webapp_keys.link_once` and `deploy.request_deploy` already accept one. |
| `approval.revision` | The bound `preview` revision. |
| `approval.conversation`, `approval.group` | The binding. |

**A handler that returns a dict with an `"error"` key has failed.** The row lands
at `state="failed"` with `failure_code` taken from the dict's `error_code` when
present (bounded to 32 chars, `[a-z_]` only), else `"handler_error"`, and the
card shows the safe `error` text. A wrapped service's documented refusal
(`capacity_revision_stale`, `deploy_coordination_unavailable`, …) therefore
survives into the approval record instead of being flattened. Anything else the
handler returns is redacted, capped, and stored in `PendingAction.result`.

---

## Argument normalization

`normalize_args(input_schema, raw)` is hand-rolled and small — the repo carries
no JSON-Schema dependency and this does not justify adding one.

- A non-dict argument set is rejected.
- Keys absent from `properties` are **dropped**, not rejected — otherwise a
  handler reading `params.get("...")` would honour a field the operator never saw
  on the card.
- A missing `required` key is rejected.
- Declared JSON types are checked, never coerced. A `bool` is not an `integer`.
- `enum` is enforced where declared.
- The canonical serialization is capped at 16 KB.

A rejection is an ordinary tool error, so the model can correct itself. Nothing
is stored.

The **fingerprint** is evidence, not a key. Resolution finds the row by `uuid`
plus the bound user and conversation; the fingerprint is then recomputed from the
stored `args` and compared, which catches a tampered row and gives audit a
stable, secret-free identifier for "the same operation".

It is hashed over the **redacted** arguments. The digest reaches an incident
event and a `logit.Log` payload, and an unsalted SHA-256 over raw arguments is
reversible for a low-entropy value — a reader of either plane sees the argument
*names* on the same line, so a six-digit `onetime_code` is a short offline
search. Redaction happens inside `fingerprint()` so the value stored at proposal
and the value recomputed at resolution cannot drift apart. The trade: two
proposals differing only in a masked value dedupe to one card, which is right —
the operator cannot tell them apart either, and the stored `args` remain the sole
authority for what runs.

### Dedupe, supersession, and the per-conversation cap

- A proposal whose `(conversation, tool_name, args_fingerprint)` already has a
  **live** `pending` row returns that row. That is what "the model called the same
  tool twice" actually is.
- Otherwise the row is inserted **first**, then one UPDATE marks older `pending`
  rows with the same triple and `pk__lt=<new pk>` as `superseded`.
  Insert-then-supersede is what stops the concurrent proposals of the tool thread
  pool from annihilating each other.
- Different argument sets for the same tool coexist: "block these five IPs" is
  five cards.
- A per-conversation cap of 20 live `pending` rows bounds growth inside one
  25-turn loop; the oldest is superseded when a 21st arrives.

---

## Resolution: the gates, in order

`approvals.resolve(user, action_id, decision, request=None, conversation_id=None)`

1. **Load** by `uuid` + bound `user` (+ `conversation` when supplied).
2. **Lazy state** must be `pending`.
3. **Fingerprint** must match the stored `args`.
4. **Live registry**: the tool must still be registered and still `mutates`.
5. `cancel` stops here — terminal, no dispatch.
6. **Infrastructure mode** — answered *before* the caller's grants, so the
   refusal is about the installation, not about who is asking.
7. **Live actor** — `User.objects.filter(pk=…, is_active=True)`.
8. **Literal superuser**, when the live entry **or the row snapshot** declares it.
9. **Permission + `authorize`** against that live actor.
10. **Group activity** — `group.is_effectively_active()`.
11. **Fresh auth**, when the live entry **or the row snapshot** declares it —
    the stricter (shorter) of the two windows (see below).
12. **Bound revision** — `preview` re-run and compared.
13. **Atomic claim** — `filter(pk=…, state="pending", expires_at__gt=now).update(state="executing", modified=now)`.
14. **Dispatch**, then persist the outcome and write the message.

### Why the claim is a conditional UPDATE

`select_for_update` is the repo's usual pattern, but it would hold a database
lock across a handler that may call a cloud provider. The conditional UPDATE
returning 1 **is** the claim; returning 0 means someone else already claimed it,
and the caller re-reads the row and returns *that* outcome. Two tabs, a
double-click, or a retried request therefore get the same terminal answer, and
the handler runs once.

The `expires_at__gt=now` predicate rides along so the atomic claim and the lazy
`effective_state()` can never disagree.

### Fresh auth, and why it is never delegated

`auth_time` is a claim on the access token, so the REST endpoint proves recency
from the `Authorization` header with zero new transport.

The WebSocket authenticates once at connect and holds no per-message token, so it
**cannot** prove recency — and putting a token in a message body is exactly what
must not happen. A WS approval of an action with `fresh_auth_seconds` set is
refused with `reauth_required`; the client re-authenticates and re-submits over
REST.

The service refuses **before** `fresh_auth.is_fresh` is consulted whenever
`request is None` or `getattr(request, "bearer", None) != "bearer"`. Both of
those cases return `True` from `is_fresh` **by design** (machine credentials have
no interactive login to be recent), so delegating them would be the bypass.

`require_fresh` is called with the tool's declared window, which means the
`X-Mojo-Test-Fresh-Auth-Window` header is inert here — `resolve_window` consults
it only when `seconds is None`.

### Execution never calls the LLM

On approve, the service runs the handler and writes a **server-authored**
assistant `Message`: one line of text plus the resolved approval block. The model
sees it in history on its next turn, so the conversation stays coherent — but the
execution report is authored by the server, not paraphrased by a model with an
incentive to say it went well. It also keeps the security path free of API-key,
rate-limit and token-cost failure modes.

The consequence is deliberate and documented rather than designed around: **a
multi-step skill or plan pauses at the approval card.** The outcome lands in
history and the operator's next message resumes the procedure. There is no
automatic resumption.

---

## The one non-oracular failure

`resolve()` raises `ApprovalRefused(code)` — transport-neutral, because the
WebSocket dispatcher shares this code and `infrastructure.refuse()` returns a
`JsonResponse`. Each transport maps the code once.

| `ApprovalRefused.code` | REST | WebSocket |
|---|---|---|
| `action_unavailable` | `409`, `error_code: "action_unavailable"` | `assistant_error`, `code` |
| `reauth_required` | `440` via `merrors.ReauthRequiredException` | `assistant_error`, `code`, `action_id` |
| `permission_denied` | `403` | `assistant_error`, `code` |
| `infrastructure_external` | `403`, `error_code: "infrastructure_external"` | `assistant_error`, `code` |
| `precondition_failed` | `409` | `assistant_error`, `code` |

**Every** unresolvable case — unknown id, wrong user, wrong conversation, wrong
tool, expired, already used, canceled, superseded, tool no longer registered,
tool no longer mutating, fingerprint mismatch, malformed id, bad decision —
returns the identical `action_unavailable` body. Distinguishable outcomes exist
**only** for the bound owner of a live record, so a stolen id learns nothing.

A handler that ran and failed is **not** an HTTP error. It is `200` with
`state: "failed"`, because the mutation was attempted and the operator has to be
told so.

---

## Audit vocabulary

| incident category | level | when |
|---|---|---|
| `assistant:approval:proposed` | 4 | pending action created |
| `assistant:approval:approved` | 5 | consumed; execution starting |
| `assistant:approval:canceled` | 4 | operator declined |
| `assistant:approval:denied` | 6 | resolution refused — filed with `report_event_suppressed`, key `<user_id>:<failure_code>` plus a budget, because the unknown-id case has no bound tool and an id-guessing loop must not mint unbounded keys |
| `assistant:approval:failed` | 6 | handler raised or returned an error |
| `assistant:tool:<name>` | 5 | success — **unchanged**, so existing RuleSets keep firing |

Each lifecycle point also writes one `logit.Log` row:
`kind="assistant:approval:<state>"`, `model_name="assistant.PendingAction"`,
`model_id=<pk>`, with a payload carrying `conversation_id`, `tool`,
`args_fingerprint` and `decision`. The message carries argument **names** only.

The proposal path passes the synthetic request from `_build_request(user, …)`
rather than `None`: `Log.logit(None, …)` writes `uid=0`, which would lose the
actor on exactly the rows where it matters most.

**No argument value, token, or credential is written to either system.** Card
`args` and stored `result`s go through `logit.sanitize_dict()` plus an additional
mask for keys containing `password`, `secret`, `token`, `auth_key` or
`onetime_code`. The stored `args` keep real values — execution needs them — and
are never rendered.

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `LLM_ADMIN_APPROVAL_TTL` | `600` | Approval window in seconds. Clamped to 60–3600. |

`PendingAction.EXECUTING_TIMEOUT_SECONDS` (900) is a module constant, not a
setting — a second knob there buys nothing.

The nightly `assistant_approval_sweep` job (03:15, `cleanup` channel) persists
lapsed `pending` → `expired` and stalled `executing` → `failed/unknown_outcome`,
and deletes terminal rows older than 30 days. Correctness never depends on it;
`effective_state()` already answers.

---

## Built-in mutating tools and their Admin twins

59 built-in tools declare `mutates=True`. Every one is gated. The extra gates
mirror whatever the visual Admin endpoint that performs the same operation
requires — so a tool is never easier to reach through chat than through the
portal.

| Tool(s) | Domain | Permission | Admin twin | Extra gates |
|---|---|---|---|---|
| `disable_user`, `enable_user`, `force_logout`, `update_user_permission` | users | `manage_users` | `account/rest/admin_people.py` (`denies_key_backed_session` + `requires_fresh_auth(seconds=600)`) | `fresh_auth_seconds=600` |
| `block_ip`, `unblock_ip`, `whitelist_ip`, `unwhitelist_ip` | security | `manage_security` | `incident/rest/ipset.py` — RestMeta CRUD | — |
| `update_incident`, `bulk_update_incidents`, `merge_incidents` | security | `manage_security` | `incident/rest/event.py` — RestMeta CRUD | — |
| `create_ticket`, `update_ticket`, `add_ticket_note` | security | `manage_security` | `incident/rest/ticket.py` — RestMeta CRUD | — |
| `create_rule`, `add_rule_condition`, `update_ruleset`, `delete_ruleset`, `delete_rule` | security | `manage_security` | `incident/rest/event.py` ruleset/rule — RestMeta CRUD | — |
| `cancel_job`, `retry_job`, `run_job`, `run_scheduled_task_now` | jobs | `manage_jobs` | `jobs/rest/control.py` — `requires_global_perms('manage_jobs','jobs')` | — |
| `create_scheduled_task`, `update_scheduled_task`, `delete_scheduled_task` | jobs | `manage_jobs` | `jobs/rest/scheduled_task.py` — RestMeta CRUD | — |
| `create_group`, `invite_to_group` | groups | `manage_groups` | `account/rest/group.py` — RestMeta CRUD + `group/member/invite` | — |
| `save_model_instance`, `delete_model_instance`, `export_data` | models | `view_admin` | generic RestMeta CRUD; no single twin | — |
| `write_memory`, `delete_memory` | memory | `assistant` | `assistant/rest/memory.py` — `requires_perms('assistant')` | — |
| `save_skill`, `update_skill`, `delete_skill` | skills | `assistant` | `assistant/rest/assistant.py` skill CRUD | — |
| `set_metric_gauge` | metrics | `write_metrics` | `metrics/rest/values.py` `value/set` | — |
| `send_notification` | comms | `comms` | `account/rest/notification.py` — RestMeta CRUD | — |
| `retry_platform_deployment`, `verify_platform_deployment`, `converge_platform_deployment` | cloud | `manage_platform`, `admin` | `account/rest/admin_platform.py` deploy retry/verify/converge (`denies_key_backed_session` + `requires_fresh_auth(600)`; **no** `infrastructure.refuse()`) | `fresh_auth_seconds=600`, `summarize`, `preview` |
| `apply_framework_update` | cloud | `manage_platform`, `admin` | `account/rest/admin_platform.py` framework update (same, **plus** `infrastructure.refuse()`) | `fresh_auth_seconds=600`, `requires_managed_infrastructure`, `summarize`, `preview` |
| `apply_managed_upgrade` | cloud | `manage_aws` | `aws/rest/maintenance.py` apply (same, plus `_require_manage_tier`) | `fresh_auth_seconds=600`, `requires_managed_infrastructure`, `authorize` (superuser OR `manage_platform` OR `admin`), `summarize`, `preview` |
| `apply_capacity_change`, `apply_capacity_plan` | cloud | `manage_aws` | `aws/rest/capacity.py` apply / plan / plan-apply (same, plus `_require_superuser`) | `fresh_auth_seconds=600`, `requires_superuser`, `requires_managed_infrastructure`, `authorize` (literal superuser), `summarize`, `preview` |
| `start_webapp_setup` | webapp | `view_admin` + `authorize` | `edge/rest/webapp_onboarding.py` `onboarding/create` (no step-up) | group WebApp authority in `preview` and the handler |
| `answer_webapp_setup_step`, `cancel_webapp_setup` | webapp | `view_admin` + `authorize` | `edge/rest/webapp_onboarding.py` `onboarding/choose` / `onboarding/cancel` (`requires_fresh_auth(600)`) | `fresh_auth_seconds=600`; bound operation revision |
| `attach_webapp_address`, `detach_webapp_address`, `take_webapp_offline`, `set_webapp_serving`, `switch_webapp_certificate`, `request_webapp_certificate`, `add_webapp_route`, `remove_webapp_route`, `rollback_webapp` | webapp | `view_admin` + `authorize` | `edge/rest/web_app.py` (`denies_key_backed_session` + `requires_fresh_auth(600)` + `requires_perms('manage_webapp')`) | `fresh_auth_seconds=600`; `manage_webapp` re-checked per group |
| `revoke_webapp_deploy_key` | webapp | `view_admin` + `authorize` | `edge/rest/web_app.py` `webapp/revoke_key` (`requires_fresh_auth(300)`) | `fresh_auth_seconds=300`; `str(approval.uuid)` is the service's idempotency key |
| `delete_webapp` | webapp | `view_admin` + `authorize` | `DELETE edge/webapp/<pk>` (no step-up) | `fresh_auth_seconds=600` — a deliberate **escalation** over the twin |

The `cloud` domain (item #2570) is the first real user of `requires_superuser`
and `requires_managed_infrastructure`; see
[cloud_tools.md](cloud_tools.md). Note where the gates are **absent**: the three
deploy endpoints do not call `infrastructure.refuse()`, so those three tools
stay available under external infrastructure mode, exactly as the Admin
controls do. Mirroring means copying the twin, not tightening it.

The `webapp` domain is the first to use `authorize=` in earnest, because WebApp
authority is group-scoped and the registry's `permission` check is global-only.
It is also the first to bind a live service revision into `preview.revision` —
see [webapp_tools.md](webapp_tools.md).

**Adding a mutating tool?** Find the Admin endpoint that performs the same
operation, read its decorators, and mirror them. An omission here is the whole
failure mode this table exists to make visible.

---

## Key files

- `mojo/apps/assistant/services/approvals.py` — the protocol
- `mojo/apps/assistant/models/pending_action.py` — the record and `effective_state()`
- `mojo/apps/assistant/services/agent.py` — the gate in `_execute_tool`, `approval` kwarg in `_call_handler`, prompt corrections
- `mojo/apps/assistant/rest/assistant.py` — `POST`/`GET /api/assistant/action`
- `mojo/apps/assistant/handler.py` — `assistant_approval` over the socket
- `mojo/apps/realtime/handler.py` — the server-stamped `_bearer` on every delivered message
- `mojo/apps/assistant/jobs.py`, `cronjobs.py` — the sweep
- `tests/test_assistant/37_test_approval_gate.py`, `38_test_approval_rest.py`
- `tests/test_assistant_extended_serial/39_test_approval_ws.py`, `37_test_approval_infrastructure.py`
