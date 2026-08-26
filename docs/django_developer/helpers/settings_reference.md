# Framework Settings Reference

This reference lists framework-recognized setting keys (names only, no values).

## Startup/Bootstrap Keys (Restart Required)

These are read while URL/module bootstrap happens, so changes require a process restart.

- `DATABASE_POOL_OPTIONS` — **file-only**, disabled unless it is a strict
  psycopg 3 dictionary with positive `max_size` and `timeout`, non-negative
  `min_size` no greater than `max_size`, and optional positive `max_idle`,
  `max_lifetime`, `max_waiting`, and `reconnect_timeout`. `True`, unknown keys
  and alias-embedded pool intent are
  rejected. Even a valid candidate is injected only into `default` when the
  process proves exact `MOJO_PROCESS_ROLE=api` and
  `MOJO_PROCESS_LAUNCHER=asgi`; every other role strips it and keeps an
  ordinary connection. `False` needs no sizing settings and is the production
  default. An enabled API/ASGI candidate also requires `MIDDLEWARE` to be a
  list or tuple; django-mojo injects its database-pool error boundary
  automatically for view exceptions; `AuthenticationMiddleware` handles pool
  failures from database-backed bearer handlers, including API keys, at source
  so both paths return a bounded `503`. A database-backed `Setting` row is
  ignored.
- `DATABASE_POOL_ALIASES` — **file-only** exact allowlist. The laboratory
  accepts only `["default"]`; readers and multi-destination budgets are not
  inferred. Required only when pooling is enabled.
- `DATABASE_POOL_API_WORKERS` — **file-only** positive integer count of ASGI
  workers per API node. No default; enabling without an explicit value fails.
- `DATABASE_POOL_NODE_COUNT` — **file-only** positive integer count of API
  nodes. No default; enabling without an explicit value fails.
- `DATABASE_POOL_OBSERVER_RESERVE` — **file-only** positive integer number of database
  connections reserved from pool headroom for independent observation;
  default `2`. Required capacity is constrained by both the 60-percent ceiling
  and actual headroom after server-reserved, live, and observer connections.
- `DATABASE_POOL_IDENTITY` — **file-only** exact dictionary containing only
  `project`, `node`, `application`, and `deployment`. Every value is a bounded
  printable identifier. Required only for an enabled candidate and used to
  produce a stable, PostgreSQL-bounded `application_name` per worker.
- `DATABASE_POOL_LAB_PROBE_ENABLED` — **file-only**, default false/absent.
  Enables the local, per-worker Unix-socket exhaustion probe only after the
  ordinary pool candidate is valid and active. It adds no HTTP route.
- `DATABASE_POOL_LAB_TRACE_LEASES` — **file-only**, default false/absent.
  Enables MojoLand's per-worker acquire/return correlation only while the
  native pool is active. It captures bounded request-path, thread, and Python
  stack evidence for the pooling laboratory; do not enable it as ordinary
  production telemetry.
- `MOJO_POOL_TELEMETRY_ROOT` / `MOJO_POOL_ERROR_FILE` /
  `MOJO_POOL_PROBE_SOCKET` — launcher-owned local output paths for atomic
  worker snapshots, the DB-independent acquisition-error signal, and an
  optional exact probe socket. They do not activate pooling.
- `MOJO_PROCESS_ROLE` / `MOJO_PROCESS_LAUNCHER` — launcher-owned environment
  identity. Applications must overwrite inherited values before importing
  Django. Only `api` / `asgi` can receive the pool; management, jobs, cron,
  tests, preflight and unknown/missing pairs remain unpooled.
- `DATABASE_CONN_MAX_AGE` — **file-only** compatibility default for
  `CONN_MAX_AGE` (seconds), default `0`. An explicit per-alias value always
  wins. Native psycopg pooling always uses
  `CONN_MAX_AGE = 0`. A database-backed `Setting` row is ignored.
- `DATABASE_CONN_HEALTH_CHECKS` — **file-only** default `CONN_HEALTH_CHECKS`
  for server-backed aliases, default `True`. With a native pool, Django checks
  a connection when it is checked out so an idle-timeout or restart casualty
  is replaced instead of surfacing as a 500.
- `DATABASE_READER_HOST` — **file-only** reader database hostname. When set,
  django-mojo derives a `reader` alias from `DATABASES["default"]` (unless one
  is explicitly declared) and installs the database router and request
  middleware that route safe, unpinned reads to it. Unset, no reader alias or
  routing is installed. A database-backed `Setting` row is ignored.
- `DATABASE_READER_PORT` — **file-only** optional reader port. When django-mojo
  derives the `reader` alias, it replaces the copied primary port; unset, the
  primary port is retained. Ignored when the application explicitly declares
  `DATABASES["reader"]`.
- `DATABASE_READER_CONN_MAX_AGE` — **file-only** connection lifetime in seconds
  for a django-mojo-derived `reader` alias. The pooling laboratory never pools
  a reader; this legacy setting affects its ordinary connection only.
  Ignored when the application
  explicitly declares `DATABASES["reader"]`.
- `MOJO_API_MODULE`
- `MOJO_APPEND_SLASH`
- `MOJO_PREFIX`
- `REST_AUTO_PREFIX`

## Runtime Keys

These are read through `mojo.helpers.settings.settings` during normal runtime.

### ACCOUNT

