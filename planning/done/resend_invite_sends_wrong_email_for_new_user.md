# resend_invite sends group notification instead of account-setup invite for new users

**Type**: bug
**Status**: Resolved — 2026-03-16
**Date**: 2026-03-16

## Description

`Member.on_action_resend_invite` calls `send_invite()`, which always sends the `group_invite` template (a plain group-access notification with no token or link). When the member user has never logged in, this is wrong — they need an account-setup invite with an `iv:` token link so they can set their password and access the group.

## Observed

```
POST /api/member/<pk>?action=resend_invite

→ user receives "You've been added to <group>" email with no login link
  (correct for existing users, wrong for users who have never logged in)
```

## Root Cause

`Member.send_invite` always sends `group_invite` regardless of whether the user has ever logged in. No check on `user.last_login` before choosing the email path.

## Acceptance Criteria

- When `member.user.last_login is None`: send `user.send_invite(group=self.group)` — `invite` template with `iv:` token link.
- When `member.user.last_login` is set: send `group_invite` notification as before.
- `on_action_resend_invite` behaviour is unchanged (delegates to `send_invite`).

## Resolution

`Member.send_invite` now checks `user.last_login` before sending:
- `None` → `user.send_invite(group=self.group)` — `invite` template with `iv:` token link
- Set → `group_invite` notification as before

## Files Changed

- `mojo/apps/account/models/member.py` — branching logic in `send_invite`
- `tests/test_accounts/member_invite.py` — regression tests (all passing)
