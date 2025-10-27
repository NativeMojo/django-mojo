# PhoneHub - SMS and Phone Lookup Service

Simple Django app for SMS and phone number operations using Twilio.

## Quick Start

```python
from mojo.apps import phonehub

# Normalize phone numbers to E.164 format
phone = phonehub.normalize('(415) 555-1234')  # Returns: +14155551234

# Validate phone numbers (USA/Canada only)
is_valid = phonehub.validate('4155551234')  # Returns: True or False

# Get detailed validation info
result = phonehub.validate('415-555-1234', detailed=True)
print(result.valid)                      # True
print(result.normalized)                 # +14155551234
print(result.area_code)                  # 415
print(result.area_code_info.location.state)   # CA
print(result.area_code_info.location.region)  # San Francisco

# Get area code information
info = phonehub.get_area_code_info('415')
print(info.location.state)    # CA
print(info.location.region)   # San Francisco

# Lookup phone details (carrier, line type, registered owner)
phone = phonehub.lookup('+14155551234')
print(phone.carrier)           # e.g., "Verizon Wireless"
print(phone.is_mobile)         # True/False
print(phone.registered_owner)  # Registered customer name
print(phone.owner_type)        # BUSINESS or CONSUMER

# Send SMS
sms = phonehub.send_sms('+14155551234', 'Hello!')
print(sms.status)  # sent, failed, etc.
```

## Setup

### 1. Add Twilio Credentials to Django Settings

```python
# settings.py
TWILIO_ACCOUNT_SID = 'ACxxxxxxxxxxxxxxxxx'
TWILIO_AUTH_TOKEN = 'your_auth_token_here'
```

### 2. Install Dependencies

```bash
pip install twilio
```

### 3. Run Migrations

```bash
python manage.py migrate phonehub
```

## Core Functions

### `normalize(phone_number, country_code='US')`

Converts phone numbers to E.164 format (+1XXXXXXXXXX). Only handles USA/Canada/Caribbean (NANP).

```python
phonehub.normalize('4155551234')          # +14155551234
phonehub.normalize('(415) 555-1234')      # +14155551234
phonehub.normalize('1-415-555-1234')      # +14155551234
phonehub.normalize('+14155551234')        # +14155551234

# International numbers return None
phonehub.normalize('+442071234567')       # None (UK number)
```

**Returns:** E.164 formatted string or `None` if invalid/international

---

### `validate(phone_number, country_code='US', detailed=False)`

Validates USA/Canada phone numbers according to NANP rules.

**Simple validation (boolean):**
```python
phonehub.validate('4155551234')           # True
phonehub.validate('123')                  # False
phonehub.validate('+442071234567')        # False (international)
```

**Detailed validation:**
```python
result = phonehub.validate('415-555-1234', detailed=True)

# Valid number response structure
result.valid                               # True
result.normalized                          # '+14155551234'
result.area_code                           # '415'
result.area_code_info.valid                # True
result.area_code_info.area_code            # '415'
result.area_code_info.type                 # 'geographic'
result.area_code_info.description          # 'Geographic area code (USA/Canada/Caribbean)'
result.area_code_info.location.state       # 'CA'
result.area_code_info.location.region      # 'San Francisco'
result.area_code_info.location.country     # 'US'
result.error                               # None

# International number (helpful error)
result = phonehub.validate('+3322312111', detailed=True)
{
    'valid': False,
    'normalized': None,
    'error': 'International number detected: France (+33) - only USA/Canada/Caribbean supported',
    'international': {
        'country_code': '33',
        'country': 'France',
        'region': 'Europe',
        'is_nanp': False
    }
}
```

**Validation Rules (NANP):**
- Area code must be valid and assigned
- Area code cannot start with 0 or 1
- Area code cannot be N11 format (211, 311, etc.)
- Exchange (first 3 digits after area code) cannot start with 0 or 1
- Exchange cannot be N11 service codes (411, 911, etc.)
- Cannot be all same digits (e.g., 8888888888)

---

### `get_area_code_info(phone_number)`

