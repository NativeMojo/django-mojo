# PhoneHub REST Endpoints — Django Developer Reference

All endpoints are registered under the `/api/phonehub/` prefix.

## Phone Number Endpoints

### CRUD — PhoneNumber

```python
@md.URL('number')
@md.URL('number/<int:pk>')
def on_phone_number(request, pk=None):
    return PhoneNumber.on_rest_request(request, pk)
```

| Method | Path | Action |
|---|---|---|
| GET | `/api/phonehub/number` | List phone numbers (filtered to `is_valid=True` by default) |
| GET | `/api/phonehub/number/<id>` | Get a single phone number |
| POST | `/api/phonehub/number` | Create a phone number record |
| POST/PUT | `/api/phonehub/number/<id>` | Update a phone number record |
| DELETE | `/api/phonehub/number/<id>` | Delete a phone number record |

**Permissions:** `view_phone_numbers`, `manage_phone_numbers`, or `manage_users`

---

### Normalize — `POST /api/phonehub/number/normalize`

Normalize any phone number input to E.164 format. Does not hit the carrier — pure string normalization.

**Requires:** `view_phone_numbers` or `manage_phone_numbers`

**Request:**
```json
{
  "phone_number": "415-555-1234",
  "country_code": "US"
}
```

`country_code` is optional. Without it, 10-digit numbers are assumed NANP (US/CA).

**Response:**
```json
{
  "status": true,
  "data": {
    "phone_number": "+14155551234"
  }
}
```

Returns `{"status": false, "error": "..."}` if the number cannot be normalized.

---

### Lookup — `POST /api/phonehub/number/lookup`

Look up carrier and line type data for a phone number. Results are cached; re-fetches automatically when expired.

**Requires:** `view_phone_numbers` or `manage_phone_numbers`

**Request:**
```json
{
  "phone_number": "+14155551234",
  "force_refresh": false
}
```

Pass `force_refresh: true` to bypass the cache and re-query the provider.

**Response:**
```json
{
  "status": true,
  "data": {
    "id": 1,
    "phone_number": "+14155551234",
    "country_code": "US",
    "state": "CA",
    "carrier": "AT&T",
    "line_type": "mobile",
    "is_mobile": true,
    "is_voip": false,
    "is_valid": true,
    "registered_owner": "John Smith",
    "owner_type": "CONSUMER",
    "lookup_provider": "twilio",
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T10:30:00Z"
  }
}
```

---

## SMS Endpoints

### CRUD — SMS

```python
@md.URL('sms')
@md.URL('sms/<int:pk>')
def on_sms(request, pk=None):
    return SMS.on_rest_request(request, pk)
```

| Method | Path | Action |
|---|---|---|
| GET | `/api/phonehub/sms` | List SMS records |
| GET | `/api/phonehub/sms/<id>` | Get a single SMS |
| POST/PUT | `/api/phonehub/sms/<id>` | Update an SMS record |
| DELETE | `/api/phonehub/sms/<id>` | Delete an SMS record |

**Permissions:** `view_sms`, `manage_sms`, or `owner` (users can view their own)

---

### Send — `POST /api/phonehub/sms/send`

Send an SMS message.

**Requires:** `send_sms` permission

**Request:**
```json
{
  "to_number": "+14155551234",
  "body": "Your verification code is 483921",
  "from_number": "+18005551234",
  "group": 5,
  "metadata": {"purpose": "verification"}
}
```

`from_number`, `group`, and `metadata` are optional.

**Response:**
```json
{
  "status": true,
  "data": {
    "id": 42,
    "direction": "outbound",
    "to_number": "+14155551234",
    "from_number": "+18005551234",
    "body": "Your verification code is 483921",
    "status": "sent",
    "provider": "twilio",
    "provider_message_id": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    "sent_at": "2024-01-15T10:30:00Z",
    "created": "2024-01-15T10:30:00Z"
  }
}
```

On failure: `status` will be `"failed"` and `error_message` will contain the provider error.

