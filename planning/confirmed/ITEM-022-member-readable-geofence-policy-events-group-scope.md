---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-022
type: feature
title: Member-readable geofence policy + events — group-scoped visibility for brand admins (not just global platform staff)
priority: P2
effort: M
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-017, ITEM-020, mverify_portal#ITEM-014]
links: []
---

# Member-readable geofence policy + events — group-scoped visibility for brand admins

## What & Why

ITEM-017's REST contract (`GET /api/geo/rules`, incident Events) and the
consuming portal page (mverify_portal ITEM-014) are both, by design,
**platform-admin only**: every read requires a GLOBAL user-level permission
(`view_geofence`/`manage_geofence`/`security` for rules; `view_security`/
`security` for events) — member/group-scoped grants explicitly do not
authorize (`mojo/apps/account/rest/geofence.py` docstring: "these endpoints
manage platform-wide config"). That's correct for the global config surface,
but it means a brand's OWN admin (a GroupMember of a single tenant/platform,
not a platform-wide operator) cannot see geofencing status for their own
platform at all — even though it is actively enforcing on their sign-up and
checkout pages right now.

This item is the group-scoped counterpart: let a member with an appropriate
per-group permission see (a) the effective geofencing policy for **their own
group only**, and (b) enforcement events for their own group — without
exposing platform-wide internals (other groups' rules, full posture
operational detail, exemption counts, `enforced_endpoints`) to them.

Raised during mverify_portal ITEM-014's design review (2026-07-08: "is this
not admin, what about by platform/group?") and left as an open question
until now.

## Acceptance Criteria

- [ ] A group-scoped, read-only path exists for a caller holding an
      appropriate **per-group** permission to fetch the effective
      geofencing policy for **their own group only** — NOT a loosened
      version of the existing global endpoint's full response. Investigate
      first: can `/api/geo/rules` support a member-scoped mode with a
      deliberately narrower payload (e.g. plain "baseline + your group's
      rules", omitting `enforced_endpoints`/`cache_ttl`/`allowlist_summary`/
      other-group data), or does this want a distinct endpoint? Decide and
      justify in the plan.
- [ ] **Verify before designing**: does the framework's existing
      group-scoped list fallback (`mojo/models/rest.py`'s `on_rest_handle_list`
      — authorizes GROUP-scoped `view_security`/`security` grants filtered to
      `group__in=<the caller's groups>`) already work for Incident Event
      access to `geofence_block`/`geofence_exempt`/`geofence_config`
      categories? If yes, this item's event-side work may just be
      confirming/exercising it (+ a regression test) rather than building
      new mechanics; if no, close the gap.
- [ ] No cross-tenant leakage: a group-scoped caller never sees another
      group's rules, another group's events, or global operational detail
      not relevant to their own platform (cache TTL, allowlist internals,
      enforced-endpoints list).
- [ ] Permission key(s) used are checked against product conventions before
      inventing new ones — e.g. does the calling product already have a
      group-scoped "compliance"-flavored role that should carry this, rather
      than a bespoke key? (mverify_portal's existing group-scoped pages use
      `admin_compliance`/`admin_verify`-style keys — check fit.)
