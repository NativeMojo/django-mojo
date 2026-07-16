---
# id is assigned by /scope on pickup — leave it blank
id: DM-028
type: bug
title: POST /api/group/member/invite returns a raw 500 (TypeError) for unauthenticated callers instead of a clean 403
priority: P2
effort: S
owner: backend
opened: 2026-07-10
depends_on: []
related: []            # inbox siblings (un-ID'd): rest-decorator-500-handler-ignores-logit-return-real-error (governs the leak), group-me-member-endpoint-oracle-touch (same file)
links: []
---

# POST /api/group/member/invite returns a raw 500 (TypeError) for unauthenticated callers instead of a clean 403

## What & Why
`POST /api/group/member/invite` (`on_group_invite_member`, `mojo/apps/account/rest/group.py:36-50`) has **no framework authentication gate**. Its decorators are `@md.POST` → `@md.requires_params('email','group')` → `@md.custom_security(...)`. `@md.custom_security` is a **pure no-op marker** (`mojo/decorators/auth.py:189-211`: sets metadata, registers in `SECURITY_REGISTRY`, `return func` unchanged — no wrapper, no `request.user` check); it only declares "security is handled inside the view." So the view body is the only line of defense, and its very first statement calls `request.group.user_has_permission(request.user, perms)`.

For an unauthenticated request `request.user` is `ANONYMOUS_USER` (`mojo/middleware/mojo.py:12-17`), a bare `objict` whose `has_permission` is a **zero-argument** `lambda: False`. `Group.user_has_permission` (`mojo/apps/account/models/group.py:213-221`) calls `user.has_permission(perms)` at **line 214** (its `check_user` param defaults to `True`) — one line **before** the `hasattr(user, "is_request_user")` guard at line 216 that is supposed to deflect non-`User` identities. Passing one argument to a zero-arg lambda raises:

```
TypeError: <lambda>() takes 0 positional arguments but 1 was given
```

That exception escapes the view, is caught by the generic 500 handler (`mojo/decorators/http.py:202-217`), and is returned on the wire as **HTTP 500** `{"error":"<lambda>() takes 0 positional arguments but 1 was given","code":500,"status":false}` — the raw interpreter message, not a clean auth rejection.

It **is** fail-closed: the crash is inside the permission check, before `request.group.invite()`, so no `GroupMember` is created and nothing is exposed. This is a robustness / API-hygiene bug, not a data-exposure hole. But it is an **unauthenticated-reachable public endpoint** returning a 500 that leaks an internal Python message instead of a clean 403, and it is now consumed by the **maestro workspaces app** for member invites — a clean rejection matters for that API consumer.

**Important correction to the originally-proposed fix:** guarding `request.group is None` does **not** fix the reported repro. For an anonymous POST carrying `group: 1`, the dispatcher resolves `request.group` to the **real active Group** with no auth check (`mojo/decorators/http.py:73-81`, via `Group.get_active(int(...))`), so `request.group` is a genuine object on the crashing path. A `request.group is None` guard only covers a *different* variant — an inactive/missing group id, where line 41 would instead raise `AttributeError: 'NoneType' object has no attribute 'user_has_permission'` (also a 500). Both variants should end in a clean rejection, but the primary fix is an **auth gate**, not a None-check.

## Acceptance Criteria
- [ ] An unauthenticated `POST /api/group/member/invite` returns a clean **403** — body `{"error":"Permission Denied","code":403,"status":false}` — never a 500, never a raw Python/interpreter message. (403, not 401: this is the framework convention for unauthenticated requests — the `requires_auth()` / `PermissionDeniedException` default, matching `on_group_me_member` at group.py:89 and the DM-012 "never a 401" invariant. If the maestro consumer specifically needs 401, flag it in /scope.)
- [ ] No `GroupMember` / membership side effect for a rejected anonymous call (already true — assert it stays true).
- [ ] The auth gate runs **before** the param check and the view body (see Investigation → placement).
- [ ] `/scope` decides whether to ALSO harden the model layer (Investigation → open question): the `ANONYMOUS_USER.has_permission` zero-arg lambda and/or the guard ordering in `Group.user_has_permission`, so any *other* unguarded caller passing a non-`User` identity can't reproduce the same arity crash.
- [ ] Regression test: anonymous → 403 (not 500), and no `GroupMember` created.

## Repro — bugs only
1. Ensure group `1` exists and is active. Then, with **no** Authorization header:
   `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>/api/group/member/invite -H 'Content-Type: application/json' -d '{"email":"x@y.com","group":1}'`
