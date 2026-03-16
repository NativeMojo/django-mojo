# Member.send_invite() uses misleading email template name

**Type**: bug
**Status**: Resolved — 2026-03-16

## Description

`Member.send_invite()` sends a `group_invite` email with no token or login link in the context, but the template copy said "You're invited to join" — implying an invite flow with a call-to-action link.

## Root Cause

Seed template copy was written as if it were an account-setup invite. The member already has an account; this is a group access notification.

## Resolution

- No code changed.
- Updated seed template `mojo/apps/aws/seeds/email_templates/group_invite.json`:
  - Subject: "You're invited to join…" → "You've been added to…"
  - Body: "You've been invited to join" → "You now have access to… Log in to your account to get started."
  - Title: "Group invitation" → "You've been added to a group"
  - `metadata.description` and `notes` updated to explicitly state: no token, existing user, not an account-setup invite.

## Files Changed

- `mojo/apps/aws/seeds/email_templates/group_invite.json`

## Follow-up

Downstream projects that have already seeded `group_invite` should re-seed or manually update their SES template to match the new copy.
