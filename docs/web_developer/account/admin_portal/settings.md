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
booleans, plus two fields the client cannot derive from a value:

| Field | Type | Meaning |
|---|---|---|
| `unit` | string | What an integer counts, e.g. `"days"`. `""` when not applicable. |
| `unset_meaning` | string | What the platform does while the value is absent, e.g. `"installs the newest published release"`. `""` when the server deliberately offers no such sentence — notably for a missing encryption key. |

Render `unset_meaning` only for an absent value, and never invent one: a blank
string means the server is declining to reassure, not that you should guess.
Sensitive deployment settings expose only
`{"configured":true|false}`. Arbitrary Django settings, paths, environment
names, ignored raw values, provider responses, and exceptions are omitted from
catalog entries. The superuser-only provider status includes its configured S3
object key and a failure class in `remote_error`, but never either API key.
The fixed order is General, Sign-in & registration, Users, Email, Domains &
DNS, Edge & Web Apps, and Security & operations; sections from absent optional
applications are omitted.

For a literal superuser, GET also includes `provider_setup`: availability, the
application publisher allowlist, configured-only secret flags, the desired
GeoIP values, the effective system SMS row, and edit/loaded/published revisions.
When an override has been published, `geoip` reflects that desired document
even while `pending_restart` is true. `loaded_revision` is only the revision
loaded by the node serving this request, not proof of whole-fleet convergence.
Non-superusers receive no provider setup payload. Three fields are new:

| Field | Meaning |
|---|---|
| `geoip.GEOIP_API_KEY_MOJO_HINT`, `sms.api_key_hint` | The **last four characters** of the stored key, or `""` for an absent key or one shorter than eight characters. Never a prefix, never a length, never the value. Use it to say "Key set · ····9f2c" instead of a password box meaning "leave blank to keep". |
| `geoip_providers` | Sorted provider identifiers this installation can dispatch to. A convenience for building a picker — the server's validator remains the authority, so a configured value that is not in the list must still render as selected. |
| `verify_state` | Per-topic verification of the **stored** configuration: `{ok, code, message, at}` per topic, absent until something has been verified. Use it to mark a failing integration on a list without testing on every page load. A test of an unsaved draft never appears here. |

The response shape is:

```json
{
  "provider_setup": {
    "available": true,
    "bucket_configured": true,
    "restart_configured": true,
    "object_key": "config/prod/django.override.json",
    "delegated_keys": [
      "GEOIP_ADDITIONAL_PROVIDERS",
      "GEOIP_FALLBACK_PROVIDER",
      "GEOIP_MOJO_PROVIDER_URL",
      "GEOIP_MOJO_SYNC_ENABLED",
      "GEOIP_PRIMARY_PROVIDER"
    ],
    "loaded_revision": "0123456789abcdef0123456789abcdef",
    "published_revision": "0123456789abcdef0123456789abcdef",
    "configuration_revision": "fedcba9876543210fedcba9876543210",
    "published_version": "s3-version-id",
    "pending_restart": false,
    "remote_error": null,
    "geoip": {
      "GEOIP_PRIMARY_PROVIDER": "mojo",
      "GEOIP_FALLBACK_PROVIDER": "ipinfo",
      "GEOIP_ADDITIONAL_PROVIDERS": [],
      "GEOIP_MOJO_PROVIDER_URL": "https://api.mojoverify.com",
      "GEOIP_MOJO_SYNC_ENABLED": false,
      "GEOIP_API_KEY_MOJO_CONFIGURED": true,
      "GEOIP_API_KEY_MOJO_HINT": "9f2c"
    },
    "geoip_providers": ["ip-api", "ipinfo", "ipstack", "maxmind", "mojo"],
    "sms": {
      "configured": true,
      "remote_url": "https://sms.example.com",
      "api_key_configured": true,
      "api_key_hint": "4d81",
      "test_mode": false
    },
    "verify_state": {
      "geoip": {"ok": false, "code": "http_401",
                "message": "api.mojoverify.com rejected the API key",
                "at": "2026-08-18T09:14:00+00:00"},
      "sms": {"ok": true, "code": null, "message": "Connection verified",
              "at": "2026-08-18T09:14:00+00:00"}
    }
  }
}
```

`verify_state` is written by the server only for the configuration it actually
stores: a successful save records its topic, a failed save records nothing, and
a test records only when the credential and target both came from storage.
A client cannot cause an entry by testing a draft, so an entry can be trusted to
describe what the installation is running.

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

