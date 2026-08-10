# Platform and Advanced APIs

These built-in Admin routes are global operator surfaces, not substitutes for
tenant-scoped DNS, WebApp, or model REST APIs.

| Method | Route | Authority |
|---|---|---|
| `GET` | `/api/account/admin/platform` | Platform or Platform-security view/manage grant, or `admin` |
| `GET` | `/api/account/admin/advanced` | Any Advanced section view/manage grant, or `admin` |
| `POST` | `/api/account/admin/platform/deploy/retry` | fresh interactive `manage_platform`/`admin` |
| `POST` | `/api/account/admin/platform/deploy/verify` | fresh interactive `manage_platform`/`admin` |
| `POST` | `/api/account/admin/platform/deploy/converge` | fresh interactive `manage_platform`/`admin` |
| `POST` | `/api/account/admin/advanced/settings` | fresh interactive `manage_advanced`/`admin` plus literal superuser |

Writes refuse API keys and group-scoped tokens and use a fixed 600-second
fresh-auth window. `retry` requires `deployment` and reuses that row's SHA.
`verify` and `converge` also require `deployment`; proof must match UUID and
SHA. `Idempotency-Key` makes retry submission replay-safe.

Overview sections have this stable shape:

```json
{
  "status": "healthy",
  "observed_at": "2026-08-10T18:00:00+00:00",
  "stale_after": "2026-08-10T18:10:00+00:00",
  "data": {"reachable": true}
}
```

Render `unhealthy`, `unauthorized`, `unavailable`, `timeout`, and
`unconfigured` distinctly. Never infer health from absence or retain evidence
past `stale_after`. Provider errors are intentionally not exposed.

The settings endpoint accepts exactly one typed family:

```json
{"auth": {"theme.app_title": "Operations", "theme.accent_color": "#112233", "login.methods": ["password", "passkey"], "registration.enabled": true, "registration.methods": ["password", "github"], "registration.passkey_prompt": "optional"}}
```

```json
{"edge_topology": {"nodes": ["edge-a", "edge-b"], "pools": ["public-web"]}}
```

Auth appearance and method fields are allowlisted. Login methods must include
`password`; enabled registration must have at least one method. Navigation,
redirect, API-base, external CSS, credentials, arbitrary settings, deploy
commands, AWS settings, and KMS settings are not writable here.
