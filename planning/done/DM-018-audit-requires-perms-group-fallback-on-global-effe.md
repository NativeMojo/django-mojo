---
# id is assigned by /scope on pickup — leave it blank
id: DM-018
type: bug
title: Audit requires_perms group-fallback on global-effect endpoints (privilege escalation class)
priority: P1
effort: M
owner: backend
opened: 2026-07-08
depends_on: []
related: [DM-017]
links: []
---

# Audit requires_perms group-fallback on global-effect endpoints

## What & Why

DM-017's post-build security review confirmed a privilege-escalation class:
`@md.requires_perms(...)` (`mojo/decorators/auth.py:14-54`) falls back to
`request.group.user_has_permission(request.user, perms, True)` when the user
lacks the permission globally (`REQUIRES_PERMS_IS_GROUP` default True,
`auth.py:11`), and `request.group` is resolved from a **client-supplied**
`"group": <id>` field (`mojo/decorators/http.py:74`, `auth.py:42-43`).
`GroupMember.add_permission`/`set_permissions` accept **arbitrary string
keys** (gate `MEMBER_PERMS_PROTECTION` defaults `{}`, `member.py:8-9`), so any
tenant/group admin can hand a teammate any permission name *scoped to their
own group* — and that grant satisfies `requires_perms` on ANY endpoint if the
caller just adds their own group id to the request.

