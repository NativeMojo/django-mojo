# PhoneHub Quick Reference

## Import

```python
from mojo.apps import phonehub
```

## Three Main Functions

```python
# Normalize phone number to E.164 format
num = phonehub.normalize(phone_number)

# Lookup phone info (carrier, mobile/voip, etc.)
phone = phonehub.lookup(phone_number, group=None)

# Send SMS
sms = phonehub.send_sms(phone_number, message, group=None)
```

## Common Patterns

### Validate and Send
```python
num = phonehub.normalize(user_input)
if num:
    sms = phonehub.send_sms(num, 'Your code: 123456')
```

### Check Phone Type
```python
phone = phonehub.lookup('+14155551234')
if phone.is_mobile:
    # Send SMS
    phonehub.send_sms(phone.phone_number, 'Hello!')
elif phone.is_voip:
    # Handle VOIP differently
    pass
```

### Organization Context
```python
# Use org-specific config
sms = phonehub.send_sms(
    '+14155551234',
    'Message from ACME',
    group=request.group
)
```

## Model Access

```python
# Access models if needed
phone = phonehub.PhoneNumber.objects.get(phone_number='+14155551234')
config = phonehub.PhoneConfig.get_for_group(my_group)
sms_list = phonehub.SMS.objects.filter(user=request.user)
```

## Setup (One-time)

```python
from mojo.apps.phonehub.models import PhoneConfig

config = PhoneConfig.objects.create(
    name="My Config",
    provider='twilio',
    twilio_from_number='+14155551234',
    test_mode=True
)

config.set_twilio_credentials('ACxxxx', 'token')
config.save()
```

## REST API

```bash
# Normalize
POST /api/phonehub/phone/normalize
{"phone_number": "415-555-1234"}

# Lookup
POST /api/phonehub/phone/lookup
{"phone_number": "+14155551234"}

# Send SMS
POST /api/phonehub/sms/send
{"to_number": "+14155551234", "body": "Hello!"}
```

## That's It!

See README.md for full docs, QUICKSTART.md for setup, EXAMPLES.md for more patterns.
