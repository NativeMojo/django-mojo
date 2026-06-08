# Step-Up Authentication (handling HTTP 440)

Some sensitive operations may require that you have **authenticated recently**
(not just that you hold a valid token). When enabled by the deployment, calling a
sensitive endpoint with a token whose login is too old returns:

```
HTTP 440
{ "status": false, "error": "reauth_required", "code": 440 }
```

This is a **distinct third state**:

| Status / code | Meaning | What the client should do |
|---|---|---|
| `403` | You don't have permission | Show "not allowed"; don't retry |
| `401` (token invalid/expired) | Your token is bad/expired | Refresh the token, or send to login |
| **`440` `reauth_required`** | Token is fine, but your login isn't *recent* enough | **Step-up re-authenticate**, then retry |

**Do not treat 440 like 401.** Refreshing the token does **not** clear it — a
refreshed token keeps its original login time on purpose. Branch on
`code === 440` (or `error === "reauth_required"`) *before* any refresh logic.

## Handling 440 in a client

1. Detect `code === 440` / `error === "reauth_required"` on a response.
2. Run any login/verify flow the user has available, for the **same** signed-in
   user, to get fresh tokens — e.g.:
   - Passkey assertion (`/api/account/passkeys/...`)
   - SMS code: `POST /api/auth/sms/verify`
   - TOTP: `POST /api/account/totp/verify`
   - Password login: `POST /api/auth/login`
3. Replace the stored access/refresh tokens with the new pair.
4. Retry the original request.

There is no separate "step-up" endpoint — succeeding through any normal
login/verify flow refreshes your recent-auth window.

## Gated operations (when the feature is enabled)

Change username, change email, change phone, revoke sessions (log out
everywhere), enable/confirm/disable TOTP, regenerate recovery codes, register a
passkey, deactivate account — and the same operations performed by an admin on
another user (the admin's own login must be recent).

By default the deployment ships with this **disabled**, so you will not see 440
unless an operator turns it on.

## Related change: no more `current_password` on these endpoints

`POST /api/auth/username/change` and `POST /api/auth/sessions/revoke` **no longer
require `current_password`**. Ownership is proven by your authenticated session
(and, when enabled, by the recent-auth window above). This makes them usable by
passwordless accounts (passkey / SMS-OTP). Sending `current_password` is harmless
but ignored.
