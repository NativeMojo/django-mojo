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

**A misspelled or unsupported `graph` value does not error.** If you request
a graph name a resource doesn't define, the response falls back to `default`
silently — `200 OK`, with `default`'s fields rather than the ones you
expected. The response envelope's `graph` key still echoes back whatever name
you requested, even when a fallback happened, so it is **not** a reliable way
to detect the typo — compare the fields you got against the resource's
documented `default` graph instead. This also means a resource that gates a
sensitive field behind an opt-in graph (see e.g.
[account/api_keys.md](../account/api_keys.md#security-notes)) will quietly
omit that field on a typo'd graph name rather than telling you it doesn't
exist.

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
