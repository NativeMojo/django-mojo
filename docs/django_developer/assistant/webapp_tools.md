# Assistant WebApp Tools — Django Developer Reference

The `webapp` domain makes the common WebApp lifecycle conversational: create an
app, give it an address, watch it come up, deploy, roll back, take it offline,
delete it. It is loaded on demand with `load_tools(domain="webapp")`.

Every tool is a thin wrapper over the service the matching Admin endpoint
already calls — the same authority, the same fresh-auth window, the same
bounded projection. Nothing here automates the portal, calls local REST, or
makes a workflow decision of its own.

Source: `mojo/apps/assistant/services/tools/webapp/` — `common.py` (authority,
resolution, binding, audit), `reads.py` (12 read tools), `onboarding.py` (3
setup tools), `day2.py` (11 day-2 tools).

## The two-tier authority model

The registry's `permission` check is `user.has_permission()`, which reads
`User.permissions` plus `is_superuser` and nothing else
(`account/models/user.py`). WebApp authority is **group-scoped**
(`account/services/webapp_authority.py`), so a global permission name in the
registry would lock out exactly the operator the module exists to serve.

| Tier | What it is | Where it runs |
|---|---|---|
| `permission="view_admin"` | The registry gate. Same pattern as `add_context`, `query_model`, `get_system_health`. | Listing, dispatch, proposal, execution |
| `authorize=common.authorized` | Does this operator manage apps **anywhere**? `has_global_webapp_authority(user)` or one EXISTS query over their groups for `manage_webapp`/`security`. | Every place `has_permission` runs |
| The per-group check | The exact rule for this workspace. | `preview`, then again in the handler |

`authorize` is deliberately cheap. It is evaluated on every listing, dispatch,
proposal and execution, so it must not be `eligible_webapp_groups` — that
filters every active group through a nine-deep `select_related`, and it is only
called where its result *is* the answer (`list_webapp_groups`).

Three tiers of per-group check:

| Helper | Rule | Used by |
|---|---|---|
| `common.can_view` | `WebApp.RestMeta.VIEW_PERMS` (`view_dns`, `manage_dns`, `security`) on an effectively-active group, resolved through `Group.user_has_permission` | every read |
| `common.can_manage_onboarding` | `webapp_authority.can_manage_group_webapps` — group `security`, **or** `manage_webapp` AND `manage_dns` | the three setup tools |
| `common.can_manage` | the above **AND** `manage_webapp` held globally or in the group | the eleven day-2 tools |

The day-2 tier is stricter on purpose. Every day-2 endpoint carries
`requires_perms("manage_webapp")` on top of its object check, and
`implied_perms` does not expand it — so a member holding only group `security`
is refused by the portal and must be refused here.

## Refusal style

| Tool kind | On refusal |
|---|---|
| Read | returns `{"error": "<sentence>"}` — never raises |
| Mutating `preview` | **raises**, which the approval gate turns into an ordinary tool error with **no record** at proposal, and `precondition_failed` at execution |
| Mutating handler | returns `{"error", "error_code"}`, so the service's documented refusal survives onto the card and into `PendingAction.failure_code` |

A preview must raise a builtin `ValueError`/`PermissionError`: those are the
only two types whose message reaches the model (`approvals.run_preview`), and
`mojo.errors` exceptions are not builtins. `common.translated()` converts at
the boundary, and `common.Refused` / `common.Denied` subclass the builtins.

Refusals are non-oracular. A missing row and an unauthorized row get the same
sentence, so no tool call can be used to probe which ids exist:

```python
NO_WEBAPP = "No app with that id is available to you."
```

## Read tools

