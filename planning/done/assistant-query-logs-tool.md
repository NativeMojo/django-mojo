# Assistant Query Logs Tool

**Type**: request
**Status**: resolved
**Date**: 2026-04-05
**Priority**: high

## Description

Add a `query_logs` tool to the assistant that queries the `logit.Log` model — the central audit trail for all request/response logging, model changes, API errors, and custom application events. Supports filtering by time range, level, kind, model_name, model_id, user, IP, and free-text search.

## Context

The `logit.Log` model is the single audit trail for the entire framework. Every HTTP request/response (via LoggerMiddleware), every model create/update (via `LOG_CHANGES`), every API error, and every custom `Log.logit()` call lands here. The assistant can already query incidents, jobs, and users — but can't see the raw logs that connect them. This tool bridges that gap.

Key `Log` fields the LLM can filter on:
- `level` — info, warn, error, debug
- `kind` — log category: "request", "response", "api_error", "model:created", "model:changed", custom kinds
- `model_name` — target model (e.g., "account.User", "incident.Incident")
- `model_id` — target model instance ID
- `uid` — user ID who triggered the event
- `ip` — client IP address
- `path` — request path
- `method` — HTTP method (GET, POST, etc.)

The composite index on `(model_name, model_id)` makes "show me everything that happened to this record" queries fast.

## Acceptance Criteria

- Tool `query_logs` accepts: `minutes` (time window, default 60), `level`, `kind`, `model_name`, `model_id`, `uid`, `ip`, `path`, `method`, `search` (free-text in log content), `limit` (default 50, max 200)
- Returns structured results: `[{"id", "created", "level", "kind", "method", "path", "ip", "uid", "username", "model_name", "model_id", "log"}, ...]`
- Excludes full `payload` and `user_agent` by default (verbose), includes them with `verbose=true`
- Permission-gated: requires `view_logs` or `security` or `admin` permission (matches Log's RestMeta VIEW_PERMS)
- Time-bounded: always requires a time window, max 7 days (10080 minutes)
- Results ordered by `-created` (newest first)
- `count_only` option for "how many errors in the last hour?"

## Investigation

**What exists**:
- `mojo/apps/logit/models/log.py` — Log model with all fields, indexes on `created`, `level`, `kind`, `path`, `ip`, `uid`, `(model_name, model_id)`, `(created, kind)`
- `Log.logit(request, log, kind, model_name, model_id, level)` — creation API
- `MojoModel.log()` / `class_logit()` — convenience methods that auto-set model_name
- `mojo/middleware/logging.py` — LoggerMiddleware auto-logs requests/responses
- RestMeta on Log: `VIEW_PERMS = ["manage_logs", "view_logs", "security", "admin"]`
- REST endpoint already exists: `GET /api/logit/logs` with filtering
- Log model has `to_dict()` via MojoModel

**What changes**:
- `mojo/apps/assistant/services/tools/logs.py` — **new file**: query_logs handler + TOOLS list
- `mojo/apps/assistant/services/tools/__init__.py` — import and register

**Constraints**:
- Log table can be very large (every request is logged when `LOGIT_DB_ALL=True`). Must enforce time window and limit.
- `log` field can contain large JSON payloads. Default to truncating at ~500 chars with a `truncated` flag; full content with `verbose=true`.
- `payload` field can be very large (request/response bodies). Exclude by default.

**Related files**:
- `mojo/apps/logit/models/log.py` — Log model
- `mojo/apps/logit/rest.py` — existing REST endpoint (pattern reference)
- `mojo/middleware/logging.py` — LoggerMiddleware (creates the logs)
- `mojo/models/rest.py` — MojoModel.log(), class_logit()

## Example Interactions

**"What happened to user 42 in the last hour?"**
→ `query_logs(uid=42, minutes=60)`
→ `[{"kind": "request", "method": "POST", "path": "/api/account/user", ...}, {"kind": "model:changed", "model_name": "account.User", "model_id": 42, "log": "{email: {old: ..., new: ...}}", ...}]`

**"Show me all errors in the last 24 hours"**
→ `query_logs(level="error", minutes=1440)`
→ `[{"kind": "api_error", "path": "/api/jobs/queue", "log": "Connection refused to Redis", ...}, ...]`

**"Any activity from IP 203.0.113.50?"**
→ `query_logs(ip="203.0.113.50", minutes=10080)`
→ `[{"kind": "request", "method": "POST", "path": "/api/account/login", "uid": 0, ...}, ...]`

**"Show me all changes to incident 789"**
→ `query_logs(model_name="incident.Incident", model_id=789)`
→ `[{"kind": "model:changed", "log": "{status: {old: 'open', new: 'resolved'}}", ...}, {"kind": "model:created", ...}]`

**"How many API errors today?"**
→ `query_logs(kind="api_error", minutes=1440, count_only=true)`
→ `{"count": 23}`

## Tests Required

- Query logs with time range filter and verify results bounded
- Query by level, kind, model_name, model_id and verify correct filtering
- Query by uid and verify user-scoped results
- Query by IP and verify IP-scoped results
- Verify free-text search in log content
- Verify permission gate (user without view_logs denied)
- Verify time window cap (>10080 minutes rejected)
- Verify limit cap (max 200)
- Verify count_only returns count
- Verify verbose=true includes payload and user_agent
- Verify log content truncation in non-verbose mode

## Out of Scope

- Writing/creating log entries (read-only)
- Log deletion or pruning
- Real-time log streaming (that would be a WebSocket feature)
- Log aggregation or analytics beyond count

## Resolution

**Status**: resolved
**Date**: 2026-04-05

### What Was Built
query_logs assistant tool for querying the logit.Log audit trail with time-bounded, filtered queries.

### Files Changed
- `mojo/apps/assistant/services/tools/logs.py` — New tool handler with filters, truncation, verbose mode, payload/user_agent masking
- `mojo/apps/assistant/services/tools/__init__.py` — Registered logs domain

### Tests
- `tests/test_assistant/8_test_log_tools.py` — 23 tests covering all filters, truncation, verbose mode, payload masking, permission gate, limits, registration
- Run: `bin/run_tests -t test_assistant.8_test_log_tools`

### Security Review
- Payload and user_agent masked via `logit.mask_sensitive_data()` before returning in verbose mode
- Limit validation: zero/negative falls back to DEFAULT_LIMIT
- Time window capped at 7 days

### Follow-up
- None
