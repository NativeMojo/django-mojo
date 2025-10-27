# PhoneHub REST API Documentation

Complete REST API reference for PhoneHub phone number and SMS operations.

## Base URL

All endpoints are prefixed with:
```
/api/phonehub/
```

## Authentication

Most endpoints require authentication via JWT token:
```
Authorization: Bearer <your_jwt_token>
```

Webhook endpoints do not require authentication (they're called by Twilio).

---

## Phone Number Endpoints

### Normalize Phone Number

Converts a phone number to E.164 format.

**Endpoint:** `POST /api/phonehub/number/normalize`

**Authentication:** Required

**Request Body:**
```json
{
    "phone_number": "415-555-1234",
    "country_code": "US"  // optional, defaults to US
}
```

**Success Response (200):**
```json
{
    "status": true,
    "data": {
        "phone_number": "+14155551234"
    }
}
```

**Error Response (200):**
```json
{
    "status": false,
    "error": "Invalid phone number"
}
```

**Example:**
```bash
curl -X POST https://api.example.com/api/phonehub/number/normalize \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "(415) 555-1234"
  }'
```

---

### Lookup Phone Number

Retrieves detailed information about a phone number including carrier, line type, and registered owner.

**Endpoint:** `POST /api/phonehub/number/lookup`

**Authentication:** Required

**Request Body:**
```json
{
    "phone_number": "+14155551234",
    "force_refresh": false,  // optional, force new lookup ignoring cache
    "group": 123            // optional, organization context
}
```

**Success Response (200):**
```json
{
    "status": true,
    "data": {
        "id": 456,
        "phone_number": "+14155551234",
        "country_code": "US",
        "region": "San Francisco",
        "state": "CA",
        "carrier": "Verizon Wireless",
        "line_type": "mobile",
        "is_mobile": true,
        "is_voip": false,
        "is_valid": true,
        "registered_owner": "John Smith",
        "owner_type": "CONSUMER",
        "address_line1": null,
        "address_city": null,
        "address_state": null,
        "address_zip": null,
        "address_country": null,
        "lookup_provider": "twilio",
        "lookup_expires_at": "2025-01-24T12:00:00Z",
        "lookup_count": 1,
        "last_lookup_at": "2024-10-24T12:00:00Z",
        "created": "2024-10-24T12:00:00Z",
        "modified": "2024-10-24T12:00:00Z"
    }
}
```

**Error Response (200):**
```json
{
    "status": false,
    "error": "Phone lookup failed"
}
```

**Example:**
```bash
curl -X POST https://api.example.com/api/phonehub/number/lookup \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+14155551234",
    "force_refresh": false
  }'
```

**Notes:**
- Lookup results are cached for 90 days
- Set `force_refresh: true` to bypass cache and perform fresh lookup
- Fresh lookups count against your Twilio API quota

---

### List Phone Numbers

Get a paginated list of all cached phone numbers.

**Endpoint:** `GET /api/phonehub/number`

**Authentication:** Required

**Query Parameters:**
- `size` (or `limit`) - Number of results per page (default: 10)
- `start` (or `offset`) - Pagination offset (default: 0)
- `search` - Search phone_number, carrier, or registered_owner
- `is_mobile` - Filter by mobile numbers (true/false)
- `is_voip` - Filter by VOIP numbers (true/false)
- `is_valid` - Filter by validity (true/false)

**Success Response (200):**
```json
{
    "status": true,
    "data": [
        {
            "id": 456,
            "phone_number": "+14155551234",
            "carrier": "Verizon Wireless",
            "line_type": "mobile",
            "is_mobile": true,
            "is_voip": false,
            "is_valid": true,
            "registered_owner": "John Smith",
            "owner_type": "CONSUMER",
            "lookup_expires_at": "2025-01-24T12:00:00Z",
            "last_lookup_at": "2024-10-24T12:00:00Z",
            "created": "2024-10-24T12:00:00Z"
        }
    ],
    "count": 150,
    "size": 10,
    "start": 0
}
```

**Example:**
```bash
curl -X GET "https://api.example.com/api/phonehub/number?is_mobile=true&size=10&start=0" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

### Get Phone Number

Retrieve a specific cached phone number by ID.

**Endpoint:** `GET /api/phonehub/number/<id>`

**Authentication:** Required

**Success Response (200):**
```json
{
    "status": true,
    "data": {
        "id": 456,
        "phone_number": "+14155551234",
        "country_code": "US",
        "region": "San Francisco",
        "state": "CA",
        "carrier": "Verizon Wireless",
        "line_type": "mobile",
        "is_mobile": true,
        "is_voip": false,
        "is_valid": true,
        "registered_owner": "John Smith",
        "owner_type": "CONSUMER",
        "lookup_expires_at": "2025-01-24T12:00:00Z",
        "created": "2024-10-24T12:00:00Z"
    }
}
```

**Example:**
```bash
curl -X GET https://api.example.com/api/phonehub/number/456 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

### Update Phone Number

Update a cached phone number record (rarely needed).

**Endpoint:** `PUT /api/phonehub/number/<id>`

**Authentication:** Required

**Permissions:** `manage_phone_numbers`

**Request Body:**
```json
{
    "carrier": "Updated Carrier",
    "is_valid": false
}
```

**Success Response (200):**
```json
{
    "status": true,
    "data": {
        "id": 456,
        "phone_number": "+14155551234",
        "carrier": "Updated Carrier",
        "is_valid": false
    }
}
```

---

### Delete Phone Number

Remove a cached phone number from the system.

**Endpoint:** `DELETE /api/phonehub/number/<id>`

**Authentication:** Required

**Permissions:** `manage_phone_numbers`

**Success Response (200):**
```json
{
    "status": true
}
```

**Example:**
```bash
curl -X DELETE https://api.example.com/api/phonehub/number/456 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## SMS Endpoints

### Send SMS

Send an SMS message via Twilio.

**Endpoint:** `POST /api/phonehub/sms/send`

**Authentication:** Required

**Permissions:** `send_sms`

**Request Body:**
```json
{
    "to_number": "+14155551234",
    "body": "Your verification code is: 123456",
    "from_number": "+14155556789",  // optional, uses default if not provided
    "group": 123,                   // optional, organization context
    "metadata": {                   // optional, custom data
        "campaign_id": "welcome_email",
        "user_id": 789
    }
}
```

**Success Response (200):**
```json
{
    "status": true,
    "data": {
        "id": 1001,
        "direction": "outbound",
        "from_number": "+14155556789",
        "to_number": "+14155551234",
        "body": "Your verification code is: 123456",
        "status": "queued",
        "provider": "twilio",
        "provider_message_id": "SM1234567890abcdef",
        "error_code": null,
        "error_message": null,
        "metadata": {
            "campaign_id": "welcome_email",
            "user_id": 789
        },
        "sent_at": "2024-10-24T12:00:00Z",
        "delivered_at": null,
        "created": "2024-10-24T12:00:00Z"
    }
}
```

**Error Response (200):**
```json
{
    "status": false,
    "error": "Failed to send SMS"
}
```

**SMS Status Values:**
- `queued` - Message queued for sending
- `sending` - Currently sending
- `sent` - Successfully sent to carrier
- `delivered` - Delivered to recipient
- `failed` - Failed to send
- `undelivered` - Sent but not delivered

**Example:**
```bash
curl -X POST https://api.example.com/api/phonehub/sms/send \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+14155551234",
    "body": "Your verification code is: 123456"
  }'
