# Assistant File Export Tool

**Type**: request
**Status**: resolved
**Date**: 2026-04-13
**Priority**: high

## Description

Add an `export_data` tool to the AI assistant that writes query results (CSV, and potentially other formats) directly to a `fileman.File` record, then returns the download URL to the user instead of dumping raw data into the conversation. This keeps large datasets out of the LLM context (saving tokens and avoiding context limits) while giving users a clickable download link.

## Context

The `query_model` tool already supports `format="csv"` but returns the CSV content inline in the tool result, which gets fed back into the LLM. For large exports this is wasteful (token cost) and can hit context limits. The infrastructure to solve this already exists: `Model.to_csv()` produces CSV strings, and `fileman.File` can store files in S3/filesystem with pre-signed download URLs.

The assistant should generate the file, store it via fileman, and respond with a download link — possibly using a new structured block type so the frontend can render a proper download button.

## Acceptance Criteria

- New `export_data` assistant tool that:
  - Accepts model, filters, ordering, fields/graph, format, and row limit
  - Runs the query and serializes to CSV (reusing `Model.to_csv()`)
  - Writes the result to a `fileman.File` record (associated with user/group)
  - Returns the download URL (not the file content) in the tool result
- Assistant responds with a download link the user can click
- New `file` structured block type so the frontend can render a download button with filename, size, and format
- Works with any MojoModel that has RestMeta configured
- Respects existing model permissions (same permission checks as `query_model`)
- File is stored with proper metadata (filename like `export_users_2026-04-13.csv`, content_type `text/csv`, category `csv`)

## Investigation

**What exists**:
- `query_model` tool (`mojo/apps/assistant/services/tools/models.py:431`) — already does CSV export inline
- `Model.to_csv()` (`mojo/models/rest.py:1195`) — returns raw CSV string via `CsvFormatter`
- `CsvFormatter` (`mojo/serializers/formats/csv.py`) — full-featured CSV serializer with field mapping, streaming, localization
- `File.create_from_file()` (`mojo/apps/fileman/models/file.py:408`) — creates File record from file object, uploads to backend
- `File.generate_download_url()` (`mojo/apps/fileman/models/file.py:299`) — returns public or pre-signed URL
- `FileManager.get_for_user_group()` — resolves the correct storage backend for a user/group
- Structured blocks system (`mojo/apps/assistant/services/agent.py:271`) — supports table, chart, stat, action, list, alert types

**What changes**:
- `mojo/apps/assistant/services/tools/models.py` — new `export_data` tool (or extend `query_model` with a `save_to_file` param)
- `mojo/apps/assistant/services/agent.py` — add `file` block type to system prompt and block validation
- Frontend — render `file` block with download button (out of scope for django-mojo, but block schema should be defined)

**Constraints**:
- Must handle the case where no `FileManager` is configured for the user/group (fail with clear error)
- File size should be bounded — enforce a row limit (e.g., 50,000 rows max)
- Pre-signed URLs expire — the download link has a limited lifetime
- Permission model must match `query_model` — user needs model VIEW_PERMS + `view_admin`
- `File.create_from_file()` expects a file-like object with `.name`, `.size`, `.content_type` — need to wrap the CSV StringIO accordingly

**Related files**:
- `mojo/apps/assistant/services/tools/models.py`
- `mojo/apps/assistant/services/agent.py`
- `mojo/apps/assistant/__init__.py` (tool registry)
- `mojo/apps/fileman/models/file.py`
- `mojo/apps/fileman/models/manager.py`
- `mojo/models/rest.py` (to_csv)
- `mojo/serializers/formats/csv.py`
- `mojo/apps/shortlink/__init__.py` (shorten helper)

## Design Decisions Needed

1. **Separate tool vs. parameter on `query_model`?** — A new `export_data` tool keeps concerns separate and gives the LLM a clear signal for when to use file export vs. inline data. A `save_to_file` flag on `query_model` is simpler but muddies the tool's purpose. Recommendation: separate tool.

2. **New `file` block type vs. inline markdown link?** — A structured block gives the frontend control over rendering (download button, file icon, size display). A markdown link is simpler but less polished. Recommendation: new `file` block type with schema `{"type": "file", "filename": "...", "url": "...", "size": 12345, "format": "csv"}`.

