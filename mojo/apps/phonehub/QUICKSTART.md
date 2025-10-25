# PhoneHub Quick Start Guide

Get up and running with PhoneHub in 5 minutes.

## Prerequisites

```bash
# Install required libraries
pip install twilio      # For Twilio support
pip install boto3       # For AWS support (optional)
```

## Step 1: Run Migrations

From your Django project (not the framework):

```bash
python manage.py makemigrations phonehub
python manage.py migrate phonehub
```

## Step 2: Create Configuration

### Option A: Using Django Shell

```bash
python manage.py shell
```

```python
from mojo.apps.phonehub.models import PhoneConfig

# Create Twilio config
config = PhoneConfig.objects.create(
    name="My Twilio Config",
    provider='twilio',
    twilio_from_number='+14155551234',  # Your Twilio number
    lookup_enabled=True,
    lookup_cache_days=30,
    test_mode=True  # Start in test mode
)

# Set credentials
config.set_twilio_credentials(
    account_sid='ACxxxxxxxxxxxxxxxxxxxxx',
    auth_token='your_auth_token_here'
)
config.save()

# Test it
result = config.test_connection()
print(result)
```

### Option B: Using REST API

```bash
# Create config
curl -X POST http://localhost:8000/api/phonehub/config \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Twilio Config",
    "provider": "twilio",
    "twilio_from_number": "+14155551234",
    "lookup_enabled": true,
    "test_mode": true
  }'

# Set credentials (assuming config ID is 1)
curl -X POST http://localhost:8000/api/phonehub/config/1/credentials \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "twilio",
    "twilio_account_sid": "ACxxxxxxxxxxxxx",
    "twilio_auth_token": "your_token"
  }'

# Test connection
curl -X POST http://localhost:8000/api/phonehub/config/1/test \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Step 3: Send Your First SMS

### Using Python (Simple API)

```python
from mojo.apps import phonehub

# Simple one-liner
sms = phonehub.send_sms('+14155559999', 'Hello from PhoneHub!')

print(f"Status: {sms.status}")
print(f"Message ID: {sms.provider_message_id}")
```

### Using REST API

```bash
curl -X POST http://localhost:8000/api/phonehub/sms/send \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "to_number": "+14155559999",
    "body": "Hello from PhoneHub!"
  }'
```

## Step 4: Lookup Phone Numbers

### Using Python (Simple API)

```python
from mojo.apps import phonehub

phone = phonehub.lookup('+14155559999')

if phone:
    print(f"Carrier: {phone.carrier}")
    print(f"Line Type: {phone.line_type}")
    print(f"Is Mobile: {phone.is_mobile}")
    print(f"Is VOIP: {phone.is_voip}")
```

### Using REST API

```bash
curl -X POST http://localhost:8000/api/phonehub/phone/lookup \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "+14155559999"
  }'
```

## Step 5: Normalize Phone Numbers

### Using Python (Simple API)

```python
from mojo.apps import phonehub

# Various formats work
normalized = phonehub.normalize('415-555-9999')      # +14155559999
normalized = phonehub.normalize('(415) 555-9999')   # +14155559999
normalized = phonehub.normalize('4155559999')       # +14155559999
```

### Using REST API

```bash
curl -X POST http://localhost:8000/api/phonehub/phone/normalize \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "phone_number": "415-555-9999"
  }'
```

## Step 6: Receive SMS (Optional)

To receive incoming SMS, configure Twilio webhooks:

1. **Go to Twilio Console** → Phone Numbers → Your Number
2. **Set Messaging Webhook URL:**
   ```
   https://yourdomain.com/api/phonehub/sms/webhook/twilio
   ```
3. **Set Status Callback URL (optional):**
   ```
   https://yourdomain.com/api/phonehub/sms/webhook/twilio/status
   ```

Incoming messages will automatically be saved to the `SMS` model with `direction='inbound'`.

## Common Tasks

### Check SMS History

```python
from mojo.apps.phonehub.models import SMS

# Get all outbound SMS
outbound = SMS.objects.filter(direction='outbound')

# Get delivered messages
delivered = SMS.objects.filter(status='delivered')

# Get failed messages
failed = SMS.objects.filter(status='failed')
for sms in failed:
    print(f"Failed: {sms.to_number} - {sms.error_message}")
```

### Check if Phone Lookup is Stale

```python
from mojo.apps import phonehub

# Get existing phone record
phone = phonehub.PhoneNumber.objects.get(phone_number='+14155559999')

if phone.needs_lookup:
    print("Lookup has expired, refreshing...")
    phone = phonehub.lookup(phone.phone_number, force_refresh=True)
```

### Disable Test Mode (Go Live!)

```python
from mojo.apps.phonehub.models import PhoneConfig

config = PhoneConfig.objects.get(id=1)
config.test_mode = False
config.save()

print("PhoneHub is now LIVE - SMS will be sent for real!")
```

### Create Organization-Specific Config

```python
from mojo.apps.phonehub.models import PhoneConfig
from mojo.apps.account.models import Group

# Get your organization
my_org = Group.objects.get(name='ACME Corp')

# Create org-specific config
config = PhoneConfig.objects.create(
    name="ACME Twilio",
    group=my_org,  # Organization-specific
    provider='twilio',
    twilio_from_number='+14155551234'
)

config.set_twilio_credentials(
    account_sid='ACxxxxxxxxxxxxx',
    auth_token='token_for_acme'
)
config.save()

# Now when you send SMS with group=my_org, it will use this config
from mojo.apps import phonehub

sms = phonehub.send_sms('+14155559999', 'Message from ACME!', group=my_org)
```

## Troubleshooting

### "No phone config found"

**Problem:** No PhoneConfig exists or none is active.

**Solution:**
```python
from mojo.apps.phonehub.models import PhoneConfig

# Check existing configs
configs = PhoneConfig.objects.all()
print(f"Found {configs.count()} configs")

# Make sure at least one is active
config = PhoneConfig.objects.first()
config.is_active = True
config.save()
```

### "Twilio credentials not configured"

**Problem:** Credentials haven't been set or are empty.

**Solution:**
```python
config = PhoneConfig.objects.get(id=1)
config.set_twilio_credentials(
    account_sid='ACxxxxxxxxxxxxx',
    auth_token='your_token'
)
config.save()

# Test it
result = config.test_connection()
print(result)
```

### "Invalid phone number"

**Problem:** Phone number format is not recognized.

**Solution:** Always use E.164 format (+1234567890) or use normalize():
```python
from mojo.apps import phonehub

normalized = phonehub.normalize('your_number_here')
if normalized:
    # Use normalized number
    phonehub.send_sms(normalized, 'Hello!')
else:
    print("Invalid phone number format")
```

## Next Steps

- Read the full [README.md](README.md) for comprehensive documentation
- Check out the REST API endpoints for integration
- Configure webhooks for receiving SMS
- Set up organization-specific configs for multi-tenant use
- Explore the `metadata` field for custom tracking/analytics

## Support

For issues or questions:
1. Check the [README.md](README.md) for detailed documentation
2. Review the code in `mojo/apps/phonehub/`
3. Check Django logs for error details
