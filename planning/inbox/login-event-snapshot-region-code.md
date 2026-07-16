---
id:
type: chore
title: UserLoginEvent.track() should also snapshot region_code (ISO 3166-2) alongside the region name
priority: P3
effort: XS
owner: backend
opened: 2026-07-16
depends_on: []
related: []
links: []
---

# Snapshot region_code on UserLoginEvent

## What & Why
`UserLoginEvent` denormalizes geo from `GeoLocatedIP` at `track()`
(`mojo/apps/account/models/login_event.py:92-106`) but copies only the
subdivision NAME (`region="California"`) — `GeoLocatedIP.region_code`
(`US-CA`, `geolocated_ip.py:38`) is dropped. Downstream consumers that
compare login geo against code-based policy lists must maintain their own
name→code mapping (wmx_api's geolocation compliance report builder now
does exactly that — `apps/wmx/reports/services/builders/geolocation.py`
`_STATE_CODE_BY_NAME`, WMX-API-131).

Add a `region_code` column, populate it in `track()` from
`geo.region_code`, expose it in the RestMeta graphs. Historical rows stay
name-only (consumers keep their mapping for history); new rows become
directly joinable against ISO code lists.

## Acceptance Criteria
- [ ] `region_code` CharField (indexed, nullable) populated by `track()`.
- [ ] Included in `basic`/`list` graphs + CSV format if present.
- [ ] Migration + test (track() writes both name and code).