Returns information about an area code. Can accept area code alone or full phone number.

```python
# Just area code
info = phonehub.get_area_code_info('415')

# Full phone number (extracts area code)
info = phonehub.get_area_code_info('+14155551234')
info = phonehub.get_area_code_info('(415) 555-1234')
```

**Returns:**
```python
# Geographic area code
info = phonehub.get_area_code_info('415')
info.valid                  # True
info.area_code              # '415'
info.type                   # 'geographic' (or 'toll_free', 'invalid')
info.description            # 'Geographic area code (USA/Canada/Caribbean)'
info.location.state         # 'CA'
info.location.region        # 'San Francisco'
info.location.country       # 'US'

# Toll-free numbers
info = phonehub.get_area_code_info('800')
info.valid                  # True
info.area_code              # '800'
info.type                   # 'toll_free'
info.description            # 'Toll-free number'
info.location               # None
```

**Supported Area Codes:** 400+ USA/Canada area codes with state/region mapping

---

### `lookup(phone_number)`

Looks up phone number details via Twilio API. Results are cached to minimize API charges.

```python
phone = phonehub.lookup('+14155551234')

print(phone.phone_number)       # +14155551234
print(phone.carrier)            # "Verizon Wireless"
print(phone.line_type)          # "mobile", "landline", "voip"
print(phone.is_mobile)          # True/False
print(phone.is_voip)            # True/False
print(phone.is_valid)           # True/False
print(phone.registered_owner)   # "John Smith" (registered customer)
print(phone.owner_type)         # "BUSINESS" or "CONSUMER"
print(phone.country_code)       # "US"
print(phone.region)             # "San Francisco"
print(phone.state)              # "CA"
```

**Caching:** Lookups are cached for 90 days by default. Cache is shared system-wide to minimize costs.

**PhoneNumber Model Fields:**
- `phone_number` - E.164 format (+14155551234)
- `country_code` - Country code (US, CA, etc.)
- `region` - Geographic region (San Francisco)
- `state` - State/province code (CA, NY, ON)
- `carrier` - Carrier/operator name
- `line_type` - mobile, landline, voip, etc.
- `is_mobile` - Boolean
- `is_voip` - Boolean
- `is_valid` - Boolean (whether reachable)
- `registered_owner` - Registered owner name (CNAM)
- `owner_type` - BUSINESS or CONSUMER
- `address_line1`, `address_city`, `address_state`, `address_zip`, `address_country` - Address if available
- `lookup_provider` - twilio or aws
- `lookup_expires_at` - When to refresh
- `lookup_count` - Times looked up
- `last_lookup_at` - Last successful lookup timestamp
- `lookup_data` - Raw provider response (JSON)

---

### `send_sms(to_number, message)`

Sends SMS via Twilio.

```python
sms = phonehub.send_sms('+14155551234', 'Your code is: 123456')

print(sms.status)              # 'queued', 'sent', 'delivered', 'failed'
print(sms.provider_message_id) # Twilio message SID
print(sms.error_message)       # Error if failed
```

**SMS Model Fields:**
- `direction` - 'outbound' or 'inbound'
- `from_number` - Sender
- `to_number` - Recipient
- `body` - Message content
- `status` - Delivery status
- `provider_message_id` - Twilio SID
- `error_code`, `error_message` - If failed

## International Number Detection

When validating international numbers, PhoneHub provides helpful error messages:

```python
# France
result = phonehub.validate('+3322312111', detailed=True)
# Error: "International number detected: France (+33) - only USA/Canada/Caribbean supported"

# UK
result = phonehub.validate('+442071234567', detailed=True)
# Error: "International number detected: United Kingdom (+44) - only USA/Canada/Caribbean supported"

# Germany
result = phonehub.validate('+4915112345678', detailed=True)
# Error: "International number detected: Germany (+49) - only USA/Canada/Caribbean supported"
```

**Supported country codes:** 50+ common international country codes including UK, Germany, France, Japan, China, India, Australia, etc.

## Examples

### Validate User Input

