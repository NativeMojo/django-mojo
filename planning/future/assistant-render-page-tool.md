# Assistant Render Page Tool

**Type**: request
**Status**: future
**Date**: 2026-04-05
**Priority**: low

## Description

Add a `render_page` assistant tool that uses Playwright (headless Chromium) to render JavaScript-heavy web pages and return the fully-rendered HTML as clean text. Complements the existing `browse_url` tool which handles static/server-rendered pages via BeautifulSoup.

## Context

`browse_url` works well for documentation, changelogs, API references, and other static content. But modern SPAs (React, Next.js CSR, Angular) render content via JavaScript — `browse_url` returns empty or skeleton HTML for these pages. A separate `render_page` tool bridges this gap for the rare cases where JS rendering is needed.

Keeping this as a separate tool (not upgrading `browse_url`) is intentional:
- `browse_url` stays fast, lightweight, and low-risk (no JS engine = no JS execution attacks)
- `render_page` is explicitly heavier, with its own permission gate and resource controls
- The LLM can choose the right tool based on context

## Design Direction

- **Playwright** as an optional dependency — not required for django-mojo to function
- **Runs on EC2 worker** via the jobs system, not in the web process. Headless Chromium is resource-heavy and should run isolated on infrastructure.
- **Job-based execution**: `render_page` publishes a job, worker spins up Playwright, renders the page, returns the result. The assistant polls or awaits the job result.
- **Separate permission**: `render_page` or similar, distinct from `browse_url`'s `view_admin`
- **Timeout**: 30s render timeout (JS-heavy pages can be slow)
- **Same SSRF protections** as `browse_url` — private IP blocking, scheme validation
- **Additional security**: disable JS popups/dialogs, block downloads, sandbox the browser context, restrict navigation away from the target URL

## Acceptance Criteria

- Tool `render_page` accepts `url` (required), optional `selector`, optional `wait_for` (CSS selector to wait for before extracting)
- Renders the page with headless Chromium via Playwright
- Extracts readable text from the rendered DOM (same extraction logic as `browse_url`)
- Returns same shape as `browse_url`: `{"url", "title", "content", "truncated"}`
- Runs as a job on EC2 worker, not in the web process
- Graceful fallback: if Playwright is not installed, returns a clear error suggesting `browse_url`
- Permission-gated separately from `browse_url`

## Dependencies

- `playwright` — optional dependency, not in core `pyproject.toml`
- Chromium browser binary on the worker EC2 instances
- Jobs system for async execution

## Security Considerations

- Headless browser can execute arbitrary JS — must sandbox browser context
- Block navigation away from target URL (no redirect chains to internal services)
- Disable file downloads, popups, permission prompts
- Apply same SSRF IP checks as `browse_url` before navigating
- Resource limits: memory cap on browser process, render timeout
- Consider running in a container or restricted user on the EC2 worker

## Out of Scope

- Interactive browsing (clicking, form filling)
- Screenshot capture
- Cookie/session persistence across calls
- Authentication to external sites
- PDF rendering

## Notes

- No urgency — `browse_url` covers the primary use cases (docs, references, APIs)
- Build when there's actual demand for JS-rendered page content
- The jobs infrastructure and EC2 worker pool already exist
