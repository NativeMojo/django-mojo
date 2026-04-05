# Assistant Read Framework Docs Tool

**Type**: request
**Status**: resolved
**Date**: 2026-04-05
**Priority**: medium

## Description

Add a `read_docs` tool to the assistant that fetches django-mojo framework documentation from the official GitHub repository. The LLM can look up how any framework feature works, check available settings, or find code examples — making it self-aware of the platform it's managing.

This tool builds on top of the `browse_url` tool (see `assistant-web-browse-tool.md`) but is specialized: it knows the docs base URL, can resolve doc paths, and presents documentation in a format optimized for LLM consumption.

## Context

The assistant manages a django-mojo application but currently has no knowledge of the framework's own documentation. When a user asks "how do I configure push notifications?" or "what settings control rate limiting?", the LLM can only guess based on code it's seen via other tools.

The framework docs live at:
- **Entry point**: `https://raw.githubusercontent.com/NativeMojo/django-mojo/refs/heads/main/docs/README.md`
- **Django developer docs**: `docs/django_developer/` — models, settings, services, architecture
- **Web developer docs**: `docs/web_developer/` — REST endpoints, request/response format, permissions

The README serves as an index with links to all doc sections. The LLM can start there and drill into specific topics.

## Acceptance Criteria

- Tool `read_docs` accepts: `path` (relative doc path, e.g., `django_developer/assistant/README.md`) or `topic` (free-text, e.g., "push notifications")
- If `path` provided: fetches that specific doc from the GitHub raw URL
- If `topic` provided: fetches the root README, identifies the relevant doc path from the index, then fetches that doc
- Always fetches from `https://raw.githubusercontent.com/NativeMojo/django-mojo/refs/heads/main/docs/`
- Returns: `{"path": "...", "content": "...", "truncated": bool}`
- Content is already markdown (no HTML stripping needed for raw GitHub URLs)
- Truncates at ~20K chars with continuation hint
- No special permissions needed beyond base assistant access (docs are public)
- Depends on `browse_url` tool being available (or uses `requests` directly)

## Investigation

**What exists**:
- `browse_url` tool (planned, see `assistant-web-browse-tool.md`) — generic web fetching
- `requests` library already in dependencies
- GitHub raw URLs return plain markdown — no HTML parsing needed
- Docs index at `docs/README.md` has a structured list of all sections with relative paths

**What changes**:
- `mojo/apps/assistant/services/tools/docs.py` — **new file**: read_docs handler + TOOLS list
- `mojo/apps/assistant/services/tools/__init__.py` — import and register

**Constraints**:
- Raw GitHub URLs are rate-limited (60 req/hr unauthenticated, 5000/hr with token). For a single assistant conversation this is fine, but if many users are hitting it, may want to cache responses briefly (5-10 min).
- Some doc files reference relative links/images that won't resolve. The tool should note this but it's not a blocker for text content.
- Topic-based lookup is a two-step fetch (index → specific doc). Should handle gracefully if topic not found in index.

**Related files**:
- `planning/requests/assistant-web-browse-tool.md` — browse_url dependency
- `mojo/apps/assistant/services/tools/__init__.py` — registration
- `docs/README.md` — the index the tool reads

## Settings (if applicable)

| Setting | Default | Purpose |
|---|---|---|
| `LLM_DOCS_BASE_URL` | `https://raw.githubusercontent.com/NativeMojo/django-mojo/refs/heads/main/docs/` | Base URL for fetching docs |
| `LLM_DOCS_CACHE_TTL` | 300 | Cache TTL in seconds for fetched docs (0 to disable) |

## Example Interactions

**"How do I set up push notifications?"**
→ LLM calls `read_docs(topic="push notifications")`
→ Fetches README index → finds `django_developer/account/push.md` → fetches and returns content
→ LLM summarizes the setup steps for the user

**"What settings does the bouncer use?"**
→ `read_docs(path="django_developer/account/bouncer.md")`
→ Returns the full bouncer docs with settings table

**"What REST endpoints are available for incidents?"**
→ `read_docs(topic="incidents")`
→ Fetches `web_developer/security/incidents.md` (or similar)

## Tests Required

- Fetch a known doc path and verify content returned
- Fetch with topic and verify correct doc resolved from index
- Verify unknown topic returns helpful "not found" message
- Verify truncation at max length
- Verify base URL is configurable via settings
- Verify graceful handling of network errors (GitHub down)

## Out of Scope

- Writing or updating documentation
- Fetching non-documentation content from GitHub (source code, issues, PRs)
- Offline/bundled docs (always fetches live from GitHub)
- Multi-page crawling (fetches one doc at a time)

## Plan

**Status**: planned
**Planned**: 2026-04-05

### Objective
Add a `read_docs` tool that fetches django-mojo framework docs from GitHub raw URLs, with direct path access and topic-based index lookup.

### Steps
1. `mojo/apps/assistant/services/tools/docs.py` — New file: `_tool_read_docs` handler with path fetch, topic-based index lookup, path validation, truncation. `TOOLS` list with `permission="view_admin"`.
2. `mojo/apps/assistant/services/tools/__init__.py` — Import `docs`, register domain
3. `tests/test_assistant/6_test_docs_tools.py` — Tests for path fetch, topic lookup, unknown topic, 404, truncation, path traversal rejection, registration
4. `docs/django_developer/assistant/README.md` — Add Docs Domain tools table

### Design Decisions
- **No caching**: raw GitHub serves fine for single-user conversations; add later if needed
- **Topic lookup via keyword matching on README index**: simple substring match on section names/descriptions, no embeddings
- **Use `requests` directly, not `browse_url`**: docs are markdown from trusted host, no SSRF/HTML needed
- **Path validation**: reject `..`, absolute URLs, anything outside docs/

### Edge Cases
- Topic matches multiple: return first match, note others
- Topic not found: return index content so LLM can browse manually
- GitHub rate limit (403): clean error
- Path not found (404): error with suggestion to use topic
- Leading `docs/` in path: strip automatically

### Testing
- All scenarios → `tests/test_assistant/6_test_docs_tools.py`

### Docs
- `docs/django_developer/assistant/README.md` — Docs Domain tools table

## Resolution

**Status**: resolved
**Date**: 2026-04-05

### What Was Built
`read_docs` assistant tool that fetches django-mojo framework documentation from GitHub raw URLs. Supports direct path access and topic-based keyword search that scans the doc indexes. Includes SSRF validation on base URL, path traversal protection on both user input and index-extracted paths, and sanitized error messages.

### Files Changed
- `mojo/apps/assistant/services/tools/docs.py` — New file: read_docs handler, index search, path validation
- `mojo/apps/assistant/services/tools/__init__.py` — Register docs domain
- `docs/django_developer/assistant/README.md` — Docs Domain table, settings, test file

### Tests
- `tests/test_assistant/6_test_docs_tools.py` — 19 tests (input validation, path fetch, topic search, truncation, index parsing, security hardening)
- Run: `bin/run_tests --agent -t test_assistant`

### Security Review
- Added SSRF validation on LLM_DOCS_BASE_URL (must be https, public host)
- Filtered index-extracted link paths: reject .., //, /, http prefixes
- Run _normalize_path on topic-resolved paths before fetching
- Sanitized 404 error messages (no base URL leak)
- Removed raw topic reflection from "not found" note

### Follow-up
- None
