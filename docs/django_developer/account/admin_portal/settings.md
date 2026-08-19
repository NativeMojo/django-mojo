# Admin Settings catalog

The built-in Admin's Settings feature is the single ongoing-configuration
surface for runtime values django-mojo intentionally exposes. It is a curated
view over the existing `account.Setting` table and
`mojo.helpers.settings.settings`; it does not add another model, store, or
resolution system.

## Registry and provenance

`account.services.admin_settings.Descriptor` is an immutable, opt-in contract.
Account registers core rows from `AccountConfig.ready()`, while dnsman, edge,
and AWS register their own rows from their application `ready()` methods. Identical
registration is idempotent and a conflicting definition fails at startup.
Optional applications simply omit their section when absent.

Every descriptor declares its stable key, category, friendly description,
type, constraints, default, resolver, raw/effective semantics, sensitivity,
scope, writability, owner, and change behavior. Two further fields exist
because the browser cannot derive them from a value:

| Field | Meaning | Example |
|---|---|---|
| `unit` | What an integer counts | `DNSMAN_CERT_RENEW_DAYS` → `"days"` |
| `unset_meaning` | What the platform does while the value is absent | `EDGE_FRAMEWORK_VERSION` → `"installs the newest published release"` |

Both default to `""`. **The registering application owns them**, because only
it knows what its own absence does; the Admin never invents either. Stamp
`unset_meaning` only where absence is a legitimate, understood state — an
unset `KMS_KEY_ID` deliberately carries none, because a missing encryption key
must not be described reassuringly. The fixed category order is
General, Sign-in & registration, Users, Email, Domains & DNS, Edge & Web Apps,
and Security & operations; categories owned by absent optional applications
are omitted. Provenance follows the real
resolver: dynamic Redis/database/deployment/default, protected database-only,
static deployment-only, merged AUTH_CONFIG, or computed posture. Static rows
report only that an ignored database shadow exists. Configured-only rows return
`{"configured": true|false}`. A legacy secret global row is never decrypted and
is excluded from the Redis lookup; it reports only configured state with source
`secret_override`. A secret BASE_URL does not satisfy Setup completeness.
Catalog entries omit paths, environment names, raw ignored values, exceptions,
and secret material. The superuser-only provider status described below names
its configured S3 object key, but never returns either API key.

## Writers and ownership

| Storage owner | Admin behavior | Runtime behavior |
|---|---|---|
| Database `Setting` | Typed immediate write | Shared Redis/database resolution |
| Dedicated model | Routed to its owner form/service | Model-specific resolution |
| `fleet_config` | Typed S3 override publication | Config-sync plus restart |
| Computed/read-only | Status only | No Admin mutation |
| Deployment/bootstrap | Guidance only | Operator-controlled base config |

`GET /api/account/admin/settings` returns schema version 1, ordered categories,
and catalog entries. `POST /api/account/admin/settings` accepts exactly one of:

```json
{"action":"set","key":"ALLOW_EMAIL_CHANGE","value":false}
```

```json
{"action":"clear","key":"ALLOW_EMAIL_CHANGE"}
```

The catalog writer owns only the four `ALLOW_*` booleans and global
`WEBAPP_BASE_URL`. Booleans are strict JSON booleans. The WebApp address is
canonicalized to one public HTTPS hostname origin. IP literals, browser-style
numeric IP forms, localhost, and private/special-use hostname suffixes are
refused, as are credentials, paths, query strings, fragments, and non-443
ports. Reserved example domains are not valid production origins. Values are
global, non-secret, validated, and immediate. Generic `Setting` writes cannot create,
change, rename, move, or delete a catalog-owned global row. Existing
group-scoped rows remain compatible; a move across the global boundary checks
both original and target key/scope.

### Provider setup is addressed by topic

Literal superusers also receive provider setup, and every call to
`provider_setup.test()` / `provider_setup.apply()` names exactly one **topic**.
`TOPICS` is `("geoip", "sms")`; there is no default, and a payload carrying the
other topic's section is refused rather than ignored.

