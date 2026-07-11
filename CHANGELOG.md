## v1.2.45 - July 10, 2026

**bug/security** — **JSONField `__replace` (and non-dict overwrites) can no longer bypass `PROTECTED_JSON_PERMS`.**
`on_rest_update_jsonfield` ran the protected-JSON guard and its
`meta:protected_changed` audit only on the merge branch, so a payload like
`{"metadata": {"__replace": true, ...}}` (or a `JSON_REPLACE_FIELDS` field, or a
non-dict value posted over a dict) rewrote the field wholesale — including
`metadata.protected.*` — with no permission check and no audit. Concretely: a
group-confined ApiKey holding only `manage_group` could rewrite
`Group.metadata.protected` (e.g. payment `allowed_origins`). The guard now runs
on every write path and in both directions — an incoming `"protected"` key, or a
replace/overwrite that would clobber an existing `protected` subtree, requires
`PROTECTED_JSON_PERMS` (superuser fallback unchanged) and is always audited; on
a replace the audit's changed-keys include removed keys. Also fixed: the guard
read `user.is_superuser` directly, which ApiKey callers don't define, so even
the previously-guarded merge path returned a 500 instead of a clean 403 for
keys. (ITEM-030)

**bug** — **`POST /api/group/member/invite` now fail-closes with a clean 403 instead of a raw HTTP 500 for unauthenticated (and unresolved-group) callers.**
The handler was gated only by `@md.custom_security` (a no-op marker) plus an
in-view group-permission check, so an anonymous request reached
`request.group.user_has_permission(request.user, …)` with `request.user` the
`ANONYMOUS_USER` sentinel — whose `has_permission` was a **zero-argument** lambda,
so calling it with a perm raised `TypeError`, surfaced as a 500 leaking the
interpreter message. The endpoint now carries `@md.requires_auth()` (anonymous →
clean 403 before the body runs) and guards a `None` `request.group` (an inactive
or nonexistent `group` id → generic 403, no inactive-vs-nonexistent oracle; was an
`AttributeError` 500). Separately, the `ANONYMOUS_USER.has_permission` sentinel now
tolerates arguments (`lambda *a, **kw: False`), fail-closing the same latent arity
crash at the other ungated permission-check sites (`chat` room admin/moderator
checks, `member.can_change_permission`) — no grant changes, since it always returns
False. No membership was ever created (the crash preceded `invite()`), so this is a
robustness / API-hygiene fix; matters for API consumers (e.g. maestro workspaces)
that expect a clean rejection. (ITEM-028)

**feature** — **GitHub OAuth is now a hosted-page login/registration method.**
`github` joins `LOGIN_METHODS` / `REGISTRATION_METHODS` in
`services/auth_config.py`, so the bouncer-hosted `/auth` and `/register` pages
render a "GitHub" button with the same wiring as Google/Apple (generic
`begin → callback → complete` flow; `MojoAuth.startGitHubLogin()` added to
`mojo-auth.js`). The provider itself (`GITHUB_CLIENT_ID` /
`GITHUB_CLIENT_SECRET` / `GITHUB_SCOPES`) already existed — this exposes it on
the hosted pages and makes it group-toggleable.

Two visible effects on upgrade: (a) groups **without** an explicit
`login.methods` / `registration.methods` list get the GitHub button by default
(all method tokens are default-on) — disable it with an explicit methods list,
and configure the GitHub credentials before relying on it; (b) github OAuth
begin and new-user signup are now **gated by group auth config** like
google/apple — a request carrying a `group_uuid` whose config excludes github
gets 403 (requests with no group context remain ungated). Also fixed the
auth-pages doc to use the real provider setting names (`GOOGLE_CLIENT_ID`,
`APPLE_CLIENT_ID`, … — the previous `GOOGLE_OAUTH_*`/`APPLE_OAUTH_*` names were
never read by the code). (ITEM-026)

**security** — **Group REST writes now require `SAVE_PERMS`, not mere membership — closing an any-member write to top-level Group fields.**
`POST`/`PUT`/`DELETE` on `/api/group/<pk>` (and Group's `POST_SAVE_ACTIONS`) was
effectively gated by the *view* check. `MojoModel._evaluate_permission`
classified every operation as a read (`VIEW_PERMS` is present in every caller's
permission keys), so `Group.check_view_permission` — whose fallthrough admits
ANY active member with a basic-graph downgrade — authorized writes too. A plain
member with zero permissions could rename the group, change
`kind`/`auth_domain`/`metadata`, and reach `on_action_realtime_message`
(publishing to `group:<id>:*` realtime topics).

The permission layer now classifies an operation by its keys: a **write** carries
a write perm-key (`CREATE`/`SAVE`/`DELETE_PERMS`) and **skips** the instance's
view hook, gating on `check_edit_permission` instead; a **read** still prefers the
view hook. `Group` gains a `check_edit_permission` that requires an actual
SAVE_PERMS grant — global (`manage_groups`/`groups`) **or** member-level
(`manage_group`), with an ApiKey confined to its own group tree **and** holding
the perm. Reads are unchanged: a plain member still `GET`s their group with the
basic-graph downgrade. The FK **attach-by-pk** path (`{"group": <id>}` on rows a
member creates) is a read check and keeps working; the FK **dict-save** path
(`{"group": {...}}`, which rewrites Group fields inline) tightens with the rest.

**Behavior change**: a plain member's Group rename/edit now returns 403 (needs
`manage_group` at the member level or a global grant); Group's member-reachable
`realtime_message` save-action likewise now needs `manage_group`. `User` (whose
`check_edit_permission` also gates VIEW-by-pk) and all hookless models are
unaffected. Regression coverage in `tests/test_account/test_group_save_perms.py`.

**security** — **`GROUP_FIELD` now scopes detail and list permissions, not just the `?group=` filter — closing cross-tenant reads on group-scoped models without a direct `group` FK.**
A model can declare `RestMeta.GROUP_FIELD` (optionally a related path, e.g.
`"original_file__group"` or `"agent__project"`) to name its owning-group FK.
Previously only `on_rest_list` (the `?group=` narrower) consulted it; the two
permission seams — detail `_evaluate_permission` and the `on_rest_handle_list`
member fallback — hard-gated on `hasattr(cls, "group")`. So a model scoped only
through `GROUP_FIELD` (no direct `group` attribute, e.g. `FileRendition`) fell
through to a **flat, tenantless** `user.has_permission` check: a holder of the
model's VIEW perm at the **user** level could read (and with SAVE, write) every
tenant's rows on the bare list and on detail-by-pk, while a legitimate group
**member** (perm at the GroupMember level) was denied even their own rows.

Now both seams treat a model as group-scoped when it has a direct `group` FK
**or** a `GROUP_FIELD`:
- **Detail** — `_evaluate_permission` resolves the instance's owning group by
  traversing `GROUP_FIELD` (new `_resolve_group_from_instance`, null-safe over
  related paths) and checks membership against the **row's** tenant, not a
  caller-supplied `?group=`.
- **List** — the member fallback filters `{GROUP_FIELD__in: <caller's groups>}`,
  so a member sees only their tenants' rows and a bare list never leaks foreign
  rows. System-level (user-level / superuser) grants still see all — unchanged.
- **Create** — the create-time group auto-assign generalizes from the hardcoded
  `group` field to a direct-FK `GROUP_FIELD` (body still wins; related-path
  `GROUP_FIELD` has no local field to stamp and is derived from the FK the body
  provides).

The FK-attach VIEW check on assign-by-pk (`on_rest_save_related_field`) inherits
this, so attaching a foreign-tenant FK (e.g. `{"agent": <other tenant's id>}`)
is now denied — closing the cross-tenant write/move path too. `ApiKey`
confinement is unaffected: a group-scoped key reaches only its own group's rows
(truly groupless models still deny keys via `ALLOW_API_KEY_GLOBAL`).
**Behavior change**: `FileRendition` (`GROUP_FIELD="original_file__group"`) moves
from over-denying members / leaking to user-level holders → correctly
member-scoped; a group-scoped ApiKey now reaches its own group's renditions
(scoped, not cross-tenant). Regression coverage in
`tests/test_fileman/9_test_rendition_group_field.py`.

**security** — **A deactivated group can no longer become `request.group` by numeric id.**
The dispatcher's numeric `group=<id>` resolution now filters `is_active=True`
(via the new `Group.get_active(pk)` classmethod), matching the `group_uuid`
branch's long-documented contract: an inactive group's id resolves exactly
like a nonexistent one — `request.group` stays `None`, the group is not
`touch()`ed (previously a full save bumped `last_activity` AND `modified`:
an anonymous existence oracle), its geofence rules no longer participate in
decisions, and evidence metrics can't be attributed to it. The
`requires_perms`/`requires_group_perms` group fallback uses the same
resolver, so a member-level grant in a deactivated group no longer
authorizes **through those decorators** (fail-closed 403). Admin lifecycle
flows are unaffected — disable/reactivate go through `/api/group/<pk>` with
global perms, and `geo/rules`/`geo/simulate`/`geo/check` keep their own
deliberate inactive-group handling. Two adjacent pre-existing gaps are NOT
closed here and are tracked as follow-up items: RestMeta **list** endpoints
resolve member grants via `User.get_groups_with_permission`, which does not
yet filter `Group.is_active`; and an API key whose own group is deactivated
keeps working unless the request explicitly names that group
(`ApiKey.validate_token` sets the key's group as request context without an
`is_active` check — deactivate the key itself to cut access). (ITEM-025)

**api** — **Sending the same key in both the query string and the JSON body no longer 500s.**
`request.DATA` previously merged a key that arrived from two sources into a
mixed list (`?group=518` + body `{"group": 518}` → `['518', 518]`), which
crashed the dispatcher's group resolution (`int()` on a list raises
`TypeError`, uncaught, *before* the REST error wrapper) into a bare Django
500 HTML page with no traceback in mojo's logs — a real-traffic pattern for
web clients that append an active-group query param to JSON form posts.
Duplicate keys now resolve deterministically: the later source wins, whole —
**query string < form body < JSON body** (documented in both doc tracks).
Multi-value keys within one source (`?tag=a&tag=b`, `tags[]=`, JSON lists)
still produce lists. A genuinely unusable `group` value (list/dict/etc.) now
returns mojo's JSON 400 `Invalid group ID` from the dispatcher, and the
`requires_perms`/`requires_group_perms` group fallback treats it as
no-group-context (fail-closed 403) instead of a coercion crash. (ITEM-024)

**security** — **Geofence-adjacent Settings can no longer be written as garbage; `kind=` coercion is loud and fail-safe.**
Write-time validation now covers every geofence-consumed Setting (previously
only rules/allowlist/strict): `GEOFENCE_ENABLED`, `GEOFENCE_FAIL_CLOSED`,
`GEOFENCE_ALLOW_PRIVATE_IPS` (JSON boolean), `GEOFENCE_CACHE_TTL`
(non-negative integer), `GEOFENCE_FAIL_CLOSED_SCOPES` (list of non-empty
strings) — rejected with a readable 400 via `POST /api/settings` AND raised on
`Setting.set()`/shell writes (validation now also runs in `save()`, closing
the programmatic back door). Registered keys also refuse `is_secret` rows —
the secret flag previously skipped validation entirely AND masked the value
(`******`) from other admins. The per-key mechanism is a public registry —
`Setting.register_validator(key, func, global_only=True)` — so downstream
apps can protect their own enforcement-bearing keys (e.g. mverify's
`PAYMENTS_GEOFENCE_RULES`). Posture-key writes now also invalidate the
geofence decision cache (an `ALLOW_PRIVATE_IPS` flip previously left stale
cached `private_ip` allows for up to `GEOFENCE_CACHE_TTL`). On the read side,
`settings.get(kind=...)` no longer silently absorbs garbage: a
present-but-uncoercible value returns the DECLARED default and logs a
`settings` warning; an unrecognized bool string no longer truthy-coerces to
`True` (allow-flavored flags previously failed open); a bracket-wrapped
unparsable list no longer comma-splits into nonsense entries. (ITEM-023)

**security** — **Member-readable geofence policy + events (group-scoped).**
New `GET /api/geo/policy`: a brand's own admin holding a group-member
`view_security`/`security` grant (or any global holder) can read the
effective geofence policy for **their own group** — baseline + group rule +
strict posture — through a deliberately narrow payload that never includes
the config plane's operational detail (enforced endpoints, allowlist
internals, cache TTL, fail-closed scopes, config provenance). The same
member grant already scopes `GET /api/incident/event` to the member's own
group via the framework group fallback, giving them their group-attributed
`geofence_block`/`geofence_exempt` history (attribution caveat documented;
`geofence_config` stays platform-only) — now locked by regression tests
(`tests/test_geofence/member_visibility.py`). The config plane (`geo/rules`
etc.) remains global-only and unchanged. (ITEM-022)

**api** — **Info endpoints open to any authenticated user (no `INFO_KEY` needed).**
`GET /api/versions`, `GET /api/sysinfo/detailed`, and
`GET /api/sysinfo/network/tcp/summary` now accept any authenticated session
without the `key` parameter; unauthenticated callers still require
`key=<INFO_KEY>` (default `MOJO`) exactly as before.

**security** — **Opt-in strict/compliance geofence posture + cached threat lists.**
New `GEOFENCE_STRICT_POSTURE` setting (default `False` — existing fail-open
deployments are untouched): when on, the engine fails **closed** on geo-lookup
failure (ORs with `GEOFENCE_FAIL_CLOSED` and the per-scope map), **denies**
private/reserved IPs, and **denies** when geofencing has no rules configured
(new reason `no_rules_strict` — no silent allow-all). Settable per-group via
the tri-state `Group.metadata["geofence_strict"]` override (null = inherit;
validated on REST write), so some tenants can run strict while others stay
permissive. The IP allowlist still exempts under strict (with
`would_block_reason` evidence), blocks under strict report as level-5
compliance events, and posture flips invalidate cached decisions
automatically. Changing the per-group override requires the **global**
`manage_geofence`/`security` permission (a tenant admin who can merely edit
the group cannot opt out of a platform-mandated posture — 403) and every
flip is recorded as a `geofence_config` incident event. `GET /api/geo/rules`
now surfaces `posture.strict_posture`
plus the group override and its resolved value. Separately, the Tor exit
list and blocklist.de are now cached in two **cache-only** incident `IPSet`
rows (`tor_exits`, `blocklist_de`; created `is_enabled=False`, the REST
`enable` action rejects them, and `sync()` hard no-ops for them — they can
never reach the kernel firewall) refreshed 6-hourly by a new `refresh_threat_lists` cron;
`detect_tor` / `check_blocklist_de` read the cache and fall back to the old
live fetch until it warms, removing the per-lookup download of the full
lists from the geolocation path. (ITEM-021)

