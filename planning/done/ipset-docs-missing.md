# IPSet Admin API Documentation Missing

**Type**: bug
**Status**: resolved
**Date**: 2026-04-01
**Severity**: high

## Description

The IPSet REST API (`/api/incident/ipset`) has no web developer documentation. Admin portal developers have no reference for how to list, create, manage, or trigger actions on IPSets — the primary mechanism for bulk-blocking countries like China and Russia that generate mass bot traffic.

Additionally, the django_developer docs have a wrong permission listed for the IPSet endpoint.

## Context

IPSets are the only way to block entire countries or large CIDR ranges at the kernel level (O(1) lookups via Linux ipset). This is a critical security feature for any production deployment facing bot attacks. Without docs, admin portal developers cannot build the UI to manage these blocks.

The backend (django_developer) docs in `logging/incidents.md` cover the model fields and actions well, but the web_developer side — which is what someone building an admin portal actually needs — has nothing.

## Issues Found

### 1. No web_developer IPSet API docs
- `docs/web_developer/security/README.md` mentions `/api/incident/ipset` in the APIs at a Glance table (line 35) but provides zero usage documentation — no field reference, no request/response examples, no action workflows
- `docs/web_developer/account/admin_portal.md` mentions ipset on line 162 in the common endpoints table but gives no guidance on how to use it
- There is no dedicated IPSet section anywhere in web_developer docs

### 2. Wrong permission in django_developer docs
- `docs/django_developer/logging/incidents.md:139` says the IPSet endpoint requires `manage_users`
- The actual model RestMeta (`mojo/apps/incident/models/ipset.py:56-58`) uses:
  - `VIEW_PERMS = ["view_security", "security"]`
  - `SAVE_PERMS = ["manage_security", "security"]`
  - `DELETE_PERMS = ["manage_security"]`

## Acceptance Criteria

- Web developer docs include a complete IPSet API section covering:
  - Endpoint paths and methods (GET list, GET detail, POST create, POST update, DELETE)
  - All model fields with descriptions (name, kind, source, source_url, source_key, data, is_enabled, cidr_count, last_synced, sync_error)
  - POST_SAVE_ACTIONS usage: sync, enable, disable, refresh_source (with request examples)
  - Common workflow: blocking a country (create → refresh_source → sync)
  - Common workflow: blocking abuse IPs via AbuseIPDB
  - Manual/custom CIDR list workflow
  - Permissions required (view_security/security for read, manage_security/security for write)
  - GRAPHS: default (excludes data) vs detailed (includes data)
- `docs/django_developer/logging/incidents.md:139` permission corrected from `manage_users` to `manage_security` / `security`
- Admin portal doc references the new IPSet section

## Investigation

**Likely root cause**: IPSet was added recently and backend docs were written but web_developer docs were not created. The permission typo in django_developer docs is a copy-paste error (likely from GeoLocatedIP which does use `manage_users`).

**Confidence**: confirmed

**Code path**:
- Model: `mojo/apps/incident/models/ipset.py:55-67` (RestMeta with correct perms)
- REST handler: `mojo/apps/incident/rest/ipset.py:5-8` (standard CRUD)
- Async jobs: `mojo/apps/incident/asyncjobs.py:115-172` (broadcast + cron)
- Firewall: `mojo/apps/incident/firewall.py` (low-level ipset/iptables)

**Regression test**: not feasible — this is a documentation-only issue

**Related files**:
- `docs/web_developer/security/README.md` — add IPSet API section
- `docs/web_developer/account/admin_portal.md` — reference new section
- `docs/django_developer/logging/incidents.md:139` — fix permission typo

## Plan

**Status**: planned
**Planned**: 2026-04-01

### Objective
Add complete web developer IPSet API documentation and fix the wrong permission in the django_developer docs.

### Steps
1. `docs/django_developer/logging/incidents.md` line 139 — Change `manage_users` to `manage_security` / `security` in the REST Endpoint table under "Bulk Blocking via IPSet"
2. `docs/web_developer/security/README.md` — Add `IPSet` row to the "APIs at a Glance" table (currently missing entirely)
3. `docs/web_developer/security/README.md` — Add a new `## IPSet Bulk Blocking` section covering: endpoint paths/methods, all fields with descriptions, POST_SAVE_ACTIONS reference, three common workflows (block a country, AbuseIPDB abuse list, manual CIDR list), permissions, and graph variants
4. `docs/web_developer/account/admin_portal.md` — Update the "Firewall / IP blocks" row in the Common Admin Endpoints table to link to the new IPSet section

### Design Decisions
- No new doc file: The IPSet section fits naturally in `security/README.md` alongside other firewall content — not large enough to warrant its own page.
- Workflows over just reference: Include three step-by-step scenarios (country block, AbuseIPDB, manual) not just field tables, matching the acceptance criteria.

### Edge Cases
- `data` field is excluded from the `default` graph — must use `graph=detailed` to see CIDR contents. Needs a callout in the docs.
- `source_key` stores the AbuseIPDB API key — sensitive; should note it is write-only and not returned in the default graph.

### Testing
- Not applicable — docs-only change; no regression test feasible per issue investigation.

### Docs
- `docs/web_developer/security/README.md` — primary change (IPSet section + APIs table row)
- `docs/django_developer/logging/incidents.md` — permission fix
- `docs/web_developer/account/admin_portal.md` — cross-reference update

## Resolution

**Status**: resolved
**Date**: 2026-04-01

### What Was Built
Complete web developer IPSet API documentation plus two security fixes surfaced during review.

### Files Changed
- `docs/web_developer/security/README.md` — Added IPSet row to APIs at a Glance table; added full `## IPSet Bulk Blocking` section (fields, actions, graphs, 3 workflows, permissions)
- `docs/django_developer/logging/incidents.md` — Fixed permission typo (`manage_users` → correct `view_security`/`manage_security`/`security`); updated GRAPHS block to show `source_key` exclusion
- `docs/web_developer/account/admin_portal.md` — Added link from Firewall row to new IPSet section
- `mojo/apps/incident/models/ipset.py` — Excluded `source_key` from all REST graphs (security fix: was previously returned in `?graph=detailed`)

### Tests
- Not applicable — docs-only change. No regression test feasible.

### Docs Updated
- `docs/web_developer/security/README.md` — new IPSet section
- `docs/django_developer/logging/incidents.md` — permission fix + graphs update
- `docs/web_developer/account/admin_portal.md` — cross-reference

### Security Review
Two findings addressed:
1. **CRITICAL (fixed)**: `source_key` (AbuseIPDB API key) was returned by `?graph=detailed`. Fixed by adding `source_key` to `exclude` in both graph variants in `RestMeta`.
2. **WARNING (fixed)**: Docs incorrectly implied `security` permission grants DELETE. Only `manage_security` is accepted for DELETE per `DELETE_PERMS`. Permissions table and field reference corrected.

### Follow-up
- None