```

---

### List SMS Messages

Get a paginated list of SMS messages (sent and received).

**Endpoint:** `GET /api/phonehub/sms`

**Authentication:** Required

**Query Parameters:**
- `size` (or `limit`) - Number of results per page (default: 10)
- `start` (or `offset`) - Pagination offset (default: 0)
- `direction` - Filter by direction: "outbound" or "inbound"
- `status` - Filter by status: "queued", "sent", "delivered", "failed"
- `to_number` - Filter by recipient number
- `from_number` - Filter by sender number
- `search` - Search in message body

**Success Response (200):**
```json
{
    "status": true,
    "data": [
        {
            "id": 1001,
            "direction": "outbound",
            "from_number": "+14155556789",
            "to_number": "+14155551234",
            "body": "Your verification code is: 123456",
            "status": "delivered",
            "provider": "twilio",
            "provider_message_id": "SM1234567890abcdef",
            "sent_at": "2024-10-24T12:00:00Z",
            "delivered_at": "2024-10-24T12:00:05Z",
            "created": "2024-10-24T12:00:00Z"
        }
    ],
    "count": 500,
    "size": 10,
    "start": 0
}
```

**Example:**
```bash
curl -X GET "https://api.example.com/api/phonehub/sms?direction=outbound&status=delivered" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

### Get SMS Message

Retrieve a specific SMS message by ID.

**Endpoint:** `GET /api/phonehub/sms/<id>`

**Authentication:** Required

**Success Response (200):**
```json
{
    "status": true,
    "data": {
        "id": 1001,
        "direction": "outbound",
        "from_number": "+14155556789",
        "to_number": "+14155551234",
        "body": "Your verification code is: 123456",
        "status": "delivered",
        "provider": "twilio",
        "provider_message_id": "SM1234567890abcdef",
        "error_code": null,
        "error_message": null,
        "metadata": {},
        "sent_at": "2024-10-24T12:00:00Z",
        "delivered_at": "2024-10-24T12:00:05Z",
        "created": "2024-10-24T12:00:00Z",
        "modified": "2024-10-24T12:00:05Z"
    }
}
```

