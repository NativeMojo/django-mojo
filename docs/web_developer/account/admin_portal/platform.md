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

`GET /api/account/admin/platform` accepts an optional `?sections=` comma-
separated allowlist, for example `?sections=deployments,api` (what the merged
Admin Deployments lane sends). It narrows work, not authority — each section
keeps its own permission check. Unknown names are ignored; an absent or empty
parameter returns the full roster above; a parameter naming no known section
returns an empty `sections` object.

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

`deployments.data.framework_pin` reports which django-mojo version the next
fleet deploy will install:

```json
{"configured": true, "value": "hold", "mode": "hold", "resolved": "1.11.9"}
```

`mode` is `latest` (nothing configured — the newest published release),
`pinned` (`value` is installed verbatim), or `hold` (stay on the last converged
fleet version). `resolved` is what the mode currently works out to, and is
`null` under `latest` — and under `hold` on a fleet with no converged
deployment, which is a refused-deploy state worth surfacing rather than hiding.
The object is additive; existing `deployments` fields are unchanged.

The three deployment actions accept `{"deployment":"<uuid>"}`. Retry returns
`{"schema_version":1,"queued":true|false,"deployment":{...}}`; verify and
converge return the same version plus the serialized `deployment`. A deployment
contains its UUID, SHA, source, status, frozen runner roster, bounded
transitions and latest-per-runner evidence, desired/current commits, and
timing. It never contains a raw idempotency key or provider exception.

**`node_evidence[].detail.stderr_tail` is permission-dependent.** When a node's
update script fails, its evidence detail can carry the last ten lines of that
script's stderr. Those lines are redacted, but not provably free of
credentials, so the key is present **only** for callers holding
`view_platform_security`, `manage_platform`, or `admin`. A caller with just
`view_platform` receives the same evidence entry with everything else intact
(`runner`, `state`, `phase`, `exit`) and no `stderr_tail` key at all — treat
its absence as "not permitted or nothing captured", never as an error. The
three deployment actions above always include it, since they already require
`manage_platform` and fresh auth.

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

```json
{"framework_pin": "1.11.9"}
```

`framework_pin` writes the framework version hold behind
`deployments.data.framework_pin`. Send a published django-mojo version,
`"hold"`, or `""` to unset (`"latest"`, `"none"`, and `"auto"` are accepted
synonyms for unset). It carries the same authority as the other two families —
superuser, fresh interactive session, no key-backed sessions — and the response
returns the **normalized** stored value, so `"HOLD"` comes back as `"hold"` and
`"latest"` as `""`. Any other value is a validation error whose message names
the accepted forms; the version is not checked against PyPI at write time. The
change applies from the next deploy and never moves one already in flight, so
re-read the Platform overview rather than assuming a running deploy shifted.

Auth appearance and method fields are allowlisted. Login methods must include
`password`; enabled registration must have at least one method. Navigation,
redirect, API-base, external CSS, credentials, arbitrary settings, deploy
commands, AWS settings, and KMS settings are not writable here.
On success settings returns
`{"schema_version":1,"saved":true,"value":{...}}` with the complete saved
typed family.
