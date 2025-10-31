# Location & Address API

The Location API provides address validation, autocomplete, geocoding, and timezone services for US addresses. It integrates with USPS (for validation) and Google Maps Platform (for autocomplete and geocoding).

**Permissions:** Most endpoints require authentication. A public sample endpoint is provided for testing.

---

## Overview

The Location API supports the following operations:

1. **Address Validation** - Validate and standardize US addresses using USPS or Google
2. **Address Autocomplete** - Get real-time address suggestions as users type
3. **Place Details** - Get full address details after user selects an autocomplete suggestion
4. **Geocoding** - Convert addresses to coordinates (latitude/longitude)
5. **Reverse Geocoding** - Convert coordinates to addresses
6. **Timezone Lookup** - Get timezone information for geographic coordinates

---

## Configuration

### USPS Configuration

Set in your Django settings:

```python
USPS_CLIENT_ID = "your_consumer_key"
USPS_CLIENT_SECRET = "your_consumer_secret"
USPS_USE_TEST_ENVIRONMENT = False  # Set to True for testing
```

Get credentials from: https://developer.usps.com/

### Google Maps Configuration

Set in your Django settings:

```python
GOOGLE_MAPS_API_KEY = "your_api_key"
```

Get API key from: https://console.cloud.google.com/google/maps-apis/

Enable these APIs:
- Address Validation API
- Places API
- Geocoding API
- Time Zone API

---

## 1. Address Validation

Validate and standardize a US address using USPS (default) or Google.

### Endpoint

**POST** `/api/location/address/validate`

### Authentication

Requires authentication.

### Request Body

```json
{
  "address1": "123 Main St",
  "address2": "Apt 4B",
  "city": "San Francisco",
  "state": "CA",
  "postal_code": "94102",
  "provider": "usps"
}
```

**Parameters:**

- `address1` (string, required) - Street address
- `address2` (string, optional) - Apartment, suite, unit number
- `city` (string, optional*) - City name (*required if postal_code not provided)
- `state` (string, required) - Two-letter state code (e.g., "CA", "NY")
- `postal_code` (string, optional) - 5-digit ZIP code
- `provider` (string, optional) - "usps" (default) or "google"

### Response (USPS)

```json
{
  "status": true,
  "data": {
    "valid": true,
    "source": "usps_v3",
    "standardized_address": {
      "line1": "123 MAIN ST",
      "line1_abbreviated": "123 MAIN ST",
      "line2": "APT 4B",
      "city": "SAN FRANCISCO",
      "city_abbreviated": "SF",
      "state": "CA",
      "postal_code": "94102",
      "zip4": "1234",
      "full_zip": "94102-1234",
      "urbanization": null
    },
    "metadata": {
      "residential": true,
      "business": false,
      "deliverable": true,
      "vacant": false,
      "carrier_route": "C001",
      "delivery_point": "23",
      "dpv_confirmation": "Y",
      "cmra": false,
      "central_delivery_point": false,
      "exact_match": true,
      "needs_secondary_info": false,
      "multiple_addresses_found": false
    },
    "corrections": {
      "corrections_applied": false,
      "correction_codes": [],
      "correction_details": []
    },
    "matches": {
      "match_codes": ["31"],
      "match_details": [
        {
          "code": "31",
          "description": "Single Response - exact match"
        }
      ]
    },
    "warnings": [],
    "firm": null,
    "original_address": {...}
  }
}
```

**Key Fields:**

- `valid` (boolean) - Whether address is valid and deliverable
- `standardized_address` - USPS-standardized address format
- `metadata.residential` - Is this a residential address?
- `metadata.business` - Is this a business address?
- `metadata.dpv_confirmation` - Delivery Point Validation status ("Y" = confirmed)
- `metadata.exact_match` - Is this an exact match?
- `metadata.cmra` - Is this a Commercial Mail Receiving Agency (PO Box equivalent)?

### Example: Invalid Address

```json
{
  "status": true,
  "data": {
    "valid": false,
    "error": "Address not found in USPS database",
    "original_address": {...}
  }
}
```

