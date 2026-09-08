# Location API

The public Location API validates US postal addresses. Validation prefers USPS
when it is configured and can fall back once to Google. The provider whose
answer governs the result is always visible in `data.provider`.

## Validate an address

```http
POST /api/location/address/validate
Content-Type: application/json
```

No authentication is required.

### Request body

| Field | Required | Description |
|---|---:|---|
| `address1` | yes | Street address. |
| `address2` | no | Apartment, suite, or other secondary information. |
| `city` | no | City name; include it whenever available. |
| `state` | yes | Two-letter state code. |
| `postal_code` | no | Five-digit ZIP code. |
| `provider` | no | `"usps"` (the default route) or `"google"`. |

Provider routing is one-way:

- Omitted or `"usps"`: USPS is tried when both USPS credentials are
  configured. A USPS exception or any result other than `valid: true` causes
  one Google attempt. If USPS is unavailable because either credential is
  absent, the request goes directly to Google.
- `"google"`: Google only. Google never falls back to USPS.
- Other values retain legacy compatibility and follow the default USPS-first
  route.

### Successful validation

```json
{
  "status": true,
  "data": {
    "valid": true,
    "provider": "usps",
    "source": "usps_v3",
    "standardized_address": {
      "line1": "1600 AMPHITHEATRE PKWY",
      "city": "MOUNTAIN VIEW",
      "state": "CA",
      "postal_code": "94043"
    }
  }
}
```

`data.provider` is `"usps"` or `"google"` and names the provider that produced
the final result. Other fields are provider-native and may include `source`,
`standardized_address`, `corrections`, `metadata`, and `original_address`.

The final provider's validation policy governs. Google may accept an address
that USPS rejected for missing secondary information, vacancy, or CMRA status;
in that case the response is a Google success with `data.provider: "google"`.

### Validation failure

A provider-level failure is returned inside the ordinary REST envelope. Check
`data.valid`/`data.status`, not only the envelope's `status`:

```json
{
  "status": true,
  "data": {
    "valid": false,
    "source": "google_address_validation",
    "status": false,
    "provider": "google",
    "error": "Google could not validate address",
    "errors": {
      "usps": "USPS could not confirm delivery",
      "google": "Google could not validate address"
    }
  }
}
```

The final provider's native failure fields remain in `data`. `data.error` is
that provider's message, while `data.errors` includes every attempted provider
in attempt order. An explicit-Google failure contains only `errors.google`.

The endpoint returns an outer `status: false` with HTTP 400 only when request
handling itself raises outside provider validation:

```json
{"status": false, "error": "..."}
```
