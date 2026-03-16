# Email stuck in "sending" status with no status_reason

**Type**: bug
**Status**: Resolved — 2026-03-16
**Date**: 2026-03-16

## Description

An email (id=575) was accepted by SES (`ses_message_id` is set) but the status remains `"sending"` and `status_reason` is `null`. No delivery, bounce, or complaint event was recorded.

## Observed Data

- `status`: "sending"
- `status_reason`: null
- `ses_message_id`: set (SES accepted the message)
- `to_addresses`: ["carmen+m2@foraylabs.net"]
- `is_email_verified`: false for this user
- Template: `group_invite`

## Suspected Causes

1. SES SNS delivery notification not received / not processed — status never updated from "sending"
2. Email silently suppressed (SES suppression list, bounce, or complaint) — event fired but not handled
3. `+` addressing in `carmen+m2@foraylabs.net` causing silent SES rejection

## Resolution

Email was eventually delivered and status updated. SES delivery notification was delayed — not a code bug. SNS subscriptions were confirmed active, suppression list was clear, and `+` addressing was not the cause. No code changes required.
