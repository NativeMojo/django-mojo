# Admin Assistant setup API

Three endpoints expose this control plane. The Assistant GET and POST are
**literal-owner-only**; the aggregate LLM-safety GET is for global security
readers. The coarse readiness
every operator needs rides in the Admin bootstrap (`features.assistant`);
nothing below a literal superuser has a reason to read a key hint or a
verification outcome.

No response from either endpoint ever carries the API key. Not masked, not
truncated, not in an error message — only presence, a four-character hint, and
where the credential comes from. The request body that *sets* it is classified
sensitive, so it never reaches the generic request logs either.

The credential is stored encrypted **in the database**. It is not encrypted in
the settings cache: the framework's `push_to_cache` writes the decrypted value
into Redis exactly as it does for every other secret setting, so Redis holds a
live credential.

The key editor, candidate/stored key checks, and model catalogue remain
Anthropic-only. Optional Bedrock Nova 2 Lite text reasoning is configured by
the deployment's static safety policy and uses the server's AWS role/profile
chain. There are no AWS-key fields, provider-options input, or Bedrock
credential probe in these endpoints, and no default Bedrock enablement. A
Bedrock route cannot supply the tool-enabled Admin Assistant's capabilities.

---

## `GET /api/account/admin/assistant`

**Requires:** a global `manage_settings` or `admin` permission, an interactive
bearer session (no API key, no group token), and a live literal superuser.

`?refresh=models` re-reads the model catalogue from the provider. `?check=discovery`
runs the remote-agent discovery self-check (below). Every other read serves a
cache, so drawing the page costs no provider round trip and no outbound request
of any kind.

```json
200 {
  "status": true,
  "data": {
    "schema_version": 2,
    "enabled": true,
    "key": {"configured": true, "hint": "4d1a", "source": "admin"},
    "handler_key": {"configured": true, "hint": "9c2e", "source": "admin"},
    "model": {
      "selected": "claude-sonnet-5",
      "effective": "claude-sonnet-5",
      "source": "admin",
      "choices": [{"id": "claude-sonnet-5", "label": "Claude Sonnet 5"}],
      "mode": "pinned", "editable": true, "policy_model": "auto:sonnet",
      "restriction": "",
      "discovery": {"status": "not_applicable", "fetched_at": null,
                    "attempted_at": null, "expires_at": null,
                    "max_stale_at": null, "error_code": ""},
      "refresh": {"attempted": false, "ok": null, "code": ""}
    },
    "verify": {"ok": true, "code": "verified",
               "message": "Anthropic accepted this key.",
               "at": "2026-08-19T11:02:00+00:00"},
    "handler_verify": {"ok": null, "code": "", "message": "", "at": ""},
    "emergency_stop": true,
    "emergency_stop_static": true,
    "emergency_stop_database": false,
    "route": {"feature": "assistant", "provider": "anthropic",
              "model": "claude-sonnet-5", "credential": "admin",
              "credential_configured": true, "ready": false,
              "error": "emergency_stopped"},
    "autonomous_triage": false,
    "autonomous_triage_activated_at": null,
    "safety": {"hours": 24, "requests": [], "breakers": []},
    "assistant_installed": true,
    "realtime_installed": true,
    "mcp": {
      "enabled": true,
      "path": "/api/assistant/mcp",
      "url": "https://admin.example.com/api/assistant/mcp",
      "discovery_url": "https://admin.example.com/.well-known/oauth-protected-resource/api/assistant/mcp",
      "discovery": {"ok": true, "code": "ok",
                    "detail": "The discovery document is reachable at the public address.",
                    "checked_at": "2026-08-21T18:30:00+00:00"},
      "grants": [
        {"id": 41,
         "client": {"id": 7, "client_id": "https://claude.ai/.well-known/mcp-client",
                    "name": "Claude"},
         "user": {"id": 1, "email": "ian@example.com", "display_name": "Ian Smith"},
         "resource": "https://admin.example.com/api/assistant/mcp",
         "scopes": ["mcp"], "access": "tools",
         "created": "2026-08-18T14:05:00+00:00",
         "last_used": "2026-08-21T17:40:00+00:00",
         "expires": "2026-09-17T14:05:00+00:00",
         "is_active": true, "revoked_reason": ""},
        {"id": 43,
         "client": {"id": 9, "client_id": "dcr-8b31e04f", "name": "Ops script"},
         "user": {"id": 2, "email": "avery@example.com", "display_name": "Avery Cole"},
         "resource": "https://admin.example.com/api",
         "scopes": ["mcp", "api"], "access": "both",
         "created": "2026-08-21T11:48:00+00:00",
         "last_used": "2026-08-22T08:05:00+00:00",
         "expires": "2026-09-20T11:48:00+00:00",
         "is_active": true, "revoked_reason": ""}
      ],
      "grant_count": 2
    }
  }
}
```

