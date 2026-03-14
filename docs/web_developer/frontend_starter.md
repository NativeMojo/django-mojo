# Frontend Starter — django-mojo REST

A practical baseline for frontend apps integrating with django-mojo JWT auth.

## 1. Login

`POST /api/login`

```json
{
  "username": "alice@example.com",
  "password": "mysecretpassword"
}
```

Save:

- `access_token`
- `refresh_token`

Suggested keys:

```text
mojo_access_token
mojo_refresh_token
```

## 2. Authenticated Requests

Send:

```text
Authorization: Bearer <access_token>
```

## 3. App Boot / Page Reload Check

On every load:

1. Read tokens from `localStorage`.
2. If no access token: logged out.
3. Call `GET /api/user/me`.
4. If success: session active.
5. If unauthorized and refresh token exists: call `POST /api/refresh_token`.
6. Save new tokens and retry `/api/user/me`.
7. If refresh fails: clear tokens and route to login.

## 4. Logout

Always clear both tokens:

```javascript
localStorage.removeItem("mojo_access_token");
localStorage.removeItem("mojo_refresh_token");
```

## 5. Minimal Client Pattern

```javascript
const ACCESS_KEY = "mojo_access_token";
const REFRESH_KEY = "mojo_refresh_token";

function getAccess() { return localStorage.getItem(ACCESS_KEY); }
function getRefresh() { return localStorage.getItem(REFRESH_KEY); }
function setTokens(access, refresh) {
  localStorage.setItem(ACCESS_KEY, access);
  localStorage.setItem(REFRESH_KEY, refresh);
}
function clearTokens() {
  localStorage.removeItem(ACCESS_KEY);
  localStorage.removeItem(REFRESH_KEY);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  const access = getAccess();
  if (access) headers.Authorization = `Bearer ${access}`;

  let res = await fetch(path, { ...opts, headers });
  if (res.status !== 401 && res.status !== 403) return res;

  const refresh = getRefresh();
  if (!refresh) return res;

  const refreshRes = await fetch("/api/refresh_token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!refreshRes.ok) {
    clearTokens();
    return res;
  }

  const payload = await refreshRes.json();
  setTokens(payload.data.access_token, payload.data.refresh_token);

  const retryHeaders = { ...(opts.headers || {}), Authorization: `Bearer ${payload.data.access_token}` };
  return fetch(path, { ...opts, headers: retryHeaders });
}
```

## 6. Security Notes

- `localStorage` is practical for this Bearer-token flow, but vulnerable to XSS.
- Use strong CSP, dependency hygiene, and output sanitization.
- Never store `mfa_token` long-term.
- For stricter security, use `HttpOnly` cookie-based refresh design.

## Related Docs

- [Authentication](account/authentication.md)
- [Core Authentication](core/authentication.md)
- [Request/Response Format](core/request_response.md)
