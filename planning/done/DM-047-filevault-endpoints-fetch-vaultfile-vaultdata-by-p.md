---
# id is assigned by /scope on pickup — leave it blank
id: DM-047
type: bug
title: filevault endpoints fetch VaultFile/VaultData by pk with no group/owner scoping — cross-tenant token mint, plaintext read, password oracle (auth-only)
priority: P1
effort: M
owner: backend
opened: 2026-07-16
depends_on: []
related: [DM-039, DM-025, DM-019]
links: []
---

# filevault endpoints fetch VaultFile/VaultData by pk with no group/owner scoping (auth-only cross-tenant access)

## What & Why
Three `filevault` REST handlers resolve a resource by client-supplied pk with a
bare `Model.objects.filter(pk=pk).first()` and no group/owner scoping, gated only
by `@md.requires_auth()`. Any authenticated user can pass any pk and reach another
tenant's resource — the same unfiltered-fetch class DM-039 (group member endpoint)
and DM-019 (cross-tenant apikey) addressed, but here it yields data/tokens, not
just an existence signal. Surfaced by DM-039's post-build security review
(2026-07-16); **unverified — /scope must confirm exact reachability, decorators,
and whether any upstream check scopes these before treating as confirmed.**

Reported sites (verify line numbers during scope):
- **`mojo/apps/filevault/rest/file.py:47-70` `on_vault_file_unlock` (CRITICAL as reported)**
  — `VaultFile.objects.filter(pk=pk).first()`, no group/owner check; mints a signed
  download token (`vault_service.generate_download_token`) for any file pk AND writes
  `unlocked_by` / `save()`. Cross-tenant exfiltration token + write side effect.
- **`mojo/apps/filevault/rest/data.py:44-56` `on_vault_data_retrieve` (CRITICAL as reported)**
  — same unfiltered fetch; returns the **decrypted plaintext** of any group's stored
  `VaultData`. Most severe: direct cross-tenant data exposure.
- **`mojo/apps/filevault/rest/file.py:73-88` `on_vault_file_password` (WARNING as reported)**
  — same unfiltered fetch; returns `valid=True/False` for a supplied password against
  any file's `hashed_password`. Cross-tenant password oracle.

## Acceptance Criteria
- [ ] Confirm each site's actual gating (decorators, any pre-check) and reproduce the
      cross-tenant access as an authenticated non-owner, OR document why a site is
      already safe and drop it from scope.
- [ ] Each remaining site scopes the fetch to the caller's group/ownership (align with
      the framework's model-security / `request.group` / owner conventions — see how
      `uses_model_security` and DM-019/DM-032 handle per-instance tenant re-verification).
- [ ] No cross-tenant token mint, plaintext read, password check, or write reachable
      by a bare authenticated user.
- [ ] Regression tests: an authenticated non-owner is denied at each site; the
      legitimate owner path is unchanged.

## Repro — bugs only
1. As authenticated user A (no relationship to tenant B), POST/GET each endpoint with
   a pk belonging to tenant B's VaultFile / VaultData.
- Expected: denied (403 / not-found), no token, no plaintext, no write.
- Actual (as reported, unverified): token minted + `unlocked_by` written
  (unlock); decrypted plaintext returned (data retrieve); password validity leaked
  (password).

## Plan

### Goal
Close the auth-only cross-tenant IDOR in the three filevault action endpoints
(`unlock`, `retrieve`, `password`) by scoping each per-pk fetch to the caller's
tenant via the framework's model-security check, and harden the (not-yet-shipped)
sharing/token model — bounded token TTL, password-required-at-unlock, corrected
docs — **without** converting the endpoints to the `on_action_*` pattern.

### Context — what exists
All three vulnerable handlers fetch by client pk with a bare
`Model.objects.filter(pk=pk).first()`, gated only by `@md.requires_auth()` (which
just asserts `request.user.is_authenticated` — no tenant/owner/permission check).
They bypass the model-security layer their co-located routers use.

