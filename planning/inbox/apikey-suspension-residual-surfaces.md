---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: DM-037 suspension guarantee has residual surfaces — no-param fallback lists active descendants (incl. raw key tokens), rotate//me self-service stays live, ALLOW_API_KEY_GLOBAL guard message wrong for GROUP_FIELD models
priority: P2
effort:
owner:
opened: 2026-07-16
depends_on: []
related: [DM-037]
links: []
---

# DM-037 suspension guarantee has residual surfaces — no-param fallback lists active descendants (incl. raw key tokens), rotate//me self-service stays live, ALLOW_API_KEY_GLOBAL guard message wrong for GROUP_FIELD models

## What & Why
DM-037 shipped the "deactivating a group instantly suspends its API keys"
contract and docs that state it unqualified
(`docs/web_developer/account/api_keys.md` ~line 68: "every group-scoped
request (list, detail, save, delete, custom endpoints, with or without a
`group=` param) is denied"). The DM-037 post-close code review
(adversarially verified 2026-07-12; finding sites confirmed unchanged as of
2026-07-16, and v1.2.49 is still unreleased) found three places where
behavior and that documented contract diverge:

1. **No-param list fallback enumerates a suspended parent's ACTIVE
   descendants — including their raw key tokens.** With parent P inactive
   and child C active, P's key sends `GET /api/group/apikey` with NO params:
   `rest_check_permission` correctly denies, but `on_rest_handle_list`'s
   group fallback (`mojo/models/rest.py:540-546`) calls
   `ApiKey.get_groups_with_permission` → `get_groups`
   (`mojo/apps/account/models/api_key.py:~230-238`), which computes
   `{self.group_id} ∪ child_ids` THEN filters `is_active=True` — yielding
   exactly the active children. The list is served with `group__in={C}`, and
   the ApiKey default graph exposes each child key's raw token via
   `"extra": [("get_token", "token")]` (api_key.py RestMeta ~line 66).
   Exposure is confined to the key's own subtree (no cross-tenant leak), but
   the docs/changelog say child access is via **explicit** `group=<child id>`
   only. Efficiency rider: the recursive `_get_all_child_ids` walk runs
   before any own-group active check — wasted N+1 queries per poll from a
   suspended key.
2. **`requires_auth`-only self-service endpoints still serve a suspended
   key.** `POST /api/group/apikey/rotate`
   (`mojo/apps/account/rest/api_key.py:37-62`, `@md.requires_auth` only)
   mints and persists a fresh secret for a suspended tenant's key, and
   `GET /api/group/apikey/me` (same file, ~13-35) returns 200 including the
   inactive group's `basic` sub-graph (`"graphs": {"group": "basic"}`,
   api_key.py ~74-83) — name/state of a supposedly cut-off tenant served to
   a supposedly cut-off credential. Low practical severity (the rotated
   token is equally suspended) but a direct doc contradiction.
3. **`ALLOW_API_KEY_GLOBAL` guard message/docs are wrong for GROUP_FIELD
   models, and its error log is unthrottled.** The guard keys on
   `is_group_scoped` (`mojo/models/rest.py:232` — `bool(GROUP_FIELD) or
   hasattr(cls, "group")`) so a `RestMeta.GROUP_FIELD`-only model (no group
   FK) trips it, yet the message (rest.py:~343 "Remove the flag or the group
   FK"), CHANGELOG, and `docs/django_developer/account/api_keys.md` all say
   "group FK". Failing closed for GROUP_FIELD models is arguably correct —
   the guidance is what's wrong. And the `logit.error` (rest.py:~341) fires
   on every denied request with no once-per-class memo: one misconfigured
   model polled at 10 req/s ≈ 860k identical error lines/day on the
   permission hot path.

## Acceptance Criteria
- [ ] Decide the no-param fallback contract and make behavior + docs agree.
      Recommended: `ApiKey.get_groups` returns `Group.objects.none()` when the
      key's OWN group is inactive (also fixes the wasted child-walk), keeping
      the explicit `group=<child id>` path (dispatcher `get_active` +
      `is_group_allowed`) as the only child access — matching the shipped doc
      wording. Alternative: keep the fallback and soften the docs.
- [ ] Regression: suspended parent P with active child C — P's key gets no
      rows (and no tokens) from `GET /api/group/apikey` with no params;
      explicit `group=<C.pk>` still works (approved DM-037 boundary).
- [ ] Decide + implement rotate//me behavior for a suspended-group key:
      either deny (aligning with the docs) or explicitly document the
      carve-out. If /me stays open, consider omitting the group sub-graph for
      an inactive group.
- [ ] `ALLOW_API_KEY_GLOBAL` guard: message/docs updated to name GROUP_FIELD
      scoping too ("group FK or RestMeta.GROUP_FIELD"), and the logit.error
      throttled to once per class per process (mirror
      `_warn_can_save_deprecated`, rest.py:43-56).
- [ ] Both doc tracks re-aligned with whatever contract is chosen; CHANGELOG
      entry (fold into the unreleased v1.2.49 block if still unshipped).
- [ ] No regression for active-group keys (existing
      tests/test_global_perms/apikey_group_inactive.py suite stays green).

## Repro — bugs only
1. Create groups P (organization) and C (team, parent=P);
   `ApiKey.create_for_group(P, permissions={"groups": True})`.
2. `P.is_active = False; P.save()`.
3. With `Authorization: apikey <token>` and NO `group=` param:
   `GET /api/group/apikey`.
- Expected (per shipped docs): denied (401/403).
- Actual: 200 listing C's API keys, each including its raw token.
4. Same key: `POST /api/group/apikey/rotate` → Expected per docs: denied;
   Actual: 200 with a fresh secret. `GET /api/group/apikey/me` → 200 with
   the inactive group's basic graph.

## Investigation
Confidence: **confirmed** — each surface was traced and adversarially
verified by the DM-037 post-close review (2026-07-12): mechanics quoted at
`mojo/models/rest.py:540-546` (fallback), `mojo/apps/account/models/api_key.py`
`get_groups` (~230-238, filter after union) and RestMeta graph extra (~66),
`mojo/apps/account/rest/api_key.py:37-62` + `~13-35` (`requires_auth`-only,
`mojo/decorators/auth.py` requires_auth checks only `is_authenticated`),
`mojo/models/rest.py:232/337-345` (guard + unthrottled log). Verified
still-current 2026-07-16 (`git diff 3a74187..HEAD` empty on all finding
files). Two adjacent candidates were REFUTED during the same review and are
NOT part of this item: the `VIEW_PERMS=["all"]` "lists all tenants" scare
(no 'all' short-circuit exists; fallback serves active-subtree only) and the
"suspended key creates orphaned shortlinks" claim (ApiKey fails the User FK
type check before insert). Regression-test feasibility: high —
`tests/test_global_perms/apikey_group_inactive.py` already has the exact
parent/child + suspended-key fixtures to extend.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Sibling items filed from the same review: parent-key one-way door
  (design question, separate item) and the identity-gate hardening chore.
  The user-identity detail re-bind hole was folded into the existing inbox
  item `member-perms-ignore-group-is-active.md`.
- If the recommended `get_groups → none()` fix lands, re-check
  `Group.on_rest_handle_list` (group.py:810-820) and the assistant tools
  (`assistant/services/tools/metrics.py:296`, `models.py:389`) — all route
  through the same derivation and inherit the fix.
