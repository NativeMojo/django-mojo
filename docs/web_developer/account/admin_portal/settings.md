# Admin Settings API

The built-in Admin exposes a curated, capability-gated Settings catalog over
django-mojo's existing database-backed settings system.

| Method | Route | Authority |
|---|---|---|
| `GET` | `/api/account/admin/settings` | exact global Settings or owner-display authority |
| `POST` | `/api/account/admin/settings` | fresh interactive `manage_settings`/`admin` |
| `POST` | `/api/account/admin/advanced/settings` | fresh interactive `manage_advanced`/`admin` plus literal superuser; typed compatibility owner |

The GET response has `schema_version`, ordered `sections`, and server-owned
`entries`. Each entry supplies friendly label, description, type, constraints,
effective value/status, source, scope, owner, change behavior, and capability
booleans. Sensitive deployment settings expose only
`{"configured":true|false}`. Arbitrary Django settings, paths, environment
names, ignored raw values, provider responses, and exceptions are omitted.

Sources include `database_cache`, `database`, `deployment`, `default`,
`computed`, merged `database+deployment+defaults`, `invalid`, and
`duplicate_override`. A duplicate is fail-closed, not a random first-row
winner.

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

Only four self-service booleans and the global default WebApp public HTTPS
origin are catalog-writable initially. IP literals, browser-style numeric IP
forms, localhost, and private/special-use hostname suffixes are refused.
Values are non-secret. Clear is
idempotent, removes every conflicting global override, and reveals the
deployment/default source. API keys and group tokens are refused, recent auth
is 600 seconds, and HTTP 440 requires explicit reauthentication without write
replay.

Public API address routes to focused System Setup. Identity is immutable.
Email, monitoring, and DNS/certificate rows route to their real owner. Brand,
authentication, and expected fleet remain typed calls to the compatibility
Advanced writer, although their only built-in UI home is Settings. Topology
uses arrays of node and pool strings.

The generic `/api/settings` remains unchanged for uncataloged and supported
group-scoped rows. Existing holders of its model permissions can read
non-secret values; do not place confidential mutable data in this catalog.
