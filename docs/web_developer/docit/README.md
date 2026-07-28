# DocIt — REST API Reference

Documentation/wiki system with hierarchical pages and Markdown rendering.

## Permissions

- Reading: `all` (public, no auth required by default)
- Writing: `manage_docit` or `owner`
- Deleting: `manage_docit`

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/docit/page` | List pages |
| POST | `/api/docit/page` | Create page |
| GET | `/api/docit/page/<id>` | Get page |
| PUT/POST | `/api/docit/page/<id>` | Update page |
| DELETE | `/api/docit/page/<id>` | Delete page |
| GET | `/api/docit/page/slug/<slug>` | Get page by slug |
| POST | `/api/docit/render` | Render Markdown to HTML |

## Get a Page

**GET** `/api/docit/page/1`

```json
{
  "status": true,
  "data": {
    "id": 1,
    "title": "Getting Started",
    "slug": "getting-started",
    "content": "# Getting Started\n\nWelcome...",
    "is_published": true,
    "created": "2024-01-01T00:00:00Z",
    "modified": "2024-01-15T10:00:00Z"
  }
}
```

## Get Page with Rendered HTML

```
GET /api/docit/page/1?graph=html
```

```json
{
  "status": true,
  "data": {
    "id": 1,
    "title": "Getting Started",
    "content": "# Getting Started\n...",
    "html": "<h1>Getting Started</h1>\n<p>Welcome...</p>"
  }
}
```

## Get Page by Slug

```
GET /api/docit/page/slug/getting-started
```

## Available Graphs

| Graph | Description |
|---|---|
| `list` | id, title, slug, is_published, order_priority, parent |
| `default` | Standard fields + content |
| `html` | Includes rendered `html` field (Markdown → HTML) |
| `tree` | Includes `children` array for hierarchical navigation |

## Create a Page

**POST** `/api/docit/page`

```json
{
  "book": 1,
  "title": "Authentication",
  "content": "## Authentication\n\nUse Bearer tokens...",
  "parent": 5,
  "is_published": true,
  "order_priority": 10
}
```

The `slug` is auto-generated from `title` (unique within the book).

## List Pages

```
GET /api/docit/page?book=1&is_published=true&sort=order_priority
GET /api/docit/page?parent=5   # children of page 5
GET /api/docit/page?search=authentication
```

## Update a Page

**POST** `/api/docit/page/1`

```json
{
  "content": "## Authentication\n\nUpdated content...",
  "is_published": true
}
```

## Render Markdown

**POST** `/api/docit/render`

Permission required: authenticated (any logged-in user).

Request:

```json
{
  "markdown": "# Hello World\n\n```python\nprint('hi')\n```"
}
```

Response:

```json
{
  "status": true,
  "html": "<h1>Hello World</h1>\n<div class=\"highlight\">...</div>\n"
}
```

Code blocks are syntax-highlighted using Pygments (`monokai` theme). The `highlight` CSS class is applied to the wrapper `<div>`. If an unrecognized language name is used, the block renders as a plain `<pre>` element with no highlighting.

Error responses:

| Status | Condition |
|---|---|
| 400 | `markdown` field missing or empty |
| 401 | Not authenticated |

## Knowledge-Base Search

**GET/POST** `/api/docit/search` — authenticated. Ranked search over
published pages of active books.

| Param | Required | Description |
|---|---|---|
| `q` | yes | Search terms, max 512 chars (`search` accepted as an alias). Websearch syntax: `"exact phrase"`, `-excluded`, `or` |
| `book` | no | Scope to one book by id or slug |
| `limit` | no | Max results — default 10, clamped to 1–50 |

The endpoint is rate-limited (120/min per IP, 60/min per device).
`snippet` is **untrusted text** — page authors control it; never render it
as HTML. In `pages` mode, matched terms are wrapped in `**` (markdown
emphasis), not HTML tags.

```json
{
  "status": true,
  "code": 200,
  "data": {
    "mode": "hybrid",
    "count": 2,
    "results": [
      {
        "page_id": 12,
        "page_slug": "quickstart",
        "page_title": "Quickstart",
        "book_id": 3,
        "book_slug": "install-guide",
        "heading": "Install > Quickstart",
        "snippet": "## Quickstart\n\nRun the installer...",
        "score": 0.032787
      }
    ]
  }
}
```

`mode` reports how the search ran: `hybrid` (vector + full-text — the
knowledge-base app with an embeddings provider), `fts` (knowledge-base app
without a provider), or `pages` (page-level full-text fallback when the
knowledge-base app is not installed). Results are chunk-level excerpts in
`hybrid`/`fts` mode (`heading` is the section breadcrumb) and page-level in
`pages` mode (`heading` empty).

Error responses:

| Status | Condition |
|---|---|
| 400 | `q` missing, empty, or over 512 chars |
| 403 | Not authenticated |
| 429 | Rate limit exceeded |

## Reindex a Book

**POST** `/api/docit/book/<id>` with `{"reindex": true}` — requires book
save permission (`manage_docit`, `docs`, or owner). Queues a background
re-embed job for every page of the book; use it to backfill after enabling
the knowledge base or after changing embedding settings.

```json
{
  "status": true,
  "data": {"queued": 12, "enabled": true}
}
```

`data.enabled` false means the knowledge-base app is not installed (no jobs
queued, `queued` is 0). A falsy action value (`{"reindex": false}`) is a
no-op. Repeat reindexes of unchanged pages are deduplicated by the jobs
system (idempotency keyed on page id + last-modified), so calling this in a
loop cannot flood the queue.

You normally do not need this after an edit. Re-embedding is queued
automatically on save, and when the server's reconciliation cron is enabled
a re-embed that got dropped (queue outage, failed job) is detected and
retried on its own — search catches up within roughly 15 minutes without
any client action. Reach for `reindex` for backfills (books that existed
before the knowledge base was enabled) and after an embedding-model change,
not as a routine step after saving a page.

## Assistant Tool

When `mojo.apps.assistant` is installed, the `docit` tool domain provides
`search_docs` (same parameters and results as the search endpoint) so the
assistant can ground answers in your documentation.