| Tool | Input | Wraps | Withholds |
|---|---|---|---|
| `list_webapp_groups` | — | `webapp_authority.eligible_webapp_groups` | capped at 50; manage-level workspaces only |
| `get_webapp_setup_options` | `group` | `webapp_onboarding.options` | — (these are the server-owned choices, verbatim) |
| `precheck_new_webapp_address` | `group`, `url` | `webapp_onboarding.precheck` | `purchase_available`, `godaddy_available` |
| `list_webapps` | `group?`, `environment?` | the `webapp/summaries` projection, rebuilt without `request.group` | capped at 50 with `truncated` |
| `get_webapp` | `webapp` | `webapp_onboarding.summary_for` | already secret-free; `deployment_key` is `{linked, active}` |
| `get_webapp_serving` | `webapp` | `webapp_serving.serving_for` + `webapp_alias.status_rows` | `pools`/`upstreams` stay `null` for a non-writer |
| `preview_webapp_alias` | `webapp`, `hostname` | `webapp_alias.preview` | occupancy; carries the **write** authority, as its twin does |
| `check_webapp_health` | `webapp` | `public_probe.probe_https_root` | the raw probe exception |
| `get_webapp_deploy_history` | `webapp` | `WebAppRelease` `basic` (25) + `WebAppDeployment` `list` (15) | `targets` / `rollback_targets` are outside both graphs |
| `get_webapp_deployment` | `deployment` | `webapp_deploy.payload` | runner ids always; per-node detail and errors from a viewer |
| `get_webapp_deploy_setup` | `webapp` | `webapp_keys.status` + `webapp_onboarding.workflow(web_app, None)` | any token; the api-key id |
| `get_webapp_setup_status` | `operation_id` | `webapp_onboarding.serialize` behind `assert_read_authority` | already `_safe()`-redacted, 32 KB bound |

### Deployment evidence is partitioned by write authority

Mirroring `serving_for(include_editables=...)`:

* **Everyone** gets node counts — `expected` / `completed` / `failed` / `pending`.
* **A writer** additionally gets per-node `status`, `changed`, `generation`, and
  at most **five** error strings truncated to 200 characters.
* **Nobody** gets runner ids.

`webapp_deploy.payload`'s raw 2000-character `error` is job stderr by another
name, and the runner id is fleet inventory. The model needs to know how many
nodes failed, never which host they were.

### `get_webapp_deploy_setup` is a deliberate non-escalation

Its twin `POST webapp/onboarding/workflow` carries `requires_fresh_auth(600)`
**because it can also mint a key** when an `action` is passed. This tool never
passes one, so `webapp_keys.link_once` is unreachable from it, and
`webapp/key_status` is already step-up-free. No step-up is therefore declared.
A future edit must not "fix" this by making the tool mutating — a test asserts
that no module in the package contains a `link_once(` call.

## Mutating tools

Every one declares `mutates=True`, a `summarize`, a `preview`, and the
fresh-auth window its Admin twin declares.

| Tool | Twin | `fresh_auth_seconds` | bound `revision` |
|---|---|---|---|
| `start_webapp_setup` | `POST webapp/onboarding/create` | — (the endpoint has none) | `group:<id>\|slug:<slug>\|env:<env>\|bucket:<bucket>` |
| `answer_webapp_setup_step` | `POST webapp/onboarding/choose` | 600 | `op:<uuid>\|rev:<revision>\|cursor:<cursor>\|choice:<sha256>` |
| `cancel_webapp_setup` | `POST webapp/onboarding/cancel` | 600 | `op:<uuid>\|status:<status>` |
| `attach_webapp_address` | `POST webapp/attach_domain` | 600 | `app:<id>\|host:<normalized>\|preview:<status>` |
| `detach_webapp_address` | `POST webapp/detach_domain` | 600 | `app:<id>\|vhost:<id>\|host:<server_name>` |
| `take_webapp_offline` | `POST webapp/detach_address` | 600 | `app:<id>\|vhost:<id>\|host:<name>\|aliases:<n>` |
| `set_webapp_serving` | `POST webapp/serving` | 600 | `app:<id>\|pool:<old>-><new>\|spa:<old>-><new>` |
| `switch_webapp_certificate` | `POST webapp/serving` (certificate) | 600 | `app:<id>\|cert:<id>\|status:<status>\|covers:<hostname>` |
| `request_webapp_certificate` | `POST webapp/certificate` | 600 | `app:<id>\|host:<hostname>\|domain:<id>` |
| `add_webapp_route` | `POST webapp/add_route` | 600 | `app:<id>\|prefix:<clean>\|upstream:<id>` |
| `remove_webapp_route` | `POST webapp/remove_route` | 600 | `app:<id>\|prefix:<clean>\|upstream:<current id>` |
| `rollback_webapp` | `POST webapp/rollback` | 600 | `app:<id>\|from:<release>\|to:<release>\|version:<v>` |
| `revoke_webapp_deploy_key` | `POST webapp/revoke_key` | 300 | `app:<id>\|key:<api_key_id>` |
| `delete_webapp` | `DELETE edge/webapp/<pk>` | 600 (**escalated**) | `app:<id>\|slug:<slug>\|releases:<n>\|addresses:<n>` |