3. **Custom field selection** — Should the user be able to pick specific fields, or just use the model's configured graphs? The CSV serializer already supports custom field lists via `FORMATS` config. Recommendation: support an optional `fields` list param, falling back to the model's default graph.

4. **File ownership** — Files should be owned by the requesting user and associated with their group (if any), using the standard `FileManager.get_for_user_group()` resolution.

## Structured Block Schema

```json
{
  "type": "file",
  "filename": "export_users_2026-04-13.csv",
  "url": "https://s3.../export_users_2026-04-13.csv?...",
  "size": 45230,
  "format": "csv",
  "row_count": 1250
}
```

## Tests Required

- Export a model to CSV file, verify File record created with correct metadata
- Verify download URL is returned (not CSV content)
- Verify file content matches expected CSV output
- Permission denied when user lacks model VIEW_PERMS
- Graceful error when no FileManager configured
- Row limit enforcement
- Custom field selection
- File block parsing and validation in agent block extractor

## Out of Scope

- Frontend rendering of the `file` block type (django-mojo is backend only — but block schema is defined here for frontend team)
- Formats beyond CSV (Excel, PDF — can be added later using same pattern)
- Scheduled/recurring exports
- Export progress tracking for very large datasets
- Streaming exports (current `to_csv` with `raw_data=True` buffers in memory, which is fine for bounded row limits)

## Plan

**Status**: resolved
**Planned**: 2026-04-13

### Objective

Add three capabilities to the AI assistant: (1) `export_data` tool that writes query results to fileman.File and returns a download URL instead of inline data, (2) `aggregate_model` tool for Django ORM aggregate/group-by queries so summaries never require fetching rows, and (3) a cleanup job for expired export files. Remove the inline CSV path from `query_model`.

### Steps

#### Step 1: `aggregate_model` tool
`mojo/apps/assistant/services/tools/models.py` — Add new tool after `query_model`.

Reuses existing helpers: `_resolve_model`, `_build_request`, `_apply_owner_group_filter`, `_validate_filter_keys`.

```
@tool(
    name="aggregate_model",
    domain="models",
    permission="view_admin",
    core=True,
    description="Run aggregate queries (count, sum, avg, min, max) on any model. "
                "Supports group_by for grouped results. Use this for summaries — "
                "never pull rows just to count or sum them.",
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {"type": "string"},
            "model_name": {"type": "string"},
            "filters": {"type": "object", "description": "ORM filters"},
            "aggregations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": "Field to aggregate"},
                        "func": {
                            "type": "string",
                            "enum": ["count", "sum", "avg", "min", "max", "count_distinct"],
                        },
                        "alias": {"type": "string", "description": "Result key name (optional)"},
                    },
                    "required": ["field", "func"],
                },
                "description": "List of aggregations to compute",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Fields to group by (e.g. ['status'] or ['status', 'category'])",
            },
            "ordering": {
                "type": "string",
                "description": "Order grouped results (e.g. '-total' or 'status')",
            },
            "limit": {
                "type": "integer",
                "description": "Max grouped rows (default 50, max 200)",
            },
        },
        "required": ["app_name", "model_name", "aggregations"],
    },
)
```

Implementation logic:
- Validate model, permissions, filter keys (same as `query_model`)
- Validate `aggregations[].field` against model fields, reject sensitive fields
- Validate `group_by` fields against model fields, reject sensitive fields
- Build Django ORM aggregates using `django.db.models` functions:
  - `count` → `Count(field)`, `count_distinct` → `Count(field, distinct=True)`
  - `sum` → `Sum(field)`, `avg` → `Avg(field)`, `min` → `Min(field)`, `max` → `Max(field)`
- If `group_by` present: `queryset.values(*group_by).annotate(**aggs).order_by(ordering)[:limit]`
- If no `group_by`: `queryset.aggregate(**aggs)` → single dict
- Auto-generate alias as `{func}_{field}` when not provided
- Return: `{"model": "...", "results": [...], "group_by": [...]}` or `{"model": "...", "results": {...}}` for flat

