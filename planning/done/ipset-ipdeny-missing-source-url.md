# IPSet _fetch_ipdeny does not set source_url

**Type**: bug
**Status**: resolved
**Date**: 2026-04-01
**Severity**: medium

## Description

`_fetch_ipdeny` silently returns `None` when `source_url` is not set, instead of deriving the URL from the ipset name. The ipdeny URL pattern is deterministic (`http://www.ipdeny.com/ipblocks/data/countries/{code}.zone`), so the method should auto-construct it from the country code in the name (e.g. `country_cn` -> `cn.zone`).

Currently only `create_country()` sets `source_url`. Any IPSet created through REST, admin, or direct ORM with `source="ipdeny"` will have `source_url=None` and `refresh_from_source()` will silently fail.

## Context

This means country-based IP blocking only works if the IPSet was created via the `create_country` helper. REST-created records with `source="ipdeny"` appear functional but the refresh action does nothing — no error, no data. Users won't know why their ipset has no CIDRs.

## Acceptance Criteria

- `_fetch_ipdeny` auto-constructs `source_url` from the name when it's not already set (extract country code from `name`, build `http://www.ipdeny.com/ipblocks/data/countries/{code}.zone`)
- `source_url` is persisted to the record so subsequent fetches don't re-derive it
- If the country code cannot be derived from the name, a clear error is raised or stored in `sync_error` (not a silent `None` return)

## Investigation

**Likely root cause**: `_fetch_ipdeny` (line 157-158) guards on `self.source_url` and returns `None` if unset, but never derives the URL from the ipset name. The URL construction logic only exists in `create_country` (line 189).

**Confidence**: confirmed

**Code path**:
- `mojo/apps/incident/models/ipset.py:154-162` — `_fetch_ipdeny` returns None when source_url missing
- `mojo/apps/incident/models/ipset.py:129-152` — `refresh_from_source` calls `_fetch_ipdeny`, gets None, returns False
- `mojo/apps/incident/models/ipset.py:179-192` — `create_country` is the only place source_url is set

**Regression test**: not feasible — requires network call to ipdeny.com or mock server

**Related files**:
- `mojo/apps/incident/models/ipset.py`

## Plan

**Status**: resolved
**Planned**: 2026-04-01

### Objective

Make `_fetch_ipdeny` auto-construct and persist `source_url` from the ipset name when it's not already set.

### Steps

1. `mojo/apps/incident/models/ipset.py:154-162` — In `_fetch_ipdeny`, when `self.source_url` is not set: extract country code from `self.name` (strip `country_` prefix), build `http://www.ipdeny.com/ipblocks/data/countries/{code}.zone`, save it to `self.source_url` and persist with `update_fields=["source_url"]`, then proceed with the fetch. If name doesn't match `country_*` pattern or code is empty, raise a clear error (caught by `refresh_from_source` and stored in `sync_error`).

### Design Decisions

- **Derive from name, not a new field**: The `create_country` helper already uses `country_{code}` naming convention — reuse that pattern rather than adding a new model field.
- **Use `http://` not `https://`**: ipdeny.com serves zone files over HTTP only.
- **Persist derived URL**: Save to `source_url` so it's visible in REST responses and doesn't re-derive on every refresh.

### Edge Cases

- **Name doesn't start with `country_`**: Raise `ValueError` with descriptive message — caught by `refresh_from_source` exception handler and stored in `sync_error`.
- **Name is `country_` with no code after prefix**: Same — validate that extracted code is non-empty.

### Testing

- URL derivation and persistence logic (no network calls needed) -> `tests/test_incident/test_ipset.py`

### Docs

- No changes needed — `docs/django_developer/logging/incidents.md` already states `source_url` is "auto-populated for known sources" (line 111).

## Resolution

**Status**: resolved
**Date**: 2026-04-02

### What Was Built
`_fetch_ipdeny` now auto-derives `source_url` from the ipset name when not set, with strict 2-letter country code validation and HTTPS.

### Files Changed
- `mojo/apps/incident/models/ipset.py` — `_fetch_ipdeny` auto-constructs and persists `source_url`, validates country code with `[a-z]{2}` regex, uses `https://`
- `tests/test_incident/test_ipset.py` — 6 tests covering derivation, error cases, path traversal rejection, and existing URL preservation

### Tests
- `tests/test_incident/test_ipset.py` — URL derivation, bad names, empty codes, invalid codes (traversal/uppercase/too long), sync_error storage, existing URL preservation
- Run: `bin/run_tests -t test_incident.test_ipset`

### Docs Updated
- None needed — existing docs already accurate

### Security Review
- Added `[a-z]{2}` regex validation to prevent path traversal and URL injection via crafted names
- Changed `http://` to `https://` to match `create_country()` and prevent MITM tampering of CIDR data

### Follow-up
- None
