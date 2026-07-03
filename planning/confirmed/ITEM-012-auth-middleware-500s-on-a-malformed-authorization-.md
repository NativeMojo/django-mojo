---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-012
type: bug
title: Auth middleware 500s on a malformed Authorization header (scheme-less / empty / 3+ parts)
priority: P2
effort: XS
owner: backend
opened: 2026-07-03
depends_on: []
related: []
links: []
---

# Auth middleware 500s on a malformed Authorization header (scheme-less / empty / 3+ parts)

## What & Why
`AuthenticationMiddleware.process_request` unpacks the `Authorization` header with an
unguarded `prefix, token = token.split()`. `str.split()` returns exactly two elements
only when the value is exactly two whitespace-separated parts. Any other shape raises
`ValueError` (too few / too many values to unpack), which nothing catches — Django turns
it into an **HTTP 500 on every affected request**.

This is a robustness / minor-DoS bug: any client can 500 a request just by sending a
malformed header, and it breaks legitimate integrations in normal operation. Concrete
case: **Coinflow payment webhooks send `Authorization: <validation_key>` with no scheme**
(a single token), which hits this crash on every delivery. The `mverify_api` project had
to add a local `WebhookAuthShimMiddleware` to work around it — but the framework should
not 500 on a malformed header regardless of who sends it.

## Acceptance Criteria
- [ ] A request whose `Authorization` header is malformed — scheme-less single token
  (`abc123`), empty string, or 3+ whitespace-separated parts (`Bearer a b`) — never
  produces an HTTP 500 from `AuthenticationMiddleware`.
- [ ] Well-formed headers (`Bearer <jwt>`, `apikey <key>`) authenticate exactly as before —
  no regression to the existing bearer/apikey/unknown-scheme paths.
- [ ] Malformed headers never authenticate: `request.bearer` / `request.user` stay unset, so
  protected endpoints still reject (fail-closed). A bare scheme-less single token is exposed
  on `request.auth_token` (prefix `"raw"`, per Investigation) for downstream validation;
  empty and 3+-part headers are plain passthrough.
- [ ] A middleware regression test covers scheme-less single token, empty string, and 3+
  parts — each asserts no 500 and that `request.user` stays anonymous; the single-token case
  also asserts `request.auth_token.token` equals the bare value. Fails on current code, passes
  after the fix.

## Repro — bugs only
1. Send any HTTP request to a django-mojo app with header `Authorization: abc123`
   (a single token, no `Bearer `/`apikey ` scheme) — e.g. a Coinflow webhook delivery.
- Expected: request is processed; it's simply treated as unauthenticated (same as sending
  no `Authorization` header), and downstream auth/permission logic decides the outcome.
- Actual: `ValueError: not enough values to unpack (expected 2, got 1)` raised at
  `mojo/middleware/auth.py:27` → unhandled → **HTTP 500**.

Also reproduces with:
- `Authorization:` (empty value) → `"".split()` → `[]` → `ValueError` → 500.
- `Authorization: Bearer a b` (3 parts) → `ValueError: too many values to unpack` → 500.

## Investigation
**Root cause — confirmed.** `mojo/middleware/auth.py:27`, in
`AuthenticationMiddleware.process_request`:

```python
22    def process_request(self, request):
23        request.bearer = None
24        token = request.META.get('HTTP_AUTHORIZATION', None)
25        if token is None:
26            return
27        prefix, token = token.split()      # <-- unguarded: ValueError unless exactly 2 parts
28        prefix = prefix.lower()
```

`token.split()` (no args) splits on any run of whitespace. It yields 2 elements only for a
well-formed `"<scheme> <token>"`. For 0, 1, or 3+ parts the tuple-unpack raises `ValueError`,
and there is no `try/except` anywhere in `process_request`, so the request 500s.

**Code path / behavior contract.**
- Line 25–26: a *missing* header already returns `None`, which in this middleware means
  "continue; request stays unauthenticated" (`request.bearer` was set to `None` on line 23,
  `request.user` is left anonymous). Downstream permission checks (`VIEW_PERMS`/`SAVE_PERMS`)
  then reject protected endpoints with the normal 401/403.
- Line 29–31: an *unknown scheme* returns `JsonResponse({'error': f'Invalid token type:
  {prefix}', ...}, status=401)` — the file's established rejection style.
- Line 43–44: a handler-level auth failure returns `JsonResponse({'error': error}, status=401)`.

So the middleware already has two exits — silent passthrough (`return`) for "no credentials",
and `JsonResponse(status=401)` for "bad credentials".

**Recommended fix (refined — passthrough + expose the bare token).** Guard the split. Treat a
*bare scheme-less token* (a single part — the Coinflow case) as **unauthenticated but
readable**: expose it on `request.auth_token` so a downstream/public endpoint can validate it
itself, without authenticating the request. Treat empty and 3+-part headers as plain "no
credentials". In every case `request.bearer` stays `None`, so the request is unauthenticated
and fail-closed:

