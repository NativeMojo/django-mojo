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
scope, writability, owner, and change behavior. The fixed category order is
General, Sign-in & registration, Users, Email, Domains & DNS, Edge & Web Apps,
and Security & operations; categories owned by absent optional applications
are omitted. Provenance follows the real
resolver: dynamic Redis/database/deployment/default, protected database-only,
static deployment-only, merged AUTH_CONFIG, or computed posture. Static rows
report only that an ignored database shadow exists. Configured-only rows return
`{"configured": true|false}`. A legacy secret global row is never decrypted and
is excluded from the Redis lookup; it reports only configured state with source
`secret_override`. A secret BASE_URL does not satisfy Setup completeness.
Responses omit paths, environment names, raw ignored values, exceptions, and
secret material.

## Writers and ownership

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

Settings is first-class navigation after Platform. The default is compact
search, optional category chips, and summary cards. Each card shows friendly
name, effective status/value, source, and at most one primary action. Exact
keys, provenance, semantics, and constraints stay under closed Technical
details. One editor opens at a time and becomes a bottom sheet on narrow
screens. Fleet topology uses explicit node/pool tokens, never comma-separated
text.

Database overrides have typed dirty/save/clear states. Owner-managed and
deployment-only rows never look generically editable. Missing Setup produces
one Continue Setup callout. Mutations use the shared non-retrying API,
skeleton, fullscreen busy lease, explicit feedback, and HTTP 440 recent-auth
prompt; the operator selects the mutation again after reauthentication.

Deterministic preview states are available with `bin/admin_preview
--settings-state normal|duplicate|invalid|delay|error|fresh`.
