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
| `is_valid` | BooleanField | Carrier verdict from the last **successful** lookup. Never written by the error path — meaningless while `lookup_unavailable` is true |
| `registered_owner` | CharField(200) | CNAM registered name, if available |
| `owner_type` | CharField(50) | `"CONSUMER"` or `"BUSINESS"` |
| `address_line1` | CharField(200) | Address from lookup data |
| `address_city` | CharField(100) | City from lookup data |
| `address_state` | CharField(50) | State from lookup data |
| `address_zip` | CharField(20) | ZIP from lookup data |
| `address_country` | CharField(5) | Country from lookup data |
| `lookup_provider` | CharField(20) | `"twilio"` or `"aws"` |
| `lookup_data` | JSONField | Error marker only (`error`, `error_at`, `error_count`); cleared on every success. Never serialized (`NO_SHOW_FIELDS`) |
| `lookup_expires_at` | DateTimeField | When to re-fetch from provider |
| `lookup_count` | IntegerField | Number of times looked up |
| `last_lookup_at` | DateTimeField | Last **successful** lookup timestamp; an error never sets it |
| `created` | DateTimeField | Auto-set on create |
| `modified` | DateTimeField | Auto-set on update |

### RestMeta

```python
VIEW_PERMS = ["view_phone_numbers", "manage_phone_numbers", "manage_users"]
SAVE_PERMS = ["manage_phone_numbers", "manage_users"]
DELETE_PERMS = ["manage_phone_numbers"]
SEARCH_FIELDS = ["phone_number", "carrier", "registered_owner"]
```

### Graphs

| Graph | Fields |
|---|---|
| `basic` | `id`, `phone_number`, `carrier`, `line_type`, `is_valid` |
| `default` | All fields except `lookup_data` (excluded via `NO_SHOW_FIELDS`), plus the `lookup_unavailable` property |

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
Returns `None` when `normalize()` cannot parse the number — there is no cache
key for it, so no row is created.

```python
phone = PhoneNumber.lookup("+14155551234")
PhoneNumber.lookup("bad")   # None
```

#### `phone.refresh(*, lookup_fn=None)`
Force re-fetch from the provider. On success every carrier field is updated and
the row is cached for `LOOKUP_TTL_DAYS`. On a provider error nothing but the
error marker is written — see "Lookup errors and `is_valid`" below.

`lookup_fn` is a keyword-only **test seam** for the provider call. Production
never passes it; when it is `None` the Twilio lookup service is used.

#### Properties

| Property | Description |
|---|---|
| `needs_lookup` | True if `lookup_expires_at` has passed |
| `is_expired` | Alias for `needs_lookup` |
| `lookup_error` | Provider error text from the last failed lookup, or `None` |
| `lookup_unavailable` | True when the provider failed **and** there is no successful lookup younger than `LOOKUP_TTL_DAYS` — i.e. no verdict is available |
| `area_code` | Extracted 3-digit area code |
| `area_code_info` | objict with area code type, location, description |

### Lookup errors and `is_valid`

One row, two TTLs (module constants in `mojo/apps/phonehub/models/phone.py`):

| Constant | Value | Meaning |
|---|---|---|
| `LOOKUP_TTL_DAYS` | 90 | How long a **successful** carrier verdict is cached |
| `LOOKUP_ERROR_TTL_MINUTES` | 15 | First negative-cache window after a provider error |
| `LOOKUP_ERROR_TTL_MAX_MINUTES` | 1440 | Ceiling for the exponential backoff |

A failed lookup is **negative-cached**, not raised and not discarded: the error
is recorded in `lookup_data` (text ANSI-stripped and truncated to 200 chars,
plus `error_at` and `error_count`) and `lookup_expires_at` is stamped
**unconditionally** on every error at `min(15 * 2**(error_count-1), 1440)`
minutes. Nothing else moves — not `is_valid`, not `carrier`/`line_type`/
`is_mobile`/`is_voip`/`registered_owner`, not `last_lookup_at`, not
`lookup_count`. A success clears the marker.

