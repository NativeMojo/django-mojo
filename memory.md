# Django-MOJO Working Memory

Use this file as a lightweight running log between AI threads.

## Memory Hygiene Rules

- Keep this file compact and current.
- Keep only active/recent context in main sections.
- Cap each section to 5 active bullets max.
- Prefer outcomes and decisions over long narrative notes.
- Remove stale items once completed or no longer relevant.
- If historical context is still useful, move it to a short dated "Archive" section.

## Current Focus

- Active task: Align shortlink + metrics behavior/docs/tests, then close security gaps.
- Requested by: ians
- Date: 2026-03-14

## Key Decisions

- [x] Decision: Always keep global shortlink analytics (`shortlink:click`) on.
  - Reason: Product requirement for global visibility.
  - Impacted files: `mojo/apps/shortlink/models/shortlink.py`, shortlink docs
- [x] Decision: Remove per-source shortlink metrics.
  - Reason: Source-based breakdown is not useful for current needs.
  - Impacted files: `mojo/apps/shortlink/models/shortlink.py`, shortlink docs
- [x] Decision: Add user-scoped per-link analytics only when `track_clicks=True` and `link.user` exists.
  - Reason: Privacy/scope balance + feature need for owner analytics.
  - Impacted files: `mojo/apps/shortlink/models/shortlink.py`, shortlink + metrics docs, tests
- [x] Decision: Support explicit metric retention controls per `record()` call.
  - Reason: Need per-link metric expiry (`expires_at + 7 days`) or no-expiry for non-expiring links.
  - Impacted files: `mojo/apps/metrics/redis_metrics.py`, metrics docs
- [x] Decision: Enforce `user-<id>` metrics account security in REST permission checks.
  - Reason: Prevent cross-user metrics access.
  - Impacted files: `mojo/apps/metrics/rest/helpers.py`, `mojo/apps/metrics/rest/base.py`, metrics tests/docs

## In-Progress Work

- [x] Item: Add explicit group-account security API tests for metrics (`group-<id>`).
  - Status: Done.
  - Next step: User to run metrics test suite in project environment.
- [ ] Item: Final consistency pass across metrics docs account naming.
  - Status: Mostly done; verify no stale examples remain.
  - Next step: Search for `group_`/`group_<id>` and normalize to `group-<id>`.

## Open Questions

- [ ] Question: Keep custom `record(expires_at, disable_expiry)` API, or move retention control into separate cleanup job strategy?
  - Owner: ians

## Handoff Notes

- What changed:
  - Expanded shortlink bot UA detection for Apple Messages + major preview clients.
  - Added/expanded shortlink bot and metrics tests.
  - Updated shortlink docs for owner permissions and metrics retrieval (`global` + `user-<id>`).
  - Added frontend starter docs and auth token storage/reload guidance.
  - Unified metrics REST permission checks and added `user-<id>` enforcement.
- What is still pending:
  - Group-level metrics permission regression test coverage.
- What to verify in downstream Django project:
  - Shortlink OG HTML returns for newly-added preview UAs.
  - `bot_passthrough=True` always returns 302.
  - Metrics read/write permissions for `global`, `group-<id>`, `user-<id>`.
  - Shortlink per-link user metric retention behavior.

## Archive

- YYYY-MM-DD: brief completed summary
