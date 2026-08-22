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

`?refresh=models` re-reads the model catalogue from the provider. Every other
read serves a shared 24-hour cache, so drawing the page costs no provider round
trip.

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
    "realtime_installed": true
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

`verify.code` is one of `verified`, `invalid_key`, `unreachable`,
`not_configured`, and `verify.message` is the fixed sentence for that code. No
provider response body, exception text, or key fragment ever appears there.

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
 "handler_api_key": "sk-ant-…", "clear_handler_api_key": false}
```

| Field | Rules |
|---|---|
| `enabled` | Boolean. Required |
| `model` | `""` for automatic, otherwise `^[a-z0-9][a-z0-9.\-]{0,80}$`. Required |
| `api_key` | Optional. The Assistant's own key. **Omit it to leave the stored credential alone** — an empty string is not "store nothing", so send nothing |
| `clear_api_key` | Optional boolean. Removes the stored Assistant key. Cannot be combined with `api_key` |
| `handler_api_key` | Optional. The **platform** key (`LLM_HANDLER_API_KEY`). Same omit-to-keep rule |
| `clear_handler_api_key` | Optional boolean. Removes the stored platform key and its verification record. Cannot be combined with `handler_api_key` |

Only these keys are accepted; anything else is a `400`. A supplied key —
either one — is **verified before anything is written**, and a rejected key
refuses the whole save — the installation never runs a credential nobody
proved.

Clearing a key while `enabled` stays true is allowed, and the next read says so
honestly: `key.source` falls back to `deployment`, `fallback` or `none`, and
`handler_key.source` to `deployment` or `none`.

---

## Errors

| Status | When |
|---|---|
| `400` | Unknown action, an unexpected field, a malformed model, or an API key over 4096 characters |
| `403` | Not a superuser, not an interactive bearer session, or the Origin does not match |
| `440` | The session is not recent enough. Step up and retry |

The failure message never contains a credential, a provider response body, or
an exception repr.

---

## See also

- [Admin Portal API Guide](../admin_portal.md) — the bootstrap and its capabilities
- [Assistant REST + WebSocket reference](../../assistant/README.md)
- [Approvals](../../assistant/approvals.md) — resolving a mutating assistant action
