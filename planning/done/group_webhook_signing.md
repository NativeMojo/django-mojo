# Feature: Group-Scoped Webhook Signing Primitive

**Type**: feature
**Status**: resolved
**Date**: 2026-05-17
**Requested by**: downstream consumer integration

## Background

Every SaaS built on django-mojo emits webhooks tied to a Group's data. Today each service rolls its own signing scheme — usually HMAC-SHA256 with a per-consumer shared secret that the operator manually coordinates between sender and receiver:

1. Operator picks a random string.
2. Sets it on the emitting service (webhook config UI).
3. Copies it to the receiver (consumer's webhook-secret config field).
4. Re-coordinates on every rotation.

This is error-prone (typo / drift), duplicates code across services, and forces every consumer to ship its own verifier. A prior downstream request collapsed credentials to one per-Group ApiKey for all integrated services; the webhook secret is the last remaining piece of operator-coordinated state.

## Proposal

Standardize Group-scoped webhook signing at the framework layer. Every django-mojo service that emits webhooks signs with the same HMAC-SHA256 helper keyed on a per-Group secret stored on the framework's `Group` model. Consumers verify with one shared helper regardless of which service emitted the webhook.

The operator never sees or types the secret. The consumer fetches it once with its ApiKey and caches it locally.

### Endpoint shape

One endpoint, behavior driven by body — matches the existing `POST_SAVE_ACTIONS` convention:

```
POST /api/account/group/webhook_secret
```

| Body | Behavior |
|---|---|
| `{}` (empty) | Return current secret; auto-create on first call if not yet set |
| `{rotate: true}` | Generate a new secret, return it, invalidate the prior |

Response (both forms):

```json
{
  "status": true,
  "data": {
    "secret": "wsec_<48 chars>",
    "created_at": "2026-05-17T...",
    "last_rotated_at": "2026-05-17T..."
  }
}
```

Auth: any `ApiKey` for the Group with `manage_group` permission (same threshold as ApiKey CRUD — if you can mint keys you can read the signing secret).

### Storage

`Group` gains one `MojoSecret` field, `webhook_secret`. Accessed via `group.get_webhook_secret()` and set via `group.set_webhook_secret(value)`. Auto-generated on first read if empty (the GET-style call into the new endpoint triggers this).

Rationale for "single secret per Group" (vs. Stripe-style per-endpoint): the common case is one consumer per Group (one operator runs one receiver). Multiple per-endpoint secrets are a feature we can add later via a sibling `WebhookEndpoint` model if anyone needs them. v1 keeps the primitive minimal.

### Framework helpers

`mojo/apps/account/webhook_signer.py` (new):

```python
def sign_for_group(group, body_bytes):
    """Return hex HMAC-SHA256 of body_bytes keyed on the Group's secret."""

def verify_for_group(group, body_bytes, signature_header):
    """Constant-time-compare an inbound signature against the expected HMAC."""
```

Consuming services replace their `_validate_signature` (or equivalent) with a single call into `verify_for_group`.

### `jobs.publish_webhook` integration

All webhook emission across django-mojo + downstream services flows through `mojo.apps.jobs.publish_webhook`. Add a `group=` kwarg so signing happens automatically — emitters just pass the Group whose data the webhook represents, and never touch HMAC themselves:

```python
def publish_webhook(url, data, *, group=None, headers=None, ...):
    ...
    if group is not None:
        body_bytes = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        webhook_headers["X-Mojo-Signature"] = webhook_signer.sign_for_group(group, body_bytes)
```

Existing call sites adopt by adding `group=...`:

`sign_for_group` is exported in case a caller needs to sign outside the Jobs queue (rare), but the default path is "pass `group` to `publish_webhook`, get a signed body for free."

### Signature header

Standardize on `X-Mojo-Signature: <hex-hmac-sha256>`. Existing service-specific headers become aliases for one release cycle, then go away.

## Acceptance Criteria

### Framework

- [ ] `Group.webhook_secret` field (MojoSecret) with `get_webhook_secret()` / `set_webhook_secret()` accessors.
- [ ] `POST /api/account/group/webhook_secret` endpoint at `mojo/apps/account/rest/group.py` (or a new sibling file):
  - Empty body returns current secret; auto-creates on first call.
  - `{rotate: true}` rotates and returns the new value.
  - Response shape matches the standard envelope above.
  - Permission: `manage_group` (or higher) on the calling ApiKey for that Group.
- [ ] `mojo/apps/account/webhook_signer.py` with `sign_for_group` and `verify_for_group` helpers. Constant-time signature comparison.
- [ ] Standard header constant `X-Mojo-Signature` exported from the same module.
- [ ] Auto-generation on first call uses `crypto.random_string(48)` (matches ApiKey token generation).
- [ ] **Ask user to run `makemigrations` and `migrate`** after adding the field.

### Tests

- [ ] `tests/test_account/test_webhook_secret.py` (or wherever account REST tests live):
  - First GET-style POST auto-creates the secret; subsequent calls return the same value.
  - `{rotate: true}` returns a new secret distinct from the previous; subsequent fetches return the new one.
  - Without `manage_group` permission, returns 403.
  - Two Groups produce two distinct secrets.
- [ ] `tests/test_account/test_webhook_signer.py`:
  - `sign_for_group(group, body)` produces the same hex for the same input.
  - `verify_for_group(group, body, sig)` accepts a valid signature and rejects a tampered body.
  - `verify_for_group` uses constant-time comparison (smoke test — at minimum, calls `hmac.compare_digest`).

### Docs

- [ ] New developer doc: `docs/django_developer/account/webhook_signing.md` — explains the primitive, the helper, the header, and how downstream services adopt it.
- [ ] Update `docs/django_developer/account/README.md` to link to it.
- [ ] Update `mkdocs.yml`.

## Downstream Migration (out of scope, separate work)

Once this lands, each affected downstream service handles its own migration in a separate request:

- Emitting services swap their webhook emit path to call `webhook_signer.sign_for_group(group, body)` with header `X-Mojo-Signature`. Existing service-specific signature headers alias for one release.
- Consuming services drop their local `webhook_secret` config fields and switch signature validation to `verify_for_group(remote_group, request.body, header)`.

## Open Questions

1. **Auto-create on first read vs. require explicit creation?** Auto-create is simpler — first `POST /api/account/group/webhook_secret` with empty body returns a freshly-minted secret. Alternative: require `{create: true}` to mint, return 404-ish on first read. Auto-create is friendlier and matches the operator workflow ("just give me a secret").
2. **Rotation overlap window?** Stripe invalidates the old secret immediately on rotate. Some services support a brief overlap (both old + new valid for N seconds) so in-flight webhooks aren't rejected. Lean: no overlap for v1 — receivers should be tolerant of brief signature-mismatch errors during rotation, same as ApiKey rotation. Adds complexity if overlap is needed.
3. **Webhook delivery system?** Out of scope here — this request only standardizes the signing primitive. Actual delivery (queueing, retries, dead-letter) stays with each emitting service for now.
4. **Per-endpoint secrets?** Punted to a future request; single per-Group secret is the v1 primitive.

---
<!-- Filled in on resolution -->

## Resolution

**Status**: Resolved — 2026-05-17
**Commit**: e9fc1b3

### What Was Built

Framework-level Group-scoped webhook signing primitive (HMAC-SHA256, per-Group secret) plus auto-signing integration with `jobs.publish_webhook`. Outbound webhooks pass `group=` and get signed at delivery time; inbound consumers verify with a one-line request helper. Operator never touches the secret directly — the framework owns it.

### Files Changed

- `mojo/apps/account/models/group.py` — `get_webhook_secret(auto_create=False)`, `get_webhook_secret_info`, `rotate_webhook_secret`, `_mint_webhook_secret_value`. Secret stored in existing `mojo_secrets` blob — no migration.
- `mojo/apps/account/rest/group.py` — `POST /api/group/webhook_secret` endpoint (read or `{rotate: true}`), `manage_group`-or-higher permission.
- `mojo/helpers/crypto/sign.py` — `WEBHOOK_SIGNATURE_HEADER = "X-Mojo-Signature"`, `sign_for_group(group, body_bytes)`.
- `mojo/helpers/crypto/__init__.py` — re-export `sign_for_group`.
- `mojo/helpers/request.py` — `verify_signed_request(request, secret, header=…)` — pulls raw body + header, returns bool.
- `mojo/apps/jobs/__init__.py` — `publish_webhook(..., group=None, ...)`; stores `sign_group_id` (never the secret) in the job payload.
- `mojo/apps/jobs/handlers/webhook.py` — at delivery, if `sign_group_id` set: canonicalize body (sorted keys, compact separators), HMAC, inject `X-Mojo-Signature`, send with `data=body_bytes` instead of `json=data`. Missing sign group → `'failed'` with `error_type='sign_group_missing'`, no retry. Unsigned path unchanged.

### Tests

- `tests/test_account/test_webhook_secret.py` — accessor defaults, auto-mint, rotation, REST endpoint (auto-create + rotate + 403 + multi-group). 8 cases.
- `tests/test_account/test_webhook_signer.py` — `sign_for_group` determinism + auto-mint, `verify_signed_request` happy path + tampered body + no-secret + no-header + cross-group rejection, constant-time-compare smoke. 8 cases.
- `tests/test_jobs/test_signed_webhook.py` — payload shape (no secret in queue), header injection, rotation-safety on retry, missing-group failure mode, unsigned-path regression guard. 6 cases.
- Run: `bin/run_tests --agent -t test_account.test_webhook_secret -t test_account.test_webhook_signer -t test_jobs.test_signed_webhook` — all 22 pass.

### Docs Updated

- New: `docs/django_developer/account/webhook_signing.md`, `docs/web_developer/account/webhook_signing.md`
- Updated: `docs/django_developer/account/README.md`, `docs/web_developer/account/README.md`, `docs/django_developer/jobs/publishing.md` (new "Signed webhooks" section), `mkdocs.yml` (2 nav entries), `CHANGELOG.md`

### Design Notes (key decisions)

- Storage reuses existing `MojoSecrets` blob — no migration.
- `auto_create=False` by default on `get_webhook_secret()` so verify paths can never accidentally mint a matching secret for a tampered request.
- Signing at handler-time (not publish-time): rotation-safe retries, secret never enters the queue payload, wire-bytes/signature consistency guaranteed.
- Just `group=` (no `sign=True` flag) — presence is intent. Forgetting a flag can't silently send unsigned.
- Helpers split between `mojo.helpers.crypto.sign` (sign primitives + group sugar) and `mojo.helpers.request` (Django request adapter). No new `webhook_signer.py` module — KISS.

### Follow-up (out of scope)

- Downstream emitting services migrate their bespoke signing to `publish_webhook(group=...)` in separate requests; legacy headers alias for one release.
- Downstream consuming services drop their local `webhook_secret` config fields and switch validation to `verify_signed_request`.
- Per-endpoint secrets (vs. per-Group) — punted; can add a sibling `WebhookEndpoint` model later if needed.

---

## Plan

**Status**: planned
**Planned**: 2026-05-17

### Objective
Add a framework-level Group-scoped webhook signing primitive: per-Group HMAC-SHA256 secret stored in `Group.mojo_secrets`, a `POST /api/group/webhook_secret` endpoint to read/rotate it, `webhook_signer` helpers, and a `group=` kwarg on `jobs.publish_webhook` that auto-signs every outbound webhook at delivery time.

### Design Decisions
- **URL prefix**: `/api/group/webhook_secret` — account app has `APP_NAME = ""`, matches existing `/api/group/apikey` (correcting the request's `/api/account/group/...` shorthand).
- **Storage**: Reuse existing `Group.mojo_secrets` (`MojoSecrets` mixin already present) — no new column, no migration. Store a single dict under key `webhook_secret`: `{"value": "wsec_…", "created_at": iso, "last_rotated_at": iso}`. Mirrors how `ApiKey` stores `token` inside its own `mojo_secrets`.
- **Signer**: Wrap existing [crypto.sign](mojo/helpers/crypto/sign.py) (`generate_signature` / `verify_signature` already HMAC-SHA256 + constant-time) — `webhook_signer.py` only contains the Group→secret lookup glue plus the `X-Mojo-Signature` header constant.
- **Token shape**: `wsec_` prefix + `crypto.random_string(48, allow_special=False)` (48 alphanumeric chars, matches `ApiKey.generate_token`). Total length 53.
- **Permission**: `@md.requires_perms("manage_group", "manage_groups", "groups")` — same threshold as `ApiKey` CRUD. Works for both API key auth (group derived from `request.api_key.group`) and user auth (group derived from `request.DATA.group`).
- **Auto-create**: First call with empty body mints the secret transparently. No separate "create" verb.
- **Rotation**: No overlap window. `{rotate: true}` mints a new value, replaces the stored `value`, updates `last_rotated_at`. Old secret immediately invalid (mirrors v1 ApiKey rotation behavior).
- **Method**: Endpoint accepts only `POST` — keeps GET safe/cacheable and prevents browser cache or referrer leaks of an auto-created secret.
- **Jobs API shape**: `publish_webhook(url, data, *, group=None, ...)`. Presence of `group` is the intent — no separate `sign=True` flag. Footgun-free: forgetting a flag can't silently produce unsigned webhooks.
- **Sign timing**: Handler-time, not publish-time. `publish_webhook` stores `sign_group_id` (int) in the job payload — never the secret. `post_webhook` looks up the Group, canonicalizes the body, computes HMAC, and sends those exact bytes. Rationale: (a) retries after secret rotation re-sign with the current secret; (b) secret never enters the queue (Redis/DB); (c) handler controls both signature and wire bytes so they're guaranteed consistent.
- **Canonical body**: `json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")`. Sorted keys + compact separators give the publisher and receiver a deterministic byte sequence to hash. Handler sends with `data=body_bytes` + `Content-Type: application/json` (replaces the existing `json=data` path when signing is active).

### Steps

1. `mojo/apps/account/models/group.py` — add three methods to `Group`:
   - `get_webhook_secret(auto_create=True) -> str | None`: reads `self.get_secret("webhook_secret")`; if `auto_create`, mints + saves on first read; returns the raw `wsec_…` value.
   - `get_webhook_secret_info(auto_create=True) -> objict`: returns `{value, created_at, last_rotated_at}` dict (same shape as the REST response `data`).
   - `rotate_webhook_secret() -> objict`: generates a new value, updates `last_rotated_at`, saves, returns the same info dict.
   - Helper `_mint_webhook_secret()` (private): `crypto.random_string(48, allow_special=False)` → `"wsec_" + token`.
   - All use `set_secret("webhook_secret", {...})` + `self.save()` (MojoSecrets `save_secrets` handles encryption).

2. `mojo/apps/account/webhook_signer.py` (new):
   ```python
   from mojo.helpers.crypto.sign import generate_signature, verify_signature

   SIGNATURE_HEADER = "X-Mojo-Signature"

   def sign_for_group(group, body_bytes):
       secret = group.get_webhook_secret(auto_create=True)
       return generate_signature(body_bytes, secret)

   def verify_for_group(group, body_bytes, signature_header):
       if not signature_header:
           return False
       secret = group.get_webhook_secret(auto_create=False)
       if not secret:
           return False
       return verify_signature(body_bytes, signature_header, secret)
   ```

3. `mojo/apps/account/rest/group.py` — add new endpoint:
   ```python
   @md.POST('group/webhook_secret')
   @md.requires_perms("manage_group", "manage_groups", "groups")
   def on_group_webhook_secret(request):
       group = request.group
       if group is None and "group" in request.DATA:
           group = Group.objects.filter(pk=int(request.DATA.group)).first()
       if group is None:
           raise merrors.PermissionDeniedException("group required")
       if request.DATA.get("rotate") is True:
           info = group.rotate_webhook_secret()
       else:
           info = group.get_webhook_secret_info(auto_create=True)
       return {"status": True, "data": info}
   ```
   (`@md.requires_perms` already does the group-context check via `REQUIRES_PERMS_IS_GROUP=True`.)

4. `mojo/apps/jobs/__init__.py::publish_webhook` — add `group=None` kwarg. If set:
   - Resolve to int id: `group_id = group.id if hasattr(group, "id") else int(group)`.
   - Add `payload["sign_group_id"] = group_id`.
   - Do **not** compute the signature here — handler does it. Do not store the secret in the payload.
   - Keep the existing `headers` kwarg behavior unchanged; the handler will inject `X-Mojo-Signature` on top (overriding if the caller pre-set it).

5. `mojo/apps/jobs/handlers/webhook.py::post_webhook` — extend payload handling:
   - After existing url/data validation, if `payload.get("sign_group_id")` is set:
     - Lookup: `Group.objects.filter(pk=sign_group_id).first()`. If missing → mark `error_type='sign_group_missing'` and return `'failed'` (do not retry — it won't recover).
     - Canonicalize body: `body_bytes = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")`.
     - Sign: `headers[webhook_signer.SIGNATURE_HEADER] = webhook_signer.sign_for_group(group, body_bytes)`.
     - Switch the `requests.post(...)` call: replace `json=data` with `data=body_bytes` and ensure `Content-Type: application/json` is in headers (publisher already sets it by default).
     - Record `payload['signed'] = True` and `payload['sign_group_id']` in `job.metadata` for debugging (signature itself goes through `_sanitize_headers` and is masked in logs).
   - Unsigned path stays exactly as today (`json=data`, no changes).

6. `tests/test_account/test_webhook_secret.py` (new) — REST + behavior tests (testit, `@th.django_unit_setup` + `@th.django_unit_test()` per `.claude/rules/testing.md`).

7. `tests/test_account/test_webhook_signer.py` (new) — helper unit tests.

8. `tests/test_jobs/test_signed_webhook.py` (new) — `publish_webhook(group=...)` integration: payload contains `sign_group_id`, no secret in payload; handler unit-test (mocking `requests.post`) verifies `X-Mojo-Signature` header value matches `webhook_signer.sign_for_group(group, body_bytes)` and `requests.post` is called with `data=body_bytes` not `json=data`.

9. `docs/django_developer/account/webhook_signing.md` (new) — primitive overview, `Group` accessor API, `webhook_signer` helper, header constant, **and a "Recommended path: use `jobs.publish_webhook(group=...)`"** subsection that shows the one-line emitter call. The raw `sign_for_group` helper is documented as the escape hatch.

10. `docs/django_developer/account/README.md` — add link to the new doc under the existing API Keys row.

11. `docs/django_developer/jobs/publishing.md` — add a "Signed webhooks" section showing `publish_webhook(url, data, group=org_group)` and what the receiver sees (`X-Mojo-Signature`, canonical body).

12. `docs/web_developer/account/webhook_signing.md` (new) — REST endpoint contract, request/response shape, auth, rotation semantics. Cross-link to django doc.

13. `docs/web_developer/account/README.md` — add link.

14. `mkdocs.yml` — add nav entry under both **Django Developer → Account** (sibling of API Keys) and **REST API → Account** (sibling of API Keys).

15. `CHANGELOG.md` — entry under upcoming version: "Added Group-scoped webhook signing primitive (`POST /api/group/webhook_secret`, `webhook_signer` helpers, and `jobs.publish_webhook(group=...)` auto-signing)."

### User Cases
- **First-time read** (operator hits the new endpoint with no secret yet): Empty body POST returns a freshly-minted `wsec_…` with `created_at == last_rotated_at == now()`. Subsequent empty-body POSTs return the same value, same `created_at`, same `last_rotated_at`.
- **Rotation**: `{rotate: true}` returns a new distinct secret, `created_at` preserved, `last_rotated_at` updated. Old signatures (signed with prior secret) fail verification immediately.
- **API key auth path**: Consumer/operator bot authenticates with `Authorization: apikey <token>` whose key has `manage_group`. `request.group` is set by `ApiKey.validate_token`; endpoint uses it directly. No `group` body param needed.
- **User auth path**: Operator with `manage_group` (member-level) or `manage_groups` (system-level) authenticates via session/JWT and sends `{"group": 42}` in body. `requires_perms` resolves the group and checks membership perms.
- **Emitter usage (recommended)**: `jobs.publish_webhook(url=receiver_url, data={...}, group=org_group)` — that's it. The job handler signs at delivery, retries pick up the current secret, the secret never enters the queue.
- **Emitter usage (escape hatch)**: For non-jobs paths (rare — synchronous webhooks, custom transports), `sig = webhook_signer.sign_for_group(group, body_bytes); response.headers[webhook_signer.SIGNATURE_HEADER] = sig`.
- **Consumer usage** (downstream consuming service): `if not webhook_signer.verify_for_group(group, request.body, request.headers.get("X-Mojo-Signature")): reject()`. Receivers must hash the raw request body bytes (no re-serialization) — the canonical body the sender used is what arrives on the wire.

### Edge Cases
- **Verify before secret exists**: `verify_for_group` is called against a Group that has never minted a secret → returns `False` (no auto-create on verify; only the endpoint and `sign_for_group` mint). Prevents a tampered request from silently creating + matching its own freshly-minted secret.
- **Missing signature header**: `verify_for_group(group, body, None)` or `""` → returns `False`, no exception.
- **Wrong group**: API key for Group A trying to read Group B's secret → `requires_perms` group check rejects (`manage_group` only granted on A). 403.
- **`{rotate: false}` or no `rotate` key**: Treated as read (auto-create on first call). Only `rotate == True` (boolean) triggers rotation — string `"true"` ignored to be conservative.
- **Concurrent rotation**: Two clients hit `{rotate: true}` at once → last writer wins. Both clients see a valid (different) secret. Acceptable; same as ApiKey rotation today.
- **Empty body bytes during signing**: `sign_for_group(group, b"")` valid — HMAC of empty bytes is a well-defined hex digest.
- **Auto-create race**: Two requests with empty body on a Group with no secret yet → both might mint independently; last save wins. Acceptable (cost is one extra random_string call). No transaction needed for v1.
- **Secret never appears in any other graph**: Confirm no `mojo_secrets` field is exposed in `Group.RestMeta.GRAPHS` (it isn't — verified). The webhook secret is reachable only through the new endpoint.
- **Sign-group missing at delivery**: If a Group is deleted between `publish_webhook` and `post_webhook` execution, handler returns `'failed'` (no retry — won't recover). Logged with `error_type='sign_group_missing'`. The job is not silently delivered unsigned.
- **Sign-group missing secret**: Group exists but has no `webhook_secret` set yet (no one called the REST endpoint). Handler calls `sign_for_group` which auto-creates (matches publish-endpoint behavior — the act of signing IS the first use). Receiver must fetch the secret out-of-band on first delivery; same as today.
- **Caller pre-set `X-Mojo-Signature` header**: Handler overwrites it. Documented as expected behavior — `group=` is authoritative.
- **Non-dict `data`**: `publish_webhook` already validates `json.dumps(data)`. Signing path requires `data` to be JSON-serializable to a dict-or-list — same constraint, no new failure mode.
- **Secret never enters the queue payload**: Verified by storing only `sign_group_id` (int). Anyone reading the queue store directly sees no signing material.
- **Signature/wire-bytes consistency**: Handler computes `body_bytes` once and uses the same bytes for both HMAC input and the `requests.post(data=body_bytes, ...)` body. No risk of `requests` re-serializing differently than the signer.

### Testing

`tests/test_account/test_webhook_secret.py`:
- `test_first_call_auto_creates` — empty-body POST returns 200, `data.secret` starts with `wsec_`, length 53, `created_at == last_rotated_at`. → `tests/test_account/test_webhook_secret.py`
- `test_subsequent_call_returns_same` — second empty-body POST returns identical secret + timestamps. → same file
- `test_rotate_returns_new_secret` — `{rotate: true}` returns secret distinct from prior value; `created_at` preserved; `last_rotated_at` advanced. Next empty-body call returns the rotated value. → same file
- `test_two_groups_independent` — Group A and Group B produce distinct secrets, neither leaks to the other. → same file
- `test_missing_permission_returns_403` — user without `manage_group` on the target Group hits the endpoint → 403. → same file
- `test_api_key_auth_path` — caller authed via `Authorization: apikey <token>` (key with `manage_group`) succeeds without a `group` body field. → same file

`tests/test_account/test_webhook_signer.py`:
- `test_sign_deterministic` — `sign_for_group(group, body)` returns the same hex for the same `(group, body)`. → `tests/test_account/test_webhook_signer.py`
- `test_verify_accepts_valid` — sign then verify with the same group + body returns `True`. → same file
- `test_verify_rejects_tampered_body` — sign body A, verify body A+`b"x"` returns `False`. → same file
- `test_verify_rejects_missing_secret` — verify against a Group with no secret minted returns `False` (no auto-create on verify path). → same file
- `test_verify_rejects_empty_header` — `verify_for_group(group, body, None)` and `""` both return `False`. → same file
- `test_verify_uses_constant_time_compare` — smoke check: `webhook_signer.verify_for_group` source imports `hmac.compare_digest` (via `crypto.sign.verify_signature`). Optional — main correctness is covered by the helper tests above.

`tests/test_jobs/test_signed_webhook.py`:
- `test_publish_stores_group_id_not_secret` — call `publish_webhook(url, data, group=g)`; fetch the published `Job`; assert `payload['sign_group_id'] == g.id` and `"secret" not in str(payload)` (no secret material in queue). → `tests/test_jobs/test_signed_webhook.py`
- `test_handler_injects_signature_header` — build a Job with `sign_group_id` set; patch `requests.post`; call `post_webhook(job)`; assert the captured `headers` includes `X-Mojo-Signature` matching `webhook_signer.sign_for_group(g, body_bytes)` and `kwargs['data'] == body_bytes` (not `kwargs['json']`). → same file
- `test_handler_rotates_signature_after_secret_rotates` — sign once, rotate group secret, re-run handler on a fresh attempt; new signature differs from prior. → same file
- `test_handler_missing_group_fails_no_retry` — `sign_group_id` references a deleted Group; handler returns `'failed'`, sets `error_type='sign_group_missing'`, does **not** raise. → same file
- `test_unsigned_path_unchanged` — `publish_webhook` without `group=` produces a payload with no `sign_group_id` and handler uses `json=data` path (regression guard). → same file

Run with `bin/run_tests --agent -t test_account.test_webhook_secret`, `... -t test_account.test_webhook_signer`, and `... -t test_jobs.test_signed_webhook`. No `bin/create_testproject` needed (no schema change).

### Docs

- `docs/django_developer/account/webhook_signing.md` — new. Sections: Overview, How it works, `Group.get_webhook_secret()` / `rotate_webhook_secret()` API, `webhook_signer.sign_for_group` / `verify_for_group`, `SIGNATURE_HEADER` constant, **"Recommended path: `jobs.publish_webhook(group=...)`"** with the one-line emitter call + canonical-body note for receivers, Security notes (constant-time compare, rotation semantics, no overlap window, secret never in queue).
- `docs/django_developer/account/README.md` — add line: `- [Webhook Signing](webhook_signing.md) — Group-scoped HMAC primitive for outbound webhooks`.
- `docs/django_developer/jobs/publishing.md` — add "Signed webhooks" subsection: `publish_webhook(url, data, group=org_group)` auto-signs at delivery time; receiver must hash the raw wire body.
- `docs/web_developer/account/webhook_signing.md` — new. REST endpoint contract (`POST /api/group/webhook_secret`), auth (API key with `manage_group` or session user with `manage_group`/`manage_groups`), request bodies (`{}` vs `{"rotate": true}`), response envelope, header receivers should check (`X-Mojo-Signature`), canonical-body rule (hash raw bytes from the wire).
- `docs/web_developer/account/README.md` — add link.
- `mkdocs.yml` — two new nav entries (django + web developer Account sections).
- `CHANGELOG.md` — entry noting new endpoint + helper + `publish_webhook(group=...)` auto-signing.