That is correct for endpoints whose effect is actually scoped to
`request.group` (the framework's normal pattern), and a cross-tenant
escalation for endpoints whose effect is **global**. DM-017's geofence
config plane was fixed with a local `_requires_global_perms`
(`mojo/apps/account/rest/geofence.py:46-80` — global `User.permissions` /
superuser only, registered in SECURITY_REGISTRY with `global_only: True`).

The review flagged at least one other global-effect endpoint using the
fallback: `mojo/apps/jobs/rest/control.py:15-16` (global job-system control).
The full surface has NOT been audited.

## Acceptance Criteria

- [ ] Enumerate every `@md.requires_perms` / `@md.requires_group_perms` usage
      and classify each endpoint's effect: group-scoped (fallback correct) vs
      global (fallback = escalation).
- [ ] Global-effect endpoints stop honoring group-scoped grants — either
      reuse/centralize `_requires_global_perms` (promote it out of
      `rest/geofence.py` into `mojo/decorators/auth.py`, e.g.
      `requires_global_perms`) or an equivalent explicit check.
- [ ] Regression tests for each fixed endpoint mirroring
      `tests/test_geofence/config_plane.py::test_group_scoped_perm_cannot_touch_global_config`.
- [ ] Consider whether `MEMBER_PERMS_PROTECTION` should ship a non-empty
      default (allowlist/denylist of member-assignable permission keys) —
      that would shrink the whole class, but is a behavior change for
      existing deployments; scope decides.
- [ ] Docs: `docs/django_developer/core/permissions.md` explains when the
      group fallback applies and how to opt out for global-effect endpoints.

## Plan

### Goal
Close the cross-tenant escalation class: promote a global-only permission
decorator into the framework, switch every global-effect `requires_perms`
endpoint to it (with an ApiKey fail-closed guard), and fix the three adjacent
holes the audit surfaced (list-literal 500, invite-path
`MEMBER_PERMS_PROTECTION` bypass, `kind="dict"` read).

### Context — what exists (audit recon, verified 2026-07-08)

**The vulnerable mechanism** — `mojo/decorators/auth.py:14-54`
(`requires_perms`): checks global `request.user.has_permission(perms)`, then
because `REQUIRES_PERMS_IS_GROUP = settings.get_static(..., True)` (:11, read
once at import), falls back to
`request.group.user_has_permission(request.user, perms, True)` (:44), setting
`request.group` itself from the client `"group"` param if unset (:42-43). The
dispatcher also sets it (`mojo/decorators/http.py:74-76` from `group`,
:101-108 from `group_uuid`, active groups only). JWTs carry `uid` only
(`user.py:647`) — no server-side user↔group binding; the member check is the
only gate. `GroupMember.add_permission`/`has_permission`
(`member.py:106-136`) accept arbitrary string keys;
`Group.user_has_permission` (`group.py:213-221`) falls through to member
perms. So any group admin can mint any permission key for a teammate,
scoped to their group — and that satisfies `requires_perms` on ANY endpoint
when the caller passes their own group id.

**Enumeration** — 59 `@md.requires_perms(` sites; `requires_group_perms`
(`auth.py:57-91`) has **zero call sites**. Classification:

*GLOBAL-EFFECT (~44 — the fix surface; every handler reads/writes
platform-wide state, verified by reading each body):*
- `mojo/apps/jobs/rest/control.py` — ALL 12: :16 config, :57 clear-stuck,
  :89 manual-reclaim, :117 purge, :151 reset-failed, :221 clear-queue,
  :260 queue-sizes, :285 rebuild-scheduled, :322 cleanup-consumers,
  :357 channels, :378 force-scheduler-lead, :418 test. (Global Redis streams
  + unfiltered `Job` table.)
- `mojo/apps/jobs/rest/jobs.py` — ALL 14: :42 status/<job_id>, :68 cancel,
  :90 retry, :123 + :144 health, :192 runners, :221 runners/ping,
  :247 runners/shutdown (**fleet op**), :272 + :293 runners/sysinfo,
  :320 runners/broadcast (**fleet shutdown/pause/resume**), :356 stats,
  :377 test, :404 tests. (Job lookups by id have no group scoping.)
- `mojo/apps/aws/rest/` — 7: `send.py:23` email/send (resolves ANY Mailbox
  by client `from_email`, no group filter), `email_ops.py:25` onboard,
  `:85` audit, `:133` reconcile (mutates SES/SNS/S3),
  `cloudwatch.py:39` resources + `:75` fetch (whole AWS account),
  `s3.py:8` bucket list/create.
- `mojo/apps/account/` — 7: `rest/user.py:36` auth/manage/clear_rate_limit
  (any IP/user), `:77` auth/manage/throttle, `rest/device.py:14`
  user/device/lookup (any device by duid → `on_rest_get`, no model re-check),
  `rest/device.py:81` system/geoip/sync (writes global threat intel; perm
  `geoip_sync`), `rest/login_event.py:67` logins/summary + `:79` logins/user
  (platform-wide login events / any user by id), `rest/push.py:128`
  devices/push/send (pushes to arbitrary user_ids).
- `mojo/apps/metrics/rest/permissions.py:9` — GET/POST/DELETE ACLs for any
  metrics account namespace.
- `mojo/apps/incident/rest/event.py:24` — health/summary (platform-wide
  events).

*JUDGMENT CALLS (switch too — flagged for sign-off):*
- `mojo/apps/phonehub/rest/sms.py:17` sms/send — row is group-stamped but
  sends SMS to arbitrary numbers via the platform provider (cost/abuse
  vector, like aws email/send).
- `mojo/apps/account/rest/push.py:243` push/config/<pk>/test — reads ANY
  PushConfig cross-tenant and fires a test push.
- `mojo/apps/assistant/rest/assistant.py:22` /api/assistant + `:56`
  /api/assistant/context — admin LLM agent whose tools act platform-wide
  (context endpoint already re-checks global VIEW_PERMS at :73-80).

*GROUP-SCOPED — fallback correct, MUST keep working:*
`account/rest/group.py:51` webhook_secret (operates on `request.group` only;
covered by `tests/test_account/test_webhook_secret.py:44,144-198` which
seeds member perms and asserts 200/denial), `assistant/rest/memory.py`
:15/:26/:40/:62 (user+group-scoped memory).

*MIXED — leave unchanged (model-security is the effective gate):*
`account/rest/device.py:25` device/location (model has no group FK → member
fallback can't apply at model layer), `aws/rest/templates.py:17`,
`email.py:24,:31`, `messages.py:23,:30` (delegate to `on_rest_request`).

*BROKEN (fix in passing):* `mojo/rest/model_permissions.py:86` and `:122`
pass a **list literal** to `requires_perms` → `set((['…'],))` raises
`TypeError` (verified) → the endpoints 500 before authorizing. Handlers read
static RestMeta metadata (global, read-only).

**Model-security path is NOT vulnerable** (no changes needed —
regression-test it): `mojo/models/rest.py:270-287` gates the member fallback
on `hasattr(cls, "group")`; `rest.py:267-268` overwrites `request.group` with
the instance's own group (a global row → `None` → fallback disabled);
create-stamping (`rest.py:1246-1248`) binds new rows to the same
`request.group` that satisfied the check — you cannot use group G for the
gate and write a `group=None` row. Verified concretely for `Setting`
(nullable group FK): member-scoped `manage_settings` CANNOT create/update a
global row. `User.check_edit_permission` (`user.py:910-913`) is global-only.

**The fix pattern to promote** — `mojo/apps/account/rest/geofence.py:46-80`
`_requires_global_perms` (DM-017): global `User.permissions`/superuser
only, registers `SECURITY_REGISTRY[key] = {..., 'global_only': True}`. Seven
usages, all in geofence.py (:168, :204, :228, :239, :260, :286, :310).
Decorator export: `mojo/decorators/__init__.py:3` does `from .auth import *`,
so a `requires_global_perms` defined in `auth.py` becomes `md.requires_global_perms`
with no `__init__` edit.

**KNOWN GAP in the current `_requires_global_perms` (fix during promotion)**:
`request.user` can be a **bare ApiKey** under `Authorization: apikey <token>`
(DM-016; `api_key.py:236-237` binds `request.group`/`request.api_key`).
`ApiKey.has_permission` is a group-scoped credential check — it must NOT
satisfy a global-only gate. The framework's canonical "is this a real request
User?" idiom is `hasattr(user, "is_request_user")` (used at `group.py:216`,
`rest.py:241/264/465`).

**MEMBER_PERMS_PROTECTION** — `member.py:8-9`:
`settings.get("MEMBER_PERMS_PROTECTION", {})` (**no `kind="dict"`** — a
DB-backed Setting would return a string and `perm in <str>` becomes substring
matching). Enforced in `can_change_permission` (`member.py:84-104`; global
`manage_groups`/`manage_users` holders bypass; listed perms require the
granter to hold the mapped requirement — `sys.<x>` escalates to the granter's
GLOBAL perm). Member-perm edits via `POST /api/group/member(/<pk>)`
(`group.py:29-33`, `set_permissions` → `can_change_permission` — correctly
gated) — but **`POST /api/group/member/invite` bypasses it**: `group.py:45`
calls `ms.on_rest_update_jsonfield("permissions", …)` directly (merges with
no `can_change_permission`). Harmless while the map is `{}`, defeats any
configured policy.

**REQUIRES_PERMS_IS_GROUP** — module-global, read once at import
(`auth.py:11`), listed bare in
`docs/django_developer/helpers/settings_reference.md:335`, never set False
in-repo. Flipping it False would break the legitimate group-scoped set — a
per-endpoint decorator is the right granularity.

**Regression-test template** —
`tests/test_geofence/config_plane.py:508-557`
`test_group_scoped_perm_cannot_touch_global_config`: create user →
`grp.add_member(user)` → `GroupMember.objects.get(...)` →
`member.add_permission(<perm>)` → login → call endpoint with
`"group": grp.pk` → assert 403 + no side effect.

### Changes — what to do

**1. `mojo/decorators/auth.py`** — add `requires_global_perms(*perms)`
(promote DM-017's `_requires_global_perms` verbatim, PLUS the ApiKey
guard):
- after the `is_authenticated` check, reject non-User identities:
  `if not hasattr(user, "is_request_user"): raise PermissionDeniedException()`
  — a group-scoped ApiKey credential must never satisfy a global-only gate
  (fail-closed; canonical idiom per DM-016).
- keep the `SECURITY_REGISTRY` entry with `'global_only': True`.
- docstring states the rule: use for any endpoint whose effect is not
  confined to `request.group`.

**2. `mojo/apps/account/rest/geofence.py`** — delete the local
`_requires_global_perms` (:46-80) and switch its 7 usages to
`@md.requires_global_perms(...)` (same perm tuples). This also picks up the
ApiKey guard for the geofence config plane.

**3. Decorator swaps** `@md.requires_perms(...)` →
`@md.requires_global_perms(...)` (same perm tuples, no other changes) on the
GLOBAL-EFFECT + JUDGMENT-CALL surface:
- `mojo/apps/jobs/rest/control.py` — all 12 sites (:16, :57, :89, :117,
  :151, :221, :260, :285, :322, :357, :378, :418).
- `mojo/apps/jobs/rest/jobs.py` — all 14 sites (:42, :68, :90, :123, :144,
  :192, :221, :247, :272, :293, :320, :356, :377, :404).
- `mojo/apps/aws/rest/send.py:23`, `email_ops.py:25,:85,:133`,
  `cloudwatch.py:39,:75`, `s3.py:8`.
- `mojo/apps/account/rest/user.py:36,:77`, `rest/device.py:14,:81`,
  `rest/login_event.py:67,:79`, `rest/push.py:128,:243`.
- `mojo/apps/metrics/rest/permissions.py:9`.
- `mojo/apps/incident/rest/event.py:24`.
- `mojo/apps/phonehub/rest/sms.py:17`.
- `mojo/apps/assistant/rest/assistant.py:22,:56`.
Do NOT touch: `account/rest/group.py:51`, `assistant/rest/memory.py` (all 4),
or the MIXED delegating endpoints (model-security governs them).

**4. `mojo/rest/model_permissions.py:86,:122`** — fix the list-literal bug
(currently 500s): unpack to varargs and, since the handlers read global
RestMeta metadata, use `@md.requires_global_perms('view_admin',
'manage_users', 'admin')`.

**5. `mojo/apps/account/models/member.py:8-9`** — read the protection map
with `kind="dict"`: `settings.get("MEMBER_PERMS_PROTECTION", {}, kind="dict")
or {}` so a DB-backed Setting works instead of degrading to substring checks.

**6. `mojo/apps/account/rest/group.py:36-47` (invite path)** — route the
invite's `permissions` payload through `ms.set_permissions(value)` (which
enforces `can_change_permission`) instead of calling
`ms.on_rest_update_jsonfield("permissions", …)` directly at :45. With the
default empty map and the inviter already holding group-manage perms this is
behavior-neutral; with a configured map the policy now actually holds.
Builder: `set_permissions` iterates a dict — pass the same dict; confirm
`active_request` is populated on `ms` (it is set by the REST layer for
instances created in-request; if not, pass `request` explicitly per its
signature `can_change_permission(self, perm, value, request)` — read
`member.py:84-104` and adapt the call so the REQUESTING user is evaluated).
- Keep `MEMBER_PERMS_PROTECTION` default `{}` (no shipped policy — a
  non-empty default is a deployment behavior change). Document a recommended
  map instead (docs change below).

