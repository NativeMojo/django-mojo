---
# id is assigned by /scope on pickup — leave it blank
id:
type: feature
title: filevault sharing/token model hardening — access audit trail, token revocation, external-sharing decision
priority: P2
effort:
owner: backend
opened: 2026-07-18
depends_on: []
related: [DM-047]
links: []
---

# filevault sharing/token model hardening

## What & Why
Deferred, but **wanted before the filevault feature ships** (it is not yet in
production — "no usage" reflects not-launched, not not-wanted). DM-047 closed the
acute cross-tenant IDOR on the three action endpoints and shipped cheap hardening
(VIEW_PERMS scoping, TTL clamp to `VAULT_TOKEN_MAX_TTL`, password-at-unlock, doc
corrections). It deliberately **left three larger sharing/token-model weaknesses**
for this follow-on because each is a bigger build than the security patch:

1. **No access audit trail.** For a secrets vault there is no record of who
   unlocked / downloaded / retrieved / password-checked what, when, or from where —
   `unlocked_by` only stores the *last* unlocker (overwritten each time). Denials
   already emit a permission-denied incident (free, via the standard raise path),
   but successful access has no trail. Proposed: a lightweight append-only
   `VaultAccessLog` model (VaultFile/VaultData FK, user, ip, action, result,
   created) — needs a model + migration (`bin/create_testproject`).
2. **No token revocation.** Download tokens are stateless HMAC blobs, replayable
   from the bound IP until expiry; a leaked/mis-issued token cannot be killed. The
   clamped short TTL bounds exposure, but there is no revoke. Proposed (if wanted):
   an `access_version` int on `VaultFile`, signed into the token and bumped to
   invalidate outstanding tokens.
3. **External-sharing coherence.** The download token is IP-bound to the
   *generating* caller's IP, so a recipient on a different network is rejected —
   it is a same-network/same-session convenience, NOT the "share a link with an
   external party" the old docs implied (docs corrected in DM-047). Decide the
   product intent: keep IP-bound-only (done — just confirm), or design real
   cross-network sharing (recipient-scoped links / emailed links — a feature of
   its own).

## Acceptance Criteria
- [ ] Decision recorded for each of the three (build vs defer-again vs drop), with
      rationale — especially the external-sharing product intent (#3).
- [ ] If audit (#1) is in: a queryable "who accessed this secret" trail across
      unlock/download/retrieve/password + denials, with a migration and tests;
      no secret material (plaintext, ekey, password) is ever logged.
- [ ] If revocation (#2) is in: an outstanding token can be invalidated; existing
      valid tokens for unchanged files still work; regression tests.
- [ ] No regression to the DM-047 tenant scoping or the public download endpoint.

## Repro — bugs only
N/A (feature/hardening — not a bug).

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Full context (token model facts, code refs, security-model analysis) is in the
  DM-047 done item: `planning/done/DM-047-filevault-endpoints-fetch-vaultfile-vaultdata-by-p.md`
  (see its `## Plan` → "Deferred" subsection and Design decisions).
- Scope may split this into separate items per piece if that's cleaner.