```python
token = request.META.get('HTTP_AUTHORIZATION', None)
if token is None:
    return
parts = token.split()
if len(parts) == 1:
    # bare scheme-less token (e.g. a Coinflow webhook validation key): expose it so a
    # downstream/public endpoint can read it, but do NOT authenticate.
    request.auth_token = objict(prefix="raw", token=parts[0])
    return                 # request.bearer stays None -> still unauthenticated / fail-closed
if len(parts) != 2:
    return                 # empty or 3+ parts: genuinely malformed -> no credentials
prefix, token = parts
prefix = prefix.lower()
```

**Why exposing `request.auth_token` is safe (verified against readers).** `request.auth_token`
is *informational*, not an auth signal — the auth gate is `request.bearer` / `request.user`,
which we leave unset. The only non-trivial reader is
`mojo/apps/account/services/fresh_auth.py`: `token_auth_time` (line 44) JWT-decodes
`request.auth_token.token`, but (a) it's wrapped in `try/except Exception: return None`, so a
non-JWT bare token degrades gracefully, and (b) it's only reached when
`request.bearer == "bearer"` (`is_fresh`, line 74) — never true for the bare-token case. The
bearer-required decorator (`mojo/decorators/auth.py:272`) and incident logging
(`mojo/apps/incident/reporter.py:81`) both key off `request.bearer`, not `auth_token`. So a
bare token grants nothing.

**Prefix name — use `"raw"` (or `"bare"`), NOT `"none"`/`"base"`.** `"none"` the string
collides with Python `None` (a `prefix is None` / `== None` check reads wrong — a latent
footgun); `"base"` doesn't convey meaning. `"raw"` reads unambiguously as "unscoped bare
token." Leave `request.bearer = None` — do **not** set it to the synthetic prefix, so
`request.bearer` keeps meaning "a recognized, handler-backed scheme was presented" (setting it
would, e.g., start logging `"raw"` as a bearer type in incident events at `reporter.py:81`).

**Design decision to lock in /scope — passthrough vs. 401.**
- **passthrough (recommended):** a garbage/scheme-less header is more like "the client isn't
  attempting bearer auth" than "the client tried the wrong scheme." Unauthenticated
  passthrough is identical to the existing no-header behavior, is fail-closed (protected routes
  still 401/403), preserves backwards compatibility, and lets a scheme-less webhook (Coinflow)
  reach a downstream handler that validates its own key. Reporter's primary suggestion.
- **`JsonResponse({'error': 'Malformed Authorization header'}, status=401)` (alternative):**
  matches the "Invalid token type" 401 style and gives an explicit error, but hard-rejects at
  the middleware, breaking the scheme-less-webhook passthrough that motivated this bug.
  Rejected unless /scope prefers strict rejection.
- Keep exact-count checks rather than `split(maxsplit=1)`: the registered handlers (`bearer`
  JWT, `apikey`) have no spaces in their tokens, so preserving "exactly 2 parts = scheme +
  token" keeps current valid-input behavior; do not widen the contract to allow spaces.

**Regression-test feasibility — straightforward.** Tests drive the real HTTP stack via
`opts.client` (separate server process), using `testit` (`@th.django_unit_test(...)`,
`def test_xxx(opts):`). The client builds the header as `f"{bearer} {token}"`; a malformed
case can be produced by setting a raw `Authorization` header directly on the request (single
token / empty / 3-part) rather than the well-formed `bearer token` form. No existing
middleware test file covers Authorization parsing — add one under
`tests/test_middleware/` (new dir) following the existing testit pattern. Each case asserts
the response status is **not** 500 (and, per the locked decision, that a no-auth-required
path succeeds while an auth-required path returns 401/403 — i.e. the header was treated as
"no credentials", not as a valid session).

**Docs to touch.** `docs/django_developer/account/auth.md` ("Middleware Auth Flow",
~lines 186–204) and `docs/django_developer/core/middleware.md` (AuthMiddleware, ~lines
109–124) document the happy path only; add the malformed-header behavior.

## Plan

### Goal
Stop `AuthenticationMiddleware` from returning HTTP 500 on a malformed `Authorization`
header: guard the `split()` unpack, and expose a bare scheme-less token on
`request.auth_token` (prefix `"raw"`) without authenticating the request.

### Context — what exists
`mojo/middleware/auth.py` (full method today; `objict` is already imported at line 9):