**7. Tests — new module `tests/test_global_perms/`**
(`__init__.py` with `TESTIT = {"requires_apps": ["mojo.apps.account"]}`):
- `escalation.py` — the core regression, table-driven. Setup: one
  member-granted user (per the DM-017 template: real Group + GroupMember +
  `member.add_permission(...)` for every perm name used by the switched
  endpoints — manage_jobs, view_jobs, jobs, manage_aws, comms, files,
  manage_users, users, security, send_notifications, manage_push_config,
  send_sms, view_admin, assistant, manage_incidents, metrics,
  manage_metrics, geoip_sync, manage_settings) and one global-granted user
  (User.add_permission("manage_jobs") etc. for the few 200-path checks).
  Then loop a literal table of every switched endpoint —
  `(method, path, body_with_group_param)` — asserting **403** for the
  member-granted user (denial fires at the decorator, so even
  side-effectful endpoints like runners/broadcast and aws onboard are safe
  to include; no handler code runs). 200-path spot-checks with the
  global-granted user ONLY on side-effect-free reads that don't need
  external services: `GET /api/jobs/control/config`,
  `GET /api/jobs/health`, `GET /api/incident/health/summary`,
  `GET /api/account/logins/summary`, `GET /api/auth/manage/throttle`
  (+ params as needed). NO aws 200-path (would call AWS).
