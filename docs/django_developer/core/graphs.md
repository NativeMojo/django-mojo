# Serialization & Graphs — Django Developer Reference

## What Are Graphs

Graphs are named serialization shapes defined in `RestMeta.GRAPHS`. They control exactly which fields are returned and how related objects are nested. The client selects a graph via `?graph=<name>`.

## Defining Graphs

```python
class RestMeta:
    GRAPHS = {
        "list": {
            "fields": ["id", "title", "created"]
        },
        "default": {
            "fields": ["id", "title", "body", "created", "modified"],
            "graphs": {
                "author": "basic"  # nest User.GRAPHS["basic"] under "author" key
            }
        },
        "full": {
            # empty dict = all model fields
        }
    }
```

## Graph Definition Keys

| Key | Type | Description |
|---|---|---|
| `fields` | list | Fields to include. Omit for all fields. |
| `exclude` | list | Fields to always exclude (useful in full graphs) |
| `graphs` | dict | Nested graphs: `{"field_name": "graph_name"}` |
| `localize` | dict | Timezone localization config for datetime fields |

## Standard Graph Names

Use these names consistently across models:

| Name | Purpose |
|---|---|
| `list` | Minimal fields for list responses |
| `default` | Standard single-instance response |
| `basic` | Minimal for use as a nested graph in other models |
| `full` | All fields (use `exclude` to protect sensitive fields) |

## Nested Graphs

Reference another model's graph by name using the `graphs` key:

```python
GRAPHS = {
    "default": {
        "fields": ["id", "title", "author_id"],
        "graphs": {
            "author": "basic"   # serializes self.author using User.GRAPHS["basic"]
        }
    }
}
```

The related model must also define a `GRAPHS["basic"]` (or whatever name you reference).

## Protecting Sensitive Fields

```python
GRAPHS = {
    "default": {
        "fields": ["id", "name", "created"],
        "exclude": ["mojo_secrets", "password_hash", "api_key"]
    },
    "full": {
        "exclude": ["mojo_secrets", "password_hash"]
    }
}
```

Also use `NO_SHOW_FIELDS` in RestMeta to globally exclude fields from all graphs:

```python
class RestMeta:
    NO_SHOW_FIELDS = ["mojo_secrets", "internal_notes"]
```

## Programmatic Serialization

```python
# Single instance
data = book.to_dict(graph="default")

# Queryset
data_list = Book.queryset_to_dict(Book.objects.all(), graph="list")
```

## Download Formats

For CSV/Excel exports, define `FORMATS` in RestMeta:

```python
class RestMeta:
    FORMATS = {
        "csv": ["id", "title", "created"],
        "csv_detailed": ["id", "title", "body", "author", "created"],
    }
```

Request via `?download_format=csv` or `?download_format=csv_detailed`. The response will be a file download rather than JSON.

## Response Envelope

All list responses are wrapped in:

```json
{
  "status": true,
  "count": 42,
  "start": 0,
  "size": 10,
  "data": [...]
}
```

Single-instance responses:

```json
{
  "status": true,
  "data": { ... }
}
```

Error responses:

```json
{
  "status": false,
  "code": 403,
  "error": "GET permission denied: Book",
  "is_authenticated": true
}
```