> **Provider-to-provider use:** This same endpoint doubles as the integration surface for the `mojo` SMS provider. A downstream django-mojo instance configured with `PhoneConfig.provider="mojo"` authenticates here using an `account.ApiKey` (`Authorization: apikey <token>`) and forwards the user's send request. `requires_perms` uses OR logic, so the api key needs **either `send_sms` or `comms`** — granting just `send_sms` is the least-privilege choice. See [README — Mojo Remote SMS Provider](README.md#mojo-remote-sms-provider) for the full setup.
>
> When the caller authenticates with an API key, the resulting `SMS` row has a null `user` (an API key is not a `User`); the caller is identified by `SMS.group`, which is set from the API key's group. Session/JWT callers still populate `SMS.user` as before.

---

### Twilio Inbound Webhook — `POST /api/phonehub/sms/webhook/twilio`

Public endpoint. Receives inbound SMS from Twilio. Validates the Twilio webhook signature before processing.

Saves the inbound message as an `SMS` record with `direction="inbound"`, then calls `SMS_INBOUND_HANDLER` if configured. Returns a TwiML response (may include a reply if the handler returns a string).

**This endpoint is public (no auth).** Twilio signature validation is the security mechanism.

---

### Twilio Status Webhook — `POST /api/phonehub/sms/webhook/twilio/status`

Public endpoint. Receives SMS delivery status updates from Twilio. Updates the corresponding `SMS` record's `status` and `delivered_at` fields.

---

## Config Endpoints

### CRUD — PhoneConfig

```python
@md.URL('config')
@md.URL('config/<int:pk>')
def on_phone_config(request, pk=None):
    return PhoneConfig.on_rest_request(request, pk)
```

| Method | Path | Action |
|---|---|---|
| GET | `/api/phonehub/config` | List configs (filtered to `is_active=True` by default) |
| GET | `/api/phonehub/config/<id>` | Get a single config |
| POST | `/api/phonehub/config` | Create a config |
| POST/PUT | `/api/phonehub/config/<id>` | Update a config |
| DELETE | `/api/phonehub/config/<id>` | Delete a config |

**Permissions:** `manage_phone_config` or `manage_groups`

`mojo_secrets` (encrypted credentials) is never returned in any response.

#### Writing encrypted credentials via REST

Provider credentials are stored encrypted in `mojo_secrets` and are **write-only over REST**. The standard auto-setter pattern routes each credential field through its `set_<field>` helper transparently:

```http
POST /api/phonehub/config/<id>
Content-Type: application/json

{
  "mojo_api_key": "<token shown once by /api/group/apikey>"
}
```

Supported credential body keys:

| Key | Routes to | Provider |
|---|---|---|
| `twilio_account_sid` | `set_twilio_account_sid` | twilio |
| `twilio_auth_token` | `set_twilio_auth_token` | twilio |
| `aws_access_key_id` | `set_aws_access_key_id` | aws |
| `aws_secret_access_key` | `set_aws_secret_access_key` | aws |
| `mojo_api_key` | `set_mojo_api_key` | mojo |

Leaving a credential key out of the body = no change. Setting any of these keys requires the same `SAVE_PERMS` as scalar updates. The response is the standard PhoneConfig payload (without secrets).

#### Test the configured provider — `POST /api/phonehub/config/<id>` with `test_connection`

Per-instance action that runs the per-provider connectivity check (`_test_twilio` / `_test_aws` / `_test_mojo`) and returns the result inline. Used by the admin portal's "Test connection" button. For the `mojo` provider the check GETs the remote's `/api/group/apikey/me` whoami endpoint — it validates the URL and api key and confirms the key carries a send permission, with no SMS row created on the remote.

```http
POST /api/phonehub/config/<id>
Content-Type: application/json

{
  "test_connection": 1
}
```

**Response:**

```json
{
  "success": true,
  "message": "Mojo provider reachable and API key valid",
  "remote_url": "https://sms-hub.example.com"
}
```

On failure, `success` is `false` and `error` is one of `missing_credentials`, `invalid_credentials`, `insufficient_permission` (mojo provider — the key is valid but lacks `send_sms`/`comms`), `timeout`, `connection_failed`, `missing_library`, or `invalid_provider`. Operators get a non-throwing dict either way; the underlying exception (if any) is logged via `mojo.helpers.logit` and not echoed back.