- `apikey_gate.py` — an ApiKey whose permission set includes a global-gate
  perm (e.g. manage_jobs / manage_geofence) must get 403 on
  `GET /api/jobs/control/config` and `GET /api/geo/rules` (the ApiKey
  guard). Builder: mint a key via the existing ApiKey model/REST the way
  `tests/test_user_mgmt/api_keys.py` does, then call with
  `Authorization: apikey <token>`.
- `model_permissions.py` — regression for change 4: global-admin user →
  200 (was 500); member-granted user → 403.
- `invite_protection.py` — with `Setting.set("MEMBER_PERMS_PROTECTION",
  {"itest_protected_perm": "manage_groups"})` (a test-only key so parallel
  member flows are untouched; delete the row + finally-cleanup per testit
  rules): invite with `permissions: {"itest_protected_perm": true}` from a
  group admin who lacks global manage_groups → the perm must NOT land on the
  member; a plain perm in the same invite still lands (map only constrains
  listed keys). Also assert the `kind="dict"` read: the DB-backed map (a
  JSON string in the Setting row) is honored.
- Existing suites must stay green — especially
  `tests/test_account/test_webhook_secret.py` (member fallback kept on the
  group-scoped endpoint) and the admin suites that use GLOBAL grants
  (`tests/test_jobs/test_sysinfo.py`, `tests/test_incident/test_health_summary.py`,
  `tests/test_aws/cloudwatch.py`, `tests/test_metrics/basic.py`).

