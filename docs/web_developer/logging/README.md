# Logging & Security — REST API Reference

- [Log API](logs.md) — Query application and audit logs
- [Incident API](incidents.md) — View and manage security incidents, tickets, and history
- [Reporting Events](reporting_events.md) — Report events from a web page
- [Security Overview](../security/README.md) — Unified guide to building a security dashboard

## Activity-oriented list reads

The built-in Admin Activity center is a reference consumer of the public list
APIs. Use the dedicated graph for bounded operator tables:

```http
GET /api/incident/incident?graph=activity&size=25&start=0&sort=-created
GET /api/incident/event?graph=activity&size=25&start=0&sort=-created
GET /api/logs?graph=activity&size=25&start=0&sort=-created
GET /api/incident/ticket?graph=activity&size=25&start=0&sort=-modified
```

The response envelope supplies `results`, `count`, `start`, and `size`. Treat a
successful `count: 0` as empty and an authorization/transport failure as
unavailable; never substitute zero for a source that could not be read. Logs
require `view_logs` (or their broader Log grants), while the three incident
sources require `view_security`; Incident/Ticket writes additionally require
`manage_security`.

Free-text `search` is intentionally narrower than the returned graph and never
searches JSON evidence or request payloads. The Event graph exposes scalar
incident/group ids without `geo_ip`; Ticket exposes scalar relationship ids and
minimal labels without nested User/Group objects. Use those scalars for deep
links rather than client-side joins.

The exact search columns are:

| Source | `search` columns |
|---|---|
| Log | `kind`, `method`, `path`, `ip`, `username`, `log`, `model_name` |
| Event and Incident | `category`, `title`, `details`, `source_ip`, `hostname`, `model_name` |
| Ticket | `title`, `description`, `status`, `category` |

WebApp onboarding `choose` and `workflow` POST bodies are never available in
request/response logs. They can carry a registrar confirmation token or a
reveal-once deployment key, so the server replaces the complete bodies with a
fixed sensitive marker before database or file logging.

Do not use `/api/incident/stats` to build Activity counts. It is not the
permission-scoped list authority. Request a size-one page from each source and
use that envelope's `count` instead.