```python
from mojo.apps import phonehub

def clean_phone_number(user_input):
    """Validate and normalize user phone input"""
    result = phonehub.validate(user_input, detailed=True)
    
    if not result.valid:
        if result.get('international'):
            raise ValueError(
                f"International numbers not supported. "
                f"Detected: {result.international.country}"
            )
        raise ValueError(result.error)
    
    return result.normalized
```

### Check Area Code Location

```python
def get_location_from_phone(phone_number):
    """Get geographic location from phone number"""
    info = phonehub.get_area_code_info(phone_number)
    
    if info.valid and info.location:
        return f"{info.location.region}, {info.location.state}"
    return "Unknown"

print(get_location_from_phone('4155551234'))  # "San Francisco, CA"
print(get_location_from_phone('2125551234'))  # "Manhattan, NY"
```

### Verify Mobile Number

```python
def is_mobile_number(phone_number):
    """Check if phone number is mobile"""
    phone = phonehub.lookup(phone_number)
    return phone and phone.is_mobile

if is_mobile_number('+14155551234'):
    # Send SMS
    phonehub.send_sms('+14155551234', 'Verification code: 123456')
else:
    # Use different verification method
    send_email_verification()
```

### Batch Normalize Phone Numbers

```python
def normalize_phone_list(phone_numbers):
    """Normalize a list of phone numbers"""
    normalized = []
    errors = []
    
    for phone in phone_numbers:
        result = phonehub.validate(phone, detailed=True)
        if result.valid:
            normalized.append(result.normalized)
        else:
            errors.append({'phone': phone, 'error': result.error})
    
    return normalized, errors
```

## Toll-Free Numbers

PhoneHub supports all USA toll-free numbers:

```python
# All toll-free codes are valid
codes = ['800', '888', '877', '866', '855', '844', '833']

for code in codes:
    result = phonehub.validate(f'{code}5551234', detailed=True)
    print(f"{code}: {result.valid}")  # All True
    print(result.area_code_info.type)  # 'toll_free'
```

## Testing

Run the PhoneHub test suite:

```bash
# From your Django project
python manage.py testit -m test_phonehub.phonenumbers
```

**Test coverage:**
- 27 comprehensive tests
- Phone normalization (NANP and international)
- NANP validation rules
- International number detection
- Area code lookup and parsing
- Edge cases and error handling

## Notes

- **USA/Canada Only**: PhoneHub currently only validates and processes NANP (North American) numbers
- **International Numbers**: Detected and rejected with helpful error messages identifying the country
- **Caching**: Phone lookups are cached system-wide (not per-user) to minimize Twilio API charges
- **E.164 Format**: All phone numbers are normalized to E.164 format (+1XXXXXXXXXX)
- **Area Codes**: 400+ area codes mapped to states/regions for USA/Canada
- **Twilio Required**: Twilio credentials required for `lookup()` and `send_sms()` functions

## Area Code Coverage

PhoneHub includes comprehensive area code mapping for:
- **USA**: All 50 states + territories
- **Canada**: All provinces and territories  
- **Caribbean**: NANP countries (Bermuda, Jamaica, etc.)
- **Toll-free**: 800, 888, 877, 866, 855, 844, 833

Examples:
- `415` → San Francisco, CA
- `212` → Manhattan, NY
- `613` → Ottawa, ON (Canada)
- `800` → Toll-free
- `787` → Puerto Rico

## File Structure

```
mojo/apps/phonehub/
├── __init__.py                          # Main API exports
├── README.md                            # This file
├── models/
│   ├── phone.py                        # PhoneNumber model (cached lookups)
│   ├── sms.py                          # SMS model (message tracking)
│   └── config.py                       # PhoneConfig (Twilio credentials)
├── services/
│   ├── phonenumbers.py                 # normalize, validate
│   ├── area_codes.py                   # NANP area code validation
│   ├── area_code_mapping.py            # Area code → state/region mapping
│   ├── international_codes.py          # Country code detection
│   └── phone.py                        # lookup, send_sms
└── tests/
    └── test_phonehub/
        └── phonenumbers.py             # 27 comprehensive tests
```
