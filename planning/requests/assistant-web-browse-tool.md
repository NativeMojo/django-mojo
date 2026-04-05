# Assistant Web Browse Tool

**Type**: request
**Status**: planned
**Date**: 2026-04-05
**Priority**: medium

## Description

Add a `browse_url` tool to the assistant that fetches a web page and returns its content as clean, readable text. This lets the LLM read documentation, reference pages, API docs, changelogs, and other web content on behalf of the user during a conversation.

## Context

The assistant already has 35+ tools across security, jobs, users, groups, metrics, and discovery domains. All tools follow the same `register_tool()` pattern with permission gating and handler functions. Adding a web browse tool fits naturally into this architecture.

The `requests` library is already a project dependency. The main work is fetching a URL, extracting readable text (stripping nav/ads/scripts), and returning it in a size the LLM context window can handle.

## Acceptance Criteria

- Tool `browse_url` registered in the assistant tool system
- Accepts a `url` parameter (required) and optional `selector` (CSS selector to narrow content)
- Fetches the page, extracts readable text content (no HTML tags, no scripts/styles/nav)
- Truncates output to a configurable max length (default ~20,000 chars) to avoid blowing context
- Returns `{"url": "...", "title": "...", "content": "...", "truncated": bool}`
- Respects a reasonable timeout (10s) and returns clean errors on failure
- Permission-gated (suggested: `admin` or a new `assistant_browse` perm)
- Blocks obviously dangerous schemes (file://, ftp://, etc.) — only http/https
- Sets a proper User-Agent header
- Tool is registered in the built-in tools under a `web` domain

## Investigation

**What exists**:
- Tool registration system: `mojo/apps/assistant/__init__.py` — `register_tool()`
- Built-in tool pattern: `mojo/apps/assistant/services/tools/` — each domain is a module with a `TOOLS` list
- Agent loop: `mojo/apps/assistant/services/agent.py` — handles tool calls with permission gates
- `requests` library already in `pyproject.toml`
- Auto-discovery: any Django app can add `assistant_tools.py` for custom tools

**What changes**:
- `mojo/apps/assistant/services/tools/web.py` — **new file**: handler + TOOLS list for `browse_url`
- `mojo/apps/assistant/services/tools/__init__.py` — import and register the web domain
- `docs/django_developer/assistant/README.md` — document the new tool
- `docs/web_developer/` — no changes needed (tool is assistant-internal, not a REST endpoint)

**Constraints**:
- Content extraction quality matters — raw HTML is useless to the LLM. Need either `beautifulsoup4` (parse + extract text) or `trafilatura` (purpose-built for article extraction). BS4 is lighter and more common; trafilatura is better at extracting main content from noisy pages.
- Must cap response size — a full docs page can be 100K+ chars, but LLM tool results should stay under ~20K to leave room for conversation context.
- Security: must validate URL scheme (http/https only), set timeout, don't follow excessive redirects. No SSRF risk since this runs server-side — consider whether to restrict to public IPs only.
- Some sites block bots — should set a reasonable User-Agent and handle 403/429 gracefully.

**Related files**:
- `mojo/apps/assistant/__init__.py`
- `mojo/apps/assistant/services/tools/__init__.py`
- `mojo/apps/assistant/services/tools/security.py` (pattern reference)
- `mojo/apps/assistant/services/agent.py`
- `docs/django_developer/assistant/README.md`

## Settings (if applicable)

| Setting | Default | Purpose |
|---|---|---|
| `LLM_BROWSE_MAX_LENGTH` | 20000 | Max chars returned from a page fetch |
| `LLM_BROWSE_TIMEOUT` | 10 | HTTP request timeout in seconds |

## Dependencies

Need one of:
- `beautifulsoup4` — lightweight HTML parsing, extract text with `.get_text()`. Already widely used, minimal footprint.
- `trafilatura` — purpose-built web content extraction (strips boilerplate, nav, ads). Heavier but much better results on real-world pages.

Recommendation: start with `beautifulsoup4` for simplicity. Can upgrade to `trafilatura` later if content quality is an issue.

## Tests Required

- Fetch a known public URL and verify title + content returned
- Verify non-http schemes (file://, ftp://) are rejected
- Verify timeout handling returns clean error
- Verify content truncation at max length with `truncated: true`
- Verify CSS selector filtering narrows content
- Verify permission gate — unpermitted user gets denied
- Verify 404/500 responses return clean error dict

## Out of Scope

- JavaScript rendering (no headless browser) — static HTML only
- Caching fetched pages
- Crawling / following links automatically
- File download (PDF, images, etc.)
- Authentication to external sites

## Plan

**Status**: planned
**Planned**: 2026-04-05

### Objective
Add a `browse_url` assistant tool that fetches web pages, extracts readable text with SSRF protection, and returns it to the LLM.

### Steps
1. `pyproject.toml` — Add `beautifulsoup4` dependency
2. `mojo/apps/assistant/services/tools/web.py` — New file: `_tool_browse_url` handler with URL validation, SSRF guard, bs4 text extraction, CSS selector support, truncation. `TOOLS` list with `permission="view_admin"`.
3. `mojo/apps/assistant/services/tools/__init__.py` — Import `web`, register domain
4. `tests/test_assistant/5_test_web_tools.py` — Tests for scheme validation, SSRF, fetch, truncation, selector, errors, registration
5. `docs/django_developer/assistant/README.md` — Add Web Domain tools table

### Design Decisions
- **beautifulsoup4**: lighter than trafilatura, no transitive deps, sufficient for docs/reference pages
- **`view_admin` permission**: same as other read-only tools, no new permission needed
- **SSRF via IP resolution**: resolve hostname before connecting, reject private/loopback/link-local
- **Strip nav/header/footer/script/style**: removes boilerplate, improves content quality
- **20K char default**: leaves room for conversation context

### Edge Cases
- Non-HTML content: return raw text, skip bs4
- Redirect loops: cap at 3 redirects
- Large pages: truncate after extraction
- Bot blocking: custom User-Agent, clean 403/429 errors

### Testing
- All scenarios → `tests/test_assistant/5_test_web_tools.py`

### Docs
- `docs/django_developer/assistant/README.md` — Web Domain tools table
