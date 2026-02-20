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

## Notes

- Always use `settings.get()` rather than importing directly from `django.conf` — it provides defaults and avoids `AttributeError` on missing keys.
- App-specific settings are cached after first load.
