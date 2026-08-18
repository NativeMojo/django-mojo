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

### Graph Resolution — Fallback, Refusal, and the Whole-Model Guard

Two layers resolve a requested graph, with different jobs.

**At the REST boundary** (where the caller controls the name via `?graph=`), an
undefined name is resolved three ways:

| Requested | Defined on the model? | Result |
|---|---|---|
| any name | yes | serve it (subject to `GRAPH_PERMISSIONS`) |
| a **common** name — `default`, `basic`, `list`, `simple`, `detail`, `detailed`, `full` | no | fall back to `default` (`200`) |
| a **special** name — anything else (`admin`, `token`, `federation`, …) | no | **refused with `400`** |

A common name describes *how much* of a record you want, so an undefined one is
a harmless generic request and falls back. A special name is a deliberate
request for a particular view; answering it with `default` would mislead the
caller and hand a prober a `200` for every name they try, so it is refused. The
common set is `COMMON_GRAPH_NAMES` in `mojo/models/rest.py`, overridable per
deployment via the conf-file-only `REST_COMMON_GRAPH_NAMES` setting.

The `on_rest_list` path requests graph `"list"`, so a model without an explicit
`list` graph still serves its `default` field set (and any `extra` values on it)
on lists — define an explicit `list` graph, or keep that field off `default`, if
that matters.

**In the serializer** (which has no request — services and `to_dict(graph=…)`
call it directly), resolution is two-way: a defined graph is served, otherwise
`default` is served. **If the requested graph AND `default` are both undefined
on a model that declares `GRAPHS`, the serializer raises** rather than dumping
every field — a model shipping a partial graph set with no `default` is a
misconfiguration, and the meta-test in `tests/test_models/graph_permissions.py`
keeps it from shipping. A model that declares **no** `GRAPHS` at all (or an empty
map) keeps its deliberate whole-model serialization: that is an opt-out gated by
`VIEW_PERMS`, not the partial-graph bug.

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

`NO_SHOW_FIELDS` removes a field from **every** caller with no way to opt back
in — a superuser cannot see it through the REST path either. When a field should
be visible to *privileged* readers but not everyone, prefer a permission-gated
graph (below) over `NO_SHOW_FIELDS`.

## Per-Graph Permissions (`GRAPH_PERMISSIONS`)

`GRAPHS` alone carries no permission — any caller who can read a record at all
can request its richest graph. To require a permission for a specific graph,
declare `RestMeta.GRAPH_PERMISSIONS`, a map of graph name → permissions:

```python
class RestMeta:
    VIEW_PERMS = ["view_platform", "manage_platform", "admin"]
    GRAPH_PERMISSIONS = {"admin": ["manage_platform", "admin"]}
```

- **Opt-in and additive.** A model that declares no `GRAPH_PERMISSIONS`, or has
  no entry for the requested graph, behaves exactly as before. `VIEW_PERMS`
  gates reading the model at all; the graph's permissions are then required *on
  top*, with the same OR-semantics (`implied_perms`, bare-domain grants) as
  every other gate.
- **Enforced on the graph actually served.** A request for an undefined common
  name that falls back to `default` is checked against `default`'s permissions,
  not the requested name's.
- **Denied → `403`** naming the graph and the permission required — never a
  silent downgrade to a thinner graph, so an operator seeing missing fields can
  tell "withheld" from "absent".
- **Enforced at the REST boundary and the assistant model tools**, not in the
  serializer (which has no request). Internal `to_dict(graph=…)` and service
  callers are unaffected — a service that needs a privileged graph already has
  its own authorization.
- **Tenancy caveat.** A member-level grant satisfies a graph permission only on
  a model whose tenancy is derivable (a `group` FK or `GROUP_FIELD`), where the
  check binds to the row's own tenant. On a groupless model, or one gated only
  by a membership view-hook, **only a global grant** satisfies the graph
  permission — fail-closed by construction.

Nested graphs are **not** permission-gated: a graph's field lists and its nested
`graphs` are developer-authored config trusted like the fields themselves. Keep
sensitive related data out of a nested graph (the convention is to snapshot the
needed fields rather than nest — see `incident/models/event.py`), or gate the
top-level graph that pulls it in.

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
