---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: filevault endpoints fetch VaultFile/VaultData by pk with no group/owner scoping — cross-tenant token mint, plaintext read, password oracle (auth-only)
priority: P1
effort:
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
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Source: DM-039 post-build security-review (2026-07-16). Findings are one agent's
  static read — treat as high-priority leads to verify, not confirmed facts.
- Related lower-confidence mentions from the same review to sanity-check during scope:
  `mojo/apps/chat/rest/rooms.py` / `messages.py` fetch `ChatRoom`/`User` by pk before
  a membership/kind check (reviewer believed they gate before returning data — confirm).
  `mojo/apps/account/rest/oauth.py:394` was checked and is NOT a match (gated behind
  `manage_users`).
