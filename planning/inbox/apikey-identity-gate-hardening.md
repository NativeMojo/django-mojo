---
# id is assigned by /scope on pickup — leave it blank
id:
type: chore
title: Harden the DM-037 identity gates — enforce the inactive-group invariant above instance hooks, unify the two machine-identity idioms, extract the duplicated decorator gate, drop the dead group_id guard
priority: P3
effort:
owner:
opened: 2026-07-16
depends_on: []
related: [DM-037, DM-019, DM-016]
links: []
---

# Harden the DM-037 identity gates — enforce the inactive-group invariant above instance hooks, unify the two machine-identity idioms, extract the duplicated decorator gate, drop the dead group_id guard

## What & Why
Four hardening/cleanup items from the DM-037 post-close code review
(adversarially verified 2026-07-12; sites unchanged as of 2026-07-16). None
is an exploitable bug today — they are structural weaknesses that make the
next change likelier to reopen the class DM-037 closed.

1. **The inactive-group invariant on detail ops is per-hook convention, not
   structural.** `_evaluate_permission` dispatches to instance hooks
   (`check_view_permission` rest.py:~260, `check_edit_permission` :~270),
   which return BEFORE the `api_key.group_inactive` gate (:~299). In-repo
   `Group` was patched via `is_group_allowed`'s active check, but any future
   model hook that grants via `api_key.has_permission(perms)` directly (the
   obvious pattern to copy — it was Group's own pre-DM-037 shape) silently
   bypasses the gate on detail GET/save/delete: the exact self-reversible-
   suspension class DM-037's follow-up fixed. Fix at the right altitude:
   gate the resolved instance's group for api_key identities BEFORE
   dispatching hooks in `_evaluate_permission` (or, minimally, a contract
   test asserting every group-scoped instance hook's api_key branch routes
   through `is_group_allowed`).
2. **Two machine-identity idioms guard one invariant.** The decorators key
   on `not hasattr(request.user, "is_request_user")`
   (`mojo/decorators/auth.py:42,99`); model security keys on
   `hasattr(request, 'api_key') and request.api_key`
   (`mojo/models/rest.py:~298,~326`). They coincide today (ApiKey is the
   only machine identity and validate_token always sets `request.api_key`),
   but a future non-User bearer identity that doesn't set `request.api_key`
   would be fail-closed at the decorators yet routed to rest.py's USER
   branch, where its self-claimed perms authorize with no group confinement
   and no inactive-group gating — the DM-019/DM-037 protections silently
   would not apply. Align both layers on one predicate (the
   `is_request_user` marker / a shared helper).
3. **The decorator gate is copy-pasted verbatim** (`auth.py:42-46` and
   `:99-103`) — a security gate whose next edit lands in one clone and not
   the other. Extract one module-level helper. The helper is also the right
   home for the `or not group.is_active` half-condition, which is currently
   unreachable (every pre-decorator `request.group` assignment is
   active-only) but is worthwhile defense-in-depth on a security boundary —
   keep it, documented once, instead of implied-load-bearing twice.
4. **Dead guard in validate_token:** `api_key.group_id and` at
   `api_key.py:~324` can never be falsy — the group FK is non-nullable
   (api_key.py:~39, no `null=True`) and the row is always DB-loaded via
   `select_related("group")`. The condition implies a fictional null-group
   key variant a future reader may design around (or "complete" by making
   the FK nullable). Simplify to
   `api_key.group if api_key.group.is_active else None`.

## Acceptance Criteria
- [ ] For api_key identities, an inactive resolved instance-group is denied
      BEFORE instance hooks run (or a contract test enforces the
      is_group_allowed routing convention on every group-scoped hook) — with
      a regression test using a synthetic model/hook that grants via bare
      `api_key.has_permission`.
- [ ] One shared machine-identity predicate used by both `auth.py`
      decorators and `mojo/models/rest.py` branches; behavior for ApiKey
      unchanged (full suite green, DM-037 regression suite green).
- [ ] The decorator gate exists once (helper), with the defense-in-depth
      `is_active` half-condition kept and documented there.
- [ ] `validate_token`'s dead `group_id` guard removed.
- [ ] No behavior change for real Users, active-group keys, or the
      federation path (`requires_global_perms(..., allow_api_keys=True)`).

## Repro — bugs only
n/a (chore — no current misbehavior; item 1's scenario is only reachable
with a hypothetical future hook, which the regression test will simulate).

## Investigation
All four traced with file:line evidence and verdicts (3× CONFIRMED, 1×
CONFIRMED-forward-looking) by the DM-037 post-close review — see
`mojo/models/rest.py:260-278` vs `:297-309` (hook-before-gate ordering),
`auth.py:42-46/99-103` (verbatim clones; second copy's comment already says
"Same ... gate as requires_perms"), `user.py:297` (`is_request_user` is the
canonical User marker — the framework idiom per DM-016), `api_key.py:39`
(non-nullable FK) + `:305` (select_related load). Constraint to respect:
`ApiKey.has_permission` itself must NOT gain group/active awareness — the
federation path (`requires_global_perms`, geoip `/sync`) depends on it
working with no group context (DM-037 owner ruling).

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Two review leftovers deliberately NOT in scope here: test-fixture
  boilerplate in `tests/test_global_perms/apikey_group_inactive.py` (nice-to-
  have contextmanager, fold in only if touching that file anyway) and the
  `event_type` taxonomy nit on the `api_key.group_inactive` denial
  (PLAUSIBLE-only; matches the adjacent groupless-denied branch, so arguably
  deliberate).
- The unthrottled `ALLOW_API_KEY_GLOBAL` logit.error + wrong "group FK"
  message are in the sibling bug item (`apikey-suspension-residual-surfaces.md`),
  not here.
