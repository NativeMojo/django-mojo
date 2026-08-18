# Graphs — REST API Reference

## What Are Graphs

Graphs are named response shapes. Each endpoint supports one or more named graphs that control which fields are returned and how related objects are nested. Use the `graph` parameter to select a shape.

## Using Graphs

```
GET /api/myapp/book/1?graph=default
GET /api/myapp/book?graph=list
GET /api/myapp/book/1?graph=full
```

## Common Graph Names

Most models provide these standard graphs:

| Graph | Description |
|---|---|
| `list` | Minimal fields, optimized for list views |
| `default` | Standard fields for a single object view |
| `basic` | Very minimal, used when nested inside other objects |
| `full` | All available fields |

The default graph used when no `graph` param is provided is `default` for single objects and `list` for lists.

### Unknown graph names: fall back or refuse

Whether an undefined `graph` value errors depends on the *kind* of name:

- **Common names** — `default`, `basic`, `list`, `simple`, `detail`,
  `detailed`, `full` — fall back to `default` silently (`200 OK`) when a
  resource doesn't define them. The envelope's `graph` key echoes the name you
  requested even after a fallback, so it is **not** a reliable typo detector —
  compare the fields you got against the resource's documented `default`.
- **Any other (special) name** — `admin`, `token`, and the like — is **refused
  with `400`** when the resource doesn't define it, rather than silently
  serving `default`. A special graph names a specific view; if the resource
  doesn't have it, that's an error, not a fallback.

### Graphs can require a permission

A resource may require a permission for a specific graph (in addition to the
permission to read the resource at all). Requesting such a graph without the
permission returns **`403`** naming the graph and the permission required —
it does **not** silently downgrade to a thinner graph. If you see a `403`
mentioning a graph, request a graph you are allowed to use (often `default`),
or obtain the named permission. This replaces the older pattern of a sensitive
field silently vanishing on an ungated graph.

## Nested Objects

Some graphs include nested related objects. For example, a `default` graph for a book might include the author object:

```json
{
  "id": 1,
  "title": "My Book",
  "author": {
    "id": 5,
    "display_name": "Alice"
  }
}
```

Without nesting, only the foreign key ID is returned:

```json
{
  "id": 1,
  "title": "My Book",
  "author_id": 5
}
```

Refer to per-resource documentation for available graphs and their field sets.

## Download Formats

Some endpoints support file downloads in addition to JSON. Use `download_format` instead of `graph`:

```
GET /api/myapp/book?download_format=csv
GET /api/myapp/book?download_format=csv&filename=my_books.csv
```

Supported formats vary by resource. The response will be a file download with the appropriate `Content-Type`.