#### Step 2: `export_data` tool
`mojo/apps/assistant/services/tools/models.py` — Add new tool.

```
@tool(
    name="export_data",
    domain="models",
    permission="view_admin",
    core=True,
    description="Export query results to a downloadable CSV file. "
                "Data is written directly to file storage (S3) — NOT returned inline. "
                "Returns a download URL for the user. Use for any export of 10+ rows. "
                "Use aggregate_model for summaries (counts, sums, averages) instead.",
    input_schema={
        "type": "object",
        "properties": {
            "app_name": {"type": "string"},
            "model_name": {"type": "string"},
            "filters": {"type": "object"},
            "search": {"type": "string"},
            "ordering": {"type": "string"},
            "limit": {"type": "integer", "description": "Max rows (default 5000, max 50000)"},
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific fields to include (optional, defaults to model's graph)",
            },
            "graph": {"type": "string", "description": "Serialization graph name"},
        },
        "required": ["app_name", "model_name"],
    },
    mutates=True,
)
```

Implementation logic:
- Same model resolution, permission checks, filter validation, owner/group filtering as `query_model`
- Higher limits: `DEFAULT_EXPORT_LIMIT = 5000`, `MAX_EXPORT_LIMIT = 50000`
- If `fields` param provided, pass directly to `CsvFormatter.serialize_queryset(fields=fields)`
- Otherwise fall through to `Model.to_csv()` which uses FORMATS or graph config
- Get `FileManager` via `FileManager.get_for_user_group(user=user, group=user.membership.group if available)`
- If no FileManager found, return `{"error": "No file storage configured. Contact your administrator."}`
- Build a file-like wrapper around the CSV string:
  ```python
  csv_data = model.to_csv(queryset[:limit], format="csv")
  csv_bytes = csv_data.encode("utf-8")
  file_obj = io.BytesIO(csv_bytes)
  file_obj.name = f"export_{app_name}_{model_name}_{date_str}.csv"
  file_obj.size = len(csv_bytes)
  file_obj.content_type = "text/csv"
  ```
- Create File record via `File.create_from_file(file_obj, file_obj.name, user=user, group=group, file_manager=fm)`
- Set metadata: `file_instance.metadata = {"source": "assistant_export", "model": model_label, "row_count": count, "expires_at": (now + 14 days).isoformat()}`
- Save and generate download URL
- **Shortlink integration**: If `mojo.apps.shortlink` is installed, wrap the download URL in a shortlink for cleaner sharing:
  ```python
  from mojo.apps.shortlink import shorten
  url = shorten(
      file=file_instance,
      source="assistant_export",
      expire_days=settings.get("FILEMAN_EXPORT_EXPIRES_DAYS", 14),
      resolve_file=True,   # dynamically resolves fresh download URL on each click
      user=user,
      group=group,
  )
  ```
  `resolve_file=True` means every click generates a fresh pre-signed URL from the File record — so even if the S3 pre-signed URL expires (default 3600s), the shortlink always works for the full 14-day file lifetime. Falls back to raw `file.generate_download_url()` if shortlink app not installed.
- Return: `{"url": url, "filename": filename, "size": len(csv_bytes), "row_count": count, "model": model_label, "expires_in": "14 days"}`
- The LLM uses this to render a `file` block in the response

#### Step 3: Remove inline CSV from `query_model`
`mojo/apps/assistant/services/tools/models.py` lines 328-330, 431-443 — Remove the `format` param from schema and the `if fmt == "csv":` branch. Update tool description to direct LLM to `export_data` for CSV.

#### Step 4: `file` block type
`mojo/apps/assistant/services/agent.py` — Three changes:

1. **Line 46**: Add `"file"` to `VALID_BLOCK_TYPES` set
2. **`_validate_block` function** (~line 51): Add validation for `file` blocks — require `url` and `filename` keys
3. **System prompt block docs** (~line 271): Add `file` block documentation:

```
**file** — for downloadable files generated by tools (CSV exports, reports):
```assistant_block
{"type": "file", "filename": "export_users_2026-04-13.csv", "url": "https://...", "size": 45230, "format": "csv", "row_count": 1250, "expires_in": "14 days"}
```
Use when a tool generates a downloadable file. The frontend renders this as a download card with filename, size, format icon, and download button. Include all fields returned by the export tool. Never fabricate URLs — only use URLs returned by export_data.
```

