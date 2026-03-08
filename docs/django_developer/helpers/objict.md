# objict — Django Developer Reference

`objict` is a dict subclass used throughout django-mojo. `request.DATA`, model metadata fields, and many framework return values are all `objict` instances.

## Import

```python
from objict import objict
```

## Attribute Access

All three forms are equivalent:

```python
d = objict(name="Joe", age=24)

d.name          # "Joe"
d["name"]       # "Joe"
d.get("name")   # "Joe"

# Missing keys return None instead of raising KeyError
d.unknown       # None
d.get("unknown")  # None
d.get("unknown", "default")  # "default"
```

## Nested / Hierarchical Keys

Use `set()` to write deeply nested values with dot-notation keys:

```python
d = objict()
d.set("user.name.first", "Joe")
d.set("user.name.last", "Smith")

d.user.name.first        # "Joe"
d.get("user.name.last")  # "Smith"
d.get("user.name.missing")  # None
```

**This is how `request.DATA` nested params work.** When a client sends `metadata.user.last_name=Smith`, you read it as:

```python
last = request.DATA.get("metadata.user.last_name")
# or
last = request.DATA.metadata.user.last_name
```

## Type-Safe Access

`get_typed(key, default, type)` converts the value to the given type, returning the default on failure:

```python
# Client sent page="3" as a string
page = request.DATA.get_typed("page", 1, int)       # 3
size = request.DATA.get_typed("page_size", 20, int) # 20 (missing → default)
ratio = request.DATA.get_typed("ratio", 0.0, float) # float conversion
```

Useful for query params that arrive as strings but need to be ints/floats.

## Creating objicts

```python
# From kwargs
d = objict(name="Joe", status="active")

# From dict
d = objict.fromDict({"name": "Joe"})

# From JSON string
d = objict.fromJSON('{"name": "Joe"}')

# Empty, then populated
d = objict()
d.name = "Joe"
d.set("address.city", "Austin")
```

## Merging

```python
d = objict(a=1, b=2)
d.extend({"b": 3, "c": 4})
# d → {"a": 1, "b": 3, "c": 4}
```

## Delta / Changes

```python
before = objict(name="Joe", age=24)
after  = objict(name="Joe", age=25)

before.changes(after)  # {"age": 25}
```

## request.DATA Patterns

`request.DATA` is an `objict` populated by `MojoMiddleware` from all request sources (POST body, GET params, JSON body). Always use it instead of `request.POST` or `request.GET`.

```python
# Simple field access
name = request.DATA.get("name")
pk   = request.DATA.get("id")

# With type conversion
page     = request.DATA.get_typed("page", 1, int)
is_admin = request.DATA.get_typed("admin", False, bool)

# Nested keys (client sent "address.city=Austin")
city = request.DATA.get("address.city")

# Attribute-style (same as get, just cleaner)
name = request.DATA.name
user_id = request.DATA.user_id

# Defaulting to avoid None checks
status = request.DATA.get("status") or "active"
```

## Serialization

```python
# To JSON string
json_str = d.to_json(as_string=True, pretty=True)

# To dict (plain)
plain = dict(d)

# Save / load file
d.save("data.json")
d2 = objict.from_file("data.json")
```
