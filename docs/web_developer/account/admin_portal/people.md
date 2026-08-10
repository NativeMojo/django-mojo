# Admin People

People provides searchable Users and Groups. Selecting a row opens the standard
right inspector rather than navigating to an arbitrary model editor.

User sections cover identity and verification, lifecycle, invitation/reset,
temporary password, session revocation, seven high-level access bundles,
stored sign-in evidence, and links to related Activity lanes. Group sections
cover identity and searchable parent selection, members and roles, API-key
lifecycle, permissions, related Activity, and read-only Advanced metadata.

Temporary passwords and newly created or rotated API-key tokens are shown once.
Copy them before closing the dialog; closing clears the input and in-memory
value. They must not be copied into URLs, browser storage, telemetry, or
downloads. The portal does not use API-key `graph=token` recovery.

Sensitive mutations may answer HTTP `440 reauth_required` when the current
interactive authentication is older than 600 seconds. Return through the
Bouncer reauthentication flow and repeat the intentional action. API keys and
group-scoped tokens cannot perform these actions.

## People-specific endpoints

These endpoints supplement ordinary User, Group, member, login-event, and
group API-key REST. Successful values appear in the normal response `data`.

| Method and route | Request | Authority and response |
|---|---|---|
| `POST /api/account/admin/user/password/reset` | `{"user": 42}` | Global `users` or `manage_users`, fresh 600-second interactive auth. Returns `{"user":42,"sent":true}`; no password or reset token is returned. |
| `POST /api/account/admin/user/password/temporary` | `{"user": 42}` | Same authority. Returns `user`, `requires_password_change:true`, and `temporary_password` exactly once. |
| `GET /api/account/admin/people/permission-bundles?user=42` | query `user` | Global `view_users`, `manage_users`, or `users`. Returns `version:1`, `user`, `selected`, and the seven bundle definitions. |
| `POST /api/account/admin/people/permission-bundles` | `{"user":42,"version":1,"selected":["people","logs_metrics"]}` | Global `manage_users` or `users`, fresh 600-second interactive auth. Returns the updated versioned description; a stale version is rejected. |
| `POST /api/account/admin/apikey/action` | `{"api_key":91,"action":"rotate"}` | The key's ordinary object edit authority, fresh 600-second interactive auth. Actions are `rotate`, `deactivate`, `reactivate`, and `revoke`; only rotate returns a one-time `token`. |

Bundle saves change only the canonical keys owned by version 1. Unknown,
product-specific, and Advanced-only permission entries survive the update.
Reset, temporary-password, bundle, and key-lifecycle routes refuse API-key and
group-token sessions.

People links to Activity through the canonical hash state: `tab`, `size`,
`sort`, `subject_type`, `subject_id`, optional `subject_model`, and bounded
`return`. Activity translates the subject to a supported stored relationship;
it refuses an unsupported subject/lane combination instead of dropping the
filter. The Activity feature separately authorizes each lane, so a visible
People record does not grant access to its Logs, Events, Incidents, or Tickets.
