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

## 7. Client Error + Event Reporting (Incidents)

There are two routes, for two different jobs:

| Route | For | Sign-in |
|---|---|---|
| `POST /api/incident/event/client` | crash and debug reports from the browser | not needed |
| `POST /api/incident/event` | security events a signed-in client reports | required |

Both put the report through incident triage (the RuleSet pipeline) once. In
both, the server records who really sent it and from where. A browser can
never set another person, another address or a trusted severity.

### Crash and debug reports: `POST /api/incident/event/client`

Works before and after sign-in. The receipt is a 2xx JSON `{"status": true}`
with **no** `data` and no event id; a repeated report gets the same receipt.
A refused report gets `400` (bad body), `413` (body over 64 KB) or `429`
(rate limit: 60 per minute per IP, 20 per minute per cookie).

- `category` must start with `frontend:` and use only `a-z 0-9 _ . : -`
  (at most 64 characters).
- `severity` (`debug`, `info`, `warning`, `error`, `fatal`) or an integer
  `level`. The server caps it below the incident threshold (at most 6), so a
  crash report never opens an incident on its own severity.
- `scope` is always `web`.
- `message` (at most 2000 characters).
- `metadata` keeps only: `stack_trace` (16 KB), `component_stack` (8 KB),
  `app`, `environment`, `build` (64 each), `reference` (128, your
  correlation key), `page_path`, `filename` (512, query and fragment
  stripped), `lineno`, `colno` (integers). Everything else is dropped.

Cross-origin calls must not rely on cookies: send `mode: "cors"` without
`credentials`, and pass a Bearer token if the user is signed in. Never put an
API key in a browser.

```javascript
const API_ORIGIN = "https://api.example.com"; // your django-mojo server
const GROUP_ID = null; // optional: the brand/group this app runs under

let reporting = false; // re-entrancy guard: a reporter error never reports itself

async function reportCrash({ severity = "error", message, stack, componentStack, extra = {} }) {
  if (reporting) return;
  reporting = true;
  try {
    const url = new URL("/api/incident/event/client", API_ORIGIN);
    if (GROUP_ID) url.searchParams.set("group", GROUP_ID);
    const headers = { "Content-Type": "application/json" };
    const token = localStorage.getItem("access_token");
    if (token) headers.Authorization = `Bearer ${token}`;
    const resp = await fetch(url, {
      method: "POST",
      mode: "cors",
      headers,
      body: JSON.stringify({
        category: "frontend:error",
        severity,
        message: String(message || "Unknown error").slice(0, 2000),
        metadata: {
          stack_trace: stack,
          component_stack: componentStack,
          app: "my-app",
          environment: "production",
          build: "2026.11.06",
          reference: crypto.randomUUID(),
          page_path: location.pathname,
          ...extra,
        },
      }),
    });
    // 429: drop it, never retry in a loop.
    return resp.ok;
  } catch (e) {
    return false;
  } finally {
    reporting = false;
  }
}

window.addEventListener("error", (event) => {
  reportCrash({
    message: event.message,
    stack: event.error && event.error.stack,
    extra: { filename: event.filename, lineno: event.lineno, colno: event.colno },
  });
});

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason || {};
  reportCrash({
    message: reason.message || String(reason),
    stack: reason.stack,
  });
});

// React error boundary
class ErrorBoundary extends React.Component {
  componentDidCatch(error, info) {
    reportCrash({
      severity: "fatal",
      message: error.message,
      stack: error.stack,
      componentStack: info && info.componentStack,
    });
  }
  render() { return this.props.children; }
}
```

### Security events from a signed-in client: `POST /api/incident/event`

Requires a signed-in user, API key or group token (anonymous: `401`). The
receipt is `{"status": true, "data": {"id": <event id>}}`. The server sets
`uid`, `source_ip`, the group and the request details; anything you send for
another user, address, group or incident is kept as `claimed_*` metadata for
the rules to judge. `level` is kept, but as your claimed severity.

```javascript
await fetch(new URL("/api/incident/event", API_ORIGIN), {
  method: "POST",
  mode: "cors",
  headers: {
    "Content-Type": "application/json",
    Authorization: `Bearer ${localStorage.getItem("access_token")}`,
  },
  body: JSON.stringify({
    category: "auth:token_refresh_failed",
    level: 5,
    scope: "account",
    title: "Token refresh failed",
    details: "Client refresh attempt returned unauthorized",
    metadata: { page_path: location.pathname },
  }),
});
```

### Guidelines

- Use stable category names (`frontend:error`, `auth:token_refresh_failed`, etc.).
- Send `error.stack` and React's `componentStack`; they are what triage needs.
- Never put passwords, tokens or other secrets in messages, stacks or paths.
- To be paged on crash reports, an operator writes a RuleSet for scope `web`
  or a `frontend:*` category (see the backend incidents guide).

## Related Docs

- [Authentication](account/authentication.md)
- [Core Authentication](core/authentication.md)
- [Request/Response Format](core/request_response.md)
- [Reporting Events](logging/reporting_events.md)
- [Incident API](logging/incidents.md)
