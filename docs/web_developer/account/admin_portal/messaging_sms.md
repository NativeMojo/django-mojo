# Admin Messaging — Text messages (SMS) API

The Admin portal's **Text messages** page (route `messaging-sms`, section
Messaging) manages the **system** `PhoneConfig` — the row every OTP, MFA,
magic-login, password-reset and phone-verification SMS on the installation
routes through — plus a zero-side-effect connection test and a test-SMS send.

`phonehub` is optional. When it is not installed, both endpoints answer with a
clean envelope instead of an error:

```json
{"schema_version": 1, "installed": false,
 "error": "Text messaging is not installed on this platform",
 "error_code": "not_installed"}
```

## Permissions

| Operation | Requires |
|---|---|
| `GET .../summary`, `test_connection`, `send_test` | any of `manage_phone_config`, `manage_groups`, `comms`, `admin` |
| `action: "save"` | the tier above **AND a literal superuser** |

The superuser requirement on `save` is deliberate and body-enforced: whoever
writes the system row controls where every second factor on the installation
is delivered (a `mojo`-provider row POSTs each message body — the code — to an
operator-chosen URL). A non-superuser holding the full phone-config tier gets
**403**, and the row is untouched. The page reads
`capabilities.system_write` and disables the editor instead of offering a
control that 403s.

The POST endpoint also requires fresh authentication (600s) and refuses
API-key-backed sessions.

## GET /api/account/admin/messaging-sms/summary

```json
{
  "schema_version": 1,
  "installed": true,
  "system": {
    "id": 7, "name": "Mojo Remote SMS", "provider": "mojo",
    "is_active": true, "test_mode": false,
    "twilio_from_number": "", "aws_region": "", "aws_sender_id": "",
    "mojo_remote_url": "https://sms.example.com",
    "secrets": {
      "twilio_account_sid": false, "twilio_auth_token": false,
      "aws_access_key_id": false, "aws_secret_access_key": false,
      "mojo_api_key": true
    }
  },
  "group_overrides": {
    "items": [{"id": 12, "group": {"id": 2, "name": "Acme West"},
               "name": "Acme Twilio", "provider": "twilio",
               "test_mode": false}],
    "count": 1, "truncated": false
  },
  "verify_state": {"ok": true, "code": null,
                   "message": "Connection verified",
                   "at": "2026-08-18T09:14:00+00:00"},
  "settings_fallback": {"twilio_number_configured": true,
                        "twilio_credentials_configured": false},
  "capabilities": {"view": true, "system_write": false},
  "configuration_revision": "fedcba9876543210fedcba9876543210"
}
```

- `system` is `null` when no active system row exists.
- `secrets` reports **which secrets are set — never their values**.
- `group_overrides` lists only **active** group rows (an inactive override is
  never honored by the fallback resolution), bounded to 25 with an honest
  `count`/`truncated`.
- `configuration_revision` is the edit token a `save` must echo back as
  `expected_revision`. It is shared with the Settings page's provider editor,
  so either editor's save invalidates a stale session of the other.

## POST /api/account/admin/messaging-sms

Dispatches on `action`:

### `save` (superuser only)

Common fields: `provider` (`twilio` | `aws` | `mojo`), optional `name`,
`test_mode`, `expected_revision`. Provider-specific fields:

| Provider | Fields |
|---|---|
| `twilio` | `twilio_from_number`, `twilio_account_sid`, `twilio_auth_token`, `clear_twilio_credentials` |
| `aws` | `aws_region`, `aws_sender_id`, `aws_access_key_id`, `aws_secret_access_key`, `clear_aws_credentials` |
| `mojo` | `remote_url` (one HTTPS origin), `api_key`, `clear_api_key` |

Rules, all fail-closed:

- Unsupported fields are refused, credentials must be supplied as a **pair or
  not at all**, and a save whose credentials do not verify against the
  provider is refused with nothing written. `test_mode` does **not** skip
  that verification — `SMS.send()` never reads `test_mode`, so a test-mode
  config still sends real messages.
- Omitted credentials keep the stored ones (and are verified as stored). A
  stored `mojo` key is only reused for the URL it was stored against — a new
  `remote_url` requires a freshly supplied key.
- A mismatched `expected_revision` is refused with "Provider configuration
  changed; reload before publishing". Every successful save bumps the shared
  revision.

Response: `{"saved": true, "topic": "sms", "provider": "...", "config_id": 7,
"revision": "...", "results": {"sms": {...}}}`.

### `test_connection`

Optional `config_id` (defaults to the system row). Runs the provider's own
verification with zero side effects and returns a **diagnosed** result:

```json
{"schema_version": 1, "installed": true, "success": false,
 "state": "failed", "error": "invalid_credentials",
 "message": "The provider rejected the API key.",
 "config": {"id": 7, "provider": "mojo", "name": "Mojo Remote SMS"}}
```

- `state` is one of `ok`, `failed`, or `test_mode` — test mode renders as
  "Test mode — provider not contacted" and is never collapsed into OK.
- Failure `message` text comes from a fixed vocabulary keyed by `error`
  (`missing_credentials`, `missing_library`, `connection_failed`, `timeout`,
  `invalid_credentials`, `insufficient_permission`, `invalid_provider`,
  `no_config`). Raw provider exception text is logged server-side and never
  returned.

### `send_test`

`{"action": "send_test", "to_number": "+14155551234"}` sends one message
through the effective system config and reports the resulting SMS row:

```json
{"schema_version": 1, "installed": true, "sent": true, "test_number": false,
 "message": "Test message handed to the provider",
 "sms": {"id": 3101, "status": "sent", "provider": "mojo",
         "from_number": "+18005550100", "to_number": "+14155551234",
         "is_test": false, "error_code": null, "error_message": null}}
```

`+1555…` recipients short-circuit before any provider call and answer
`"test_number": true` / "Test number — nothing was sent".

## Dashboard row

The Dashboard's **Software** section carries a "Text messages" row reporting
the effective provider, its active state, and the last recorded verification
outcome (`sources.sms` in the [Dashboard API](dashboard.md)). The row is
**absent, not red**, when phonehub is not installed or no system config
exists; a failed last verification renders amber.
