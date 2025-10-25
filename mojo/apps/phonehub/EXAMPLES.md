# PhoneHub Usage Examples

Quick examples showing the clean, simple API.

## Basic Import

```python
from mojo.apps import phonehub
```

## Normalize Phone Numbers

```python
# Various input formats work
num = phonehub.normalize('415-555-1234')           # +14155551234
num = phonehub.normalize('(415) 555-1234')         # +14155551234
num = phonehub.normalize('4155551234')             # +14155551234
num = phonehub.normalize('+1 415 555 1234')        # +14155551234

# Check if valid
if num:
    print(f"Normalized: {num}")
else:
    print("Invalid phone number")
```

## Lookup Phone Information

```python
# Simple lookup
phone = phonehub.lookup('+14155551234')

if phone:
    print(f"Carrier: {phone.carrier}")
    print(f"Line Type: {phone.line_type}")
    print(f"Mobile: {phone.is_mobile}")
    print(f"VOIP: {phone.is_voip}")
    print(f"Valid: {phone.is_valid}")

# Lookup with organization context
phone = phonehub.lookup('+14155551234', group=my_group)

# Force fresh lookup (bypass cache)
from mojo.apps.phonehub.services import lookup_phone
phone = lookup_phone('+14155551234', force_refresh=True)
```

## Send SMS

```python
# Simple send
sms = phonehub.send_sms('+14155551234', 'Hello!')

# Check status
if sms.status == 'sent':
    print(f"Sent! Message ID: {sms.provider_message_id}")
else:
    print(f"Failed: {sms.error_message}")

# Send with organization context
sms = phonehub.send_sms(
    '+14155551234',
    'Hello from ACME Corp!',
    group=my_group
)

# Send with user tracking
sms = phonehub.send_sms(
    '+14155551234',
    'Your verification code is: 123456',
    user=request.user,
    group=request.group
)

# Send with metadata
from mojo.apps.phonehub.services import send_sms
sms = send_sms(
    to_number='+14155551234',
    body='Your order has shipped!',
    user=request.user,
    metadata={'order_id': 12345, 'campaign': 'shipping_notifications'}
)
```

## Validate and Send SMS

```python
# Normalize and validate before sending
phone_input = request.DATA.get('phone_number')
normalized = phonehub.normalize(phone_input)

if not normalized:
    return {'error': 'Invalid phone number'}

# Optional: lookup to verify it's a mobile number
phone = phonehub.lookup(normalized)
if phone and not phone.is_mobile:
    return {'error': 'SMS can only be sent to mobile numbers'}

# Send SMS
sms = phonehub.send_sms(normalized, 'Your verification code is: 123456')
return {'success': True, 'message_id': sms.provider_message_id}
```

## Check Lookup Cache Status

```python
# Get phone record
phone = phonehub.PhoneNumber.objects.get(phone_number='+14155551234')

# Check if lookup needs refresh
if phone.needs_lookup:
    print(f"Lookup expired on {phone.lookup_expires_at}")
    print("Refreshing...")
    phone = phonehub.lookup(phone.phone_number, force_refresh=True)
else:
    days_left = (phone.lookup_expires_at - timezone.now()).days
    print(f"Cached lookup valid for {days_left} more days")
```

## Query SMS History

```python
# Get all SMS for a user
user_sms = phonehub.SMS.objects.filter(user=request.user)

# Get recent outbound SMS
from django.utils import timezone
from datetime import timedelta

last_week = timezone.now() - timedelta(days=7)
recent_sms = phonehub.SMS.objects.filter(
    direction='outbound',
    created__gte=last_week
).order_by('-created')

# Get failed messages
failed = phonehub.SMS.objects.filter(status='failed')
for sms in failed:
    print(f"Failed to {sms.to_number}: {sms.error_message}")

# Get delivered messages to a specific number
delivered = phonehub.SMS.objects.filter(
    to_number='+14155551234',
    status='delivered'
)
```

## Check Delivery Status

```python
# Send SMS and track
sms = phonehub.send_sms('+14155551234', 'Hello!')

# Initial status
print(f"Status: {sms.status}")  # 'sent' or 'queued'

# Later, check updated status (updated via webhook)
sms.refresh_from_db()
if sms.is_delivered:
    print(f"Delivered at {sms.delivered_at}")
elif sms.is_failed:
    print(f"Failed: {sms.error_message}")
```

