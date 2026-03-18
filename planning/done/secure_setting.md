# Request: Ability to Securely Store and Share django Settings

## Status
Resolved — 2026-03-17

## Priority
Medium

---

## Summary

We have a major issue when it comes to storing and sharing django settings.  Many of which include secure keys.  Most of these get stored into our django-projects settings which is , or in our var/django.conf, but things like keys need to be stored and share easily and securely. We often have dozens of nodes running the same django code, and no clean way to share our settings.

## Resolution

### What was built

DB-backed encrypted settings with Redis cache, group scoping with parent chain traversal, and transparent integration into `SettingsHelper`.

**Lookup chain**: `Redis cache → DB (group → parent chain → global) → django.conf.settings`

### Files changed

- **`mojo/apps/account/models/setting.py`** — new `Setting` model (MojoSecrets + MojoModel)
  - `key`, `value`, `is_secret`, `group` (nullable FK)
  - `Setting.set(key, value, is_secret, group)` / `Setting.remove(key, group)`
  - `Setting.resolve(name, group)` — full lookup with parent chain
  - Redis push on every save/delete, `warm_cache()` for bulk load
- **`mojo/helpers/settings/helper.py`** — `SettingsHelper.get()` now accepts `group=` kwarg
  - Checks DB-backed store (via `Setting.resolve`) before falling back to `django.conf.settings`
  - Fully backwards compatible — existing `settings.get("KEY")` calls unchanged
- **`mojo/apps/account/rest/setting.py`** — REST CRUD via `Setting.on_rest_request`
  - `VIEW_PERMS` / `SAVE_PERMS` = `["manage_settings"]`
  - Secret values masked as `******` in API responses
- **`mojo/apps/account/models/__init__.py`** — added Setting import
- **`mojo/apps/account/rest/__init__.py`** — added setting import

### Tests added

- `tests/test_helpers/secure_settings.py` — 15 tests covering:
  - Plain + secret create/update/delete
  - Redis cache push + warm
  - Group scoping + parent chain fallback + global fallback
  - SettingsHelper integration (DB override, group param, secret transparency)
  - REST create, list (masked secrets), permission enforcement

### What to run

```bash
python manage.py makemigrations  # generates Setting migration
python manage.py migrate
```

### Usage

```python
from mojo.helpers.settings import settings

# Global setting (falls through Redis → DB → django.conf.settings)
val = settings.get("API_KEY")

# Group-scoped (walks group → parent → global → django.conf.settings)
val = settings.get("FEATURE_FLAG", group=request.group)

# Programmatic set (pushes to Redis immediately)
from mojo.apps.account.models.setting import Setting
Setting.set("API_KEY", "sk-abc123", is_secret=True)
Setting.set("FEATURE_FLAG", "true", group=some_group)
```