**metrics** — **Geofence evidence metrics are now recorded per-tenant (`account=group-<id>`) alongside global.**
When a geofenced request carries a group (`group`/`group_uuid` param — e.g.
white-label auth pages), the base evidence slugs `geofence:blocks` and
`geofence:exempt` are dual-written to that group's metrics account
(`group-<id>`, the platform's per-tenant convention) in addition to `global`,
so consumer portals can chart per-tenant geofence activity through the
existing `GET /api/metrics/fetch` group-account permission gate
(`view_metrics`/`metrics`, member grant suffices). No-group requests record
exactly as before, and the per-country/per-region breakdown slugs stay
global-only — a per-group geographic cross-product would explode the Redis
key space (monthly/yearly counter keys never expire). (ITEM-020)

## v1.2.44 - July 08, 2026

**security** — **Dependency floors raised to patched versions (fleet-wide vulnerability remediation).**
Downstream deployments upgrade via `pip install django-mojo --upgrade`, which
resolves against these declared constraints (not the dev `uv.lock`). Several
runtime dependencies had known CVEs whose fixes were installable but not
*forced* — `pip --upgrade` leaves an already-satisfied dependency in place — and
`cryptography` was actively capped **below** its fix. Lower bounds are now raised
to the patched versions so the standard upgrade command pulls them: `pyjwt`
`>=2.13.0` (HS256/JWK confusion enabling forged tokens — CVE-2026-48526, plus
algorithm-allowlist bypass / DoS), `cryptography` `>=48.0.1,<49.0.0` (bundled
OpenSSL, ceiling was `<47.0.0`), `django` `>=5.2.14` (**drops Django 4.2 support**
— sensitive-info-in-cookies/cache + length-parameter handling; CVE-2026-5766 /
-35192 / -6907), `ujson` `>=5.13.0`, `mistune` `>=3.2.1` (ReDoS + several XSS).
Three runtime-transitive deps (pulled via requests / boto3 / pyfcm / twilio /
httpx) are now explicit floor pins so the upgrade command controls them too:
`urllib3>=2.7.0` (sensitive headers forwarded across origins in proxied
redirects — relevant to signed-webhook and apikey-federation calls),
`aiohttp>=3.14.1`, `idna>=3.15`. No code changes; resolves to django 5.2.16,
cryptography 48.0.1, pyjwt 2.13.0, urllib3 2.7.0, ujson 5.13.0, mistune 3.3.2,
aiohttp 3.14.1, idna 3.18. Note: one mistune math-plugin XSS
(CVE-2026-44708) has no upstream fix — do not enable the math plugin on
untrusted Markdown.

## v1.2.43 - July 08, 2026

**security** — **A group API key can no longer reach platform-global models (cross-tenant data-exposure fix).**
An `ApiKey` is a group-scoped credential, but two gaps let a low-trust group
admin escalate: (1) a key's `permissions` had no assignment gate, so an admin
could self-mint a key with any permission (e.g. `manage_users`); and (2) the
model-security layer (`MojoModel._evaluate_permission`) trusted a key's
self-claimed permission on any `uses_model_security` model that has **no `group`
foreign key** — there is no group to confine the key to and the list query
cannot group-filter, so the key read every tenant's rows. Verified end-to-end: a
group-A key self-claiming `manage_users` read another tenant's user record via
`GET /api/user`. Fixes: `_evaluate_permission` now **denies an ApiKey by
default** on a groupless model (opt back in per-model with
`RestMeta.ALLOW_API_KEY_GLOBAL = True`, default `False`; none do). Models that
override the permission check are handled too: `User.check_edit_permission` (the
choke point for `GET /api/user/<pk>`, since `User` has no `check_view_permission`)
denies keys, and `Group`'s custom view path confines a key to its own group +
descendants **and** requires it to actually hold the permission (so a
zero-permission key cannot write its own group's `auth_config`/`geofence`). And
`ApiKey.set_permissions` / `can_change_permission` (mirroring `GroupMember`) gate
assignment via a new `APIKEY_PERMS_PROTECTION` setting (dict, default `{}`). The exposed set included
`User`, `GeoLocatedIP`, `UserLoginEvent`, `Job`/`JobEvent`/`JobLog`,
`ScheduledTask`, bouncer admin models, and `FileRendition`. Legitimate key flows
are unaffected — group-scoped models (settings, webhook subscriptions, chat,
files) still work confined to the key's group, and the geoip federation-sync
receiver (a dedicated `allow_api_keys` endpoint) is untouched. Also hardened: the
assistant memory tier check no longer 500s on an ApiKey identity.
**Deployment note:** machine access to platform-global data (e.g. user
administration) must use a dedicated `allow_api_keys` endpoint or a
service-account User with a real global grant — not a group API key. See
`docs/django_developer/account/api_keys.md`.

**security** — **Global-effect admin endpoints no longer honor group-scoped permission grants (cross-tenant privilege-escalation fix).**
`@md.requires_perms(...)` falls back to a caller's group/member permission for
the group named in the request — correct for group-scoped endpoints, but a
cross-tenant escalation for endpoints whose effect is platform-wide, because
`GroupMember` permissions accept arbitrary key names that any group admin can
assign. A tenant admin could grant a teammate e.g. `manage_jobs` scoped to their
own group and then, by passing that group id, drive global job/fleet control,
AWS infrastructure, metrics ACLs, geofence config, incident health, or
cross-tenant user administration. A new framework decorator
`@md.requires_global_perms(...)` authorizes on the caller's **global**
`User.permissions` (or superuser) only — never the group fallback — and is now
applied to ~50 platform-wide endpoints across the jobs, aws, account, metrics,
incident, assistant, and geofence (ITEM-017 config plane) surfaces. This also
closes a sibling vector on the AWS email CRUD + device-location endpoints
(`EmailDomain`/`Mailbox`/`EmailTemplate`/`IncomingEmail`/`SentMessage`/
`UserDeviceLocation` — all groupless models): a self-minted group **ApiKey**
with a self-claimed `manage_aws`/`manage_users` permission could read/write
them cross-tenant through the model-security layer; the global gate now rejects
the key before the handler runs. The same
permission names are unchanged; only *where* the grant must live changes, so
deployments that granted admins these perms globally (the intended usage) are
unaffected — only member/group-scoped grants stop authorizing global actions.
A group-scoped **ApiKey** is likewise rejected by the global gate (machine
access to a global endpoint needs a service-account user); the lone
`allow_api_keys=True` exception is the geoip federation-sync receiver, whose
intended caller is a fleet-peer key. Genuinely group-scoped endpoints (group/
member management, per-group webhook secret, ApiKey-federated SMS send) keep the
group fallback. Also fixed in the same pass: `GET /api/models/permissions`
(passed a list literal to `requires_perms`, 500-ing before authorizing); the
group-invite path bypassed `MEMBER_PERMS_PROTECTION` (now routed through
`set_permissions`); and `MEMBER_PERMS_PROTECTION` is read with `kind="dict"` so
a DB-backed policy map is honored. Deployment note: any integration that relied
on a member-scoped grant or an API key to reach these global endpoints must move
to a global user grant / service account. See
`docs/django_developer/core/permissions.md` (Global vs Group-Scoped Permission
Checks).

## v1.2.42 - July 08, 2026

**feature** — **Geofence config plane: editable system rules, IP allowlist, simulate, and exemption audit.**
`GEOFENCE_SYSTEM_RULES` is now manageable at runtime as a DB-backed `Setting` row via
perm-gated REST — `GET/POST/DELETE /api/geo/rules` (full-replace writes validated by
the DSL validator with readable 400s; the DB row wins over the settings file; the
engine now reads the setting with `kind="dict"` so the stored JSON parses). New
fine-grained perms `view_geofence` / `manage_geofence` (domain category `security`)
let legal/business staff maintain jurisdiction rules without `manage_settings`;
the config-plane endpoints check **global** user grants only (no group-permission
fallback — a GroupMember-scoped grant must never authorize platform-wide
enforcement config), the geofence setting keys are global-only (group-scoped rows
rejected 400), and `bypass_holders` returns id/username without email/display_name
(users-category PII stays behind users-category perms).
Group rules (`Group.metadata.geofence`) are validated at REST save time (400 instead
of a request-time `rule_invalid` deny). Every rule/allowlist change invalidates the
decision cache automatically (Setting/Group/GeoLocatedIP model hooks — REST,
programmatic, and shell writes all covered) and is recorded as a `geofence_config`
incident event carrying old/new/actor — that stream is the admin UI's change history.
`POST /api/geo/simulate` returns an uncached what-if `GeoDecision` for an arbitrary
IP or geo dict (works while geofencing is disabled; never emits evidence).
`GET /api/geo/rules` also reports posture, allowlist summary, and every enforced
endpoint+scope. See `docs/django_developer/account/geofence.md` and
`docs/web_developer/account/geofence.md`.

**feature** — **Geofence IP allowlist — full exemption for developer/office IPs, with expiry.**
Two sources, checked after `bypass_geofence` and before rules: a `GEOFENCE_ALLOWLIST`
setting of CIDR entries (strings or `{cidr, reason, until}`; managed via
`GET/POST /api/geo/allowlist`) and the existing per-IP `GeoLocatedIP` whitelist. The
whitelist gains `whitelisted_until` (mirrors `blocked_until`; the
`/api/system/geoip` whitelist action now accepts `{reason, ttl, until}`) and a
`whitelist_active` property honored everywhere — an **expired whitelist no longer
suppresses incident-driven firewall blocking** (permanent whitelists unchanged), and
`whitelisted_until` never federates. Allowlisted requests pass jurisdiction AND
abuse rules with reason `ip_allowlisted`, but still shadow-evaluate so evidence can
record `would_block`. Exemptions are auditable: `GET /api/geo/allowlist` (expired
entries listed `active:false`) and `GET /api/geo/bypass_holders` (explicit grants +
superusers).

**feature** — **Geofence evidence plane: every block becomes an incident event, with metrics.**
Blocks now call `incident.report_event(category="geofence_block", request=…)` from
the enforcement decorator (cached denials included): level 3 ordinary jurisdiction
block, 5 abuse-flag or fail-closed-scope block, 6 lookup-failure-while-fail-open,
7 invalid-rule-at-evaluation (crosses the default `INCIDENT_LEVEL_THRESHOLD` and
pages). Exercised allowlist exemptions emit `geofence_exempt` (level 3). Events are
deduped per `(ip, reason)` hourly so a blocked client can't flood the stream;
metrics count every block including deduped ones (`geofence:blocks`,
`geofence:blocks:country:{cc}`, `geofence:blocks:region:{iso}`, `geofence:exempt`).

**feature** — **Per-scope geofence fail posture.**
`@md.requires_geofence(scope=…)` now passes its scope to the engine: scopes listed
in the new `GEOFENCE_FAIL_CLOSED_SCOPES` setting (e.g. `["payments"]`) fail
**closed** on geo-lookup failure while auth stays fail-open. Default `[]` — zero
behavior change until configured. `lookup_failed` decisions are no longer cached
(scope isn't part of the cache key, so a fail-open allow must not be replayed to a
fail-closed scope).

## v1.2.41 - July 06, 2026

**feature** — **Outbound webhook signature header and User-Agent are now configurable.**
The signature header name — hardcoded as `X-Mojo-Signature` — is now overridable via
the `WEBHOOK_SIGNATURE_HEADER` Django setting, and the outbound `User-Agent`
(`Django-MOJO-Webhook/1.0`) via `JOBS_WEBHOOK_USER_AGENT` (a setting that was already
documented but never read — now honored). Operators who don't want to advertise the
framework to third-party webhook receivers can rename both. Defaults are unchanged, so
existing deployments and consumers are unaffected. Inbound `verify_signed_request` reads
the same `WEBHOOK_SIGNATURE_HEADER` default, so send and verify stay in sync; the new
`sign.get_signature_header()` accessor is the effective-value source (the
`WEBHOOK_SIGNATURE_HEADER` constant remains as the default, for back-compat). A
caller-supplied `User-Agent` in `publish_webhook(headers=...)` still wins over the
setting. Log masking follows the configured header name. Renaming the signature header
is a contract change with your webhook consumers — they must read the same name. See
`docs/django_developer/account/webhook_signing.md`.

**fix** — **Group membership/permission helpers no longer crash on an API-key identity.**
`Group.get_member_for_user` now returns `None` for a non-`User` identity (e.g. an
`ApiKey` authenticating via `Authorization: apikey <token>`) instead of running
`members.filter(user=<ApiKey>)`, which Django rejected with `Must be "User"
instance.`. This fixes an HTTP 400 on API-key-authenticated
`GET /api/group/uuid/<uuid>`, `GET /api/group/<pk>`, and
`GET /api/group/<pk>/member`, and on any direct
`group.get_member_for_user(api_key, …)` call. Group permission checks now cleanly
grant/deny a group-scoped key via `ApiKey.has_permission` (`sys.*` still denied).
The guard lives at the single choke point, so every present and future caller is
covered. See `docs/django_developer/account/api_keys.md`.

**feature** — **testit honors `var/dev_server.conf` as a local host/port override.**
The test server's host/port is read from `config/dev_server.conf` (committed). A
gitignored `var/dev_server.conf`, when present, now overrides it (whole-file
replace — falls back to `config/` when absent), so you can retarget the local test
server without editing the tracked file. All three readers agree on the effective
file: `bin/asgi_local` (binds uvicorn), the testit runner's `--host` default, and
`th.server_settings()`. Resolution is centralized in `paths.resolve_conf()`. See
`docs/django_developer/testit/Overview.md`.

**feature** — **New `create_user` management command for bootstrapping admins.**
Django's built-in `createsuperuser` doesn't work against this project's custom
`account.User` model — it calls the manager with `username=`/`password=` only,
but `CustomUserManager.create_superuser` requires `email`, so it raises a
`TypeError`. `./manage.py create_user` is the supported replacement: creates an
email-based or phone-only user, optionally granting `--staff`/`--superuser`
and/or specific `--permission <key>` grants, with a `--password-env` option to
avoid putting the plaintext password in shell history. See
`docs/django_developer/account/bootstrap.md` for the portal-permission
reference table and why a `{"admin": true}` permission key is *not* an
equivalent of `--superuser` (it only satisfies the frontend's wildcard check,
not the backend's).

## v1.2.40 - July 03, 2026



**fix** — **Auth middleware no longer 500s on a malformed `Authorization` header.**
`AuthenticationMiddleware` unpacked the header with `prefix, token = token.split()`, which
raised `ValueError` (→ unhandled HTTP 500) whenever the value was not exactly two
whitespace-separated parts — a scheme-less single token, an empty string, or 3+ parts. Any
client (or a payment webhook such as Coinflow, which sends `Authorization: <key>` with no
scheme) could trigger a 500 on every request. Malformed headers now pass through as
unauthenticated (`request.bearer` / `request.user` left unset — protected endpoints still
reject, fail-closed). A bare scheme-less token is additionally exposed on `request.auth_token`
(prefix `"raw"`, `request.auth_token.token` = the raw value) so a downstream/public endpoint
can validate it without re-parsing the raw header.

## v1.2.39 - July 01, 2026



**security** — **Client IP is no longer spoofable via `X-Forwarded-For`.**
`request.get_remote_ip()` (which sets `request.ip`) took the *leftmost* `X-Forwarded-For`
entry — a value any client can forge — so the IP behind geofencing, API-key `allowed_ips`,
rate limiting, audit logs, and login-anomaly geo was attacker-controlled. It now reads the
proxy-authoritative `X-Real-IP` (set by the reverse proxy to the real client, overwriting any
client-supplied value), falls back to `REMOTE_ADDR`, and normalizes the result (strips an
`IP:port` suffix, unwraps bracketed IPv6, collapses IPv4-mapped IPv6). **Deployment
requirement:** your reverse proxy must set `X-Real-IP` to the real client IP — the shipped
`asgi.inc` does (`proxy_set_header X-Real-IP $remote_addr;`). `X-Forwarded-For` is no longer
consulted for `request.ip`.

**security** — **WebSocket connections no longer trust a spoofable client IP.** The realtime
WS handler resolved the client IP from `scope["client"]` / `X-Forwarded-For` / RFC 7239
`Forwarded` ahead of `X-Real-IP`, so a WS client could forge the IP recorded in connection logs,
Redis connection records, and security/incident `Event` records (skewing geolocation and
per-country metrics). It now prefers the proxy-authoritative `X-Real-IP` (normalized via the
shared `normalize_ip` helper), with the transport peer as a last-resort fallback;
`X-Forwarded-For` and `Forwarded` are no longer consulted. (`mojo.helpers.request._normalize_ip`
is now the public `normalize_ip`, shared by the HTTP and WS resolvers.)

**fix** — **IP-storage fields now handle IPv6 and a missing IP.** Several columns recording the
client IP assumed IPv4 / a non-null value: `Event.source_ip`, `Incident.source_ip`, `Log.ip`, and
`GeoLocatedIP.subnet` were too short for a full IPv6 address (silently truncating it at the DB),
and `UserLoginEvent.ip_address` / `BouncerSignal.ip_address` were non-nullable (a `None` client IP
silently dropped a login event / could crash pre-auth assessment). The IP CharFields are now 45
chars, those two fields are nullable, and `GeoLocatedIP`'s subnet computation is IPv6-safe (the
`/64` network instead of the dot-based prefix). Surfaced by the X-Real-IP resolver fixes above,
which now emit normalized IPv6 / None. Includes migrations.

## v1.2.38 - June 29, 2026



**account** — **A failed phone registration no longer burns the verified-phone token.**
If `/api/auth/register` fails *after* the single-use `verified_phone_token` was consumed —
e.g. a per-group `USER_REGISTERED_HANDLER` raises — the token is now restored, so the user
can retry the same token instead of being left un-signed-in with a dead token (previously a
retry returned "Invalid or expired phone verification"). Applies to both the existing-account
login path and new-user registration. Single-use on success and the duplicate-user guard are
unchanged.

## v1.2.37 - June 28, 2026

**account** — **Display-name moderation no longer rejects legitimate names.** A name
that merely *contains* a profanity substring — e.g. **Matsushita**, **Harshita**,
**Scunthorpe** — was hard-blocked at registration ("Invalid display name: contains
inappropriate content") because content_guard matches banned words as naive substrings.
`User.validate_name_fields` now treats a name "block" as **advisory**: the name is
allowed and logged for review instead of rejecting the signup. content_guard's scoring is
unchanged; comment/chat/contact-form moderation is unaffected.

**account** — **Honest, anti-enumeration UX for hosted SMS sign-in.** The hosted
sign-in page no longer dead-ends a phone number that has no account on a "code sent"
screen. SMS sign-in now states up front that a code is sent *only if the number is
already linked to an account* and surfaces a "Create an account" link in the SMS
view. The server is unchanged — `POST /api/auth/sms/login` still returns the same
generic success for known and unknown numbers (no account-existence signal), so
account enumeration remains impossible.

**account** — **Phone-register verify no longer burns the session on a wrong code.**
`phone_register.verify_code()` now reads the Redis session, compares the code, and
deletes the session **only on a successful match** (previously an atomic `getdel`
consumed it on the first attempt, right or wrong). A mistyped code is rejected with
400 but leaves the session intact, so the correct code can be retried on the same
`session_token` until it succeeds or the TTL expires. Brute force stays bounded by
the existing per-IP rate limit (`phone_register_verify`, 10/60s) plus the session TTL.

## v1.2.36 - June 16, 2026

fixing login page


**account** — **API key self-rotation.** New `POST /api/group/apikey/rotate`
rotates the **calling** key's secret in place (same id / name / permissions /
limits, new token); the old token is invalidated immediately and the new one is
returned exactly once. Self-service — requires only API-key auth, no
`manage_group` (same trust model as `group/apikey/me`). Model-level equivalent:
`ApiKey.rotate_token()`. Enables scheduled credential rotation without minting a
second key or leaving the old secret live.

## v1.2.35 - June 09, 2026

reg enter key fix


**account** — Optional **step-up ("recent authentication") gate** for sensitive
operations (off by default). Logins now stamp an `auth_time` claim into the JWT
(unconditional; carried forward unchanged across token refresh). When
`FRESH_AUTH_WINDOW` (seconds, default `0` = disabled) is set, sensitive endpoints
and `User` actions require the caller's token to have authenticated within that
window, else they return **HTTP 440** `{error: "reauth_required", code: 440}` — a
distinct state from `403` (no permission) and `401` (token invalid/expired) so
clients can drive a step-up re-auth (reuse any existing login/verify flow; there
is no separate step-up endpoint). New `@md.requires_fresh_auth()` decorator,
`mojo.apps.account.services.fresh_auth` helper, and `ReauthRequiredException`.
Applies to admins acting on other users (the admin's own token must be fresh) and
to passwordless accounts. **Breaking-ish:** `POST /api/auth/username/change` and
`POST /api/auth/sessions/revoke` no longer require `current_password` (it locked
out passwordless passkey/SMS accounts); ownership is the authenticated session,
freshness is the new gate. See `docs/django_developer/account/step_up_auth.md` and
`docs/web_developer/account/step_up_auth.md`.

**account** — Fixed the bouncer-hosted stepped (phone-first) **register page** so
pressing **Enter** on the phone (step 1) or 6-digit code (step 2) field now runs
that step's action (Continue / Verify) instead of firing the final step-3 submit.
The hidden step-3 "Create account" button is the form's default submit button, so
Enter previously ran final registration — whose first check is the Terms checkbox
— surfacing a confusing "Please agree to the Terms & Conditions." error on a step
where that checkbox isn't even shown. Single-pane (email) registration is
unaffected.

## v1.2.34 - June 06, 2026



## v1.2.33 - June 06, 2026



**account** — Registration forms can now collect per-group **extra (non-canonical) fields** (promo / referral / tracking codes). Declare them in the group's `auth_config.registration.extra_fields` (a list of `{name, label?, required?}`, default empty — other tenants are unaffected). On the bouncer-hosted register page each declared field is captured silently from a matching URL query param (`/register?promo=WELCOME100`) or asked for as a plain text input when absent, then wired into the register payload. Server-side, `on_register` captures any key allowlisted by the union of the group's declared `extra_fields` names and the global `REGISTRATION_EXTRA_FIELDS` setting, persists them to `user.metadata["registration"]`, and still passes them to `USER_REGISTERED_HANDLER` via the `extra=` kwarg. Defaults preserve current behavior.

## v1.2.32 - May 30, 2026



## v1.2.31 - May 30, 2026



## v1.2.30 - May 30, 2026

critical ip fix


fix whitelist not removing permanent blocks from ipset


**account** — `GeoLocatedIP.whitelist()` now removes permanently blocked IPs from the `mojo_blocked` ipset. Previously it always broadcast `broadcast_unblock_ip` (an iptables-only removal), so whitelisting an IP that had been permanently blocked left it in the ipset and the IP stayed blocked fleet-wide. The method now mirrors `unblock()`: it reads DB truth to detect whether the block was permanent (no `blocked_until`) or TTL-based, and broadcasts `broadcast_ipset_del_blocked` for permanent blocks or `broadcast_unblock_ip` for TTL blocks.

## v1.2.29 - May 25, 2026

new ai caching


enable prompt caching for assistant LLM calls


**assistant** — Anthropic prompt caching is now on by default for every `llm.call()` from the assistant agent loop. The full prefix (system prompt + tool definitions + prior conversation history) is cached automatically; reads cost ~10% of base input tokens, writes cost 25% more. Across a typical multi-turn agent loop the reads dominate and net cost / latency drop substantially. Disable with `LLM_ADMIN_PROMPT_CACHE_ENABLED = False`. Each user-message exchange now records summed token counts on the final assistant `Message.usage` JSONField (`cache_read_input_tokens`, `cache_creation_input_tokens`, `input_tokens`, `output_tokens`) and emits one INFO line per turn to `assistant.log`. Cache invalidates when `load_tools`, skill mutations, or memory mutations change the cacheable prefix — the next turn re-writes and subsequent turns hit again. A one-time WARN to `llm.log` fires if caching is enabled but the prefix is below the model's minimum (1024 tokens for Sonnet, 4096 for Opus). Adds Message migration 0006.

## v1.2.28 - May 21, 2026

allow existing users to register again with auto signup to group


**account** — Registering with a phone that already has an account now signs the user in instead of returning a duplicate error, provided the schema marks the phone field with `verify: "sms"` (the SMS-verified token proves ownership). Profile fields in the request body are ignored for the existing account. Without `verify: "sms"`, an existing phone is still a hard duplicate error. If `group_uuid` is supplied and the account is not yet a member of that group, a `GroupMember` is created and `USER_REGISTERED_HANDLER` fires for the group; if they are already a member it is a pure login with no handler. `POST /api/auth/phone/register/start` no longer rejects already-registered phones. `POST /api/auth/phone/register/verify` now returns `account_exists` (boolean) alongside `verified_phone_token` and `expires_in` — the hosted register form uses it to skip the profile step for returning users.

## v1.2.27 - May 21, 2026

fix for login history permissions


## v1.2.26 - May 21, 2026

fixing autofill sms code


**account** — The hosted login page now leads with the primary credential. For a passwordless config (no `password` in `login.methods`) it opens directly on the SMS phone-entry form instead of burying it behind a footer link; "Sign in with a code" is now a proper button alongside Passkey/Google/Apple.

**account** — SMS verification codes autofill on the hosted login and registration pages. OTP texts now carry an origin-bound `@host #code` line (required by Android Chrome's WebOTP API, used by iOS Security Code AutoFill), and the pages call the WebOTP API to fill — and submit — the code field the moment the SMS arrives.

## v1.2.25 - May 21, 2026

fix for sms proxy


**phonehub** — `POST /api/phonehub/sms/send` no longer 500s when the caller authenticates with an API key. Under API-key auth `request.user` is the `ApiKey` instance, not a `User`; the handler forwarded it into `SMS.send(user=...)`, which assigns the `SMS.user` ForeignKey and raised `ValueError: ... must be a "User" instance`. The handler now attaches a real `User` only — `SMS.user` is left null for API-key callers, who are still identified by `SMS.group`. This unblocks the `mojo` SMS provider relay, which posts to a remote instance with `Authorization: apikey`.

**phonehub** — `POST /api/phonehub/sms/send` now honors a caller-supplied `from_number`. The handler previously read only `to_number`, `body`, and `metadata`, silently dropping the documented `from_number` field so `SMS.send` always fell back to `twilio.get_from_number()`. `from_number` is now passed through to `SMS.send` (both the twilio and `mojo` provider branches), letting a caller that owns multiple sender numbers pick which one to send from.

## v1.2.24 - May 21, 2026

support for sms login on certain platforms


**account** — Passwordless registration. A `registration.fields` schema may now omit `password`. When it does, `POST /api/auth/register` no longer requires a `password` body param; the account is created with `set_unusable_password()` and the user logs in via the SMS-code flow (`POST /api/auth/sms/login` → `POST /api/auth/sms/verify`). A no-password schema must include a `phone` field with `verify: "sms"` — `validate_fields_config` rejects the config at write time, and `on_register` re-checks defensively. The default email + password schema is unchanged.

## v1.2.23 - May 21, 2026

fix in cors for s3


**fileman** — `FileManager` REST create no longer auto-stamps the `user` field with the caller. `FileManager.RestMeta` now sets `CREATED_BY_OWNER_FIELD = None`, so a create request that omits `user` (or sends `user: null`) leaves the owner unset — required for creating group-scoped managers via REST. An explicit `user` in the request body is still honored, and `group` auto-fill from `request.group` is unchanged. To close the privilege gap this opens, `on_rest_pre_save` now rejects REST creation of a *system-scoped* manager (`user` and `group` both unset) with a 403 unless the requester is a superuser; direct ORM creation is unaffected.

**fileman** — `fix_cors` now resolves allowed origins from the manager's own `allowed_origins` field. Previously `_resolve_allowed_origins_from_value_or_settings` only checked the action payload and global Django settings (`CORS_ALLOWED_ORIGINS`, `ALLOWED_ORIGINS`, `FRONTEND_*`), so a manager configured via `set_allowed_origins` would fail `fix_cors` with "No allowed origins provided" while passing `check_cors` (which already consulted that field). The resolver still raises when no origins are configured anywhere — it does not silently fall back to a wildcard.

## v1.2.22 - May 20, 2026

apikey test api


## v1.2.21 - May 20, 2026

improved auth config


**account** — Per-group auth config. All auth-page branding and method control is now driven by a structured `AUTH_CONFIG` setting (deployment-wide) and `group.metadata["auth_config"]` (per-group, deep-merged down the parent chain) instead of the retired flat `AUTH_*` / `AUTH_REGISTER_*` settings. The three-section config (`theme` / `registration` / `login`) is the single source of truth for what the hosted pages render.

- **Retired settings** (remove from `settings.py`): `AUTH_APP_TITLE`, `AUTH_LOGO_URL`, `AUTH_FAVICON_URL`, `AUTH_HERO_IMAGE_URL`, `AUTH_HERO_HEADLINE`, `AUTH_HERO_SUBHEADLINE`, `AUTH_BACK_TO_WEBSITE_URL`, `AUTH_TERMS_URL`, `AUTH_LAYOUT`, `AUTH_API_BASE`, `AUTH_SUCCESS_REDIRECT`, `AUTH_CUSTOM_CSS`, `AUTH_CUSTOM_CSS_URL`, `AUTH_ENABLE_GOOGLE`, `AUTH_ENABLE_APPLE`, `AUTH_ENABLE_PASSKEYS`, `AUTH_REGISTER_FIELDS`, `AUTH_REGISTER_IDENTITY_FIELD`, `AUTH_MIN_AGE_YEARS`. All map to keys in the `AUTH_CONFIG` object — see `docs/django_developer/account/auth_config.md` for the migration table.
- **`login.methods`** (tokens: `password`, `sms`, `passkey`, `magic`, `google`, `apple`) and **`registration.methods`** (`password`, `google`, `apple`) control which buttons are rendered and soft-gate which methods are accepted when a `group_uuid` is supplied. This is a UX guardrail, not a security boundary.
- **`registration.passkey_prompt`** (`off` / `optional` / `required`) drives a new reusable passkey enrollment page at `/{BOUNCER_PASSKEY_PATH}` (default `/passkey`). The hosted register page redirects there after signup when the prompt is enabled.
- **SMS login** — new SMS-code sign-in view on the hosted `/auth` page (requires `sms` in `login.methods`).
- **New public endpoint** `GET /api/auth/config` returns the resolved config for an optional `group_uuid` — for custom front-ends building their own auth UI.
- **`Group.on_rest_pre_save`** validates `metadata["auth_config"]` on write (rejects bad method tokens, invalid enums, `custom_css` with `<` / `@import` / external URLs, `custom_css_url` without `https://`).
- **`mojo-auth.js`** adds `registerPasskey`, `startSmsLogin` / `verifySmsLogin`, and `getAuthConfig` helpers.

## v1.2.20 - May 19, 2026

fixing broken tests


bouncer continuous detection + static-page protection


**account** — Bouncer expanded to continuous in-session detection and static-page protection.

(1) `mojo-sentinel.js` ships at `/account/static/mojo-sentinel.js` — a lightweight (~5KB) telemetry client with no UI, no fingerprinting, no gate logic. Auto-collects visibility transitions, focus/blur, paste events, click coordinate buckets, inter-action timing, page lifetime, idle gaps. Public `MojoSentinel.observe(category, payload)` lets host apps push their own events. Batched flushes (default every 15s or 25 events), final flush on `pagehide` via `navigator.sendBeacon`.

(2) Streaming scorer (`mojo.apps.account.services.bouncer.stream_scoring.score_session(muid)`) runs inline on every batched-event POST. Five universal stream analyzers registered via `@register_stream_analyzer`: `extended_session_no_idle`, `tab_never_hidden`, `coordinate_quantization`, `action_interval_regular`, `paste_into_sensitive_field`. Apps add domain analyzers (game-specific, etc.) via the same registry. Score is a monotonic high-water mark in Redis (`bouncer:session_risk:{muid}`) within a TTL window — no flapping.

(3) Gradient enforcement (`mojo.apps.account.services.bouncer.enforcement.apply_session_response`) maps score → 4 bands: freeze (≥90), shadow_ban (≥70), require_step_up (≥50), monitor (≥30). Each band fires a `security:bouncer:session_*` incident; freeze flips `BouncerDevice.risk_tier='blocked'` and optionally calls a `BOUNCER_SESSION_FREEZE_HANDLER` dotted-path callable; shadow_ban/step_up set `user.set_protected_metadata` flags apps read. Bands configurable via `BOUNCER_SESSION_BANDS` setting. Four new incident `RuleSet` defaults added via the existing `ensure_bouncer_rules` bootstrap.

(4) `mojo-bouncer.js` v2.0.0 ships at `/account/static/mojo-bouncer.js` (+ companion `.css`). The legacy mverify_api version's `/api/bouncer/{assess,submit,event}` endpoints and `apikey` auth header are gone — the modernized client points at `/api/account/bouncer/{assess,event}`, drops the two-stage submit handshake (single-call assess + `@md.requires_bouncer_token` on downstream endpoints), and sends `credentials: 'include'` on every fetch so the HttpOnly mbp cookie sets cross-origin. `data-api-base` is the supported config attribute for cross-origin embedding.

(5) `GET /api/account/bouncer/verify_pass` is the new nginx `auth_request` endpoint. Two-stage gate: (a) consults the existing `BotSignature` Redis cache — known-bot IPs/UAs return 401 with `X-Bouncer-Reason: signature` at the edge, before the cookie is even checked; (b) validates the mbp pass cookie. 200 with `X-Bouncer-Muid` on pass, 401 on miss. `BOUNCER_PASS_COOKIE_DOMAIN` lets the mbp cookie be shared across subdomains under a common parent.

(6) CORS — `mojo/middleware/cors.py` modified to set specific-origin + `Access-Control-Allow-Credentials: true` for any request to a bouncer path whose Origin is in `BOUNCER_ALLOWED_ORIGINS`. All other paths keep the existing `*` behavior (regression-tested). OPTIONS preflights get the credentialed treatment.

(7) `POST /api/account/bouncer/event` accepts a batched `{events: [...]}` payload (`bulk_create`-persisted as N `BouncerSignal(stage='event')` rows) plus calls `score_session(muid)` inline. Legacy `{event_type, data}` single-event payload still works.

(8) nginx drop-in include — `docs/web_developer/account/nginx/mojo-bouncer.conf` ships as a config artifact. Consumer server blocks set two variables (`$mojo_bouncer_host`, `$mojo_bouncer_login`), `include` the file, and add `auth_request /_mojo_bouncer_check;` to any location to gate it through the bouncer. Worked example at `nginx/example-protected-site.conf`. Bouncer-as-a-Service deployment pattern documented — one django-mojo install can serve N consumer apps.

Settings added: `BOUNCER_ALLOWED_ORIGINS` (default `[]`), `BOUNCER_PASS_COOKIE_DOMAIN` (default `''`), `BOUNCER_SESSION_RISK_TTL` (default 86400), `BOUNCER_SESSION_BANDS` (defaults: freeze=90, shadow_ban=70, require_step_up=50, monitor=30), `BOUNCER_SESSION_FREEZE_HANDLER` (default `None`). No migrations.

## v1.2.19 - May 18, 2026

making live registration fix


**account** — fix: switcher links on `/auth` and `/register` dropped post-auth forwarding params. A user landing at `/auth?redirect=/dashboard` who clicked "Create one" was sent to `/register` with no `redirect=`, then landed at `AUTH_SUCCESS_REDIRECT` instead of `/dashboard` after registering. `_auth_context()` now forwards `redirect`, `next`, `returnTo`, and `back` from the source URL alongside `group_uuid`. OAuth `code`/`state`, magic-link `token`, and reset tokens are intentionally not forwarded.

## v1.2.18 - May 17, 2026

new webhook registry


**account** — Group-scoped webhook subscriptions. New `account.WebhookSubscription` model (FK Group, https-only URL, free-form `events` list, `is_active` toggle, `metadata` JSON) stores one row per `(Group, receiver URL)`. New `mojo.apps.account.services.webhooks.dispatch(group, event_type, data, *, idempotency_key=None)` runs in the caller's thread, queues a single fan-out job on the `webhook_fanout` channel, and returns the fan-out job_id. The `handle_fanout` worker queries `WebhookSubscription` rows by `(group, is_active=True, events__contains=[event_type])` (Postgres-native JSON containment) and publishes one signed `publish_webhook(group=...)` per match — signing, retries, and `X-Mojo-Signature` injection come for free from the existing webhook primitive. Per-receiver `idempotency_key` is suffixed with the subscription id so retried fan-outs don't double-deliver. Per-row publish failures and missing-group conditions report to the incident app (categories `webhook:fanout:error` and `webhook:fanout:group_missing`); the fan-out skips bad rows and continues. New REST CRUD at `/api/group/webhook_subscriptions` and `/api/group/webhook_subscriptions/<id>` with the same `manage_group` / `manage_groups` / `groups` permission threshold as ApiKey CRUD and the existing webhook secret endpoint. URL validation runs in `on_rest_pre_save` (https-only, syntactic check). Migration `account.0044_webhooksubscription` — run `makemigrations` + `migrate` in your project after pulling. Channel additions to `JOBS_CHANNELS`: add `"webhook_fanout"` and `"webhooks"` for dedicated workers, or let them fall back to `"default"`. No event-name registry — strings in, strings out; each emitting SaaS documents its own vocabulary.

## v1.2.17 - May 17, 2026

new signing of webhooks


**account / jobs / helpers** — Group-scoped webhook signing primitive. New `Group.get_webhook_secret()` / `get_webhook_secret_info()` / `rotate_webhook_secret()` accessors on top of the existing `MojoSecrets` blob (no migration); default is `auto_create=False` for safe verify paths. New REST endpoint `POST /api/group/webhook_secret` reads or rotates the per-Group HMAC-SHA256 secret (permission: `manage_group` / `manage_groups` / `groups`). New `mojo.helpers.crypto.sign.sign_for_group(group, body_bytes)` and `WEBHOOK_SIGNATURE_HEADER` constant ("X-Mojo-Signature"). New `mojo.helpers.request.verify_signed_request(request, secret)` Django adapter that pulls raw `request.body` + the signature header and verifies in constant time. `jobs.publish_webhook(url, data, group=...)` now auto-signs at delivery: the handler canonicalizes the body (`json.dumps(data, sort_keys=True, separators=(",",":"))`), injects `X-Mojo-Signature`, and sends those exact bytes — the secret never enters the queue payload (only `sign_group_id` does). Retries re-sign with the current secret, so rotations are safe in-flight. If the sign group is deleted between publish and delivery, the job is marked `'failed'` with `error_type='sign_group_missing'` — no silent unsigned send. The unsigned `publish_webhook(...)` path is unchanged. Existing service-specific signature headers should alias `X-Mojo-Signature` for one release cycle, then go away.

## v1.2.16 - May 17, 2026

bugfix for display name generation


**account** — fix: `POST /api/auth/register` was leaving `display_name` NULL and skipping `infer_names_from_email()` because the handler calls Django `user.save()` directly, bypassing the REST framework's `on_rest_pre_save` / `on_rest_created` hooks. The handler now runs `infer_names_from_email()`, backfills `display_name`, and runs the `validate_name_fields()` content guard before save. `User.generate_display_name()` now walks a priority chain — first+last → email local-part → friendly random placeholder ("BraveTiger"-style adjective+animal) → username-derived — so every caller (REST backfill, `full_name` property, OAuth fallbacks) gets a sensible value regardless of which fields are populated. Phone numbers are intentionally NEVER used as the display_name fallback to avoid PII leakage into member lists, search results, and push device names.

## v1.2.15 - May 17, 2026

bump version?


## v1.2.14 - May 17, 2026

group uuid


**account** — new endpoint `GET /api/group/uuid/<uuid>` returns a Group by its uuid using the standard REST detail pipeline. Same permission gating as `GET /api/group/<int:pk>` (RestMeta `VIEW_PERMS`: `view_groups`, `manage_groups`, `manage_group`, `groups`). Returns 404 when the uuid doesn't match an existing group.

**account** — register-page UX redesign. When the schema marks `phone` with `verify: "sms"`, the bouncer-hosted `/register` page now presents as a three-step state machine: Step 1 (phone only) → SMS → Step 2 (6-digit code, with 30s resend cooldown + Back) → Step 3 (rest of profile fields). The previous inline Send-code / Verify subwidget on the phone field is removed; verification gets its own focused screen. DOB is now collected via three auto-advancing numeric segments (MM / DD / YYYY) instead of `<input type="date">` — mobile-friendly numeric keyboard, paste-aware (`MM/DD/YYYY`, `MM-DD-YYYY`, or ISO `YYYY-MM-DD` distributes across segments), no JS library. Wire format unchanged (ISO `yyyy-mm-dd`). When the schema has no SMS-verify phone, the form falls back to a single-pane layout — today's behavior preserved. New optional setting `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` accepts a fixed bypass code at `POST /api/auth/phone/register/verify` so dev environments can exercise the verify flow without an SMS gateway; app startup logs a warning when set. **DO NOT SET IN PROD.**

**account** — configurable bouncer registration form. `AUTH_REGISTER_FIELDS` (group-scoped, ordered list of `{name, required, verify}` dicts) drives both the rendered form and the server-side validator. Canonical fields: `first_name`, `last_name`, `email`, `phone`, `dob`, `password`. Unset → today's email-based form (no behavior change for existing deployments). When `phone` is configured with `verify: "sms"`, the form renders an inline Send-code / Verify flow; the server requires a `verified_phone_token` (minted by two new endpoints `POST /api/auth/phone/register/start` and `POST /api/auth/phone/register/verify`) before creating the User row. Identity field auto-picks email > phone (`AUTH_REGISTER_IDENTITY_FIELD` is an explicit override). Phone-as-identity registers set `User.username = phone_number` and `is_phone_verified=True`. `AUTH_MIN_AGE_YEARS` age-gates DOB when set. Forgot-password (`POST /api/auth/forgot`) now dispatches an SMS code automatically when the matched user has no email on file, or when `channel: "sms"` is passed explicitly; the bouncer's login page forgot-password subview switches to phone-mode when the configured identity is phone. `MojoAuth.startPhoneRegister`, `MojoAuth.verifyPhoneRegister`, and `forgotPasswordCode(identifier, channel)` exposed on the JS helper.

**account** — bouncer and OAuth flows now use `?group_uuid=<uuid>` instead of `?group=<uuid>` for UUID-based group selection. The framework dispatcher reserves `?group=` for integer-ID lookup and rejects non-integer values with `400 Invalid group ID` before any view runs, so the previous bouncer `?group=<uuid>` convention never actually worked end-to-end. `_resolve_group()` now reads `?group_uuid=`, `_auth_context()` emits `?group_uuid=` in cross-links (login ↔ register), `_serve_challenge()` preserves `group_uuid` through the bot-check redirect, and the OAuth callback bounce appends `?group_uuid=` to the frontend URI. The OAuth `on_oauth_begin` no longer silently falls back to `request.GET.get("group")` — callers must pass `group_uuid` explicitly. Consumer apps and frontends that built URLs with `?group=<uuid>` must switch to `?group_uuid=<uuid>`. Hostname-based resolution via `Group.auth_domain` is unchanged.

## v1.2.13 - May 16, 2026

bug fixes for multi-tennant config


**account** — bouncer-hosted register/login forms now forward `group_uuid` from `_matConfig` to the server so multi-tenant deployments can satisfy `REQUIRE_GROUP_ON_REGISTRATION` from the hosted page, and `USER_LOGIN_HANDLER` receives operator context on password logins. `MojoAuth.login(username, password, options)` accepts an optional third `options` argument; `options.group_uuid` is forwarded into the POST body. Two-arg `MojoAuth.login()` callers are unaffected.

## v1.2.12 - May 16, 2026

- new federated ip info support, better geofencing support


**account** — three extension hooks for the register and login flows:

- `PRE_REGISTER_VALIDATOR` — dotted-path callable invoked before user creation; raise `ValueException` to reject with 400. Signature: `(*, email, group, request, extra)`. Plaintext password is intentionally not passed.
- `USER_REGISTERED_HANDLER` — fires inside the register `transaction.atomic()` block and from the OAuth path on new-user create. Raising rolls back the transaction. Signature: `(*, user, request, group, source, extra)`. `source` ∈ `{"password", "oauth"}` in v1.
- `USER_LOGIN_HANDLER` — fires on every successful `jwt_login()` call across all login paths. Errors are caught, logged, and swallowed. Signature: `(*, user, request, source, is_new_user)`.

**account** — `POST /api/auth/register` accepts a new optional `group_uuid` body param. Set `REQUIRE_GROUP_ON_REGISTRATION = True` to make it mandatory. Extra body keys are forwarded to `USER_REGISTERED_HANDLER` via `extra` if listed in `REGISTRATION_EXTRA_FIELDS`; unrecognised keys are silently dropped. `MojoAuth.register()` in mojo-auth.js now forwards the full payload. The register template gains `{% block extra_fields %}` and `{% block pre_submit_script %}` extension points.

## Unreleased

**phonehub** — new `mojo` SMS provider lets one django-mojo instance delegate outbound SMS to another over HTTP. Set `PhoneConfig.provider='mojo'`, `mojo_remote_url`, and an encrypted `mojo_api_key` (via `set_mojo_api_key()`); `SMS.send()` then POSTs to the remote's existing `POST /api/phonehub/sms/send` with `Authorization: apikey <token>`. `SMS.send()` now dispatches by `PhoneConfig.provider` — `twilio`/`aws`/no config paths are unchanged. Provider failures map to `error_code` values `timeout`, `http_<status>`, `remote_error`, `remote_failed`, or `config_error`; no silent failover. A remote that returns HTTP 200 but marks the SMS `failed`/`undelivered` is now correctly recorded as a failed send locally (not silently `sent`). `PhoneConfig.test_connection()` gains a mojo branch that validates the config by GETting the remote's `/api/group/apikey/me` whoami endpoint and confirming the key carries a send permission — zero side effects, no SMS row created (a valid key lacking `send_sms`/`comms` returns `error="insufficient_permission"`). New setting `SMS_REMOTE_TIMEOUT` (default `10`). Test numbers (`+1555…`) continue to short-circuit locally without hitting the remote. Migration: `phonehub.0003_phoneconfig_mojo_remote_url_and_more`.

**phonehub** — admin REST surface for `PhoneConfig`: individual auto-setters (`set_twilio_account_sid`, `set_twilio_auth_token`, `set_aws_access_key_id`, `set_aws_secret_access_key`, `set_mojo_api_key`) let admin clients write encrypted credentials transparently via the standard `POST /api/phonehub/config/<id>` body (e.g. `{"mojo_api_key": "<token>"}`) without exposing them in any graph. New per-instance action `POST /api/phonehub/config/<id>` with `{"test_connection": 1}` returns the per-provider connectivity-check dict inline — used by admin "Test connection" buttons.

**account** — new `GET /api/group/apikey/me` whoami endpoint for `ApiKey` (group-scoped) authentication. Returns the calling key's own identity, group, and granted permissions via a new token-free `me` graph; requires only `@requires_auth` (no management permission). A user/JWT request has no API key and gets `401`. Lets a key holder verify a token works and introspect what it can do — used by `PhoneConfig.test_connection()` for the mojo SMS provider.

**account** — mojo GeoIP provider and abuse-signal federation. One django-mojo instance can use another as its primary/fallback GeoIP source (`GEOIP_PRIMARY_PROVIDER='mojo'`), and pushes observed threat escalations and attacker/abuser flag flips back to the upstream asynchronously via `POST /api/system/geoip/sync` (requires `geoip_sync` ApiKey permission). `block()` now atomically escalates `threat_level` to at least `high`. Per-fleet firewall state (`is_blocked`, `is_whitelisted`, etc.) is never federated.

**account** — geofencing policy engine. System-wide and per-group rules gate access at the HTTP layer before any view logic runs.

- Rule DSL supports `country` (ISO 3166-1), `region` (ISO 3166-2), and `abuse` (`tor`, `vpn`, `datacenter`, `proxy`) with `in` / `not_in` / `eq` operators. Empty `{}` = allow all.
- Two rule levels, both must pass: `GEOFENCE_SYSTEM_RULES` (settings, hard floor) and `Group.metadata['geofence']` (per-tenant).
- `@md.requires_geofence()` applied to all built-in auth endpoints. Blocked requests return 403 with `{error, code, reason, detail}` only — country/region/abuse omitted to prevent information leakage.
- `bypass_geofence` permission short-circuits all checks without a cache write (revocation is immediate).
- `GET /api/geo/check?group_uuid=<uuid>` — public pre-flight endpoint returning the full `GeoDecision` shape for UI "not available in your region" gates.
- New `GEOFENCE_ENABLED`, `GEOFENCE_SYSTEM_RULES`, `GEOFENCE_CACHE_TTL`, `GEOFENCE_FAIL_CLOSED`, `GEOFENCE_ALLOW_PRIVATE_IPS`, and `GEOFENCE_TEST_OVERRIDE` settings (all default to permissive/off).
- `GeoLocatedIP` gains a `region_code` field (ISO 3166-2); backfilled lazily via `refresh()`. `geolocate_ip()` output includes `region_code`.

## v1.2.11 - May 12, 2026

add resolve to ticket handler


## v1.2.10 - May 11, 2026

bugfix in catchall rules


## v1.2.12 - May 11, 2026

**incident** — new `resolve://` handler scheme. Chaining `resolve://` at the end of a handler string (e.g. `ticket://?priority=8,resolve://`) automatically resolves the incident once all prior handlers have run. Accepts optional `?status=closed` to close instead of resolve, and `?note=...` for a custom history note. Records the status change in incident history and fires the resolved metrics counter, matching the behaviour of a manual REST resolution.

## v1.2.11 - May 11, 2026

**incident** — a `RuleSet` with no conditions now matches every event in its category, acting as a catch-all. Previously both `check_all_match` and `check_any_match` returned `False` when there were no rules, making condition-free rulesets unreachable.

## v1.2.10 - May 11, 2026

**fileman** — fix transparent images rendering with a black background when creating renditions. Transparent areas in PNG, WEBP, and GIF files are now preserved correctly; when converting to a non-alpha format (e.g. JPEG) transparent areas are composited onto a white background instead of black. Also handles palette-mode (`P`) and grayscale+alpha (`LA`) images correctly.

## v1.2.9 - May 11, 2026

LLM fix for GeoIP refrence


## v1.2.8 - May 11, 2026

security patch for use of system user


## v1.2.7 - May 11, 2026

ticket AI not responding fix


## v1.2.6 - May 11, 2026

security updates


### Added
- **Incident `metadata.event_count`** — the incident's `metadata` now carries a running `event_count` that is incremented every time an event links to it (`Event.link_to_incident`). Merge (`Incident.on_action_merge`) adds the moved events to the target's count so it stays accurate after consolidation.
- **Disable lifecycle for User and Group** — unified state and audit metadata for `is_active=False` under `metadata.protected.disable.*`. New `disable` and `reactivate` POST_SAVE_ACTIONS on both models capture `reason`, `at`, `by_user_id`, `by_username`, `note`, with FIFO-capped `history` of prior cycles. The inactive sweep, `pii_anonymize()`, and admin REST writes all flow through a single `mojo.apps.account.services.disable` service. Legacy keys (`disable_warned`, `disable_warn_date`, `no_disable`) are still honoured on read for one release; data migration `0041_disable_lifecycle_migrate` populates the new namespace from legacy keys without removing them. See `docs/django_developer/account/disable_lifecycle.md`.
- **`GET /api/auth/manage/throttle`** — admin-only support endpoint that reads the per-account login attempt counter from Redis without modifying it. Returns `{count, limit, window, retry_after_seconds}`. Pairs with the existing `clear_rate_limit` POST endpoint. New `read_account_attempt` helper in `mojo.decorators.limits`.
- **Credential-change permission relaxed from superuser-only to admin tier** — `_handle_existing_user_pre_save` now allows callers with `users` (domain category), `manage_users` (strict admin), or `is_superuser` to direct-write `email`, `username`, or replace an existing `phone_number` on another user's record via REST. Previously gated to superuser only, which forced operational dependence on the `is_superuser` flag for routine credential fixes. Self-service users without an admin perm are still blocked — they must use the change flows (`POST /api/auth/email/change/request` etc.) that verify channel ownership via OTP/link. Phone clear/first-set continues to be allowed without admin perms.
- **Force-verify and admin password-set relaxed to admin tier** — `is_email_verified` and `is_phone_verified` moved from `SUPERUSER_ONLY_FIELDS` to a new `ADMIN_ONLY_FIELDS` constant, gated on `users` / `manage_users` / `is_superuser`. `set_new_password` and `can_change_password` now also accept `users` alongside `manage_users` and superuser, so admin password-resets without a `current_password` work at the broader admin tier. `is_dob_verified`, `is_superuser`, and `is_staff` remain superuser-only.
- **`Group.member_count`** — new property + `extra` on the `default` graph (not `basic` — that stays minimal). Returns the count of active direct members (excludes inactive memberships and descendant groups). Lets list endpoints render member counts without fetching the membership collection.
- **`users` and `manage_users` collapsed to one admin tier for User operations** — every `manage_users` check on the User model and its REST surface now also accepts `users`. Affected: `MANAGE_USERS_ONLY_FIELDS` field gate (now `ADMIN_ONLY_FIELDS`, with the old name retained as a back-compat alias), `set_permissions`, `on_action_disable` / `on_action_reactivate`, `POST /api/auth/manage/clear_rate_limit`, `GET /api/auth/manage/throttle`, and the `SYS_USER_PERMS_PROTECTION` defaults for protected permission grants. `requires_mfa` joined `ADMIN_ONLY_FIELDS` (was superuser-only). Deployments that simplify away the `view_X` / `manage_X` split by holding only `users` for admin work now have a single, consistent gate. Superusers always qualify.
- **Five User POST_SAVE_ACTIONS as preferred surface for self-service account ops** — `change_username`, `revoke_sessions`, `confirm_totp`, `regenerate_totp_codes`, `disable_totp`. Each mirrors a dedicated `/api/auth/*` or `/api/account/totp/*` endpoint. All require `self.is_request_user()` — admins acting on another user record cannot trigger them even with `manage_users`. The legacy dedicated endpoints (`/api/auth/username/change`, `/api/auth/sessions/revoke`, `/api/account/totp/{confirm,recovery-codes/regenerate}`, `DELETE /api/account/totp`) remain available for back-compat. Behavior note: `revoke_sessions` POST_SAVE_ACTION returns a status only; the legacy endpoint returns a fresh JWT — clients using the new path must re-authenticate after.

## v1.2.5 - May 08, 2026

llm support for updating group in ticket


## v1.2.4 - May 07, 2026

new fanout on metrics


### Added
- **Metrics fan-out across child groups** — `/api/metrics/fetch` now accepts a `child_kind` query parameter. When set with `account=group-<parent_id>`, the endpoint resolves all active descendants of the parent group whose `kind` matches and returns the per-bucket sum of the metric across every matching child. Permission is checked once on the parent; members of the parent or any ancestor group are authorized via the existing `Group.user_has_permission` parent-chain walk. The descendant set is capped at `METRICS_FANOUT_MAX_CHILDREN` (default 200). Empty descendant sets return zero-filled series. Implemented as `mojo.apps.metrics.rest.helpers.fetch_group_fanout`. See `docs/web_developer/metrics/metrics.md` (Parent-Group Fan-Out) and `docs/django_developer/metrics/fetching.md` (Group Fan-Out).
- **Metrics fan-out — per-child breakdown** — `/api/metrics/fetch?breakdown=true` (with `child_kind`) returns one series per child group instead of the sum, keyed by child `name` (with `name#<id>` disambiguation when names collide). Response includes a top-level `groups` map of `key -> id` so dashboards can resolve names to ids without a second call. Single-slug only — multi-slug + breakdown returns 400.

## v1.2.3 - May 07, 2026

FIX in how AI Assistant discovers skills


### Added
- **Assistant skill catalog in system prompt** — `build_skill_catalog()` now runs at the start of every conversation turn and injects a markdown list of all accessible active skills (name, ID, tier, description, triggers, auto-execute flag) into the system prompt. The LLM can recognize skills from the catalog and load their steps with `find_skill(skill_id=<id>)` without a keyword search round-trip.
- **`get_skill(user, skill_id, group=None)`** — new service function in `mojo.apps.assistant.services.skills` that loads a single skill by primary key with permission checks (tier scoping and user-tier owner guard).
- **`update_skill(user, skill_id, group=None, **fields)`** — new service function for partial updates. Only the keyword arguments provided are written; all other fields are left unchanged. Validates each supplied field with the same rules as `save_skill`.
- **`find_skill` tool — `skill_id` parameter** — the `find_skill` assistant tool now accepts an optional `skill_id` integer. When provided it calls `get_skill()` directly instead of running a keyword search. The `query` parameter still works as before and is used when `skill_id` is omitted.
- **`update_skill` tool** — new core tool (`domain="skills"`, `mutates=True`) that exposes `update_skill()` to the LLM. Accepts `skill_id` (required) plus any subset of `name`, `description`, `triggers`, `steps`, `auto_execute`, `is_active`.

## v1.2.2 - May 07, 2026

ticket status changes

### Changed
- **LLM agent `create_ticket` — two-layer deduplication** — ticket dedup now checks both the current incident and all open tickets for the same incident category. Previously only same-incident duplicates were suppressed; now a second incident in the same category appends its findings to the existing open ticket instead of creating a new one.
- **LLM agent prompt — open tickets section** — the per-incident prompt now includes an "Open Tickets for This Category" section listing up to 10 open `llm_review` tickets for the current category. The agent is instructed to prefer `add_ticket_note` over `create_ticket` when a matching ticket exists.
- **`add_ticket_note` — `incident_id` parameter** — the tool now accepts an optional `incident_id` that auto-appends a clickable incident reference card to the note, making it easy for human reviewers to navigate from a ticket note back to the triggering incident.


## v1.2.1 - May 06, 2026

MORE AI improvements


## v1.2.1 - May 06, 2026

assistant `add_context` tool — clickable model references on messages

### Added
- **Assistant `add_context` tool** — new core tool (`domain="models"`, always sent) that lets the LLM attach validated clickable model references to its response. The LLM calls `add_context` with a list of `{app_name, model_name, pk, label}` objects when it mentions specific records; each reference is validated through `_resolve_model()` + `_check_ai_access()` + `pk.exists()` before being returned. Invalid references are silently filtered. Max 20 refs per call.
- **`context` block type** — agent loop accumulates validated refs from all `add_context` calls in a turn and injects `{"type": "context", "references": [...]}` onto the final `Message.blocks` array. Multiple `add_context` calls in one turn merge into a single block. No block is injected when all refs fail validation.
- **System prompt guidance** — built-in system prompt instructs the LLM to use `add_context` when referencing specific records so admins can click through rather than searching.
- **Frontend rendering contract** — `context` block: render as a compact strip of linked cards per reference. REST URL: `/api/{app_name}/{model_name_lowercase}/{pk}`. Click action: `Modal.showModel(instance)` or navigate to detail view. See `docs/web_developer/assistant/blocks.md`.

## v1.2.0 - May 06, 2026

significant recent chants cause for a version bump


## v1.1.43 - May 06, 2026

ticket updates


## v1.1.42 - May 06, 2026

hotfix for race condition in token refresh


## v1.1.41 - May 06, 2026

bugfix for large scale game deployments the pool logic incorrectly re-inits


### Fixed
- **`redis.pool.RedisModelPool` — race + resurrect bugs in `init_pool` auto-init** — `init_pool()` is now idempotent (no-op when `is_ready()`) and guarded by a Redis `SET NX EX` lock at `{pool_key}:init_lock` so concurrent first-time inits don't race on `destroy_pool()` + per-item rebuild. `is_ready()` now checks set existence only (the available list is auto-deleted by Redis when empty during normal "all checked out" operation). `add_to_pool()` and `get_next_instance()` lazy-init via the idempotent `init_pool()` for cold-start convenience; `remove_from_pool()` no longer auto-inits — it returns `False` when the pool is uninitialized rather than rebuilding from the queryset just to remove. Use `init_pool(force=True)` to explicitly rebuild from the DB queryset. See `docs/django_developer/helpers/redis.md`.

### Changed
- **`add_to_pool()` return value semantic** — when called on a cold pool, the lazy init may add the instance from the queryset; in that case `add_to_pool()` now returns `False` ("already a member after init") rather than `True`. The behaviour for items NOT in the queryset is unchanged: `True` when newly added, `False` when already present.

## v1.1.40 - May 05, 2026

hotfix for ticket generator creating dups


## v1.1.39 - May 05, 2026

bugfix when sorting null fields


## v1.1.38 - May 04, 2026

assistant `aggregate_model` — FK group_by + `having`


### Added
- **`aggregate_model` — forward FK fields in `group_by`** — the assistant `aggregate_model` tool now resolves forward `ForeignKey` / `OneToOneField` group_by entries to their column attname (e.g. `"group"` → `"group_id"`). Both the relation name and the column name are accepted on input; the column form is what appears as the key on each result row, matching SQL `GROUP BY` semantics. Reverse relations and many-to-many fields are rejected with a clear error. Ordering on grouped queries is now strictly validated to reference either a resolved group_by column or an aggregation alias.
- **`aggregate_model` — `having` parameter** — new optional `having` object applies a post-aggregation filter (SQL `HAVING`) after `group_by` + `annotate`. Keys must reference an aggregation alias; lookup suffixes are restricted to scalar comparisons (`gte`, `gt`, `lte`, `lt`, `exact`, `in`, `isnull`, `range`). Requires `group_by`. Lets the assistant express "groups whose aggregate crosses a threshold" queries (e.g. `{"total__gte": 2}`) without pulling raw rows. See `docs/django_developer/assistant/README.md` and `docs/web_developer/assistant/README.md`.

## v1.1.37 - May 04, 2026

NEW advanced date filter, see filtering docs


### Added
- **REST list filtering — date-component lookups + partial-date shorthand** — list endpoints now accept Django's standard date-component lookups on `DateField` / `DateTimeField` columns (`?created__year=2026`, `?created__month=4`, `?created__quarter=2`, `__day`, `__week`, `__hour`, etc.), composable with `__in` / `__not`. The bare exact-match operator on a date field also accepts a partial date (`?created=2026-04`, `?created=2026`, `?created=2026-04-02`) and expands it to a tz-aware UTC `__gte` / `__lte` range using the request's `timezone` param (or `request.group.timezone`, falling back to UTC). `dr_start` / `dr_end` accept the same partial-date forms and expand to start/end of period. New helpers: `mojo.helpers.dates.parse_partial_date`, `partial_date_to_range`. Invalid component values and out-of-range partials return `400`. Component lookups themselves still run in DB time — use the partial-date shorthand for tz-aware semantics. See `docs/web_developer/core/filtering.md`.

### Changed
- **`redis.pool` — `skip_predicate` may now return a retry-after duration** — `RedisBasePool` and `RedisModelPool` predicates can return a positive `int`/`float` (seconds) to signal "temporarily ineligible, retry after N seconds." When any predicate returns numeric, `get_next_available` / `get_next_instance` honour the caller's `timeout` as a true wallclock budget — they hold deferred candidates out of the available list and sleep until the soonest retry-after matures (capped at 1s per sleep so peer checkins are observed promptly), republishing all deferred items on exit so peers are not starved. The pre-existing bool path (`True`/`False`) and predicate-less callers behave exactly as before; the no-predicate fast path is byte-identical to the previous implementation. See `docs/django_developer/helpers/redis.md`.

## v1.1.36 - May 03, 2026

- added strict login throttling for failed logins


### Added
- **Per-account login throttling and bypass-resistant tiers** — `POST /api/login` now applies a 5-tier defense stack: IP (100/60s), server-set cookie muid (10/300s, bypass-resistant), per-resolved-account (10/900s, configurable via `LOGIN_USERNAME_LIMIT` / `LOGIN_USERNAME_WINDOW`), an `invalid_password` incident rule that fleet-wide IP-blocks for 30 minutes after 5 level-5 events in 15 minutes, and IP limits on TOTP/passkey verify endpoints (`MFA_VERIFY_IP_LIMIT` / `MFA_VERIFY_IP_WINDOW`). Per-account counter is cleared on successful password match. Admin clear endpoint `POST /api/auth/manage/clear_rate_limit` now accepts `username` or `user_id` to release a stuck account.
- **`muid_limit` / `muid_window` on `rate_limit` and `strict_rate_limit`** — new optional parameters for server-set cookie dimension (bypass-resistant alternative to `duid_limit`).
- **`check_account_attempt(key, account_id, limit, window, request=None)`** — view-level helper for per-account sliding-window throttling; fail-open on Redis error. `clear_rate_limits()` accepts new `muid=` and `account_id=` params.

- **`redis.pool` — optional `skip_predicate` for conditional checkout** — `RedisBasePool` and `RedisModelPool` accept an optional `skip_predicate` callable that marks a pool member as temporarily ineligible without removing it from the pool. The next-available loop returns the candidate to the head of the list and tries the next one, bounded by the current pool size. Useful for cooldown windows, maintenance flags, or any "in-pool but not right now" pattern. Predicate exceptions are caught, logged, and treated as skip. `get_specific_instance` / `checkout_specific_instance` bypass the predicate by design. See `docs/django_developer/helpers/redis.md`.

## v1.1.35 - May 01, 2026

BUGFIX non auth return 200 on lists


## Unreleased (post v1.1.34)

### Added
- **Cross-origin auth handoff (authorization-code style)** — when the auth page redirects to a different origin after login, it mints a one-time code via `POST /api/auth/handoff` (authenticated) and appends it as `?auth_code=<code>` on the redirect URL. The consuming app calls `POST /api/auth/exchange` (public, single-use, rate-limited 20/min/IP) to swap the code for access + refresh tokens. New helpers in `mojo-auth.js`: `requestHandoffCode()`, `exchangeAuthCode(code)`, `handleAuthCodeFromURL()`. New service `mojo/apps/account/services/auth_handoff.py` (Redis-backed, key prefix `auth:handoff:`, single-use via `GET`+`DELETE`). New setting `AUTH_HANDOFF_CODE_TTL` (default `60`). Same-origin redirects are unchanged. Reuses `jwt_login(..., source="handoff")` so login-event tracking, last-login bump, and webapp-URL metadata fire on exchange. Redirect destinations are intentionally not allowlisted — see `docs/django_developer/account/auth.md` for the security trade-off.

## v1.1.34 - April 26, 2026

new CRUD '_mode' support for aggretating'


## Unreleased (post v1.1.33)

### Added
- **Generic aggregation modes on every list endpoint** — `?_mode=count|top|distinct|summary|histogram` on any `MojoModel` list URL returns the matching aggregation shape over the same permission-scoped, group-scoped, and filter-scoped queryset. Replaces the legacy `?size=0` count hack and bespoke "top X by Y" endpoints. Existing list behavior is unchanged when `_mode` is absent. The `_*` query-param prefix is now reserved for the framework — every key starting with `_` is consumed by the aggregation layer (or other framework features) and skipped by the field-filter parser. Server-side caps protect the database (`MOJO_REST_AGG_TOP_CAP=100`, `MOJO_REST_AGG_DISTINCT_CAP=1000`, `MOJO_REST_AGG_HISTOGRAM_CAP=10000`). Aggregation responses include a `took_ms` field rounded to the nearest 10ms. Models can opt into a stricter allow-list via `RestMeta.AGGREGATION_FIELDS = [...]`; `RestMeta.SENSITIVE_FIELDS` is also honored. Full client-facing contract in `docs/web_developer/core/aggregation.md`.
- **`with_delta=true` on `/api/metrics/series`** — opt-in flag that returns `prev_data`, `prev_when`, and a `deltas` map (`{delta, delta_pct}` per slug; `delta_pct` omitted when prev value is 0). Backwards compatible — response is unchanged when flag is absent. Also fixes a bug where passing `?when=` to this endpoint would 500 due to missing datetime coercion.
- **`GET /api/incident/health/summary`** — returns the most recent `Event` per `system:health:*` category in a single call, sorted by category. Replaces N round-trips for the Security Dashboard health strip. Accepts optional `?prefix=` to query other namespaced roots. Requires `view_security` / `security` permission.
- **`auth:failures` metric slug** — aggregate counter bumped once per auth-failure event (`invalid_password`, `login:unknown`, `totp:login_failed`, `totp:login_unknown`, `passkey:login_failed`). Recorded under `account=incident`, `category=auth`. Requires `INCIDENT_EVENT_METRICS=True`.

## v1.1.33 - April 26, 2026

ai assistant fixes, new group bundling and group event support


## Unreleased

### Added
- **`Event.group` and `Incident.group`** — nullable FK to `account.Group` (`on_delete=SET_NULL`, `db_index=True`). Auto-derived from caller `group=` kwarg → `request.group` precedence in `incident.report_event()`. Deletion of the group sets the FK to `null` but `metadata.group_id` and `metadata.group_name` snapshots in the event record are preserved, so audit history survives group rename or deletion.
- **`MojoModel` incident methods auto-stamp group** — `instance.report_incident()` reads `self.group`; `class_report_incident()` and `class_report_incident_for_user()` read `request.group`. All use `setdefault` semantics so caller-supplied `group=None` is preserved.
- **Four new `BundleBy` modes** (IDs 10–13, existing 0–9 unchanged):
  - `GROUP_ID` (10) — bundle by group
  - `GROUP_AND_MODEL_NAME` (11) — bundle by group + model type
  - `GROUP_AND_MODEL_NAME_AND_ID` (12) — bundle by group + model instance
  - `GROUP_AND_SOURCE_IP` (13) — bundle by group + source IP
- **`metadata.group_mismatch` audit flag** — when events from different groups are bundled into the same incident, `Incident.group` is set to `null` and `metadata.group_mismatch=True` is stamped. This flag is set once and never cleared — it is an audit-stable marker, not a transient state.
- **`Event` and `Incident` REST graphs now include `group_id`** — the default response shape includes a scalar `group_id` on both models. The full `Group` object is intentionally NOT nested into the response: the simple serializer does not gate nested object graphs on the requester's permissions, so embedding the live group would leak cross-tenant fields (name, kind, last_activity) to anyone with system-wide `view_security`. Consumers should look up the group by id through the standard `/api/group/<id>` endpoint, which respects per-group view permissions. The `metadata.group_id` and `metadata.group_name` snapshot remain available for audit display (the snapshot is captured at event creation time under the requester's context).

### Fixed
- **Spurious permission_denied events on list endpoints** — Recovery paths in `on_rest_handle_list` (Group's empty-list fallback, `MOJO_REST_LIST_PERM_DENY=False` branch, owner/group-filtered fallbacks) previously emitted a `user_permission_denied` / `view_permission_denied` / `group_member_permission_denied` event even though the request returned HTTP 200. This filled security logs with false-positive denials that masked real ones. The REST dispatcher (`mojo/decorators/http.py`) is now the single emission site — events are recorded only when the request actually responds 401 or 403.
- **Assistant WS dropped intermediate prose** — when the model wrote prose alongside `tool_use` blocks in the same turn (e.g. "Both IPs are benign — bulk-updating now" followed by a `bulk_update_incidents` call), the `text` block was persisted into `Message.tool_calls` but never published over WebSocket. Live users only saw the final wrap-up; the intermediate analysis appeared only after a manual page refresh. Fixed by introducing a new `assistant_text` WS event that fires before the turn's `assistant_tool_call` events, and by cleaning up the `Message` row shape so intermediate text lives in `Message.content` (and parsed `assistant_block` fences in `Message.blocks`) instead of being buried inside `Message.tool_calls`. The terminal `assistant_response` continues to fire and remains the signal the client uses to clear the thinking indicator.

### Changed
- **Assistant chart blocks support the new web-mojo SeriesChart / PieChart options** — the `chart` block schema now accepts `stacked`, `grouped`, `crosshair_tracking`, `cutout`, `show_labels`, `show_percentages`, `colors`, `show_legend`, `legend_position`, and per-series `color` / `fill` / `smoothing`. The system prompt teaches the LLM when to use each. `_validate_block` gained a `chart` branch that enforces shape (chart_type, labels, series, length-matching) and clamps / coerces soft fields (`cutout` to `[0, 1]`, `stacked` to `{True, False, "auto"}`, `crosshair_tracking` to `bool`). Unknown top-level fields pass through unchanged for forward compatibility. Existing minimal chart blocks render identically — no client coordination required.
- **`MojoModel.rest_check_permission` is now a pure boolean predicate** with no side effects. New `MojoModel.rest_check_permission_or_raise` raises `PermissionDeniedException` with structured metadata (`branch`, `perms`, `permission_keys`, `model_name`, `instance`, `event_type`) for handlers that respond 401/403.
- **New incident event categories:**
  - `feature_disabled` — raised when `CAN_UPDATE/CAN_DELETE/CAN_CREATE/CAN_BATCH = False` rejects a request. Distinguishable from per-user denials; still 403, still audited.
  - `fk_attach_denied` — emitted by `on_rest_save_related_field` when a FK assignment is silently skipped due to missing VIEW_PERMS on the related instance. Carries `field_name`, `related_model`, `related_id`, `branch`. No HTTP error — the parent save still returns 200.
- **Unauthenticated requests at permission-gated REST handlers now return HTTP 401** (previously returned 403). Authenticated-but-forbidden continues to return 403. Incident category for unauthenticated paths is `unauthenticated`.
- **`MOJO_APP_STATUS_200_ON_ERROR` is now honored uniformly** for both raise-based 401/403 responses and the existing `rest_error_response` paths.

## v1.1.32 - April 25, 2026

shortlink support


### Added
- **Fileman URLs via shortlink, by default** — `File.generate_download_url()` and `FileRendition.generate_download_url()` now return a `/s/<code>` URL backed by a tier-1 `ShortLink` row (auto-created on first read, cached in a new `shortlink_code` column). The shortlink resolver regenerates the underlying backend URL per click, so S3 presigns stay fresh behind a stable short URL. Opt-out is available globally via `FILEMAN_USE_SHORTLINKS=False` and per-FileManager via `FileManager.settings["use_shortlinks"]`. Optional per-manager settings: `shortlink_track_clicks` (bool, default False), `shortlink_expire_days` (int, default 0 = never). Shortlink is treated as an **optional** dependency — when the app isn't installed, fileman falls back to direct backend URLs (behavior identical to pre-shortlink). `bot_passthrough=False` across the board — preview crawlers hit the OG interstitial, never the signed URL.
- **Fileman share action (tier-2 share links)** — new `POST /api/fileman/file/<id>` and `POST /api/fileman/rendition/<id>` with body `{"share": true}` or `{"share": {"expire_days": 30, "track_clicks": true, "note": "for Alice"}}`. Each call mints a distinct `ShortLink` row (`source="fileman-share"`) attributed to the sharing user, enabling per-sharer audit ("whose link got used, how many times"). Returns `{url, shortlink_code, expires_at, track_clicks}`. `expire_days` is clamped to 3650; `note` is truncated to 512 chars. Returns an error dict when shortlink isn't installed.
- **`GET /api/fileman/rendition[/<pk>]`** — read-only REST endpoint for `FileRendition` (needed to support the rendition `share` action). Create/delete remain blocked.
- **`ShortLink.rendition` FK** — new nullable FK from `shortlink.ShortLink` to `fileman.FileRendition`. `shortlink.shorten(rendition=r)` and `ShortLink.create(rendition=r)` accept the new kwarg; the resolver prefers `rendition.get_direct_download_url()` over `file`.
- **`get_direct_download_url()`** on File and FileRendition — escape hatch that returns the raw backend URL, bypassing shortlink wrapping. Used by the shortlink resolver (preventing recursion) and as the disabled-path fallback.

### Changed
- **`regenerate_renditions` action shape** — promoted from a string-switch inside `{"action": "regenerate_renditions"}` to a discrete POST_SAVE_ACTIONS key. Clients now send `{"regenerate_renditions": true}` (regenerate all defaults) or `{"regenerate_renditions": ["thumbnail", "preview"]}` (specific roles). The legacy `{"action": "regenerate_renditions"}` shape is no longer recognized. Legacy `{"action": "mark_as_*"}` shapes remain supported for UI compatibility.
- **File delete cleanup** — `File.on_rest_pre_delete` now also removes auto-generated shortlink rows (`source__in=["fileman", "fileman-share"]`) for the file and its renditions. Human-created shortlinks (other `source` values) are preserved (FK `SET_NULL`).

## v1.1.31 - April 23, 2026

BUGFIX in video rendering


### Changed
- **Fileman renditions are now async** — `File.mark_as_completed()` no longer blocks on ffmpeg/Pillow work. It enqueues a `mojo.apps.jobs` job (`mojo.apps.fileman.asyncjobs.process_file_renditions`) on the `renditions` channel via `transaction.on_commit`, with `idempotency_key="renditions:<file_id>"` to collapse duplicate publishes. The file's `upload_status` flips to `completed` immediately; the `renditions` map may be empty briefly until the worker finishes. Fixes video renditions never appearing (the previous synchronous path timed out on any non-trivial video).
- **Fileman `regenerate_renditions` action** — `POST /api/fileman/file/<id>` with `{"action": "regenerate_renditions"}` enqueues a background regenerate job. Optional `roles: [...]` scopes regeneration to specific rendition roles; omit to rebuild all defaults.
- **Fileman REST hardening** — `/api/fileman/manager` and `/api/fileman/file` endpoints now decorate with `@md.uses_model_security(Model)` (required by the current framework for RestMeta endpoints). `FileRendition` REST is now read-only (`CAN_CREATE=False`, `CAN_DELETE=False`) — renditions are derived data and are managed through the parent `File` (cascade on delete, `regenerate_renditions` action for rebuild).

### Removed
- **Fileman Celery layer removed** — `mojo/apps/fileman/tasks.py` and `mojo/apps/fileman/signals.py` deleted. They were never wired (the signals import in `apps.py` had always been commented out), `tasks.py` had a broken import (`process_new_file`), and Celery was not a declared dependency. All background work now flows through `mojo.apps.jobs`.
- **Fileman dead utils removed** — `get_file_manager`, `validate_file_request`, `initiate_upload`, `finalize_upload` deleted from `mojo/apps/fileman/utils/upload.py`. They referenced `File` fields (`uploaded_by`, `original_filename`, `file_path`, `upload_expires_at`) that no longer exist. `direct_upload` and `get_download_url` — the live functions — are preserved.

### Docs
- Renamed `docs/django_developer/files/` → `docs/django_developer/fileman/` and `docs/web_developer/files/` → `docs/web_developer/fileman/` (folder names now match the URL prefix and avoid confusion with the `files` permission category). New `docs/django_developer/fileman/renditions.md` covers the async pipeline.

## v1.1.30 - April 22, 2026

new bouncer contact us, improved qrcode generation


### Added
- **QR code builder page** — new `GET /qrcode/builder` serves a self-contained HTML form for generating vCard QR codes interactively (name/phone/email/logo/color controls, live preview, PNG download). Useful as an admin/dev tool. Public endpoint; uses the existing `/api/qrcode/vcard` API.
- **QR code endpoint hardening** — `/api/qrcode` and `/api/qrcode/vcard` now enforce a 512KB cap on decoded `logo` payloads (`MAX_LOGO_BYTES` in `mojo.helpers.qrcode`) and apply `@md.rate_limit` (60/min/IP for base endpoint, 30/min/IP for vcard). Protects the public endpoints against DoS via oversized logo blobs or high-volume image generation.
- **QR code vCard endpoint** — new `POST /api/qrcode/vcard` accepts a structured `vcard` object (`name`, `org`, `title`, `phone`, `email`, `url`, `address`, `note`) and encodes it as a QR code. Supports vCard 3.0 (default) and MeCard via `vcard_format`. Auto-defaults `error_correction` to `h`; when `logo` is provided, forces `h` and defaults `size` to 512 for scannability. New `mojo.helpers.qrcode.build_vcard()` helper performs RFC 6350 escaping and is reusable outside the endpoint. Also fixes a latent bug in `/api/qrcode` where error-path responses crashed because `md.response_error` does not exist.
- **Bouncer public messages (contact / support intake)** — new `account.PublicMessage` model, bouncer-gated HTML page at `/contact` (configurable via `BOUNCER_CONTACT_PATH`), and public submit endpoint `POST /api/account/bouncer/message` protected by `@md.requires_bouncer_token('public_message')` + `@md.strict_rate_limit('public_message_submit', ip_limit=5, ip_window=300)`. Ships with `contact_us` and `support` kinds — schemas live in `mojo.apps.account.services.public_message.KIND_SCHEMAS` as a single source of truth for both the form renderer and submit validator. Submissions fire an incident event, a metric, and an email notification to every `User` flagged via `metadata.protected.notify_public_messages=True` (group-scoped when the bouncer resolves a group). Admin list/detail/status-update surface at `/api/account/public_message[/<pk>]` behind `view_support` / `manage_support` / `support` / `security` perms with automatic group-scoped filtering. See `docs/django_developer/account/bouncer.md` § Public Messages and `docs/web_developer/account/public_messages.md`.

## v1.1.29 - April 20, 2026

Security AI update


### Fixed
- **Incident pruning preserves ticketed incidents** — `prune_incidents` and `Incident.check_delete_on_resolution()` now skip any incident referenced by a `Ticket`. If the LLM or an operator created a ticket from an incident, that incident is worth keeping as history. Previously a ticket's `incident` FK (`on_delete=SET_NULL`) would silently go to `NULL` when the incident was pruned or auto-deleted on resolution, stranding the conversation transcript.
- **`Incident.on_action_merge` reassigns tickets before delete** — merged-in incidents' tickets are now repointed to the target incident instead of losing their linkage.
- **`Incident.add_history` tolerates a deleted parent** — if the in-memory incident's row has been deleted between load and insert, the method returns silently instead of raising a FK violation. Fixes noisy `ForeignKeyViolation` errors from `execute_llm_ticket_reply` when the LLM operated on an incident that had been removed between turns.
- **LLM agent deduplicates tickets and rule proposals** — `_tool_create_ticket` now reuses an open, LLM-linked ticket on the same incident instead of spawning duplicates, and `_tool_create_rule` now matches proposals by `(category, handler, sorted rule conditions)` against existing `llm_proposed` RuleSets: pending matches bump `metadata.occurrence_count` and append to the approval ticket, active matches are skipped silently. Previously repeated agent invocations could create hundreds of identical approval tickets for the same pattern.

## v1.1.28 - April 20, 2026

Bugfix in AI Assistant serializer


## v1.1.28 - April 20, 2026

### Fixed
- **Assistant agent tool-result serialization crash** — `ujson.dumps` was used at the tool-result boundary with no fallback, so any tool returning a `datetime`, `Decimal`, `UUID`, or Django `Model` instance crashed the agent turn silently. Replaced with a stdlib `json` serializer (`_dumps_tool_result`) that handles `datetime`/`date`/`Decimal`/`UUID`/`Model`/`QuerySet`/`bytes`/`set`. On unrecoverable failure, a JSON error payload is returned to the LLM so the turn continues instead of stalling.

### Added
- **Three new assistant incident categories** — `assistant:error:serialize` (level 7, tool-result serialization failure), `assistant:error:parallel` (level 6, parallel tool or plan-step failure), `assistant:error:unhandled` (level 8, catch-all agent-loop exception). Previously these failure paths only logged to file and were invisible to the incident system.

## v1.1.27 - April 20, 2026

BUG FIX release for ip post actions


## v1.1.26 - April 20, 2026

SECURITY PATCH: when assigning users; fix GeoLocatedIP POST_SAVE_ACTIONS


### Fixed
- **`GeoLocatedIP` POST_SAVE_ACTIONS broken by trailing comma** — A stray trailing comma in `RestMeta.POST_SAVE_ACTIONS` turned the list into a 1-tuple containing a list. The REST dispatcher checks `action in POST_SAVE_ACTIONS`, which always returned `False` against a tuple, silently dropping all six action handlers (`block`, `unblock`, `whitelist`, `unwhitelist`, `refresh`, `threat_analysis`) on `PUT /api/system/geoip/<pk>`. Fixed by removing the trailing comma; all six actions now route correctly. Regression test added in `tests/test_account/test_geoip_actions.py`.

### Changed
- **Create-time owner auto-stamp now respects body-provided values** — `on_rest_save` in `mojo/models/rest.py` previously overwrote `CREATED_BY_OWNER_FIELD` (default `"user"`) with `request.user` unconditionally on every create, discarding any value the body provided. It now mirrors the existing `group` behavior: the framework only auto-stamps when the field is `None` after the body has been applied. Body-provided `user` values win; self-signup (body omits `user`) is unchanged. **Breaking (intended fix)**: any model that implicitly relied on the clobber as create-time authorization (i.e. "users cannot create records owned by someone else") now becomes permissive at the framework level — callers with `SAVE_PERMS` plus `VIEW_PERMS` on `account.User` can designate another user as owner via the body. Migration: if you need strict self-ownership, set `CREATED_BY_OWNER_FIELD = None` on the model's `RestMeta` and re-assert `self.user = self.active_user` in `on_rest_pre_save`. See `docs/django_developer/rest/permissions.md` → "Create-time owner stamping" for policy patterns (strict self-ownership, admin-creates-for-user). Motivated by consumer reports of admin flows (e.g. enrolling another group member as an operator) silently creating rows against the authenticated user and leaking raw Postgres uniqueness errors.

## v1.1.25 - April 19, 2026

release for more advanced ai tools, more security checks


### Added
- **`CAN_UPDATE` RestMeta flag** — Real gate for PUT/POST on existing instances, mirroring `CAN_CREATE` / `CAN_DELETE`. Defaults `True`, so no existing model changes behavior unless it opts in. Returns `403` with `error = "UPDATE not allowed: <ModelName>"` on denial — a distinct message from permission failures. Previously `CAN_SAVE` was referenced in 8+ RestMetas but **never read by rest.py**, leaving `account.LoginEvent` and `shortlink.Click` silently updateable despite declaring `CAN_SAVE = False`. `CAN_SAVE` is now honored as a one-release deprecated alias (emits a once-per-class `logit.warn` when used alone; `CAN_UPDATE` wins when both are set). 8 existing models migrated: `login_event.py` and `click.py` to `CAN_UPDATE = False`; six others had redundant `CAN_SAVE = True` removed (True is the default). Enforced in two places: `on_rest_handle_save` in `mojo/models/rest.py` (REST path) **and** `_tool_save_model_instance` update branch in `mojo/apps/assistant/services/tools/models.py` (assistant path) — the assistant calls `instance.on_rest_save` directly and would otherwise bypass the gate.

### Changed
- **Breaking (intended fix)**: PUT to `/api/account/loginevent/<pk>` and `/api/shortlink/click/<pk>` is now correctly denied. These RestMetas declared `CAN_SAVE = False` with the obvious intent of making the rows append-only audit records, but the flag was never enforced — this closes the silent permissions gap.

### Added
- **Logging sanitization coverage aligned** — `mask_sensitive_data()` now derives its regex from `SENSITIVE_KEYS` at import time, so the string masker and `sanitize_dict()` share a single source of truth (previously the regex covered 11 keys while the dict sanitizer covered 21, so fields like `new_password`, `refresh_token`, `auth_token`, `private_key`, `otp`, `mfa_code` passed through stringified log lines uncredited). Adding a key to `SENSITIVE_KEYS` now automatically extends both code paths. Also adds `mask_token(token, visible=4)` for credential-safe logging: reveals only the last 4 chars for long tokens and fully masks short ones. Applied to `event_metadata["bearer"]` in `mojo/apps/incident/reporter.py` so raw bearer tokens no longer land in the audit log — forward-only; existing rows unchanged.
- **`DENY_AI_*` RestMeta flags** — Per-model opt-out for the assistant's model tools. Four verb-specific flags (`DENY_AI_VIEW`, `DENY_AI_CREATE`, `DENY_AI_UPDATE`, `DENY_AI_DELETE`) plus a `DENY_AI` shorthand that denies all four. All default `False`, so no existing model changes behavior. Gated in `_check_ai_access` in `mojo/apps/assistant/services/tools/models.py` and checked by `describe_model`, `query_model`, `aggregate_model`, `export_data`, `save_model_instance` (verb picked from `pk` presence), and `delete_model_instance`. Denied requests return `"<model> is not available to the assistant"` — distinct from permission-denied errors — and emit a level-4 `assistant_ai_denied` incident event. REST behavior is untouched; the flags are assistant-layer only.
- **Assistant metrics domain expanded from 3 to 13 tools** — Full read surface plus one write tool:
  - **Discovery**: `list_metric_accounts` (unions configured + data-inferred accounts), `list_metric_categories`, `list_metric_slugs` (with category and prefix filters, default 500 cap), `list_metric_gauges`, `describe_metric_slug` (greps the codebase for `metrics.record()` call sites), `resolve_group_account` (name/id → `group-<id>` with ambiguity handling).
  - **Fetch**: `fetch_metrics` rewritten with auto-granularity (<=3h → minutes, <=3d → hours, else days), retention notes when the range exceeds the granularity's TTL, and standard response metadata (`account`, `granularity`, `dt_start`, `dt_end`, `slug_count`). Plus `fetch_metric_values` (point-in-time snapshot) and `fetch_metrics_by_category` (capped at `max_slugs=50`).
  - **Gauges**: `get_metric_gauge` (read) and `set_metric_gauge` (write) for operational toggles like `maintenance_mode` and feature flags. Write tool gated by `write_metrics` and per-account `check_write_permissions`, writes a `logit.Log` audit entry (slug + account only, never the value).
  - Every read tool enforces per-account permissions via `mojo.apps.metrics.rest.helpers.check_view_permissions` — closing the prior loophole where `view_admin` at the tool level let admins read any account. All tools accept the five REST account forms (`public`, `global`, `group-<id>`, `user-<id>`, custom). Tool-level gate changed from `view_admin` to `view_metrics`/`metrics`.
  - Duplicate tool registrations (`list_metric_categories`, `list_metric_slugs`) removed from `discovery.py` and re-homed with proper gating.
  - `get_system_health` and `get_incident_trends` retained unchanged.
- **`save_model_instance` assistant tool** — Create or update any MojoModel instance from the assistant. Pass `pk` to update, omit to create. Honors the REST permission chain exactly: creates require `CAN_CREATE` plus `CREATE_PERMS`/`SAVE_PERMS`/`VIEW_PERMS`; updates require `SAVE_PERMS`/`VIEW_PERMS` on the target instance. FK fields can be set by primary key in `data`. Permission denials report a level-6 incident event. `mutates=True`, `core=False`, requires `view_admin`.
- **Per-mutation audit trail for assistant model tools** — Successful create/update/delete writes an entry to `logit.Log` with kind `assistant:model:created` / `:updated` / `:deleted`. Failed saves write `assistant:model:save_failed`. The audit message lists changed field NAMES only (never values) and the `payload` JSON carries `conversation_id` so audit entries tie back to the assistant turn. `delete_model_instance` was retrofitted to write the same audit entries.
- **Tool dispatcher threads HTTP context** — `run_assistant(...)` now accepts the originating Django request and builds a slim `request_meta` objict (ip, user_agent, path, method). Tool handlers can opt into the context by adding `request_meta` and/or `conversation` as keyword-only parameters; existing handlers are unchanged. Without this, assistant-originated incident events recorded source ip as None instead of the user's real ip.

### Fixed
- **FK assignment by scalar pk now requires VIEW_PERMS on the related model** — `MojoModel.on_rest_save_related_field` previously assigned looked-up FK targets without checking permissions on the related instance. A user with SAVE_PERMS on model A but no perms on model B could set `a_instance.b = <any B pk>` via REST, allowing cross-model privilege escalation (e.g. re-parenting a record under a Group the user doesn't belong to). Now the scalar-pk path runs `field.related_model.rest_check_permission(request, "VIEW_PERMS", related_instance)`; on denial the assignment is silently skipped (matching the existing dict-value branch) and `rest_check_permission` records an incident event. **Behavior change**: existing REST callers that today assign FKs by pk to targets they lack VIEW_PERMS on will now silently no-op the assignment instead of succeeding. Self-reference, FK clear (value=0/None/""), and the `on_rest_related_save` custom-hook branch are unaffected.

## v1.1.24 - April 16, 2026

bugfix when working with s3 buckets in the eu


## v1.1.23 - April 16, 2026

better docs, better logging


## v1.1.22 - April 15, 2026

BUGFIX: collision when saving related field with same pk


## v1.1.21 - April 14, 2026

### Fixed
- **Plaintext passwords no longer leak into logs** — `sanitize_dict()` added to `mojo/helpers/logit.py` strips known sensitive keys (`password`, `token`, `api_key`, `secret`, `authorization`, `ssn`, `cvv`, etc.) from any dict before it is persisted. Applied automatically in two chokepoints: `incident/reporter.py` sanitizes all dict kwargs passed to `report_event` (including `request_data`), and `logit/models/log.py` sanitizes the `payload` field before DB write. No call-site changes required — all callers are protected.

## v1.1.21 - April 13, 2026

new ai assistant file support


## v1.1.21 - April 13, 2026

### Added
- **`aggregate_model` assistant tool** — Run Django ORM aggregate queries (count, sum, avg, min, max, count_distinct) with optional `group_by` on any MojoModel. Enforces the same permission and owner/group scoping as `query_model`. Sensitive fields are blocked as aggregation or group-by targets. Use this for all summary questions — never pull rows just to count them. Requires `view_admin`.
- **`export_data` assistant tool** — Export query results to a downloadable CSV file stored in `fileman.File` (S3 or local). Data is written directly to file storage — not returned inline. Returns a download URL (shortlink if `mojo.apps.shortlink` is installed). The assistant presents the result using the new `file` structured block. Requires `view_admin` + a configured `FileManager` for the user/group. Row limit: default 5,000, max 50,000. Setting: `FILEMAN_EXPORT_EXPIRES_DAYS` (default 14).
- **`file` structured block type** — New block type for downloadable files. Schema: `{"type": "file", "filename", "url", "size", "format", "row_count", "expires_in"}`. Frontend should render as a download card with filename, size, format icon, and download button.
- **`metadata.expires_at` cleanup job** — `mojo/apps/fileman/cronjobs.py` registers a daily cron at 04:00 UTC that publishes `cleanup_expired_files` to the `cleanup` job channel. The async job deletes all active `fileman.File` records whose `metadata.expires_at` has passed, removing both the database record and the storage backend file. Works for any file with `expires_at` in metadata — not just assistant exports.

### Changed
- **`query_model` CSV removed** — The `format` parameter and inline CSV output have been removed from `query_model`. Use `export_data` for all CSV exports. `query_model` is now JSON-only and intended for small result sets (detail lookups, spot-checking).

## v1.1.20 - April 13, 2026

new github auth and app support


## v1.1.19 - April 13, 2026

### Added
- **GitHub OAuth provider** — Users can now sign in with their GitHub account via the standard OAuth flow (`GET /api/auth/oauth/github/begin`, `POST /api/auth/oauth/github/complete`). Handles private email addresses by falling back to `GET /user/emails`. Settings: `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_SCOPES`.
- **`mojo.apps.github` app** — New built-in app providing the `GitHubInstall` model for tracking GitHub App installations, a service layer for JWT generation and installation token management (`get_install_token`, `generate_jwt`, `verify_webhook_signature`), and a REST CRUD endpoint at `/api/github/github_install`. Permissions: `view_github`, `manage_github`.
- **`@md.requires_github_webhook()` decorator** — Validates the `X-Hub-Signature-256` HMAC-SHA256 signature on incoming GitHub webhook requests. Returns `403` on invalid or missing signatures.
- **`delete_model_instance` assistant tool** — Generic delete for any MojoModel with `CAN_DELETE = True`. Enforces the full `DELETE_PERMS` → `SAVE_PERMS` → `VIEW_PERMS` permission chain (owner and group checks included), calls `on_rest_pre_delete()`, and executes inside `transaction.atomic()`. Reports a security event on permission denial. Requires `view_admin` as a baseline; model-level perms gate actual execution.
- **`delete_rule` assistant tool** — Delete a single rule condition from a ruleset by rule ID, without removing the entire ruleset. Requires `manage_security`. Returns the remaining rule count on success.

## v1.1.19 - April 12, 2026

### Fixed
- **Bouncer redirect URL dropped for absolute URLs** — `auth_base.html` previously rejected any `?redirect=` value that did not start with `/`. Absolute URLs (e.g. `http://myapp.example.com/portal/`) are now accepted and used as-is.
- **Redirect param lost through bouncer challenge** — `_serve_challenge()` now forwards `redirect` (and aliases `next`, `returnTo`) and `back` params to the post-challenge login redirect URL so they survive the challenge → login page transition.

### Added
- **`?back=<url>` query param** — overrides the "Back to website" hero link on a per-request basis. Falls back to the `AUTH_BACK_TO_WEBSITE_URL` setting when not provided. Preserved through the bouncer challenge redirect.

## v1.1.19 - April 11, 2026

new oauth brand support


## v1.1.19 - April 11, 2026

### Added
- **Per-group white-label auth pages** — `Group.auth_domain` field maps a custom hostname to a group. The bouncer resolves the group from the request hostname (Redis-cached) or `?group=<uuid>` query param and applies that group's `AUTH_*` settings (logo, brand, OAuth, success redirect) to the login and registration pages.
- **`Group.resolve_by_auth_domain(hostname)`** — class method for hostname→group lookup with Redis caching (24h hits, 1h misses). Cache is invalidated automatically on `auth_domain` or `is_active` changes.
- **Per-group challenge branding** — `BOUNCER_CHALLENGE_LOGO_URL` and `BOUNCER_CHALLENGE_BRAND` settings resolve per-group. The challenge page uses the configured default branding; set these settings on a group to override.
- **OAuth `group_uuid` round-trip** — `group_uuid` is embedded in OAuth state so branding survives the Google/Apple provider redirect. The callback appends `?group=<uuid>` to the frontend redirect URI.
- **`groupUuid` in `window._matConfig`** — `auth_base.html` now passes `groupUuid` into `window._matConfig` so `mojo-auth.js` can include it in auth API calls.

## v1.1.18 - April 09, 2026

test are passing new release with new ai and better group logging


### Fixed
- **Markdown renderer not rendering tables** — Mistune plugins (table, url, task_lists, footnotes, etc.) were never being loaded because `_discover_plugins()` was commented out and `plugins=[]` was hardcoded. Tables and other plugin-dependent syntax now render correctly.

## v1.1.17 - April 08, 2026

missing expose user from conversation

### Added
- **`Log.gid` field** — `logit.Log` now records a `gid` (group ID) on every log entry. Auto-populated from `self.group_id` when using `MojoModel.log()`, or from `request.group.id` when using `Log.logit()` directly. Can be overridden by passing `gid=<value>` explicitly. Indexed on `(gid)` and `(gid, kind)` for efficient per-group audit trail queries. `gid` is included in both `basic` and `default` REST graphs.


## v1.1.16 - April 07, 2026



## v1.1.15 - April 07, 2026

bug fix on wrong end points and tool trucating


## v1.1.14 - April 07, 2026

new AI skills, imrpoved tools, and much more see changelog


## v1.1.16 - April 07, 2026

Critical performance fix: eliminated per-request Aurora lock contention on `account_userdevice`.

### Fixed
- **`UserDevice.track()` is now login-only** — `validate_jwt()` no longer calls `user.track()` on every authenticated request. It calls `user.touch()` instead, which issues a single targeted `UPDATE` on `account_user` with no device table involvement.
- **`muid` comparison bug** — the muid field on `UserDevice` was being updated whenever the incoming `_muid` cookie differed from the stored value (including on every request for devices that already had a muid). The condition now only sets `muid` when the device has no muid yet, making device identity write-once and stable across cookie resets.
- **`GeoLocatedIP.last_seen` staleness guard** — `GeoLocatedIP.get_or_create()` no longer writes `last_seen` on every call. It checks `GEOLOCATION_DEVICE_LOCATION_AGE` (default 300 seconds) and skips the update if the record was seen recently, eliminating redundant row writes on high-traffic deployments.
- **`user.touch()` uses `UPDATE` not `atomic_save()`** — `last_activity` updates are now issued as `User.objects.filter(pk=...).update(last_activity=now)`, avoiding a full-model save and the associated row lock.

## v1.1.15 - April 07, 2026

Assistant learned skills — reusable multi-step procedures.

### Added
- **`Skill` model** — database-backed reusable procedures scoped by tier (`global`, `user`, `group`). Each skill has a name, description, trigger phrases, and an ordered list of tool steps. A unique constraint prevents duplicate names within the same scope.
- **Skills service** (`mojo/apps/assistant/services/skills.py`) — `find_skills`, `save_skill`, `list_skills`, `delete_skill`. Permission model mirrors memory: global/user tiers require `assistant` permission; group tier requires group membership (write requires `assistant` on the `Member` record).
- **Four core assistant tools** — `find_skill`, `save_skill`, `list_skills`, `delete_skill`. All are `core=True` (always active, no `load_tools` call needed) and require `assistant` permission.
- **`skills` domain description** added to `DOMAIN_DESCRIPTIONS` in `mojo/apps/assistant/__init__.py`.
- **Skills settings** — `LLM_ADMIN_SKILLS_ENABLED` (default `True`), `LLM_ADMIN_SKILLS_MAX_PER_USER` (20), `LLM_ADMIN_SKILLS_MAX_PER_GROUP` (30), `LLM_ADMIN_SKILLS_MAX_GLOBAL` (20), `LLM_ADMIN_SKILLS_MAX_STEPS` (10).
- **System prompt updated** — the LLM is instructed to call `find_skill` when a request sounds like a stored procedure, confirm before executing steps when `auto_execute` is false, and evaluate step conditions against the previous step's result.
- **REST endpoint** — `GET/DELETE /api/assistant/skill` and `GET /api/assistant/skill/<id>?graph=detail` via RestMeta. `VIEW_PERMS = ["view_admin", "assistant", "owner"]`; `SAVE_PERMS = ["view_admin"]`.

## v1.1.13 - April 06, 2026

huge changes in our AI Agent, ability to spawn agents, build tasks and plans, and much more


## v1.1.14 - April 06, 2026

Rich blocks, task planning, and parallel tool execution for the admin assistant.

### Added
- **New block types** — `action`, `list`, `alert` blocks added alongside existing `table`, `chart`, `stat`. Action blocks render as confirmation cards with clickable buttons. List blocks replace single-row tables for key/value summaries. Alert blocks surface important warnings and status notices with a `level` field (`info`, `success`, `warning`, `error`).
- **`action_id` on action blocks** — each `action` block is tagged server-side with a UUID so the frontend can correlate button clicks with the originating card.
- **`assistant_action` WebSocket message type** — client sends `{type: "assistant_action", value: "...", conversation_id: N}` when the user clicks a button in an action block. The server converts it to a regular user message and the conversation continues.
- **Task planning tools** — `create_plan` and `update_plan` meta-tools (both `core=True`, `view_admin` permission). The LLM calls `create_plan` for complex requests requiring 3+ tool calls; `update_plan` marks step progress as work proceeds.
- **`assistant_plan` WS event** — published after `create_plan` succeeds. Payload: `{plan_id, title, steps}`.
- **`assistant_plan_update` WS event** — published after each plan step status change. Payload: `{plan_id, step_id, status, summary}`.
- **Parallel tool execution** — when the LLM requests multiple tool calls in a single turn, non-meta tools now run concurrently via `ThreadPoolExecutor`. Meta-tools (`load_tools`, `create_plan`, `update_plan`) always run first serially since they modify conversation state.
- **Plan-aware parallel step execution** — when `create_plan` is called with steps marked `parallel=True` and a `tool`+`tool_input`, the system immediately executes those steps concurrently without waiting for the LLM to call them individually.
- **`LLM_ADMIN_MAX_PARALLEL_TOOLS` setting** — controls the `ThreadPoolExecutor` pool size. Default: `4`.
- **`planning` domain** — new tool domain description added to `DOMAIN_DESCRIPTIONS`.
- **`_validate_block()` in `agent.py`** — structural validation for all block types, called from `_parse_blocks()`. Invalid blocks are silently dropped.

### Changed
- **Mutating operation confirmation** — the system prompt now instructs the LLM to use `action` blocks for confirmations instead of asking the user to type "yes". Both tracks of documentation updated.

## v1.1.13 - April 06, 2026

Two-tier tool loading for the admin assistant.

### Added
- **Two-tier tool loading** — assistant tools are now split into core (always sent) and domain (loaded on demand). New conversations start with only the core tools, reducing token overhead. Domain tools are loaded by calling `load_tools(domain=...)` and persist in `conversation.metadata["active_domains"]`.
- **`load_tools` core tool** — the primary discovery and activation mechanism. Call with no arguments to list available domains; call with `domain` or `domains` to activate tools for the rest of the conversation.
- **`get_core_tools_for_user(user)`** — returns only tools with `core=True`, filtered by permission.
- **`get_domain_tools_for_user(user, domains)`** — returns tools for the specified domain(s), filtered by permission.
- **`get_available_domains(user)`** — returns a dict of domains the user can access, with tool count, description, and example tool names. Domains containing only core tools are excluded.
- **`core=True` flag on `@tool` and `register_tool()`** — marks a tool as always-on. Core tools: `load_tools`, `read_memory`, `write_memory`, `delete_memory`, `describe_model`, `query_model`, `read_docs`, `browse_url`, `query_logs`, `query_files`, `get_file`, `analyze_image`.

### Changed
- **Discovery tool domain reassignment** — `list_metric_categories` and `list_metric_slugs` moved to the `metrics` domain; `list_job_channels` moved to `jobs`; `list_event_categories` moved to `security`; `list_permissions` moved to `users`. These now load alongside the tools they support rather than always loading with the discovery domain.
- **`list_tools` is no longer a core tool** — it remains in the `discovery` domain and is available after calling `load_tools(domain="discovery")`.
- **Backward compatibility** — old conversations with `tool_use` blocks in their message history automatically fall back to receiving all tools, so no migration is required.
- **System prompt updated** — tool selection guidance replaced with tool loading guidance. The LLM is instructed to auto-load a domain when the user's request clearly maps to one.

## v1.1.12 - April 05, 2026

bugfix in how ipsets are handled


## v1.1.11 - April 05, 2026

new AI Dream support, firewall sync performance fix


### Changed
- **`sync_firewall` skips unchanged IPSets** — each enabled `IPSet` is now compared against a Redis-stored last-sync timestamp (`mojo:sync_firewall:last_sync`). Sets whose `modified` time is not newer than the last sync are skipped entirely. Permanent blocks (`mojo_blocked`) are similarly skipped if no `GeoLocatedIP` record has changed since the last sync. First run after deploy or reboot loads everything as before.
- **`ipset_load()` uses `ipset restore` with atomic swap** — bulk CIDR loading now pipes all entries to `sudo ipset restore` in a single subprocess call instead of spawning one process per CIDR. CIDRs are loaded into a `<name>_tmp` set and atomically swapped with the live set, so the live set is never empty during a reload. This reduces a 30-minute hourly sync (thousands of CIDRs) to under 30 seconds.

### Added
- **Assistant persistent memory** — three-tier memory system (global, user, group) stored in Redis. Memories are injected into the system prompt at conversation start. New LLM tools: `read_memory`, `write_memory`, `delete_memory` (all require `assistant` permission). New REST endpoints: `GET/POST/DELETE /api/assistant/memory`. Nightly cleanup job with mechanical phase (orphan removal, size enforcement) and LLM-assisted dreaming phase (consolidation, deduplication). When global memory is empty, an onboarding prompt guides the LLM to ask the user about their platform.
- **Assistant incident event reporting** — security-relevant assistant actions now emit incident events via `incident.report_event()`: permission denied (level 5-6), mutating tool execution (level 5), tool exceptions (level 6), agent crashes (level 7), API errors (level 5-7). Events flow through the rule engine for automated response.
- **`Conversation.group` FK** — conversations can now be linked to a group for group-tier memory injection. Pass `group_id` in the WebSocket `assistant_message` payload to create a group-scoped conversation.
- **Email template seed auto-load** — `EmailTemplate.get_or_load_from_seed(name)` checks the DB first; on a miss it auto-loads from `mojo/apps/aws/seeds/email_templates/{name}.json` and creates the record. Used by `send_template_email()` and `User.send_template_email()`. Add a seed JSON file and no manual DB setup is needed.
- **Auto-disable inactive users and groups** — opt-in nightly sweep (`inactive_sweep`) warns entities 7 days before the threshold and disables them after 90 days of inactivity. Controlled by `ACCOUNT_AUTO_DISABLE_ENABLED` / `GROUP_AUTO_DISABLE_ENABLED`. Superusers, staff, and entities with `metadata["protected"]["no_disable"] = True` are exempt. Warning and disable actions emit incident events.
- **`Group.get_protected_metadata()` / `Group.set_protected_metadata()`** — single-key helpers for `metadata["protected"]`, matching the same API already on `User`.
- **Assistant `query_logs` tool** — new Logs domain tool that queries the `logit.Log` audit trail. Filterable by time range, level, kind, model, user, IP, path, method, and free-text. Requires `view_logs` permission.
- **`account_inactive_warning` and `group_inactive_warning` email seed templates** — ship with default HTML/text content and auto-load on first send.

## v1.1.10 - April 03, 2026

ipset blocking now in place


## v1.1.10 - April 03, 2026

ipset-based permanent IP blocking, fleet reconciliation cron

### Added
- **ipset permanent block routing** — `GeoLocatedIP.block(ttl=None)` now routes through the `mojo_blocked` ipset instead of individual iptables rules. O(1) kernel lookup regardless of how many IPs are blocked. TTL blocks (`ttl > 0`) continue to use individual iptables rules.
- **`firewall.ipset_add(name, ip)` and `firewall.ipset_del(name, ip)`** — new single-IP ipset operations. Both are idempotent. `ipset_add` creates the set if it does not exist and ensures the iptables DROP rule is present.
- **`broadcast_ipset_add_blocked` / `broadcast_ipset_del_blocked`** — new broadcast job handlers that apply `ipset_add`/`ipset_del` on each fleet instance for real-time permanent block/unblock propagation.
- **`sync_firewall` cron job (hourly)** — rebuilds all ipsets from DB truth every hour. Restores permanent blocks after server restart (iptables/ipset state is lost on reboot) and reconciles fleet drift from missed broadcasts.
- **`FIREWALL_BLOCKED_IPSET_NAME` setting** — configures the ipset name for permanent blocks. Default: `"mojo_blocked"`.

### Changed
- **`sweep_expired_blocks` now runs every 5 minutes** (previously every minute). TTL blocks sitting an extra 4 minutes after expiry has no practical impact and reduces cron overhead 12x.
- **`unblock()` of a permanent block** now broadcasts `broadcast_ipset_del_blocked` instead of `broadcast_unblock_ip`, matching the new block routing.

## v1.1.9 - April 03, 2026

new AI Agent support, improved security

### Added
- **Incident delete-on-resolution** — RuleSets with `metadata.delete_on_resolution = True` now auto-delete their incidents the moment they transition to `resolved` or `closed`. Triggered from all resolution paths: REST saves, the BlockHandler, and the LLM agent. Keeps the database clean for high-volume noise patterns.
- **`metadata.do_not_delete` per-incident flag** — set `True` on any incident to exempt it from both delete-on-resolution and periodic pruning. Intended for confirmed serious threats requiring long-term retention.
- **`prune_incidents` async job** — periodic cron job that deletes resolved, closed, and ignored incidents older than `INCIDENT_PRUNE_DAYS` (default: 90 days). Skips incidents with `do_not_delete = True`.
- **`INCIDENT_PRUNE_DAYS` setting** — configures the age threshold for the `prune_incidents` job. Default: `90`.
- **LLM agent: `delete_on_resolution` param on `create_rule` tool** — agent can now propose noise-pattern rules with automatic cleanup enabled.
- **LLM agent: `do_not_delete` param on `update_incident` tool** — agent can protect serious incidents from auto-deletion when assessing confirmed threats.


## v1.1.8 - April 02, 2026

new assistant app, major cleanup in testit


## v1.1.8 - April 02, 2026

### Added
- **Parallel test execution (`-j N`)** — `bin/run_tests` now runs up to 3 modules in parallel by default using `ThreadPoolExecutor`. Set a specific thread count with `-j N`. Parallelism is forced to 1 when `-s`, `-v`, or `--continue` is active.
- **Rich progress UI** — when the `rich` package is installed and `-j > 1`, the runner displays a live per-module progress table. Use `--plain` to disable (e.g. in CI).
- **Agent mode (`--agent`)** — writes `var/test_failures.json` after the run with structured per-failure data: `test_source`, `file_path`, `line`, `traceback`, and the last 20 lines of the server error log. For use by LLM agents and CI pipelines.
- **`TESTIT` module config in `__init__.py`** — each test package can declare a `TESTIT` dict to control parallel behaviour (`serial`), app requirements (`requires_apps`), server settings (`server_settings`), and extra guards (`requires_extra`). Read via AST — no import side effects.
- **`opts.client.last_response`** — `RestClient` now captures every response as an `objict` with `method`, `path`, `status_code`, `body`, `headers`, and `elapsed_ms`. Available immediately after each request for test diagnostics.

### Changed
- **Thread-safe helpers** — `TEST_RUN` counters use a `Lock`; active test tracking uses `threading.local` so parallel modules do not overwrite each other's state.
- **`jobs.publish()` channel fallback** — `jobs.publish()` no longer raises when a channel is not configured; it falls back gracefully.

## v1.1.7 - April 01, 2026

lot of cleanup


## v1.1.6 - April 01, 2026

bug fixes in security system


## v1.1.6 - April 01, 2026

### Added
- **LLM deep analysis for incidents** — new `analyze` POST_SAVE_ACTION on `Incident`. Admins can POST `{"action": "analyze"}` to `/api/incident/incident/<id>` to trigger an async agent that investigates the incident, merges related open incidents in the same category, and proposes a disabled RuleSet for human approval. Requires `manage_security` and `LLM_HANDLER_API_KEY`.
- **`execute_llm_analysis` job** — new job entry point (`mojo.apps.incident.handlers.llm_agent.execute_llm_analysis`) for the analysis agent. Uses `ANALYSIS_PROMPT` and a 14-tool set (12 base tools plus `merge_incidents` and `query_open_incidents`). Stores the agent's summary in `incident.metadata["llm_analysis"]["summary"]`.
- **`merge_incidents` LLM tool** — available in analysis mode only. Merges a list of source incidents into a target incident; enforces same-category and excludes resolved/ignored sources.
- **`query_open_incidents` LLM tool** — available in analysis mode only. Returns all `new`/`open`/`investigating` incidents, optionally filtered by category, with event counts.

## v1.1.5 - April 01, 2026

BUGFIX cron scheduler broken


## v1.1.4 - March 31, 2026

fixed logging bug, fixed iptables blocking


## v1.1.4 - March 31, 2026

### Changed
- **Logging convention enforced across framework** — all `import logging` / `logging.getLogger()` calls in `mojo/apps/` and `mojo/helpers/aws/` replaced with `from mojo.helpers import logit`. Ensures all framework logs route to `var/log/` files (`mojo.log`, `error.log`, `debug.log`), benefit from sensitive data masking, and appear in structured format. The only file permitted to import stdlib `logging` is `mojo/helpers/logit.py`. Rule added to `.claude/rules/core.md` to prevent regressions.
- **Broadcast job functions renamed** — `block_ip`, `unblock_ip`, `sync_ipset`, and `remove_ipset` in `mojo.apps.incident.asyncjobs` renamed to `broadcast_block_ip`, `broadcast_unblock_ip`, `broadcast_sync_ipset`, and `broadcast_remove_ipset`. The new names reflect that broadcast handlers receive a plain `dict` from pub/sub (not a `Job` instance). All callers updated.
- **`geo.block()` is now idempotent** — `GeoLocatedIP.block()` returns `True` immediately if the IP is already actively blocked, without re-broadcasting, incrementing `block_count`, or overwriting `blocked_reason`.
- **BlockHandler includes incident/event IDs in block reason** — `blocked_reason` now encodes the triggering incident and event for traceability (e.g. `auto:ruleset:incident:42:event:87`).
- **BlockHandler auto-resolves the incident** — after a successful block, the `block://` handler records a `handler:block` entry in `IncidentHistory` and sets the incident status to `resolved` (unless it is already `resolved` or `ignored`).

## v1.1.3 - March 31, 2026

bug fixes in new llm security agent, added anthropic requirement


## v1.1.2 - March 30, 2026

### Fixed
- **`execute_handler` in `mojo.apps.incident.handlers.event_handlers`** — function now correctly accepts a `Job` model instance and reads `job.payload`, matching the job engine's calling convention (`func(job)`). Previously it expected a plain dict, causing all incident handlers dispatched via the job queue to fail at runtime.
- **`execute_llm_handler` and `execute_llm_ticket_reply` in `mojo.apps.incident.handlers.llm_agent`** — same job signature bug: both functions now correctly accept a `Job` instance and read `job.payload`. Previously they crashed at runtime when dispatched via the job queue, preventing the LLM agent from ever running in production.

### Added
- **`th.run_pending_jobs(channel=None, status="pending")`** — New testit helper that executes pending jobs from the database using the real job engine calling convention (`func(job)`). No Redis or running engine required. Returns the count of jobs executed. Use this in tests instead of calling job functions directly with a dict — it exercises the full publish→dispatch pipeline and will catch job function signature mismatches.
- **Anthropic Python SDK** (`anthropic>=0.52.0`) — `_call_claude()` in the LLM agent now uses the official `anthropic` SDK instead of raw `httpx` calls. The SDK handles retries, error types, and API versioning. `LLM_HANDLER_API_KEY` and `LLM_HANDLER_MODEL` are now read at call time (via `settings.get()`) rather than at module import, so runtime settings changes take effect without a restart.
- **LLM agent mocked flow tests** — 4 tests in `tests/test_incident/llm_agent.py` covering the full `jobs.publish()` → `th.run_pending_jobs()` → mocked `_call_claude` → real tool dispatch → DB side effects pipeline.


## v1.1.1 - March 30, 2026

huge changes in how security is handled, better login tracking, better audit trails, more compliant for industry systems like health and kyc.


### Added
- **`UserLoginEvent`** — New model recording every successful login with denormalized geo data from `GeoLocatedIP`
  - Fields: `ip_address`, `country_code`, `region`, `city`, `latitude`, `longitude`, `source`, `user_agent_info`, `is_new_country`, `is_new_region`, `device`
  - Hooked into `jwt_login()` via `UserLoginEvent.track()` — all standard login paths are covered automatically
  - `is_new_country` / `is_new_region` flags for per-user anomaly detection
  - Per-country and per-region metrics recorded on each login (`login:country:*`, `login:region:*`, `login:new_country`, `login:new_region`)
- **New REST endpoints** (require `manage_users` + `security` + `users` permissions):
  - `GET /api/account/logins` — Paginated login history with filtering by user, country, region, anomaly flags, source, date range
  - `GET /api/account/logins/<pk>` — Single event detail
  - `GET /api/account/logins/summary` — System-wide aggregation by country (or region drill-down)
  - `GET /api/account/logins/user` — Per-user aggregation by country (or region drill-down), requires `user_id`
- **Settings**: `LOGIN_EVENT_TRACKING_ENABLED`, `LOGIN_EVENT_FLAG_NEW_COUNTRY`, `LOGIN_EVENT_FLAG_NEW_REGION` (all startup-time, default `True`)

## v1.0.59 - March 2026

## v1.0.80 - March 27, 2026

new security firewall with auto blocking and ipset support


## v1.0.80 - March 26, 2026

### Added
- **`mojo.apps.chat`** — New real-time chat app built on the realtime WebSocket system
  - Room types: direct (1:1 DM), group (invite-only), channel (public join/leave)
  - Models: ChatRoom, ChatMessage, ChatMembership, ChatReaction, ChatReadReceipt
  - WebSocket handler: send, edit, flag, react, typing indicators, read receipts
  - REST endpoints: room CRUD, membership, message history, DMs, unread counts
  - Per-room content rules: URL/media/phone restrictions, max length, rate limiting, disappearing messages
  - Content guard integration for moderation (block/warn)
  - Admin message flagging (hidden from view, preserved as evidence)
  - Full User/Group/Member permission integration (chat, manage_chat, moderate_chat)
  - Subscription auth via `on_realtime_can_subscribe` for `chat:` topics

## v1.0.79 - March 26, 2026

google oauth take 5


## v1.0.78 - March 26, 2026

google auth fix take 2


## v1.0.77 - March 26, 2026

bugfix google oauth


## v1.0.76 - March 24, 2026

new ip time


## v1.0.75 - March 22, 2026



## v1.0.74 - March 22, 2026

fixing status again where it probed falsely for cluster redis


## v1.0.73 - March 22, 2026

status command fix


## v1.0.72 - March 22, 2026

new bouncer security


## v1.0.72 - March 22, 2026

### New Feature: Bouncer — Server-Gated Bot Detection

Bots are blocked before they ever see the login form, field names, or auth API endpoint URLs.

- **Server-side gate**: Django pre-screens every request to the login page (IP, headers, GeoIP, device cookie) before rendering anything. Clearly-bot traffic receives a honeypot decoy page; suspicious traffic receives the challenge page; known-good devices (valid pass cookie) receive the login page directly.
- **Randomized challenge page**: 4 layout variants, 10 button label variants, per-render CSS nonce (class names change every render), randomized honeypot field name, randomized button movement seed. No stable CSS selectors or XPath across sessions — automation breaks.
- **HMAC-signed bouncer token**: IP-bound, device-bound, single-use Redis nonce, 15-minute TTL, page_type-scoped. Attached to every auth API call by `mojo-auth.js`; validated server-side by `@md.requires_bouncer_token('login')` on the login endpoint.
- **HttpOnly pass cookie**: Set on allow/monitor decisions. Lets known-good devices skip the interactive challenge for 24h without bypassing IP/header scoring.
- **Adaptive bot signature learning**: After every high-confidence block (`risk_score >= BOUNCER_LEARN_MIN_SCORE`), `BotLearner` registers the bot's subnet /24, user agent, browser fingerprint, and signal-set campaign hash in `BotSignature`. Redis cache checked at pre-screen — matched signatures serve decoy immediately, before any scoring runs.
- **Pluggable scoring**: `register_analyzer` decorator, settings-driven `BOUNCER_SCORE_WEIGHTS` and `BOUNCER_THRESHOLDS`, per-page-type threshold overrides. Custom analyzers drop in without touching framework code.
- **New models**: `BouncerDevice` (pre-auth device reputation), `BouncerSignal` (assess/event audit log), `BotSignature` (adaptive learning registry). Full REST CRUD via operator portal.
- **Decoy honeypot**: `/login` and `/signin` serve a visually identical login page that POSTs to a dead endpoint returning plausible errors with a 300ms delay. Detection is never revealed.
- **Gradual rollout**: `BOUNCER_REQUIRE_TOKEN=False` (default) logs missing tokens without blocking. Flip to `True` to enforce. Per-group opt-in via `group.metadata["require_bouncer_token"]`.
- **Default branding** on the challenge page: dark navy gradient, `#6384ff` indigo, animated scan line, pulse rings — opt-in override per group. Login page branding (`BOUNCER_LOGO_URL`, `BOUNCER_ACCENT_COLOR`) is configurable.
- All features opt-in via settings. Existing projects are unaffected.
- Docs: `docs/django_developer/account/bouncer.md`, `docs/web_developer/account/bouncer.md`

## v1.0.71 - March 21, 2026

take 36, apple oauth in prod


## v1.0.70 - March 21, 2026

apple oauth fix take 35


## v1.0.69 - March 21, 2026

fix in correct inclusion of postgres binary


## v1.0.68 - March 21, 2026

another fix for apple oauth


## v1.0.67 - March 21, 2026

bump


## v1.0.66 - March 21, 2026

bump


## v1.0.65 - March 21, 2026

again


## v1.0.61 - March 21, 2026

testing new deploy process


## v1.0.60 - March 18, 2026

MAJOR FIX, dont allow db settings until django ready


## v1.0.59 - March 18, 2026

hot fix for db settigns issues


## v1.0.58 - March 18, 2026

new dynamic settings that do not read from db at import


### Improvements

- settings/runtime: completed broad settings hardening pass to avoid frozen import-time settings in runtime paths (jobs engine/scheduler/public API, logging middleware, incident/log async jobs, incident metrics, account activity/permission/geolocation/notification model knobs, serializer datetime mode, geoip providers/config, OpenAPI prefix). Runtime behavior now reads via `mojo.helpers.settings.settings` at call-time in these modules.

### Docs

- docs: added `docs/django_developer/helpers/settings_reference.md` — names-only framework settings key reference generated from framework usage; includes startup/bootstrap keys (restart required) and runtime keys. Updated `docs/django_developer/helpers/README.md`, `docs/django_developer/helpers/settings.md`, `docs/django_developer/README.md`, and `mkdocs.yml` nav.

## v1.0.57 - March 17, 2026

new db redis backed secure django settings
new apple oauth


### Docs

- docs: added `docs/web_developer/account/admin_portal.md` — admin-portal integration guide for REST APIs (auth flow, permission model, group context, common admin endpoints, and secure settings API usage via `/api/settings`); updated `docs/web_developer/account/README.md` and `mkdocs.yml` nav

## v1.0.56 - March 17, 2026

bug fixes for memeber invites


### New Features

- account: added Apple Sign In OAuth provider (`services/oauth/apple.py`); ES256 client_secret JWT generated per-request from `APPLE_CLIENT_ID`, `APPLE_TEAM_ID`, `APPLE_KEY_ID`, `APPLE_PRIVATE_KEY`; profile extracted from `id_token` (no separate userinfo endpoint); frontend flow identical to Google — `GET /api/auth/oauth/apple/begin` + `POST /api/auth/oauth/apple/complete`

### Bug Fixes

- account: `POST /api/auth/password/reset/token` now accepts `iv:` (invite) tokens in addition to `pr:` (password reset) tokens; `iv:` path verifies via `verify_invite_token`, sets `is_email_verified=True`, and issues JWT
- account: extracted `User.check_password_strength(password)` from `set_new_password`; both token-based and code-based password reset endpoints now enforce strength requirements; `set_new_password` unchanged for CRUD flows
- account: `Member.send_invite()` now sends account-setup invite (with `iv:` token link) when `user.last_login is None`; existing users continue to receive the `group_invite` notification

## v1.0.55 - March 16, 2026

BUG FIX for iv tokens used in password reset


## v1.0.54 - March 16, 2026

fix short link


## v1.0.53 - March 16, 2026

greatly improved invite flow


## v1.0.52 - March 16, 2026

BUGFIX passkeys required username


## v1.0.51 - March 15, 2026

pretty big changes for security


### New Features

- account: added `UserAPIKey` model — user-level long-lived JWT tokens tracked in the database with a per-key signing secret stored in `mojo_secrets`; each token carries `token_type="user_api_key"` and `jti` in the payload, linking it to the `UserAPIKey` record; revocation (`POST /api/account/api_keys/<id>` with `{"revoke": ...}`) rotates the per-key secret and sets `is_active=False`, immediately rejecting the token without affecting the user's session or other keys; `label` and `allowed_ips` are optional; generate logic and `validate_jwt` branch live entirely in `UserAPIKey` and `User` respectively — no coupling to `User.auth_key`; `POST /api/auth/generate_api_key` creates a key and returns the token once; `GET /api/account/api_keys` lists the owner's keys; incidents logged on generate (`api_key:generated`) and revoke (`api_key:revoked`) via `user.log()` (`mojo/apps/account/models/user_api_key.py`, `mojo/apps/account/rest/user_api_key.py`, `mojo/apps/account/models/user.py`, `mojo/apps/account/utils/jwtoken.py`)
- account: `allowed_ips` is now optional on `POST /api/auth/generate_api_key` and `POST /api/auth/manage/generate_api_key` — omitting it (or passing an empty list) creates an unrestricted token; IP restriction remains enforced when the list is non-empty (`mojo/apps/account/rest/user.py`)
- account: added `dob` (DateField) and `is_dob_verified` (BooleanField) to User model — `dob` is user-writable, `is_dob_verified` is system-only (in `NO_SAVE_FIELDS`, never REST-writable); changing `dob` automatically resets `is_dob_verified = False`; both fields cleared by `pii_anonymize()`; added `get_age()` helper that returns current age in whole years; `is_dob_verified` also in `SUPERUSER_ONLY_FIELDS` so only superusers can set it via direct model save (`mojo/apps/account/models/user.py`)
- account: `current_password` is now **optional** on `POST /api/auth/email/change/request` and `POST /api/auth/phone/change/request` — if provided it is still validated (wrong password → 401), but omitting it allows OAuth-only and passkey-only users (no usable password) to use the change flows; a notification is always sent to the **old** email/phone alerting the account owner of the request; phone change now sends an SMS to the current number when one is on file (`mojo/apps/account/rest/user.py`)
- aws: CloudWatch `fetch()` response shape now matches the metrics app — `data` is a `{slug: [values]}` dict and the timestamp axis is returned as `labels` (was `[{slug, values}]` list with `periods` key); all callers updated accordingly (`mojo/helpers/aws/cloudwatch.py`)
- aws: CloudWatch fetch endpoint now accepts `dr_start`/`dr_end` Unix timestamp params (aliases for `dt_start`/`dt_end`); all datetime inputs are normalized to UTC-aware via `mojo.helpers.dates.parse_datetime` before use — eliminates offset-naive/offset-aware comparison errors (`mojo/apps/aws/rest/cloudwatch.py`, `mojo/helpers/aws/cloudwatch.py`)

### Docs

- docs: updated `docs/web_developer/aws/cloudwatch.md` — response shape updated to `{data: {slug: [values]}, labels: [...]}`, `periods` → `labels` throughout, `dr_start`/`dr_end` documented as preferred time-range params

---

## v1.0.58

- account: added notification preferences endpoints — `GET /api/account/notification/preferences` and `POST /api/account/notification/preferences` let users control which notification kinds they receive on which channels (in-app, email, push); default is allow, only suppress on explicit opt-out; preferences stored in `user.metadata["notification_preferences"]` — no migration required (`mojo/apps/account/rest/notification_prefs.py`, `mojo/apps/account/services/notification_prefs.py`)
- account: wired notification preference enforcement into all three delivery paths — `Notification.send()` checks `in_app` channel, `send_template_email()` checks `email` channel when `kind=` is passed, `push_notification()` checks `push` channel when `kind=` is passed; system/transactional emails (password reset, verification, magic login, deactivation) never pass `kind` and are therefore never suppressed (`mojo/apps/account/models/notification.py`, `mojo/apps/account/models/user.py`)
- account: added TOTP recovery codes — 8 single-use `xxxx-xxxx-xxxx` hex codes generated on TOTP confirm, bcrypt-hashed and stored in `UserTOTP.mojo_secrets`; `GET /api/account/totp/recovery-codes` returns masked codes; `POST /api/account/totp/recovery-codes/regenerate` requires live TOTP code; `POST /api/auth/totp/recover` consumes `mfa_token` + `recovery_code` to issue JWT; warning notification sent when last code consumed (`mojo/apps/account/rest/totp.py`, `mojo/apps/account/models/totp.py`)
- account: added self-service username change — `POST /api/auth/username/change` requires `current_password`; validates via `content_guard`, checks uniqueness, lowercases; OAuth-only accounts (no usable password) get 400; `ALLOW_USERNAME_CHANGE` setting (default `True`) (`mojo/apps/account/rest/user.py`)
- account: added session revoke / log-out-everywhere — `POST /api/auth/sessions/revoke` requires `current_password`, rotates `auth_key` to invalidate all active JWTs, returns fresh JWT for the calling session; rate-limited (5/IP/5min); incidents logged on success and failure (`mojo/apps/account/rest/user.py`)
- account: added self-service account deactivation — two-step email confirmation flow: `POST /api/account/deactivate` sends `dv:` token email (15-min TTL), `POST /api/account/deactivate/confirm` validates token and calls `pii_anonymize()`; `ALLOW_SELF_DEACTIVATION` setting (default `True`), `DEACTIVATE_TOKEN_TTL` setting (default 900); already-inactive is idempotent 200 (`mojo/apps/account/rest/user.py`, `mojo/apps/account/utils/tokens.py`)
- account: added security events log — `GET /api/account/security-events` returns auth-relevant audit events for the authenticated user from `incident.Event`; no special permission required; returns only `created`, `kind`, `summary`, `ip`; never exposes `details`, `title`, `metadata`; supports `size`, `dr_start`, `dr_end` params; hard cap 100 results (`mojo/apps/account/rest/user.py`)
- account: added OAuth connection management endpoints — `GET /api/account/oauth_connection` lists linked providers; custom `DELETE /api/account/oauth_connection/<id>` with lockout guard (blocks unlink when no usable password and last active connection); `manage_users` admins bypass the guard (`mojo/apps/account/rest/oauth.py`)

### Security / Bug Fixes

- account: OAuth user creation now calls `set_unusable_password()` on new users — previously left `password=""` which could technically pass `check_password("")` in edge cases; now Django's unusable password sentinel is correctly stored (`mojo/apps/account/rest/oauth.py`)

### Docs

- docs: updated `docs/web_developer/account/user_self_management.md` — added sections for Notification Preferences (11), Username Change (12), Linked OAuth Accounts (13), Account Deactivation (14), Security Events (15); updated quick reference table with all new endpoints; renumbered Files (16), Activity Log (17), QR Codes (18), Realtime Events (19)
- docs: updated `docs/web_developer/account/mfa_totp.md` — added recovery code sections for view, regenerate, and recovery login endpoints
- docs: updated `docs/web_developer/account/oauth.md` — added Managing Connections section with list and unlink endpoints

---

## v1.0.57

### Security / Bug Fixes

- account: OAuth auto-link by email now sets `is_email_verified = True` on the matched user if it was not already set — the provider has confirmed ownership of the address, so no separate verification step is needed (`mojo/apps/account/rest/oauth.py`)

### Docs

- docs: added `docs/django_developer/account/oauth.md` — new Django developer reference covering required settings, `OAuthConnection` model, auto-link logic, email verification behaviour, MFA bypass rationale, adding new providers, CSRF state token design, and security notes
- docs: updated `docs/web_developer/account/oauth.md` — documented email verification on auto-link, added Security Behaviour section covering email verification gate interaction and MFA bypass rationale, added optional settings table
- docs: updated `docs/django_developer/account/README.md` — added OAuth entry to index

---

## v1.0.56

### New Features

- account: added `method` param to `POST /api/auth/verify/email/send` — pass `{ "method": "code" }` to send a 6-digit OTP to the user's inbox instead of a verification link; default `"link"` is fully backward-compatible (`mojo/apps/account/rest/verify.py`)
- account: added `POST /api/auth/verify/email/confirm` — authenticated endpoint to confirm email ownership by submitting the 6-digit OTP; mirrors `POST /api/auth/verify/phone/confirm` exactly; sets `is_email_verified=True` and emits `account:email:verified` realtime event (`mojo/apps/account/rest/verify.py`)
- account: added `method` param to `POST /api/auth/email/change/request` — pass `{ "method": "code" }` to send a 6-digit OTP to the new address instead of a confirmation link; default `"link"` is fully backward-compatible (`mojo/apps/account/rest/user.py`)
- account: extended `POST /api/auth/email/change/confirm` — now accepts `{ "code": "123456" }` (requires authentication) alongside the existing `{ "token": "ec:..." }` (unauthenticated, token is the credential); both paths commit the change, rotate `auth_key`, and return a fresh JWT (`mojo/apps/account/rest/user.py`)
- account: updated `POST /api/auth/email/change/cancel` — now clears both link-flow JTI and code-flow OTP in a single call, regardless of which method was used to initiate the change (`mojo/apps/account/rest/user.py`)
- account: added `generate_email_verify_code()` and `verify_email_verify_code()` to token infrastructure — 6-digit OTP stored in `mojo_secrets`, TTL controlled by `EMAIL_VERIFY_CODE_TTL` (default 10 min), single-use (`mojo/apps/account/utils/tokens.py`)
- account: added `generate_email_change_otp()` and `verify_email_change_otp()` to token infrastructure — 6-digit OTP stored in `mojo_secrets`, TTL controlled by `EMAIL_CHANGE_CODE_TTL` (default 10 min), single-use; mutually exclusive with the `ec:` link token so both paths cannot be active simultaneously (`mojo/apps/account/utils/tokens.py`)

### Docs

- docs: updated `docs/web_developer/account/email_verification.md` — added code flow section for `POST /api/auth/verify/email/send` and `POST /api/auth/verify/email/confirm`; updated write-protection table; added `EMAIL_VERIFY_CODE_TTL` to settings reference; updated realtime events section
- docs: updated `docs/web_developer/account/email_change.md` — added code flow for request and confirm; restructured confirm into Option A (code), Option B (link→API page), Option C (link→frontend); updated cancel, security notes, template requirements, and settings reference; added `email_change_code` template docs
- docs: updated `docs/django_developer/account/email_change.md` — added code flow token infrastructure reference; updated endpoint table; added confirm routing logic; documented `email_change_code` template; added cancel internals section; added settings reference table; expanded security design notes

## v1.0.55


### New Features

- account: added `GET /api/auth/email/change/confirm` — browser-friendly confirm endpoint for email change links; renders `account/email_change_confirm.html` on success or error; supports `?redirect=<url>` param for automatic redirect after 3 seconds on success (`mojo/apps/account/rest/user.py`, `mojo/apps/account/templates/account/email_change_confirm.html`)
- account: upgraded `GET /api/auth/verify/email/confirm` — now renders `account/email_verify_confirm.html` instead of returning JSON; supports `?redirect=<url>` param; handles error states (invalid token, disabled account) with descriptive template pages (`mojo/apps/account/rest/verify.py`, `mojo/apps/account/templates/account/email_verify_confirm.html`)
- account: added realtime WebSocket event `account:email:changed` — emitted to all active sessions after a successful email change confirm (both GET and POST paths); allows open sessions to react to the `auth_key` rotation cleanly (`mojo/apps/account/rest/user.py`)
- account: added realtime WebSocket event `account:email:verified` — emitted after `GET /api/auth/verify/email/confirm` succeeds (`mojo/apps/account/rest/verify.py`)
- account: added realtime WebSocket event `account:phone:verified` — emitted after `POST /api/auth/verify/phone/confirm` succeeds (`mojo/apps/account/rest/verify.py`)
- account: added `POST /api/auth/phone/change/request` — begin a self-service phone number change; requires `current_password`; sends a 6-digit OTP via SMS to the new number (`mojo/apps/account/rest/user.py`)
- account: added `POST /api/auth/phone/change/confirm` — commit a phone number change by submitting the session token and OTP; sets `is_phone_verified=True` on success (`mojo/apps/account/rest/user.py`)
- account: added `POST /api/auth/phone/change/cancel` — cancel a pending phone number change immediately; idempotent (`mojo/apps/account/rest/user.py`)
- account: added `KIND_PHONE_CHANGE` (`pc:`) token kind to the token infrastructure with `generate_phone_change_token()` and `verify_phone_change_token()`; TTL defaults to 10 minutes (`mojo/apps/account/utils/tokens.py`)

### Security / Bug Fixes

- account: `on_rest_pre_save` now normalizes and uniqueness-checks `phone_number` on every REST save, and resets `is_phone_verified=False` whenever the phone number changes — previously the verified flag was not cleared on a direct phone number update (`mojo/apps/account/models/user.py`)
- account: `_handle_existing_user_pre_save` now blocks direct REST replacement of an existing phone number for non-superusers — must use the `auth/phone/change/*` flow to prove ownership of the new number before it is committed (`mojo/apps/account/models/user.py`)

### Docs

- docs: added `docs/web_developer/account/phone_change.md` — full REST API reference for the phone number change flow
- docs: updated `docs/web_developer/account/README.md` — added Phone Number Change link
- docs: updated `docs/web_developer/account/email_verification.md` — added Realtime Events section, Template Customisation section, and cross-reference to phone_change.md
- docs: updated `docs/web_developer/account/email_change.md` — documented GET confirm endpoint, Option A/B integration patterns, redirect param, Realtime Events section, and Template Customisation section

## v1.0.50 - March 15, 2026

fix local dev bugs for passkeys and uploads
added list graph for user


## v1.0.49 - March 14, 2026

support to get runners sysinfo


### New Features

- jobs: added `jobs.get_sysinfo(runner_id=None, timeout=5.0)` — collects live host system info (OS, CPU, memory, disk, network) from one or all active runners via the existing `broadcast_execute`/`execute_on_runner` control channel; always returns a list of reply dicts (`mojo/apps/jobs/__init__.py`, `mojo/apps/jobs/services/sysinfo_task.py`)
- jobs: added `GET /api/jobs/runners/sysinfo` — REST endpoint returning sysinfo from all active runners; accepts optional `timeout` query param (`mojo/apps/jobs/rest/jobs.py`)
- jobs: added `GET /api/jobs/runners/sysinfo/<runner_id>` — REST endpoint returning sysinfo for a specific runner; returns 404 when the runner does not respond (`mojo/apps/jobs/rest/jobs.py`)

### Tests

- tests: added `tests/test_jobs/test_sysinfo.py` — permission guard tests (always run), Python API shape tests, and live-runner tests (skipped via `TestitSkip` when no runners are active)

### Docs

- docs: updated `docs/django_developer/jobs/README.md` — added Runner Sysinfo section covering `get_sysinfo()` usage, return shape, and `psutil` requirement
- docs: updated `docs/web_developer/jobs/jobs.md` — added Runner Sysinfo section covering both REST endpoints, response shapes, and error reply format

## v1.0.48 - March 14, 2026

new aws metrics support


### Improvements

- aws: added `memory` category for EC2 — fetches `mem_used_percent` from the `CWAgent` namespace (requires the CloudWatch Agent installed on the instance; instances without the agent return all-zero values) (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `disk` category for EC2 — fetches `disk_used_percent` from the `CWAgent` namespace, targeting the root filesystem (`path="/"`); rounds out the three core utilisation metrics alongside `cpu` and `memory` (requires the CloudWatch Agent; instances without the agent return all-zero values) (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `CATEGORY_NAMESPACE_OVERRIDE` table — maps `(account, category)` pairs that require a non-default CloudWatch namespace; used as the extension point for any future categories that live outside their account's primary namespace (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `CATEGORY_EXTRA_DIMENSIONS` table — maps `(account, category)` pairs that require additional fixed dimensions beyond the primary instance dimension (e.g. `disk` requires `path="/"` to target the root filesystem); appended automatically inside `fetch()` (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `resolve_namespace(account, category)` helper — returns the correct CloudWatch namespace for a given account/category pair, consulting `CATEGORY_NAMESPACE_OVERRIDE` before falling back to `ACCOUNT_NAMESPACE`; `fetch()` now calls this instead of the bare `ACCOUNT_NAMESPACE` lookup (`mojo/helpers/aws/cloudwatch.py`)

### Bug Fixes

- aws: fixed CloudWatch `_fetch_values` returning all-zero values for every metric on live systems — two root causes (`mojo/helpers/aws/cloudwatch.py`):
  1. **Timezone mismatch**: boto3 returns CloudWatch `Timestamp` values as timezone-aware datetimes (`tzlocal()`); bucket keys were naive UTC. Added `replace(tzinfo=None)` to strip timezone before key lookup.
  2. **Period offset mismatch**: CloudWatch returns datapoints at an internal offset (e.g. `:17` past the hour) rather than on clean period boundaries. A plain `replace(second=0, microsecond=0)` was not sufficient — timestamps are now floored to the period boundary using `_align_to_period()` before being used as dict keys, matching how `_build_buckets` constructs the bucket list.

### Tests

- tests: added `cw_fetch_ec2_memory` — verifies the `memory` category returns a valid `200` response with correct shape; non-zero assertion is conditional on the CloudWatch Agent being present (all-zero is legitimate when the agent is not installed) (`tests/test_aws/cloudwatch.py`)
- tests: added `cw_fetch_ec2_disk` — verifies the `disk` category returns a valid `200` response with correct shape; same conditional non-zero pattern as memory (`tests/test_aws/cloudwatch.py`)

### Docs

- docs: updated `docs/django_developer/aws/cloudwatch.md` — added `memory` to category table with CWAgent footnote, documented `CATEGORY_NAMESPACE_OVERRIDE` and `resolve_namespace()` under a new Namespace Resolution section, updated module-level helper examples
- docs: updated `docs/web_developer/aws/cloudwatch.md` — added `memory` to EC2-only category table with CWAgent footnote and install link

---

## v1.0.54

### Improvements

- aws: CloudWatch `fetch()` now resolves friendly names for chart slugs — EC2 instances use their `Name` tag value (e.g. `"web-server-1"`) instead of the raw AWS instance ID (e.g. `"i-0abc1234"`); RDS and ElastiCache identifiers are already human-readable and are used as-is (`mojo/helpers/aws/cloudwatch.py`)
- aws: `fetch()` `slugs` input parameter now accepts either friendly names or raw AWS IDs — both are resolved to the underlying instance ID before the CloudWatch call is made (`mojo/helpers/aws/cloudwatch.py`)
- aws: added `CloudWatchHelper.list_resource_slugs(account)` — returns `[{id, slug}]` for a given account type; used internally by `fetch()` for id↔slug mapping and available directly for callers that need to enumerate resources with their display names (`mojo/helpers/aws/cloudwatch.py`)
- aws: `GET /api/aws/cloudwatch/resources` now includes a `slug` field on every resource entry — the same friendly name that will appear in chart labels; use `slug` (not `id`) as input to the `fetch` endpoint's `slugs` parameter (`mojo/apps/aws/rest/cloudwatch.py`)

### Tests

- tests: updated `cw_resources_list` — asserts that each resource entry now includes a non-empty `slug` field; stashes both `ec2_id` (raw AWS ID) and `ec2_slug` (friendly name) for downstream tests (`tests/test_aws/cloudwatch.py`)
- tests: updated single-slug and per-resource tests to pass the friendly slug (not the raw AWS ID) as the `slugs` parameter, matching production usage
- tests: added `cw_fetch_ec2_slug_is_name` — verifies end-to-end that when an EC2 instance has a `Name` tag the returned `slug` in the response matches the friendly name advertised by the `resources` endpoint, and does not look like a raw instance ID (`tests/test_aws/cloudwatch.py`)

### Docs

- docs: updated `docs/django_developer/aws/cloudwatch.md` — documented friendly-slug behavior in `fetch()`, updated examples to use friendly names, documented `list_resource_slugs()`, clarified `slugs` parameter accepts names or IDs
- docs: updated `docs/web_developer/aws/cloudwatch.md` — added friendly-name overview, updated `resources` response shape to show `slug` field, updated `fetch` query parameter description and all response examples

---

## v1.0.53

### Tests

- tests: `login_with_phone_e164`, `login_with_phone_unformatted`, and `login_with_phone_wrong_password` now raise `TestitSkip` when `ALLOW_PHONE_LOGIN=False` on the server — these tests require phone-as-username login to be enabled and were failing unconditionally on servers where it is not (`tests/test_accounts/accounts.py`)

---

## v1.0.52

### Bug Fixes

- account: `REQUIRE_VERIFIED_EMAIL` gate was incorrectly blocking logins where the identifier was a plain **username** — the gate now only fires when `source == "email"` (i.e. the user submitted an email address as their login identifier). Username-based logins are never gated by email verification status (`mojo/apps/account/rest/user.py`)

### Tests

- tests: fixed `test_email_gate_blocks_unverified`, `test_email_gate_allows_verified`, and `test_email_gate_wrong_password_returns_401` — all three were posting with `username=TEST_USER` (a plain username), which would no longer trigger the gate; they now use the email address as the login identifier to correctly exercise the gate path
- tests: added `test_email_gate_does_not_block_username_login` — asserts that a user with an unverified email can still log in via username when `REQUIRE_VERIFIED_EMAIL=True`

### Docs

- docs: updated `docs/web_developer/account/email_verification.md` — clarified that `REQUIRE_VERIFIED_EMAIL` only gates email-identifier logins; username logins are not affected
- docs: updated `docs/web_developer/account/authentication.md` — added explicit callout that the email gate does not apply to plain username logins

---

## v1.0.51

### AWS CloudWatch Monitoring

- aws: added `CloudWatchHelper` in `mojo/helpers/aws/cloudwatch.py` — boto3 wrapper for fetching live time-series metrics from CloudWatch for EC2 instances (`AWS/EC2`), RDS DB instances (`AWS/RDS`), and ElastiCache clusters (`AWS/ElastiCache`)
- aws: high-level `CloudWatchHelper.fetch(account, category, slugs, ...)` mirrors the metrics app API exactly — same `account`/`category`/`slugs` parameters, same `periods` + `data` response shape; existing frontend chart components work without modification
- aws: when `slugs` is omitted, all instances for the account type are discovered automatically via `list_instance_ids(account)` — no need to specify IDs for the common case
- aws: mapping tables in `cloudwatch.py` — `ACCOUNT_NAMESPACE`, `ACCOUNT_DIMENSION`, `CATEGORY_METRIC`, `GRANULARITY_SECONDS`, `STAT_MAP` — drive all account/category/granularity resolution; invalid combos raise `ValueError` (REST layer converts to `400`)
- aws: two REST endpoints under `manage_aws` permission (`mojo/apps/aws/rest/cloudwatch.py`):
  - `GET /api/aws/cloudwatch/resources` — list EC2, RDS, and ElastiCache resource IDs (use as `slugs`)
  - `GET /api/aws/cloudwatch/fetch` — time-series metric data; params: `account`, `category`, `slugs` (optional), `dt_start`, `dt_end`, `granularity` (`minutes`/`hours`/`days`), `stat` (`avg`/`max`/`min`/`sum`)
- aws: gap buckets (no CloudWatch datapoints) filled with `0.0` so `periods` and `values` are always the same length and cover the full requested range
- aws: `CloudWatchHelper` exported from `mojo/helpers/aws/__init__.py`
- docs: added `docs/django_developer/aws/cloudwatch.md` — helper usage, category reference table, IAM policy, and testing guide
- docs: added `docs/web_developer/aws/cloudwatch.md` — endpoint reference, category tables by account type, response shape, granularity guide, and error codes
- docs: updated both README indexes to include the new AWS CloudWatch section

### Tests

- tests: added `tests/test_aws/cloudwatch.py` — permission guard, missing-param, invalid account, invalid category, and wrong-account-for-category validation tests always run (no AWS credentials needed); live resource-list and metric fetch tests skip gracefully via `TestitSkip` when `AWS_KEY` is not configured on the server

---

## v1.0.50

### Bug Fixes

- rest: `on_rest_save_related_field` now calls `_set_field_change` before every `setattr` — FK assignments (e.g. `org`) previously never appeared in `changed_fields`, so guards like `MANAGE_USERS_ONLY_FIELDS` silently did nothing when a relation was set via a raw PK integer (`mojo/models/rest.py`)
- settings: `SettingsHelper.get()` now reads from the live `django.conf.settings` proxy on every call instead of caching `self.root` — the cached reference went stale under Django's `override_settings`, causing settings changes to be ignored (`mojo/helpers/settings/helper.py`)
- account: `on_email_change_request` now reads `ALLOW_EMAIL_CHANGE` at call time via `settings.get()` instead of using the module-level constant frozen at import time (`mojo/apps/account/rest/user.py`)

### Phone Gate

- account: confirmed `REQUIRE_VERIFIED_PHONE` gate applies symmetrically to password login when the identifier is a phone number (`ALLOW_PHONE_LOGIN=True`) — `lookup_from_request_with_source` returns `source="phone_number"` which flows into `_check_verification_gate` via `jwt_login`; no code change was required, only test coverage was missing

### Phone Verification Endpoints

- account: `POST /api/auth/verify/phone/send` — authenticated; sends a 6-digit OTP to the user's `phone_number` on file; no-ops with 200 if already verified; returns 400 if no phone number is set (`mojo/apps/account/rest/verify.py`)
- account: `POST /api/auth/verify/phone/confirm` — authenticated; submits the 6-digit code to set `is_phone_verified=True`; code is single-use and expires after `PHONE_VERIFY_CODE_TTL` seconds (default 10 min); does not issue a new JWT (`mojo/apps/account/rest/verify.py`)
- tokens: `generate_phone_verify_code(user)` / `verify_phone_verify_code(user, code)` — stores code + timestamp in user secrets, same pattern as SMS OTP (`mojo/apps/account/utils/tokens.py`)
- docs: updated `docs/web_developer/account/email_verification.md` — replaced "coming soon" note with full endpoint reference, added `PHONE_VERIFY_CODE_TTL` to settings table, updated write-protection table with the new confirm endpoint

### Email Template Seeds

- aws: added seed JSON files for all account email templates — `email_verify.json`, `email_verify_link.json`, `email_change_confirm.json`, `email_change_notify.json` (`mojo/apps/aws/seeds/email_templates/`)
- aws: `seed_email_templates` command skips existing records by default; `--update-existing` is an explicit opt-in — safe to re-run at any time

### Tests

- tests: fixed `test_accounts.verification` and `test_accounts.email_change` suites — all tests now pass
- tests: gate tests (`REQUIRE_VERIFIED_EMAIL`, `REQUIRE_VERIFIED_PHONE`, `ALLOW_PHONE_LOGIN`, `ALLOW_EMAIL_CHANGE`) now read the live server setting and raise `TestitSkip` with a descriptive message when the required setting is not active — `override_settings` has no effect across the testit process boundary
- tests: added three new phone-gate tests covering the password-login-via-phone-identifier path (off by default, blocks unverified, allows verified)
- tests: fixed email change REST tests — replaced invalid `user_id=opts.user_id` kwarg pattern (silently ignored by `requests`) with explicit `opts.client.login()` / `opts.client.logout()` calls
- tests: fixed `resp.json()` → `resp.json` throughout email change tests — `json` is a plain `objict` attribute on the testit `RestClient` response, not a callable
- tests: write-protect and field-protect test actors now created with `is_email_verified=True` in setup; individual tests ensure the target user's `is_email_verified` is restored before each login so the email gate does not block test logins when `REQUIRE_VERIFIED_EMAIL=True` is active
- tests: `sms auto-verify` standalone test now skips when `REQUIRE_VERIFIED_PHONE=True` — the gate correctly fires before auto-verify in that configuration, and the gate behavior is already covered by the dedicated gate tests

## v1.0.49

### Self-Service Email Change

- account: added `POST /api/auth/email/change/request` — authenticated, password-confirmed request to change email; sends a confirmation link to the new address and a notification to the old address; current email is unchanged until confirmed
- account: added `POST /api/auth/email/change/confirm` — public token-exchange endpoint; commits new email, sets `is_email_verified=True`, rotates `auth_key` (invalidates all prior sessions), and issues a fresh JWT in one step
- account: added `POST /api/auth/email/change/cancel` — authenticated cancel; clears `pending_email` and nulls the stored `ec:` JTI so the outstanding link is dead immediately, before its 1-hour TTL expires; idempotent (no-op when no change is pending)
- account: `username` is automatically mirrored to the new email address on confirm when it matched the old email address
- account: email availability is re-checked at confirm time to guard against the race where another account registers the target address in the 1-hour window
- tokens: added `KIND_EMAIL_CHANGE = "ec"` token (1-hour TTL, configurable via `EMAIL_CHANGE_TOKEN_TTL`) with `generate_email_change_token(user, new_email)` / `verify_email_change_token(token)` — stores `pending_email` in user secrets alongside the JTI (same pattern as `magic_login_channel`)
- account: added `ALLOW_EMAIL_CHANGE` setting (default `True`) — set to `False` to disable the entire self-service email change flow; request endpoint returns 403 when disabled

### Tests

- tests: added `tests/test_accounts/email_change.py` — token unit tests (prefix, pending_email storage, verify tuple return, single-use, kind-mismatch rejection, expiry, auth-key rotation, re-request invalidation, garbage rejection) and REST endpoint tests for all three endpoints (request happy path, auth required, wrong password, missing password, same-email, duplicate-email, invalid format, setting disabled, confirm commit, auth-key rotation, username mirroring, inactive user, race condition, token single-use, kind mismatch, cancel clears pending + JTI, cancel no-op, cancel-then-confirm rejected)

### Docs

- docs/web: added `docs/web_developer/account/email_change.md` — full reference for all three endpoints, recommended UI flow, security notes, and settings reference
- docs/web: `docs/web_developer/account/README.md` already lists the new doc (entry was pre-existing)

## v1.0.48

### Email & Phone Verification

- account: added `REQUIRE_VERIFIED_EMAIL` setting (default `False`) — blocks password/email-based logins until `is_email_verified=True`
- account: added `REQUIRE_VERIFIED_PHONE` setting (default `False`) — blocks SMS-based logins until `is_phone_verified=True`
- account: login gate returns structured `{"error": "email_not_verified"}` / `{"error": "phone_not_verified"}` 403 so clients can prompt appropriately rather than showing a generic error
- account: added `POST /api/auth/email/verify/send` — sends a verification link; anti-enumeration (always 200, inactive users silently ignored)
- account: added `POST /api/auth/email/verify` — exchanges `ev:` token, sets `is_email_verified=True`, issues JWT in one step
- account: added `POST /api/auth/invite/accept` — exchanges `iv:` invite token, sets `is_email_verified=True`, issues JWT
- tokens: added `KIND_INVITE = "iv"` token (7-day TTL, configurable via `INVITE_TOKEN_TTL`) with `generate_invite_token` / `verify_invite_token`
- account: `send_invite` now issues a purpose-specific `iv:` token instead of the legacy `pr:` password-reset alias
- account: added `User.lookup_from_request_with_source()` — returns `(user, source)` where source is `"email"`, `"phone_number"`, or `"username"`; used to select the correct verification gate at login
- sms: standalone SMS OTP verify (`/api/auth/sms/verify` without `mfa_token`) now auto-sets `is_phone_verified=True` on success — phone receipt proves ownership
- sms: MFA-step SMS verify does **not** auto-set `is_phone_verified` (completing your own 2FA is not a verification act)

### User Model Field Security

- account: `is_email_verified` and `is_phone_verified` are now superuser-only via REST (both create and update paths)
- account: `requires_mfa`, `last_activity`, `auth_key` added to superuser-only field guard
- account: `is_active` and `org` now require `manage_users` permission to write via REST; owners can no longer deactivate/reactivate their own accounts or self-assign an org
- account: `SUPERUSER_ONLY_FIELDS` and `MANAGE_USERS_ONLY_FIELDS` extracted to module-level `frozenset` constants for audibility
- account: removed `creds_changed` flag passed between `on_rest_pre_save` and `_handle_existing_user_pre_save`; logic now lives where it belongs

### Tests

- tests: added `tests/test_accounts/verification.py` covering token unit tests (prefix, single-use, expiry, auth-key rotation, resend invalidation, cross-user rejection), REST endpoint correctness and security, verification gate (email + phone), SMS auto-verify, and full field write-protection matrix for all newly protected fields

### Docs

- docs/web: added `docs/web_developer/account/email_verification.md` — full reference for verification endpoints, invite flow, phone verification, UI flow, and settings
- docs/web: updated `authentication.md` with Email Verification Gate section
- docs/web: updated `account/README.md` index with link to new verification doc

## v1.0.45 - March 14, 2026
## v1.0.47 - March 14, 2026

support for a user saving to /api/user/me


## v1.0.46 - March 14, 2026

user level security for metrics
improved shorten for bots


## v1.0.45 - March 14, 2026

fixing shortlink permissions



- docs/agenting: synchronized `Agent.md` and `CLAUDE.md` with current repo structure and rules
- prompts: expanded `prompts/planning.md` and `prompts/building.md` with explicit mode routing and preflight steps
- process: added mandatory new-thread startup protocol (read `Agent.md` + `CLAUDE.md`, then choose planning vs building mode)
- conventions: removed stale doc-path references and reinforced framework constraints (no migrations, no project-level test execution in this repo)
- process: restored `memory.md` as an explicit source of thread-to-thread context and added a repository memory template
- process: added memory hygiene rules to keep `memory.md` compact, pruned, and decision-focused
- process: aligned startup preflight across `Agent.md`, `CLAUDE.md`, and prompt modes to read `memory.md` before planning/building
- docs: linked root developer documentation tracks to each other for clearer source-of-truth navigation
- docs/auth: added explicit frontend token storage guidance (`localStorage`) and page-reload session validation/refresh flow to web authentication docs
- docs/web: added `frontend_starter.md` and linked it from web root/core docs for a single frontend bootstrap guide
- docs/shortlink: clarified owner permissions for shortlink CRUD endpoints and documented that click-history remains `manage_shortlinks` scoped
- shortlink: expanded bot user-agent detection to cover Apple Messages and major chat/mail preview clients (Signal, Teams/Outlook preview, Google Chat/Gmail preview, Yahoo Mail, Thunderbird, Spark, Notion, Linear, Zoom)
- tests: expanded shortlink bot detection/OG preview tests to cover new user-agent signatures and preserve browser redirect behavior
- shortlink/metrics: resolve now records global click metric only (removed per-source metric) and optionally records user-scoped per-link metrics when `track_clicks=True` and `user` is set
- metrics: `metrics.record()` now supports `expires_at` override and `disable_expiry` for per-call retention control
- tests/docs: added shortlink metric behavior tests and updated metrics/shortlink developer docs for new retention/account behavior
- metrics/security: unified account permission checks across metrics endpoints and added `user-<id>` account enforcement (self-access by default, deny other-user access)
- tests: added metrics API coverage for `user-<id>` account read/write permissions
- docs/web-shortlink: added explicit metrics retrieval guide for global and user-scoped shortlink analytics (`shortlink:click` and `sl:click:<code>`)
- tests: added metrics API coverage confirming `group-<id>` account permissions still work (authorized member allowed, outsider denied)
- docs/frontend: added incident/event reporting guidance for uncaught errors, promise rejections, and auth/session anomalies in frontend starter

## v0.1.3 - May 29, 2025
## v1.0.44 - March 14, 2026

new shortlink management


## v1.0.43 - March 13, 2026

new shortlink app for url shortening


## v1.0.43 - March 13, 2026

- NEW: shortlink app — URL shortener with OG previews, file linking, metrics, and opt-in click tracking
- shortlink: bot detection for rich link previews (Slack, Twitter, Facebook, WhatsApp, Android/iOS Messages)
- shortlink: async OG metadata scraping via jobs system
- shortlink: bot_passthrough flag to skip preview rendering for transactional links
- shortlink: is_protected flag to prevent auto-deletion by cleanup job
- shortlink: cron job to prune expired links after 7-day grace period


## v1.0.42 - March 13, 2026

fileman cleanup, bug fixes


## v1.0.42 - March 13, 2026

- fileman: full audit of REST API, docs, and tests; fixed backend path handling, rendition.get_setting, missing import, removed nonexistent is_upload_expired property
- account: add user.pii_anonymize() for GDPR right-to-erasure compliance
- magic login: SMS channel support via method=sms on /api/auth/magic/send; channel tracked in mojo_secrets and cleared after verify
- bugfix: MojoSecrets.refresh_from_db now clears _exposed_secrets cache to prevent stale reads after DB reload


## v1.0.41 - March 12, 2026

sms mappings


## v1.0.40 - March 12, 2026

support sms to fake numbers mappings


## v1.0.39 - March 12, 2026

new notification system made easy


## v1.0.38 - March 12, 2026

bug fix in refresh token not have correct expiry


## v1.0.37 - March 12, 2026

fixing bug in sms login, fixing bug in tests


## v1.0.36 - March 12, 2026

typo fix


## v1.0.35 - March 12, 2026

support username in sms login


## v1.0.34 - March 12, 2026

bug fixes, more security patches


## v1.0.33 - March 12, 2026

improve MFA support


## v1.0.32 - March 11, 2026

ability to login with phonenumber


## v1.0.31 - March 11, 2026

new rate limiting login


## v1.0.30 - March 11, 2026

proper phone hub endpoints


## v1.0.29 - March 11, 2026

making some common phone apis publlic


## v1.0.28 - March 08, 2026

don't save when only doing model actions


## v1.0.27 - March 08, 2026

fixing api key permission checks
fixing false test


## v1.0.26 - March 08, 2026

bugfix for metrics decorators


## v1.0.25 - March 07, 2026

streamlined response with simile dicts now


## v1.0.24 - March 04, 2026

NEW django cache support to deal with collisions using django-redis-cache


## v1.0.23 - March 03, 2026

save api keys
- Added first-party Django Redis cache backend: `mojo.cache.MojoRedisCache` (replaces `redis_cache.RedisCache` usage).
- Added migration docs for cache backend settings and dependency cleanup.


## v1.0.22 - March 03, 2026

New feature to send and wait for events to come back


## v1.0.21 - March 01, 2026

new content guard


## v1.0.20 - February 27, 2026

* superuser rightfully has all permissions


## v1.0.19 - February 26, 2026

new oauth flows


## v1.0.18 - February 24, 2026

- New API KEYs support, new rate limit decorators, and metrics decorators


## v1.0.17 - February 12, 2026

BUGFIX for OneToOne fields


## v1.0.16 - February 12, 2026

NEW FILEVAULT APP


## v1.0.15 - February 10, 2026

* ADDED auto email templates
* Cleanup of filemaner and is_public check


## v1.0.14 - February 07, 2026

* Major cleanup of domain utils


## v1.0.13 - February 01, 2026

* BUGFIX USPS requires caps on states


## v1.0.12 - February 01, 2026

* Fixing Phone lookup for international numbers


## v1.0.11 - February 01, 2026

* Improved API key access
* better docs for realtime and metricsw
* new improved ability to have absolute routing ie prefix with /
* major bug fix in cron parsing of multiple times
* new domain helper utility


## v1.0.10 - December 24, 2025

bug fix for issue when multiple people access IoT lock


## v1.0.9 - December 17, 2025

bug fix when using list helpers
allow incidents to ignore rules


## v1.0.8 - December 11, 2025

fix for iso format null


## v1.0.7 - December 09, 2025

fixing rule field to text


## v1.0.6 - December 06, 2025

fixing bug when lock syncs via realtime more then once


## v1.0.5 - December 06, 2025

fixing realtime debugging


## v1.0.4 - December 04, 2025

bug fix when using isnull=False


## v1.0.3 - December 03, 2025

bug fix in sync of metadata


## v1.0.2 - December 03, 2025

fixing bug in fetching category data


## v1.0.1 - December 03, 2025

we are ready for 1.0 release


## v0.1.141 - December 03, 2025

fixing bug in category


## v0.1.140 - December 03, 2025

* adding scope to security events


## v0.1.139 - December 03, 2025

fixing bug in calculating totals


## v0.1.138 - December 01, 2025

missing fileman migrations


## v0.1.137 - December 01, 2025

* improvements to file handling
* improvements to metrics labeling weekly


## v0.1.136 - November 25, 2025

BUGFIX search


## v0.1.135 - November 25, 2025

BUGFIX: membership not propogating


## v0.1.134 - November 23, 2025

* missing migration file


## v0.1.133 - November 23, 2025

BUGFIX in bundling incidents by rules


## v0.1.132 - November 21, 2025

BUGFIX is broadcast messages


## v0.1.131 - November 21, 2025

publish broadcast async


## v0.1.130 - November 21, 2025

Adding server name to incidents so cyber engine can do action on one server


## v0.1.129 - November 19, 2025

syntax error


## v0.1.128 - November 19, 2025

BUGFIX in permissions for member invites


## v0.1.127 - November 19, 2025

* BUGFIX tier level access for a platform vs kyc customer


## v0.1.126 - November 19, 2025

BUGFIX when publishing templates with non native types


## v0.1.125 - November 19, 2025

fixing issue when inviting kyc client vs customer


## v0.1.124 - November 18, 2025

HOTFIX cyber report downloads failing in csv format


## v0.1.123 - November 18, 2025

* ADDED logic for improved date handling in relation to government ids"


## v0.1.122 - November 17, 2025

* CRITICAL FIX in log permissions fail gracefully
* sysinfo in correct fields
* improved email template handling


## v0.1.120 - November 01, 2025

No auth required for address suggestions


## v0.1.119 - October 31, 2025

Updating geo location


## v0.1.118 - October 30, 2025

Another TYPO


## v0.1.117 - October 30, 2025

TYPO in fcm (push notifications)


## v0.1.116 - October 30, 2025

ability to log Push notifications for debugging


## v0.1.115 - October 28, 2025

New Phonehub, qrcode, improved testit


## v0.1.114 - October 26, 2025

Advanced Compliance features


## v0.1.113 - October 24, 2025

NEW phonehub which provide detailed compliance for phone numbers


## v0.1.112 - October 22, 2025

BUGFIX searching for group members


## v0.1.111 - October 22, 2025

bugfix allow user to subscribe to self


## v0.1.110 - October 21, 2025

more socket cleanup


## v0.1.109 - October 21, 2025

Custom FCM implementation to work around issues


## v0.1.108 - October 21, 2025

Cleanup of FCM


## v0.1.107 - October 21, 2025

* BUGFIXES in rules and events


## v0.1.105 - October 17, 2025

* New incident engine cleanup


## v0.1.104 - October 16, 2025

Missing key migrations


## v0.1.103 - October 16, 2025

HOTFIX raw json lists in posts not handled correctly


## v0.1.102 - October 15, 2025

Update geo ip for forensics


## v0.1.101 - October 15, 2025

Config to allow incident and rule deletion


## v0.1.100 - October 15, 2025

* Cleanup and debugging of rules and incidents


## v0.1.99 - October 15, 2025

HOTFIX - shared context bug with requests


## v0.1.98 - October 14, 2025

Invite tokens


## v0.1.97 - October 13, 2025

HOTFIX don't show protected fields in changes


## v0.1.96 - October 13, 2025

Invalidate user login tokens when after a TTL


## v0.1.95 - October 13, 2025

Fixing broken login flows


## v0.1.94 - October 11, 2025

BUGFIX automated email setup


## v0.1.93 - October 11, 2025

Fixing aws email auto config


## v0.1.92 - October 11, 2025

test fails to catch syntax error


## v0.1.91 - October 11, 2025

FIXING SES Audit


## v0.1.90 - October 11, 2025

BUGFIX filestore for each user + group


## v0.1.89 - October 11, 2025

BUGFIX filemanager creating empty


## v0.1.87 - October 11, 2025

fix user upload


## v0.1.86 - October 11, 2025

Fixing file uploads for group


## v0.1.85 - October 10, 2025

group support


## v0.1.84 - October 10, 2025

simple group data


## v0.1.83 - October 10, 2025

dump all even lists


## v0.1.82 - October 10, 2025

* LOGIT_DEBUG_ALL for all logging


## v0.1.81 - October 08, 2025

Better logging


## v0.1.80 - October 08, 2025

Bugfix non str id in redis pool


## v0.1.79 - October 08, 2025

BUGfix geolocated


## v0.1.78 - October 08, 2025

* BUGFIX geoip no provider


## v0.1.77 - October 06, 2025

Syntax error tests failed


## v0.1.76 - October 06, 2025

Fixing cloud messaging mobile registration


## v0.1.75 - October 06, 2025

legacy login support debug


## v0.1.74 - October 06, 2025

Legacy login


## v0.1.73 - October 06, 2025

HOTFIX channels package removal


## v0.1.72 - October 05, 2025

Robustness of redis pools


## v0.1.71 - October 05, 2025

FIXES in aws email sending


## v0.1.70 - October 05, 2025

ADDED missing stats helper


## v0.1.69 - October 05, 2025

* mroe debug


## v0.1.68 - October 05, 2025

* trying to fix cluster bug


## v0.1.67 - October 05, 2025

* Bug fix in redis cluster mode


## v0.1.66 - October 05, 2025

* Fixes to group level permissions


## v0.1.65 - October 03, 2025

* ADDED advanced permissions via group/child/parent chaining


## v0.1.64 - October 02, 2025

* Bug in managing group members


## v0.1.63 - October 01, 2025

* ADDED ticket status changes to notes


## v0.1.62 - September 30, 2025

* Ticket bug fix


## v0.1.61 - September 30, 2025

* Fixing int fields


## v0.1.60 - September 29, 2025

* FIX no more raising redis timeout in pools


## v0.1.59 - September 28, 2025

* Bug fixes in realtime


## v0.1.58 - September 28, 2025

* more realtime logic


## v0.1.57 - September 26, 2025

* Atomic save bug


## v0.1.56 - September 26, 2025

HOTFIX atomic commits


## v0.1.55 - September 25, 2025

BUGFIX checking group member permission


## v0.1.54 - September 25, 2025

ossec fixes


## v0.1.53 - September 25, 2025

debug ossec


## v0.1.52 - September 25, 2025

* FIX ossec alerts not parsing


## v0.1.51 - September 25, 2025

* FIX password without current password


## v0.1.50 - September 25, 2025

* realtime disconnect dead connections


## v0.1.49 - September 25, 2025

* REWRITE of realtime


## v0.1.47 - September 24, 2025

debug


## v0.1.46 - September 24, 2025

debug


## v0.1.45 - September 24, 2025

debug


## v0.1.44 - September 24, 2025

* more robust error handling on channels


## v0.1.43 - September 24, 2025

* debug


## v0.1.42 - September 24, 2025

* debugging channels


## v0.1.41 - September 24, 2025

* REALTIME support


## v0.1.40 - September 24, 2025

* ADDED Channels


## v0.1.39 - September 24, 2025

* CRITICAL FIX: potential credential leakage


## v0.1.38 - September 24, 2025

* Added ticket category


## v0.1.37 - September 23, 2025

* FIX job reaper falsely kill done jobs


## v0.1.36 - September 23, 2025

* fixing filtering on no related models


## v0.1.35 - September 23, 2025

* FIX cron scheduling


## v0.1.34 - September 22, 2025

* Fixed advanced filtering


## v0.1.33 - September 22, 2025

* Ticket bug fix


## v0.1.32 - September 21, 2025

* Added new auto security checks on rest end points


## v0.1.31 - September 18, 2025

* Last fix did not take


## v0.1.30 - September 18, 2025

* ANother bug fix in jobs claiming jobs it cannot run


## v0.1.29 - September 18, 2025

* BUGFIX infinite retries on import func errors


## v0.1.28 - September 18, 2025

* BUGFIX job select_for_update bug


## v0.1.27 - September 18, 2025

* Debugging for jobs engine


## v0.1.26 - September 17, 2025

* Minor fixes in metrics and activity tracking


## v0.1.25 - September 16, 2025

* Added: more helpers to testit
* Added: more logic for redis pool and "with syntax"


## v0.1.24 - September 12, 2025

* New status commands


## v0.1.23 - September 12, 2025

* BUGFIX saving metrics perms


## v0.1.22 - September 10, 2025

* FIX for serverless/clusters


## v0.1.21 - September 10, 2025

* More servless bug fixes


## v0.1.20 - September 10, 2025

* BUG fixing serverless valkey/redis


## v0.1.19 - September 09, 2025

* attempting to fix pipeline bugs


## v0.1.18 - September 09, 2025

fixing redis auth


## v0.1.17 - September 09, 2025

* Fix pyright auto importing wrong modules


## v0.1.16 - September 09, 2025



## v0.1.15 - September 09, 2025

  * Major cleanup and new features see docs


## v0.1.14 - July 08, 2025

  CLEANUP and UnitTests for tasks


## v0.1.13 - June 09, 2025

   ADDED fileman app, a complete filemanager for django with rendition support and multiple backends and renderers
   UPDATED simple serializer greatly improved and new advanced serializer with support for other output formats
   UPDATED incidents subsystem for handling system events, rules and incidents
   


## v0.1.10 - June 06, 2025

   CHANGED license from MIT to Apache 2.0
   ADDED to new fileman app with file storage
   ADDED new notify framework that support mail, sms, etc
   ADDED crypto support for hmac signing and verifying
   ADDED more tests
   NOTE framework is not ready for primetime yet, but soon


## v0.1.9 - June 04, 2025

   UPDATE moved mojo tests into mojo project root, but still require a django project to run
   FIXED crypto encrypt,decrypt, and hash with proper tests
   ADDED incident system for report events and having them trigger incidents, including rules engine
   ADDED MojoSecrets which allows storing of secret encrypted data into a model
   ADDED helper scripts for talking to godaddy api and automating SES setup
   ADDED new mail handling system (work in progress)


## v0.1.8 - June 01, 2025

  Updaing version info and tagging release


## v0.1.7 - June 01, 2025

   Updating version info and release


## v0.1.4 - May 30, 2025

  ADDED: lots of improvements to making metrics cleaner and passing all tests
  ADDED: mojo JsonResponse to use ujson and ability to add future logic for custom handling of certain data


## v0.1.3 - May 29, 2025

  ADDED support to ignore github release and use tags


## v0.1.3 - May 29, 2025

  ADDED: more robust publishing, including github releases



  CLEANUP: moved django apps into apps folder to be more readable
  ADDED: more utility functions and trying to use more builting functions and less custom
  ADDED: useragent parsing and remote ip
  ADDED: support for nested apps
  ADDED: version info to default api
  ADDED: testit support for django_unit_setup and django_unit_test in django env