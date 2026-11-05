# Sign-in Setup (Admin)

One Admin page configures the platform's own login: the hosted login page's
look and feel, and the Google, Apple, GitHub and Microsoft sign-in providers. It is
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
| Microsoft | `MICROSOFT_CLIENT_ID`, **`MICROSOFT_CLIENT_SECRET`**, `MICROSOFT_TENANT`, `MICROSOFT_CLIENT_SECRET_EXPIRES` |

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
it on or off edits both `login.methods` and `registration.methods`. Google,
Apple and GitHub start enabled; Microsoft starts off.

A provider is **ready** when every required field is configured. The fields in
`signin_setup.OPTIONAL_FIELDS` are not required: `MICROSOFT_TENANT` (empty
reads as `common`) and `MICROSOFT_CLIENT_SECRET_EXPIRES`.

## The Microsoft card

| Field | Saved as | Checked |
|---|---|---|
| Application (client) ID | `MICROSOFT_CLIENT_ID` | a GUID, stored lower-case |
| Client secret value | `MICROSOFT_CLIENT_SECRET` (secret) | write-only |
| Tenant | `MICROSOFT_TENANT` | `common`, `organizations`, `consumers` or a GUID, stored lower-case |
| Secret expires on (YYYY-MM-DD) | `MICROSOFT_CLIENT_SECRET_EXPIRES` | an ISO date |

Entra does not tell the app when its secret expires, so the admin types the
date and the card warns from it. Each provider carries `notices`
(`[{tone, text}]`, empty for the others):

| Expiry | Notice |
|---|---|
| not recorded | `info` "Record the secret's expiry date so this page can warn you" |
| under 30 days away | `warning` "Client secret expires on {date}" |
| past | `danger` "Client secret expired on {date}; Microsoft sign-in fails until it is replaced" |

Registration steps and the provider's validation rules are in
[OAuth](oauth.md#microsoft-settings).

## Callback URLs

The provider callback is `<origin>/api/auth/oauth/<provider>/callback`, where
`<origin>` is the Origin of the page that starts sign-in (`rest/oauth.py`
`_get_origin`). The page lists one per likely login origin —
`ALLOWED_REDIRECT_URLS` origins first, then `BASE_URL`, then the Admin's own
origin — and the matching domains (Apple's Services ID wants both).

## Provider consoles

Open Admin → Sign-in. Each card shows the callback URLs and domains to
register. Register **every** callback URL listed, not only the first: a
sign-in that starts from an origin whose callback is missing fails at the
provider. Paste the values into the card, switch it on and save. The next
sign-in uses them, with no deploy or restart.

**Google** (Google Cloud Console → Google Auth Platform → Clients):
1. Register the app with Google Auth (its branding) if the project has not;
   the console asks for this before it creates a client.
2. Click **Create Client** and choose the **Web application** type.
3. Add each callback URL under **Authorized redirect URIs**. Google requires
   HTTPS (localhost is exempt) and refuses raw IP hosts; a change can take
   from 5 minutes to a few hours to apply.
4. Paste the client ID and client secret into the card. Google shows the full
   secret only when the client is created.

**Apple** (Apple Developer → Certificates, Identifiers & Profiles):
1. Enable **Sign in with Apple** on your app's primary App ID.
2. Under Identifiers, add a **Services ID** (a reverse-domain identifier),
   select **Sign in with Apple**, click **Configure**, choose the primary App
   ID, and under **Website URLs** enter each domain and each callback URL the
   card lists, comma-separated.
3. Under Keys, create a key with Sign in with Apple enabled and download the
   `.p8`. Apple lets you download it only once.
4. Paste your Team ID, the Services ID's identifier (as **Services ID**), the
   key identifier (shown below the key's name, as **Key ID**) and the whole
   `.p8` file into the card.

**GitHub** (Settings → Developer settings → OAuth Apps → New OAuth App):
1. Set **Authorization callback URL** to a callback URL from the card. An
   OAuth app accepts up to 10 callback URLs, so add every one the card lists.
2. Generate a client secret.
3. Paste the **Client ID** and **Client secret** into the card.

**Microsoft:** see [OAuth](oauth.md#microsoft-settings).

## What the on/off switch does

The switch shows or hides the provider's button on the hosted login page by
editing the system `login.methods` and `registration.methods`. It is not an
API restriction (Ian's direction, NativeMojo #9275): `/begin` for Google,
Apple or GitHub with no group still starts sign-in while they are switched
off. A request that names a group still goes through
`auth_config.assert_login_method` against the group's resolved methods, which
inherit the system switch unless the group sets its own. Microsoft, off by
default, is refused at `/begin` with no group until it is switched on.

## Authority

`set_auth_safe_fields` accepts a live User with `is_superuser`, global
`manage_settings` or global `admin` (`admin_settings.require_catalog_writer`).
Other protected keys (`BASE_URL`, fleet topology, the framework pin) remain
superuser-only through `system_settings.set_value`, and the catalog's
`owner_edit` capability still advertises only that superuser tier.

See the API contract in the web developer track:
[Sign-in Setup](../../web_developer/account/signin_setup.md).
