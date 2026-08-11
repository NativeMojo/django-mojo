# Admin Dashboard API

The packaged Admin uses six primary destinations: Dashboard, People, Web Apps,
Domains & DNS, Platform, and Activity. Domains & DNS is permission-gated by
DNS read/manage authority because it is an ongoing application control, not a
setup-only surface. Deployments and literal-superuser System Setup live under
Platform; Advanced is one expert-diagnostics destination rather than a
resource list.

`GET /api/account/admin/dashboard` requires the same global source-access grant
as the built-in Admin (`view_admin`, `manage_users`, or `admin`). It then checks
each source independently before collecting it and returns:

```json
{
  "schema_version": 1,
  "overall": "degraded",
  "observable_sources": 6,
  "sources": {
    "public_api": {"status": "healthy", "observed_at": "...", "data": {}},
    "security": {"status": "permission_denied", "data": {}},
    "incidents": {"status": "degraded", "data": {"open": 2}}
  }
}
```

Statuses are semantic and must not be collapsed: `permission_denied` means the
collector was not run; `unknown` means no authoritative answer returned;
`unconfigured` is observed absence of configuration; `stale` is old evidence;
`degraded` needs attention; `unhealthy` is a proven failure. Only observable
statuses contribute to `overall`. Dashboard does not invoke the System Setup
readiness API.

| Source | Read authority |
|---|---|
| Public API, fleet, last deployment | `view_platform`, `manage_platform`, `admin` |
| Web Apps | `view_dns`, `manage_dns`, `security`, `admin` |
| Detection posture (`security`) | `view_platform_security`, `manage_platform`, `admin` |
| Open Incidents and Tickets | `view_security`, `manage_security`, `security`, `admin` |

Packaged cross-links use one canonical hash state for `subject_type`,
`subject_id`, `subject_model`, `inspector`, Activity filters, and `return`.
Unknown keys are rejected by Activity rather than broadening a filtered query.
Actionable cards also use bounded `focus`: missing Public API configuration
opens Setup on `django.base_url`, fleet evidence focuses Platform's fleet card,
and incident/ticket cards open their exact Activity tab. Ignored, resolved, and
closed incidents never contribute to the open-attention count.

Secret reveal endpoints are exceptions by exact route and response. All later
status/detail/list/aggregate responses remain non-reveal, and request/response
logging records only fixed sensitivity markers for password, API-key, WebApp
deployment-key, DNS credential, registrar confirmation, and certificate
material routes.
