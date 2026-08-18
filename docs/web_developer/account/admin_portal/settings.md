# Admin Settings API

The built-in Admin exposes a curated, capability-gated Settings catalog over
django-mojo's existing database-backed settings system.

| Method | Route | Authority |
|---|---|---|
| `GET` | `/api/account/admin/settings` | exact global `manage_settings`, `view_advanced_settings`, `manage_advanced`, or `admin`; literal superuser also passes |
| `POST` | `/api/account/admin/settings` | fresh interactive `manage_settings`/`admin`; literal superuser also passes |
| `POST` | `/api/account/admin/advanced/settings` | fresh interactive `manage_advanced`/`admin` gate plus active literal superuser; typed compatibility owner |

`manage_settings` also admits the Admin source session and bootstrap. Catalog
write and owner-display capabilities are separate: a manage-settings-only user
does not receive AUTH_CONFIG or topology owner rows. Literal
`permissions.admin` is an exact grant for these listed gates, not a backend
permission wildcard; `User.is_superuser` is the wildcard.

The GET response has `schema_version`, ordered `sections`, and server-owned
`entries`. Each entry supplies friendly label, description, type, constraints,
effective value/status, source, scope, owner, change behavior, and capability
booleans. Sensitive deployment settings expose only
`{"configured":true|false}`. Arbitrary Django settings, paths, environment
names, ignored raw values, provider responses, and exceptions are omitted.
The fixed order is General, Sign-in & registration, Users, Email, Domains &
DNS, Edge & Web Apps, and Security & operations; sections from absent optional
applications are omitted.

For a literal superuser, GET also includes `provider_setup`: availability,
delegated keys, configured-only secret flags, the effective GeoIP/SMS values,
and loaded/published revisions. Non-superusers receive no provider setup
payload.

Sources include `database_cache`, `database`, `deployment`, `default`,
`computed`, merged `database+deployment+defaults`, `invalid`,
`secret_override`, and `duplicate_override`. A legacy secret global row is
never decrypted or requested from Redis and returns only
`{"configured":true}`; a secret BASE_URL leaves Setup incomplete. A duplicate
is fail-closed, not a random first-row winner. Dynamic rows resolve through
Redis/database before deployment/default; protected rows use their dedicated
database owner; static rows remain deployment-only and only flag an ignored
database shadow; AUTH_CONFIG is merged; posture rows are computed.

Catalog mutations accept only:

```json
{"action":"set","key":"ALLOW_PHONE_CHANGE","value":false}
```

```json
{"action":"set","key":"WEBAPP_BASE_URL","value":"https://apps.mojoverify.com"}
```

```json
{"action":"clear","key":"WEBAPP_BASE_URL"}
```

The same fresh-auth endpoint accepts the superuser-only provider action:

```json
{
  "action": "configure_providers",
  "providers": {
    "geoip": {
      "GEOIP_PRIMARY_PROVIDER": "mojo",
      "GEOIP_FALLBACK_PROVIDER": "ipinfo",
      "GEOIP_ADDITIONAL_PROVIDERS": [],
      "GEOIP_MOJO_PROVIDER_URL": "https://api.mojoverify.com",
      "GEOIP_MOJO_SYNC_ENABLED": false,
      "GEOIP_API_KEY_MOJO": "optional-new-secret",
      "clear_api_key": false
    },
    "sms": {
      "remote_url": "https://sms.example.com",
      "api_key": "optional-new-secret",
      "clear_api_key": false,
      "test_mode": false
    }
  }
}
```

Blank/omitted secret values preserve the encrypted value; clearing requires
the explicit boolean. The static fields publish one KMS-encrypted, integrity-
marked S3 override and return its revision. The database secret and system
`PhoneConfig` take effect without a restart; static GeoIP selection takes
effect after config sync installs the composed file and restarts the service.

Set returns the normalized value; Clear returns the number of removed global
rows (including every duplicate):

```json
{"schema_version":1,"saved":true,"key":"ALLOW_PHONE_CHANGE","effective_value":false}
```

```json
{"schema_version":1,"cleared":true,"key":"WEBAPP_BASE_URL","removed":2}
```

Only four self-service booleans and the global default WebApp public HTTPS
origin are catalog-writable initially. IP literals, browser-style numeric IP
forms, localhost, and private/special-use hostname suffixes are refused.
Credentials, paths, query strings, fragments, non-443 ports, and reserved
example domains are also refused. Values are non-secret. Set refuses ambiguous
duplicates. Clear is idempotent, removes every conflicting global override,
and reveals the deployment/default source. Setting one legacy secret row
replaces it with the validated non-secret value. API keys and group tokens are
refused, recent auth is 600 seconds, and HTTP 440 requires explicit
reauthentication without write replay.

Public API address routes to focused System Setup. Identity is immutable.
Email, monitoring, and DNS/certificate rows route to their real owner. Brand,
authentication, and expected fleet remain typed calls to the compatibility
Advanced writer, although their only built-in UI home is Settings. Topology
uses arrays of node and pool strings. Installation identity rows are read-only;
secure posture, KMS state, and the local Setup probe are deployment/file-only
and expose guidance rather than an editor.

The generic `/api/settings` remains unchanged for uncataloged and supported
group-scoped rows. Existing holders of its model permissions can read
non-secret values; do not place confidential mutable data in this catalog.
Catalog protection applies only to the five global allowlisted overrides and
checks both sides of a scope/key move; compatible group-scoped rows remain on
the generic API. POST request bodies are classified `admin_settings` before
dispatch, and mutation audit records actor id, key, action, and a fixed source
without recording the submitted value.