- `ACCOUNT_CLOSURE_HANDLER` — **file-only** (read with `settings.get_static`).
  Dotted path to a product callable `handler(user)` that owns permanent account
  closure, called by the `account/deactivate/confirm` endpoint in place of a
  direct `user.pii_anonymize()`. Default `None` — unset, the confirm endpoint
  anonymizes directly. A DB-backed `Setting` row is deliberately ignored: the
  value names code the worker imports and calls, so honouring a DB row would
  turn `manage_settings` into arbitrary code execution. Fails closed — an
  unresolvable path, a raising handler, or a handler that returns without
  closing the account all anonymize nothing and leave the account active. See
  [Account Closure Delegation](../account/disable_lifecycle.md#account-closure-delegation-account_closure_handler).

### ADMIN FLEET CONFIG

- `ADMIN_FLEET_CONFIG_BUCKET` — S3 bucket containing the exact Admin-owned
  override object; falls back to `AWS_CONFIG_BUCKET`.
- `ADMIN_FLEET_CONFIG_PREFIX` — required non-empty environment prefix; falls
  back to `AWS_CONFIG_PREFIX`.
- `ADMIN_FLEET_CONFIG_FILENAME` — bounded object basename, default
  `django.override.json`.
- `ADMIN_FLEET_CONFIG_KMS_KEY_ID` — SSE-KMS key for publication; falls back to
  `KMS_KEY_ID`.
- `ADMIN_FLEET_CONFIG_ALLOWED_KEYS` — positive application-side fleet key
  delegation. Default empty (publisher disabled).
- `ADMIN_FLEET_CONFIG_RESTART_ENABLED` — explicit confirmation that config-sync
  restarts services after install; required to enable Admin publication.

All are file-only bootstrap controls read with `get_static()`. None can grant
itself through the Admin. See [Admin fleet overrides](../deploy/README.md#admin-fleet-overrides).

### ALLOW

- `ALLOW_EMAIL_CHANGE` — dynamic boolean, default `True`; Admin Settings can
  manage a global non-secret override.
- `ALLOW_PHONE_CHANGE` — dynamic boolean, default `True`; Admin Settings can
  manage a global non-secret override.
- `ALLOW_PHONE_LOGIN`
- `ALLOW_SELF_DEACTIVATION` — dynamic boolean, default `True`; Admin Settings
  can manage a global non-secret override.
- `ALLOW_USER_REGISTRATION`
- `ALLOW_USERNAME_CHANGE` — dynamic boolean, default `True`; Admin Settings can
  manage a global non-secret override.

These four Admin-managed values are read with `kind="bool"`, so stored JSON
`false` remains false. The catalog uses the existing `account.Setting` table,
protects only global rows from alternate writers, and preserves supported
group-scoped rows. See [Admin Settings catalog](../account/admin_portal/settings.md).

### ALLOWED

- `ALLOWED_REDIRECT_URLS` — list, default `[]`. URLs accepted as the OAuth
  `redirect_uri` landing page on `GET /api/auth/oauth/<provider>/begin`. Matched
  as a **URL**, not a string prefix: same scheme (`http`/`https` never substitute
  for each other), same host case-folded, same port (scheme default when
  absent), and a path at or under the entry path on a `/` segment boundary.
  Query and fragment are ignored on both sides; `*.` wildcards are **not**
  supported and are skipped as unusable entries; list IDN hosts in punycode. A
  path carrying a `.`/`..` segment (any `%2e` spelling) is refused outright, not
  normalized, on both the `redirect_uri` and every entry. A
  **custom scheme** (`myapp://callback`, a mobile deep link) is supported under
  narrower rules — exact scheme + exact case-folded authority + the same path
  rule, no default ports and no wildcards — and the `/callback` bounce emits it
  as the `Location` (the 302 widens to exactly the one custom scheme admitted
  here, or the deployment's own `OAUTH_REDIRECT_URI`; anything else is a 400).
  Empty (or unset) refuses any `redirect_uri` outright. **Not the only source**: the group this request
  resolved (`?group=` / `?group_uuid=`) contributes its own
  `Group.metadata["allowed_redirect_urls"]`, inherited up the parent chain, so
  the effective allowlist is this setting plus that tenant-writable list. That
  per-group value is coerced by the **same** `kind="list"` rules
  (`redirect_allowlist.coerce_entries`), so a bare string is the single entry it
  spells and a non-list value (int / bool / float / dict — an object's keys are
  **not** entries) is dropped as unusable. Read
  through `settings.get` with `kind="list"`, so a **global** `Setting` row works
  — it holds text, and both a JSON array and a comma-separated string are
  coerced correctly. A group-scoped `Setting` row is never consulted (per-group
  entries live in group metadata instead). An entry that can never match is
  skipped and files a Redis-suppressed incident rather than a per-request log
  line: `auth:redirect_allowlist_unusable_entry` (level 3) for a broken entry in
  this setting, `auth:redirect_allowlist_tenant_entry_unusable` (level 1,
  budgeted) for a broken group entry, and `auth:oauth_redirect_refused` (level 3,
  budgeted) when a `redirect_uri` matches nothing. See
  [OAuth](../account/oauth.md#allowlist-configuration),
  [redirect allowlist incidents](../account/oauth.md#redirect-allowlist-incidents),
  and [the per-group source](../account/oauth.md#the-per-group-source).

### API

- `API_METRICS`
- `API_METRICS_GRANULARITY`
- `API_THROTTLE_ENABLED` — global per-identity API throttle enforcement
  on/off (default `True`; accounting runs regardless). See
  [Authenticated-Abuse Hardening](../security/abuse_hardening.md#settings).
- `API_THROTTLE_USER`
- `API_THROTTLE_APIKEY`
- `API_THROTTLE_WINDOW`
- `API_THROTTLE_EXEMPT_PREFIXES`
- `API_THROTTLE_REPORT_FLOOR`
- `API_THROTTLE_CONFIG_TTL`

### APIKEY

- `APIKEY_PERMS_PROTECTION` — dict, **merged over a framework floor** (read with
  `kind="dict"`, so a DB-backed `Setting` JSON string is honored). Maps a
  permission key → the permission(s) the granter must hold to assign it to an
  `ApiKey`, gating `ApiKey.set_permissions` on REST write. The effective map is
  `{**configured, **ApiKey.APIKEY_PERMS_PROTECTION_DEFAULTS}` — currently the
  floor protects `geoip_sync`, `dnsman_acme_federation`, `edge_node`, and
  `mojosec_ingest` with their matching global `sys.*` grants. Deployments may
  add protected permissions, but cannot override or relax a framework-floor
  entry. It is a merge rather than a plain default because `settings.get`
  returns a configured value wholesale, which would otherwise drop the floor
  the moment a deployment set this at all.
  Mirrors
  [`MEMBER_PERMS_PROTECTION`](#member); `sys.`-prefixed requirements escalate to
  a global grant. Stops a group admin from self-minting a key with permissions
  they aren't entitled to grant.

### APPLE

- `APPLE_CLIENT_ID`
- `APPLE_KEY_ID`
- `APPLE_PRIVATE_KEY`
- `APPLE_TEAM_ID`

### AUTH

- `AUTH_BEARER_HANDLERS` — **file-only** (`settings.get_static`). Dict, default
  `{}`. Maps an `Authorization` scheme prefix to a `(token, request) ->
  (instance, error)` handler path. `bearer` and `apikey` are built in and need
  no entry. **Replaces the default wholesale — there is no merge.** Registering
  [group-scoped tokens](../account/auth.md#group-scoped-tokens) is opt-in here:
  `{"grouptoken": "mojo.apps.account.services.group_token.validate_token"}`.
  An unregistered scheme is answered with `401 Invalid token type`.
- `AUTH_BEARER_NAME_MAP` — **file-only** (`settings.get_static`). Dict, default
  `{"bearer": "user", "apikey": "user"}`. Names the request attribute each
  scheme's resolved instance is assigned to. **Replaces the default wholesale —
  there is no merge**, so always write the COMPLETE map. Declaring only a new
  entry silently un-maps `bearer` and `apikey`: `request.user` never populates
  and every request degrades to anonymous 403s with no diagnostic. With group
  tokens registered the full map is
  `{"bearer": "user", "apikey": "user", "grouptoken": "user"}`.
- `AUTH_CSP_DIRECTIVES` — **file-only** (`settings.get_static`). Dict, default
  `{}`. Per-directive merge over the default Content-Security-Policy on the
  hosted auth pages. A present key replaces that directive wholesale, an empty
  value drops it, an unknown key is emitted as-is (so you can add `report-uri`).
  The per-request nonce is always appended to the final `script-src` and cannot
  be removed. Has no effect while `AUTH_CSP_ENABLED` is off. See
  [Content Security Policy](../security/csp.md).
- `AUTH_CSP_ENABLED` — **file-only** (`settings.get_static`). Bool, default
  **`False`**. The **opt-in switch** for the CSP on the hosted auth pages:
  `True` sends the header, and with it unset or `False` no header is sent at
  all. The `nonce="{{ csp_nonce }}"` attributes are stamped into the templates
  either way — a nonce with no CSP is inert, so the default is a no-op. See
  [Content Security Policy](../security/csp.md).
- `AUTH_CSP_REPORT_ONLY` — **file-only** (`settings.get_static`). Bool, default
  `False`. `True` sends `Content-Security-Policy-Report-Only` instead of the
  enforcing header — set it alongside `AUTH_CSP_ENABLED = True` as the first
  step of a rollout, especially if you ship your own auth-template overrides.
  Meaningless on its own. See [Content Security Policy](../security/csp.md).
- `AUTH_HANDOFF_CODE_TTL` — int, default `60`. Seconds before a cross-origin
  handoff code expires.
- `AUTH_HANDOFF_ALLOWED_URLS` — list, **unset by default (monitor mode)**.
  Destination URLs `POST /api/auth/handoff` may mint a code for, matched on
  **exact host + path prefix** (`https://*.example.com/` admits one extra
  dot-free label); a destination or entry whose path carries a `.`/`..` segment
  (any `%2e` spelling) is refused outright, not normalized. A custom scheme (a
  mobile deep link) is usable here too —
  same shared matcher, same narrower custom-scheme rules as
  `ALLOWED_REDIRECT_URLS` — but note that it only serves a **custom frontend**
  calling `POST /api/auth/handoff` directly: the bundled hosted auth pages
  scheme-guard `?redirect=` in the browser and refuse a custom scheme before the
  server is consulted. **Setting it — any list, even `[]` — turns enforcement on**:
  `redirect_uri` becomes required and an unlisted destination gets a `400` with
  no code minted, plus an `auth:handoff_destination_refused` incident. Unset,
  with no resolver either, is monitor mode: the code is minted as always and an
  unlisted destination files an `auth:handoff_destination_unlisted` incident
  naming it. Deliberately separate from `ALLOWED_REDIRECT_URLS`, which was
  written under different semantics (wildcards are inert there). See
  [Cross-Origin Auth Handoff](../account/auth.md#cross-origin-auth-handoff).
- `AUTH_HANDOFF_RESOLVER` — dotted path to `fn(url, request=None) -> bool`,
  loaded via `mojo.helpers.modules.load_function()` and cached. Default `""`.
  **When set it decides** and `AUTH_HANDOFF_ALLOWED_URLS` is not consulted — the
  answer for a multi-tenant platform whose destinations live in a DB, not a
  settings file. **Setting it also turns handoff enforcement on**, exactly like
  the list. The resolver is security-critical deployment code: it must compare
  hosts exactly (never substring/prefix), check the scheme, and fail closed. The
  framework wraps the call — a resolver that raises, or a dotted path that fails
  to import, refuses **everything** and is logged. Unlike `USER_LOGIN_HANDLER`,
  it never fails open.
- `AUTH_HANDOFF_GROUP_TOKEN_MODE` — **file-only** (`settings.get_static`). One
  of `"off"` (default), `"monitor"`, `"enforce"`. When gating is on, a handoff
  code minted for a **gated destination host** exchanges into a group-scoped
  token package instead of a platform JWT pair, and the OAuth completion leg
  refuses that destination outright. `monitor` predicts both outcomes into the
  incident feed and binds nothing. An unrecognized string is treated as
  `enforce` and logged — a typo in a security switch must not disable it.
  **`enforce` additionally requires `AUTH_HANDOFF_ALLOWED_URLS` or
  `AUTH_HANDOFF_RESOLVER`**, and refuses every handoff without one. File-only
  because a DB/Redis-backed `Setting` row is writable through the generic
  settings REST plane, and a remotely-writable mode would let settings-write
  access silently downgrade every gated destination back to a platform JWT. See
  [Gated destinations](../account/auth.md#gated-destinations--deliver-a-group-token-instead-of-a-jwt).
- `AUTH_HANDOFF_GROUP_TOKEN_HOSTS` — **file-only** (`settings.get_static`,
  `kind="dict"`). `{host_entry: group_uuid}`, default `{}`. A **deny** rule, so
  the matching is deliberately looser than the allowlist's: entries are hosts
  (a full URL is reduced to its host with a warning — scheme, port and path are
  ignored), and **every entry covers the host and all of its subdomains at any
  depth** (`example.com` and `*.example.com` are the same rule). List IDN hosts
  in punycode; IP-literal and single-label entries are refused in every
  encoding. A defective entry, or two entries normalizing to one host with
  different groups, refuses every handoff while gating enforces.
- `AUTH_HANDOFF_GROUP_TOKEN_RESOLVER` — **file-only** (`settings.get_static`).
  Dotted path to `fn(url, request=None) -> Group | uuid | int pk | None`,
  default `""`. **When set it decides** and the host map is not consulted.
  Fails closed exactly like `AUTH_HANDOFF_RESOLVER`: raising, failing to
  import, naming an unknown or inactive group, or returning a junk type all
  refuse.
- `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` — **file-only** (`settings.get_static`). A fixed code accepted in place of the real SMS code during phone verification; never set it in production. Deliberately not readable from the DB/Redis settings plane, so a `Setting` row cannot arm an authentication bypass at runtime.

### AWS

- `AWS_DEFAULT_REGION`
- `ADMIN_AWS_INVENTORY_ENABLED` — **file-only**
  (`settings.get_static`), bool, default `False`. Opts the built-in Admin
  Advanced API into one bounded EC2, RDS, and ElastiCache inventory page.
  When false the section reports `unconfigured`. It never creates or mutates
  resources and omits network endpoints and IP addresses.
- `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` — **protected, DB-backed with a file
  fallback** (`settings.get`, `kind="list"`), default `[]`. Exact SNS topic ARN
  allowlist for `/api/aws/cloudwatch/sns/alarm`; missing/empty denies all.
  A protected `Setting` row wins over `django.conf` as a whole value, so System
  Setup merges both planes when it reconciles monitoring — see
  [settings.md](settings.md#protected-settings-live-on-two-planes).
- `AWS_GUARDDUTY_FINDING_TOPIC_ARNS` — **file-only**
  (`settings.get_static`, `kind="list"`), default `[]`. Exact SNS topic ARN
  allowlist for `/api/aws/guardduty/sns/finding`; missing/empty denies all.
  Deliberately **separate** from `AWS_CLOUDWATCH_ALARM_TOPIC_ARNS` — a topic
  allowlisted for alarms must not be able to deliver findings, or confirm a
  subscription, on the GuardDuty receiver. See
  [../aws/guardduty.md](../aws/guardduty.md).
- `AWS_MONITORING_NAME` — **file-only**, optional stable deployment slug used
  by `aws-check` for owned SNS topic and CloudWatch alarm names. Falls back to
  the static `BASE_URL` hostname.
- `AWS_CHECK_CRON_MAX_AGE` — **file-only**, default `180` seconds. Maximum age
  of a completed cron-dispatch heartbeat accepted by `aws-check`.
- `AWS_CHECK_CPU_CREDIT_FLOOR` — **file-only**, int, default `20`.
  `CPUCreditBalance` at or below this alarms. Created only on burstable
  families (`t2`/`t3`/`t3a`/`t4g`, `db.t*`), which are the only ones that
  publish the metric.
- `AWS_CHECK_RDS_FREEABLE_MEMORY_FLOOR` — **file-only**, int bytes, default
  `268435456` (256 MiB). RDS `FreeableMemory` at or below this alarms.
- `AWS_CHECK_RDS_MAX_CONNECTIONS` — **file-only**, int, default `500`. RDS
  `DatabaseConnections` at or above this alarms. **The default is deliberately
  forgiving.** RDS derives `max_connections` from instance memory — roughly 112
  on `db.t3.micro`, ~225 on `db.t3.small`, ~405 on `db.t3.medium` — so no single
  default is correct everywhere. Erring high means it never fires spuriously,
  which matters because a chronically-firing alarm gets muted and muting the
  operations topic silences every other alarm with it. The cost: on a class whose
  ceiling is below 500 the alarm cannot fire at all. Tune it to ~80% of your
  instance class's `max_connections`.
- `AWS_CHECK_CERT_EXPIRY_DAYS` — **file-only**, int, default `14`. The
  deployment-wide certificate-expiry alarm fires when the soonest certificate is
  this many days from expiring. That alarm uses `TreatMissingData=breaching`, so
  a publisher that stops running alarms too; see
  [../aws/aws_check.md](../aws/aws_check.md).
- `AWS_VERSION_DRIFT_ENABLED` — **file-only** (`settings.get_static`), bool,
  default `False`. Arms the daily managed-service version drift scan
  (`mojo.apps.aws.cronjobs.check_version_drift`). Read inside the cron
  function, so it is not frozen at import.
- `AWS_INFRA_DRIFT_ENABLED` — **file-only** (`settings.get_static`), bool,
  default `False`. Arms the daily fleet drift scan
  (`mojo.apps.aws.cronjobs.check_infra_drift`, 07:20), which compares the
  instances actually serving traffic against the nodes recorded in
  `EDGE_EXPECTED_TOPOLOGY`. Read inside the cron function, so it is not frozen
  at import. See [../aws/infra_drift.md](../aws/infra_drift.md).
- `AWS_VERSION_DRIFT_DEADLINE_DAYS` — **file-only**, int, default `180`. A
  published standard-support end date this close (or closer) raises the drift
  event to level 8; an already-past date is level 10. See
  [../aws/version_drift.md](../aws/version_drift.md).
- `AWS_KEY`
- `AWS_REGION`
- `AWS_SECRET`

### BASE

- `BASE_URL`

### BEDROCK

- `BEDROCK_EMBED_MODEL`
- `BEDROCK_REGION` — falls back to `AWS_REGION`

### BOUNCER

- `BOUNCER_ACCENT_COLOR`
- `BOUNCER_ALLOWED_ORIGINS` — list of origins allowed to call the bouncer
  endpoints with credentials (`Access-Control-Allow-Credentials: true` plus a
  specific `Access-Control-Allow-Origin`). Consulted first and unconditionally,
  in both origin modes; it is the mechanism that names the permitted origins
  once `BOUNCER_ALLOW_ANY_ORIGIN` is `False`. Matched by exact string, so
  non-http entries such as `capacitor://localhost` are valid. **File-only**
  (`settings.get_static`).
- `BOUNCER_ALLOW_ANY_ORIGIN` — **file-only** (`settings.get_static`, default
  `True`). By default the three public bouncer endpoints (`assess`, `event`,
  `message`) echo any well-formed `http(s)` request `Origin` with credentials —
  this is an open API platform and third-party callers are expected. Set it
  `False` to restrict those endpoints to `BOUNCER_ALLOWED_ORIGINS`. `verify_pass`
  and the permission-gated admin endpoints (`device`, `signal`, `signature`) are
  never covered either way; `Origin: null` and malformed origins are always
  refused. An uncoercible value degrades to the declared default (`True`), so a
  typo'd opt-out does not silently take effect. See
  [account/bouncer.md](../account/bouncer.md#cross-origin-embedding).
  Deliberately not readable from the DB/Redis settings plane, so a `Setting` row
  cannot change the origin policy at runtime.
- `BOUNCER_CHALLENGE_BRAND`
- `BOUNCER_CHALLENGE_LOGO_URL`
- `BOUNCER_CONTACT_PATH`
- `BOUNCER_LEARN_CAMPAIGN_THRESHOLD`
- `BOUNCER_LEARN_ENABLED`
- `BOUNCER_LEARN_FP_THRESHOLD`
- `BOUNCER_LEARN_MIN_SCORE`
- `BOUNCER_LEARN_SIGNAL_SET_TTL`
- `BOUNCER_LEARN_SUBNET_THRESHOLD`
- `BOUNCER_LEARN_SUBNET_TTL`
- `BOUNCER_LEARN_UA_THRESHOLD`
- `BOUNCER_LEARN_UA_TTL`
- `BOUNCER_LOGIN_PATH`
- `BOUNCER_LOGO_URL`
- `BOUNCER_PASS_COOKIE_DOMAIN` — `Domain` attribute for the `mbp` pass cookie
  (e.g. `'.example.com'`), so a subdomain deployment shares it between the app
  host and the bouncer host. It also widens the set of subdomains the cookie
  rides along on for credentialed same-site calls.
- `BOUNCER_PASS_COOKIE_TTL`
- `BOUNCER_PUBLIC_MESSAGE_MAX_LENGTH`
- `BOUNCER_REGISTER_PATH`
- `BOUNCER_REQUIRE_TOKEN`
- `BOUNCER_SCORE_WEIGHTS`
- `BOUNCER_SUCCESS_REDIRECT`
- `BOUNCER_THRESHOLDS`
- `BOUNCER_THRESHOLDS_OVERRIDES`
- `BOUNCER_TOKEN_TTL`

### DEACTIVATE

- `DEACTIVATE_TOKEN_TTL`

### DNSMAN

- `DNSMAN_ACME_CONTACT_EMAIL`
- `DNSMAN_ACME_DIRECTORY_URL`
- `DNSMAN_ACME_HUB_ZONE` — enables the optional protected ACME delegation hub;
  file-only. See [ACME federation](../dnsman/AcmeFederation.md).
- `DNSMAN_ACME_HUB_HOSTED_ZONE_ID`
- `DNSMAN_ACME_HUB_TTL`
- `DNSMAN_ACME_HUB_LEASE_SECONDS`
- `DNSMAN_ACME_HUB_SWEEP_LIMIT`
- `DNSMAN_ACME_HUB_URL` — downstream challenge client's HTTPS hub origin;
  file-only, with plain HTTP allowed only for localhost/loopback development.
- `DNSMAN_ACME_HUB_API_KEY` — downstream project's protected federation ApiKey;
  file-only and never logged.
- `DNSMAN_ACME_HUB_CONNECT_TIMEOUT` — downstream connect timeout (default `5`,
  strict range 0.1–30 seconds).
- `DNSMAN_ACME_HUB_READ_TIMEOUT` — downstream read timeout (default `30`,
  strict range 0.1–120 seconds).
- `DNSMAN_ACME_HUB_RETRIES` — identical idempotent downstream retries (default
  `1`, accepted values `0` or `1`). The client retries only connect/read
  ambiguity and HTTP 502/503/504; it never retries redirects or
  400/401/403/409/429. There is no downstream zone-name setting. See
  [ACME federation](../dnsman/AcmeFederation.md#downstream-challenge-client).
- `DNSMAN_ALLOWED_RECORD_TYPES`
- `DNSMAN_CERT_RENEW_DAYS`
- `DNSMAN_CERT_RETRY_BASE_SECONDS` — retry base after a failed renewal whose
  existing material is still valid (default `3600`; clamped to 60–86400
  seconds and exponentially bounded at 86400).
- `DNSMAN_CERT_ISSUING_STALE_SECONDS` — static grace period before an abandoned
  `issuing` certificate is requeued (default `1800`; clamped to 60–86400
  seconds). A live advisory lock still prevents concurrent reclamation.
- `DNSMAN_CERT_SYNC_CHANNEL`
- `DNSMAN_DNS_PROPAGATION_TIMEOUT`
- `DNSMAN_MAX_DOMAIN_PRICE`
- `DNSMAN_PURCHASE_ENABLED` — global kill switch for any real-money registrar
  call (default `False`). Defaults and meanings for the whole family:
  [dnsman README settings table](../dnsman/README.md#settings).
- `DNSMAN_QUOTE_TTL_MINUTES`
- `DNSMAN_REGISTRANT_CONTACT` — **group-scopable and DB-backed.** A `Setting`
  row per group overrides the global row, which overrides the conf file; stored
  `is_secret` so it never reaches a REST graph. Edited through
  `/api/dnsman/registrant`, not here:
  [the registrant contact](../dnsman/Registrar.md#the-registrant-contact).
- `DNSMAN_SEARCH_BATCH_LIMIT`

### DOCIT_KB

- `DOCIT_KB_MAX_DISTANCE` — cosine-distance relevance ceiling for the
  knowledge-base vector search leg (default unset — no floor, an unbounded
  kNN). Details:
  [docit knowledge base → Relevance floor](../docit/knowledge.md#relevance-floor).
- `DOCIT_KB_RECONCILE_ENABLED` — kill switch for the knowledge-base
  reconciliation cron dispatcher (default `True`). Details:
  [docit knowledge base](../docit/knowledge.md#settings).
- `DOCIT_KB_RECONCILE_LIMIT` — maximum pages one reconciliation sweep may queue
- `DOCIT_KB_RECONCILE_LOOKBACK_HOURS` — how far back the sweep's stale arm looks

### DUID

- `DUID_HEADER`

### EDGE

Paths, the privileged argv and the whole `EDGE_DEPLOY_*` family are
**file-only** deliberately: `settings.get` resolves a DB-backed `Setting` row
first, and `Setting` is REST-writable by any holder of a global
`manage_settings` grant — a containment boundary, or a root subprocess argv,
that a `Setting` row can move is not one. Full tables with the surrounding
reasoning: [edge README](../edge/README.md#settings),
[web apps and releases](../edge/webapps.md#settings),
[fleet code deploy](../edge/deploy.md#settings).

- `EDGE_ROOT` — **file-only** (`settings.get_static`), default
  `/opt/api/var/edge`. Root of the generation tree (`generations/`, `current`,
  `installed.json`) and of the certificate material the installer writes.
- `EDGE_WWW_BASE` — **file-only** (`settings.get_static`), default `/opt/www`.
  The containment boundary every served file root must resolve under.
- `EDGE_SOCKET_BASE` — **file-only** (`settings.get_static`), default
  `/run/mojo`. The containment boundary every `kind=unix` upstream socket path
  must resolve under.
- `EDGE_LOG_DIR` — **file-only** (`settings.get_static`), default
  `<EDGE_ROOT>/log`. App-owned log directory for the rendered base; it must
  stay writable by the app user or the unprivileged staged `nginx -t` cannot
  open it.
- `EDGE_HTTP_ENABLED` — **file-only** (`settings.get_static`), bool, default
  `True`. Set false on DNSMAN/DNS-01-only fleets to render HTTPS-only vhosts.
  This controls nginx generation, not load-balancer listeners or firewall
  ingress. False also means `EDGE_ACME_WEBROOT` is not read.
- `EDGE_ACME_WEBROOT` — **file-only** (`settings.get_static`), default
  `/var/www/certbot`. Filesystem root the optional per-name port-80 blocks
  serve the HTTP-01 challenge path from when `EDGE_HTTP_ENABLED=True`.
- `EDGE_WEBAPP_CNAME_TARGET` — **file-only** (`settings.get_static`), optional
  override for guided WebApp onboarding. Public FQDN used as the complete
  non-apex CNAME value the browser cannot supply. Unset by default: the
  destination then derives from the DB-backed `BASE_URL` (its hostname), so an
  ordinary installation needs no destination configuration once `BASE_URL` is
  set. Set this only for a split serving topology, where the tier serving web
  apps is not the tier the platform's own hostname fronts. See
  [webapp_destination.resolve()](../edge/webapps.md#url-first-entry-and-external-domains).
- `EDGE_MIME_TYPES` — **file-only** (`settings.get_static`), default
  `/etc/nginx/mime.types`. The mime include in the rendered http base.
- `EDGE_HTTP_DEFAULT_SERVER` — **file-only** (`settings.get_static`), bool,
  default `False`. Flag-gates the rendered catch-all servers — it changes which
  server answers every unmatched name on the node, so it is a cutover step
  rather than a tuning knob. See [templates.md](../edge/templates.md).
- `EDGE_TLS_PROTOCOLS` — **DB-backed** (`settings.get`), default
  `TLSv1.2 TLSv1.3`. The TLS floor in the rendered http base, re-asserted
  against the render-time whitelist; the resolved value is part of the hashed
  desired state, so a change converges the fleet on the next sweep.
- `EDGE_POOLS` — **DB-backed** (`settings.get`, `kind="list"`), default
  `["default"]`. Both the pools the convergence sweep broadcasts to and the
  allowlist `Vhost.pool` is validated against; an empty list falls back to
  `["default"]` rather than declaring none.
- `EDGE_NODE_ID` — **file-only** (`settings.get_static`), optional. Overrides
  the normalized system hostname used by default as the stable 1–63 character
  node identity returned by safe fleet proof. Use it when platform hostnames
  are ephemeral or duplicated. The effective identity must match one node in
  protected `EDGE_EXPECTED_TOPOLOGY`.
- `EDGE_CONVERGE_ENABLED` — **file-only** (`settings.get_static`), bool,
  default `True`. `False` stops the ten-minute convergence sweep entirely —
  nothing is published to the `edge` channel — for deployments that install this
  app only for the fleet-deploy plane.
- `EDGE_COMMAND_TIMEOUT` — **file-only** (`settings.get_static`), int seconds,
  default `60`. Ceiling on each `nginx -t` / reload subprocess the installer
  runs.
- `EDGE_KEEP_GENERATIONS` — **file-only** (`settings.get_static`), int, default
  `5`. Generations retained on disk, i.e. the nginx-config rollback depth; the
  live `current` generation is never pruned.
- `EDGE_KEEP_RELEASES` — **file-only** (`settings.get_static`), int, default
  `5`. Release directories retained per vhost, i.e. the web-app rollback depth;
  the promoted release and anything a retained generation still symlinks are
  exempt, so even `0` cannot delete what is being served.
- `EDGE_RELEASE_BUCKETS` — **DB-backed** (`settings.get`, `kind="list"`),
  default `[]`. The S3 buckets a web app may use; **fails closed** — with none
  declared no web app can be registered, no upload URL signed, and no node
  fetch performed, since the API signs uploads with the platform's own AWS
  credentials.
- `EDGE_RELEASE_MAX_FILES` — **DB-backed** (`settings.get`), int, default
  `5000`. Cap on manifest entries, checked before a single presigned URL is
  minted.
- `EDGE_RELEASE_MAX_BYTES` — **DB-backed** (`settings.get`), int bytes, default
  `1073741824` (1 GiB). Cap on the manifest's total declared size, checked
  alongside the file cap. A count cap does not bound bytes, and every node in
  the pool fetches a promoted release onto its own disk — so this is what stops
  one oversized build (a bundle that accidentally ships `node_modules`) filling
  the fleet.
- `EDGE_RELEASE_UPLOAD_TTL` — **DB-backed** (`settings.get`), int seconds,
  default `3600`. Lifetime of each presigned release PUT.
- `EDGE_RELEASE_FETCH_TIMEOUT` — **file-only** (`settings.get_static`), int
  seconds, default `60`. Per-attempt connect/read timeout on a node's S3
  release GET.
- `EDGE_RELEASE_FETCH_BUDGET` — **file-only** (`settings.get_static`), int
  seconds, default `300`. Wall-clock ceiling for one release's fetch; the
  remainder is left for the next converge, which resumes by hash.
- `EDGE_DEPLOY_SCRIPT` — **file-only** (`settings.get_static`, `kind="list"`).
  Its default is the permanent packaged locator:

  ```python
  ["sudo", "-n", "bash", "-c",
   'exec bash "$(python3 -m mojo.deploy locate update.sh)" "$@"',
   "django-mojo-update"]
  ```

  This settings-free endpoint needs no project script. Override the complete
  argv when the project already points at a small shim, e.g.
  `["sudo", "-n", "/opt/api/aws/update.sh"]`; a shim that uses the same
  locator remains supported and receives framework fixes automatically.
- `EDGE_DEPLOY_NODE_TYPE` — **file-only** (`settings.get_static`), default
  `api`. `api` uses the built-in migration/nginx/API lifecycle; reserved `code`
  performs only checkout and dependency installation; another valid lowercase
  name selects `aws/deploy/<type>.sh`. Values are 1–32 lowercase letters,
  digits, dashes or underscores and must begin with a letter. API nodes consume
  the `edge` job channel; non-API nodes consume `platform-deploy`.
- `EDGE_DEPLOY_BRANCH` — **file-only** (`settings.get_static`), default `main`.
  Only pushes to `refs/heads/<this>` start a deploy.
- `EDGE_DEPLOY_CANARY_TIMEOUT` — **file-only** (`settings.get_static`), int
  seconds, default `600`. How long the orchestrator waits for the canary node,
  and the expiry on deploy jobs; keep it below `EDGE_DEPLOY_STATUS_TTL`.
- `EDGE_DEPLOY_STATUS_TTL` — **file-only** (`settings.get_static`), int
  seconds, default `900`. Expiry on the Redis deploy target/status keys — the
  backstop that stops a canary dying hard from wedging every future deploy.
- `EDGE_PYPI_VERSION_TTL` — **file-only** (`settings.get_static`), int seconds,
  default `21600` (6 hours). How long a successful PyPI lookup for the newest
  published `django-mojo` is cached by `edge.services.framework_version`. The
  Admin dashboard reads it on every page load, so this is what keeps the page
  off the network; a daily cron warms it.
- `EDGE_PYPI_VERSION_ERROR_TTL` — **file-only** (`settings.get_static`), int
  seconds, default `900`. The shorter TTL used when the lookup failed, so an
  outage is retried in minutes rather than hours. Both are floored at `60`.

### EMAIL

- `EMAIL_CHANGE_CODE_TTL`
- `EMAIL_CHANGE_TOKEN_TTL`
- `EMAIL_TASK_CHANNEL`
- `EMAIL_VERIFY_CODE_TTL`
- `EMAIL_VERIFY_TOKEN_TTL`

### EMBEDDINGS

- `EMBEDDINGS_DIM` — must match the `PageChunk` vector column (1024); changing it requires a migration + re-embed
- `EMBEDDINGS_PROVIDER` — `bedrock` (default) or `mock`

### EVENTS

- `EVENTS_ON_ERRORS`

### FILEMAN

- `FILEMAN_EXPORT_EXPIRES_DAYS` — days until assistant `export_data` files
  expire and are deleted by the cleanup job (default `14`). See
  [assistant settings](../assistant/README.md#settings).
- `FILEMAN_SVG_MAX_BYTES` — first of five independent caps bounding SVG-to-PNG
  rasterization for renditions (default `2097152`, 2 MB). Defaults and the
  bomb each one stops:
  [SVG rasterization](../fileman/renditions.md#svg-rasterization).
- `FILEMAN_SVG_MAX_EMBEDDED_PIXELS` — default `40000000` (40 Mpx)
- `FILEMAN_SVG_MEMORY_MB` — default `512`; Linux-only backstop
- `FILEMAN_SVG_RASTER_BOX` — default `1024`, applied to both width and height
- `FILEMAN_SVG_TIMEOUT` — default `15` seconds
- `FILEMAN_USE_SHORTLINKS` — global default for wrapping File/FileRendition
  download URLs in `/s/<code>` short links (default `True`). See
  [fileman shortlinks](../fileman/shortlinks.md).

### FORCED

- `FORCED_PASSWORD_TOKEN_TTL` — DB-backed integer seconds, default `600`.
  Lifetime of the single-use `tp:` credential returned after a temporary-
  password login. See [User administrator temporary passwords](../account/user.md#administrator-temporary-passwords).

### FRESH

- `FRESH_AUTH_ENFORCE` — bool, default `True`. The master switch for step-up
  ("fresh auth") re-authentication. Set it false and no endpoint asks for a
  password again, including the ones that hard-code their own window.

  It exists because `FRESH_AUTH_WINDOW` is not the off switch it looks like: an
  endpoint's explicit `requires_fresh_auth(600)` takes precedence over the
  setting, and around twenty endpoints pass one — deploy-key minting, API-key
  rotation, capacity changes, email admin, domain purchase. With
  `FRESH_AUTH_WINDOW` at its `0` default those endpoints still re-prompted
  every ten minutes and nothing in configuration could stop it.

  **Turning it off is a real reduction in security**, not a UX preference: it
  removes the re-auth prompt from every sensitive mutation in the product. Do
  it only where the session is already strongly protected (short-lived tokens,
  SSO with its own step-up, a trusted admin network).
- `FRESH_AUTH_WINDOW` — int seconds, default `0` (off). The global freshness
  window for endpoints that do **not** name their own. An endpoint that passes
  an explicit `seconds` ignores this; only `FRESH_AUTH_ENFORCE` overrides those.

### GEOFENCE

- `GEOFENCE_ALLOW_PRIVATE_IPS`
- `GEOFENCE_ALLOWLIST`
- `GEOFENCE_CACHE_TTL`
- `GEOFENCE_ENABLED`
- `GEOFENCE_FAIL_CLOSED`
- `GEOFENCE_FAIL_CLOSED_SCOPES`
- `GEOFENCE_STRICT_POSTURE`
- `GEOFENCE_SYSTEM_RULES`
- `GEOFENCE_TEST_OVERRIDE`

### GEOIP

- `GEOIP_ADDITIONAL_PROVIDERS`
- `GEOIP_API_KEY_IP-API`
- `GEOIP_API_KEY_IPINFO`
- `GEOIP_API_KEY_IPSTACK`
- `GEOIP_ENABLE_CLOUD_DETECTION`
- `GEOIP_ENABLE_TOR_DETECTION`
- `GEOIP_ENABLE_VPN_DETECTION`
- `GEOIP_FALLBACK_PROVIDER`
- `GEOIP_PRIMARY_PROVIDER`

### GEOLOCATION

- `GEOLOCATION_ALLOW_SUBNET_LOOKUP`
- `GEOLOCATION_CACHE_DURATION_DAYS`
- `GEOLOCATION_DEVICE_LOCATION_AGE`
- `GEOLOCATION_ENABLE_BLOCKLIST_CHECK`
- `GEOLOCATION_ENABLE_INTERNAL_THREAT_CHECK`
- `GEOLOCATION_INTERNAL_ABUSER_CATEGORY_PREFIX`
- `GEOLOCATION_INTERNAL_ABUSER_EVENT_THRESHOLD`
- `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_CATEGORIES`
- `GEOLOCATION_INTERNAL_ATTACKER_CONFIRMED_THRESHOLD`
- `GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD`
- `GEOLOCATION_INTERNAL_ATTACKER_MIN_TARGETS`
- `GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_CATEGORIES`
- `GEOLOCATION_INTERNAL_ATTACKER_SUSPECT_THRESHOLD`
- `GEOLOCATION_INTERNAL_SHARED_EGRESS_MIN_DEVICES`
- `GEOLOCATION_INTERNAL_THREAT_DRY_RUN`
- `GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS`
- `GEOLOCATION_INTERNAL_THREAT_WINDOW_HOURS`
- `GEOLOCATION_RECHECK_THREATS_MAX`
- `GEOLOCATION_INTERNAL_ATTACKER_EXCLUDED_CATEGORIES` *(deprecated — replaced by
  the CONFIRMED/SUSPECT allowlists; still honored, logs a warning)*
- `GEOLOCATION_INTERNAL_THREAT_EVENT_THRESHOLD` *(deprecated — no predicate
  reads it)*

Every `GEOLOCATION_INTERNAL_*` name above is read through `settings.get()` on
each call, so a DB-backed `Setting` row retunes threat detection without a
restart. See
[account/geoip.md](../account/geoip.md#threat-intelligence).

### GITHUB

- `GITHUB_APP_ID`
- `GITHUB_APP_PRIVATE_KEY`
- `GITHUB_CLIENT_ID`
- `GITHUB_CLIENT_SECRET`
- `GITHUB_SCOPES`
- `GITHUB_WEBHOOK_SECRET`

### GOOGLE

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`
- `GOOGLE_MAPS_API_KEY`
- `GOOGLE_SCOPES`
- `GOOGLE_SERVICE_ACCOUNT_FILE`

### GROUP

- `GROUP_LAST_ACTIVITY_FREQ`
- `GROUP_TOKEN_TTL` — **file-only** (`settings.get_static`). Int, default
  `3600`. Lifetime in seconds of a
  [group-scoped token](../account/auth.md#group-scoped-tokens). There is no
  refresh path — expiry means re-mint, and it is the one refusal that reports a
  distinct message (`"Group token expired"`) so a client can re-mint instead of
  prompting a full re-auth. Clock-skew tolerance for a future `iat` is a fixed
  60s module constant, not a setting. It is also the `expires_in` a
  [gated handoff exchange](../account/auth.md#gated-destinations--deliver-a-group-token-instead-of-a-jwt)
  reports; `user.org.metadata["access_token_expiry"]` is deliberately **not**
  consulted, because that knob tunes JWT lifetimes only.

### INCIDENT

- `INCIDENT_EVENT_METRICS`
- `INCIDENT_EVENT_PRUNE_DAYS`
- `INCIDENT_LEVEL_THRESHOLD`
- `INCIDENT_METRICS_MIN_GRANULARITY`

### INFO

- `INFO_KEY`

### INFRASTRUCTURE

- `INFRASTRUCTURE_MODE` — **file-only** (`settings.get_static`). One of
  `"managed"` (default; unset and `""` mean this) or `"external"`. Says whether
  this installation's AWS estate is the portal's to mutate. In `external` mode
  the two mutating controls — `POST /api/aws/maintenance/apply` and
  `POST /api/account/admin/platform/framework/update` — answer 403
  `infrastructure_external` at both the REST and service layers, and the
  framework overview reports `blocked_reason: "infrastructure_external"`. Reads
  are unaffected. An unrecognized value, a non-string, or a settings read that
  raises is treated as `external` and logged — a typo in a switch whose job is
  to refuse must not disable the refusal. File-only because a DB/Redis-backed
  `Setting` row is writable through the generic settings REST plane, and a
  remotely-writable mode would let settings-write access silently re-arm every
  mutation this switch exists to disable. **An `external` installation should
  also set `EDGE_FRAMEWORK_VERSION`** to `hold` or an explicit version: deploy
  retry is deliberately not gated, and with an unset pin a retry installs the
  newest published django-mojo. See
  [Infrastructure mode](../aws/infrastructure_mode.md).

### INSTALLED

- `INSTALLED_APPS`

### INVITE

- `INVITE_TOKEN_TTL`

### IPVERIFY

- `IPVERIFY_API_KEY`
- `IPVERIFY_HOST`

### JOBS

- `JOBS_ALLOWED_CHANNELS` — the deployment's declared user channels (set
  identically on every box). Unset (default) = monitor mode: an undeclared
  publish still routes and files a `jobs:undeclared_channel` incident.
  Setting it (any list, even `[]`) turns enforcement on: `publish()` refuses
  a channel outside `DEFAULT_CHANNELS` ∪ `JOBS_CHANNELS` ∪ this list ∪
  `*-engine` with `ValueError` plus a `jobs:rejected_channel` incident. See
  [Jobs — Channels](../jobs/settings.md#channels).
- `JOBS_CHANNELS`
- `JOBS_DAEMON_WORKDIR`
- `JOBS_DEBUG`
- `JOBS_DEFAULT_BACKOFF_BASE`
- `JOBS_DEFAULT_BACKOFF_MAX`
- `JOBS_DEFAULT_CHANNEL`
- `JOBS_DEFAULT_EXPIRES_SEC`
- `JOBS_DEFAULT_MAX_RETRIES`
- `JOBS_ENGINE_CLAIM_BATCH`
- `JOBS_ENGINE_CLAIM_BUFFER`
- `JOBS_ENGINE_LOGFILE`
- `JOBS_ENGINE_MAX_WORKERS`
- `JOBS_ENGINE_READ_TIMEOUT`
- `JOBS_HOSTNAME_CHANNEL` — when `True` (default), each engine also consumes
  its box-direct channel, named after its runner id (default
  `<hostname>-engine`), so a publisher can address one specific engine with
  no configuration. See [Jobs — Channels](../jobs/settings.md#channels).
- `JOBS_IDLE_TIMEOUT_MS`
- `JOBS_LOCAL_QUEUE_MAXSIZE`
- `JOBS_PAYLOAD_MAX_BYTES`
- `JOBS_REDIS_PREFIX`
- `JOBS_REDIS_URL`
- `JOBS_RUNNER_HEARTBEAT_SEC`
- `JOBS_SCHEDULER_LOCK_TTL_MS`
- `JOBS_SCHEDULER_LOGFILE`
- `JOBS_STREAM_MAXLEN`
- `JOBS_VISIBILITY_TIMEOUT_MS`
- `JOBS_WEBHOOK_DEFAULT_TIMEOUT`
- `JOBS_WEBHOOK_MAX_RETRIES`
- `JOBS_WEBHOOK_MAX_TIMEOUT`
- `JOBS_WEBHOOK_USER_AGENT` — outbound webhook `User-Agent` (default `"Django-MOJO-Webhook/1.0"`); override to avoid advertising the framework.
- `JOBS_XPENDING_IDLE_MS`

### JWT

- `JWT_ALGORITHM`
- `JWT_REFRESH_TOKEN_EXPIRY`
- `JWT_TOKEN_EXPIRY`

### KMS

- `KMS_KEY_ID`

### LOG

- `LOG_PUSH_MESSAGES`

### LOGIT

- `LOGIT_ALWAYS_LOG_PREFIX`
- `LOGIT_DB_ALL`
- `LOGIT_DEBUG_ALL`
- `LOGIT_FILE_ALL`
- `LOGIT_MAX_RESPONSE_SIZE`
- `LOGIT_NO_LOG_PREFIX`
- `LOGIT_PRUNE_DAYS`
- `LOGIT_REQUEST_BODY`
- `LOGIT_RETURN_REAL_ERROR` — default `True`. When `False`, an unhandled 500 returns the generic body `{"error": "system error", ...}` instead of the exception text. **File-only** (`settings.get_static`), so a `Setting` row cannot re-enable leakage on a deployment that turned it off. Honored by both 500 handlers — the logging middleware and the REST dispatcher (`mojo/decorators/http.py`). Deliberate 4xx messages (`ValueException`, permission denials) are *not* affected; they remain client feedback.

### MAESTRO

- `MAESTRO_API_KEY` — deployment-wide Maestro workspace reporting ApiKey. Required to report Incidents/Tickets; static/secret and never read from a database `Setting` row.
- `MAESTRO_API_URL` — Maestro API origin (default `https://maestromojo.com`). Static; production values must be HTTPS with a public host.
- `MAESTRO_CALLBACK_BASE` — public base URL for the fixed signed Maestro webhook receiver; falls back to static `BASE_URL` when unset (see [Maestro Workspace Reporting](../security/maestro_board.md)).
- `MAESTRO_LINK_TIMEOUT` — outbound Maestro reporting timeout in seconds (default `10`).
- `MAESTRO_ALLOW_HTTP` — dev-only: allow HTTP/local Maestro origins (default off).

### MAGIC

- `MAGIC_LOGIN_TOKEN_TTL`

### MAXMIND

- `MAXMIND_ACCOUNT_ID`
- `MAXMIND_LICENSE_KEY`

### MEDIA

- `MEDIA_HOST`
- `MEDIA_URL`

### MEMBER

- `MEMBER_PERMS_PROTECTION` — dict, default `{}` (read with `kind="dict"`, so a
  DB-backed `Setting` JSON string is honored). Maps a member-assignable
  permission key → the permission(s) the granter must themselves hold to assign
  it, gating `GroupMember.set_permissions` / the group-invite path. Empty by
  default (any group admin holding `manage_group`/`manage_members`/`manage_users`/
  `manage_groups` may assign arbitrary member keys). Example — require a *global*
  `manage_jobs` to grant a member `manage_jobs`:
  `{"manage_jobs": "sys.manage_jobs"}` (the `sys.` prefix escalates the
  requirement to the granter's global permission). Unlike
  `APIKEY_PERMS_PROTECTION` this has **no framework floor** — it really is empty
  until you populate it. Populating it is safe: `set_permissions` skips any key
  whose value would not change the stored state, so a protected permission the
  admin never touched (the admin UI submits the whole switch catalog on every
  save) does not deny the write. Granting and revoking a protected key both
  still require the stated authority. Use it to stop tenant admins
  from minting high-privilege member grants.

### METRICS

- `METRICS_DEFAULT_MAX_GRANULARITY`
- `METRICS_DEFAULT_MIN_GRANULARITY`
- `METRICS_FANOUT_MAX_CHILDREN`
- `METRICS_TIMEZONE`
- `METRICS_TRACK_USER_ACTIVITY`

### MFA

- `MFA_TOKEN_TTL`

### MISC

- `DEBUG`
- `VERSION`

### MOJO

- `MOJO_APP_STATUS_200_ON_ERROR`
- `MOJO_CUSTOM_SERIALIZERS`
- `MOJO_DEFAULT_SERIALIZER`
- `MOJO_REST_LIST_PERM_DENY`
- `MOJO_SERIALIZER_CACHE`
- `MOJO_TEMP_DIR`

### NOTIFICATION

- `NOTIFICATION_DEFAULT_EXPIRY`

### OAUTH

Social-login (client-side) keys:

- `OAUTH_ALLOW_REGISTRATION`
- `OAUTH_REDIRECT_URI`
- `OAUTH_STATE_TTL`

Authorization-server keys — see
[account/oauth_server.md](../account/oauth_server.md). All five are read with
`get_static`, i.e. the **deployment file only**: a `manage_settings` holder must
not be able to lengthen a credential lifetime through a database `Setting` row.
Changing any of them needs a restart.

- `OAUTH_SERVER_PATH` — string, default `api/account/oauth`. Path root of the
  authorization server's endpoints. The same constant derives the issuer, the
  route registrations and the request-logging labels, so they cannot disagree.
- `OAUTH_ACCESS_TTL` — int seconds, default `3600`. Access-token lifetime.
- `OAUTH_REFRESH_TTL_DAYS` — int days, default `30`. Absolute ceiling on a
  grant, measured from consent and never slid by a refresh.
- `OAUTH_REFRESH_GRACE_SECONDS` — int seconds, default `30`. How long a rotated
  refresh token is forgiven as a lost response before reuse counts as replay
  and revokes the grant family.
- `OAUTH_CODE_TTL` — int seconds, default `300`. Authorization-code lifetime.

The authorization server is inert until `BASE_URL` is set **and** at least one
registered resource is enabled; until then every endpoint answers 404.

### OPENAPI

- `OPENAPI_DOCS_KEY`

### PASSKEYS

- `PASSKEYS_RP_NAME`

### PASSWORD

- `PASSWORD_RESET_CODE_TTL`
- `PASSWORD_RESET_TOKEN_TTL`

### PHONE

- `PHONE_CHANGE_TOKEN_TTL`
- `PHONE_VERIFY_CODE_TTL`

### PUBLIC_MESSAGE

- `PUBLIC_MESSAGE_NOTIFY_SUBJECT`
- `PUBLIC_MESSAGE_NOTIFY_TEMPLATE`

### REDIS

- `REDIS_CONNECT_TIMEOUT`
- `REDIS_DB_INDEX`
- `REDIS_MAX_CONN`
- `REDIS_PASSWORD`
- `REDIS_PORT`
- `REDIS_READ_FROM_REPLICAS`
- `REDIS_READER_URL` — **file-only** full URL for the opt-in standalone reader
  client. With `get_connection(reader=True)`, it takes precedence over all
  other reader parts and creates a separate pooled client; in cluster mode it
  is ignored and the primary cluster client is returned.
- `REDIS_READER_SERVER` — **file-only** standalone reader hostname. Used only
  when `REDIS_READER_URL` is unset; configuring either activates the separate
  reader client for `get_connection(reader=True)` outside cluster mode. With
  neither configured, that call returns the primary client.
- `REDIS_READER_PORT` — **file-only** reader port; defaults to the primary's
  effective port when a standalone reader is configured.
- `REDIS_READER_DB_INDEX` — **file-only** reader database index; defaults to
  the primary's effective database index.
- `REDIS_READER_USERNAME` — **file-only** reader ACL username; defaults to the
  primary's effective username.
- `REDIS_READER_PASSWORD` — **file-only** reader ACL password; defaults to the
  primary's effective password.
- `REDIS_READER_SCHEME` — **file-only** reader `redis`/`rediss` scheme; defaults
  to the primary's effective scheme (and is forced to `redis` for a host
  containing `localhost`).
- `REDIS_SCHEME`
- `REDIS_SERVER`
- `REDIS_SOCKET_TIMEOUT`
- `REDIS_URL`
- `REDIS_USERNAME`

### REQUIRE

- `REQUIRE_VERIFIED_EMAIL`
- `REQUIRE_VERIFIED_PHONE`

### REQUIRES

- `REQUIRES_PERMS_IS_GROUP` — bool, default `True` (read once at import). When
  `True`, `@md.requires_perms` falls back to the caller's **group/member**
  permission (`request.group.user_has_permission(...)`) if their global grant is
  missing — correct for endpoints scoped to `request.group`. Setting it `False`
  globally disables that fallback for **every** `requires_perms` endpoint,
  which breaks legitimate group-scoped admin flows; to disable the fallback for
  a single platform-global endpoint, decorate it with
  `@md.requires_global_perms(...)` instead (see
  [permissions.md](../core/permissions.md#global-vs-group-scoped-permission-checks)).

### ROUTE53

- `ROUTE53_PRICE_CACHE_HOURS` — per-TLD registrar price cache in the route53
  helper (default `24`; `<= 0` disables). The quote/money path always
  bypasses it. See [Registrar.md](../dnsman/Registrar.md#the-price-cache).

### SECRET

- `SECRET_KEY` — the deployment signing/wrapping root. Beyond Django's own
  uses, mojo derives from it directly: bouncer token signing, bouncer pass
  cookies, and filevault's per-file key wrapping. **File-only** everywhere in
  mojo — filevault reads it through `mojo.helpers.crypto.keys.secret_keys()`
  (`settings.get_static`), so a `Setting` row named `SECRET_KEY` can never
  re-key file wrapping at runtime.
- `SECRET_KEY_FALLBACKS` — Django's own rotation list (default `[]`), honored
  by mojo's crypto too: bouncer token/cookie **verification** and filevault
  **unwrap**/token-validation accept material produced under any listed key,
  while signing and wrapping always use the primary `SECRET_KEY`. **File-only**
  (`settings.get_static`) — a DB-settable fallback list would be a
  runtime-injectable key-acceptance list. See
  [crypto.md](crypto.md#secret_key-rotation-secret_key_fallbacks) for the
  rotation procedure and what it cannot cover (stored `hash.hash()` digests).

### SERIALIZE

- `SERIALIZE_DATETIME_TO_FLOAT`

### SHORTLINK

- `SHORTLINK_BASE_URL`
- `SHORTLINK_HOME_URL`
- `SHORTLINK_SITE_NAME`

### SMS

- `SMS_FAKE_MAPPINGS`
- `SMS_INBOUND_HANDLER`
- `SMS_OTP_TTL`

### SNS

- `SNS_CERT_TTL_SECONDS` — **file-only** (`settings.get_static`, `kind="int"`),
  default `3600`. Process-local SNS signing-certificate cache lifetime in
  seconds.
- `SNS_VALIDATION_BYPASS_DEBUG` — **file-only** (`settings.get_static`,
  `kind="bool"`), default `False`. Development-only SES receiver bypass; never
  applies to CloudWatch alarms.

### THREAT

- `THREAT_INTEL_ABUSEIPDB_API_KEY`
- `THREAT_INTEL_ABUSEIPDB_ENABLED`
- `THREAT_INTEL_BLOCKLIST_DE_ENABLED`
- `THREAT_INTEL_SPAMHAUS_ENABLED`

### TOR

- `TOR_EXIT_NODE_LIST_URL`

### TOTP

- `TOTP_ISSUER`
- `TOTP_RECOVERY_BCRYPT_ROUNDS` — **file-only** (`settings.get_static`), default
  `12`. bcrypt cost factor for hashing TOTP recovery codes
  (`UserTOTP.generate_recovery_codes`). Floored at bcrypt's own minimum of `4`;
  an unparsable value falls back to `12`. Production should leave this at the
  default — it exists so a generated test project can trade hashing strength
  for speed (test projects set it to `4`).

### TRAFFIC

- `TRAFFIC_CONCENTRATION_RPM` — sustained requests/minute by one identity
  that trigger a concentration alert (default `120`). See
  [Authenticated-Abuse Hardening](../security/abuse_hardening.md#settings).
- `TRAFFIC_CONCENTRATION_SUSTAIN_WINDOWS`
- `TRAFFIC_CONCENTRATION_SHARE`
- `TRAFFIC_CONCENTRATION_MIN_TOTAL`

### TWILIO

- `TWILIO_ACCOUNT_SID`
- `TWILIO_AUTH_TOKEN`
- `TWILIO_NUMBER`

### USE

- `USE_TZ`

### USER

- `USER_LAST_ACTIVITY_FREQ`
- `USER_PERMS_PROTECTION`

### USPS

- `USPS_CLIENT_ID`
- `USPS_CLIENT_SECRET`
- `USPS_USE_TEST_ENVIRONMENT`

### WEBAPP

- `WEBAPP_AUTH_PATH`
- `WEBAPP_BASE_URL` — dynamic public origin. Admin Settings can manage one
  canonical public-HTTPS global override; existing group-scoped rows remain
  available through the generic Setting API and group inheritance.

### WEBHOOK

- `WEBHOOK_SIGNATURE_HEADER` — header name used for the outbound webhook HMAC signature and honored as the default by inbound `verify_signed_request` (default `"X-Mojo-Signature"`). Override to avoid advertising the framework; renaming is a contract change with your webhook consumers, who must read the same name. See [Webhook Signing](../account/webhook_signing.md).

### WS

- `WS_DATABASE_WORKERS` — **file-only**, startup-time size of each ASGI
  process's dedicated realtime database executor (default `4`, clamped to
  `1..32`). Changing it requires a process restart. This bounds concurrent ORM
  work from sockets; it does not change a psycopg pool's `max_size`.
- `WS_CONNECT_RATE_LIMIT` — per-IP websocket connect rate, checked before
  accept (default `30`/min, `<= 0` disables). See
  [Authenticated-Abuse Hardening](../security/abuse_hardening.md#4-websocket-connection-limits).
- `WS_MAX_CONNECTIONS` — concurrent sockets per authenticated identity
  (default `10`, `<= 0` disables).
- `WS_UNAUTH_TIMEOUT` — seconds an unauthenticated socket may live before
  being closed (default `10`).

## Notes

- Source: static scan of `settings.get(...)`, `getattr(settings, ...)`, and `settings.KEY` across `mojo/` (excluding migrations).
- Keys here are framework-known knobs only; project-specific settings may exist in your Django project and are not listed unless referenced by framework code.
