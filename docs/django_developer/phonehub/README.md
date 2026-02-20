# PhoneHub — Django Developer Reference

PhoneHub provides phone number management, validation, carrier lookup, and SMS messaging.

## Models

### PhoneNumber

System-wide cache of phone number lookup data. Not tied to individual users.

```python
from mojo.apps.phonehub.models import PhoneNumber

# Normalize to E.164
normalized = PhoneNumber.normalize("+1 415-555-1234")  # "+14155551234"

# Lookup with carrier/type data (caches result, refreshes every 90 days)
phone = PhoneNumber.lookup("+14155551234")
phone.carrier        # "AT&T"
phone.line_type      # "mobile", "landline", or "voip"
phone.is_valid       # True/False
phone.caller_name    # Registered name (if available)

# Force refresh from carrier
phone.refresh()
```

### SMS

Audit trail for sent/received SMS messages.

| Field | Type | Description |
|---|---|---|
| `user` | FK → User | Associated user |
| `group` | FK → Group | Associated group |
| `from_number` | CharField | Sender phone number |
| `to_number` | CharField | Recipient phone number |
| `body` | TextField | Message body |
| `direction` | CharField | `inbound` or `outbound` |
| `status` | CharField | `queued`, `sending`, `sent`, `delivered`, `failed` |
| `provider_sid` | CharField | Twilio message SID |

## Sending SMS

```python
from mojo.apps.phonehub.services import twilio

# Send a message
sms = twilio.send_sms(
    to="+14155551234",
    body="Your verification code is 483921",
    from_number="+18005551234"  # optional, uses default
)
```

## REST Endpoints

```python
# Standard CRUD
@md.URL('number')
@md.URL('number/<int:pk>')
def on_phone(request, pk=None):
    return PhoneNumber.on_rest_request(request, pk)

# Normalize endpoint
@md.POST('number/normalize')
...

# Lookup endpoint
@md.POST('number/lookup')
...
```

## RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["view_phonehub", "manage_phonehub"]
    SAVE_PERMS = ["manage_phonehub"]
    SEARCH_FIELDS = ["phone_number", "carrier", "caller_name"]
```

## Settings

| Setting | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_DEFAULT_FROM` | Default sending number |
| `PHONEHUB_LOOKUP_TTL` | Days before re-lookup (default 90) |
