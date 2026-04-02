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
