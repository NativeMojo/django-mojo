# Phone Number Change — REST API Reference

## Overview

Self-service phone number change is a two-step flow: **request → confirm.**

1. The authenticated user submits their desired new number and current password.
2. A 6-digit OTP is sent via SMS to the **new** number. The current number is not changed yet.
3. The user submits the OTP (along with the session token returned in step 1) to commit the change.

`current_password` is always required. A valid Bearer token alone is not sufficient — this prevents an attacker who has stolen a session token from silently redirecting SMS communications to an attacker-controlled number.

The feature is controlled by the `ALLOW_PHONE_CHANGE` setting (default `True`). When set to `False`, all requests to `POST /api/auth/phone/change/request` return 403.

> **Note — first-time number vs. change:**
> This flow is required only when **replacing** an existing phone number with a new one. If the account has no phone number yet, you may set one directly via `POST /api/user/me` with a `phone_number` field. Clearing a phone number (setting it to `null`) is similarly allowed via the profile endpoint. The change flow exists specifically to prove ownership of the incoming number before replacing a verified one.

---

## Step 1 — Request the Change

**POST** `/api/auth/phone/change/request`

Requires authentication (Bearer token). Rate limited.

**Request:**

```json
{
  "phone_number": "+14155550123",
  "current_password": "mysecretpassword"
}
```

Phone numbers are accepted in any common format (E.164, national with country code, etc.) and are normalized server-side. Use E.164 (`+` followed by country code and number) for maximum reliability.

**Error cases:**

| Condition | Status | Response |
|---|---|---|
| `current_password` missing | 400 | `"error": "current_password is required to change your phone number"` |
| `current_password` incorrect | 401 | `"error": "Incorrect password"` |
| `phone_number` has an invalid format | 400 | `"error": "Invalid phone number format"` |
| `phone_number` is the same as the current number | 400 | `"error": "New phone number must be different from current phone number"` |
| `phone_number` is already registered to another account | 400 | `"error": "Phone number already in use"` |
| SMS delivery to the new number failed | 400 | `"error": "Failed to send SMS to the new number — check the number and try again"` |
| Feature disabled via `ALLOW_PHONE_CHANGE` | 403 | `"error": "Phone number change is not allowed"` |

On success, a 6-digit OTP is sent via SMS to the **new** number. The current number and `is_phone_verified` flag are untouched until Step 2 is completed.

**Response:**

```json
{
  "status": true,
  "session_token": "pc:3a1b2c4d...",
  "message": "A verification code has been sent to your new phone number."
}
```

Store the `session_token` — it must be submitted alongside the OTP in the confirm step. It is an opaque signed token that identifies this change session; it does not contain the phone number.

---

## Step 2 — Confirm the Change

**POST** `/api/auth/phone/change/confirm`

Requires authentication (Bearer token). The user must be logged in with the same session that initiated the request. Rate limited.

**Request:**

```json
{
  "session_token": "pc:3a1b2c4d...",
  "code": "847291"
}
```

On success:

- The new phone number is committed to the account.
- `is_phone_verified` is set to `true`.
- The user's existing session remains valid — no JWT rotation occurs (unlike email change, which rotates the `auth_key` because email can be a login identifier).

**Response:**

```json
{
  "status": true,
  "message": "Phone number updated successfully."
}
```

The `session_token` and OTP are **single-use** and expire after 10 minutes by default (configurable via `PHONE_CHANGE_TOKEN_TTL`).

**Error responses:**

```json
{
  "status": false,
  "code": 400,
  "error": "Invalid code"
}
```

```json
{
  "status": false,
  "code": 400,
  "error": "Expired code"
}
```

If another account registered the target number in the window between request and confirm:

```json
{
  "status": false,
  "code": 400,
  "error": "Phone number is no longer available"
}
```

---

## Cancelling a Pending Change

**POST** `/api/auth/phone/change/cancel`

Requires authentication (Bearer token).

Immediately invalidates the outstanding session token and OTP — even before the 10-minute TTL expires. The account is unchanged.

**Response:**

```json
{
  "status": true,
  "message": "Pending phone number change has been cancelled."
}
```

This endpoint is **idempotent**: if there is no pending change, it still returns 200 with the same response body.

---

## Recommended UI Flow

1. Show the user a form with fields for `phone_number` (new number) and `current_password`.
2. Call `POST /api/auth/phone/change/request`. On success, store the `session_token` and display a prompt: *"A 6-digit code has been sent to +14155550123. Enter it below to confirm."*
3. Optionally show a **Cancel** button that calls `POST /api/auth/phone/change/cancel`.
4. When the user submits the code, call `POST /api/auth/phone/change/confirm` with the `session_token` and `code`.
5. On success, the user's phone number is updated. No re-login is required. Update any locally cached profile data.

If the user does not receive the SMS or enters the wrong code, allow them to restart from Step 1. Calling `/request` again generates a fresh session token and OTP, automatically invalidating the previous one.

---

## Verification State After Change

After a successful confirm, `is_phone_verified` is `true` for the new number. You can confirm this via the profile endpoint:

**GET** `/api/user/me`

```json
{
  "status": true,
  "data": {
    "id": 42,
    "phone_number": "+14155550123",
    "is_phone_verified": true
  }
}
```

---

## How This Differs from Email Change

| | Email change | Phone number change |
|---|---|---|
| Confirmation method | Link **or** 6-digit OTP sent to new address (your choice via `method` param) | 6-digit OTP sent to new number |
| Session token | Link: embedded in URL as `ec:` token. Code: no session token — user is authenticated | Returned in the `/request` response as `session_token` |
| Token / code TTL | Link: 1 hour (configurable). Code: 10 minutes (configurable) | 10 minutes (configurable) |
| Auth required on confirm | Link path: no — token is the credential. Code path: yes — Bearer token required | Yes — Bearer token required |
| Sessions invalidated on confirm | Yes — `auth_key` is rotated (both paths) | No — phone is not a JWT signing input |
| New JWT issued on confirm | Yes (both paths) | No — existing session continues |
| Setting the field for the first time | Must use change flow | Direct profile update allowed |

---

## Security Notes

- **`current_password` is always required.** A stolen JWT alone is not enough to redirect SMS messages to an attacker-controlled number.
- **The OTP is sent only to the new number.** The old number receives no notification. If you want to alert users of changes to their account, send a notification to the old number or email address in your application layer.
- **The session token proves identity; the OTP proves number ownership.** Both must be correct for the change to commit.
- **The `session_token` (pc:) is single-use and bound to the user's `auth_key`.** It cannot be replayed, transferred to another user, or used after it has been consumed or cancelled.
- **Availability is re-checked at confirm time.** Another account may have registered the target number in the 10-minute window. The confirm step will reject the request if this has occurred.
- **Direct replacement of an existing phone number via `POST /api/user/me` is blocked.** The REST layer enforces use of this change flow whenever an existing verified number is being replaced, ensuring the new number is always OTP-verified before it is committed. Clearing a phone number (setting it to `null`) and setting one for the first time are not restricted.
- **`is_phone_verified` is always reset** if the phone number is changed by any path. It is only set back to `true` after the OTP confirm step succeeds.

---

## Settings Reference

| Setting | Default | Description |
|---|---|---|
| `ALLOW_PHONE_CHANGE` | `True` | Set to `False` to disable self-service phone number change entirely. The request endpoint returns 403 when disabled. |
| `PHONE_CHANGE_TOKEN_TTL` | `600` (10 min) | Expiry time for phone change session tokens and OTP codes, in seconds |