**Example:**
```bash
curl -X GET https://api.example.com/api/phonehub/sms/1001 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## Webhook Endpoints

### Twilio Incoming SMS Webhook

Receives incoming SMS messages from Twilio.

**Endpoint:** `POST /api/phonehub/sms/webhook/twilio`

**Authentication:** None (public endpoint - called by Twilio)

**Request Body (Twilio sends form-encoded data):**
```
From=+14155551234
To=+14155556789
Body=Hello, this is a reply
MessageSid=SM1234567890abcdef
```

**Response (200):**
```xml
<?xml version="1.0" encoding="UTF-8"?>
<Response></Response>
```

**Configuration:**

In your Twilio console, set your phone number's incoming message webhook to:
```
https://yourdomain.com/api/phonehub/sms/webhook/twilio
```

**Notes:**
- This endpoint automatically creates an SMS record with `direction: "inbound"`
- Returns empty TwiML response (you can customize to auto-reply)
- No authentication required - Twilio validates via IP whitelist

---

### Twilio Status Callback Webhook

Receives delivery status updates from Twilio.

**Endpoint:** `POST /api/phonehub/sms/webhook/twilio/status`

**Authentication:** None (public endpoint - called by Twilio)

**Request Body (Twilio sends form-encoded data):**
```
MessageSid=SM1234567890abcdef
MessageStatus=delivered
ErrorCode=30006  // only present if failed
ErrorMessage=Landline or unreachable carrier  // only present if failed
```

**Success Response (200):**
```json
{
    "success": true,
    "status": "delivered"
}
```

**Error Response (404):**
```json
{
    "status": false,
    "error": "SMS not found"
}
```

**Configuration:**

When sending SMS via Twilio API, set the status callback URL to:
```
https://yourdomain.com/api/phonehub/sms/webhook/twilio/status
```

**Twilio Status Mapping:**
- `queued` → `queued`
- `sending` → `sending`
- `sent` → `sent`
- `delivered` → `delivered`
- `failed` → `failed`
- `undelivered` → `undelivered`

---

## Common Response Codes

| Code | Description |
|------|-------------|
| 200  | Success |
| 400  | Bad Request - Missing or invalid parameters |
| 401  | Unauthorized - Missing or invalid authentication token |
| 403  | Forbidden - User lacks required permissions |
| 404  | Not Found - Resource doesn't exist |
| 500  | Internal Server Error |

---

## Error Response Format

All error responses follow this format:

```json
{
    "status": false,
    "error": "Error message describing what went wrong"
}
```

---

## Rate Limiting

- Phone lookups count against your Twilio API quota
- Cached lookups do not count against quota
- SMS sending counts against your Twilio SMS quota
- Consider implementing rate limiting in production

---

## Example Workflows

### Complete Phone Validation & Lookup Flow

```bash
# 1. Normalize user input
curl -X POST https://api.example.com/api/phonehub/number/normalize \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone_number": "(415) 555-1234"}'

# Response: {"status": true, "data": {"phone_number": "+14155551234"}}

# 2. Lookup detailed information
curl -X POST https://api.example.com/api/phonehub/number/lookup \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone_number": "+14155551234"}'

# Response includes carrier, line_type, registered_owner, etc.
```

### Send SMS with Delivery Tracking

```bash
# 1. Send SMS
curl -X POST https://api.example.com/api/phonehub/sms/send \
  -H "Authorization: Bearer TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+14155551234",
    "body": "Your code: 123456"
  }'

# Response: {"status": true, "data": {"id": 1001, "status": "queued", ...}}

# 2. Check delivery status later
curl -X GET https://api.example.com/api/phonehub/sms/1001 \
  -H "Authorization: Bearer TOKEN"

# Response shows updated status (sent, delivered, etc.)
```

---

## Testing

### Test with cURL

```bash
# Set your token
TOKEN="your_jwt_token_here"

# Normalize a number
curl -X POST http://localhost:8000/api/phonehub/number/normalize \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone_number": "4155551234"}'

# Lookup a number
curl -X POST http://localhost:8000/api/phonehub/number/lookup \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"phone_number": "+14155551234"}'

# Send SMS
curl -X POST http://localhost:8000/api/phonehub/sms/send \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+14155551234",
    "body": "Test message"
  }'
```

### Test with Python

```python
import requests

BASE_URL = "https://api.example.com/api/phonehub"
TOKEN = "your_jwt_token_here"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# Lookup phone number
response = requests.post(
    f"{BASE_URL}/number/lookup",
    headers=HEADERS,
    json={"phone_number": "+14155551234"}
)
print(response.json())

# Send SMS
response = requests.post(
    f"{BASE_URL}/sms/send",
    headers=HEADERS,
    json={
        "to_number": "+14155551234",
        "body": "Hello from PhoneHub!"
    }
)
print(response.json())
```

---

## Production Considerations

1. **Webhook Security**
   - Configure Twilio IP whitelist
   - Validate webhook signatures
   - Use HTTPS endpoints only

2. **Caching**
   - Default 90-day cache reduces API costs
   - Use `force_refresh: true` only when needed
   - Monitor cache hit rates

3. **Error Handling**
   - Implement retry logic for failed SMS
   - Monitor delivery rates
   - Set up alerts for high failure rates

4. **Rate Limiting**
   - Implement per-user rate limits
   - Queue SMS for bulk sending
   - Monitor API quota usage

5. **Monitoring**
   - Track SMS delivery rates
   - Monitor phone lookup cache hit ratio
   - Alert on webhook failures
   - Log error codes and messages
