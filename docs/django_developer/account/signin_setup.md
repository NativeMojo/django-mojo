# Sign-in Setup (Admin)

One Admin page configures the platform's own login: the hosted login page's
look and feel, and the Google, Apple and GitHub sign-in providers. It is
ordinary admin configuration — global `manage_settings` or `admin` (or a
superuser) may use it, with no step-up or approval.

| File | Role |
|---|---|
| `mojo/apps/account/services/signin_setup.py` | Provider catalog, state read, credential + on/off writes |
| `mojo/apps/account/rest/admin_signin.py` | `GET/POST /api/account/admin/signin` |
| `mojo/apps/account/services/system_settings.py` | `set_auth_safe_fields` — the system `AUTH_CONFIG` merge writer |

## Where values live

**Provider credentials** are plain global `Setting` rows under the exact keys
the providers already read with `settings.get`:

| Provider | Keys (secret in **bold**) |
|---|---|
| Google | `GOOGLE_CLIENT_ID`, **`GOOGLE_CLIENT_SECRET`** |
| Apple | `APPLE_TEAM_ID`, `APPLE_CLIENT_ID` (the Services ID), `APPLE_KEY_ID`, **`APPLE_PRIVATE_KEY`** (whole `.p8`) |
| GitHub | `GITHUB_CLIENT_ID`, **`GITHUB_CLIENT_SECRET`** |

`settings.get` resolves a database row before `django.conf`, so a value saved
here overrides the deployment file and takes effect on the next sign-in — no
deploy, no restart. A key set only in `django.conf` keeps working and is
reported as `source: "deployment"`. Secret rows are stored `is_secret=True` and
never returned; reads report `configured`, `source` and a four-character hint.
(Secret rows are currently also cached decrypted in Redis — NativeMojo #2592.)

**Look and feel and methods** are the system `AUTH_CONFIG` row, merged by
`system_settings.set_auth_safe_fields` over the paths in `AUTH_SAFE_PATHS`.
`password` must stay in `login.methods` so an admin can always recover access.
Group `metadata.auth_config` overrides still layer on top, unchanged.

A provider is **enabled** when it is in the system `login.methods`; switching
it on or off edits both `login.methods` and `registration.methods`.

## Callback URLs

The provider callback is `<origin>/api/auth/oauth/<provider>/callback`, where
`<origin>` is the Origin of the page that starts sign-in (`rest/oauth.py`
`_get_origin`). The page lists one per likely login origin —
`ALLOWED_REDIRECT_URLS` origins first, then `BASE_URL`, then the Admin's own
origin — and the matching domains (Apple's Services ID wants both).

## Authority

`set_auth_safe_fields` accepts a live User with `is_superuser`, global
`manage_settings` or global `admin` (`admin_settings.require_catalog_writer`).
Other protected keys (`BASE_URL`, fleet topology, the framework pin) remain
superuser-only through `system_settings.set_value`, and the catalog's
`owner_edit` capability still advertises only that superuser tier.

See the API contract in the web developer track:
[Sign-in Setup](../../web_developer/account/signin_setup.md).
