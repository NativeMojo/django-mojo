# PhoneHub Models — Django Developer Reference

## PhoneNumber

System-wide cache of phone number lookup data. Not tied to any user or group — shared across the entire system to minimize provider API costs.

```python
from mojo.apps.phonehub.models import PhoneNumber
```

### Fields

| Field | Type | Description |
|---|---|---|
| `phone_number` | CharField(20), unique | E.164 format, e.g. `+14155551234` |
| `country_code` | CharField(5) | e.g. `"US"`, `"GB"` |
| `region` | CharField(100) | Country/region name |
| `state` | CharField(3) | US state code |
| `carrier` | CharField(100) | Carrier name, e.g. `"AT&T"` |
| `line_type` | CharField(20) | `"mobile"`, `"landline"`, or `"voip"` |
| `is_mobile` | BooleanField | True if mobile line |
| `is_voip` | BooleanField | True if VoIP line |
| `is_valid` | BooleanField | True if number passed validation |
| `registered_owner` | CharField(200) | CNAM registered name, if available |
| `owner_type` | CharField(50) | `"CONSUMER"` or `"BUSINESS"` |
| `address_line1` | CharField(200) | Address from lookup data |
| `address_city` | CharField(100) | City from lookup data |
| `address_state` | CharField(50) | State from lookup data |
| `address_zip` | CharField(20) | ZIP from lookup data |
| `address_country` | CharField(5) | Country from lookup data |
| `lookup_provider` | CharField(20) | `"twilio"` or `"aws"` |
| `lookup_data` | JSONField | Raw provider response |
| `lookup_expires_at` | DateTimeField | When to re-fetch from provider |
| `lookup_count` | IntegerField | Number of times looked up |
| `last_lookup_at` | DateTimeField | Last lookup timestamp |
| `created` | DateTimeField | Auto-set on create |
| `modified` | DateTimeField | Auto-set on update |

### RestMeta

```python
VIEW_PERMS = ["view_phone_numbers", "manage_phone_numbers", "manage_users"]
SAVE_PERMS = ["manage_phone_numbers", "manage_users"]
DELETE_PERMS = ["manage_phone_numbers"]
SEARCH_FIELDS = ["phone_number", "carrier", "registered_owner"]
LIST_DEFAULT_FILTERS = {"is_valid": True}
```

### Graphs

| Graph | Fields |
|---|---|
| `basic` | `id`, `phone_number`, `carrier`, `line_type`, `is_valid` |
| `default` | All fields except `lookup_data` |

### Key Methods

#### `PhoneNumber.normalize(phone_number)` (classmethod)
Normalize to E.164 format. Returns `None` if invalid.

```python
PhoneNumber.normalize("+1 415-555-1234")  # "+14155551234"
PhoneNumber.normalize("4155551234")       # "+14155551234"
PhoneNumber.normalize("bad")              # None
```

#### `PhoneNumber.lookup(phone_number)` (classmethod)
Get or create a cached `PhoneNumber`. Auto-refreshes if the cache has expired.

```python
phone = PhoneNumber.lookup("+14155551234")
```

#### `phone.refresh()`
Force re-fetch from the provider and update all fields.

#### Properties

| Property | Description |
|---|---|
| `needs_lookup` | True if `lookup_expires_at` has passed |
| `is_expired` | Alias for `needs_lookup` |
| `area_code` | Extracted 3-digit area code |
| `area_code_info` | objict with area code type, location, description |

---

## SMS

Audit trail for all sent and received SMS messages.

```python
from mojo.apps.phonehub.models import SMS
```

### Fields

| Field | Type | Description |
|---|---|---|
| `user` | FK → account.User | Associated user (optional) |
| `group` | FK → account.Group | Associated group (optional) |
| `direction` | CharField | `"outbound"` or `"inbound"` |
| `from_number` | CharField(20) | Sender in E.164 format |
| `to_number` | CharField(20) | Recipient in E.164 format |
| `body` | TextField | Message text |
| `status` | CharField | `queued`, `sending`, `sent`, `delivered`, `failed`, `undelivered`, `received` |
| `provider` | CharField(20) | `"twilio"` or `"aws"` |
| `provider_message_id` | CharField(100) | Provider SID/message ID |
| `error_code` | CharField(50) | Provider error code on failure |
| `error_message` | TextField | Provider error message on failure |
| `metadata` | JSONField | Arbitrary metadata dict |
| `is_test` | BooleanField | True if sent to a test number (`+1555...`) |
| `sent_at` | DateTimeField | When status changed to `sent` |
| `delivered_at` | DateTimeField | When status changed to `delivered` |
| `created` | DateTimeField | Auto-set on create |
| `modified` | DateTimeField | Auto-set on update |