`delete_webapp` carries a step-up the REST delete does not. Chat is a
model-initiated surface and the criterion demands one; the escalation is
one-directional. (The missing step-up on `DELETE edge/webapp/<pk>` itself is a
separate finding, not fixed here.)

Previews reuse the services' own gates — `_require_primary`,
`_resolve_certificate`, `_require_routes`, `_clean_prefix`,
`_resolve_upstream`, `dedicated_support` — rather than reimplementing them, so
a proposal can never disagree with what approval then does.

### The bound revision is the anti-substitution mechanism

The gate re-runs `preview` before dispatch and refuses when the revision moved.
That is what stops a follow-up model turn substituting another tenant, another
resource, or a stale choice — the card the operator approved names the exact
one.

`answer_webapp_setup_step` binds the **numeric** operation revision and passes
*that* integer to `choose_for_actor` at execution. Reading it live would turn
the service's optimistic-concurrency guard (`webapp_onboarding.choose`) into a
tautology. Because the worker bumps `revision` on every `_save_state`, a moved
revision is the expected case for a card left open: the gate answers
`precondition_failed`, and the assistant re-reads with `get_webapp_setup_status`
and re-proposes the current step — the wait-state loop the wizard already runs.

### The required `reason`

`cancel_webapp_setup`, `detach_webapp_address`, `take_webapp_offline`,
`rollback_webapp`, `revoke_webapp_deploy_key` and `delete_webapp` take a
required `reason` (3–300 characters). No WebApp endpoint accepts a typed
confirmation echo — the portal's `confirmAction({requireReason: true})`
collects a reason and discards it client-side — and a model-typed echo of a
slug proves nothing, because the model can echo anything. The human act is the
approval click; the `reason` is normalized, bound into the approval, rendered
on the card, and written to `logit.Log`. That is strictly more evidence than
the portal keeps today.

## `ASSISTANT_ORIGIN`: why a chat setup and a portal setup never cross

`webapp_onboarding.request_origin()` only ever returns a `scheme://host`
string, so the module constant `ASSISTANT_ORIGIN = "assistant"` can never equal
one. A browser therefore cannot continue an assistant-created operation, and
the assistant cannot continue a browser-created one. Each surface owns what it
starts; neither impersonates the other, and the refusal says where to go.

The seams that make this work, all in `edge/services/webapp_onboarding.py`:

| Function | Contract |
|---|---|
| `assert_read_authority(operation, actor)` | actor identity, effectively-active group, live authority. **No origin comparison.** |
| `assert_continue_authority(operation, actor, origin, has_group_token=False)` | the full contract with a caller-supplied origin |
| `choose_for_actor(operation, actor, origin, payload, ...)` | `choose` without an HTTP request; refuses a purchase under `ASSISTANT_ORIGIN` **before the row lock** |
| `cancel_for_actor(operation, actor, origin, ...)` | `cancel` without an HTTP request |

`_assert_current` still runs the browser path, with its five checks in their
original order — `request_origin` stays in position 5, because it refuses a
malformed or cross-origin Origin itself and resolving it earlier would change
which refusal an unauthorized cross-origin caller sees. A parity test asserts
the exact message.

`assert_read_authority` is a **deliberate relaxation**. Today
`webapp/onboarding/detail` is origin-bound; this item lets the same
administrator *read* (never continue) an operation they started on the other
surface, because reporting on a setup is not continuing it.