| Field | Meaning |
|---|---|
| `enabled` | The `LLM_ADMIN_ENABLED` flag |
| `key.configured` | Legacy picker/display resolution only; guarded readiness is `route.credential_configured` |
| `key.hint` | The last four characters, or `""` when the value is too short to hint at safely |
| `key.source` | Exact Admin-target provenance: `admin` (stored here) · `deployment` (the settings file) · `none` |
| `handler_key` | The **platform** key, `LLM_HANDLER_API_KEY` — available only to policy routes declaring `credential: "handler"`; it never substitutes for an Admin-routed feature. Same `configured` / `hint` / `source` shape; `source` is `admin` · `deployment` · `none` |
| `model.selected` | Applicable owner/deployment pin; empty for automatic. Fixed routes preserve the inert legacy value for old clients; use `mode` to identify policy authority |
| `model.effective` | The exact guarded Assistant route model |
| `model.source` | `admin` · `deployment` · `automatic` · `policy`; fixed policies report `policy` |
| `model.choices` | At most 40 suggestions from known provider metadata; may be empty. Saving a manual pin does not require catalogue verification |
| `model.mode` | `auto` · `pinned` · `fixed` |
| `model.editable` | Whether policy explicitly delegates the Anthropic Assistant route |
| `model.policy_model` | Exact policy literal or `auto:sonnet`; empty for unavailable policy |
| `model.restriction` | Empty, `fixed_policy`, `provider_unsupported`, `deployment_pin`, or `policy_unavailable` |
| `model.discovery` | `status`: `fresh` · `stale` · `unavailable` · `not_applicable`; UTC ISO timestamps or null for `fetched_at`, `attempted_at`, `expires_at`, `max_stale_at`; bounded `error_code` |
| `model.refresh` | Actual requested refresh outcome; ordinary reads: `{attempted:false,ok:null,code:""}` |
| `verify` | How the **stored** Assistant credential last checked. `ok` is `null` when it never has |
| `handler_verify` | The same record for the stored platform key |
| `emergency_stop` | Effective deployment OR authoritative database stop |
| `emergency_stop_static` | Whether deployment configuration is holding the stop on; it requires removal and redeploy |
| `emergency_stop_database` | The editable authoritative database half; both Admin checkboxes bind and submit only this value |
| `route` | Exact guarded Assistant provider, model and credential name plus safe readiness; never a key or fingerprint |
| `autonomous_triage` | Authoritative primary-DB catch-all switch; fail-closed on ambiguity |
| `autonomous_triage_activated_at` | Primary-DB no-history watermark, or `null` |
| `safety` | Bounded aggregate usage and breaker rows; no per-row records or fingerprints |
| `mcp` | Remote agent access — see below |

The additive fields keep `schema_version: 2`; clients detect `model.editable`
by presence. `route.model` equals `model.effective`, including when stops or
missing controls prevent readiness. An unavailable/expired automatic model
has empty `effective` and cannot be ready. Additive `route.can_invoke` reports
passive invocation admission: policy agreed, stop clear and exact credential
present. It can be true while a cold/expired automatic route is not yet ready;
that allows the composer/socket to reach the guard that discovers and selects
the model. The bootstrap Assistant `ready` capability means this enabled
invocation admission, so it must not be replaced with strict `route.ready`
when deciding whether a first turn is possible. Neither predicate performs
provider work during GET/preflight; the guard still owns resolution, limits
and the actual model. Fixed/Bedrock admission behavior remains unchanged.
Fixed/pinned models do not depend
on catalogue availability; discovery may be `not_applicable`.

Only `routes.assistant.model="auto:sonnet"` on Anthropic delegates selection.
Existing literal policy models remain fixed, including Bedrock. The delegated
priority is owner-stored `LLM_ADMIN_MODEL`, file-owned `LLM_ADMIN_MODEL`, then
newest compatible active Sonnet by explicit provider line/lifecycle/release,
output-cap and required image metadata. No ID-name guessing or hardcoded
newest model is used. Modern dateless IDs are pinned releases, not floating
aliases. Before opting in, inspect/clear legacy pins, deploy the new policy,
and activate its hash normally. No installed policy is rewritten.