### RestMeta

```python
VIEW_PERMS = ["view_sms", "manage_sms", "owner"]
SAVE_PERMS = ["manage_sms"]
DELETE_PERMS = ["manage_sms"]
SEARCH_FIELDS = ["to_number", "from_number", "body"]
```

The `"owner"` permission allows users to view their own SMS records.

### Graphs

| Graph | Fields |
|---|---|
| `basic` | `id`, `direction`, `from_number`, `to_number`, `body`, `status`, `created` |
| `default` | `id`, `direction`, `from_number`, `to_number`, `body`, `status`, `provider`, `error_message`, `sent_at`, `delivered_at`, `created` + nested `user`/`group` (basic) |
| `full` | All fields + nested `user`/`group` (default) |

### Key Methods

#### `SMS.send(body, to_number, ...)` (classmethod)
Create and send an SMS. This is the primary way to send — prefer `phonehub.send_sms()` at the module level.

```python
sms = SMS.send(
    body="Your code is 483921",
    to_number="+14155551234",
    metadata={"purpose": "verification"},
    user=request.user,
    group=request.group,
    from_number="+18005551234",
)
```

#### Status helpers

```python
sms.is_outbound   # direction == "outbound"
sms.is_inbound    # direction == "inbound"
sms.is_delivered  # status in ["delivered", "received"]
sms.is_failed     # status in ["failed", "undelivered"]

sms.mark_sent(provider_message_id="SM123")
sms.mark_delivered()
sms.mark_failed(error_code="30006", error_message="Landline unreachable")
```

---

## PhoneConfig

Per-group (or system-wide) provider configuration with encrypted credentials.

```python
from mojo.apps.phonehub.models import PhoneConfig
```

Inherits from `MojoSecrets, MojoModel`. Credentials are stored encrypted in a single `mojo_secrets` JSON field — never in plain text.

### Fields

| Field | Type | Description |
|---|---|---|
| `group` | OneToOneField → account.Group | `null` = system default config |
| `name` | CharField(100) | Config name |
| `is_active` | BooleanField | Whether this config is active |
| `provider` | CharField | `"twilio"` (default) or `"aws"` |
| `twilio_from_number` | CharField(20) | Default Twilio sender number |
| `aws_region` | CharField(20) | AWS region (default `"us-east-1"`) |
| `aws_sender_id` | CharField(11) | AWS SNS sender ID (max 11 chars) |
| `lookup_enabled` | BooleanField | Whether to perform carrier lookups |
| `lookup_cache_days` | IntegerField | Days before re-lookup (default 90) |
| `test_mode` | BooleanField | Prevent sending real SMS when True |
| `created` | DateTimeField | Auto-set on create |
| `modified` | DateTimeField | Auto-set on update |

### RestMeta

```python
VIEW_PERMS = ["manage_phone_config", "manage_groups"]
SAVE_PERMS = ["manage_phone_config", "manage_groups"]
DELETE_PERMS = ["manage_phone_config", "manage_groups"]
SEARCH_FIELDS = ["name"]
LIST_DEFAULT_FILTERS = {"is_active": True}
```

### Graphs

| Graph | Fields |
|---|---|
| `basic` | `id`, `name`, `provider`, `test_mode`, `is_active` |
| `default` | All non-secret fields + nested `group` (basic) |
| `full` | All non-secret fields + nested `group` (default) |

`mojo_secrets` is always excluded from all graphs.

### Key Methods

#### `PhoneConfig.get_for_group(group=None)` (classmethod)
Returns the config for a group, falling back to the system default.

```python
config = PhoneConfig.get_for_group(request.group)
```

#### Credential management

```python
# Twilio
config.set_twilio_credentials("ACxxxx", "auth_token_here")
config.get_twilio_account_sid()
config.get_twilio_auth_token()

# AWS
config.set_aws_credentials("AKIAXXXX", "secret_key_here")
config.get_aws_access_key_id()
config.get_aws_secret_access_key()
```

#### `config.test_connection()`
Test that the configured credentials are valid.

```python
result = config.test_connection()
result["success"]  # True or False
result["message"]  # Human-readable result
```