- [ ] Docs (both tracks) distinguish the two audiences explicitly: platform
      staff (global, full config) vs. brand admin (group-scoped, their
      platform's effective policy + events only).
- [ ] Tests: a group-scoped grant sees only their own group's data; a
      global-only grant continues to work unchanged; a group-scoped grant
      without the new permission still gets 403.

## Plan

**Approved 2026-07-09.** Shape: one new narrow endpoint `GET /api/geo/policy` gated by
member/global `view_security`/`security`; the events side is regression-test + docs only
(the mechanics already work). Global config plane (`geo/rules` etc.) untouched.

### Goal

Let a group member holding a per-group `view_security`/`security` grant read (a) the
effective geofence policy for their own group via a new, deliberately narrow
`GET /api/geo/policy`, and (b) their own group's geofence events via the existing
Event REST surface — with zero cross-tenant or platform-internals leakage and the
global config plane unchanged.

### Context — what exists (verified 2026-07-08, HEAD 55d9d1f — re-verify line refs at build time; ITEM-021 is touching geofence files)

**Config plane is global-only by design.** Every `geo/*` admin endpoint in
`mojo/apps/account/rest/geofence.py` uses `@md.requires_global_perms(...)` (no member
fallback; ApiKeys rejected). The module docstring (lines 22–25) states member grants
must never authorize platform-wide config. `GET geo/rules` (geofence.py:127–168)
returns the full compliance artifact:

- `system.rule` + `source`/`modified` (:132–141); `posture` incl. `fail_closed`,
  `fail_closed_scopes`, `allow_private_ips`, `strict_posture`, `cache_ttl` (:142–149);
  `allowlist_summary` (:150, helper :117–124); `evaluation_order` (:151);
  `enforced_endpoints` (:152, helper :104–114).
- When `group_uuid` is passed (`_resolve_group_param` :90–101 — deliberately returns
  inactive groups for admins), a `group` block (:154–167):
  `{id, uuid, is_active, rule: (group.metadata or {}).get("geofence") or {},
  strict_posture (raw tri-state), strict_posture_effective}`.
- Views return plain dicts: `return {"status": True, "data": data}` (:168).
- `SYSTEM_RULES_KEY = "GEOFENCE_SYSTEM_RULES"` (:37).

**Rule storage:** system baseline = `GEOFENCE_SYSTEM_RULES` setting; per-group rule =
`Group.metadata["geofence"]` (plain `models.JSONField(default=dict)`,
`mojo/apps/account/models/group.py:41`); strict override =
`Group.metadata["geofence_strict"]` tri-state (None = inherit global
`GEOFENCE_STRICT_POSTURE`). Engine accessors `_system_rules(request=None)`
(engine.py:113–119), `_group_rules(group)` (:122–126), `_strict_posture(request, group)`
(:129–139) support test-mode header overrides; `geo/rules` deliberately reads persisted
settings directly instead — the member endpoint mirrors `geo/rules`.

**Permission plumbing:**

- `@md.requires_perms(*perms)` (`mojo/decorators/auth.py:14–54`): requires auth (:32);
  global `request.user.has_permission(perms)` first (:37); else (with
  `REQUIRES_PERMS_IS_GROUP` default True) resolves `request.group` from numeric
  `request.DATA.group` if unset (:42–43) and requires
  `request.group.user_has_permission(request.user, perms, True)` (:44) —
  **no `request.group` → PermissionDenied (403)**.
- `Group.user_has_permission(user, perms, check_user=True)`
  (`mojo/apps/account/models/group.py:213–221`): global perms first, then the ApiKey
  guard (`hasattr(user, "is_request_user")` — ITEM-016 choke point), then
  `get_member_for_user(check_parents=True).has_permission(perms)`.
- The dispatcher (`mojo/decorators/http.py:69–117`) resolves `request.group` BEFORE the
  view: numeric `group` param (:74–81, no `is_active` filter, enforces
  `api_key.is_group_allowed`) or `group_uuid` (:101–111, **active groups only**).

**Event side already works mechanically:**

- `Event` (`mojo/apps/incident/models/event.py`): nullable `group` FK (:57–58);
  `RestMeta.VIEW_PERMS = ["view_security", "security"]`,
  `SAVE_PERMS = ["manage_security", "security"]`, `CREATE_PERMS = ["all"]`; the default
  graph exposes `group_id` as a scalar only (deliberate anti-leak, comment :71–83); no
  custom permission overrides. REST endpoint: `mojo/apps/incident/rest/event.py:17–20`
  (`event`, `event/<int:pk>` → `Event.on_rest_request`).
- Framework group-scoped list fallback (`mojo/models/rest.py`, `on_rest_handle_list`
  :462–519): a caller without global VIEW_PERMS but with a matching member grant gets
  `request.user.get_groups_with_permission(perms)` → `filter(group__in=<those groups>)`
  (:489–496). **Rows with `group=None` are excluded** (`__in` never matches NULL). If a
  `group` param also set `request.group`, `on_rest_list` (:659–668) narrows further to
  that one group. Single-instance GET (`_evaluate_permission` :202–325): instance with a
  group → member grant passes (:270–287); groupless instance → global perms only
  (:288–318). Already proven for Event by
  `tests/test_account/test_aggregation_permissions.py` (member `view_security` in group
  A sees exactly A's events; groupless rows invisible).
- **Attribution caveat (ITEM-020, deliberate):** the geofence evidence calls
  (`mojo/apps/account/services/geofence/evidence.py` — `report_block` :49–62,
  `report_exempt` :77–91, `report_config_change` :110–122) never pass `group=`; the
  reporter (`mojo/apps/incident/reporter.py::_resolve_event_group` :13–41) falls back to
  `request.group`. So `geofence_block`/`geofence_exempt` events carry a group only when
  the geofenced request itself carried `group`/`group_uuid`; `geofence_config` events
  (admin config writes) are effectively always groupless. Do NOT reopen attribution
  (deriving group from `user.org` was explicitly rejected in ITEM-020).
- Read-side precedent for member-scoped geofence data: metrics
  `_check_group_account_permission` (`mojo/apps/metrics/rest/helpers.py:8–23`) — global
  perm OR `group.user_has_permission(user, perms, False)`; already serves
  `geofence:blocks`/`geofence:exempt` under `account="group-<id>"`.

**Permission keys:** `view_geofence`/`manage_geofence` exist ONLY as global perms (used
solely in rest/geofence.py + the permissions doc — never in any RestMeta, never granted
as member perms). The member-grantable security keys across the incident app are
`view_security`/`security`. Doc anchors: `docs/django_developer/core/permissions.md`
security category :107, `view_security` :131, geofence keys :133–134, "geofence config
endpoints accept view_geofence/manage_geofence/security, not RestMeta perms" :187,
"always include the category permission" rule :292.

**Portal (context only — no work in this repo):** mverify_portal's `GeofencingPage.js`
policy card calls `GET /api/geo/rules?group_uuid=...` and 403s for members; its events
card is client-gated on `hasPermission(['view_security','security'])`, which already
passes for member grants (web-mojo `User.js:37` falls through to the active member's
perms). Once this lands, the events card lights up for members as-is; pointing the
policy card at `geo/policy` is a follow-up in that repo.

### Changes — what to do

1. **`mojo/apps/account/rest/geofence.py`** — add ONE endpoint (place after
   `on_geo_check`, before the config-plane divider comment at :83 — it is member-facing,
   not config-plane):

   ```python
   @md.GET("geo/policy")
   @md.requires_perms("view_security", "security")
   def on_geo_policy(request):
       """Member-readable effective geofence policy for ONE group (the caller's).

       Deliberately narrow: baseline + the group's own rule + effective strict
       posture. Never includes platform operational detail (enforced_endpoints,
       allowlist internals, fail-closed scopes, cache TTL, config provenance).
       Auth: global view_security/security OR a member grant on request.group —
       @requires_perms verified the grant against request.group itself, so the
       response is structurally confined to the caller's group.
       """
       group = request.group
       if group is None:
           raise merrors.ValueException("group required")   # 400
       strict_global = settings.get("GEOFENCE_STRICT_POSTURE", False, kind="bool")
       gf_strict = (group.metadata or {}).get("geofence_strict")
       data = {
           "group": {"id": group.pk, "uuid": group.get_uuid(),
                     "name": group.name, "is_active": group.is_active},
           "enabled": settings.get("GEOFENCE_ENABLED", True, kind="bool"),
           "evaluation_order": ["system", "group"],
           "system_rule": settings.get(SYSTEM_RULES_KEY, {}, kind="dict") or {},
           "group_rule": (group.metadata or {}).get("geofence") or {},
           "strict_posture": gf_strict,
           "strict_posture_effective": (bool(gf_strict) if gf_strict is not None
                                        else strict_global),
       }
       return {"status": True, "data": data}
   ```

   Forbidden in this payload, now and forever: `enforced_endpoints`,
   `allowlist_summary`, `cache_ttl`, `fail_closed`, `fail_closed_scopes`,
   `allow_private_ips`, system-rule `source`/`modified`. (A test locks this.)

2. **Same file — module docstring (:1–26):** add `GET /api/geo/policy` under a new
   "Member plane" line; amend the SECURITY paragraph: the config plane stays
   global-only; `geo/policy` is the one deliberate member-scoped read, confined to
   `request.group` with a narrowed payload.

3. **`tests/test_geofence/member_visibility.py`** — new test module (see Tests).

4. **Docs + `CHANGELOG.md`** — see Docs.

No model changes → no `bin/create_testproject`. No engine/evidence changes. No changes
to `geo/rules` or any other existing endpoint.

### Design decisions

1. **Distinct endpoint (`geo/policy`), not a member mode on `geo/rules`.** Separate
   response builders make future leaks structurally impossible (a field added to the
   admin artifact can never reach members); decorators stay single-purpose
   (`requires_global_perms` vs `requires_perms`); the docstring invariant "member grants
   never open the config plane" stays literally true. Rejected: branching inside
   `on_geo_rules_get` — drift-prone; one forgotten branch = leak.
2. **Keys = `view_security`/`security` (global or member) via `@md.requires_perms`.**
   Matches `Event.VIEW_PERMS` exactly, so ONE member grant lights up both policy and
   events; `security` is the domain-category key project rules require. Rejected:
   member-scoped `view_geofence` (global-only today; giving one string two scopes
   invites exactly the config-plane confusion the docstring warns about);
   `admin_compliance`/`admin_verify` (mverify product role names — wrong layer for the
   framework; the portal maps its roles to member grants). ITEM-018's
   requires_perms-escalation concern does not apply: the endpoint's effect is genuinely
   confined to `request.group` (read-only; response derived solely from it).
3. **Payload includes the system baseline + effective strict posture** because those
   actually apply to the group's traffic ("effective policy" = baseline + your rules).
   Global internals that don't change what is enforced for them (scope lists, TTLs,
   allowlist ops data, config provenance) are excluded.
4. **Read persisted config directly (settings + `group.metadata`), mirroring
   `geo/rules`** — not the engine's test-header-aware `_system_rules`/`_strict_posture`.
   This endpoint reports persisted policy (a config read), not a simulated evaluation —
   the same choice `geo/rules` already made. No engine edits → no collision with
   ITEM-021's in-flight engine work.
5. **Event side: no emission changes.** Member visibility of group-attributed events
   already works via the RestMeta fallback; attribution stays `request.group`-only
   (ITEM-020 decision). `geofence_config` stays effectively platform-only (groupless) —
   acceptable: those describe platform config changes; a member-visible history of
   group-rule edits can be a future item.
6. **`request.group` is the only group input.** Do NOT use `_resolve_group_param` here
   (it deliberately returns inactive groups, an admin affordance). Dispatcher semantics
   apply: `group_uuid` resolves active groups only; numeric `group` resolves inactive
   too, but the member grant check still confines.

### Edge cases & risks

- **No group param:** member → 403 (decorator, fail-closed); global holder → 400
  `"group required"` from the view. Both asserted.
- **Member requests another group:** decorator checks the grant against THAT
  `request.group` → 403. Asserted both directions (A→B, B→A).
- **Inactive group via `group_uuid`:** dispatcher won't set `request.group` → member
  403 / global 400. Documented; admins inspect inactive groups via `geo/rules`.
- **ApiKey caller:** `requires_perms` → `ApiKey.has_permission` (self-claimed perms,
  `sys.*` denied) can pass for a group key claiming `view_security`; the dispatcher's
  `api_key.is_group_allowed` (http.py:80–81, :108–111) confines it to its own group
  tree → it sees only its own group's policy. Group-confined, read-only — acceptable.
  Do not add any `ALLOW_API_KEY_GLOBAL`-style widening.
- **Leak creep:** the regression test asserts the forbidden keys are absent from the
  payload.
- **Member undercount on events** (attribution caveat): docs must set expectations —
  members see group-attributed activity, not verified totals (mirror the Metrics
  caveat wording already in `docs/web_developer/account/geofence.md`).
- **ITEM-021 is in_progress in this working tree** (strict posture + threat cache;
  geofence.py/engine.py/tests are being modified). Build this AFTER ITEM-021 closes
  (WIP=1 enforces it) and re-verify the line refs above; the strict-posture fields
  cited already exist in `geo/rules` at HEAD 55d9d1f.

### Tests

New `tests/test_geofence/member_visibility.py` (testit — read
`docs/django_developer/testit/Overview.md` first; run
`bin/run_tests --agent -t test_geofence.member_visibility`; read
`var/test_failures.json`, never terminal output). Follow `tests/test_geofence/
config_plane.py` setup idioms (unique-suffix emails/group names,
`opts.client.login(...)` — never raw `POST /api/auth/login`) and
`tests/test_account/test_aggregation_permissions.py` (:39–58, :70–105) for the
member-grant + event-visibility pattern. Hygiene (from project memory): call
`grp.get_uuid()` right after creating groups (uuid is lazily assigned); this module
makes no `geo/check` calls so it never writes the shared decision cache; setup deletes
its own Events/users/groups before creating (long-lived DB).

Setup (`@th.django_unit_setup()`): groups `gfmv-a`, `gfmv-b` (unique-suffixed names);
`group_a.metadata = {"geofence": {"country": {"in": ["US"]}}}` via direct model save
(JSONField; rule validation is not under test — literal matches the valid rule at
config_plane.py:273). Users: `member_a` (GroupMember in A, membership
`.add_permission("view_security")`), `member_b` (same in B), `plain_a` (member in A, no
grant), `global_viewer` (global `add_permission("view_security")`), `geo_viewer`
(global `add_permission("view_geofence")` only). Events: after deleting leftovers
(`Event.objects.filter(category="geofence_block", group__in=[A, B])` plus this module's
marked groupless rows), create `category="geofence_block"`: ×2 `group=A`, ×3 `group=B`,
×1 `group=None` with a `gfmv`-marked details string.

1. **Member reads own group:** `member_a` GET `/api/geo/policy?group_uuid=<A>` → 200;
   `data.group_rule == {"country": {"in": ["US"]}}`; `data.system_rule` is a dict;
   `data.group.id == A.pk`; forbidden keys absent from `data`: `enforced_endpoints`,
   `allowlist_summary`, `cache_ttl`, `fail_closed_scopes`, `posture`, `source`.
2. **Cross-tenant denied:** `member_a` → `?group_uuid=<B>` → 403; `member_b` →
   `?group_uuid=<A>` → 403.
3. **No grant denied:** `plain_a` → `?group_uuid=<A>` → 403.
4. **Group required:** `global_viewer` no param → 400; `member_a` no param → 403.
5. **Global grants:** `global_viewer` → 200 for both `<A>` and `<B>`; `geo_viewer`
   (global `view_geofence` only) → 403 on `geo/policy` (locks the key choice — that
   audience uses `geo/rules`).
6. **Config plane unchanged:** `member_a` GET `/api/geo/rules?group_uuid=<A>` → 403.
7. **Member-scoped events (the AC verification):** `member_a` GET
   `/api/incident/event?category=geofence_block` → exactly the 2 group-A rows (assert
   the groupless row's id is NOT present); `global_viewer` GET
   `?category=geofence_block&group=<A.pk>` → exactly 2 (unfiltered global list may
   contain other modules' leftovers — don't assert an exact global total); `plain_a` →
   empty list or 401/403 (mirror the tolerance in test_aggregation_permissions.py).

Every assert carries a descriptive failure message.

### Docs

- `docs/web_developer/account/geofence.md` — Permissions section (:10): explicit
  two-audience table (platform staff → config plane via global
  `view_geofence`/`manage_geofence`/`security`; brand admin → `geo/policy` + event feed
  via member `view_security`/`security`). New `GET /api/geo/policy` section (params,
  400/403 behavior, full response example, and what is deliberately absent). Member
  events-feed note (`/api/incident/event?category=geofence_block|geofence_exempt`) with
  the attribution caveat mirroring the Metrics section's "reported activity, not
  verified counts".
- `docs/django_developer/account/geofence.md` — new "Member visibility (group-scoped)"
  subsection alongside Config Plane (:205) / Evidence Plane (:241): audience split,
  endpoint + keys and why they're the Event keys, why the payload is narrow, event
  attribution reality (`reporter._resolve_event_group` falls back to `request.group`),
  and that `geofence_config` remains platform-only.
- `docs/django_developer/core/permissions.md` — update the geofence notes (:133–134,
  :187): geofence *config* keys remain global-only; member `view_security`/`security`
  now additionally grants `geo/policy` + own-group geofence events.
- `CHANGELOG.md` — feature entry.
- No new doc files → no README index changes.

### Open questions

None. (Portal follow-up — pointing mverify_portal's policy card at `geo/policy` — is
filed in that repo, not here.)

## Notes

- **Not urgent / not blocking**: the platform-admin surface (ITEM-017 +
  mverify_portal ITEM-014) already fully serves the compliance-evidence use
  case (Coinflow, etc.). This is a distinct audience/capability, not a fix.
- **Portal-side consequence, once this lands**: mverify_portal's
  `GeofencingPage.js` already degrades per-card by permission (built that
  way deliberately) — a group-scoped grant holder visiting the SAME page
  today would see "Not permitted" on every card. Once a group-scoped read
  path exists, either (a) the existing page's permission checks widen to
  accept the new group-scoped grant and the page naturally lights up scoped
  to their own group, or (b) a distinct member-facing page is filed
  separately in mverify_portal — decide once the backend shape is chosen.
- **Design trap to avoid**: do not just relax `requires_global_perms` on the
  existing `/api/geo/rules` endpoint — its current response includes
  platform-wide operational detail (posture internals, allowlist counts,
  `enforced_endpoints` for every scope) that a single brand's staff should
  not see. The response shape for a member-scoped caller needs to be
  deliberately narrower, not just differently authorized.
- Sibling filed same day: `geofence-group-scoped-metrics.md` (dual-write
  `account=group-<id>` for block/exempt metrics) — related but distinct:
  that item is about WHERE metrics are recorded; this item is about WHO can
  read policy/events for their own group. Both are needed for a fully
  group-scoped experience.
