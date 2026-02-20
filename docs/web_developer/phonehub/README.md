# PhoneHub — REST API Reference

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/phonehub/number` | List phone numbers |
| GET | `/api/phonehub/number/<id>` | Get phone number |
| POST | `/api/phonehub/number/normalize` | Normalize to E.164 format |
| POST | `/api/phonehub/number/lookup` | Lookup carrier/type info |

## Permissions

- `view_phonehub` or `manage_phonehub`

## Normalize a Phone Number

**POST** `/api/phonehub/number/normalize`

```json
{
  "phone_number": "415-555-1234",
  "country_code": "US"
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "phone_number": "+14155551234"
  }
}
```

## Lookup Phone Info

**POST** `/api/phonehub/number/lookup`

```json
{
  "phone_number": "+14155551234",
  "force_refresh": false
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 1,
    "phone_number": "+14155551234",
    "carrier": "AT&T",
    "line_type": "mobile",
    "is_valid": true,
    "caller_name": "John Doe",
    "created": "2024-01-01T00:00:00Z"
  }
}
```

| Field | Description |
|---|---|
| `line_type` | `mobile`, `landline`, or `voip` |
| `carrier` | Carrier name |
| `is_valid` | Whether the number is valid |
| `caller_name` | Registered name if available |

Pass `force_refresh: true` to bypass the cache and re-query the carrier.

## List Phone Numbers

**GET** `/api/phonehub/number`

```
GET /api/phonehub/number?line_type=mobile&carrier=AT%26T
```