- Expected: `403`, body `{"error":"Permission Denied","code":403,"status":false}`; no `GroupMember` created.
- Actual: `500`, body `{"error":"<lambda>() takes 0 positional arguments but 1 was given","code":500,"status":false}`.

## Investigation
- **Root cause — confidence: `confirmed` (traced by analysis).** The raising line is `mojo/apps/account/models/group.py:214` (`if check_user and user.has_permission(perms)`); the offending value is `ANONYMOUS_USER.has_permission = lambda: False` at `mojo/middleware/mojo.py:17`. Real identities don't hit this: `User.has_permission(self, perm_key)` (`user.py:492`), `GroupMember.has_permission` (`member.py:108`), and `ApiKey.has_permission` (`api_key.py:111`) all accept the arg — only the anonymous objict's zero-arg lambda mismatches. The `hasattr(user,"is_request_user")` guard at group.py:216 would have returned `False` for `ANONYMOUS_USER` and prevented the crash, but it is ordered **after** the failing call.
- **`request.group` IS resolved for anonymous callers.** `mojo/decorators/http.py:73-81` resolves a numeric `group` param via `Group.get_active(pk)` with no auth/api-key check on the anonymous path — so `request.group` is a real active Group when group 1 exists. (Confirms the reporter's env had an active group 1, and is why the `request.group is None` guard is a red herring for this repro.)
- **Correct fix + placement.** Add `@md.requires_auth()` (`mojo/decorators/auth.py:268-288`) directly beneath `@md.POST('group/member/invite')` and above `@md.requires_params('email','group')`. The route decorator (`@md.POST`) registers the fully-wrapped stack and must stay outermost (`_register_route`, http.py:233-298); the inner decorator nearest the route decorator runs first at dispatch, so this makes the auth gate fire before param validation and before the body. `requires_auth()` raises `PermissionDeniedException()` (default code/status 403) → clean 403. This is the established pattern (`device.py:161-164`, `totp.py:67-70`, and the sibling `on_group_me_member` at group.py:89-91). `@md.custom_security` can stay as a descriptive marker (the view still does the group-permission check) — /scope's call whether to keep or drop it.
- **Open question for /scope — model-layer hardening (defense-in-depth, optional).** The endpoint gate fixes *this* endpoint, but the underlying footgun remains: `Group.user_has_permission(user, perms)` with the default `check_user=True` calls `user.has_permission(perms)` before verifying `user` is a real request `User`. Two cheap hardenings, independently or together: (a) make `ANONYMOUS_USER.has_permission` accept args — `lambda *a, **k: False` at `middleware/mojo.py:17`; (b) reorder `Group.user_has_permission` so the `is_request_user` guard precedes the `has_permission` call (group.py:213-217). Note DM-016 already established `user_has_permission` as the ApiKey-safe choke point via that `is_request_user` guard — this is the same guard, just positioned one line too late to catch a zero-arg-lambda identity. Recommend at least (a); it's a one-token change that closes the class of bug.
- **The 500 body leak is separately governed** by the inbox sibling `rest-decorator-500-handler-ignores-logit-return-real-error`: the decorator's `except Exception` branch returns `str(err)` unconditionally (ignores `LOGIT_RETURN_REAL_ERROR`). Even before that item lands, gating auth here removes this endpoint's 500 entirely; after it lands, any residual 500 would scrub to `"system error"`. Cross-referenced, not blocking.
- **Regression-test feasibility: high.** A testit client can issue the anonymous POST (no auth) and assert status 403 + `GroupMember.objects.filter(...).count()` unchanged. Sibling group tests already exercise anonymous/permission paths (`tests/test_middleware/`, `tests/test_global_perms/`).

## Plan

### Goal
Make `POST /api/group/member/invite` reject an unauthenticated caller with a clean **403** (never a raw 500 leaking a Python `TypeError`), and harden the anonymous-user sentinel so the same arity crash cannot recur at the other ungated permission-check call sites.

### Context — what exists
- **Endpoint** `on_group_invite_member` — `mojo/apps/account/rest/group.py:36-50`. Decorator stack: `@md.POST('group/member/invite')` → `@md.requires_params('email','group')` → `@md.custom_security("securted by group security")`. Body's first real statement: `request.group.user_has_permission(request.user, perms)` (line 41), `perms = ["manage_users","manage_members","manage_group","manage_groups"]`.
- **`@md.custom_security`** — `mojo/decorators/auth.py:189-211`. Pure metadata/no-op: sets `_mojo_custom_security`, registers in `SECURITY_REGISTRY` (the `requires_auth: True` there is advisory only), `return func` unchanged. **No runtime auth gate.** So the view body is the only defense.
- **`@md.requires_auth()`** — `mojo/decorators/auth.py:268-288`. Wrapper does `if not request.user.is_authenticated: raise PermissionDeniedException()` then calls the view. `PermissionDeniedException()` defaults `reason='Permission Denied', code=403, status=403` (`mojo/errors.py:76`), rendered by `dispatch_error_handler` at `mojo/decorators/http.py:148-173` as `{"error":"Permission Denied","code":403,"status":False}`, **wire 403**.
- **`ANONYMOUS_USER`** — `mojo/middleware/mojo.py:12-17`, an `objict` with `has_permission=lambda: False` (**zero-arg**). Assigned to `request.user` on every unauthenticated request (`mojo/middleware/mojo.py:30`); the auth middleware only overwrites it when a bearer token validates (`mojo/middleware/auth.py:24-56`).
- **`Group.user_has_permission`** — `mojo/apps/account/models/group.py:213-221`:
  ```python
  def user_has_permission(self, user, perms, check_user=True):
      if check_user and user.has_permission(perms):     # line 214  <-- raises for ANONYMOUS_USER
          return True
      if not hasattr(user, "is_request_user"):          # line 216  <-- guard, deliberately AFTER 214
          return False
      ms = self.get_member_for_user(user, check_parents=True)
      ...
  ```
  Line 214 calls the zero-arg lambda with one arg → `TypeError: <lambda>() takes 0 positional arguments but 1 was given`. **The 216 guard order is intentional (DM-016):** an ApiKey identity legitimately grants via `has_permission(perms)` at 214, and 216 only exists to keep the `User`-typed member lookup from running on a non-User. **Do NOT reorder it** — that would deny valid ApiKey grants.
- **Dispatcher resolves `request.group` with no auth check** — `mojo/decorators/http.py:73-81`: `if "group" in request.DATA...: request.group = Group.get_active(int(request.DATA.group))`. So for an anon POST with `group:1` (active), `request.group` is a **real Group** (this is why the crash reaches line 214). For an **inactive/nonexistent** group id, `Group.get_active` returns `None` → `request.group` stays `None` (DM-025 "inactive == nonexistent, silent" contract) → line 41 would instead `AttributeError` (also a 500).
- **Generic 500 handler** — `mojo/decorators/http.py:202-217` returns `{"error": str(err), "code": 500, "status": False}` (status 500). This is what leaks the raw `TypeError`/`AttributeError` text.
- **The standard model path already fail-closes cleanly for anon** — `mojo/models/rest.py:339` (`if not request.user.is_authenticated: return 401`) sits *before* the permission call at `rest.py:345`, so `uses_model_security` endpoints (e.g. `GET /api/group`) return a clean **401**, never the lambda crash (verified against `tests/test_account/test_group_list_no_perms.py:164-186`). The crash is **confined** to the `custom_security`+manual-`user_has_permission` pattern. Same latent crash also lives, un-fixed by the endpoint gate, at `mojo/apps/chat/rest/rooms.py:300` & `:312` and `mojo/apps/account/models/member.py:87` — change #2 fail-closes those in one shot (see Notes / follow-up).
- **Decorator ordering** — `_register_route` (`http.py:233-298`) stores the exact function object handed to it (`URLPATTERN_METHODS[key] = view_func`, :271); the route decorator (`@md.POST`) must stay **outermost** so it captures the whole wrapped stack, and among the inner decorators the one **nearest the route decorator runs first**. So `@md.requires_auth()` goes directly beneath `@md.POST(...)`, above `@md.requires_params(...)` — matching `device.py:161-164` and `totp.py:67-70`.

### Changes — what to do
1. **`mojo/apps/account/rest/group.py`** — in `on_group_invite_member`:
   - Add `@md.requires_auth()` **between** `@md.POST('group/member/invite')` and `@md.requires_params('email', 'group')`. Keep `@md.custom_security(...)` (the in-view group-permission check is still real, so the marker stays accurate). Effect: anonymous → clean 403 before param-check and before the body ever runs.
   - Add a `request.group is None` guard as the first line of the body (before building `perms`), raising a generic `PermissionDeniedException` so an authenticated caller passing an inactive/nonexistent `group` id also gets a clean rejection instead of an `AttributeError` 500. Follow the sibling pattern (`on_group_me_member` group.py:93-99, `on_group_webhook_secret` group.py:67-74) and the DM-025 no-oracle contract (don't reveal inactive-vs-nonexistent):
     ```python
     if request.group is None:
         raise merrors.PermissionDeniedException(
             reason="permission denied: Group",
             model_name="Group",
             branch="group_invite_unknown_group",
             event_type="user_permission_denied",
         )
     ```
2. **`mojo/middleware/mojo.py:17`** — change `has_permission=lambda: False` to `has_permission=lambda *a, **kw: False` in the `ANONYMOUS_USER` objict. Return value is unchanged (always `False`); this only stops the arity `TypeError` when any code calls `request.user.has_permission(perm)` on an anonymous request, converting a 500 into the intended clean deny at every ungated site.

### Design decisions
- **Add an auth gate; do not refactor to `@md.requires_perms(...)`.** KISS (core.md). The existing in-view `user_has_permission` authorization works; swapping to `requires_perms` (OR across 4 perms + group fallback) is a larger behavioral change with regression risk and is out of scope for a P2 robustness fix. (Per the memory Watch List, `group/member*` deliberately stays group-scoped — we keep the group-scoped custom check and only add the missing *authentication* precondition.)
- **Keep `@md.custom_security`.** The view performs custom group-permission authorization not expressible as a stock decorator; the marker remains accurate and keeps the security-auditor registration. `requires_auth` adds authentication; it does not replace the in-view authorization.
- **403, not 401.** Framework convention — the `requires_auth()`/`PermissionDeniedException` default, matching `on_group_me_member` and the DM-012 "never a 401" invariant. (If the maestro consumer specifically needs 401, that's a deliberate future deviation, not done here.)
- **Fix the lambda arity; do NOT reorder the `user_has_permission` guard.** Reordering `is_request_user` before the `has_permission` call at group.py:214 would break ApiKey grants (see Context). The `*a, **kw` lambda closes the anonymous crash without touching that ordering.
- **Verified safe against public/anonymous access (the reviewer's concern).** The lambda always returns `False`; no access anywhere depends on it returning truthy. `"all"` in VIEW_PERMS means "any *authenticated* user" — anonymous is denied at `rest.py:339` *before* the lambda is reached, today and after. Genuine public access is empty/undefined perms (`rest.py:222`) or `@public_endpoint` (`auth.py:163-186`), neither of which calls `has_permission`. The one `try/except` near a `has_permission` call (geofence `engine.py:392-398`) is `is_authenticated`-gated and doesn't grant on exception. So change #2 can only convert crashes → clean denials; it removes zero grants.

### Edge cases & risks
- **Anon + active group** (the reported repro): #1 rejects at the auth gate → 403. ✓
- **Anon + inactive/nonexistent group**: #1 still rejects at the auth gate first → 403 (never reaches the None-guard or body). ✓
- **Authenticated + inactive/nonexistent group**: passes auth, `request.group is None`, the new None-guard → clean 403 (was an `AttributeError` 500). ✓
- **Authenticated member without the perms** (unchanged): reaches `user_has_permission` → `False` → `PermissionDeniedException()` 403. ✓
- **ApiKey identity** (unchanged): `has_permission(perms)` at group.py:214 still runs and can grant; the `*a,**kw` lambda only affects the anonymous objict, not ApiKey. ✓
- Risk of the None-guard leaking existence: mitigated by returning a **generic** `PermissionDeniedException` (no inactive-vs-nonexistent distinction), consistent with DM-025.

### Tests
Use testit (`docs/django_developer/testit/Overview.md`). New file **`tests/test_account/test_group_invite_anonymous.py`**. The testit client does not auto-authenticate; `opts.client.logout()` (drops the `Authorization` header) reproduces the real `ANONYMOUS_USER` path. Assert wire status via `resp.status_code` and body via `resp.response` (an objict). Model setup/cleanup on `tests/test_global_perms/invite_protection.py:23-52` and `tests/test_account/test_group_member_count.py`.
- **Primary regression — anonymous → clean 403, no side effect** (fails today with 500):
  - setup: delete-then-create a test-owned **active** Group (integer pk; `uuid` may stay None — the endpoint resolves by pk). Delete any leftover invitee `User`/`GroupMember` first.
  - `opts.client.logout()`, then `POST /api/group/member/invite` with `{group: <pk>, email: "anon_invitee@example.test"}`.
  - assert `resp.status_code == 403`; `resp.response.error == "Permission Denied"`; `resp.response.code == 403`; `resp.response.status is False`.
  - assert `GroupMember.objects.filter(group=...).count()` unchanged **and** no invitee `User` created.
  - teardown in `finally`: delete the invitee `User`/`GroupMember` and the test Group.
- **Secondary — authenticated caller + inactive/nonexistent group → clean 403** (covers the None-guard; fails today with an `AttributeError` 500): authenticate a user with `manage_group` on some active group, POST with `group=<an inactive or bogus pk>` → assert `status_code == 403` (generic), no membership created. (Reuse the `invite_protection.py` authenticated-admin setup.)
- Run: `bin/run_tests --agent -t test_account.test_group_invite_anonymous` (plus the baseline per `.claude/rules/build-baseline.md`).

### Docs
- `docs/web_developer/` — if the group-invite endpoint is documented, note it requires authentication (401/403-family without a valid token) and that a bad/inactive `group` yields a 403, not a 500. If undocumented, no new page needed.
- `docs/django_developer/` — no behavioral API change to document beyond the changelog; optionally a one-line note that `ANONYMOUS_USER.has_permission` tolerates args (fail-closed) if the middleware doc enumerates the sentinel.
- `CHANGELOG.md` — one line: unauthenticated / bad-group `POST /api/group/member/invite` now returns a clean 403 instead of a 500; `ANONYMOUS_USER.has_permission` hardened against arity crashes at ungated permission checks.

### Open questions
- None blocking. (Deferred, non-blocking: whether `chat/rest/rooms.py:300/312` and `member.py:87` — now fail-closed by change #2 but still un-*gated* — each also warrant an explicit `requires_auth`/`requires_perms` gate. Tracked as a possible follow-up item; not in scope here.)

## Notes
- **Baseline (2026-07-10, pre-edit, `bin/run_tests --agent`):** GREEN — status `passed`, total 2402, passed 2346, failed **0**, skipped 56 (opt-in `test_incident`/`test_security` skipped by default). Target module `test_account` 185/185, `test_global_perms` 14/14, `test_middleware` 18/18. No pre-existing failures, so every post-change failure is attributable to this change.
- Same file, distinct concern from the sibling inbox item `group-me-member-endpoint-oracle-touch` (that one is an authenticated-user existence oracle + inactive-group touch on `GET /api/group/<pk>/member`; this one is an unauthenticated 500 on `POST /api/group/member/invite`).
- **Regression confirmed before fix:** anonymous → `500 {'error': '<lambda>() takes 0 positional arguments but 1 was given', ...}`; authed + inactive group → `500 {'error': "'NoneType' object has no attribute 'user_has_permission'", ...}`. Both → clean 403 after the fix.
- **Post-build agents (2026-07-10):** test-runner GREEN — full default suite 2348 passed / **0 failed** / 56 skipped (+2 vs baseline = the new regression module; zero regressions). security-review CLEAN — all 5 properties confirmed (auth gate unreachable for anon; no inactive-vs-nonexistent oracle; lambda fail-closed, no grant path; ApiKey/member authz unchanged; 403 correct). docs-updater synced both tracks (see files changed).
- **security-review INFO (accepted, no change):** the None-guard's `reason="permission denied: Group"` differs from the plain `PermissionDeniedException()` ("Permission Denied") at the unauthorized branch, so an *already-authenticated* caller could distinguish "group doesn't resolve" from "I lack permission" by the `error` string. Left as-is — matches the local sibling pattern (`on_group_me_member`, `on_group_webhook_secret` both use distinct None-guard reasons), reveals only group existence (not treated as sensitive here; `on_group_by_uuid` returns an explicit 404), and is a strict improvement over the pre-fix raw 500. (If a uniform-403 posture is later wanted, apply it across all three group endpoints in one item, not just here.)
- **Spotted-in-passing (out of scope, worth a separate chore):** docs use the bare `@md.requires_auth` (no parens — would `TypeError` at decoration) in `docs/django_developer/core/mojo_model.md` (2×), `docs/web_developer/account/custom_auth_models.md` (2×), plus inline in `api_keys.md` and `shortlink/README.md`. docs-updater fixed only the one block it was already editing in `core/decorators.md`.

## Resolution
- closed: 2026-07-10
- branch: main
- files changed: mojo/apps/account/rest/group.py, mojo/middleware/mojo.py, tests/test_account/test_group_invite_anonymous.py, CHANGELOG.md, docs/web_developer/account/group.md, docs/web_developer/account/admin_portal.md, docs/django_developer/core/decorators.md, docs/django_developer/core/middleware.md, memory.md
  <!-- corrected: scripts/close.sh auto-detected a stale/over-broad diff that included files from prior committed items (bouncer.md, settings_reference.md, django_developer/account/group.md, group-metadata-replace inbox); the list above is DM-028's actual files -->
- code committed: 8ccd1b9 (rest/group.py, middleware/mojo.py, test, CHANGELOG, web_developer/account/group.md); post-build docs + memory + this item in the close commit
- tests added: tests/test_account/test_group_invite_anonymous.py (2 regression tests — anonymous POST → clean 403 + no side effect; authenticated caller + inactive/unknown group → clean 403 + no side effect)
