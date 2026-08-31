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
      "choices": [{"id": "claude-sonnet-5", "label": "Claude Sonnet 5"}]
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
| `model.selected` | The legacy picker pin; `""` means no legacy pin |
| `model.effective` | The exact guarded Assistant route model |
| `model.source` | Provenance for the legacy picker pin: `admin` · `deployment` · `automatic` (no pin) |
| `model.choices` | Picker suggestions only. Never validated against — the list is network-dependent |
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
| `tools` | The Assistant's tools only. Every change still waits for an approval in the Admin |
| `api` | Full REST API access as that person. The approval step does **not** apply |
| `both` | Both of the above, on one credential |

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
| `model` | Legacy picker pin: `""` clears it; otherwise `^[a-z0-9][a-z0-9.\-]{0,80}$`. Required. The safety-policy route still owns the guarded runtime model |
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

`provider` may be omitted to reset all breakers. The action increments breaker
generations and does not clear either emergency stop.

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
