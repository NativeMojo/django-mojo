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