```python
21  class AuthenticationMiddleware(MiddlewareMixin):
22      def process_request(self, request):
23          request.bearer = None
24          token = request.META.get('HTTP_AUTHORIZATION', None)
25          if token is None:
26              return
27          prefix, token = token.split()          # <-- BUG: ValueError unless exactly 2 parts
28          prefix = prefix.lower()
29          if prefix not in AUTH_BEARER_HANDLERS_CACHE:
30              if prefix not in AUTH_BEARER_HANDLER_PATHS:
31                  return JsonResponse({'error': f'Invalid token type: {prefix}', ...}, status=401)
...          (loads handler, runs it, 401 on handler error)
38          handler = AUTH_BEARER_HANDLERS_CACHE[prefix]
39          request.auth_token = objict(prefix=prefix, token=token)   # existing auth_token shape
...
47          request.bearer = prefix
```

- Returning `None` from `process_request` = "continue; request stays unauthenticated" (same
  as the existing line 25–26 no-header path). `request.bearer`/`request.user` are the real
  auth signals; `request.auth_token` is informational (see Investigation for the verified
  reader analysis — `fresh_auth.py` is `try/except`-guarded and only runs when
  `bearer == "bearer"`; `decorators/auth.py:272` and `incident/reporter.py:81` key off
  `request.bearer`). A bare token grants nothing.
- Middleware is installed in the test project: `testproject/config/settings/local/__init__.py:57`
  lists `mojo.middleware.auth.AuthenticationMiddleware`, so `opts.client` requests pass
  through it (integration test is viable).
- Test infra: `tests/test_auth/fresh_auth.py:74` fabricates fake requests with
  `objict(bearer=..., auth_token=objict(token=...), META={})` — the pattern to reuse for an
  in-process middleware call. `testit/client.py` `_make_request` does
  `headers.update(kwargs.pop("headers"))`, so a `headers={"Authorization": ...}` kwarg on
  `opts.client.get(...)` overrides the default auth header. Public endpoint lives in
  `mojo/apps/account/rest/auth_config.py` (`@md.public_endpoint()`).

### Changes — what to do
1. `mojo/middleware/auth.py` — replace the unguarded unpack. Exact edit:

   Replace:
   ```python
           token = request.META.get('HTTP_AUTHORIZATION', None)
           if token is None:
               return
           prefix, token = token.split()
           prefix = prefix.lower()
   ```
   With:
   ```python
           token = request.META.get('HTTP_AUTHORIZATION', None)
           if token is None:
               return
           parts = token.split()
           if len(parts) == 1:
               # bare, scheme-less token (e.g. a Coinflow webhook validation key):
               # expose it for a downstream/public endpoint to read, but do NOT
               # authenticate — request.bearer stays None (fail-closed).
               request.auth_token = objict(prefix="raw", token=parts[0])
               return
           if len(parts) != 2:
               return  # empty or 3+ parts: genuinely malformed -> no credentials
           prefix, token = parts
           prefix = prefix.lower()
   ```
   No new import (`objict` already imported at line 9). Everything from line 29 onward is
   unchanged.

2. `tests/test_middleware/__init__.py` — new package init. Mirror the TESTIT config of
   `tests/test_auth/__init__.py` (the account app must be available — the tests import the
   middleware, which imports account models).

3. `tests/test_middleware/auth_malformed_header.py` — new regression test (below).

### Design decisions
- **Passthrough, not 401.** A scheme-less/garbage header is treated as "no credentials"
  (identical to the existing no-header path) rather than hard-rejected. Fail-closed (protected
  endpoints still 401/403 via the permission layer), backwards-compatible, and it lets the
  scheme-less Coinflow webhook reach a downstream handler that validates its own key. A 401
  would break that passthrough. (Settled with reporter.)
- **Expose the bare token on `request.auth_token`, prefix `"raw"`, leave `request.bearer =
  None`.** Gives downstream a clean handle (`request.auth_token.token`) instead of re-parsing
  the raw header. `"raw"` over `"none"` (string collides with Python `None` — latent footgun)
  and over `"base"` (vague). `request.bearer` is left unset so it keeps meaning "a recognized
  handler-backed scheme was presented" (setting it would log `"raw"` as a bearer in incident
  events).
- **Only the single-part case is exposed.** Empty and 3+-part headers have no meaningful
  single token → plain passthrough, `auth_token` not set.
- **Exact-count checks, not `split(maxsplit=1)`.** Registered handlers (`bearer` JWT,
  `apikey`) have no spaces in their tokens, so "exactly 2 = scheme+token" preserves current
  valid-input behavior without widening the contract.

### Edge cases & risks
- Empty string `""` — line 25 `is None` does NOT catch it; `"".split()` → `[]` → `len != 2` →
  return. Handled.
- Whitespace-only `"   "` — `.split()` → `[]` → return. Handled.
- Bare token with stray whitespace `" tok "` — `.split()` → `["tok"]` → exposed as `"tok"`.
  Fine.