The same fresh-auth endpoint accepts the superuser-only provider actions, and
**every provider request names exactly one `topic`** — `"geoip"` or `"sms"`.
There is no default: a missing or unrecognized topic is refused. The body must
be exactly `action`, `topic`, and `providers`, and `providers` must carry only
that topic's section plus `expected_revision`. Sending the other topic's
section is an error, not an ignored extra, so a save can never quietly write an
integration the operator did not open.

```json
{
  "action": "configure_providers",
  "topic": "geoip",
  "providers": {
    "expected_revision": "0123456789abcdef0123456789abcdef",
    "geoip": {
      "GEOIP_PRIMARY_PROVIDER": "mojo",
      "GEOIP_FALLBACK_PROVIDER": "ipinfo",
      "GEOIP_ADDITIONAL_PROVIDERS": [],
      "GEOIP_MOJO_PROVIDER_URL": "https://api.mojoverify.com",
      "GEOIP_MOJO_SYNC_ENABLED": false,
      "GEOIP_API_KEY_MOJO": "optional-new-secret",
      "clear_api_key": false
    }
  }
}
```

```json
{
  "action": "configure_providers",
  "topic": "sms",
  "providers": {
    "expected_revision": "0123456789abcdef0123456789abcdef",
    "sms": {
      "remote_url": "https://sms.example.com",
      "api_key": "optional-new-secret",
      "clear_api_key": false,
      "test_mode": false
    }
  }
}
```

Use the identical body with `"action":"test_providers"` for a zero-side-effect
credential and permission check. Both provider actions require a fresh
interactive literal-superuser Bearer session and an `Origin` exactly matching
the Admin request origin. `results` carries **one key — the requested topic**:

```json
{"tested":true,"topic":"sms","success":true,"results":{"sms":{"success":true,"code":null,"message":"Connection verified"}}}
```

A failure names the host that answered, so it can be shown as-is:

```json
{"tested":true,"topic":"geoip","success":false,"results":{"geoip":{"success":false,"code":"http_401","message":"api.mojoverify.com rejected the API key"}}}
```

Attach the message to the field it concerns: `http_401`, `http_403`, and
`insufficient_permission` are about the key; `timeout`, `http_404`, and
`connection_failed` are about the URL.

**No stored value is ever returned to a client.** The response carries
configured flags and four-character hints only; there is no request, parameter,
or graph that returns `GEOIP_API_KEY_MOJO` or the SMS key. Blank or omitted
secret values preserve the encrypted value; clearing requires the explicit
boolean.

What each topic writes differs, and so does what it requires:

- **`geoip`** requires the full fleet publishing plane (object location, all
  provider delegations, KMS key, config-sync restart). After the check passes it
  writes the encrypted database secret, then publishes one KMS-encrypted,
  integrity-marked S3 override under an ETag precondition and returns its
  revision/version. It never touches the SMS row.
- **`sms`** has **no fleet precondition** and works on an installation with no
  publishing plane at all. It writes only the system `PhoneConfig` row and never
  writes S3 — but it still reads the published document, because
  `expected_revision` binds both the edit revision and the published revision.

`expected_revision` is the current `configuration_revision`, which advances for
every successful provider edit — including DB-only key or SMS changes — and is
also bound to the published S3 revision, so an operator restore invalidates an
open form. It is deliberately **not** split per topic: one token means a GeoIP
publication invalidates an open SMS form and vice versa, which is the safe
direction. DB/model values take effect without a restart; static GeoIP selection
takes effect after config sync installs the composed file and restarts the
service. The DB/model writes share a stable installation-wide lock rather than
the acting user's row, and roll back if conditional S3 publication fails. An
unchanged static document skips S3 publication while still advancing the edit
revision. Mutation responses name their topic:

```json
{"published":true,"unchanged":false,"topic":"geoip","revision":"0123456789abcdef0123456789abcdef","published_revision":"0123456789abcdef0123456789abcdef","version_id":"s3-version-id","pending_restart":true,"results":{"geoip":{"success":true}}}
```

```json
{"published":false,"unchanged":true,"topic":"sms","revision":"fedcba9876543210fedcba9876543210","published_revision":null,"version_id":null,"pending_restart":false,"results":{"sms":{"success":true}}}
```

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
