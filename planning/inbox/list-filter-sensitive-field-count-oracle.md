---
id:
type: bug
title: "List filter path has no sensitive-field guard — count() is a value-probing oracle on auth_key/secrets"
priority: P2
owner: backend
opened: 2026-07-18
depends_on: []
related: [DM-051]
links: []
---

# List filter path has no sensitive-field guard — count() is a value-probing oracle

## What & Why
The RestMeta list-filter path (`on_rest_list_filter` →
`build_rest_filters`, `mojo/models/rest.py`) applies `queryset.filter(**f)`
over **any** model field with no whitelist/blacklist. The only forbidden-field
concepts that exist are save-side (`NO_SAVE_FIELDS`), serialize-side
(`NO_SHOW_FIELDS`), and — crucially — the *aggregation* layer's
`RestMeta.SENSITIVE_FIELDS`/`AGGREGATION_FIELDS` (`rest_aggregation.py:330-400`),
which guard only the **value-exposing** modes (top/distinct/summary/histogram).

Nothing guards the *count* a filter produces. Any caller with plain list
permission on a model can filter on a sensitive column and read the row count
from the list envelope (`count`) or `_mode=count`:

```
GET /api/<model>?auth_key__startswith=a    → count: 1
GET /api/<model>?auth_key__startswith=ab   → count: 1   ...
```

i.e. a char-by-char **value-probing oracle** over secret/opaque columns
(`auth_key`, tokens, hashes, any indexed secret) for rows the caller can list.
The `took_ms` 10ms rounding in the aggregation layer damps *timing* oracles but
does nothing for this **count-difference** oracle.

This is **pre-existing** (predates DM-051) and DM-051 adds no new exposure —
`_stats` bundles reuse the same parser, so they can reach the same fields, but
every such count is already obtainable via a plain list request today. Filed so
the oracle itself isn't lost. Found during DM-051 scope recon (2026-07-18).

## Acceptance Criteria
- [ ] A caller cannot filter a list endpoint on a field the model marks
      sensitive — the filter is rejected (400) or silently dropped, consistently
      for the plain list path, `_mode=count`, and DM-051 `_stats` bundles (they
      share `build_rest_filters`, so one guard covers all three).
- [ ] Reuse the existing `RestMeta.SENSITIVE_FIELDS` declaration rather than a
      new mechanism (align filter-path and aggregation-path field safety).
- [ ] Decide + document the posture: reject-loud vs drop-silent (drop-silent
      avoids a presence oracle on the field name; reject-loud is clearer for
      legit callers). Lean drop-silent to match how unknown fields already
      behave on the filter path.
- [ ] Audit which in-tree models actually expose secret/opaque columns through a
      listable model and set `SENSITIVE_FIELDS` on them (ApiKey.auth_key,
      User auth/secret columns, MojoSecrets subclasses, …).
- [ ] Tests: filtering a sensitive field yields no oracle (count identical
      with/without the sensitive filter, or 400); non-sensitive filters
      unaffected; covers list, `_mode=count`, and `_stats`.
- [ ] Docs: `docs/web_developer/core/filtering.md` +
      `docs/django_developer/core/mojo_model.md` note the filter-path
      sensitive-field guard.

## Repro — bugs only
1. Pick a listable model with an opaque/secret column (e.g. one exposing
   `auth_key`). As a caller with only list perm, request
   `?<col>__startswith=<guess>` and read `count`; vary the guess prefix.
- Expected: sensitive columns are not filterable — no count signal that reveals
  the stored value.
- Actual: `count` changes with the guessed prefix → char-by-char value recovery.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- **Batching amplification (DM-051 security-review, 2026-07-18):** DM-051's
  `_mode=count&_stats=` now lets one authenticated request carry up to
  `MOJO_REST_AGG_STATS_CAP` (default 12) arbitrary-field count probes. This is
  NOT new value exposure (each count is already obtainable via a plain list
  request), but it amortizes any per-request rate-limit / anomaly detection by
  up to ~12× — a char-by-char probing loop that a limiter would throttle
  per-request now advances ~12 guesses per request. Per-COUNT DB work is
  unchanged and the cap bounds it. Fix belongs here (guard the shared
  `build_rest_filters` choke point) — that covers list, `_mode=count`, AND
  `_stats` at once.
- Natural home for the guard: inside `build_rest_filters`
  (`mojo/models/rest.py`, the shared parser DM-051 extracts) so list,
  `_mode=count`, and `_stats` are all covered at one choke point.
- Cross-check the aggregation layer's `_validate_field` (`rest_aggregation.py`)
  for the `SENSITIVE_FIELDS` read pattern to stay consistent.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
