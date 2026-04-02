# Markdown Render Endpoint

**Type**: request
**Status**: resolved
**Date**: 2026-04-02
**Priority**: medium

## Description
Add a standalone REST endpoint that accepts raw markdown text and returns rendered HTML. This avoids requiring consumers to create/save a Page just to preview or render markdown content.

## Context
The docit app already has a `MarkdownRenderer` service (`mojo/apps/docit/services/markdown.py`) that converts markdown to HTML with syntax highlighting via Pygments. Currently the only way to get rendered HTML is through the Page model's `html` property (accessed via `?graph=html` on the page endpoint). A lightweight render endpoint unlocks live previews, markdown-powered UI components, and any workflow that needs HTML without persisting a document.

## Acceptance Criteria
- POST endpoint accepts `{"markdown": "# Hello"}` and returns `{"html": "<h1>Hello</h1>\n"}`
- Works for any authenticated user (no special permissions beyond login)
- Returns 400 if `markdown` field is missing or empty
- Handles invalid/malformed markdown gracefully (mistune is lenient, so this is mostly free)
- No data is persisted — pure stateless transform

## Investigation
**What exists**:
- `MarkdownRenderer` class in `mojo/apps/docit/services/markdown.py` — singleton pattern, `render(text)` returns HTML
- `HighlightRenderer` for fenced code blocks with Pygments (monokai style)
- Plugins architecture exists but plugins are currently commented out (table, url, task_list, footnotes, etc.)
- Docit REST layer already has endpoints for Book, Page, PageRevision, Asset

**What changes**:
- `mojo/apps/docit/rest/` — add a new handler file for the render endpoint (or add to an existing utility handler if one exists)

**Constraints**:
- Must require authentication — unauthenticated markdown rendering could be abused
- Input size should have a reasonable limit to prevent abuse (large payloads eating CPU on Pygments highlighting)
- `escape=False` in the renderer means raw HTML in markdown passes through — this is existing behavior and the consumer is responsible for sanitization if displaying user-generated content

**Related files**:
- `mojo/apps/docit/services/markdown.py` — the renderer (no changes needed)
- `mojo/apps/docit/rest/` — new endpoint lives here

## Endpoints
| Method | Path | Description | Permission |
|---|---|---|---|
| POST | `/api/docit/render` | Render markdown to HTML | `@md.requires_auth()` |

## Tests Required
- POST with valid markdown returns correct HTML
- POST without `markdown` field returns 400
- POST with empty string returns 400
- Unauthenticated request returns 401
- Code blocks render with syntax highlighting
- Large markdown input renders successfully

## Out of Scope
- Enabling the commented-out mistune plugins (table, footnotes, etc.) — separate decision
- HTML sanitization / XSS filtering on output — consumer responsibility, same as existing Page.html behavior
- Rate limiting — can be added later if abuse is observed
- GET method support — POST-only since the input can be large

## Plan

**Status**: planned
**Planned**: 2026-04-02

### Objective
Add a stateless `POST /api/docit/render` endpoint and fix the existing `HighlightRenderer` crash on invalid language names.

### Steps
1. `mojo/apps/docit/services/markdown.py` — Wrap `get_lexer_by_name` in a try/except for `pygments.util.ClassNotFound`. On invalid language, fall back to plain escaped `<pre><code>` block (same as the `not info` branch).
2. `mojo/apps/docit/rest/render.py` (new) — Add render endpoint:
   - `@md.URL('render')` + `@md.requires_auth()`
   - Read `markdown` from `request.DATA`, validate present and non-empty (return 400 otherwise)
   - Instantiate `MarkdownRenderer`, call `render()`, return `{"html": result}`
3. `mojo/apps/docit/rest/__init__.py` — Add `from .render import *`
4. `tests/test_docit/docit_core.py` — Add tests:
   - Valid markdown renders correct HTML
   - Missing `markdown` field returns 400
   - Empty string returns 400
   - Code block with valid language renders with syntax highlighting
   - Code block with invalid language renders gracefully (no crash)
   - Unauthenticated request returns 401
5. `docs/django_developer/docit/README.md` — Add Render Endpoint section
6. `docs/web_developer/` — Add docit render endpoint docs for API consumers

### Design Decisions
- `@md.requires_auth()` not `@md.uses_model_security`: this is a utility endpoint, not a RestMeta model endpoint — any authenticated user can use it
- POST-only: markdown content can be large, doesn't fit GET semantics
- No input size limit in v1: mistune is fast; can add throttling later if abused

### Edge Cases
- Invalid language in fenced code blocks: `get_lexer_by_name` raises `ClassNotFound` — fix by catching and falling back to plain `<pre>` output
- Malformed markdown: mistune is lenient by design, renders best-effort HTML — no special handling needed
- `escape=False` passes raw HTML through: existing behavior, consumer responsibility to sanitize if displaying user-generated content

### Testing
- Render endpoint happy path → `tests/test_docit/docit_core.py`
- Render endpoint validation (missing/empty input) → `tests/test_docit/docit_core.py`
- Render endpoint auth check → `tests/test_docit/docit_core.py`
- Invalid language code block graceful fallback → `tests/test_docit/docit_core.py`

### Docs
- `docs/django_developer/docit/README.md` — Add render endpoint section with usage example
- `docs/web_developer/` — Add endpoint contract (method, path, request/response format, permissions)

## Resolution

**Status**: resolved
**Date**: 2026-04-02

### What Was Built
Stateless `POST /api/docit/render` endpoint that accepts `{"markdown": "..."}` and returns `{"html": "..."}`. Also fixed `HighlightRenderer.block_code` to catch `ClassNotFound` on invalid language names instead of crashing.

### Files Changed
- `mojo/apps/docit/services/markdown.py` — Added try/except for `ClassNotFound` in `block_code`
- `mojo/apps/docit/rest/render.py` — New render endpoint handler
- `mojo/apps/docit/rest/__init__.py` — Wired up render module
- `tests/test_docit/docit_core.py` — 6 new tests (render endpoint + invalid language fallback)

### Tests
- `tests/test_docit/docit_core.py` — 27/27 passing
- Run: `bin/run_tests -t test_docit`

### Docs Updated
- Handled by post-build docs agent

### Security Review
- Handled by post-build security agent

### Follow-up
- None