## Filter Mobile Numbers from List

```python
phone_numbers = ['+14155551234', '+14155556789', '+14155559999']

mobile_numbers = []
for num in phone_numbers:
    phone = phonehub.lookup(num)
    if phone and phone.is_mobile:
        mobile_numbers.append(num)

print(f"Found {len(mobile_numbers)} mobile numbers")

# Send SMS to all mobile numbers
for num in mobile_numbers:
    phonehub.send_sms(num, 'Special mobile offer!')
```

## Validate VOIP Numbers

```python
# Check if number is VOIP (might want to block)
phone_input = '+14155551234'
phone = phonehub.lookup(phone_input)

if phone and phone.is_voip:
    print("Warning: This is a VOIP number")
    # Decide whether to allow SMS to VOIP numbers
else:
    sms = phonehub.send_sms(phone_input, 'Your code: 123456')
```

## REST API Examples

### Send SMS via REST
```bash
curl -X POST http://localhost:8000/api/phonehub/sms/send \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+14155551234",
    "body": "Hello from API!"
  }'
```

### Lookup Phone via REST
```bash
curl -X POST http://localhost:8000/api/phonehub/phone/lookup \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+14155551234"
  }'
```

### Normalize Phone via REST
```bash
curl -X POST http://localhost:8000/api/phonehub/phone/normalize \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "415-555-1234"
  }'
```

## Working with Organizations

```python
from mojo.apps.account.models import Group

# Get organization
acme_corp = Group.objects.get(name='ACME Corp')

# Lookup with org context (uses org-specific config)
phone = phonehub.lookup('+14155551234', group=acme_corp)

# Send SMS with org context
sms = phonehub.send_sms(
    '+14155551234',
    'Message from ACME Corp',
    group=acme_corp,
    user=request.user
)

# Query org's SMS history
org_sms = phonehub.SMS.objects.filter(group=acme_corp)
```

## Error Handling

```python
# Normalize with validation
phone_input = request.DATA.get('phone')
normalized = phonehub.normalize(phone_input)

if not normalized:
    return {'error': 'Invalid phone number format'}

# Lookup with error handling
phone = phonehub.lookup(normalized)
if not phone:
    return {'error': 'Unable to lookup phone number'}

if not phone.is_valid:
    return {'error': 'Phone number is not valid/reachable'}

# Send with error handling
sms = phonehub.send_sms(normalized, 'Your code: 123456')

if not sms:
    return {'error': 'Failed to send SMS'}

if sms.is_failed:
    return {'error': f'SMS failed: {sms.error_message}'}

return {'success': True, 'message_id': sms.provider_message_id}
```

## Bulk SMS Example

```python
# Send to multiple recipients
recipients = ['+14155551234', '+14155556789', '+14155559999']
message = 'Important update from ACME Corp'

results = []
for number in recipients:
    # Normalize
    normalized = phonehub.normalize(number)
    if not normalized:
        results.append({'number': number, 'status': 'invalid'})
        continue
    
    # Send
    sms = phonehub.send_sms(normalized, message, user=request.user)
    results.append({
        'number': normalized,
        'status': sms.status,
        'message_id': sms.provider_message_id
    })

# Summary
sent = len([r for r in results if r['status'] == 'sent'])
print(f"Sent {sent}/{len(recipients)} messages")
```

## Integration with User Model

```python
# Add phone to user profile
user = request.user
phone_input = '+14155551234'

# Validate and normalize
normalized = phonehub.normalize(phone_input)
if normalized:
    user.phone = normalized
    user.save()
    
    # Lookup and store info
    phone = phonehub.lookup(normalized, user=user)

# Send SMS to user
def send_user_notification(user, message):
    if user.phone:
        return phonehub.send_sms(user.phone, message, user=user)
    return None
```

## Testing Mode

```python
from mojo.apps.phonehub.models import PhoneConfig

# Enable test mode
config = PhoneConfig.objects.get(id=1)
config.test_mode = True
config.save()

# SMS will be logged but not actually sent
sms = phonehub.send_sms('+14155551234', 'Test message')
print(f"Test mode: {sms.is_test}")  # True
print(f"Status: {sms.status}")      # 'sent' (but not really sent)

# Disable test mode for production
config.test_mode = False
config.save()
```
