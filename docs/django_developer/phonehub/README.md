# PhoneHub — Django Developer Reference

PhoneHub provides phone number normalization, carrier lookup, SMS sending, and inbound webhook handling.

## Sections

- [models.md](models.md) — PhoneNumber, SMS, PhoneConfig
- [rest.md](rest.md) — All REST endpoints

## Quick Usage

Import the `phonehub` module for all common operations:

```python
from mojo.apps import phonehub
```

### Normalize a phone number

```python
normalized = phonehub.normalize("+1 (415) 555-1234")
# "+14155551234"

normalized = phonehub.normalize("4155551234")
# "+14155551234"  (NANP assumed for 10-digit numbers)

normalized = phonehub.normalize("bad-number")
# None
```

### Validate a phone number

```python
is_valid = phonehub.validate("+14155551234")
# True

# Detailed validation result
result = phonehub.validate("+14155551234", detailed=True)
# objict with validation breakdown
```

### Look up carrier and line type

```python
phone = phonehub.lookup("+14155551234")
phone.carrier           # "AT&T"
phone.line_type         # "mobile"
phone.is_mobile         # True
phone.is_voip           # False
phone.is_valid          # True
phone.registered_owner  # "John Smith" (CNAM, if available)
phone.owner_type        # "CONSUMER" or "BUSINESS"
phone.country_code      # "US"
phone.state             # "CA"
```

Results are cached in the database and refreshed automatically after `lookup_cache_days` (default 90 days). To force a re-fetch:

```python
from mojo.apps.phonehub.models import PhoneNumber
phone = PhoneNumber.lookup("+14155551234")
phone.refresh()  # force re-fetch from provider
```

### Send an SMS

```python
sms = phonehub.send_sms(
    phone_number="+14155551234",
    message="Your verification code is 483921",
)
sms.status              # "sent" or "failed"
sms.provider_message_id # provider SID/ID

# With optional context
sms = phonehub.send_sms(
    phone_number="+14155551234",
    message="Welcome to the platform!",
    user=request.user,
    group=request.group,
    from_number="+18005551234",  # override default sender
    metadata={"template": "welcome"},
)
```

Numbers starting with `+1555` are treated as test numbers — no real SMS is sent, the record is saved with `is_test=True`.

### Area code information

```python
info = phonehub.get_area_code_info("+14155551234")
info.area_code   # "415"
info.type        # "geographic"
info.location    # "San Francisco"
info.description # "San Francisco, CA"

# Also works with just the area code string
info = phonehub.get_area_code_info("800")
info.type        # "toll_free"
```

### Detect country

```python
country = phonehub.detect_country("+447911123456")
country.country_code  # "44"
country.country       # "United Kingdom"
country.is_nanp       # False
```

## Models

See [models.md](models.md) for full field reference on `PhoneNumber`, `SMS`, and `PhoneConfig`.

## REST API

See [rest.md](rest.md) for all endpoint details, or the [web developer docs](../../web_developer/phonehub/README.md) for the client-facing reference.

## Settings

| Setting | Description |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_NUMBER` | Default sender phone number |
| `SMS_INBOUND_HANDLER` | Optional dotted path to inbound SMS handler (e.g. `"myapp.services.on_sms"`) |

### Inbound SMS handler

If `SMS_INBOUND_HANDLER` is set, it is called whenever an inbound SMS is received via the Twilio webhook:

```python
# myapp/services/sms.py
def on_sms(sms):
    # sms is a saved SMS instance with direction="inbound"
    if "HELP" in sms.body.upper():
        return "Reply STOP to unsubscribe."
    # return a string to auto-reply, or None for no reply
```

## Provider Configuration

PhoneHub supports Twilio (default) and AWS SNS. Credentials can be stored globally in Django settings, or per-group via `PhoneConfig` (which encrypts credentials using MojoSecrets).

See [models.md — PhoneConfig](models.md#phoneconfig) for credential management.
