# Rejected: Invite Token Channel Encoding (email vs SMS)

## Request

Encode the delivery channel (`email` or `sms`) and inviter identity (`invited_by_id`)
into the invite token payload, so `on_invite_accept` can set the correct verified flag
(`is_email_verified` vs `is_phone_verified`) and record attribution.

## Decision: Deferred / Not Now

## Reason

The current invite flow is email-only. There is no SMS invite path in the framework.
Adding channel encoding now would be speculative complexity with no active use case to
validate against.

The token system (`_generate` / `_verify` in `mojo/apps/account/utils/tokens.py`)
encodes a fixed payload of `{uid, ts, jti, kind}`. Extending it to carry arbitrary
extra claims is a reasonable future change, but it should be done when an SMS invite
path actually exists and the channel distinction has a concrete effect on behaviour.

### What would be needed when this is revisited

1. Extend `_generate(user, kind, extra=None)` to merge `extra` into the payload dict.
2. Extend `_verify` to return `(user, claims)` instead of just `user` (breaking change
   for existing callers — `on_invite_accept`, `on_user_password_reset_token`, etc.).
3. `generate_invite_token(user, channel="email", invited_by_id=None)` passes extra claims.
4. `on_invite_accept` unpacks claims; sets `is_phone_verified` when `channel=="sms"`;
   sets `is_email_verified` when `channel=="email"`.
5. `group.invite()` accepts optional `invited_by` user + `channel`, writes
   `metadata["protected"]` at user-creation time.

### Attribution via metadata.protected

`invited_by_id` and `invited_to_group_id` in `user.metadata["protected"]` can be set
at user-creation time inside `group.invite()` without token changes — the group and
inviter are known at that moment. Token channel encoding is only needed for the
verified-flag routing, not for attribution.

## See Also

- `planning/requests/oauth_registration_gate_and_new_user_flag.md` — registration
  source tracking via `metadata.protected`
- `mojo/apps/account/utils/tokens.py` — token generation/verification internals