**`mojo/apps/filevault/rest/file.py:47-70` `on_vault_file_unlock`** (verbatim):
```python
@md.POST('file/<int:pk>/unlock')
@md.requires_auth()
def on_vault_file_unlock(request, pk=None):
    """Generate a signed, IP-bound download token."""
    vault_file = VaultFile.objects.filter(pk=pk).first()
    if not vault_file:
        raise me.ValueException("File not found", code=404)
    ttl = request.DATA.get("ttl", None)
    if ttl:
        ttl = int(ttl)                         # UNBOUNDED — ttl=99999999 mints a multi-year token
    client_ip = get_remote_ip(request)
    token = vault_service.generate_download_token(vault_file, client_ip, ttl=ttl)
    download_url = f"/api/filevault/file/download/{token}"
    vault_file.unlocked_by = request.user      # cross-tenant write
    vault_file.save()
    return dict(token=token, download_url=download_url, ttl=ttl or 300)
```

**`mojo/apps/filevault/rest/file.py:73-89` `on_vault_file_password`** — same bare
fetch (line 80); returns `dict(valid=...)` from
`crypto_vault.verify_password(password, vault_file.hashed_password)` for any pk.

**`mojo/apps/filevault/rest/data.py:44-58` `on_vault_data_retrieve`** — same bare
fetch (line 48); returns `dict(data=vault_service.retrieve_data(vault_data, password=...))`
— decrypted plaintext for any pk.

**Safe siblings (the pattern to match):** `on_vault_file` (`file.py:9-12`) and
`on_vault_data` (`data.py:7-10`) route through `Model.on_rest_request(request, pk)`,
which calls `get_instance_or_404` then `rest_check_permission_or_raise` against
VIEW/SAVE_PERMS. These three custom endpoints skip that path entirely.

**Framework tools for the fix** (`mojo/models/rest.py`):
- `Model.get_instance_or_404(pk)` (~164-190) — framework 404 + `ALT_PK_FIELD` handling.
- `Model.rest_check_permission_or_raise(request, perms, instance)` (~403-433) — runs
  `_evaluate_permission`: owner-id match, OR rebind `request.group = instance.group`
  and require `instance.group.user_has_permission(request.user, perms)`. Raises
  `PermissionDeniedException` (403) otherwise. `perms` accepts a string
  (`"VIEW_PERMS"`) or list. This is the exact tenant re-verification the safe GET
  path uses (rebind at rest.py ~329-338; membership at ~340-373).
- `@md.uses_model_security(Model)` (`mojo/decorators/auth.py`) — metadata-only marker
  (`rest.md` treats its absence on model endpoints as a defect); the actual gate is
  the explicit `rest_check_permission_or_raise` call.

**Models** (`mojo/apps/filevault/models/`): `VaultFile` (`file.py`) and `VaultData`
(`data.py`) each have a direct **`group` FK** (non-null) + **`user` FK** (nullable,
default `OWNER_FIELD="user"`), plus `hashed_password` (nullable). Both:
```python
VIEW_PERMS = ["view_vault", "manage_vault", "files", "owner"]
SAVE_PERMS = ["manage_vault", "files", "owner"]
```
So `VIEW_PERMS` admits `view_vault`/`manage_vault`/`files`/`owner` (read tier);
`view_vault` is absent from `SAVE_PERMS` (write tier). `VaultFile.unlocked_by` is a
nullable FK audit stamp.

**Token / sharing chain** (`mojo/helpers/crypto/vault.py`, `services/vault.py`):
- `VAULT_TOKEN_TTL = 300` (vault.py:32). `generate_download_token(vault_file,
  client_ip, ttl=None)` (services/vault.py:199-204) → `crypto_vault.generate_access_token`
  which HMAC-signs `{fid, ip, exp, iat}` (vault.py:213-233). `validate_access_token`
  does a **hard IP equality** check (`payload["ip"] != client_ip → None`, vault.py:256).
- `on_vault_file_download` (`file.py:92-116`, `@md.public_endpoint`) redeems the token —
  stays public/token-based (cannot become an action; no pk/user/JSON). After
  `validate_download_token`, there is **no** group/owner re-check — a valid token alone
  yields bytes.
- **Password is part of the decryption KDF at download:**
  `passphrase = (password + ekey) if password else ekey` (`download_file_streaming`,
  services/vault.py ~168; password verified first at ~150-154 when `hashed_password`).
  ⇒ a token minted without the password already cannot decrypt a protected file.

**Chat leads: NOT bugs.** `chat/rest/rooms.py` and `messages.py` verify
`ChatMembership`/room-admin **before** returning data or acting (rooms.py:256-259,
messages.py:22-28). Confirmed safe; drop from scope.

