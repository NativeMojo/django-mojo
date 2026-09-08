# Location Helpers

`mojo.helpers.location` validates US postal addresses and provides Google
Places autocomplete/detail helpers. Address validation prefers USPS and has a
single, one-way fallback to Google.

## Configuration

Configure Google for every installation that uses address validation:

```python
GOOGLE_MAPS_API_KEY = "..."
```

USPS is attempted only when both credentials are present:

```python
USPS_CLIENT_ID = "..."
USPS_CLIENT_SECRET = "..."
```

If either USPS value is absent or empty, validation goes directly to Google.
This is not a failed USPS attempt, so it does not emit a failover warning.

## Validate an address

```python
from mojo.helpers.location import validate_address

result = validate_address({
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "postal_code": "94043",
})
```

The optional `provider` input controls routing:

| Value | Behavior |
|---|---|
| omitted or `"usps"` | Try USPS when both credentials exist, then Google if USPS raises or does not return `valid is True`. |
| `"google"` | Try Google only. Google never falls back to USPS. |
| any other value | Compatibility behavior: use the same USPS-first route as the default. |

Only the boolean value `True` in a provider mapping counts as successful
validation. A missing, false, or non-boolean `valid` value is a provider
failure. The router copies provider mappings before adding its own fields, so
provider-owned objects are not mutated.

Every result includes `provider`, identifying the provider whose response
governs the result. Provider-native fields such as `valid`, `source`,
`standardized_address`, `corrections`, `metadata`, and `original_address` are
preserved.

Example success:

```json
{
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
```

## Fallback and final-provider policy

A configured USPS attempt falls through to Google when it raises any ordinary
exception, returns a non-mapping value, or returns a mapping whose `valid`
value is not exactly `True`. There is at most one call to each provider.

The final provider's policy is authoritative. In particular, Google may return
a success after USPS rejects an address for missing secondary information,
vacancy, or CMRA status. Consumers must use the returned `provider` and final
payload rather than assuming USPS policy governed a default request.

Each USPS-to-Google transition emits one warning with the provider name,
failure class, and bounded status only. The warning never includes the address,
credentials, or provider error text.

## Failure response

When all attempted providers fail, the final provider's native failure mapping
is retained and the router adds consistent attribution:

```json
{
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
```

`error` is always the final provider's message. `errors` contains every
attempted provider in attempt order. A direct Google failure therefore has
only `errors.google`. If a provider raises or returns a non-mapping value, the
router synthesizes the same `status`, `provider`, `error`, and `errors` fields.

## Autocomplete and place details

`get_address_suggestions()` and `get_place_details()` remain Google-only and
do not participate in address-validation fallback:

```python
from mojo.helpers.location import get_address_suggestions, get_place_details

suggestions = get_address_suggestions("1600 Amph")
details = get_place_details(suggestions["data"][0]["place_id"])
```