Catalogue cache identity includes policy hash, provider and exact discovery
credential fingerprint; discovery and runtime credentials must match. It is
fresh for 24 hours. Failed/incomplete refreshes may retain last-good data for
seven days from its original success, without extending that deadline.
Complete success with no compatible model invalidates the old selection.
A stale usable automatic route may be ready, with `status:"stale"`.
Normal GET never makes provider requests. First authorized calls after expiry
and explicit owner refreshes share a Redis 30-second owner lock and 60-second
cooldown, at most five pages of 100/500 entries, and a 20-second or smaller
policy deadline. Each page passes its own safety/permit/ledger and shares
one operation ID; smaller installed loop budgets remain binding. No SDK
retries or scheduler are added, and Redis coordination failure cannot issue
an uncontrolled request.

`GET ?refresh=models` reports that request truthfully: complete success has
`attempted:true,ok:true,code:""`; a failed fetch has `attempted:true,ok:false`
and a safe code even if cached data remains usable. Lock/cooldown denial has
`attempted:false,ok:false` and `refresh_in_progress`/`refresh_throttled`.
Never show a success toast merely because `model.effective` is nonempty.

For an explicitly configured Bedrock route, `credential` is `aws`.
`credential_configured=true` declares that the installation's AWS chain was
selected; `ready=true` adds policy agreement and a cleared emergency stop.
Neither value resolves credentials or proves IAM, model, or region access,
and drawing the page never invokes Bedrock. Only an authorized guarded call
proves access. AWS setup requires `bedrock:InvokeModel` for the selected
inference profile and destination foundation models, from a supported source
region; see [the deployment contract](../../../django_developer/security/llm_safety.md#bedrock-routes).

`verify.code` is one of `verified`, `invalid_key`, `unreachable`,
`not_configured`, and `verify.message` is the fixed sentence for that code. No
provider response body, exception text, or key fragment ever appears there.

### `mcp` — remote agent access

| Field | Meaning |
|---|---|
| `enabled` | The `ASSISTANT_MCP_ENABLED` switch. Off by default |
| `path` | The MCP door's absolute request path (`ASSISTANT_MCP_PATH`, default `/api/assistant/mcp`) |
| `url` | The **connect address** to paste into an AI client, or `""` when no public address is configured |
| `discovery_url` | The protected-resource metadata URL clients look for |
| `discovery` | The last self-check verdict — `{ok, code, detail, checked_at}` |
| `grants` | Active connections **to either remote-agent resource** — the MCP endpoint and the REST API root — newest first, for **every** user. At most 200 rows |
| `grant_count` | The true number of active connections to those two resources, even when `grants` is sliced |

Each row's **`access`** says what that connection can reach:

| `access` | Means |
|---|---|
| `tools` | **Tools** — plain `mcp`; shared changes wait for approval in Admin, with the existing own-memory/skill exception |
| `execute` | **Delegated tools** — explicit `mcp mcp:execute`; ordinary tools execute within the person's live permissions, while protected tools keep their confirmation checks |
| `api` | Full REST API access as that person. The approval step does **not** apply |
| `both` | **Tools + full API** — `mcp api`; MCP shared changes remain proposals, and direct REST calls have full-API reach |

The execution scope is offered only at the exact MCP resource. It requires a
new, explicit consent flow; switching on remote access or refreshing an old
connection never upgrades it. See [MCP](../../assistant/mcp.md#requesting-delegated-tool-execution).

`discovery.code` is one of:

| Code | `ok` | Means |
|---|---|---|
| `ok` | `true` | The discovery document is served at the public address |
| `unreachable` | `false` | The public address answered, but not with the document — `detail` names the cause (an HTTP status, a redirect, a non-document body, or a fetch failure) |
| `disabled` | `false` | Nothing was probed: the switch is off, or no public address is configured |

`checked_at` is `""` and `ok` is `null` when no check has run — the switch being
off, or the 60-second cache having expired. `detail` comes from a fixed
vocabulary; no response body or exception text ever appears there.

A grant row carries names, ids and dates only. No token, jti, or hash is ever
returned.

### `?check=discovery`

Runs the self-check: the server fetches **its own** `discovery_url` and reports
one of the three verdicts above. It is an explicit control, never a page-load
side effect, and it re-probes at most once every 60 seconds — the cached network
verdict is served first. Switching the switch drops that cache, so "turn it on,
check now" never shows the previous answer.

---

## `POST /api/account/admin/assistant`

Everything the `GET` requires, **plus** recent authentication (600 seconds) and
a same-Origin request. A stale session answers `440` with
`{"error": "reauth_required"}`; step up and re-submit the identical body.

Every action answers with the fresh `state()`, so a second editor holding a stale
page sees the truth on its very next call.

### `action: "verify"`

```json
{"action": "verify"}                                  // check the exact stored admin key
{"action": "verify", "target": "handler"}             // check the stored platform key
{"action": "verify", "api_key": "sk-…"}               // check a candidate before saving
{"action": "verify", "api_key": "sk-…", "target": "handler"}
```

`target` is `assistant` (default, exact `LLM_ADMIN_API_KEY`) or `handler`
(exact `LLM_HANDLER_API_KEY`). There is no fallback. It chooses which stored key is
checked when no candidate is supplied, and which record (`verify` /
`handler_verify`) a stored-key check is written to.

```json
200 {"status": true, "data": {
  "schema_version": 2, "verified": true,
  "result": {"ok": false, "code": "invalid_key",
             "message": "Anthropic rejected this key."},
  "state": { … }
}}
```

Checking the **stored** key is recorded and shows up in `verify` on the next
read. A candidate does not overwrite that stored-key record, but every
actor-authorized candidate recovery probe writes a durable
`llm:candidate_probe` audit with only `requested`, `accepted`, or `rejected`.
It never records the candidate, a fingerprint, a model, or provider text. If
the requested audit cannot be written, the provider is not called.

A stored-key check is an ordinary guarded request and is refused while the
emergency stop is effective. Only a supplied candidate receives the stopped
exception: one installation-wide single-flight, fixed `Reply OK` request on
the configuration route/model with no tools, images, cache, caller model,
context, or pagination. Candidate input must be a non-empty string and the
service re-proves active literal-owner authority; REST also requires fresh
authentication. A configuration route with capabilities `["text"]` is
sufficient.

The configuration route must be Anthropic. A Bedrock configuration route is
refused before any candidate/stored probe; the emergency-stop exception never
tests the AWS chain. Model catalogue refresh likewise remains Anthropic-only.

### `action: "save"`

```json
{"action": "save", "enabled": true, "model": "claude-sonnet-5",
 "api_key": "sk-ant-…", "clear_api_key": false,
 "handler_api_key": "sk-ant-…", "clear_handler_api_key": false,
 "mcp_enabled": true}
```

| Field | Rules |
|---|---|
| `enabled` | Boolean. Required |
| `model` | Optional. On delegated routes omit it; old clients may echo the current `selected` as a no-op. A differing echo is rejected under the installation lock with an instruction to use `set_model_selection`. Nondelegating policies retain the inert legacy writer (`""` clears) |
| `api_key` | Optional. The Assistant's own key. **Omit it to leave the stored credential alone** — an empty string is not "store nothing", so send nothing |
| `clear_api_key` | Optional boolean. Removes the stored Assistant key. Cannot be combined with `api_key` |
| `handler_api_key` | Optional. The **platform** key (`LLM_HANDLER_API_KEY`). Same omit-to-keep rule |
| `clear_handler_api_key` | Optional boolean. Removes the stored platform key and its verification record. Cannot be combined with `handler_api_key` |
| `mcp_enabled` | Optional boolean — remote agent access. **Omit it or send `null` to leave the switch alone.** Any other non-boolean (`"true"`, `1`) is a `400`, never coerced |
| `emergency_stop` | Optional boolean. Protected database stop for ordinary provider requests |
| `autonomous_triage` | Optional boolean. Enables only post-watermark catch-all incident work |

`GET /api/account/admin/llm-safety?hours=24` is available to globally
authorized security readers. `hours` is clamped to 1–168. It returns schema version 2 and
bounded provider/feature/status aggregates and breaker counts, never per-row
ledger/circuit/attempt records or credential fingerprints.

### `action: "set_model_selection"`

Use a separate request to change active model selection:

```json
{"action":"set_model_selection","mode":"pinned","model":"opaque-model-id"}
```

```json
{"action":"set_model_selection","mode":"auto"}
```

Exact keys only. Pinned requires a nonblank structurally valid ID matching
`^[a-z0-9][a-z0-9.\-]{0,80}$`; auto forbids a `model` key. This uses the
existing owner/fresh-interactive-auth/same-origin boundary, installation
lock, protected settings writer, cache invalidation and audit. No network
validation is required to save a known manual pin. The answer is
`{schema_version:2,saved:true,state:<passive fresh state>}`.
Fixed/unsupported policy routes reject the action. A nonblank deployment
`LLM_ADMIN_MODEL` can be overridden by an owner pin but cannot be cleared
by deleting the database row: `restriction:"deployment_pin"` remains visible
even during the owner override, and Auto is rejected with a readable
instruction to remove the deployment pin. Generic Save omits model on capable
clients; an omitted/equal old-client echo cannot race and overwrite a pin.

### LLM safety actions

All three use the same recent-auth literal-owner boundary as save:

```json
{"action": "activate_policy"}
```

Activates the exact hash of the currently deployed static policy. Keep the
deployment emergency stop on until every node has the same policy.

```json
{"action": "reset_breaker", "provider": "anthropic"}
```

`provider` accepts `anthropic` or `bedrock`, or may be omitted to reset all
breakers. The action increments breaker generations and does not clear either
emergency stop.

```json
{"action": "historical_triage",
 "before": "2026-08-31T00:00:00Z", "limit": 20}
```

`before` is an ISO timestamp and `limit` is an integer from 1 through 100.
This is the bounded explicit opt-in for pre-watermark incidents. The
autonomous switch gates catch-all pickup only; manual analysis and linked
ticket work remain explicit but still obey the complete guard.

Remote agent access is independent of `enabled` and of any API key: a remote
client brings its own model. Switching it off **pauses** existing connections
rather than revoking them — they are still listed, and turning the switch back
on brings them back.

Only these keys are accepted; anything else is a `400`. A supplied key —
either one — is **verified before anything is written**, and a rejected key
refuses the whole save — the installation never runs a credential nobody
proved.

Clearing a key while `enabled` stays true is allowed, and the next read says so
honestly. Each exact target independently reports `deployment` or `none`; an
Admin route never reports or uses the handler credential as a fallback.

### `action: "revoke_grant"`

Disconnect one remote agent.

```json
{"action": "revoke_grant", "grant_id": 41}
```

Exactly these two keys — anything more or less is a `400`, and `grant_id` must
be a positive integer (`"41"`, `0`, `true` and floats are all a `400`).

```json
200 {"status": true, "data": {
  "schema_version": 2, "revoked": 1, "state": { … }
}}
```

`revoked` is `1` when a live connection was killed and `0` when the id was
unknown or already dead — never a `404`. The connection's access token is
refused at the MCP door on its next request and its refresh token is refused at
the token endpoint. Repaint from `state`.

### `action: "revoke_all_grants"`

Disconnect every remote agent, for every user.

```json
{"action": "revoke_all_grants"}
```

Exactly that one key. `revoked` is the number of connections killed.

The listing, the count and **Disconnect all** are scoped to the **two**
remote-agent resources — the MCP endpoint and the REST API root — so
Disconnect all sweeps a full-API connection as well as a tool-door one, and a
grant this installation issued for some other protected resource is neither
listed above nor swept here.

**`revoke_grant` is not path-scoped.** It revokes by id, whatever resource that
grant names. It is owner-only and the id has to come from somewhere, so this is
not a way to reach grants the page does not show — but do not read it as a
scoped operation.

---

## Errors

| Status | When |
|---|---|
| `400` | Unknown action, unexpected field, malformed/empty candidate, malformed model, API key over 4096 characters, non-boolean switches, invalid `hours`, invalid reset provider, invalid historical timestamp/limit, or a non-positive `grant_id` |
| `403` | Not a superuser, not an interactive bearer session, or the Origin does not match |
| `440` | The session is not recent enough. Step up and retry |

The failure message never contains a credential, a provider response body, or
an exception repr.

---

## See also

- [Admin Portal API Guide](../admin_portal.md) — the bootstrap and its capabilities
- [Assistant REST + WebSocket reference](../../assistant/README.md)
- [Approvals](../../assistant/approvals.md) — resolving a mutating assistant action
- [Connecting an AI client over MCP](../../assistant/mcp.md) — the operator's connect runbook
- [OAuth authorization server](../oauth_server.md) — how a remote client signs in and what a grant is