### Changes — what to do
1. **`mojo/apps/filevault/rest/file.py` — `on_vault_file_unlock`:**
   - Add `@md.uses_model_security(VaultFile)`.
   - Replace the bare fetch + manual 404 with `vault_file = VaultFile.get_instance_or_404(pk)`.
   - Immediately after: `VaultFile.rest_check_permission_or_raise(request, "VIEW_PERMS", vault_file)`
     — **before** any token mint or write.
   - **Password-at-unlock:** if `vault_file.hashed_password`, require a `password`
     param and verify via `crypto_vault.verify_password(...)`; missing/wrong →
     generic `PermissionDeniedException`/403 (before minting/writing). Non-protected
     files unchanged.
   - Keep the `unlocked_by` write, but only after all checks pass.
   Target shape:
   ```python
   @md.POST('file/<int:pk>/unlock')
   @md.uses_model_security(VaultFile)
   def on_vault_file_unlock(request, pk=None):
       vault_file = VaultFile.get_instance_or_404(pk)
       VaultFile.rest_check_permission_or_raise(request, "VIEW_PERMS", vault_file)
       if vault_file.hashed_password:
           pw = request.DATA.get("password", None)
           if not pw or not crypto_vault.verify_password(pw, vault_file.hashed_password):
               raise me.PermissionDeniedException("Invalid password", code=403)
       ttl = request.DATA.get("ttl", None)
       ttl = int(ttl) if ttl else None
       client_ip = get_remote_ip(request)
       token = vault_service.generate_download_token(vault_file, client_ip, ttl=ttl)
       vault_file.unlocked_by = request.user
       vault_file.save()
       return dict(token=token,
                   download_url=f"/api/filevault/file/download/{token}",
                   ttl=ttl or VAULT_TOKEN_TTL)
   ```
   (Confirm the exact `PermissionDeniedException`/`ValueException` import + code the
   file already uses; match it. `crypto_vault` is already imported locally in
   `on_vault_file_password` — reuse or hoist.)
2. **`mojo/apps/filevault/rest/file.py` — `on_vault_file_password`:** add
   `@md.uses_model_security(VaultFile)`; swap fetch to `get_instance_or_404`; add
   `VaultFile.rest_check_permission_or_raise(request, "VIEW_PERMS", vault_file)` after
   fetch. Keep its `valid=True/False` return (now reachable only by a permitted viewer).
3. **`mojo/apps/filevault/rest/data.py` — `on_vault_data_retrieve`:** add
   `@md.uses_model_security(VaultData)`; swap fetch to `get_instance_or_404`; add
   `VaultData.rest_check_permission_or_raise(request, "VIEW_PERMS", vault_data)` after
   fetch. Keep decrypt+return.
4. **`mojo/apps/filevault/services/vault.py` — clamp TTL at the mint chokepoint.** In
   `generate_download_token`, clamp before minting:
   `ttl = min(ttl or VAULT_TOKEN_TTL, VAULT_TOKEN_MAX_TTL)` (and treat `ttl <= 0` as
   default). Every mint path is then bounded, not just the handler.
5. **`mojo/helpers/crypto/vault.py`** — add `VAULT_TOKEN_MAX_TTL` alongside
   `VAULT_TOKEN_TTL` (default **3600s / 1h**), overridable via settings the same way
   `VAULT_TOKEN_TTL` is sourced. (Ceiling value is adjustable — 1h is the proposed default.)
6. **`docs/django_developer/filevault/README.md`** — (a) document the new VIEW_PERMS
   scoping on unlock/retrieve/password; (b) document the TTL ceiling and
   password-required-at-unlock; (c) **correct** the "Sharing Download Access" section
   (~line 147) that implies an unauthenticated external recipient can download — the
   token is IP-bound to the unlocker, so it is effectively same-egress-IP / same-session,
   **not** cross-network external sharing.
7. **`CHANGELOG.md`** — security fix + behavior changes (VIEW_PERMS gating, TTL ceiling,
   password-at-unlock, doc correction).
8. **No `web_developer` filevault page exists** (verified) — nothing to update there;
   the doc change is django_developer + CHANGELOG.

