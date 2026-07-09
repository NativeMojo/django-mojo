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
`global_only=True` (the default) also rejects group-scoped rows. All
`GEOFENCE_*` keys are registered this way (see
[Geofencing](../account/geofence.md)); downstream apps register their own keys
at import time (e.g. mverify's `PAYMENTS_GEOFENCE_RULES`).

## Notes

- Always use `settings.get()` rather than importing directly from `django.conf` — it provides defaults and avoids `AttributeError` on missing keys.
- App-specific settings are cached after first load.

## Framework Keys

For the framework-recognized setting names (without values), see:

- [settings_reference.md](settings_reference.md)
