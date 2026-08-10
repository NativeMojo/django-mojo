# Admin Dashboard integration contract

The built-in Admin has five primary navigation items, in this order:
Dashboard, People, Web Apps, Platform, and Activity. System Setup, deployments,
and the collapsed Advanced raw-resource disclosure are destinations inside
Platform; they are not additional primary navigation.

Dashboard answers two questions only: can customers use the system, and can
operators detect failures? `GET /api/account/admin/dashboard` returns a small
matrix for public API, edge fleet, Web Apps, detection, last deployment, open
Incidents, and open Tickets. Each source checks its own permission before its
collector runs. A denied source is `permission_denied`, a collector failure is
`unknown`, missing configuration is `unconfigured`, old evidence is `stale`,
actionable evidence is `degraded`, and a proven failure is `unhealthy`.

| Source | Global read authority |
|---|---|
| Public API, fleet, last deployment | `view_platform`, `manage_platform`, `admin` |
| Web Apps | `view_dns`, `manage_dns`, `security`, `admin` |
| Detection posture (`security`) | `view_platform_security`, `manage_platform`, `admin` |
| Open Incidents and Tickets | `view_security`, `manage_security`, `security`, `admin` |

The endpoint itself first requires Admin source access (`view_admin`,
`manage_users`, or `admin`); those source grants do not bypass the per-row
checks above.

`overall` is computed only from observable sources. Permission-denied and
unknown sources never become healthy and never contribute a guessed state. If
nothing is observable, overall is `unknown`. Dashboard never calls System
Setup readiness. Its Setup link is rendered only when bootstrap publishes
`capabilities.setup`, which is derived from literal `User.is_superuser`.

Cross-feature hashes use `assets/components/routes.js`. It is the canonical
encoder/decoder for Activity tab and filters, subject type/id/model, inspector
identity, and bounded return location. Use `routeHref()` or `activityHref()`;
do not concatenate a second query vocabulary in a feature package.

Secret-bearing request and response bodies are classified before views run.
This includes People password and API-key actions, WebApp deployment-key
linkage, DNS credential linking, registrar quote/purchase confirmation, and
certificate material. Authorized reveal responses remain deliberately usable,
but request logging stores only a fixed server-owned marker. Ordinary graphs,
Admin aggregate responses, Activity, and audit rows must never contain the raw
value.

For deterministic visual review, `bin/admin_preview` supports
`--dashboard-state healthy|degraded|denied|unknown` in addition to the feature
state switches. Every launch resets provider rows and preview events.
