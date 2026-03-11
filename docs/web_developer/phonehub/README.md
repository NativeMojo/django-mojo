# PhoneHub — REST API Reference

PhoneHub provides phone number normalization, carrier lookup, SMS sending, and delivery status tracking.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/phonehub/number` | List phone numbers |
| GET | `/api/phonehub/number/<id>` | Get a phone number |
| POST | `/api/phonehub/number/normalize` | Normalize to E.164 format |
| POST | `/api/phonehub/number/lookup` | Look up carrier and line type |
| GET | `/api/phonehub/sms` | List SMS records |
| GET | `/api/phonehub/sms/<id>` | Get a single SMS |
| POST | `/api/phonehub/sms/send` | Send an SMS |
| GET | `/api/phonehub/config` | List phone configs |
| GET | `/api/phonehub/config/<id>` | Get a phone config |

---

## Normalize a Phone Number

Converts any phone number format to E.164. No carrier lookup — pure normalization.

**POST** `/api/phonehub/number/normalize`

```json
{
  "phone_number": "415-555-1234",
  "country_code": "US"
}
```

`country_code` is optional. 10-digit numbers without a country code are assumed US/CA.

**Response:**
```json
{
  "status": true,
  "data": {
    "phone_number": "+14155551234"
  }
}
```

**On invalid input:**
```json
{
  "status": false,
  "error": "Could not normalize phone number"
}
```

---

## Look Up Phone Info

Returns carrier, line type, and owner data for a phone number. Results are cached on the server — re-fetched automatically when the cache expires (default 90 days).

**POST** `/api/phonehub/number/lookup`

```json
{
  "phone_number": "+14155551234",
  "force_refresh": false
}
```

Pass `force_refresh: true` to bypass the cache and fetch fresh data from the carrier.

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
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T10:30:00Z"
  }
}
```

| Field | Values | Description |
|---|---|---|
| `line_type` | `mobile`, `landline`, `voip` | Type of phone line |
| `is_mobile` | bool | True for mobile numbers |
| `is_voip` | bool | True for VoIP numbers |
| `is_valid` | bool | Whether the number passed validation |
| `registered_owner` | string or null | CNAM registered name, if available |
| `owner_type` | `CONSUMER`, `BUSINESS`, or null | Registered owner type |

---

## Send an SMS

**POST** `/api/phonehub/sms/send`

**Requires:** `send_sms` permission

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

**On delivery failure:**
```json
{
  "status": true,
  "data": {
    "id": 43,
    "status": "failed",
    "error_code": "30006",
    "error_message": "Landline or unreachable carrier"
  }
}
```

Note: The outer `status` is `true` (the request succeeded); check `data.status` for the delivery result.

---

## List SMS Records

**GET** `/api/phonehub/sms`

Supports standard filtering, search, and pagination.

```
GET /api/phonehub/sms?direction=outbound&status=delivered
GET /api/phonehub/sms?search=+14155551234
GET /api/phonehub/sms?start=0&size=20&graph=default
```

**Response:**
```json
{
  "status": true,
  "count": 150,
  "start": 0,
  "size": 20,
  "data": [
    {
      "id": 42,
      "direction": "outbound",
      "from_number": "+18005551234",
      "to_number": "+14155551234",
      "body": "Your verification code is 483921",
      "status": "delivered",
      "created": "2024-01-15T10:30:00Z"
    }
  ]
}
```

### SMS status values

| Status | Description |
|---|---|
| `queued` | Queued for sending |
| `sending` | In progress |
| `sent` | Accepted by provider |
| `delivered` | Confirmed delivered |
| `failed` | Send failed |
| `undelivered` | Provider could not deliver |
| `received` | Inbound message received |

---

## Permissions

| Permission | Description |
|---|---|
| `view_phone_numbers` | Read phone number records |
| `manage_phone_numbers` | Create, update, delete phone number records |
| `view_sms` | Read SMS records |
| `manage_sms` | Update and delete SMS records |
| `send_sms` | Send SMS messages |
| `manage_phone_config` | Manage provider configuration |
| `owner` | Users can view their own SMS records |