**Frontend contract for `file` block**:
| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | Always `"file"` |
| `filename` | string | yes | Display filename (e.g. `export_users_2026-04-13.csv`) |
| `url` | string | yes | Download URL (may be pre-signed, time-limited) |
| `size` | integer | no | File size in bytes |
| `format` | string | no | File format (`csv`, future: `xlsx`, `pdf`) |
| `row_count` | integer | no | Number of data rows in the file |
| `expires_in` | string | no | Human-readable expiry (e.g. `"24 hours"`) |

Frontend should render as a card/chip with: file icon (based on `format`), filename, human-readable size, row count if present, download button linking to `url`, and a subtle expiry note.

#### Step 5: Update system prompt guidance
`mojo/apps/assistant/services/agent.py` — In the tool loading / general guidance section (~line 220-230), add:

```
## Data Strategy
- **Summaries**: Use `aggregate_model` for counts, sums, averages, group-by breakdowns. Never pull rows just to count or summarize them.
- **Exports**: Use `export_data` when users want rows as a file. Data goes directly to storage — never returned through you. Present the download URL using a file block.
- **Inline data**: Use `query_model` only when you need to inspect specific records (small result sets, detail lookups). Keep limits low (10-50 rows).
- **Never return raw CSV**: If you find yourself looking at CSV data, something went wrong. Use export_data instead.
```

#### Step 6: Cleanup job for expired export files
`mojo/apps/fileman/jobs.py` (new file) — Register a job function that:
- Queries `File.objects.filter(metadata__source="assistant_export", metadata__expires_at__lt=now_iso)`
- For each file: calls `file.on_rest_pre_delete()` (deletes from storage), then `file.delete()`
- Logs count of cleaned files
- Job function signature: `def cleanup_expired_exports(job):`

The cleanup should also support a broader `metadata__expires_at` check (not just `source=assistant_export`) so other features can use the same expiration pattern. Filter logic:
```python
expired = File.objects.filter(
    metadata__expires_at__lt=now.isoformat(),
    is_active=True,
)
```
This catches any file with an `expires_at` in metadata, regardless of source. Log the `metadata.source` for each deleted file for audit trail.

The job should be published on a schedule by the project — document that projects should add a ScheduledTask or cron entry:
```python
from mojo.apps.jobs import publish
publish("mojo.apps.fileman.jobs.cleanup_expired_exports", {})
```
Default expiration is 14 days. Recommend daily cleanup schedule. Document a setting `FILEMAN_EXPORT_EXPIRES_DAYS` (default 14) so projects can tune it.

#### Step 7: Update `query_model` description
Update the `query_model` tool description to explicitly say:
- "For CSV exports, use export_data instead."  
- "For aggregate queries (count, sum, avg), use aggregate_model instead."
- Reduce `MAX_LIMIT` comment in description to emphasize this is for small inline result sets.

### Design Decisions

- **Separate tools (not flags)**: `export_data`, `aggregate_model`, and `query_model` are three distinct tools. Clear separation guides the LLM to pick the right one. Tool descriptions form a decision tree: summaries → `aggregate_model`, exports → `export_data`, inspection → `query_model`.
- **`export_data` is `core=True`**: Always available, like `query_model`. The LLM needs to know about it from the first turn to avoid defaulting to inline data dumps.
- **`aggregate_model` is `core=True`**: Same reasoning — always available for summary questions.
- **`mutates=True` on `export_data`**: Creates a File record. LLM will note this is a side-effect operation.
- **Expiration via `metadata` JSONField**: The `upload_expires_at` field was removed in migration 0003. Using `metadata.expires_at` is flexible and doesn't require a schema change. Any file with `metadata.expires_at` gets cleaned up — not just assistant exports.
- **`metadata.source = "assistant_export"`**: Allows filtering/auditing of LLM-generated files separately from user uploads.
- **Custom `fields` param on `export_data`**: Users may ask "export just email and name" — the `fields` array passes directly to `CsvFormatter`. Falls back to model graph config when omitted.
- **Remove inline CSV from `query_model`**: Eliminates the token-wasteful path entirely. No ambiguity about which tool to use.
- **group_by in v1 for `aggregate_model`**: Essential for real analytics ("incidents by category", "users by role"). Uses Django's `values().annotate()` pattern — well-tested ORM path.
- **Block type `file`**: Gives frontend full control over rendering. Schema is simple and extensible for future formats.
- **Shortlink wrapping (if installed)**: Uses `shorten(file=file_instance, resolve_file=True)` so each click resolves a fresh download URL. This solves two problems: (1) cleaner URLs for sharing in chat vs raw S3 pre-signed URLs, and (2) pre-signed URL expiry is no longer an issue since each click generates a new one. Gracefully falls back to raw download URL if shortlink app not installed. Shortlink expires at the same time as the file (FILEMAN_EXPORT_EXPIRES_DAYS).

