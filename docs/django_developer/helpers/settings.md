# settings — Django Developer Reference

## Import

```python
from mojo.helpers.settings import settings
```

## Reading Settings

```python
# With default
value = settings.get("MY_SETTING", "default_value")
value = settings.get("TIMEOUT_SECONDS", 30)

# Without default (returns None if missing)
value = settings.get("MY_SETTING")

# Dict-style access
value = settings["MY_SETTING"]

# Attribute-style access
value = settings.MY_SETTING
```

## Checking for Apps

```python
if settings.is_app_installed("mojo.apps.fileman"):
    from mojo.apps.fileman.models import File
```

## App-Specific Settings

Load settings scoped to a specific app:

```python
app_settings = settings.get_app_settings("myapp")
value = app_settings.get("MYAPP_API_KEY", "")
```

## Settings Profile

Load a profile-specific settings module (useful for dev/staging/prod):

```python
from mojo.helpers.settings import load_settings_profile
load_settings_profile(context)
```

## Defining Settings in Django settings.py

```python
# settings.py
MY_APP_API_KEY = "abc123"
MY_APP_TIMEOUT = 30
MY_APP_FEATURE_FLAG = True
```

Then access via:

```python
settings.get("MY_APP_API_KEY", "")
```

## `kind=` Coercion

`settings.get(name, default, kind=...)` coerces the resolved value (`"int"`,
`"float"`, `"bool"`, `"dict"`, `"list"`):

```python
enabled = settings.get("MY_FLAG", False, kind="bool")
scopes = settings.get("MY_SCOPES", [], kind="list")   # JSON list or "a, b" CSV
rules = settings.get("MY_RULES", {}, kind="dict")     # JSON object
```

**A present-but-uncoercible value degrades to the DECLARED default and logs a
`settings` warning** (`logit.warning("settings", ...)`) — garbage is treated as
*unset-but-loud*, never silently absorbed:

- An unrecognized `bool` string (not one of `true/1/yes/on/y` /
  `false/0/no/off/n/""`) returns the declared default — it does **not**
  truthy-coerce to `True` (the old behavior failed open for allow-flavored
  flags).
- An unparsable `dict` returns the declared default (or `{}`).
- A bracket-wrapped but unparsable `list` (e.g. `'["payments",]'`) returns the
  declared default — it is **not** comma-split into nonsense entries. Plain
  comma strings (`"a, b"`) still split.
- Garbage `int`/`float` returns the declared default.

Because the default is what a garbage value degrades to, pass the same default
at every read site of a key (the framework's geofence reads already do).

## `settings.get_static()` — Conf-File-Only Reads

`settings.get()` is DB/Redis-aware: it checks the `Setting` model (Redis cache →
DB, group parent chain → global) before falling back to the Django settings
file. That's the right default for most keys, but it also means **any key read
with `settings.get()` can be overridden by a global `Setting` row** — writable
via the generic `POST /api/settings` REST (with `manage_settings`) or direct
Redis access — even if the key has no registered validator, no group scoping
rule, and no cache-invalidation or audit trail of its own.

`settings.get_static(name, default=None, kind=None)` skips the DB/Redis lookup
entirely and reads **only** the Django settings file (same `kind=` coercion as
`get()`). Use it for:

- Settings that gate test/dev plumbing and must never be remotely armable
  (e.g. `MOJO_TEST_MODE`, `GEOFENCE_TEST_OVERRIDE`) — a DB row must not be able
  to flip a behavior that was only ever meant to be a deploy-time flag.
  See [testit Overview](../testit/Overview.md#security-gate) and
  [Geofencing](../account/geofence.md#settings-reference).
- Settings read before Django (or the DB) is ready, e.g. `REDIS_URL` /
  `REDIS_SERVER` in `mojo/helpers/redis/client.py` — the DB-backed path itself
  depends on Redis, so reading it via `get()` would be circular.
- Process-boot constants read once at import time (URL prefixes, middleware
  toggles) where DB-backed override was never intended.

If a setting is meant to be admin-tunable at runtime, use `settings.get()` and
register a validator (see below) instead of reaching for `get_static()`.

## Write-Time Validation (Registered Keys)

Enforcement-bearing DB settings should be **validated at write time** so
garbage can never persist. Register a validator on the `Setting` model:

```python
from mojo.apps.account.models.setting import Setting

def _validate_my_rules(key, parsed):
    # parsed is the JSON-decoded value; raise ValueError on a bad one
    if not isinstance(parsed, dict):
        raise ValueError(f"{key} must be a JSON object")

Setting.register_validator("MYAPP_RULES", _validate_my_rules)  # global_only=True
```

A registered key is validated on **every** write path — the generic
`POST /api/settings` REST (readable `400`) and `Setting.set()` / direct
`.save()` (raises `ValueException`). The value must be valid JSON;
`global_only=True` (the default) also rejects group-scoped rows, and
registered keys refuse `is_secret` rows (validators need the plaintext, and a
masked value would hide enforcement config from admins). All
`GEOFENCE_*` keys are registered this way (see
[Geofencing](../account/geofence.md)); downstream apps register their own keys
at import time (e.g. mverify's `PAYMENTS_GEOFENCE_RULES`).

## Notes

- Always go through the settings helper (`settings.get()` for DB-tunable keys, `settings.get_static()` for conf-file-only keys — see above) rather than importing directly from `django.conf`; the helper provides defaults and avoids `AttributeError` on missing keys.
- App-specific settings are cached after first load.

## Framework Keys

For the framework-recognized setting names (without values), see:

- [settings_reference.md](settings_reference.md)
