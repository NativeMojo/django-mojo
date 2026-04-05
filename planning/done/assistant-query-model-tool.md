# Assistant Query Model Tool

**Type**: request
**Status**: planned
**Date**: 2026-04-05
**Priority**: high

## Description

Add `describe_model` and `query_model` tools to the assistant that let the LLM introspect and query any MojoModel in the system. The LLM can discover available fields, then build filtered queries — all enforced through the same RestMeta permission system the REST API uses. Supports JSON results via `to_dict()` and CSV export via `to_csv()`.

## Context

The assistant currently has domain-specific query tools (query_incidents, query_users, query_jobs, etc.) that each hand-build ORM filters and manually serialize results. A generic `query_model` tool would let the LLM answer ad-hoc questions across any model without needing a dedicated tool per domain. It reuses the existing RestMeta permission gates (`rest_check_permission`, `VIEW_PERMS`, owner/group filtering) so it never leaks data the user couldn't see via the REST API.

All models inherit `MojoModel` which provides `to_dict(graph)` and `to_csv(queryset, format)`. Models are discoverable via `django.apps.apps.get_model(app_label, model_name)`. Field metadata is available via `model._meta.get_fields()`.

## Acceptance Criteria

- `describe_model` tool: accepts `app_name` + `model_name`, returns field names/types, available graphs, RestMeta permissions info, and filterable fields
- `query_model` tool: accepts `app_name` + `model_name` + optional `filters` dict, `search` string, `ordering`, `limit`, `graph`, and `format` (json/csv)
- Permission enforcement: uses `rest_check_permission(request, "VIEW_PERMS")` — same as REST layer. Owner/group filtering applies automatically.
- Results use `to_dict(graph=graph)` for JSON, `to_csv(queryset, format)` for CSV
- Default limit of 50 results, max 200
- `count_only` option returns just the count (for "how many X?" questions)
- Sensitive fields excluded: passwords, auth_keys, onetime_codes, secrets
- Clean error on invalid app/model name, permission denied, bad filter field

## Investigation

**What exists**:
- `MojoModel.to_dict(graph)` — serializes instance to dict using RestMeta GRAPHS
- `MojoModel.to_csv(queryset, format, timezone)` — exports queryset to CSV/XLSX
- `MojoModel.rest_check_permission(request, permission_keys, instance)` — multi-level permission check (owner/group/user/api_key)
- `MojoModel.on_rest_list_filter(request, queryset)` — processes query params into ORM filters (supports `__in`, `__not`, `__isnull`, `__gte`, `__lte`, etc.)
- `django.apps.apps.get_model(app_label, model_name)` — resolves model class from names
- `model._meta.get_fields()` — returns all field objects with name, type, related info
- `MojoModel.get_rest_meta_prop(name)` — reads RestMeta properties
- `MojoModel.get_rest_meta_graph(names)` — reads graph definitions
- Existing tools in `services/tools/` use direct ORM — this tool would be more generic

**What changes**:
- `mojo/apps/assistant/services/tools/models.py` — **new file**: `describe_model` + `query_model` handlers + TOOLS list
- `mojo/apps/assistant/services/tools/__init__.py` — import and register the models domain

**Constraints**:
- Must build a synthetic request object for `rest_check_permission()` since tool handlers receive `(params, user)` not a Django request. The agent already has `SYSTEM_REQUEST` / `ACTIVE_REQUEST` patterns that could be reused.
- Filter keys must be validated against actual model fields to prevent ORM injection (e.g., `__password__icontains`). Whitelist filterable fields from `_meta.get_fields()`, excluding sensitive ones.
- `to_csv()` returns raw data — the tool would need to either return it as a downloadable link or as text content. For the assistant context, text/CSV string is more useful.
- Some models have `NO_REST = True` — these should be excluded from the tool.

**Related files**:
- `mojo/models/rest.py` — MojoModel, RestMeta, to_dict, to_csv, permission checking
- `mojo/apps/assistant/services/tools/discovery.py` — pattern reference (list_tools already introspects models)
- `mojo/apps/assistant/services/tools/__init__.py` — registration
- `mojo/apps/assistant/__init__.py` — register_tool API

## Example Interactions

**"How many users signed up this week?"**
→ `query_model(app="account", model="User", filters={"created__gte": "2026-03-29"}, count_only=true)`
→ `{"count": 47}`

