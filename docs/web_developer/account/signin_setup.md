# Sign-in Setup API (Admin)

Configure the platform login page and its Google / Apple / GitHub / Microsoft sign-in.
Requires global `manage_settings` or `admin` (superusers pass). API keys and
group tokens are refused.

## GET /api/account/admin/signin

```json
{
  "status": true,
  "data": {
    "schema_version": 1,
    "auth": {"theme": {...}, "login": {"methods": [...], "heading": "..."}, "registration": {...}},
    "editable": ["login.heading", "login.methods", "theme.app_title", "..."],
    "options": {"login_methods": [...], "registration_methods": [...], "layouts": [...],
                "appearances": [...], "hero_image_positions": [...], "passkey_prompts": [...]},
    "providers": [{
      "name": "apple", "label": "Apple",
      "enabled": true, "ready": false, "missing": ["Key ID"],
      "callback_url": "https://example.com/api/auth/oauth/apple/callback",
      "callback_urls": ["https://example.com/api/auth/oauth/apple/callback"],
      "domains": ["example.com"],
      "console_url": "https://developer.apple.com/...", "help": "...",
      "fields": [
        {"key": "APPLE_TEAM_ID", "label": "Team ID", "secret": false, "multiline": false,
         "configured": true, "source": "admin", "value": "ABCDE12345", "hint": null},
        {"key": "APPLE_PRIVATE_KEY", "label": "Private key (.p8)", "secret": true, "multiline": true,
         "configured": false, "source": "none", "value": null, "hint": null}
      ],
      "notices": []
    }]
  }
}
```

`auth` is the public view of the resolved system login config. `source` is
`admin` (saved here), `deployment` (server config file) or `none`. Secret
fields never carry `value`.

`notices` is a list of `{"tone": "warning" | "danger" | "info", "text": "..."}`
to show on the card; it is `[]` for every provider but Microsoft, whose card
warns about the client secret's expiry:

```json
{"name": "microsoft", "label": "Microsoft", "enabled": false, "ready": false,
 "missing": ["Client secret value"],
 "fields": [
   {"key": "MICROSOFT_CLIENT_ID", "label": "Application (client) ID", ...},
   {"key": "MICROSOFT_CLIENT_SECRET", "label": "Client secret value", "secret": true, ...},
   {"key": "MICROSOFT_TENANT", "label": "Tenant", ...},
   {"key": "MICROSOFT_CLIENT_SECRET_EXPIRES", "label": "Secret expires on (YYYY-MM-DD)", ...}
 ],
 "notices": [{"tone": "info", "text": "Record the secret's expiry date so this page can warn you"}]}
```

`missing` lists only required fields: the Microsoft tenant (empty means
`common`) and the expiry date are optional.

## POST /api/account/admin/signin

Send either or both parts; the response is the full GET payload.

**Look and feel** — flat dotted paths, only those in `editable`:

```json
{"auth": {"theme.app_title": "Acme", "theme.layout": "editorial", "login.heading": "Welcome back"}}
```

**A provider** — `values` maps setting keys to text. Omitted or `""` keeps the
stored value; `null` clears it. `enabled` switches the provider on or off for
login and registration.

```json
{"provider": "apple",
 "values": {"APPLE_TEAM_ID": "ABCDE12345", "APPLE_CLIENT_ID": "com.example.signin",
            "APPLE_KEY_ID": "XYZ987ABCD", "APPLE_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----"},
 "enabled": true}
```

Errors are `400 {"status": false, "error": "..."}` — for example removing
`password` from `login.methods`, an Apple key that is not a `.p8`, a Microsoft
client ID that is not a GUID, a Microsoft tenant that is not `common`,
`organizations`, `consumers` or a GUID, an expiry that is not `YYYY-MM-DD`, or
a key that does not belong to the provider. Changes apply on the next page load.
