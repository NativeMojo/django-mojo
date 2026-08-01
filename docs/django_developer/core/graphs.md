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
| `extra` | list | Computed fields, sourced from a method/property instead of a column: `[("method_name", "output_key")]`, or a bare `"method_name"` to use the method name as the key. Called on the instance for every row serialized under that graph — put anything with a side effect (an audit write, a decrypt) here rather than in `fields`, since `extra` runs per-graph, not globally. |

## Standard Graph Names

Use these names consistently across models:

| Name | Purpose |
|---|---|
| `list` | Minimal fields for list responses |
| `default` | Standard single-instance response |
| `basic` | Minimal for use as a nested graph in other models |
| `full` | All fields (use `exclude` to protect sensitive fields) |

### Fallback for Unrecognized or Missing Graphs

If `?graph=<name>` names a graph the model doesn't define, the serializer falls
back to `default` — silently, with a `200` and no error. This applies whether
the name is a typo or simply undefined; only a model's own `GRAPHS` dict
decides what exists. The same fallback happens for **list** responses: `on_rest_list`
requests graph `"list"`, and if the model defines no `"list"` graph, the
serializer falls back to `"default"` — so a model without an explicit `list`
graph exposes its full `default` field set (and any `extra` values on it) on
every list response too. If a `default` graph carries something you don't want
on lists, either define an explicit `list` graph or keep that field off
`default` (see [Opt-In Sensitive Graphs](#opt-in-sensitive-graphs) below).

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

### Opt-In Sensitive Graphs

`exclude` and `NO_SHOW_FIELDS` hide a field everywhere. Sometimes a field
should be readable, just not on every ordinary read — a live credential that
a caller may deliberately ask for, but that shouldn't ride along on a list or
a routine detail fetch (and, because of the [fallback](#fallback-for-unrecognized-or-missing-graphs)
above, defining no `list` graph means `default` doubles as the list response
too). The pattern: leave the sensitive value off `default`/`list`, and expose
it only from a dedicated graph the caller must name explicitly, via an
`extra` method — which also gives you a hook to audit-log the read:

```python
class ApiKey(MojoSecrets, MojoModel):   # secrets model — stores the token via MojoSecrets
    def rest_get_token(self):
        # decrypt + write an audit log row here; see mojo/apps/account/models/api_key.py
        ...

    class RestMeta:
        GRAPHS = {
            "default": {
                "fields": ["id", "name", "is_active"],   # no token
            },
            "token": {
                "fields": ["id", "name", "is_active"],
                "extra": [("rest_get_token", "token")],  # only here, and audited
            },
        }
```

The permission bar for `?graph=token` is whatever `VIEW_PERMS` already grants
— this pattern narrows *where* a value travels, not *who* may ask for it. See
[account/api_keys.md](../account/api_keys.md#security-notes) for the full
worked example, including why creation and `/rotate` responses attach the
token explicitly rather than relying on a request-selected graph.

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
