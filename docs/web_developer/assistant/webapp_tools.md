# Assistant WebApp Tools — REST/Client Reference

What a client sees when an operator runs WebApp work through the assistant:
which tools exist, what their results look like, which blocks to render them
as, and what each approval card binds.

Load the domain with `load_tools(domain="webapp")`. It appears in
`GET /api/assistant/domains` only for operators who hold WebApp authority in at
least one workspace — a global `view_admin` user with no WebApp grant anywhere
will never see it.

## Read tools

| Tool | Arguments | Render as |
|---|---|---|
| `list_webapp_groups` | — | `list` of workspaces |
| `get_webapp_setup_options` | `group` | `list` per choice group; `alert` for `destination_error` / `apps_domain_error` |
| `precheck_new_webapp_address` | `group`, `url` | `alert` for the verdict; `table` for `records` |
| `list_webapps` | `group?`, `environment?` | `table` (slug, address, version, last deploy) + `stat` row from `fleet` |
| `get_webapp` | `webapp` | `stat` row + `table` of addresses |
| `get_webapp_serving` | `webapp` | `table` of routes and addresses; `stat` for certificate |
| `preview_webapp_alias` | `webapp`, `hostname` | `alert` |
| `check_webapp_health` | `webapp` | `stat` (`healthy` / `unhealthy` / `not_configured`) |
| `get_webapp_deploy_history` | `webapp` | two `table`s: versions, deployments |
| `get_webapp_deployment` | `deployment` | `progress` from `nodes`, `alert` on failure |
| `get_webapp_deploy_setup` | `webapp` | `file`-style block for `workflow.yaml`; `stat` for the key |
| `get_webapp_setup_status` | `operation_id` | `progress` from `cursor`/`status`; `table` for `evidence.address.records` |

### `context_ref` → `add_context`

Every read returns a `context_ref` hint:

```json
{"app_name": "edge", "model_name": "WebApp", "pk": 42, "label": "storefront"}
```

The model passes it to `add_context`, which is the only tool that can produce a
`context` block. Only `edge.WebApp` and `dnsman.Domain` refs are ever emitted:
the client builds `/api/{app}/{model_lowercased}/{pk}`, and releases and
deployments do not serve at those paths — a ref to either would render a dead
link. Tools never build a URL themselves.

### Things the result deliberately does not contain

* No deploy key, ever. `get_webapp_deploy_setup` returns
  `key: {linked, active, created, last_used, last_action}` and the workflow
  yaml, whose only secret reference is `${{ secrets.MOJO_DEPLOY_KEY }}`.
* No runner ids, on any path.
* No per-node error text for an operator who could not change the app.
  `get_webapp_deployment` gives every viewer `nodes.{expected, completed,
  failed, pending}`, and only a writer gets `nodes.detail` and `nodes.errors`
  (at most five, truncated to 200 characters).
* No fleet inventory for a viewer: `get_webapp_serving` returns
  `serving.pools: null` and `upstreams: null`, plus `can_manage: false`.
* No purchase options in `precheck_new_webapp_address`.

## Approval cards

Every mutating tool produces an `approval` block instead of running — see
[approvals.md](approvals.md) for the block shape and the resolve endpoints.
What each card binds into its `preview.revision`:

| Tool | Card names | Step-up |
|---|---|---|
| `start_webapp_setup` | workspace, slug, environment, bucket | none |
| `answer_webapp_setup_step` | operation, revision, cursor, a hash of the choice | 600s |
| `cancel_webapp_setup` | operation, its current status | 600s |
| `attach_webapp_address` | app, normalized hostname, the preview verdict | 600s |
| `detach_webapp_address` | app, address id, hostname | 600s |
| `take_webapp_offline` | app, address, hostname, how many extra addresses go with it | 600s |
| `set_webapp_serving` | app, pool `old->new`, spa `old->new` | 600s |
| `switch_webapp_certificate` | app, certificate, its status, what it covers | 600s |
| `request_webapp_certificate` | app, hostname, domain | 600s |
| `add_webapp_route` | app, cleaned prefix, destination | 600s |
| `remove_webapp_route` | app, cleaned prefix, current destination | 600s |
| `rollback_webapp` | app, from-version, to-version, version string | 600s |
| `revoke_webapp_deploy_key` | app, key id | **300s** |
| `delete_webapp` | app, slug, version count, address count | 600s |

**`requires_fresh_auth: true` ⇒ REST only.** The WebSocket path answers
`{"type": "assistant_error", "code": "reauth_required", "action_id": ...}`;
step up and re-submit the same `action_id` over `POST /api/assistant/action`.
Only `start_webapp_setup` can be resolved over the socket.

Two windows are worth surfacing differently in the UI: 300 seconds on
`revoke_webapp_deploy_key` (matching `POST webapp/revoke_key`) and 600 on
everything else. `delete_webapp` carries a step-up that the REST delete does
not — chat is a model-initiated surface, so the escalation is deliberate.

### `precondition_failed` on a setup step is normal

`answer_webapp_setup_step` binds the setup's numeric revision, and the
background worker bumps that revision every time it advances. A card left open
while the setup progresses will resolve as `409 precondition_failed`. That is
the design, not an error state: re-read with `get_webapp_setup_status` and let
the assistant propose the current step again. Render it as "this moved on,
here is where it is now", not as a failure.

### The required reason

Six tools take a `reason` (3–300 characters): `cancel_webapp_setup`,
`detach_webapp_address`, `take_webapp_offline`, `rollback_webapp`,
`revoke_webapp_deploy_key`, `delete_webapp`. It is bound into the approval and
rendered in `preview.details.reason`. There is no typed-confirmation echo
anywhere in this domain — the approval click is the confirmation, and the
reason is the evidence that outlives it.

## Handoffs, not tools

Three operations are not available in chat and come back as wording, not as an
action:

| Operation | What the client should show |
|---|---|
| Create or rotate a deploy key | `get_webapp_deploy_setup.key_handoff` — the key is shown exactly once, in Admin → Deployments → the app → Key tab |
| Buy a domain | Refused with a plain sentence; the Domains page owns purchase |
| Upload a build from a laptop | `get_webapp_deploy_setup.upload_handoff` — push to the connected repository, or use the interactive Deploys workflow |

## Progress is never inferred

The tools report what the server knows and nothing more, and the tool
descriptions tell the model the same:

* A deployment that is `queued` or `deploying` has **not** landed. Read
  `get_webapp_deployment` for what actually converged.
* A certificate that is `pending` or `issuing` is **not** issued. An address in
  `certificate_pending` is not live.
* A setup whose status is `waiting` with `records` in
  `evidence.address` means the operator must publish those DNS records
  themselves before anything else can happen.
* A failed fleet deployment is restored to the previous version by the platform
  itself. That is not the rollback an operator asked for, and the assistant is
  told not to describe it as one.