### cURL Example

```bash
curl -X POST https://api.example.com/api/location/address/validate \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "postal_code": "94043",
    "provider": "usps"
  }'
```

---

## 2. Address Autocomplete

Get address suggestions in real-time as users type. Uses Google Places Autocomplete API.

### Endpoint

**GET** `/api/location/address/suggestions`

### Authentication

Requires authentication.

### Query Parameters

- `input` (string, required) - Partial address text (minimum 3 characters)
- `session_token` (string, optional) - Session token for billing optimization. If not provided, a new one is generated and returned.
- `country` (string, optional) - ISO country code (default: "US")
- `lat` (float, optional) - Latitude for location bias
- `lng` (float, optional) - Longitude for location bias
- `radius` (int, optional) - Radius in meters for location bias

### Response

```json
{
  "success": true,
  "session_token": "550e8400-e29b-41d4-a716-446655440000",
  "data": [
    {
      "id": "ChIJj61dQgK6j4AR4GeTYWZsKWw",
      "place_id": "ChIJj61dQgK6j4AR4GeTYWZsKWw",
      "description": "1600 Amphitheatre Parkway, Mountain View, CA, USA",
      "main_text": "1600 Amphitheatre Parkway",
      "secondary_text": "Mountain View, CA, USA",
      "types": ["street_address"]
    },
    {
      "id": "ChIJAQBsW0K6j4ARUkKeZqCqMQI",
      "place_id": "ChIJAQBsW0K6j4ARUkKeZqCqMQI",
      "description": "1600 Amphitheatre Circle, Mountain View, CA, USA",
      "main_text": "1600 Amphitheatre Circle",
      "secondary_text": "Mountain View, CA, USA",
      "types": ["street_address"]
    }
  ],
  "size": 2,
  "count": 2
}
```

**Response Fields:**

- `success` (boolean) - Whether request succeeded
- `session_token` (string) - Session token to reuse for subsequent requests
- `data` (array) - List of address suggestions
- `id` / `place_id` (string) - Unique identifier for the place (same value, `id` for UI compatibility)
- `description` (string) - Full address description
- `main_text` (string) - Primary address line
- `secondary_text` (string) - City, state, country
- `size` / `count` (int) - Number of suggestions returned

### Session Tokens (Cost Optimization)

Google charges **$2.83/1000 sessions** with session tokens vs **$17/1000 requests** without them.

**The API automatically handles session tokens for you:**

1. First autocomplete request (no `session_token` parameter) → API generates and returns a `session_token`
2. Subsequent autocomplete requests → Include `session_token` from first response
3. Place details request → Use same `session_token` to complete the session
4. New address entry → Omit `session_token` to get a new one

**Important Notes:**

