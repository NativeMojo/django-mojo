# PhoneHub - SMS and Phone Lookup Service

A simple Django app providing SMS and phone lookup capabilities via Twilio and AWS SNS.

## Features

- **Phone Number Normalization**: Convert phone numbers to E.164 format
- **Phone Lookup**: Get carrier, line type (mobile/voip), validity, and **caller name** (registered owner)
- **Caller Name Verification**: Lookup registered customer name via Twilio CNAM
- **SMS Sending**: Send SMS via Twilio or AWS SNS
- **SMS Receiving**: Handle incoming SMS via webhooks
- **Caching**: Store lookup results with configurable expiration
- **Multi-provider**: Support for both Twilio and AWS
- **Multi-tenant**: Organization-specific or system-wide configurations

## Models

### PhoneNumber (`mojo.apps.phonehub.models.PhoneNumber`)
**Pure lookup cache - shared across entire system to minimize Twilio API charges.**

Stores phone number lookup data with caching and expiration. Not tied to users or groups - this is intentional to maximize cache hits and minimize costs. If the same number is looked up by different parts of your system, they all share the same cached data.

**Key Fields:**
- `phone_number`: E.164 formatted number (+1234567890) - unique across system
- `country_code`: Country code (US, CA, GB, etc.)
- `carrier`: Carrier/operator name
- `line_type`: mobile, landline, voip, etc.
- `is_mobile`, `is_voip`: Boolean flags
- `caller_name`: Registered owner/caller name from carrier (CNAM)
- `caller_type`: BUSINESS or CONSUMER
- `address_*`: Address fields if available (when present in carrier data)
- `lookup_expires_at`: When to re-lookup this number
- `lookup_count`: How many times this number has been looked up
- `lookup_data`: Raw provider response (JSON)

### PhoneConfig (`mojo.apps.phonehub.models.PhoneConfig`)
Phone service configuration with encrypted credentials (via MojoSecrets).

**Key Fields:**
- `group`: Organization (null = system default)
- `provider`: 'twilio' or 'aws'
- `twilio_from_number`: Default Twilio sending number
- `aws_region`: AWS region for SNS
- `lookup_enabled`: Enable/disable lookups
- `lookup_cache_days`: Days to cache lookup results (default: 30)
- `test_mode`: Don't send real SMS in test mode

**Secrets (encrypted in mojo_secrets):**
- Twilio: `twilio_account_sid`, `twilio_auth_token`
- AWS: `aws_access_key_id`, `aws_secret_access_key`

### SMS (`mojo.apps.phonehub.models.SMS`)
Stores sent and received SMS messages with delivery tracking.

**Key Fields:**
- `direction`: 'outbound' or 'inbound'
- `from_number`, `to_number`: Phone numbers
- `body`: Message content
- `status`: queued, sending, sent, delivered, failed, etc.
- `provider_message_id`: Provider's tracking ID
- `error_code`, `error_message`: Error details if failed

## Simple API Usage

PhoneHub provides a clean, simple API for common operations:

```python
from mojo.apps import phonehub

# Normalize phone number to E.164 format
num = phonehub.normalize('(415) 555-1234')  # Returns: +14155551234

# Lookup phone info (carrier, mobile/voip detection)
phone = phonehub.lookup('+14155551234')
print(f"Carrier: {phone.carrier}, Mobile: {phone.is_mobile}")

# Lookup with organization context
phone = phonehub.lookup('+14155551234', group=my_group)

# Send SMS
sms = phonehub.send_sms('+14155551234', 'Hello from PhoneHub!')
print(f"Status: {sms.status}")

# Send SMS with organization context
sms = phonehub.send_sms(
    '+14155551234', 
    'Hello!',
    group=my_group,
    user=request.user
)
```

## Services API

For more control, you can also use the service functions directly:

Located in `mojo.apps.phonehub.services.phone`:

### `normalize_phone(phone_number, country_code='US')`
Normalize phone number to E.164 format.

```python
from mojo.apps.phonehub.services import normalize_phone

normalized = normalize_phone('415-555-1234')  # Returns: +14155551234
```

### `lookup_phone(phone_number, group=None, force_refresh=False)`
Lookup phone number info (uses cached data if available and not expired).

```python
from mojo.apps.phonehub.services import lookup_phone

phone = lookup_phone('+14155551234', group=my_group, force_refresh=True)
print(f"Carrier: {phone.carrier}, Type: {phone.line_type}")
print(f"Mobile: {phone.is_mobile}, VOIP: {phone.is_voip}")
```

### `send_sms(to_number, body, from_number=None, group=None, user=None, metadata=None)`
Send SMS message via configured provider.

```python
from mojo.apps.phonehub.services import send_sms

sms = send_sms(
    to_number='+14155551234',
    body='Hello from PhoneHub!',
    group=my_group,
    user=request.user,
    metadata={'campaign_id': 123}
)
print(f"Status: {sms.status}, Message ID: {sms.provider_message_id}")
```

### `handle_incoming_sms(from_number, to_number, body, provider='twilio', ...)`
Handle incoming SMS (called by webhook handlers).

## REST API Endpoints

### Phone Number Operations

**Normalize Phone Number**
```
POST /api/phonehub/phone/normalize
{
    "phone_number": "415-555-1234",
    "country_code": "US"
}
```

**Lookup Phone Number**
```
POST /api/phonehub/phone/lookup
{
    "phone_number": "+14155551234",
    "force_refresh": false
}
```

**CRUD Phone Numbers**
```
GET /api/phonehub/phone          # List all
GET /api/phonehub/phone/123      # Get one
POST /api/phonehub/phone         # Create
PUT /api/phonehub/phone/123      # Update
DELETE /api/phonehub/phone/123   # Delete
```

