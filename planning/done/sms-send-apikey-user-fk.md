# SMS send via API key crashes — ApiKey assigned to SMS.user ForeignKey

**Type**: issue
**Status**: Resolved
**Date**: 2026-05-21
**Resolved**: 2026-05-21
**Severity**: high

## Resolution
Fixed in `mojo/apps/phonehub/rest/sms.py` — `on_sms_send` now resolves the
caller to a real `User` (via `hasattr(request.user, "is_request_user")`)
and passes `None` for API-key callers, so `SMS.user` is left null and
`SMS.group` (set from the API key's group) identifies the caller.
Regression test: `tests/test_phonehub/sms_send_apikey.py` (verified to
fail with the reported `ValueError` before the fix, pass after).

## Description
`POST /api/phonehub/sms/send` returns a 500 when the request is authenticated
with an API key (`Authorization: apikey <token>`). Django raises:

```
ValueError: Cannot assign "<ApiKey: SMS Key@WMWX DevTesting>":
"SMS.user" must be a "User" instance.
```

When a request authenticates via API key, `request.user` is the `ApiKey`
instance, not a `User`. The `on_sms_send` handler passes `request.user`
straight into `SMS.send(user=...)`, which assigns it to the `SMS.user`
ForeignKey (`account.User`). Django rejects the type mismatch and the
request fails.

## Context
API-key-authenticated SMS sending is a first-class supported flow, not an
edge case:

- `mojo/apps/phonehub/models/config.py:357-362` documents that
  `POST /api/phonehub/sms/send` accepts API keys carrying `send_sms` or
  `comms` permission.
- The `mojo` SMS provider relays messages by POSTing to a remote
  django-mojo instance's `/api/phonehub/sms/send` with
  `Authorization: apikey <token>` (`mojo/apps/phonehub/services/mojo_provider.py:48`).

This bug means **the `mojo` provider relay is broken end to end** — any
django-mojo instance configured with `provider='mojo'` will get a 500 from
the remote on every send. It was not caught because the
`sms_mojo_provider` tests all mock `requests.post`, so the receiving side
(`on_sms_send` under API-key auth) is never exercised.

## Acceptance Criteria
- `POST /api/phonehub/sms/send` with `Authorization: apikey <token>`
  succeeds and creates an `SMS` row (no `ValueError`).
- When the caller is an API key, `SMS.user` is left null (the field is
  `null=True, blank=True`); `SMS.group` is still set from the API key's
  group.
- When the caller is a real `User`, `SMS.user` continues to be populated
  as before — no regression to the session/JWT path.
- A regression test exercises the API-key SMS-send path and fails while
  the bug is present, passes once fixed.

## Investigation
**Likely root cause**: `request.user` is an `ApiKey`, not a `User`, on
API-key-authenticated requests, and `on_sms_send` forwards it unguarded
into a `User` ForeignKey.

**Confidence**: confirmed (code analysis — deterministic Django FK type error)

**Code path**:
- `mojo/middleware/auth.py:19` — `AUTH_BEARER_NAME_MAP` defaults to
  `{"bearer": "user", "apikey": "user"}`, so the `apikey` prefix maps to
  the `user` attribute.
- `mojo/middleware/auth.py:42-46` — `ApiKey.validate_token` returns the
  `ApiKey` instance; `setattr(request, "user", instance)` sets
  `request.user` to that `ApiKey`.
- `mojo/apps/account/models/api_key.py:215-247` — `validate_token` also
  sets `request.group` and stamps `is_authenticated` / `username` on the
  ApiKey so it duck-types as a user for permission checks.
- `mojo/apps/phonehub/rest/sms.py:39-45` — `on_sms_send` calls
  `SMS.send(..., user=request.user, group=request.group, ...)`.
- `mojo/apps/phonehub/models/sms.py:244` — `SMS.send` does
  `cls.objects.create(user=user, ...)`; `SMS.user` is a
  `ForeignKey("account.User")` (`sms.py:16`), so assigning an `ApiKey`
  raises `ValueError`.

**Fix direction (for /design)**: resolve `user` at the REST boundary in
`on_sms_send` — pass `request.user` only when it is a real `User`,
otherwise pass `None`. The codebase's canonical "is this a real User"
check is `hasattr(request.user, "is_request_user")` (see
`mojo/models/rest.py:264`, `mojo/apps/account/rest/user.py:29`). `group`
is already correct because the middleware sets `request.group` from the
API key. Optionally `SMS.send` could also guard defensively, but the
clean fix is at the handler boundary.

**Regression test**: not written — `/build` should add one. Feasible as a
testit `@django_unit_test`: create a `Group` + `ApiKey` (with `send_sms`
permission) via `ApiKey.create_for_group`, then
`opts.client.post("/api/phonehub/sms/send", {...},
headers={"Authorization": f"apikey {token}"})` to a `+1555` test number,
and assert a 200 with an `SMS` row whose `user` is null and `group`
matches the key's group.

**Related files**:
- `mojo/apps/phonehub/rest/sms.py` (primary fix site — `on_sms_send`)
- `mojo/apps/phonehub/models/sms.py` (`SMS.send` — optional defensive guard)
- `tests/test_phonehub/` (new regression test)

**Broader concern (out of scope, note for triage)**: any other REST
handler that forwards `request.user` into a `User` ForeignKey will have
the same failure mode under API-key auth. Worth a follow-up audit, but
this issue is scoped to the SMS send path.
