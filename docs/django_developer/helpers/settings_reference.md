# Framework Settings Reference

This reference lists framework-recognized setting keys (names only, no values).

## Startup/Bootstrap Keys (Restart Required)

These are read while URL/module bootstrap happens, so changes require a process restart.

- `MOJO_API_MODULE`
- `MOJO_APPEND_SLASH`
- `MOJO_PREFIX`
- `REST_AUTO_PREFIX`

## Runtime Keys

These are read through `mojo.helpers.settings.settings` during normal runtime.

### ALLOW

- `ALLOW_EMAIL_CHANGE`
- `ALLOW_PHONE_CHANGE`
- `ALLOW_PHONE_LOGIN`
- `ALLOW_SELF_DEACTIVATION`
- `ALLOW_USER_REGISTRATION`
- `ALLOW_USERNAME_CHANGE`

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

- `APIKEY_PERMS_PROTECTION` — dict, default `{}` (read with `kind="dict"`, so a
  DB-backed `Setting` JSON string is honored). Maps a permission key → the
  permission(s) the granter must hold to assign it to an `ApiKey`, gating
  `ApiKey.set_permissions` on REST write. Empty by default (any group admin with
  a key-management perm may assign any non-`sys.` key). Mirrors
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
- `DNSMAN_ALLOWED_RECORD_TYPES`
- `DNSMAN_CERT_RENEW_DAYS`
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

### FRESH

- `FRESH_AUTH_WINDOW`

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
- `GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD`
- `GEOLOCATION_INTERNAL_THREAT_EVENT_THRESHOLD`
- `GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS`

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

- `MAESTRO_CALLBACK_BASE` — public base URL for the maestro board webhook receiver; falls back to `BASE_URL` when unset (see [Maestro Board Link](../security/maestro_board.md))
- `MAESTRO_LINK_TIMEOUT` — outbound HTTP timeout in seconds for maestro link API calls (default `10`)
- `MAESTRO_ALLOW_HTTP` — dev-only: allow `http://` board-link pastes and local/private hosts (default off; production pastes must be https to a public hostname)

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
  requirement to the granter's global permission). Use it to stop tenant admins
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

- `OAUTH_ALLOW_REGISTRATION`
- `OAUTH_REDIRECT_URI`
- `OAUTH_STATE_TTL`

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

- `SNS_CERT_TTL_SECONDS`
- `SNS_VALIDATION_BYPASS_DEBUG`

### THREAT

- `THREAT_INTEL_ABUSEIPDB_API_KEY`
- `THREAT_INTEL_ABUSEIPDB_ENABLED`
- `THREAT_INTEL_BLOCKLIST_DE_ENABLED`
- `THREAT_INTEL_SPAMHAUS_ENABLED`

### TOR

- `TOR_EXIT_NODE_LIST_URL`

### TOTP

- `TOTP_ISSUER`

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
- `WEBAPP_BASE_URL`

### WEBHOOK

- `WEBHOOK_SIGNATURE_HEADER` — header name used for the outbound webhook HMAC signature and honored as the default by inbound `verify_signed_request` (default `"X-Mojo-Signature"`). Override to avoid advertising the framework; renaming is a contract change with your webhook consumers, who must read the same name. See [Webhook Signing](../account/webhook_signing.md).

### WS

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
