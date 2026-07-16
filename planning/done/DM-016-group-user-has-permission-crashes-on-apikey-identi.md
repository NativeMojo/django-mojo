---
# id is assigned by /scope on pickup — leave it blank
id: DM-016
type: bug
title: Group.user_has_permission crashes on ApiKey identity (filter(user=apikey) → "Must be User instance")
priority: P1
effort: S
owner: backend
opened: 2026-07-06
depends_on: []
related: []            # surfaced by mojo-verify MVERIFY-API-010 (payments processor admin via API key)
links: []
---

# Group.user_has_permission crashes on ApiKey identity (filter(user=apikey) → "Must be User instance")

## What & Why

Passing an **ApiKey identity** into `Group.user_has_permission(user, perms)` can
reach a member lookup that runs `GroupMember.objects.filter(user=<ApiKey>)`, which
Django rejects because the FK target is `account.User`:

```
Cannot query "ClaudeTest@Club Axo - Stage": Must be "User" instance.
```

(`"ClaudeTest@Club Axo - Stage"` is `ApiKey.__str__` → `f"{self.name}@{self.group}"`,
confirming a bare `ApiKey` instance reaches the ORM query.)

Discovered live against `api.mojoverify.com` while building mojo-verify
MVERIFY-API-010 Phase A. The mojo-verify caller is a `POST_SAVE_ACTIONS` handler
that re-checks permissions group-aware:

```python
# mverify_api apps/mojopay/payments/models/processor.py:299
if not self.group.user_has_permission(self.active_user, ["manage_payments", "admin"]):
    raise PermissionDeniedException(...)
```

When the request authenticates with `Authorization: apikey <key>`, `active_user`
is the `ApiKey` instance (`MojoModel.active_user` → `request.user`). The same
failure shape was reported for `GET /api/group/uuid/<uuid>` — i.e. **any** path
that hands an `ApiKey` to `user_has_permission` / `get_member_for_user`.

