---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-025
type: bug
title: Dispatcher numeric group= resolution skips is_active (group_uuid path filters it)
priority: P3
effort: S
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-020, ITEM-024]
links: []
---

# Dispatcher numeric group= resolution skips is_active (group_uuid path filters it)

## What & Why

The REST dispatcher resolves `request.group` from two client-supplied params
(`mojo/decorators/http.py:69-111`), and the two branches disagree:

- `group=<int>` (`http.py:74-91`): `modules.get_model_instance("account",
  "Group", int(...))` — **no `is_active` filter**, and it calls
  `request.group.touch()` on whatever it finds.
- `group_uuid=<uuid>` (`http.py:101-111`): `Group.objects.filter(uuid=...,
  is_active=True)` — with an explicit SECURITY comment (`http.py:96-100`)
  explaining why: inactive groups must never become `request.group` via a
  public path (touch side-effect = existence disclosure; inactive groups
  shouldn't be resolvable at all).

The numeric path contradicts the documented security rationale of its sibling
branch: an unauthenticated caller can make an **inactive** group the request
group by integer id — it gets touched (modified-timestamp side effect /
existence oracle), its geofence rules participate in the decision, and (since
ITEM-020) evidence metrics are attributed to its `group-<id>` account.

Surfaced by the ITEM-020 post-build security review. Same review's product
note, worth deciding while here: on public auth surfaces the group param has
no membership check (by design — white-label flows), so any existing group id
can be *attributed* activity by an anonymous blocked caller. ITEM-020
documented per-group geofence counters as "reported activity, not verified
counts"; if stronger integrity is ever wanted, this dispatcher choke point is
where it would go.

## Acceptance Criteria

- [ ] The numeric `group=` branch resolves only active groups (matching the
      `group_uuid` branch), or a deliberate, documented decision is recorded
      for why inactive groups must remain resolvable by id (and if so, at
      minimum the `touch()` side effect on inactive groups is removed).
- [ ] Behavior change is verified against authenticated flows that pass
      `group=<id>` legitimately (REST list/detail with group context, member
      endpoints) — no regression for active groups.
- [ ] Regression test: request with an inactive group's id → `request.group`
      is None (and no `modified` bump on the inactive group).

## Repro — bugs only

1. Create a group, set `is_active=False`.
2. Send any mojo REST request with `group=<that id>` (e.g. as a query param).
- Expected: `request.group` stays None (as it would via `group_uuid`).
- Actual: the inactive group becomes `request.group` and gets `touch()`ed.

## Plan

### Goal
A client-supplied numeric `group=<id>` resolves `request.group` for **active** groups only — an inactive group's id yields `request.group = None` exactly like a nonexistent id (no `touch()`, no `modified` bump, no existence oracle), matching the `group_uuid` branch's documented security contract, at all three resolution sites (dispatcher + both auth-decorator fallbacks).

### Context — what exists
All line refs current post-ITEM-024 (which added the `(TypeError, ValueError)` guards in these exact regions — do not disturb that behavior).

- **The three fix targets** — the only `get_model_instance("account", "Group", ...)` call sites in the repo, all identical, none filtering `is_active`:
  1. `mojo/decorators/http.py:76` (dispatcher numeric branch) — resolves pre-auth on every dispatched request, then `touch()`es (http.py:77-78). The surrounding structure already handles a `None` result: `if request.group is not None: request.group.touch()` and the api_key check is `if api_key and request.group and ...` — so returning `None` for inactive needs no other changes in the block.
  2. `mojo/decorators/auth.py:44` (`requires_perms` fallback) — re-resolves when the dispatcher left `request.group` unset (`if "group" in request.DATA and not request.group:` auth.py:42). **This is why the dispatcher alone is not enough**: post-dispatcher-fix, an inactive id leaves `request.group=None`, and this fallback would re-resolve the same inactive group and authorize a member-scoped grant against it. Falls through to `if not request.group or not request.group.user_has_permission(...): raise PermissionDeniedException()` (auth.py:48) — `None` already fails closed.
  3. `mojo/decorators/auth.py:90` (`requires_group_perms` fallback) — identical shape (deny at auth.py:94). Zero callers in `mojo/` today, but exported and API-symmetric; fix for consistency.
- **The sibling contract to match** — `group_uuid` branch, `mojo/decorators/http.py:104-114`: `Group.objects.filter(uuid=..., is_active=True).first()`, with the SECURITY comment at http.py:99-103 (inactive groups must never become `request.group` via a public path; touch side effect = existence disclosure).
- **`modules.get_model_instance`** (`mojo/helpers/modules.py:94-95`) is generic — `get_model(app, model).objects.filter(id=pk).last()` — used across many models. Do NOT add is_active there; fix at the Group call sites.
- **`Group.touch()`** (`mojo/apps/account/models/group.py:223-229`): sets `last_activity` then `atomic_save()` → full `save()` with **no update_fields** (`mojo/models/rest.py:1569-1574`), so `modified` (auto_now) bumps too. Throttled by `GROUP_LAST_ACTIVITY_FREQ` = 300s (group.py:15): writes when `last_activity is None` or >300s stale — a never-touched inactive group ALWAYS writes on first probe. Tests must control `last_activity` to defeat the throttle.
- **No existing "active group by id" helper on Group** — the only active-filtering classmethod is `resolve_by_auth_domain` (group.py:745-775, hostname-keyed). `LIST_DEFAULT_FILTERS = {"is_active": True}` (group.py:54-56) applies to REST list only, not detail-by-pk.
- **Blast radius verdict: NO flow needs numeric `group=` to resolve an inactive group.**
  - Admin disable/reactivate goes through `/api/group/<pk>` (URL pk, not the `group=` param) via `POST_SAVE_ACTIONS` `on_action_disable`/`on_action_reactivate` (group.py:600-631), authorized on **global** `manage_groups` — proven by `tests/test_account/test_disable_lifecycle.py:542-569`, which POSTs `{"disable": {...}}`/`{"reactivate": {...}}` with no `group=` param. Detail-by-pk does not apply LIST_DEFAULT_FILTERS, so admins keep seeing inactive groups.
  - Bouncer white-label (`mojo/apps/account/rest/bouncer/views.py:36-68`) and OAuth begin (`oauth.py:80-98`) are uuid-driven and already active-only; public registration (`user.py:306-310`) already rejects inactive.
  - `geo/check` (`mojo/apps/account/rest/geofence.py:67-88`) deliberately surfaces inactive groups (system-only eval + `group_inactive` hint) via its OWN `Group.objects.filter(uuid=...)` lookup, independent of the dispatcher — untouched by this fix. Admin inspection of inactive groups stays on `geo/rules`/`geo/simulate` via `_resolve_group_param` (geofence.py:135-146, uuid-driven, `requires_global_perms`-gated, deliberate per its comment).
- **ApiKey interplay**: `ApiKey.validate_token` sets `request.group = api_key.group` server-side; the dispatcher numeric branch then unconditionally overwrites `request.group` with its resolved value when a `group=` param is present (http.py:76) — today that already clobbers to `None` for a nonexistent id; post-fix an inactive id behaves identically. A deactivated group's key loses group context → groupless model security fails closed (ITEM-019) — correct.
- **Test fixtures to reuse**:
  - Member-with-perms: `tests/test_global_perms/_helpers.py:46-61` (`Group.objects.create(name=..., kind="organization")`; `GroupMember.objects.get_or_create(user=..., group=...)`; `member.permissions = {p: True ...}; member.save()`), or the `member_visibility.py:75-80` `add_permission("view_security")` style.
  - The behavioral endpoint: `GET /api/geo/policy` (`mojo/apps/account/rest/geofence.py:91-125`, `@md.requires_perms("view_security", "security")`) reads `request.group` and authorizes via the member fallback; `tests/test_geofence/member_visibility.py:99-142` already pins its 403-no-oracle contract for uuids.
  - ITEM-024's `tests/test_middleware/request_data_merge.py` covers the coercion guards ('Invalid group ID' 400, fail-closed on garbage) — extend, don't duplicate.
  - No existing test asserts Group `modified`/`last_activity` (non-)bump — write fresh assertions; snapshot before, refetch after.

### Changes — what to do
1. `mojo/apps/account/models/group.py` — add a small classmethod, the single place that owns the invariant:
   ```python
   @classmethod
   def get_active(cls, pk):
       """Resolve a client-supplied numeric group id to an ACTIVE group, else None.
       Inactive ids resolve exactly like nonexistent ones (no existence oracle)."""
       return cls.objects.filter(pk=pk, is_active=True).first()
   ```
2. `mojo/decorators/http.py:76` — numeric branch: local-import Group (same circular-import pattern the uuid branch uses at http.py:105) and replace `modules.get_model_instance("account", "Group", int(request.DATA.group))` with `Group.get_active(int(request.DATA.group))`. Keep `int()` at the call site so ITEM-024's `(TypeError, ValueError)` → 400 path is untouched. Extend the uuid branch's SECURITY comment (http.py:99-103) or add a mirror note on the numeric branch so the contract is stated where both branches live.
3. `mojo/decorators/auth.py:44` and `:90` — same replacement (`Group.get_active(int(request.DATA.group))`, local import inside the wrapper). The existing `except (TypeError, ValueError): request.group = None` and the `not request.group → PermissionDeniedException` deny path stay exactly as they are.
4. `tests/test_middleware/group_param_is_active.py` — new test file (see Tests).
5. Docs + `CHANGELOG.md` — see Docs.

### Design decisions
- **`Group.get_active(pk)` classmethod, not inline filters at 3 sites** — one owner for a security invariant beats three copies (same "guard the choke point" reasoning as ITEM-016); `get_model_instance` stays generic because it serves many models.
- **Inactive resolves to silent `None` (= nonexistent), NOT a 400/404** — a distinct error for inactive-vs-nonexistent would build the very existence oracle the uuid branch's comment forbids. Matches `group_uuid` semantics exactly.
- **`touch()` for active groups unchanged** — the activity metric and throttle are intentional; only the inactive slice loses the side effect (by never resolving).
- **`requires_group_perms` fixed despite zero callers** — exported API, identical latent hole; two-line change.
- **Out of scope — `group/<pk>/member` endpoint** (`mojo/apps/account/rest/group.py:92-100`): also resolves a client pk without `is_active` and touches, but it is post-auth (`@requires_auth`) and admins may legitimately need member lists of inactive groups (pre-reactivation review); gating it needs its own product decision. Left in Notes as a follow-up candidate.
- **Out of scope — anonymous attribution integrity** (the ITEM-020 product note): callers choosing any *active* group's context on public surfaces is the white-label design; ITEM-020 already documents per-group counters as "reported activity, not verified counts." This item only closes the inactive slice. Recorded as the deliberate decision the acceptance criteria asked for.

### Edge cases & risks
- **Active-group flows must not regress** (AC #2): resolution result for active ids is byte-identical (`filter(pk=..., is_active=True).first()` vs `filter(id=...).last()` — pk is unique, so last/first are the same row); touch still fires; member-fallback perms still authorize. Covered by the control assertions in the tests + full-suite baseline comparison.
- **Parent/child**: `get_active` filters only the named group's own flag — parent-chain traversal (`check_parents` etc.) is downstream and unchanged, same as the uuid branch today.
- **`group=0` / negative / garbage**: `0` is falsy → branch skipped (unchanged); negative → no match → None (unchanged); non-numeric → ITEM-024's 400 (unchanged, already tested).
- **ApiKey with its own group deactivated**: numeric `group=` now clobbers to None instead of resolving inactive → groupless fail-closed model security (ITEM-019). Deny-consistent; no legitimate active-key flow changes.
- **Touch throttle vs tests**: set `last_activity=None` (or stale) on fixture groups before asserting bump/no-bump, else the 300s throttle masks the signal.
- **geo/check contract intact**: it never depended on dispatcher resolution for inactive groups (own lookup) — its `group_inactive` behavior must still pass (existing `test_geofence` suite covers it; baseline comparison catches any surprise).

### Tests
New file `tests/test_middleware/group_param_is_active.py` (testit; run `bin/run_tests --agent -t test_middleware.group_param_is_active`). Setup (`@th.django_unit_setup()`, delete-before-create): one user (self-save pattern from ITEM-024's file), one ACTIVE group, one INACTIVE group (`is_active=False`), both with `last_activity=None`; plus a member-perms user with `view_security` GroupMember grants in a second inactive group (fixture pattern `tests/test_global_perms/_helpers.py:46-61`).
1. **Dispatcher regression (THE bug)**: logged-in self POST `/api/user/<pk>?group=<inactive_pk>` (benign body) → **200** (request proceeds; inactive behaves like nonexistent, not an error), AND refetched inactive group's `last_activity` is still None and `modified` unchanged (no touch — FAILS pre-fix, where touch writes both).
2. **Active control (AC #2)**: same POST with `?group=<active_pk>` → 200 AND `last_activity` transitioned None → set (touch still fires for active groups).
3. **Nonexistent-id equivalence**: same POST with `?group=999999999` → 200 (silent None today and after — pins the no-oracle equivalence).
4. **auth.py member-fallback leak (fails pre-fix)**: member-perm user (`view_security` in the INACTIVE group), `GET /api/geo/policy?group=<inactive_pk>` → **403** (pre-fix: dispatcher resolves the inactive group, member grant authorizes, 200 + policy payload for a deactivated tenant). Control: same user with the grant in an ACTIVE group, `?group=<active_pk>` → 200 (numeric member fallback keeps working).
5. **`requires_group_perms` in-process** (no HTTP callers exist): real user with a real GroupMember perm grant in an inactive group; decorate a dummy view, call the wrapper with `objict(user=<real User>, DATA=objict(group=str(inactive_pk)), group=None)` → expect `PermissionDeniedException` (pre-fix: the inactive group resolves, the member grant passes, the dummy executes — test fails). Mirrors ITEM-024's in-process wrapper test at `tests/test_middleware/request_data_merge.py:178-198`.
Bug discipline: tests 1, 4, 5 must FAIL on unfixed code; 2, 3 are controls.

### Docs
- `docs/django_developer/core/middleware.md:131` — `request.group` auto-population: add "and the group is active — an inactive group's id never resolves (same as nonexistent)".
- `docs/django_developer/account/group.md:240-248` — `## request.group` section: same active-only note.
- `docs/django_developer/account/geofence.md:309-312` — already states the uuid branch is active-only; extend the sentence to cover numeric `group=`.
- `docs/django_developer/account/api_keys.md:51-55` — dispatcher `group=<id>` description: inactive ids resolve to no group context.
- `docs/django_developer/core/decorators.md:118-121` — `requires_perms` group fallback: resolves active groups only, unusable/inactive → deny.
- `docs/web_developer/core/authentication.md:53-61` and `docs/web_developer/account/group.md:19-27` — Group Context sections: an inactive group's id does not resolve; requests behave as if no group was passed.
- `docs/web_developer/account/geofence.md:48-50` — mirror the existing uuid wording for numeric `group=`.
- `CHANGELOG.md` (Unreleased) — **security** entry: numeric `group=` now matches `group_uuid` (active-only, no touch/oracle on inactive), including the `requires_perms`/`requires_group_perms` fallback. (ITEM-025)

### Open questions
- none

## Notes

- Baseline (2026-07-09, before first edit): `bin/run_tests --agent` → **GREEN** — total 2382, passed 2326, failed 0, skipped 56 (status "passed", failures []). Anything red after the change is this item's.
- Recon verdict (2026-07-09): no flow in mojo/ or tests/ relies on numeric `group=` resolving an inactive group — admin lifecycle uses `/api/group/<pk>` + global perms; uuid surfaces are already active-only; `geo/check`/`geo/rules`/`geo/simulate` do their own deliberate inactive handling independent of the dispatcher.
- Follow-up candidate (NOT this item): `group/<pk>/member` (`mojo/apps/account/rest/group.py:92-100`) resolves a client pk without `is_active` and `touch()`es — post-auth, and inactive-member-listing may be legitimate for admins; needs its own decision.
- The api_key confinement inside both branches (`is_group_allowed`) is unaffected — it only fires for key-authenticated requests, and both checks are already None-guarded.
- Deliberate decision recorded (per AC option 1): active-only resolution, silent None for inactive. The alternative in AC ("keep inactive resolvable, drop touch") was rejected — it would leave a deactivated tenant's geofence rules + metrics attribution selectable by anonymous callers, contradicting the uuid branch's stated contract.
- Post-build agents (2026-07-10): test-runner green (2331/0, +5); docs-updater corrected the api_keys.md fail-closed overstatement + auth.md step 5 (agent died mid-report; edits verified + committed in aca2fab). Security review: fix itself clean — anti-oracle verified across dispatcher/decorators/incident-events; zero unfiltered `get_model_instance("account","Group")` remain — but 2 WARNINGs on ADJACENT pre-existing gaps that undercut the original claim: RestMeta LIST member resolution (`get_groups_with_permission`, user.py:406-447) and `ApiKey.validate_token` group context (api_key.py:272-303) both ignore `Group.is_active`; plus the known `group/<pk>/member` outlier. CHANGELOG narrowed (aca2fab); all three filed as inbox items: `member-perms-ignore-group-is-active` (P2), `apikey-group-context-ignores-group-is-active` (P2), `group-me-member-endpoint-oracle-touch` (P3).

## Resolution
- closed: 2026-07-10
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/README.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/auth.md,docs/django_developer/account/geofence.md,docs/django_developer/account/geoip.md,docs/django_developer/account/group.md,docs/django_developer/core/decorators.md,docs/django_developer/core/middleware.md,docs/django_developer/core/permissions.md,docs/django_developer/helpers/settings.md,docs/django_developer/helpers/settings_reference.md,docs/django_developer/logging/incidents.md,docs/django_developer/security/README.md,docs/django_developer/testit/Overview.md,docs/web_developer/account/README.md,docs/web_developer/account/admin_portal.md,docs/web_developer/account/geofence.md,docs/web_developer/account/geoip.md,docs/web_developer/account/group.md,docs/web_developer/account/login_events.md,docs/web_developer/core/authentication.md,docs/web_developer/core/request_response.md,docs/web_developer/security/README.md,memory.md,mojo/__init__.py,mojo/apps/account/models/group.py,mojo/apps/account/models/setting.py,mojo/apps/account/rest/geofence.py,mojo/apps/account/services/geofence/engine.py,mojo/apps/account/services/geofence/evidence.py,mojo/apps/incident/asyncjobs.py,mojo/apps/incident/cronjobs.py,mojo/apps/incident/migrations/0031_alter_ipset_source.py,mojo/apps/incident/models/ipset.py,mojo/decorators/auth.py,mojo/decorators/http.py,mojo/helpers/geoip/detection.py,mojo/helpers/geoip/threat_intel.py,mojo/helpers/request_parser.py,mojo/helpers/settings/helper.py,mojo/rest/info.py,planning/.next_id,planning/done/ITEM-020-geofence-evidence-metrics-dual-write-group-scoped-.md,planning/done/ITEM-021-geofence-hardening-opt-in-strict-compliance-enforc.md,planning/done/ITEM-022-member-readable-geofence-policy-events-group-scope.md,planning/done/ITEM-023-geofence-adjacent-settings-bypass-write-time-valid.md,planning/done/ITEM-024-same-key-in-query-string-json-body-merges-to-a-lis.md,planning/in_progress/ITEM-025-dispatcher-numeric-group-resolution-skips-is-activ.md,planning/inbox/geofence-hardening.md,planning/inbox/geofence-settings-write-validation-gap.md,pyproject.toml,tests/test_geofence/_helpers.py,tests/test_geofence/evidence_plane.py,tests/test_geofence/member_visibility.py,tests/test_geofence/settings_validation.py,tests/test_geofence/strict_posture.py,tests/test_geofence/threat_cache.py,tests/test_helpers/settings_coercion.py,tests/test_middleware/group_param_is_active.py,tests/test_middleware/request_data_merge.py,uv.lock
- tests added: tests/test_middleware/group_param_is_active.py — 5 tests: inactive id not resolved/not touched (last_activity + modified unchanged; THE regression), active id still resolves + touches (control), nonexistent id silently no-group (oracle-equivalence control), member grant in inactive group denied on geo/policy via numeric group= (403; active control 200), requires_group_perms in-process fail-closed with a real member grant in an inactive group. The 3 regressions confirmed FAILING pre-fix; 5/5 passing post-fix.