- Valid `Bearer <jwt>` / `apikey <key>` — `len == 2` path is byte-for-byte the old behavior;
  no regression (existing `tests/test_auth/` covers the happy path).
- `request.auth_token` exposure is safe — verified against all readers (see Investigation); no
  code treats its presence as proof of auth.

### Tests
New file `tests/test_middleware/auth_malformed_header.py`. Run:
`bin/run_tests --agent -t test_middleware.auth_malformed_header`.

Three in-process unit tests (authoritative regression — each **raises `ValueError` at line 27
on current code**, so it fails-while-broken and passes only after the fix):

```python
from testit import helpers as th
from objict import objict

@th.django_unit_test("auth middleware: bare scheme-less token passes through and is exposed as prefix 'raw'")
def test_bare_single_token(opts):
    from mojo.middleware.auth import AuthenticationMiddleware
    req = objict(META={'HTTP_AUTHORIZATION': 'baretoken123'}, bearer=None)
    result = AuthenticationMiddleware().process_request(req)
    assert result is None, "malformed header must pass through (return None), not crash or reject"
    assert req.bearer is None, "bare token must NOT authenticate: request.bearer must stay None"
    assert getattr(req, 'auth_token', None) is not None, "bare token must be exposed on request.auth_token"
    assert req.auth_token.prefix == 'raw', f"bare-token prefix must be 'raw', got {req.auth_token.prefix!r}"
    assert req.auth_token.token == 'baretoken123', f"bare-token value must be preserved, got {req.auth_token.token!r}"

@th.django_unit_test("auth middleware: empty Authorization header passes through without 500")
def test_empty_header(opts):
    from mojo.middleware.auth import AuthenticationMiddleware
    req = objict(META={'HTTP_AUTHORIZATION': ''}, bearer=None)
    result = AuthenticationMiddleware().process_request(req)
    assert result is None, "empty Authorization header must pass through (return None), not crash"
    assert req.bearer is None, "empty header must leave request.bearer None"
    assert getattr(req, 'auth_token', None) is None, "empty header must not set request.auth_token"

@th.django_unit_test("auth middleware: 3+ part Authorization header passes through without 500")
def test_three_part_header(opts):
    from mojo.middleware.auth import AuthenticationMiddleware
    req = objict(META={'HTTP_AUTHORIZATION': 'bearer tok extra'}, bearer=None)
    result = AuthenticationMiddleware().process_request(req)
    assert result is None, "3+ part Authorization header must pass through (return None), not crash"
    assert req.bearer is None, "3+ part header must leave request.bearer None"
    assert getattr(req, 'auth_token', None) is None, "3+ part header must not set request.auth_token"
```

One integration smoke test (faithful real-stack repro — hits the actual 500 path):

```python
@th.django_unit_test("auth middleware via HTTP: malformed header returns 200 on public endpoint, not 500")
def test_http_public_endpoint_no_500(opts):
    # AuthenticationMiddleware is in the test-project MIDDLEWARE; the headers= override
    # wins over the client's default Authorization header.
    resp = opts.client.get(PUBLIC_URL, headers={"Authorization": "baretoken123"})
    assert resp.status_code != 500, f"malformed Authorization header must not 500 (got {resp.status_code})"
    assert resp.status_code == 200, f"public endpoint must still succeed with a malformed header (got {resp.status_code})"
```

Builder: set `PUBLIC_URL` to the real path — read the `@md.URL(...)` decorator in
`mojo/apps/account/rest/auth_config.py` (likely `/api/account/auth/config`; do not guess —
confirm it). If `objict` raises on an unset attribute instead of returning `None`, the
`getattr(req, 'auth_token', None)` form already handles it.

### Docs
- `docs/django_developer/account/auth.md` ("Middleware Auth Flow", ~L186–204) and
  `docs/django_developer/core/middleware.md` (AuthMiddleware, ~L109–124): document malformed-
  header behavior — bare token → `request.auth_token` (prefix `"raw"`), unauthenticated;
  empty/3+ → passthrough; never 500.
- `CHANGELOG.md`: note the robustness fix.
- No `docs/web_developer/` change (server-side middleware robustness; no REST contract change).

### Open questions
None. (Passthrough-vs-401, prefix `"raw"`, and `auth_token` exposure are all resolved.)

## Notes
Discovered while building `MVERIFY-API-007` (wallet payments) in the `mverify_api` project.
Coinflow webhooks carry a scheme-less `Authorization: <validation_key>`. That project shipped
a local `WebhookAuthShimMiddleware` as a workaround; this item fixes the framework so the shim
is no longer required to avoid a 500 — and, with the bare token exposed on
`request.auth_token.token` (prefix `"raw"`), the webhook endpoint can read the validation key
from there instead of re-parsing the raw header, so the shim can likely be retired entirely.
Fix is a small guard (~6 lines) plus a regression test; effort ~XS.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