| Topic | Owns | Fleet precondition | S3 |
|---|---|---|---|
| `geoip` | The five static GeoIP values (S3 document) and the encrypted `GEOIP_API_KEY_MOJO` `Setting` row | Required | Conditional `put_object` |
| `sms` | The active system `PhoneConfig` row only | **None** | Never written |

The split is the point. A rejected GeoIP credential used to block an SMS save
outright, and SMS was unreachable on an installation that never publishes fleet
configuration; now each topic saves and fails alone. GeoIP never touches
`PhoneConfig`, and SMS never publishes the fleet document.

`sms` fails closed on its own too: phonehub is an optional app, and
`_normalize_sms` checks `apps.is_installed("mojo.apps.phonehub")` before any
credential check or write, refusing with
`ValueException("Text messaging is not installed on this platform")` rather
than letting a model import fail deep inside the writer.

SMS still performs the same guarded read-only `_published()` fetch `state()`
performs, because the single `expected_revision` token binds
`(edit_revision, published_revision)` — skipping the read would fail every SMS
save on an installation with published fleet config. Where `state()` may
degrade to `remote_error` and a `None` revision, the writer must not: an
unreadable document fails the save closed with the existing
"Provider configuration changed; reload before publishing" vocabulary, because
a silently absent published revision would let a stale token compare equal. The
S3 client is built only inside the branches that need one, so an AWS-less
installation can save SMS without constructing it at all.

**The token is deliberately not split per topic.** One installation-wide edit
revision means a GeoIP publication invalidates an open SMS form and vice versa.
That is the conservative choice: two tokens would let two operators each hold a
form that looks current while the other's write lands.

Both topics keep `transaction.atomic()`, the installation-wide
`User.objects.select_for_update()` lock, a re-read locked `_superuser`,
`_write_configuration_revision`, and `_audit` on success **and** failure with
`topic=` in the message.

The API-key inputs are configured-only: blank means preserve and Clear is
explicit. Static GeoIP keys are protected from generic database writes because
their import-time consumers ignore DB rows. The same-origin, fresh interactive
form can test a supplied or preserved key without sending an SMS, then applies
encrypted DB/model writes before the conditional S3 publication. If publication
loses an ETag race or AWS refuses the write, the surrounding database
transaction rolls back and the response fails without claiming a fleet change.
An unchanged static patch does not create a new S3 revision, but every
successful provider edit advances the database configuration revision so a
stale form cannot overwrite a credential-only or SMS-only edit.

### Stored-key hints

`state()` returns `GEOIP_API_KEY_MOJO_HINT` and `sms.api_key_hint`: the **last
four characters** of the stored key, or `""` for anything shorter than eight
characters and for an absent key. Four trailing characters of a long credential
identify which key is installed without narrowing a guess. Never a prefix
(provider keys are prefixed by convention), never a length, and never the value
itself. The GeoIP hint costs one bounded row fetch and one decrypt inside
`state()`, sliced immediately and never logged. The whole surface is
superuser-only, exactly like the rest of `provider_setup`.

### Provider picker

`state()` also returns `geoip_providers`: `known_providers()` sorts
`mojo.helpers.geoip.PROVIDERS`, or returns `[]` if that import fails. A picker
is convenience only — `validate_settings` remains the authority on what may be
saved — so an unavailable registry costs a list, not a working page. The GeoIP
panel's Primary/Fallback selects and Additional-providers picker always keep a
currently configured value even when it is absent from the list, rather than
silently dropping it on save.

### Persisted verification state

`ADMIN_PROVIDER_VERIFY_STATE` is one non-secret global `account.Setting` row
holding `{"geoip": {ok, code, message, at}, "sms": {…}}`, returned by `state()`
as `verify_state`. It exists so a failing integration is visible on the
settings list without an outbound call on every page load.

**It records the STORED configuration only.** The write rules are the whole
contract:

- `apply()` **success** records its topic — the verified candidate is now what
  the installation is running.
