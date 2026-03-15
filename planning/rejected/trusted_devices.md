# Rejected: Trusted Devices ("Remember This Device" for MFA)

## Request

> POST /api/account/devices/trust — "remember this device" for MFA. Skip TOTP on trusted devices for 30 days.

## Decision: Rejected

## Reason

The current JWT architecture uses a single `auth_key` per user as the signing
secret. All active JWTs for a user are validated against that one key — there
is no per-device identity embedded in the token that the MFA challenge path
can interrogate.

True trusted-device MFA skipping requires one of:

1. **Per-device signing keys** — each device gets its own `auth_key` or
   supplemental key embedded in its JWT. The `jwt_login` and MFA challenge
   paths would need to read device trust state before deciding whether to
   issue a challenge. This is a meaningful change to the core auth
   architecture.

2. **A Redis-backed device trust store** checked at MFA challenge time —
   simpler, but still requires touching `get_mfa_methods()` in `user.py`
   and introducing a new trust-check lookup on every MFA-gated login.

Either path adds complexity to the most security-sensitive code path in the
framework (login + MFA). The risk-to-reward ratio is poor at this stage:

- The current MFA UX (enter a code on each login) is not unusually
  burdensome.
- Trusted-device logic is highly product-specific — what counts as
  "this device", how long trust lasts, whether trust survives a password
  change, etc. are decisions better made at the project level.
- If implemented incorrectly (e.g. trust stored in a cookie the user
  controls), it degrades MFA security significantly.

## Revisit Conditions

Reconsider if:

- Per-device JWT keys are introduced for another reason (e.g. granular
  session revocation).
- A downstream project demonstrates a strong, concrete UX need and is
  willing to co-design the trust model.

## Related

- `mojo/apps/account/rest/user.py` — `get_mfa_methods()`, `jwt_login()`
- `planning/requests/session_revoke.md` — per-device session work that
  would be a prerequisite