**"Show me the 10 most recent failed jobs"**
→ `query_model(app="jobs", model="Job", filters={"status": "failed"}, ordering="-created", limit=10)`
→ `[{"id": 123, "func_name": "send_email", "status": "failed", ...}, ...]`

**"What fields does the Incident model have?"**
→ `describe_model(app="incident", model="Incident")`
→ `{"fields": [{"name": "status", "type": "CharField", ...}, ...], "graphs": ["default", "detail"], "permissions": {"view": ["security", "view_admin"], "save": ["security", "manage_incidents"]}}`

**"Export all active groups as CSV"**
→ `query_model(app="account", model="Group", filters={"is_active": true}, format="csv")`
→ `{"format": "csv", "content": "id,name,created\n1,Admins,2026-01-01\n...", "count": 12}`

## Tests Required

- Describe a known model and verify fields/graphs returned
- Query with valid filters and verify results match
- Query with `count_only=true` and verify count returned
- Query with `format="csv"` and verify CSV output
- Verify permission denied for model user can't access
- Verify NO_REST models are excluded
- Verify sensitive field names rejected in filters
- Verify limit cap (max 200)
- Verify invalid app/model returns clean error
- Verify owner filtering applies when RestMeta has `"owner"` in VIEW_PERMS

## Out of Scope

- Write/update/delete operations (read-only tool)
- Raw SQL queries
- Cross-model joins or aggregations beyond count
- Creating new models or modifying schema

## Plan

**Status**: planned
**Planned**: 2026-04-05

### Objective
Add `describe_model` and `query_model` tools that let the LLM introspect and query any MojoModel, reusing RestMeta permission gates and serialization, with security event reporting for denials and probes.

### Steps
1. `mojo/apps/assistant/services/tools/models.py` — New file with two handlers:
   - `_tool_describe_model`: resolve model via `apps.get_model`, validate MojoModel + not NO_REST, return fields (name/type/nullable/choices minus sensitive), graphs, VIEW_PERMS/SAVE_PERMS, SEARCH_FIELDS
   - `_tool_query_model`: build synthetic request via `objict(user=user, DATA=filters, method="GET")`, check `rest_check_permission(request, "VIEW_PERMS")`, apply owner/group filtering from VIEW_PERMS, validate filter keys against `_meta.get_fields()`, reject sensitive field filters, support `count_only`, `format="csv"` via `to_csv()`, JSON via `queryset_to_dict(graph)`, limit default 50 max 200
   - Security events via `incident.report_event()`: permission denied (category="assistant", level=5), sensitive field probe (category="assistant", level=7)
   - Audit logging via `logit` for all calls (model, user, result count)
   - `TOOLS` list: both with `permission="view_admin"`, `mutates=False`
2. `mojo/apps/assistant/services/tools/__init__.py` — Import `models`, register domain
3. `tests/test_assistant/7_test_model_tools.py` — Tests for describe, query, count_only, csv, permission denied, NO_REST exclusion, sensitive field rejection, limit cap, ordering, bad model, security event creation, registration
4. `docs/django_developer/assistant/README.md` — Add Models Domain tools table

### Design Decisions
- **Synthetic request via objict**: reuses full RestMeta permission + filter machinery
- **Double permission gate**: `view_admin` to call the tool + model's own `VIEW_PERMS`
- **Owner/group filtering from `on_rest_handle_list` logic**: replicate the owner/group auto-filter so assistant queries respect the same data boundaries as REST
- **Sensitive field substring check**: reject filter keys containing `password`, `auth_key`, `onetime_code`, `secret` — simple and effective
- **`incident.report_event()` for security events**: permission denied and sensitive field probes flow through the incident system (RuleSet matching → Incident creation), not just log files
- **`logit` for audit**: all calls logged at info level in `assistant.log` for operational visibility
- **No NO_REST models**: if excluded from REST, excluded from assistant

### Edge Cases
- Model not found: `LookupError` → clean error
- Non-MojoModel: check `hasattr(model, 'RestMeta')` → reject
- Empty queryset: return empty list, not error
- Bad filter field: validate against `_meta.get_fields()`, reject with error
- Bad ordering field: validate exists before applying

### Testing
- All scenarios → `tests/test_assistant/7_test_model_tools.py`

### Docs
- `docs/django_developer/assistant/README.md` — Models Domain tools table