That asymmetry is deliberate. `is_valid` is the carrier verdict from the last
**successful** lookup, so an outage must never overwrite it — writing `False`
would mint a false negative from someone else's downtime. Read
`lookup_unavailable` first and treat it as "no verdict"; only then is `is_valid`
meaningful. A previously-good row that errors inside its TTL keeps serving its
cached verdict (`lookup_unavailable` is False); a verdict older than
`LOOKUP_TTL_DAYS` that cannot be refreshed does not (`True`), which is what
stops a disconnected or reassigned number from serving a dated "valid" forever.

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
| `provider` | CharField(20) | `"twilio"`, `"aws"`, or `"mojo"` |
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

#### Twilio sender/credential resolution

For `twilio`/`aws` provider configs (and when no config exists), the sender
and Twilio credentials resolve **atomically, anchored on the config's stored
credential pair** (`twilio.resolve_credentials(config, from_number)`):

| Config stores | Resolution |
|---|---|
| **both** `twilio_account_sid` and `twilio_auth_token` | the config owns the whole triple — the number comes from the caller's `from_number` or `config.twilio_from_number`, else the send fails `config_error` |
| **neither** credential | settings own the credentials (`TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN`); number falls back caller → `config.twilio_from_number` → `TWILIO_NUMBER` |
| **exactly one** credential | the send fails `config_error` with no provider call — a foreign number is never mixed with the default account's keys (Twilio 21606) |

`PhoneConfig._test_twilio()` validates the same pair this resolution would
send with, so a passing connection test vouches for the pair production uses.

> **Upgrade note:** before this resolution existed, `twilio_from_number` and
> config-stored Twilio credentials were dead — sends always used
> `settings.TWILIO_NUMBER` and settings credentials. An installation with
> those fields populated changes sending behavior on upgrade.

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
| `provider` | CharField | `"twilio"` (default), `"aws"`, or `"mojo"` |
| `twilio_from_number` | CharField(20) | Twilio sender number — used by `SMS.send()` (see "Twilio sender/credential resolution" below) |
| `aws_region` | CharField(20) | AWS region (default `"us-east-1"`) |
| `aws_sender_id` | CharField(11) | AWS SNS sender ID (max 11 chars) |
| `mojo_remote_url` | CharField(255) | Base URL of the remote django-mojo SMS provider (e.g. `https://sms.example.com`). Trailing slash stripped on save. |
| `lookup_enabled` | BooleanField | Whether to perform carrier lookups |
| `lookup_cache_days` | IntegerField | Days before re-lookup (default 90) |
| `test_mode` | BooleanField | Short-circuits `test_connection()` only. **`SMS.send()` does not read it** — a test-mode config still sends real messages |
| `created` | DateTimeField | Auto-set on create |
| `modified` | DateTimeField | Auto-set on update |

### RestMeta

```python
VIEW_PERMS = ["manage_phone_config", "manage_groups"]
SAVE_PERMS = ["manage_phone_config", "manage_groups"]
DELETE_PERMS = ["manage_phone_config", "manage_groups"]
SEARCH_FIELDS = ["name"]
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

# Mojo remote provider
config.set_mojo_api_key("apikey_token_from_remote")
config.get_mojo_api_key()
```

Each individual secret also has an auto-setter (`set_twilio_account_sid`, `set_twilio_auth_token`, `set_aws_access_key_id`, `set_aws_secret_access_key`, `set_mojo_api_key`) so the REST layer can route a write straight through a POST body field — see [rest.md — Writing encrypted credentials via REST](rest.md#writing-encrypted-credentials-via-rest).

#### `config.test_connection()`
Test that the configured credentials are valid. Dispatches to the per-provider
test method (`_test_twilio` / `_test_aws` / `_test_mojo`). The mojo branch GETs
the remote's `/api/group/apikey/me` whoami endpoint to validate URL reachability
and the api key, then checks the returned permissions include `send_sms` (or
`comms`) so the key can actually send. No SMS row is created on the remote — the
check has zero side effects. A valid key without a send permission returns
`error="insufficient_permission"`.

```python
result = config.test_connection()
result["success"]  # True or False
result["message"]  # Human-readable result
result["error"]    # e.g. "invalid_credentials", "timeout", "missing_credentials"
```