**8. Docs + CHANGELOG** (see Docs section). No schema changes → no
`bin/create_testproject` needed.

### Design decisions
- **Per-endpoint decorator, not `REQUIRES_PERMS_IS_GROUP=False`** — the
  global flag is read once at import and would break the legitimate
  group-scoped endpoints (webhook_secret, assistant memory). Surgical opt-out
  per endpoint; the flag's semantics are unchanged.
- **Same perm tuples, only the check scope changes** — deployments that
  granted admins these perms GLOBALLY (the intended way; all existing tests
  do) see zero change. Only member-minted grants stop working on
  global-effect endpoints — which is the vulnerability.
- **ApiKey identities are rejected by the global gate** — an ApiKey is a
  group-scoped credential (`ApiKey.has_permission`); letting one satisfy a
  platform-global gate recreates the same escalation through a different
  door. Fail-closed via the canonical `is_request_user` idiom. (Deployments
  needing machine access to fleet endpoints should use a real service
  account user.) Flagged for sign-off since it tightens current geofence
  behavior too.
- **MIXED delegating endpoints unchanged** — `on_rest_request` re-gates via
  model security, which is structurally safe (three verified guards); the
  outer `requires_perms` is a coarse pre-filter. Switching them would add no
  security and risk breaking legitimate group-scoped model flows.
- **sms/send, push-config test, assistant switched despite group-stamped
  rows** — they consume platform-wide resources (SMS spend, any tenant's
  push config, an agent with platform tools); the stamped row doesn't confine
  the effect. Flagged as judgment calls for sign-off.
- **`MEMBER_PERMS_PROTECTION` stays `{}` by default** — a shipped policy map
  would change existing deployments' group-admin flows. Instead: fix the
  invite bypass + the `kind="dict"` read so a configured map actually works,
  and document a recommended map. Rejected: non-empty default (behavior
  change); rejected: removing arbitrary member perm keys (would break the
  legitimate pattern of app-defined group perms).
- **model_permissions list-literal fixed as part of this item** — it is a
  `requires_perms` call-site bug inside the audited surface; fixing it
  elsewhere would mean shipping the audit with a known-broken auth gate in
  scope. Behavior change is 500 → enforced perms (strictly better).

### Edge cases & risks
- **Breaking legitimate member-granted admin flows**: the audit found no
  test or doc that relies on member-scoped grants for any switched endpoint
  (only webhook_secret + assistant memory rely on the fallback, both kept).
  Residual risk is a deployment that granted e.g. member-scoped manage_jobs
  intentionally — release note calls this out explicitly.
- **ApiKey machine integrations** hitting jobs/aws endpoints with key perms
  would newly 403 — called out in CHANGELOG; service-account users are the
  supported path. (Verify in recon-during-build whether any in-repo caller
  does this — the geoip federation client uses `apikey` auth against
  `system/geoip/lookup`, which is NOT switched (uses `requires_auth`), and
  `system/geoip/sync` federation POSTs use a key with `geoip_sync`… **builder
  must check**: if fleet sync callers authenticate via ApiKey, the ApiKey
  guard on `device.py:81` would break federation → in that case leave
  `geoip/sync` on `requires_perms` but document, or gate it with an explicit
  key-perm check instead. Decide from evidence: grep
  `mojo/apps/account/asyncjobs.py:91` sync client auth.)
- **403 vs 500 ordering in the table-driven test**: denial fires in the
  decorator before any handler/external call — safe to enumerate every
  endpoint including fleet-destructive ones.