- `apply()` **failure** records nothing. The rejected candidate was never
  stored, so recording it would make the list describe a configuration that
  does not exist.
- `test()` records **only** when the tested credential came from storage: no
  replacement key typed, not a clear request, and the tested URL (and, for
  GeoIP, the sync flag) matching the effective stored one. That single decision
  is made at the existing `api_key or _stored_*_key` fallback point and returned
  alongside the results, so there is one place to get it right.
- A draft test returns its results and persists nothing.

The key is in `FLEET_PROVIDER_KEYS`, so `is_catalog_protected()` is true and
`Setting.save`/`set`/`remove` and the REST surface all refuse it. The writer
therefore mirrors `_write_configuration_revision`: a queryset `.update()` with
`bulk_create` fallback, inside `transaction.atomic()` holding the same
installation lock. Reads tolerate the duplicate `(key, NULL)` rows PostgreSQL
still permits — the oldest row wins rather than the read failing. The blob
stores a host-bearing message from a fixed vocabulary and never a key,
credential, request body, or exception repr.

The GeoIP panel reports the provider edit revision, the published S3 revision,
and the revision loaded by the node serving the request. The edit revision
guards all provider fields; a published/loaded mismatch means the normal
config-sync/restart cycle is pending, not that every node has restarted. Publishing requires
the S3 location, KMS key, application allowlist, and the independent node
bootstrap delegation documented in [Node deployment tooling](../../deploy/README.md#admin-fleet-overrides).

PostgreSQL permits duplicate `(key, NULL)` rows under the legacy constraint.
The catalog reports `duplicate_override`, refuses Set, and offers one Clear
operation. Clear locks and removes every global duplicate atomically. Set and
Clear publish Redis `hset`/`hdel` only from `transaction.on_commit`; rollback
cannot change cache state. Setting a single legacy secret catalog row replaces
it with the validated non-secret value; Clear removes it. The POST body is
classified `admin_settings` before view dispatch so generic request logging
stores only the fixed sensitivity marker. Audit contains actor id, key, action,
and fixed source only—never values or request bodies.

Mutation requires an interactive non-key session, authentication within 600
seconds, and a freshly reread active literal User with exact global
`manage_settings` or `admin` (or literal superuser). `manage_settings` also
admits Admin source/bootstrap. Literal `permissions.admin` is an exact grant,
not a wildcard; only `is_superuser` is a wildcard.

BASE_URL remains focused-Setup-owned; identity is immutable; DNS certificate
knobs are display/owner-only; file-only rows instruct the operator to change
Django production settings and deploy. `AUTH_CONFIG`,
`EDGE_EXPECTED_TOPOLOGY`, and `EDGE_FRAMEWORK_VERSION` keep the existing
`POST /api/account/admin/advanced/settings` typed writer and active literal
superuser plus `manage_advanced`/`admin` authority. That endpoint remains for
compatibility, but Settings is its only browser UI home.

`EDGE_FRAMEWORK_VERSION` ("Framework version hold") is the topology's sibling:
a protected, owner-writable string in Edge & Web Apps that decides which
django-mojo version every fleet deploy installs. It accepts a published
version, `hold` (stay on the last converged fleet version), or unset (newest
published release); `latest`, `none`, and `auto` are unset synonyms, and
anything else is refused with a message naming the accepted forms. Unlike the
other deploy settings it is deliberately database-backed rather than file-only,
because freezing the framework must not itself require a deploy — protection,
not a config file, is what keeps a generic `manage_settings` grant away from it.
Semantics and the refusal behavior live in
[django_developer/edge/deploy.md](../../edge/deploy.md).

The generic `/api/settings` remains for uncataloged and supported group-scoped
rows. Its existing permission holders can read non-secret Setting values, so
the catalog adds no confidential mutable value and does not claim to redact
that legacy surface.

## Browser contract

Settings is first-class navigation after Platform, and it reads like a status
page: one row per thing, one plain sentence each, one level down for anything
you can change. It is built on the shared row components in
`assets/components/rows.js` (`rowSection`, `statusRow`, `rowLink`) — the feature
consumes them and never forks them.

### Rows say what the platform does

`features/settings/language.js` owns every sentence. The old page had one
formatter that collapsed each value into a storage word; those words say a
value exists and never say what happens because of it. Rows now read
"30 days before expiry", "Users cannot change their own email address",
"HTTPS redirect on · secure cookies on · HSTS off", "SES · sender verified ·
3 templates missing".

`sentence(row)` runs three guards **before** any per-key formatter, and that
order is the contract — a value the server refused to resolve has no meaning
for a formatter to narrate:

1. `duplicate_override` → "Conflicting values saved — clear one"
2. `source === 'invalid'` → "Could not be read"
3. unset → `unset_meaning ? "Not set — <meaning>" : "Not set"`

Only then does the per-key registry run (in a `try` that degrades to the
generic formatter), and only then the generic fallback: integer plus `unit`,
boolean, list joined, `configured_only` as "Set". Provenance left the list
entirely except for one word: a value nobody chose shows a muted `· default`,
because that reads differently from the same value someone set. The dot carries
colour only for a failing verification, a pending restart, a duplicate, or an
unreadable value; a healthy row is plain.

`actionFor(row)` gives a row at most one thing to do, and a pointer when there
is nothing: Clear conflicts, Edit/Set into the setting's own panel, a muted
"managed by *owner* →" link where an owner route exists, muted "managed by
deployment" with no link for file-only rows, and nothing at all for immutable
identity. Whole rows are links to their own panel; controls inside a row are
not.

### Integrations, and what a non-superuser sees

The list leads with a synthesised **Integrations** group: GeoIP (collapsing all
six `GEOIP_*` descriptors into one row, with a hidden search corpus of their
keys and labels), text messaging (built entirely from `provider_setup.sms`),
and the two Email rows lifted out of their own category — which therefore
disappears rather than showing one orphan. Category chips derive from the groups
that actually rendered, so there are no empty groups and no dead chips.

A reader without `provider_setup` (i.e. not a superuser) sees neither
integration row. The six `GEOIP_*` descriptors then fall back to individual
read-only rows in Security & operations with their ownership pointers, so
nothing silently vanishes with the collapsed row.

### One topic at a time

Editors live in `features/settings/panels.js`, routed by
`#/settings?focus=<topic-or-key>`: `geoip`, `sms`, `auth`, `topology`, or any
catalog key. An unknown or unavailable focus renders the list rather than an
error. Drill-in links carry `search` and `category`, so returning restores the
filter, and every entry re-fetches — a panel always opens against a fresh
`expected_revision` rather than one the list cached.

Every catalog row has a panel, including rows nobody can change: that is where
provenance, semantics, constraints, and Technical details now live, along with
"Reset to default" and "Clear conflicts". Fleet topology still uses explicit
node/pool tokens, never comma-separated text.

A stored key renders as a state row — "Key set · ····9f2c" with **Replace** and
**Remove** — instead of a password box meaning "leave blank to keep". Replace
swaps in one password input; Remove confirms inline on the row itself, and a
reload abandons the confirm. A failed test attaches to the field it concerns:
`http_401`/`http_403`/`insufficient_permission` to the key, `timeout`/
`http_404`/`connection_failed` to the URL, anything else to the panel.

Database overrides have typed dirty/save/clear states. Owner-managed and
deployment-only rows never look generically editable. Missing Setup produces
one Continue Setup callout. Mutations use the shared non-retrying API,
skeleton, fullscreen busy lease, explicit feedback, and HTTP 440 recent-auth
prompt; the operator selects the mutation again after reauthentication.

Deterministic preview states are available with `bin/admin_preview
--settings-state normal|duplicate|invalid|provider_failed|unset|restricted|delay|error|fresh`.
The three newest are the ones that only exist with provider status:
`provider_failed` (a red integration row carrying a host-bearing diagnosis and
a Fix link, with the other topic unaffected), `unset` (no stored keys, no
version hold), and `restricted` (no `provider_setup` at all — the non-superuser
fallback above).
