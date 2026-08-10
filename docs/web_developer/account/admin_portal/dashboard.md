# Admin Dashboard API

The packaged Admin uses exactly five primary destinations: Dashboard, People,
Web Apps, Activity, and Platform. Deployments, literal-superuser System Setup,
and a collapsed Advanced resource list live under Platform.

`GET /api/account/admin/dashboard` requires ordinary Admin source access and
returns independently permissioned source envelopes:

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
| Detection posture | `view_platform_security`, `manage_platform`, `admin` |
| Open Incidents and Tickets | `view_security`, `manage_security`, `security`, `admin` |

Packaged cross-links use one canonical hash state for `subject_type`,
`subject_id`, `subject_model`, `inspector`, Activity filters, and `return`.
Unknown keys are rejected by Activity rather than broadening a filtered query.

Secret reveal endpoints are exceptions by exact route and response. All later
status/detail/list/aggregate responses remain non-reveal, and request/response
logging records only fixed sensitivity markers for password, API-key, WebApp
deployment-key, DNS credential, registrar confirmation, and certificate
material routes.
