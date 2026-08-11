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

Platform returns sections `api`, `fleet`, `jobs`, `sanity`, `database`,
`redis`, `deployments`, `certificates`, `security`, and `webapps`. Advanced
returns `hosting`, `aws_inventory`, and `network_security`. The curated
[Settings API](settings.md) owns effective configuration and typed UI controls.
Permission is checked per section, so a caller admitted to the endpoint can
still receive `unauthorized` for a narrower section.

`sanity.data.local_target_source` is one of `configured_static`,
`request_server_port`, or `default_80`; no raw local setting is serialized.
Security returns boolean secure-posture controls plus the names of disabled
HTTPS redirect, secure-cookie, and HSTS controls. Its open incident count and
rows exclude `ignored`, `resolved`, and `closed` using the same predicate as
the Dashboard.

`webapps.data` keeps four facts independent: configured HTTPS origins,
freshness-bearing current public health, historical onboarding status, and
deployment-key active/inactive state. Current health uses a DNS-pinned HTTPS
root probe, follows no redirect, and is bounded to 24 rows, four workers, 1.5
seconds per row, and a 2.5-second collector deadline. Each row supplies
`observed_at` and `stale_after`; missing, unsafe, timed-out, and unknown proof
never becomes healthy. Historical `not_started` alone does not degrade a
currently healthy origin.

The three deployment actions accept `{"deployment":"<uuid>"}`. Retry returns
`{"schema_version":1,"queued":true|false,"deployment":{...}}`; verify and
converge return the same version plus the serialized `deployment`. A deployment
contains its UUID, SHA, source, status, frozen runner roster, bounded
transitions and latest-per-runner evidence, desired/current commits, and
timing. It never contains a raw idempotency key or provider exception.

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
On success settings returns
`{"schema_version":1,"saved":true,"value":{...}}` with the complete saved
typed family.
