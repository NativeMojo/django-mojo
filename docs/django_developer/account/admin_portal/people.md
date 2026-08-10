# Admin People feature

People is the account-owned Admin feature at
`assets/features/people/{feature.js,page.js,styles.css,manifest.json}`. It uses
the foundation's standard table, right inspector, lifecycle, relationship,
modal, and view primitives; it owns no shell or shared component code.

The server descriptor publishes independent booleans for User and Group view,
User and Group management, API-key custody, sign-in evidence, and the Logs,
Events, Incidents, and Tickets lanes. Those values hide unusable controls but
REST/model security is always authoritative.

Custom boundaries are deliberately small:

- selected-user reset and temporary-password actions require global
  `users`/`manage_users`, reject key-backed sessions, and use an explicit
  600-second freshness window;
- permission bundles are version 1, map the seven operator-facing bundles to
  canonical permission keys, and diff only those managed keys so unknown and
  Advanced-only entries survive;
- API-key rotate/deactivate/reactivate/revoke requires object edit authority,
  rejects key-backed sessions, and uses the same explicit freshness window;
- request and response bodies for auth/password/API-key routes are replaced by
  server-owned markers before debug or file logging can inspect them.

Bundle version 1 is exact:

| Bundle | Canonical permission keys |
|---|---|
| People | `view_users`, `manage_users`, `view_groups`, `manage_groups` |
| Platform | `view_admin`, `view_global`, `manage_aws` |
| Network & Hosting | `view_dns`, `manage_dns` |
| Deployments | `manage_webapp`, `manage_deploy`, `view_github`, `manage_github` |
| Security & Incidents | `view_security`, `manage_security` |
| Logs & Metrics | `view_logs`, `manage_logs`, `view_metrics`, `manage_metrics` |
| System Administration | `manage_settings`, `view_jobs`, `manage_jobs`, `view_taskqueue`, `admin_compliance`, `admin_verify` |

Activity links accept exactly `tab`, `start`, `size`, `sort`, `search`, `date`,
`user`, `group`, `ip`, `hostname`, `incident`, `model`, and `model_id`.
Destinations own actor-versus-subject correlation, bounds, deduplication, and
lane authorization.

The preview provider resets Users, Groups, members, keys, login evidence, and
all transient actions between launches. Secret dialogs hold plaintext only in
closure/input state and erase both on close. The portal never requests the
compatible `graph=token` API-key recovery graph.