### Design decisions
- **Reject the `on_action_*` refactor** (considered and rejected). The action-dispatch
  path (`on_rest_handle_save`, rest.py:523; dispatch loop 1441-1449) gates **every**
  action uniformly at `SAVE_PERMS` with **no per-action override** (no `ACTION_PERMS`
  map / decorator / hook exists anywhere). Converting `retrieve`/`password` (reads) to
  actions would force them to write-tier perms (`manage_vault`/`files`/`owner`),
  breaking a `view_vault`-only user, and would drop the dedicated URLs. The explicit
  `rest_check_permission_or_raise` call gets the **same** `request.group`→`instance.group`
  rebind + tenant check while preserving per-endpoint permission level — strictly better
  here.
- **`VIEW_PERMS` for all three, including `unlock`.** Unlock is semantically "I want to
  download a file I can see"; `unlocked_by` is an audit stamp, not user content. Read-tier
  matches `retrieve`/`password`. (The only write is the `unlocked_by` stamp; if you'd
  rather require write-tier for the token mint, bump `unlock` to
  `["SAVE_PERMS","VIEW_PERMS"]` — but note that resolves to `SAVE_PERMS` only.)
- **Clamp centralized in `generate_download_token`** (the filevault mint chokepoint),
  not the handler — any caller is bounded. Default ceiling 3600s, settings-overridable.
- **Password-at-unlock is defense-in-depth, not an exfil fix.** Because the password is
  in the download KDF, a token minted without it already can't decrypt. The value: view
  access alone should not let you mint a download capability for a password-protected
  file — you must prove password knowledge to initiate a share, matching the vault's
  intent. Lowest-priority of the build-now set; drop if undesired.
- **`get_instance_or_404` over `filter(pk).first()`** for framework-consistent 404 /
  `ALT_PK_FIELD` behavior.
- **Denials return framework-standard 403 (bad pk → 404),** same as every other
  model-security endpoint (`on_vault_file` GET already behaves this way). The residual
  existence signal from sequential pks is a layer-wide concern, not this item's; collapsing
  only these three to a uniform 404 would be inconsistent and pointless. Conscious choice,
  not an oversight.

### Edge cases & risks
- **Legit owner / group member** must still succeed at all three (test the happy path).
- **Non-password file unlock** unchanged (no password required).
- **`request.group` rebind:** `rest_check_permission_or_raise` mutates `request.group` to
  `instance.group`; safe here — single instance, no loop, and nothing downstream in these
  handlers relies on a caller-supplied group. (Contrast DM-032's per-row reset, which was
  needed only in a batch loop.)
- **TTL clamp** must preserve the documented 300s default: `min(ttl or default, max)`, and
  treat `ttl <= 0` as default (avoid instantly-expired tokens).
- **No new password oracle:** unlock's wrong-password → generic 403; it exposes nothing
  the (now VIEW_PERMS-gated) `password` endpoint doesn't already give a permitted viewer.

### Tests
testit, REST-level (none exist for these endpoints today). New file
`tests/test_filevault/3_test_rest_scoping.py`. Follow the cross-tenant HTTP pattern in
`tests/test_account/test_group_me_member_oracle.py` (`opts.client.login/logout`,
`opts.client.post/get`, assert on `resp.status_code` / `resp.response`) and the filevault
setup in `tests/test_filevault/2_test_service.py` (create users with
`user.add_permission("view_vault")`, `Group.get_or_create`, a filesystem `FileManager` at
`/tmp/...`, and rows via `vault_service.upload_file` / `store_data`). **Delete existing
test users/groups/rows before creating** (long-lived DB rule).
Setup: group A (owner user A, `view_vault`), group B (outsider user B, `view_vault`, NOT a
member of A); a `VaultFile` and a `VaultData` in group A; one **password-protected**
`VaultFile` in group A.
Cases (each `assert` carries a descriptive message):
1. Outsider B → `POST file/<A_file>/unlock` → 403; response carries no `token`; re-fetch
   file and assert `unlocked_by` unchanged (no write).
2. Outsider B → `POST data/<A_data>/retrieve` → 403; no `data` in response.
3. Outsider B → `POST file/<A_file>/password` → 403 (no `valid` leaked).
4. Owner A → all three → success (token minted; plaintext returned; `valid` returned).
5. TTL clamp: owner A unlock with `ttl=99999999` → decode returned token payload, assert
   `exp - iat <= VAULT_TOKEN_MAX_TTL`.
6. Password-at-unlock: owner A unlock the protected file with **no** password → 403; with
   **wrong** password → 403; with **correct** password → token minted.