- Session tokens expire after a few minutes of inactivity (Google doesn't document exact time)
- A session token becomes invalid after use with Place Details
- Always start a fresh session (omit `session_token`) for each new address entry
- If a session is abandoned (user doesn't select), the token expires automatically
- Using an expired or reused token results in per-request billing (83% more expensive!)

### cURL Examples

**First request (no session_token):**
```bash
curl -X GET "https://api.example.com/api/location/address/suggestions?input=1600+Amph" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Subsequent request (reusing session_token from first response):**
```bash
curl -X GET "https://api.example.com/api/location/address/suggestions?input=1600+Amphitheatre&session_token=550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**With location bias:**
```bash
curl -X GET "https://api.example.com/api/location/address/suggestions?input=Main+St&lat=37.7749&lng=-122.4194&radius=5000" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## 3. Place Details

Get full address details for a place selected from autocomplete suggestions.

### Endpoint

**GET** `/api/location/address/place-details`

### Authentication

Requires authentication.

### Query Parameters

- `place_id` (string, required) - Place ID from autocomplete suggestion
- `session_token` (string, optional but recommended) - Same session token used in autocomplete

### Response

```json
{
  "success": true,
  "address": {
    "street_number": "1600",
    "street_name": "Amphitheatre Parkway",
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "county": "Santa Clara County",
    "state": "California",
    "state_code": "CA",
    "postal_code": "94043",
    "country": "United States",
    "country_code": "US",
    "formatted_address": "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA",
    "latitude": 37.4224764,
    "longitude": -122.0842499
  }
}
```

### cURL Example

```bash
curl -X GET "https://api.example.com/api/location/address/place-details?place_id=ChIJj61dQgK6j4AR4GeTYWZsKWw&session_token=abc-123" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## 4. Geocoding

Convert an address to geographic coordinates (latitude/longitude).

### Endpoint

**POST** `/api/location/address/geocode`

### Authentication

Requires authentication.

### Request Body

```json
{
  "address": "1600 Amphitheatre Parkway, Mountain View, CA"
}
```

Or with address components:

```json
{
  "address": {
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "postal_code": "94043"
  }
}
```

### Response

```json
{
  "success": true,
  "latitude": 37.4224764,
  "longitude": -122.0842499,
  "formatted_address": "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA",
  "place_id": "ChIJj61dQgK6j4AR4GeTYWZsKWw",
  "address_components": {
    "street_number": "1600",
    "street_name": "Amphitheatre Parkway",
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "California",
    "state_code": "CA",
    "postal_code": "94043",
    "country": "United States",
    "country_code": "US"
  }
}
```

### cURL Example

```bash
curl -X POST https://api.example.com/api/location/address/geocode \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "address": "1600 Amphitheatre Parkway, Mountain View, CA"
  }'
```

---

## 5. Reverse Geocoding

Convert geographic coordinates to an address.

### Endpoint

**GET** `/api/location/address/reverse-geocode`

### Authentication

Requires authentication.

### Query Parameters

- `lat` (float, required) - Latitude
- `lng` (float, required) - Longitude

### Response

```json
{
  "success": true,
  "formatted_address": "1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA",
  "place_id": "ChIJj61dQgK6j4AR4GeTYWZsKWw",
  "address_components": {
    "street_number": "1600",
    "street_name": "Amphitheatre Parkway",
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "California",
    "state_code": "CA",
    "postal_code": "94043",
    "country": "United States",
    "country_code": "US"
  }
}
```

### cURL Example

```bash
curl -X GET "https://api.example.com/api/location/address/reverse-geocode?lat=37.4224764&lng=-122.0842499" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## 6. Timezone Lookup

Get timezone information for geographic coordinates.

### Endpoint

**GET** `/api/location/timezone`

### Authentication

Requires authentication.

### Query Parameters

- `lat` (float, required) - Latitude
- `lng` (float, required) - Longitude
- `timestamp` (int, optional) - Unix timestamp (default: current time)

### Response

```json
{
  "success": true,
  "timezone_id": "America/Los_Angeles",
  "timezone_name": "Pacific Daylight Time",
  "raw_offset": -28800,
  "dst_offset": 3600,
  "total_offset": -25200
}
```

**Response Fields:**

- `timezone_id` (string) - IANA timezone identifier
- `timezone_name` (string) - Human-readable timezone name
- `raw_offset` (int) - Raw offset from UTC in seconds (without DST)
- `dst_offset` (int) - Daylight Saving Time offset in seconds
- `total_offset` (int) - Total offset from UTC in seconds (raw + dst)

### cURL Example

```bash
curl -X GET "https://api.example.com/api/location/timezone?lat=37.4224764&lng=-122.0842499" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

---

## 7. Sample Validation (Public)

Test address validation without authentication.

### Endpoint

**GET** `/api/location/address/validate-sample`

### Authentication

None required (public endpoint).

### Response

Returns a validated sample address (1600 Amphitheatre Parkway) for testing purposes.

```json
{
  "status": true,
  "data": {
    "valid": true,
    "source": "google",
    "standardized_address": {...}
  },
  "sample": true
}
```

### cURL Example

```bash
curl -X GET https://api.example.com/api/location/address/validate-sample
```

---

## End-to-End Workflow: Address Entry Form

Here's how to implement a complete address entry form with autocomplete and validation:

### Step 1: First Autocomplete Request (Get Session Token)

```javascript
// User types "1600 Amph"
let sessionToken = null;

const response = await fetch(
  `/api/location/address/suggestions?input=${encodeURIComponent(input)}`,
  {
    headers: { 'Authorization': `Bearer ${token}` }
  }
);

const result = await response.json();
sessionToken = result.session_token;  // Save for subsequent requests

// Display suggestions to user
result.data.forEach(suggestion => {
  console.log(suggestion.description);
  // Show in dropdown UI
});
```

### Step 2: Subsequent Autocomplete Requests (Reuse Session Token)

```javascript
// User continues typing "1600 Amphitheatre"
const response = await fetch(
  `/api/location/address/suggestions?input=${encodeURIComponent(input)}&session_token=${sessionToken}`,
  {
    headers: { 'Authorization': `Bearer ${token}` }
  }
);

const { success, data } = await response.json();

// Display updated suggestions
data.forEach(suggestion => {
  console.log(suggestion.description);
  // Show in dropdown UI
});
```

### Step 3: User Selects a Suggestion

```javascript
// User clicks on suggestion
const placeId = selectedSuggestion.place_id;

const response = await fetch(
  `/api/location/address/place-details?place_id=${placeId}&session_token=${sessionToken}`,
  {
    headers: { 'Authorization': `Bearer ${token}` }
  }
);

const { success, address } = await response.json();

// Populate form fields
document.getElementById('address1').value = address.address1;
document.getElementById('city').value = address.city;
document.getElementById('state').value = address.state_code;
document.getElementById('postal_code').value = address.postal_code;

// Clear session token for next address entry
sessionToken = null;
```

### Step 4: Validate on Submit (Optional)

```javascript
// Before final submission, validate with USPS
const response = await fetch('/api/location/address/validate', {
  method: 'POST',
  headers: {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    address1: address.address1,
    city: address.city,
    state: address.state_code,
    postal_code: address.postal_code,
    provider: 'usps'
  })
});

const { status, data } = await response.json();

if (!data.valid) {
  alert(`Address validation failed: ${data.error}`);
  return;
}

// Use standardized address from USPS
const standardized = data.standardized_address;
// Submit to your backend
```

---

## Python Helper Functions

The REST API wraps Python helper functions that can also be used directly in your Django application:

```python
from mojo.helpers import location

# Validate address
result = location.validate_address({
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "postal_code": "94043",
    "provider": "usps"
})

# Get autocomplete suggestions
suggestions = location.get_address_suggestions(
    input_text="1600 Amph",
    session_token="abc-123",
    country="US"
)

# Get place details
details = location.get_place_details(
    place_id="ChIJ...",
    session_token="abc-123"
)
```

---

## Error Handling

All endpoints return standard error responses:

```json
{
  "status": false,
  "error": "Error message description"
}
```

Or for autocomplete/geocoding endpoints:

```json
{
  "success": false,
  "error": "Error message description"
}
```

**Common Errors:**

- `400 Bad Request` - Missing required parameters or invalid input
- `401 Unauthorized` - Missing or invalid authentication token
- `429 Too Many Requests` - Rate limit exceeded
- `503 Service Unavailable` - External API (USPS/Google) unavailable

---

## Rate Limits and Costs

### USPS

- Access tokens valid for 8 hours
- Rate limits apply per client credentials
- Free for commercial use (requires approved account)

### Google Maps Platform

**Pricing (as of 2024):**
- Address Validation: $0.50 per 100 requests
- Autocomplete (with session tokens): $2.83 per 1000 sessions
- Autocomplete (without session tokens): $17.00 per 1000 requests
- Geocoding: $5.00 per 1000 requests
- Time Zone: $5.00 per 1000 requests

**Best Practices:**
- Always use session tokens for autocomplete to save 83% on costs
- Cache geocoding results when possible
- Use USPS for validation (free) and Google for autocomplete (better UX)

---

## Support

For issues or questions:
- USPS API Support: https://emailus.usps.com/s/web-tools-inquiry
- Google Maps Support: https://developers.google.com/maps/support
