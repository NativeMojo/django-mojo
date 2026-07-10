---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: ApiKey.validate_token grants group context without checking the key's group is_active — deactivated tenants' keys keep working
priority: P2
effort:
owner:
opened: 2026-07-10
depends_on: []
related: [ITEM-025, ITEM-019]
links: []
---

# ApiKey.validate_token grants group context without checking the key's group is_active — deactivated tenants' keys keep working

## What & Why
`ApiKey.validate_token` (`mojo/apps/account/models/api_key.py:272-303`) checks
`api_key.is_active` and `expires_at` (line 286) but never
`api_key.group.is_active`, then unconditionally sets
`request.group = api_key.group` (line 292). Deactivating a group therefore
does NOT cut off its machine credentials: the ordinary key request (no
explicit `group=` param) keeps full group-scoped access to the deactivated
tenant's data. ITEM-025's active-only resolution only bites when a request
explicitly passes `group=<id>` — the dispatcher then clobbers `request.group`
to None and model security fails closed. Docs now state the honest behavior
and workaround ("deactivate the key itself") — commit aca2fab — but the
fail-closed expectation is that tenant deactivation suspends its keys.

Secondary hardening from the same review (latent, not currently exploitable):
the ITEM-019 groupless-ApiKey branch (`mojo/models/rest.py:288-311`) denies by
default, but if a future group-scoped model ever sets
`RestMeta.ALLOW_API_KEY_GLOBAL = True`, a key could reach UNSCOPED access by
supplying any inactive group id (dispatcher yields request.group=None and its
`is_group_allowed` confinement check is skipped when group is None). No model
sets the flag today. Add a guard (or loud assertion) against combining a
`group` FK with `ALLOW_API_KEY_GLOBAL=True` on one model.

## Acceptance Criteria
- [ ] A key whose group is inactive fails authentication-time group context: either the token is rejected outright or `request.group` is not set and model security fails closed — decide + document which (product decision: is reactivating a group expected to instantly restore its keys?).
- [ ] Ordinary key requests (no `group=` param) against a deactivated group's data are denied.
- [ ] Keys of active groups are unaffected (including child-group access via `is_group_allowed`).
- [ ] Guard/assertion prevents `group` FK + `ALLOW_API_KEY_GLOBAL=True` on one model (or the combination is explicitly documented as forbidden).
- [ ] Docs in both tracks updated to replace the "deactivate the key itself" workaround with the new behavior.
- [ ] Regression test: key of a deactivated group → denied on a group-scoped endpoint without any `group=` param.

## Repro — bugs only
1. Create group G + `ApiKey.create_for_group(G, ...)` with a working permission.
2. Set `G.is_active=False`.
3. Call any group-scoped endpoint with `Authorization: apikey <token>` and NO `group=` param.
- Expected: denied (deactivated tenant's credentials suspended).
- Actual: full normal access — `request.group = G` straight from `validate_token`.

## Investigation
Traced by the ITEM-025 post-build security review (2026-07-10) — confidence:
**high** (code-path reading of api_key.py:272-303 + rest.py:288-311; re-verify
during /scope). Pre-existing; ITEM-025 documented it rather than fixing it
(auth-time behavior is out of a dispatcher-resolution item's scope).
Regression-test feasibility: high — apikey client patterns exist
(`tests/test_global_perms/_helpers.py:use_apikey`, `tests/test_user_mgmt/api_keys.py`).

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Decide the failure mode carefully: rejecting the token entirely (401) vs stripping group context (403 at model security). 401 is cleaner but changes auth semantics; group-context stripping matches ITEM-025's shape.
- Check the geoip federation receiver (`allow_api_keys=True` surface) for interaction before changing validate_token.