### SMS Operations

**Send SMS**
```
POST /api/phonehub/sms/send
{
    "to_number": "+14155551234",
    "body": "Hello!",
    "from_number": "+14155556789",  // optional
    "metadata": {}                   // optional
}
```

**CRUD SMS Messages**
```
GET /api/phonehub/sms           # List all
GET /api/phonehub/sms/123       # Get one
```

**Webhooks** (no auth required - called by providers)
```
POST /api/phonehub/sms/webhook/twilio          # Incoming SMS
POST /api/phonehub/sms/webhook/twilio/status   # Status updates
POST /api/phonehub/sms/webhook/aws             # AWS webhook (placeholder)
```

### Configuration

**CRUD Configs**
```
GET /api/phonehub/config          # List all
GET /api/phonehub/config/123      # Get one
POST /api/phonehub/config         # Create
PUT /api/phonehub/config/123      # Update
DELETE /api/phonehub/config/123   # Delete
```

**Test Configuration**
```
POST /api/phonehub/config/123/test
```

**Set Credentials**
```
POST /api/phonehub/config/123/credentials
{
    "provider": "twilio",
    "twilio_account_sid": "ACxxxx",
    "twilio_auth_token": "xxxx"
}
```

## Setup

### 1. Install Dependencies

**For Twilio:**
```bash
pip install twilio
```

**For AWS:**
```bash
pip install boto3
```

### 2. Run Migrations

```bash
# From your Django project (not this framework)
python manage.py makemigrations phonehub
python manage.py migrate phonehub
```

### 3. Create Configuration

```python
from mojo.apps.phonehub.models import PhoneConfig

# Create system-wide Twilio config
config = PhoneConfig.objects.create(
    name="System Twilio",
    provider='twilio',
    twilio_from_number='+14155551234',
    lookup_enabled=True,
    lookup_cache_days=30
)

# Set credentials (encrypted)
config.set_twilio_credentials(
    account_sid='ACxxxxxxxxxxxx',
    auth_token='your_auth_token'
)
config.save()

# Test connection
result = config.test_connection()
print(result)
```

### 4. Configure Twilio Webhooks (for receiving SMS)

In your Twilio console, set the webhook URL for incoming messages:
```
https://yourdomain.com/api/phonehub/sms/webhook/twilio
```

For status callbacks, set:
```
https://yourdomain.com/api/phonehub/sms/webhook/twilio/status
```

## Usage Examples

### Send SMS
```python
from mojo.apps import phonehub

# Simple send
sms = phonehub.send_sms('+14155551234', 'Your verification code is: 123456')

if sms.status == 'sent':
    print(f"SMS sent! ID: {sms.provider_message_id}")
else:
    print(f"Failed: {sms.error_message}")

# Send with organization context
sms = phonehub.send_sms(
    '+14155551234',
    'Your verification code is: 123456',
    group=request.group,
    user=request.user
)
```

### Lookup Phone
```python
from mojo.apps import phonehub

phone = phonehub.lookup('+14155551234')

if phone:
    print(f"Number: {phone.phone_number}")
    print(f"Caller Name: {phone.caller_name}")  # Registered owner
    print(f"Caller Type: {phone.caller_type}")  # BUSINESS or CONSUMER
    print(f"Carrier: {phone.carrier}")
    
    if phone.is_mobile:
        print("This is a mobile number")
    elif phone.is_voip:
        print("This is a VOIP number")
    else:
        print(f"This is a {phone.line_type} number")
```

### Normalize Phone Numbers
```python
from mojo.apps import phonehub

# Handles various formats
num = phonehub.normalize('415-555-1234')      # +14155551234
num = phonehub.normalize('(415) 555-1234')   # +14155551234
num = phonehub.normalize('4155551234')       # +14155551234
```

### Check if Lookup is Stale
```python
from mojo.apps import phonehub

phone = phonehub.PhoneNumber.objects.get(phone_number='+14155551234')

if phone.needs_lookup:
    # Re-lookup
    phone = phonehub.lookup(phone.phone_number, force_refresh=True)
```

## Permissions

**Phone Numbers:**
- `view_phone_numbers`: View phone lookup data
- `manage_phone_numbers`: Create/update/delete phone numbers

**SMS:**
- `view_sms`: View SMS messages
- `manage_sms`: Send SMS and manage messages

**Configuration:**
- `manage_phone_config`: Manage phone configurations
- `manage_groups`: Manage group-level configs

## Notes

- **Test Mode**: Set `test_mode=True` on PhoneConfig to prevent sending real SMS during development
- **AWS Limitations**: AWS SNS doesn't provide comprehensive phone lookup like Twilio. Use Twilio for lookups.
- **Caching**: Phone lookups are cached by default for 30 days (configurable via `lookup_cache_days`)
- **Security**: All credentials are encrypted using MojoSecrets - never exposed in API responses
- **Multi-tenant**: Each organization can have its own configuration, or use system default

## File Structure

```
mojo/apps/phonehub/
├── __init__.py
├── README.md
├── models/
│   ├── __init__.py
│   ├── phone.py       # PhoneNumber model
│   ├── config.py      # PhoneConfig model
│   └── sms.py         # SMS model
├── services/
│   ├── __init__.py
│   └── phone.py       # Business logic
└── rest/
    ├── __init__.py
    ├── phone.py       # Phone endpoints
    ├── sms.py         # SMS endpoints
    └── config.py      # Config endpoints
```

## Future Enhancements

- MMS support (media messages)
- AWS Pinpoint integration for better SMS receiving
- Scheduled SMS sending
- SMS templates
- Bulk SMS operations
- Analytics and reporting
- Rate limiting
- Cost tracking
