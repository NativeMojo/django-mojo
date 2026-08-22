# Admin Assistant setup API

Two endpoints, both **owner-only for read and write**. The coarse readiness
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
    "schema_version": 1,
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
         "scopes": ["mcp"],
         "created": "2026-08-18T14:05:00+00:00",
         "last_used": "2026-08-21T17:40:00+00:00",
         "expires": "2026-09-17T14:05:00+00:00",
         "is_active": true, "revoked_reason": ""}
      ],
      "grant_count": 1
    }
  }
}
```

| Field | Meaning |
|---|---|
| `enabled` | The `LLM_ADMIN_ENABLED` flag |
| `key.configured` | Whether **any** credential resolves |
| `key.hint` | The last four characters, or `""` when the value is too short to hint at safely |
| `key.source` | `admin` (stored here) · `deployment` (the settings file) · `fallback` (resolving through the platform key) · `none` |
| `handler_key` | The **platform** key, `LLM_HANDLER_API_KEY` — used by every LLM feature (incident triage, the LLM agent) and as the Assistant's fallback. Same `configured` / `hint` / `source` shape; `source` is `admin` · `deployment` · `none` |
| `model.selected` | The stored pin, `""` for automatic |
| `model.effective` | What resolution actually returns right now |
| `model.source` | `admin` · `deployment` · `automatic` |
| `model.choices` | Picker suggestions only. Never validated against — the list is network-dependent |
| `verify` | How the **stored** Assistant credential last checked. `ok` is `null` when it never has |
| `handler_verify` | The same record for the stored platform key |
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
| `grants` | Active connections, newest first, for **every** user. At most 200 rows |
| `grant_count` | The true number of active connections, even when `grants` is sliced |

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

Both actions answer with the fresh `state()`, so a second editor holding a stale
page sees the truth on its very next call.

### `action: "verify"`

```json
{"action": "verify"}                                  // check what the Assistant resolves
{"action": "verify", "target": "handler"}             // check the stored platform key
{"action": "verify", "api_key": "sk-…"}               // check a candidate before saving
{"action": "verify", "api_key": "sk-…", "target": "handler"}
```

`target` is `assistant` (default) or `handler`. It chooses which stored key is
checked when no candidate is supplied, and which record (`verify` /
`handler_verify`) a stored-key check is written to.

```json
200 {"status": true, "data": {
  "schema_version": 1, "verified": true,
  "result": {"ok": false, "code": "invalid_key",
             "message": "Anthropic rejected this key."},
  "state": { … }
}}
```

Checking the **stored** key is recorded and shows up in `verify` on the next
read. Checking an unsaved candidate is **not** recorded: a draft is not the
configuration this installation is running.

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
| `model` | `""` for automatic, otherwise `^[a-z0-9][a-z0-9.\-]{0,80}$`. Required |
| `api_key` | Optional. The Assistant's own key. **Omit it to leave the stored credential alone** — an empty string is not "store nothing", so send nothing |
| `clear_api_key` | Optional boolean. Removes the stored Assistant key. Cannot be combined with `api_key` |
| `handler_api_key` | Optional. The **platform** key (`LLM_HANDLER_API_KEY`). Same omit-to-keep rule |
| `clear_handler_api_key` | Optional boolean. Removes the stored platform key and its verification record. Cannot be combined with `handler_api_key` |
| `mcp_enabled` | Optional boolean — remote agent access. **Omit it or send `null` to leave the switch alone.** Any other non-boolean (`"true"`, `1`) is a `400`, never coerced |

Remote agent access is independent of `enabled` and of any API key: a remote
client brings its own model. Switching it off **pauses** existing connections
rather than revoking them — they are still listed, and turning the switch back
on brings them back.

Only these keys are accepted; anything else is a `400`. A supplied key —
either one — is **verified before anything is written**, and a rejected key
refuses the whole save — the installation never runs a credential nobody
proved.

Clearing a key while `enabled` stays true is allowed, and the next read says so
honestly: `key.source` falls back to `deployment`, `fallback` or `none`, and
`handler_key.source` to `deployment` or `none`.

### `action: "revoke_grant"`

Disconnect one remote agent.

```json
{"action": "revoke_grant", "grant_id": 41}
```

Exactly these two keys — anything more or less is a `400`, and `grant_id` must
be a positive integer (`"41"`, `0`, `true` and floats are all a `400`).

```json
200 {"status": true, "data": {
  "schema_version": 1, "revoked": 1, "state": { … }
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

---

## Errors

| Status | When |
|---|---|
| `400` | Unknown action, an unexpected field, a malformed model, an API key over 4096 characters, a non-boolean `mcp_enabled`, or a `grant_id` that is not a positive integer |
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