- **Parallel-test hygiene**: the invite-protection test writes a global
  Setting row — protect a test-only perm key, clean before/after
  (long-lived DB rule), and never touch keys real flows use. Global-grant
  users are per-test unique (uuid emails).
- **`security` perm as domain category**: several switched endpoints accept
  bare `security`/`jobs`/`comms` category perms — unchanged; only WHERE the
  perm may live changes (global user grants).

### Tests
(see Changes #7 — module `tests/test_global_perms/` with `escalation.py`
table-driven 403 sweep over every switched endpoint + 200-path spot checks,
`apikey_gate.py`, `model_permissions.py` 500-fix regression,
`invite_protection.py`; plus keep-green checks on the webhook-secret member
flow and global-grant admin suites.)

### Docs
- `docs/django_developer/core/permissions.md` — new section "Global vs
  group-scoped permission checks": when `requires_perms`' fallback applies,
  when to use `md.requires_global_perms` (any endpoint whose effect isn't
  confined to `request.group`), the ApiKey rule, `MEMBER_PERMS_PROTECTION`
  semantics + a recommended protection map, invite path now enforcing it.
- `docs/django_developer/helpers/settings_reference.md` — describe
  `REQUIRES_PERMS_IS_GROUP` (currently a bare key) and add
  `MEMBER_PERMS_PROTECTION` (dict, default `{}`, `kind="dict"`-read).