### Edge Cases

- **No FileManager configured**: `export_data` returns a clear error: "No file storage configured." The LLM can relay this to the user.
- **Empty queryset**: `export_data` should still create a CSV with headers only. Return `row_count: 0` so the LLM can tell the user "the export is empty but here's the file."
- **Very large exports**: Capped at 50,000 rows. `to_csv` buffers in memory via StringIO — at 50K rows × ~500 bytes/row that's ~25MB, acceptable. If we need larger, streaming can be added later.
- **Pre-signed URL expiry vs file expiry**: S3 pre-signed URLs expire based on FileManager settings (default 3600s), but the file itself persists for 14 days. With shortlink + `resolve_file=True`, each click dynamically generates a fresh pre-signed URL — so the short URL just works for the full 14-day lifetime. Without shortlink, raw pre-signed URLs may expire and the LLM can re-generate one by querying the File record.
- **Aggregate on non-numeric fields**: `Sum`/`Avg` on string fields will raise a Django error — catch and return `{"error": "Cannot compute sum/avg on non-numeric field 'name'"}`. Validate field type before building the aggregate.
- **group_by cardinality**: If `group_by` produces thousands of groups, limit to 200 rows and include `"truncated": true` in the result.
- **Sensitive field in aggregations/group_by**: Reject any sensitive field in `aggregations[].field` or `group_by` — same `_is_sensitive_field` check.
- **FileManager resolution for assistant**: The user making the request may not have a group context in the assistant. Use `user.membership.group` (primary group) as fallback. If no FileManager at all, error clearly.
- **Concurrent exports**: No issue — each export creates a unique File with UUID-based storage filename.

### Testing

- `aggregate_model` flat aggregates (sum, avg, count, min, max) → `tests/test_assistant/test_aggregate_model.py`
- `aggregate_model` with group_by and ordering → same file
- `aggregate_model` count_distinct → same file
- `aggregate_model` rejects sensitive fields in aggregations and group_by → same file
- `aggregate_model` permission denied → same file
- `aggregate_model` non-numeric field with sum/avg → same file
- `export_data` creates File with correct metadata → `tests/test_assistant/test_export_data.py`
- `export_data` returns URL, not CSV content → same file
- `export_data` custom fields param → same file
- `export_data` permission denied → same file
- `export_data` no FileManager configured → same file
- `export_data` empty queryset → same file
- `export_data` row limit enforcement → same file
- `export_data` metadata has `source` and `expires_at` → same file
- `query_model` no longer accepts `format=csv` → `tests/test_assistant/test_query_model.py` (update existing)
- `file` block validation (valid and invalid) → `tests/test_assistant/test_blocks.py`
- Cleanup job deletes expired files → `tests/test_fileman/test_cleanup_exports.py`
- Cleanup job ignores non-expired files → same file

### Docs

- `docs/django_developer/assistant/README.md` — Add `export_data` and `aggregate_model` tool docs, data strategy guidance, file block schema
- `docs/web_developer/assistant/README.md` — Add `file` block type to block schema reference for frontend team (rendering contract)
- `docs/django_developer/files/README.md` — Document `metadata.expires_at` expiration pattern and cleanup job
- `CHANGELOG.md` — New assistant tools: `export_data`, `aggregate_model`; new `file` block type; removed inline CSV from `query_model`; export file cleanup job
