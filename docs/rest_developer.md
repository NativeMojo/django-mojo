# MOJO REST Developer Guide: Graphs Deep Dive

This guide explains how to define, extend, and leverage the `RestMeta.GRAPHS` system in Django-MOJO models.
It is intended for Django/MOJO backend developers. For REST API consumers, see the docs in `rest_api/`.

---

## What Are Graphs?

Graphs are declarative, named serialization configurations for your Django models. They tell the MOJO framework how to output your model (and any related/nested models) when returning API responses—allowing you to control exactly what fields and relationships are exposed under various API contexts.

---

## Where Do Graphs Live?

Graphs are defined in the `RestMeta` inner class of your MOJO model:

```python
class Project(MojoModel):
    name = models.CharField(...)
    created = models.DateTimeField(...)
    owner = models.ForeignKey('User', ...)

    class RestMeta:
        GRAPHS = {
            "default": {
                "fields": ["id", "name", "created", "owner"],
                "graphs": {
                    "owner": "basic"
                }
            },
            "basic": {
                "fields": ["id", "name"]
            }
        }
```

- Each key in `GRAPHS` names a graph.
- `"fields"` — which model fields should be included when serializing under this graph.
- `"graphs"` — controls how any **related field** (FK, O2O, M2M) should itself be serialized by referencing another graph (often from the related model).

---

## Core Patterns & Advanced Nesting

### Basic Structure

- Each graph is a dictionary.
- Fields can be any model field (including relationships).
- Use the `"graphs"` key to control serialization of nested objects.

### Nested Graph Example

Suppose `Project` has an `owner` (User) field and you want nested data for the user in the API response:

```python
class User(MojoModel):
    username = models.CharField(...)
    email = models.EmailField(...)

    class RestMeta:
        GRAPHS = {
            "basic": {"fields": ["id", "username"]},
            "detailed": {"fields": ["id", "username", "email"]}
        }

class Project(MojoModel):
    owner = models.ForeignKey(User, ...)

    class RestMeta:
        GRAPHS = {
            "default": {
                "fields": ["id", "owner"],
                "graphs": {"owner": "basic"}
            }
        }
```

- `Project` with the `"default"` graph will embed a serialized `"owner"` using the `"basic"` graph of the related `User`.

### Deeper Nesting

You can nest graphs multiple layers deep for more complex domains (organizations > teams > members...).

---

## Field Options & Special Cases

- **fields:** List the fields you want returned for this graph; any relations listed should also have a graph handler in `"graphs"` for best results.
- **graphs:** Dictionary mapping field names (relation fields) to the graph name to use for serialization.
- You can use the same or different field graphs for list vs detail endpoints by naming them (`"list"`, `"default"`, `"detailed"`, etc.) and using the `graph` query param or context in API clients.

---

## Best Practices

- **Keep It Minimal:** Only include fields and relationships that are really needed in each graph context.
- **Avoid Circular Relations:** Don't nest parent-child graphs so deeply you risk recursion/infinity.
- **Use a "basic" or "list" graph:** For lists and lightweight API calls, define a minimal graph without costly relationships or sensitive fields.
- **Use "default" for detail responses:** Include richer, but still controlled, information.
- **Customize via `"graphs"` as needed:** Always declare how nested FKs/O2Os should be serialized for control and safety.
- **DRY Patterns:** Reuse graph names ("basic", "default") across your models—a consistent API helps everyone.
- **Advanced: Compose or generate graphs dynamically (by override or using helper mixins) if you have meta or API-layer needs.**

---

## Extension Patterns

- **Graph Inheritance:** If you have a base model with common fields via an abstract model, you can reference its graph definitions directly, or build up graphs with shared fragments via Python.
- **Overrides:** You can override the `to_dict` or `GraphSerializer` for custom serialization logic, if the graph system’s declarative style isn’t enough for some edge case.
- **Dynamic Graphs:** For extremely dynamic scenarios (per-user, runtime-driven), you can programmatically select or extend graphs in your model/endpoint code before returning the API result, though this should be used sparingly.

---

## Debugging and Evolving Graphs

- **Testing:** Always test your API outputs for every graph you expose—pay special attention to nested serializations and sensitive data exposure.
- **Versioning:** If you need backwards compatibility, use new graph names for new versions and softly deprecate old ones.
- **Audit:** Review graphs periodically to ensure no private fields have slipped into too-public graphs.

---

## References

- See the REST user guide for details on how to select different graphs at request time (`docs/rest_api/using_graphs.md`).
- Core serialization logic lives in `mojo/serializers/simple.py`.

---

## FAQ

**Q: What if a field is missing from "fields" but present in "graphs"?**
- Only fields in `"fields"` are returned; `"graphs"` only influences *how* related objects will be serialized if their field is also listed.

**Q: Can graphs be selected dynamically by API users?**
- Yes! Typically via the `graph` query param in REST requests (`?graph=list`, `?graph=detailed`, etc.).

**Q: What if a related model doesn’t have a graph with the needed name?**
- You’ll see an error or raw PK output—always define your graphs for every related model.

---

**Questions or best-practice tips? Improve this doc and help the community!**