This is a framework concern: `ApiKey.has_permission` already exists and its
docstring says it *mirrors `GroupMember.has_permission`*, so a group-scoped API
key is a first-class permission-bearing identity. `Group.user_has_permission`
should treat it as one (grant/deny), never run a `User`-typed ORM query against
it, and never raise. Plain `RestMeta` CRUD already works for API keys (the
dispatcher's `rest_check_permission` consults `ApiKey.has_permission`); only the
direct `group.user_has_permission(api_key, …)` call is broken.

Relevant code (django-mojo `mojo/apps/account/models/`):
- `group.py:213` — `user_has_permission(self, user, perms, check_user=True)`
- `group.py:216` — guard `if not hasattr(user, "is_request_user"): return False`
- `group.py:253` — `get_member_for_user(...)`
- `group.py:267` / `group.py:280` — `self.members.filter(user=user)` (throws)
- `api_key.py:103` — `ApiKey.has_permission(perm_key)` (the intended mirror)

## Acceptance Criteria
- [ ] `Group.user_has_permission(api_key, perms)` returns a **bool** for an
      `ApiKey` identity — never raises. Grants when the key's permissions include
      a required perm; denies otherwise.
- [ ] No `members.filter(user=…)` query is ever executed with a non-`User`
      identity (guard/branch, not a try/except swallow).
- [ ] Same guarantee for any caller that routes an `ApiKey` into
      `get_member_for_user` (e.g. the `GET /api/group/uuid/<uuid>` path).
- [ ] Regression test: an `ApiKey` with no backing `User` passed through
      `user_has_permission` — one perm it holds (→ True), one it lacks (→ False),
      asserting no exception in either case.
- [ ] `sys.*` still denied for API keys (existing `ApiKey.has_permission`
      contract preserved).

## Repro — bugs only
1. Create a group-scoped `ApiKey` with `manage_payments` granted.
2. `POST /api/payments/processor/<pk>` body `{"test_connection": true}` (or
   `{"rotate_secret": true}`) with header `Authorization: apikey <key>`.
- Expected: permission check passes (or cleanly denies) and the action runs; a
  bool result from `user_has_permission`.
- Actual: HTTP 400 — `Cannot query "<name>@<group>": Must be "User" instance.`
  raised from `members.filter(user=<ApiKey>)`.

Also reported: `GET /api/group/uuid/<uuid>` with the same `apikey` auth → same
failure shape.

## Plan

### Goal
Make `Group.get_member_for_user` return `None` (never raise) when handed a
non-`User` identity such as an `ApiKey`, so every group membership/permission path
degrades to a clean grant/deny instead of a 400 `Must be "User" instance.`

### Root cause — settled during /scope (it's "Candidate 2"; version is moot)
Confirmed by reading the current tree (django-mojo 1.2.40) + git history:

- **`Group.user_has_permission` does NOT crash on an `ApiKey` in this tree.** Its
  guard at `group.py:216` (`if not hasattr(user, "is_request_user"): return False`)
  catches a non-`User` *before* the member query at line 218. That guard has
  existed since **v1.0.27** and `ApiKey.has_permission` since **v1.0.18** — both
  long before 1.2.40 — so "prod predates the guard" (Candidate 1) cannot explain a
  crash on a ≥1.2.40 host. The `check_user=False` variant
  (`metrics/rest/helpers.py:19`) also returns `False` at line 216, no crash.
- **The real crash is a shared choke point reached by callers that DON'T have that
  guard.** `Group.get_member_for_user` (`group.py:253`) runs
  `self.members.filter(user=user)` (`group.py:267`, and again on the parent walk
  at `group.py:280`) with **no identity-type guard**. Passing an `ApiKey` there
  makes Django raise `Cannot query "<name>@<group>": Must be "User" instance.`
  (`"<name>@<group>"` is `ApiKey.__str__`, `api_key.py:88`).
- Three call sites reach it **directly** with a bare `ApiKey` via `request.user`
  (set by the apikey auth middleware — see Context), bypassing the
  `user_has_permission` guard entirely. Fixing the choke point fixes all three at
  once. The fix is version-independent, which makes "confirm the deployed prod
  version" moot.

### Context — what exists
`request.user` for `Authorization: apikey <token>` is a **bare `ApiKey` instance**:
`mojo/middleware/auth.py` maps `"apikey" → ApiKey.validate_token` and does
`setattr(request, "user", instance)` (auth.py:16, 54-55). `MojoModel.active_user`
(`mojo/models/rest.py:68`) just returns `request.user`. So `active_user` /
`request.user` can be an `ApiKey`.

`ApiKey` (`mojo/apps/account/models/api_key.py`):
- `class ApiKey(MojoSecrets, MojoModel)` — **no `user` FK at all** (group-scoped:
  `group = ForeignKey("account.Group", ...)`, line 29). It is inherently
  "no backing User." Has `permissions = JSONField(default=dict)` (line 36).
- **Does NOT define or inherit `is_request_user`, and has no `__getattr__`** — so
  `hasattr(api_key, "is_request_user")` is `False`. (`is_request_user` is defined
  ONLY on `User`, `user.py:296`; it is the framework's standard "is this a real
  request User?" type-guard, used across `mojo/models/rest.py` at lines 241, 264,
  465 and at `group.py:216`.)
- `ApiKey.has_permission(perm_key)` (line 103) already mirrors
  `GroupMember.has_permission`: accepts a list/set (OR logic), **always denies
  `sys.*`**, returns `True` for `all`/`authenticated`/`member`, else looks up the
  `permissions` dict. **Leave this untouched** — it is the intended grant/deny for
  a key identity.
- Factory used by tests: `ApiKey.create_for_group(group=, name=, permissions={})`
  → returns `(api_key, raw_token)`.

The buggy choke point, **current code verbatim** (`mojo/apps/account/models/group.py:253`):
```python
253	    def get_member_for_user(self, user, check_parents=False, is_active=True, max_depth=8):
254	        """..."""
266	        # First check direct membership
267	        queryset = self.members.filter(user=user)          # <-- raises for ApiKey
268	        if is_active:
269	            queryset = queryset.filter(is_active=True)
270	        member = queryset.last()
271	        if member is not None or not check_parents:
272	            return member
        ...
280	            queryset = current.members.filter(user=user)   # <-- and again on parent walk
        ...
291	        return None
```

`Group.user_has_permission` (`group.py:213`) — for reference, **do not change**:
```python
213	    def user_has_permission(self, user, perms, check_user=True):
214	        if check_user and user.has_permission(perms):   # ApiKey WITH perm -> True here
215	            return True
216	        if not hasattr(user, "is_request_user"):        # ApiKey WITHOUT perm -> False here
217	            return False
218	        ms = self.get_member_for_user(user, check_parents=True)
219	        if ms is not None:
220	            return ms.has_permission(perms)
221	        return False
```

**Direct callers of `get_member_for_user(request.user)` that bypass the guard**
(these are the live crash paths; each already handles a `None` return as
deny/empty, verified — the fix turns the crash into their existing not-a-member
branch):
- `mojo/apps/account/models/group.py:561` — `check_view_permission(self, perms, request)`;
  reached by `GET /api/group/uuid/<uuid>` (`on_group_by_uuid`, `account/rest/group.py:13`)
  and `GET /api/group/<pk>`. `None → line 566 return False`.
- `mojo/apps/account/rest/group.py:98` — `on_group_me_member` (`GET /api/group/<pk>/member`).
  `None → returns {"id": -1, "permissions": []}`.
- `mojo/apps/account/models/member.py:87` — `can_change_permission`. `None → line 93 return False`.
- (`group.py:218` inside `user_has_permission` is already guarded by line 216; the
  fix just makes it doubly safe.)

### Changes — what to do
**Single change.** In `mojo/apps/account/models/group.py`, add a guard as the first
statements of `get_member_for_user` (right after the docstring, before line 267):

```python
        # A GroupMember always links an account.User. A non-User identity — e.g.
        # an ApiKey authenticating the request (request.user can be a bare ApiKey)
        # — has no member row, and `.members.filter(user=<ApiKey>)` makes Django
        # raise 'Must be "User" instance.'. Treat it as "no membership" so every
        # caller (guarded or not) degrades to deny/None instead of crashing.
        if not hasattr(user, "is_request_user"):
            return None
```

No other production code changes. `user_has_permission`, `ApiKey.has_permission`,
`check_view_permission`, and the REST handlers stay as-is.

### Design decisions
- **Fix the choke point, not each caller.** All membership lookups funnel through
  `get_member_for_user`; guarding it once covers all present and future callers
  (DRY) and is version-independent. Guarding each caller instead would repeat the
  check 3× and miss the next new caller. (This is exactly the inconsistency that
  caused the bug: some callers had the guard, some didn't.)
- **Guard with `hasattr(user, "is_request_user")`, not `isinstance(user, User)`.**
  This is the framework's established idiom (already at `group.py:216` and
  throughout `rest.py`), needs no import (avoids any `Group`↔`User` import-order
  question), and is exactly the "is this a real request User?" question we mean.
  Also naturally handles `user=None` (returns `None`).
- **Do NOT touch `user_has_permission`** (rejected the item's belt-and-suspenders
  suggestion, per KISS + user sign-off). It already grants an `ApiKey` its perms
  via line 214 and denies via line 216; with the choke point guarded, line 218 is
  doubly safe. No behavior change, minimal diff.
- **Out of scope (note, don't fix):** `metrics/rest/helpers.py:19` calls
  `user_has_permission(request.user, perm, check_user=False)`, which denies
  API keys metrics access (line 216 guard). That is pre-existing, intentional
  (membership-based), unaffected by this fix, and not in the acceptance criteria.

### Edge cases & risks
- **Parent-chain branch:** the guard is before *both* `filter(user=...)` sites
  (line 267 and the parent walk at 280), so `check_parents=True` is safe too. The
  regression test uses a child group (which has a parent) to exercise this.
- **No caller dereferences the result unguarded:** all four callers above already
  branch on `is None` — verified. So converting raise→`None` cannot introduce a
  `NoneType` error.
- **A real `User` is unaffected:** `User` defines `is_request_user`, so the guard
  is `False` for it and the method runs exactly as before.
- **`user=None`** (defensive): `hasattr(None, "is_request_user")` is `False` →
  returns `None` (previously `filter(user=None)` returned no rows anyway).

### Tests
Add to the existing suite `tests/test_user_mgmt/api_keys.py` (pattern:
`@th.django_unit_setup()` + `@th.unit_test("name")`; setup already creates
parent/child groups and cleans `ApiKey.objects.filter(name__startswith="test_")`).
Use `opts.child_id` so `check_parents=True` walks to a parent.

1. **Regression (the fail-before-fix assertion) — `get_member_for_user` directly:**
```python
@th.unit_test("apikey_get_member_for_user_is_none")
def test_apikey_get_member_for_user_is_none(opts):
    """DM-016: get_member_for_user must return None for an ApiKey identity,
    never run a User-typed query. Before the fix this raised
    'Cannot query "...": Must be "User" instance.'."""
    from mojo.apps.account.models import Group, ApiKey
    child = Group.objects.get(pk=opts.child_id)   # child has a parent
    api_key, _ = ApiKey.create_for_group(
        group=child, name="test_member_lookup", permissions={"manage_payments": True})
    member = child.get_member_for_user(api_key, check_parents=True)
    assert member is None, \
        "get_member_for_user must return None for an ApiKey, not raise or return a member"
```

2. **Contract coverage — `user_has_permission` returns bools, never raises**
   (note: this passes even pre-fix, so it is coverage, NOT the regression):
```python
@th.unit_test("apikey_user_has_permission_bool")
def test_apikey_user_has_permission_bool(opts):
    """DM-016: user_has_permission returns a bool for an ApiKey — grants a held
    perm, denies a lacked one and sys.*, never raises."""
    from mojo.apps.account.models import Group, ApiKey
    child = Group.objects.get(pk=opts.child_id)
    api_key, _ = ApiKey.create_for_group(
        group=child, name="test_perm_bool", permissions={"manage_payments": True})
    assert child.user_has_permission(api_key, ["manage_payments", "admin"]) is True, \
        "ApiKey holding manage_payments must be granted"
    assert child.user_has_permission(api_key, ["manage_users"]) is False, \
        "ApiKey lacking the perm must be denied (bool), not raise"
    assert child.user_has_permission(api_key, ["sys.superuser"]) is False, \
        "sys.* must always be denied for an ApiKey"
```

Run: `bin/run_tests --agent -t test_user_mgmt.api_keys` (read `var/test_failures.json`).
Both test names start with `test_` so the existing setup cleans them on re-run.
Capture the green baseline with `bin/run_tests --agent` before editing (build-baseline rule).

### Docs
- `CHANGELOG.md`: entry — group membership/permission helpers are ApiKey-safe;
  `Group.get_member_for_user` returns `None` (never raises) for a non-`User`
  identity; fixes 400 `Must be "User" instance.` on apikey-authed
  `GET /api/group/uuid/<uuid>`, `GET /api/group/<pk>`, `GET /api/group/<pk>/member`.
- `docs/django_developer/` (account / api-key area): note that `get_member_for_user`
  and the group permission helpers accept any request identity and treat a
  non-`User` (e.g. `ApiKey`) as "no membership."
- `docs/web_developer/`: the above endpoints no longer 400 under `Authorization:
  apikey` — they cleanly grant/deny.
- (Placement handled by the post-build `docs-updater` agent.)

### Open questions
None. Root cause settled, fix approved (minimal, `hasattr` guard), prod-version
question is moot because the fix is version-independent.

## Notes

**Root cause — 2 candidates; settle during /scope by confirming the deployed
django-mojo version on api.mojoverify.com.**

The installed + pinned version in mojo-verify is `django-mojo 1.2.40`
(site-packages wheel, not an editable link to this tree; `pyproject.toml`:
`django-mojo>=1.2.40`). This working tree is also 1.2.40, and both have the
**identical** `user_has_permission` body — including the `line 216` guard
`if not hasattr(user, "is_request_user"): return False`.

- `is_request_user` is defined **only** on `User` (`user.py:296`); `ApiKey`
  (`ApiKey(MojoSecrets, MojoModel)`) does not define or inherit it, and has no
  `__getattr__`. So `hasattr(api_key, "is_request_user")` is **False**.
- By static analysis of 1.2.40, an `ApiKey` caller therefore either:
  - satisfies `line 214` `user.has_permission(perms)` via `ApiKey.has_permission`
    → returns `True` (no crash), or
  - fails `line 214`, hits the `line 216` guard → returns `False` (no crash).
- In **neither** branch does 1.2.40 reach `get_member_for_user`. So **1.2.40 as
  written should not reproduce this.**

**Candidate 1 — version skew (most likely).** `api.mojoverify.com` runs a
django-mojo **older than 1.2.40**, whose `user_has_permission` predates the
`line 216` guard and/or predates `ApiKey.has_permission`, so an `ApiKey` fell
through to `members.filter(user=…)`. If so, shipping/deploying ≥1.2.40 already
fixes it and this collapses to "confirm the deployed version + add the missing
regression test." Confidence: high that 1.2.40's code does not crash on this
static path; medium that prod is simply behind.

**Candidate 2 — residual gap even on 1.2.40.** If the live host *is* 1.2.40 and
still crashes, then either (a) `request.user` for apikey auth is not a bare
`ApiKey` but something exposing `is_request_user` (making the guard pass), or
(b) a **second, unguarded** call path reaches `get_member_for_user` /
`filter(user=…)` directly — the cited `GET /api/group/uuid/<uuid>` handler is the
prime suspect. Confidence: medium.

**Durable fix that covers both candidates** (recommended shape — /scope to
confirm): special-case group-scoped key identities at the top of
`Group.user_has_permission` (and defensively in `get_member_for_user`): if the
identity is an `ApiKey` (or, more generally, not a `User`), delegate to
`user.has_permission(perms)` when the key belongs to this group / a child, else
deny — and **never** run the `User`-typed member query against it. This makes the
result independent of version skew and of which call path is taken.

**Regression-test feasibility:** unit-level — construct a `Group` + an `ApiKey`
with a permissions dict (no backing `User`) and call `group.user_has_permission`
directly; no HTTP/dev-server needed. Add alongside existing account/group tests.

**Downstream link:** unblocks mojo-verify MVERIFY-API-010 Phase A. That repo needs
no code change if the framework fix ships and the deployed pin is confirmed
≥ the fixed version (pre-launch; single-owner; no app-level defensive guard
warranted per KISS unless /scope finds Candidate 2 requires a stopgap).

**Build baseline (2026-07-06, before first edit — `bin/run_tests --agent`):**
default suite **all green** — `status: passed`, total 2290, passed 2234, failed 0,
skipped 56 (from `testproject/var/test_failures.json`, `failures: []`). The 325
extra reds in the terminal (test_incident 243, test_security 82) are opt-in
`--full` modules excluded from the default suite (test_security is separately
tracked as red). Green baseline → any new failure is attributable to this change.

## Resolution
- closed: 2026-07-06
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/account/api_keys.md,docs/django_developer/account/group.md,docs/web_developer/account/api_keys.md,mojo/apps/account/models/group.py,planning/in_progress/DM-016-group-user-has-permission-crashes-on-apikey-identi.md,tests/test_user_mgmt/api_keys.py
- tests added: tests/test_user_mgmt/api_keys.py — `apikey_get_member_for_user_is_none` (regression: get_member_for_user(ApiKey) → None, not raise; failed before fix / passes after) and `apikey_group_user_has_permission_bool` (contract: grants held perm, denies lacked perm + sys.*, never raises)