- `docs/web_developer/account/admin_portal.md` — note that
  jobs/aws/metrics-ACL/incident-health/geofence admin endpoints require
  GLOBAL permission grants (member/group grants and API keys don't apply).
- `CHANGELOG.md` — security block: the escalation class, the ~44 switched
  endpoints (by cluster), the ApiKey rule, model_permissions 500 fix, invite
  protection fix, deployment-facing notes.

### Open questions
None blocking. Three judgment calls flagged for sign-off (recommendations
baked in): (1) sms/send + push-config-test + assistant switched to global
despite group-stamped rows; (2) ApiKey identities rejected by the global
gate (tightens geofence too; machine access = service-account users);
(3) `geoip/sync` — switch stands UNLESS build-time evidence shows fleet
federation authenticates via ApiKey, in which case keep it off the ApiKey
guard and gate explicitly (builder decides from `asyncjobs.py:91` evidence
and documents).

## Notes

- Origin: DM-017 post-build security review (2026-07-08), which rated the
  geofence instance CRITICAL (platform-wide jurisdiction rules + IP allowlist
  writable by a single-tenant admin). The geofence surface is already fixed;
  this item is the rest of the audit.
- The reviewer verified the fallback mechanism itself is long-standing,
  pre-existing behavior — the audit is about which endpoints sit on top of it.

- **Baseline (2026-07-08, `bin/run_tests --agent`)**: passed — 2316 total,
  2260 passed, 0 failed, 56 skipped. Green.
- **Build outcome (2026-07-08, commit `21197b1`)**: full suite 2323 / 2267 / **0
  failed** / 56 skipped (+7 = the new module; baseline invariant held). The
  legitimate group-fallback flows stayed green (`test_webhook_secret`,
  `test_geoip_sync_endpoint`, phonehub apikey SMS) as did the admin suites.
- **Plan deviations (evidence-driven at build time):**
  1. **`phonehub sms/send` NOT switched** — the plan listed it as a judgment
     call; reading the code (`phonehub/rest/sms.py:39-42`) shows it is
     ApiKey-by-design (handles `request.user` being an ApiKey) and its effect is
     group-scoped (SMS attributed to `request.group`); it is also
     ApiKey-federated (`phonehub/services/mojo_provider.py:47` POSTs to a
     remote's sms/send with `apikey`). Switching would break SMS federation. Left
     on `requires_perms`.
  2. **`geoip/sync` resolved (open Q #3)** — fleet federation authenticates via
     ApiKey (`account/asyncjobs.py:93` `Authorization: apikey`), so it uses
     `requires_global_perms('geoip_sync', allow_api_keys=True)` — closes the
     member fallback while keeping the federation key working. This drove the
     `allow_api_keys` parameter on the promoted decorator.
  3. **`model_permissions` tested via import, not HTTP** — the route is not
     mounted in the testproject (`mojo/rest/` isn't auto-discovered), so the
     500-fix regression asserts the decorator metadata is a flat varargs list
     (the bug was a nested list) + `global_only`, rather than an HTTP 200.
- **Post-review completion (security review, 2026-07-08):** the review found a
  sibling vector — six delegating `requires_perms` endpoints on **groupless**
  models (`aws/rest/{email,templates,messages}.py`: EmailDomain/Mailbox/
  EmailTemplate/IncomingEmail/SentMessage; `account/rest/device.py` location →
  UserDeviceLocation). A self-minted group **ApiKey** with self-claimed
  `manage_aws`/`manage_users` reached them cross-tenant via the model-layer
  api_key branch (`mojo/models/rest.py:288`). These are in DM-018's audited
  surface (they are `requires_perms` endpoints I'd misclassified as
  "model-security re-gates" — true for the GroupMember vector, false for the
  ApiKey vector), so switched them to `requires_global_perms` and extended
  `apikey_gate.py` to assert 403 on all six. The **broader** ApiKey issue —
  unrestricted `ApiKey.permissions` + the same `rest.py:288` branch letting a
  self-minted key reach OTHER groupless `uses_model_security` models (User,
  GeoLocatedIP) — is a distinct pre-existing mechanism; filed as
  `planning/inbox/apikey-permissions-unrestricted-groupless-model-access.md`
  (P1) with the full evidence chain.

## Resolution
- closed: 2026-07-08
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/disable_lifecycle.md,docs/django_developer/account/group.md,docs/django_developer/account/login_events.md,docs/django_developer/account/push.md,docs/django_developer/assistant/README.md,docs/django_developer/aws/cloudwatch.md,docs/django_developer/core/decorators.md,docs/django_developer/core/permissions.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/jobs/admin.md,docs/django_developer/metrics/permissions.md,docs/web_developer/account/admin_portal.md,docs/web_developer/account/login_events.md,docs/web_developer/account/push.md,docs/web_developer/account/user.md,docs/web_developer/assistant/README.md,docs/web_developer/aws/cloudwatch.md,docs/web_developer/jobs/jobs.md,docs/web_developer/security/README.md,memory.md,mojo/apps/account/models/member.py,mojo/apps/account/rest/device.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/rest/group.py,mojo/apps/account/rest/login_event.py,mojo/apps/account/rest/push.py,mojo/apps/account/rest/user.py,mojo/apps/assistant/rest/assistant.py,mojo/apps/aws/rest/cloudwatch.py,mojo/apps/aws/rest/email.py,mojo/apps/aws/rest/email_ops.py,mojo/apps/aws/rest/messages.py,mojo/apps/aws/rest/s3.py,mojo/apps/aws/rest/send.py,mojo/apps/aws/rest/templates.py,mojo/apps/incident/rest/event.py,mojo/apps/jobs/rest/control.py,mojo/apps/jobs/rest/jobs.py,mojo/apps/metrics/rest/permissions.py,mojo/decorators/auth.py,mojo/rest/model_permissions.py,planning/in_progress/DM-018-audit-requires-perms-group-fallback-on-global-effe.md,planning/inbox/apikey-permissions-unrestricted-groupless-model-access.md,tests/test_global_perms/__init__.py,tests/test_global_perms/_helpers.py,tests/test_global_perms/apikey_gate.py,tests/test_global_perms/escalation.py,tests/test_global_perms/invite_protection.py,tests/test_global_perms/model_permissions.py,uv.lock
  switched endpoints — jobs/rest/{control,jobs}.py, aws/rest/{cloudwatch,
  email_ops,s3,send}.py, account/rest/{user,push,login_event,device}.py,
  metrics/rest/permissions.py, incident/rest/event.py, assistant/rest/
  assistant.py, account/rest/geofence.py (dropped local copy),
  mojo/rest/model_permissions.py; adjacent fixes — account/models/member.py
  (kind="dict"), account/rest/group.py (invite→set_permissions); docs
  (core/permissions.md, helpers/settings_reference.md, web admin_portal.md),
  CHANGELOG.md
- tests added: tests/test_global_perms/ — escalation.py (member-grant 403
  sweep over ~25 global endpoints + global-grant/superuser 200 checks),
  apikey_gate.py (ApiKey denied on global gate + geoip/sync allow_api_keys
  accepted), invite_protection.py (MEMBER_PERMS_PROTECTION enforced on invite +
  kind="dict"), model_permissions.py (flat-perms 500-fix regression),
  _helpers.py