## What is excluded, and why

| Excluded | Reason |
|---|---|
| Minting or rotating a deploy key | `webapp_keys.link_once` returns the token once and clears the encrypted copy before commit; a replay returns `token: None`. Routing it through an approval would put the credential in `PendingAction.result` and in the server-authored history message the model reads next turn — or, if suppressed, mint a key nobody can ever see. Chat explains setup, returns the workflow file, and can revoke. |
| Buying a domain | The purchase rides a single-use `confirm_token` — the app's only real-money mutation. It cannot be a tool argument, and `choose_for_actor` refuses it at the **service** under `ASSISTANT_ORIGIN`, not merely in the tool. |
| Uploading a local build | The register → presigned PUT → complete flow is not proxied through the LLM. `get_webapp_deploy_setup` returns the handoff wording. |
| `group_intent=new` | Creating an `account.Group` as a side effect makes "the exact group" unbindable at proposal time — the group does not exist yet. `start_webapp_setup` requires a concrete, already-eligible `group`. |
| Verify / retry / converge | Those are platform-fleet verbs, not WebApp verbs. A WebApp's only recovery verb is rollback. |

## Limitations

### `take_webapp_offline` does not stop an API-backed address

`webapp_lifecycle.take_offline` deletes the primary vhost only when its kind is
`site`. A `site_api` primary is unlinked from the app, but its enabled `Vhost`
row survives — and desired state selects every enabled vhost in the pool, so
nodes keep rendering it and the address goes on answering from its upstream
routes. Only the extra (alias) addresses stop.

That is the REST detach handler's long-standing behaviour, lifted verbatim into
the shared service; changing it belongs in its own item. What this domain does
is refuse to overclaim about it: `_preview_offline` branches on the kind, binds
`kind:<kind>` into the approval revision, sets
`details.address_stops_serving`, and the card says the address "KEEPS serving
its upstream routes until they are removed". The tool description and the
execution result say the same. To actually stop an API-backed address, remove
its routes (`remove_webapp_route`) or delete the app.

## Context links

`_extract_context_refs` keys on the tool name (`agent.py`), so a tool cannot
inject a `context` block; and the client builds
`/api/{app}/{model_lowercased}/{pk}`, which `WebAppRelease` and
`WebAppDeployment` do not satisfy. Read tools therefore return a `context_ref`
hint dict — only ever `edge.WebApp` or `dnsman.Domain` — that the model passes
straight to `add_context`. No tool builds a URL or a portal hash route; a
handoff names the surface in words ("Admin → Deployments → the app's Key tab").

## Audit

The approval gate already files `assistant:approval:*` and
`assistant:tool:<name>`. Each mutating handler additionally writes one
`logit.Log` row:

| Field | Value |
|---|---|
| `kind` | `assistant:webapp:<tool_name>` |
| `model_name` | `edge.WebApp` |
| `model_id` | the app's pk |
| `payload` | `{group_id?, operation?, reason?, bound}` — ids and the reason only |

Argument values other than ids and the reason are never written. No new
incident category is introduced.

## Tests

| File | Tier | Covers |
|---|---|---|
| `tests/test_edge/31_assistant_webapp_tools.py` | default core, serial | registration, `authorize`, tenant scoping, non-oracular refusals, read projections |
| `tests/test_edge/32_assistant_webapp_mutations.py` | default core, serial | every `preview`'s refusals, the exact bound revisions, the two idempotent executions |
| `tests/test_assistant_extended_serial/40_test_webapp_provider_paths.py` | `extended`, serial | external DNS, foreign CNAME, ambiguous provider, pending certificate, health, failed fleet deployment |

The ORM-fixture modules live in `tests/test_edge/` rather than
`tests/test_assistant/` because `declare_pools` / `declare_release_buckets`
write `EDGE_*` settings — a protected prefix the isolation scanner refuses in a
parallel default-tier package. `test_edge` is already `serial: True` and
already owns those fixtures.
