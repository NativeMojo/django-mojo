# Logging & Security — Django Developer Reference

- [logit App](logit.md) — Database logging via the Log model
- [Incident System](incidents.md) — Security events, rule engine, fleet-wide IP blocking, OSSEC integration

## Built-in Admin Activity center

The built-in Admin portal consumes Logs, Incidents, Events, and Tickets through
their existing REST models. The Activity provider publishes three independent
booleans: `view_logs`, `view_security`, and `manage_security`. Do not collapse
them into one UI grant: a log viewer must not gain Incident/Ticket access, and a
security reader must not gain lifecycle writes.

Activity list calls use `graph=activity`. Keep these graphs purpose-built:

- `Event.activity` uses scalar `group_id` and `incident_id` and must not include
  `geo_ip`; resolving it per row is an N+1 query path.
- `Ticket.activity` uses scalar User/Group/Incident ids plus only the minimal
  labels needed in the table. Its list path selects those relations in one
  query and never reuses the nested default account graphs.
- Log and Incident use explicit bounded field lists. Structured evidence is
  visible only behind the endpoint's existing permission and is masked again
  by the browser before rendering or copying.

Every Activity-backed model declares explicit `SEARCH_FIELDS`. Never add JSON
metadata, payloads, credential material, device ids, or user-agent strings to
those fields; search/filter/count behavior is an information surface even when
the corresponding value is absent from a graph.

The landed allowlists are exact: Log searches `kind`, `method`, `path`, `ip`,
`username`, `log`, and `model_name`; Event and Incident search `category`,
`title`, `details`, `source_ip`, `hostname`, and `model_name`; Ticket searches
`title`, `description`, `status`, and `category`.

Credential-bearing Admin and onboarding routes are classified by path before
request parsing. In particular, WebApp onboarding `choose` and `workflow`
POSTs replace both request and response bodies with a fixed marker in database
and file logs; confirmation tokens and reveal-once deployment keys never enter
`requests.log` even when broad debug logging is enabled.

Ticket `created` and `modified` are indexed because the center pages and sorts
on both. The schema change is owned by incident migration `0038`. Generate any
successor migration with `bin/create_testproject` and inspect the operations
before committing.

The browser deliberately does not use `/api/incident/stats`. It gets each count
from a size-one request to the same scoped list endpoint, preserving permissions
and filters and distinguishing an actual zero from an unavailable source.
Incident/Ticket lifecycle changes continue through their normal REST saves so
`IncidentHistory` and `TicketNote` hooks remain the audit authority.

The Activity table keeps search visible and collapses secondary query controls
behind a Filters disclosure. Active secondary filters are counted on the
trigger, while Clear resets only those visible query controls and preserves any
permission-scoped subject link that established the current context.