### Docs
`docs/django_developer/filevault/README.md` (scoping, TTL ceiling, password-at-unlock, and
the corrected IP-bound sharing section) + `CHANGELOG.md`. No `web_developer` filevault page
exists.

### Deferred — wanted pre-launch hardening (separate follow-on, not built here)
Split out to keep this security fix small/shippable; each is a larger build. These are
**wanted before the feature ships**, not "someday" — track them (recommend a companion
`planning/inbox/` item when this is built, so they survive DM-047 closing):
- **Access audit trail** — a `VaultAccessLog` model (unlock/download/retrieve/password +
  denials, with user/IP/action/result/time) answering "who touched this secret." Needs a
  model + migration (`bin/create_testproject`). (Denials already raise via the standard
  permission path; this adds the *successful-access* trail a secrets vault should have.)
- **Token revocation** — e.g. an `access_version` int on `VaultFile` signed into the token,
  bumped to invalidate outstanding tokens. Adds state to a deliberately stateless token; the
  clamped short TTL bounds exposure in the meantime.
- **Real cross-network external sharing** — if genuine third-party sharing (recipient on a
  different IP) is a product requirement, it's its own design (recipient tokens / emailed
  links), distinct from the current IP-bound model.

### Open questions
None blocking. Calls made (flag to change): VIEW_PERMS for `unlock`; TTL ceiling 3600s
(settings-overridable); keep password-at-unlock; framework-standard 403/404 (no uniform-404
collapse); audit + revocation deferred to a tracked follow-on.

## Resolution
- closed: 2026-07-18
- branch: main
- files changed: CHANGELOG.md,docs/django_developer/filevault/README.md,docs/web_developer/account/user_self_management.md,docs/web_developer/filevault/README.md,mojo/apps/filevault/rest/data.py,mojo/apps/filevault/rest/file.py,mojo/helpers/crypto/vault.py,tests/test_filevault/3_test_rest_scoping.py
- tests added: tests/test_filevault/3_test_rest_scoping.py (8) — cross-tenant unlock/retrieve/password denied 403 (no token, no plaintext, no oracle, no unlocked_by write); owner happy-path for all three; TTL clamp to VAULT_TOKEN_MAX_TTL; password-required-at-unlock (none/wrong/correct).

## Notes
- **Build outcome (2026-07-18):** implemented as scoped, with two minor deviations
  (both flagged + confirmed): (1) kept `@md.requires_auth()` and did NOT add
  `@md.uses_model_security` — the latter is a metadata-only marker for the
  `on_rest_request` CRUD routers; these are custom action endpoints, so the explicit
  `rest_check_permission_or_raise` IS the gate; (2) `VAULT_TOKEN_MAX_TTL` shipped as a
  plain module constant (3600s) next to `VAULT_TOKEN_TTL`, not a Django setting
  (neither is settings-driven). Full suite green (2467 passed, 0 failed; +8 new).
  Post-build security-review: fix correct + complete, no bypass, no missed site
  (INFO only: unlock's password-fail `ValueException(code=403)` returns wire-status
  400 / body code 403 — matches the pre-existing filevault password-error convention;
  the regression tests assert body `code`). Docs synced both tracks; the
  `web_developer/filevault` page was stale (wrong perms — fixed). Pre-existing doc
  gaps flagged for a follow-on (NOT in scope): VaultData `retrieve` has no
  `web_developer` section; the django Settings table lists non-existent
  `FILEVAULT_DEFAULT_TTL`/`FILEVAULT_S3_BUCKET`.
- **Build baseline (2026-07-18, pre-edit, `bin/run_tests --agent`):** total 2515,
  passed 2459, failed **0**, skipped 56 (`var/test_failures.json`). All-green — every
  post-change failure is attributable to this build.
- Source: DM-039 post-build security-review (2026-07-16). Findings are one agent's
  static read — treat as high-priority leads to verify, not confirmed facts.
- Related lower-confidence mentions from the same review to sanity-check during scope:
  `mojo/apps/chat/rest/rooms.py` / `messages.py` fetch `ChatRoom`/`User` by pk before
  a membership/kind check (reviewer believed they gate before returning data — confirm).
  `mojo/apps/account/rest/oauth.py:394` was checked and is NOT a match (gated behind
  `manage_users`).
