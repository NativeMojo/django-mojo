![](https://github.com/311labs/objict/workflows/tests/badge.svg)

# objict

`objict` is a Python library that extends the standard dictionary to allow attribute-style access, hierarchical keys, and additional functionalities like JSON/XML serialization, delta comparison, and more. It's inspired by the [uberdict](https://github.com/eukaryote/uberdict) library.

## Installation

To install `objict`, use the following pip command:

```bash
pip install pyobjict
```

## Features

- **Attribute Access**: Access dictionary values using dot notation (e.g., `d.key`).
- **Hierarchical Keys**: Seamlessly handle nested dictionary structures with keys like `a.b.c`.
- **Serialization**: Convert to/from JSON, XML, and ZIP formats.
- **File Operations**: Read from and write to files conveniently.
- **Delta Comparison**: Compute differences between two `objicts`.
- **Resilient Missing Keys**: Returns `None` instead of raising an error for missing attributes.
- **Type Safety and Conversion**: Supports type-safe fetching of values.

## Basic Usage

```python
from objict import objict

# Creating an objict
d1 = objict(name="John", age=24)
print(d1.name)  # Output: John
print(d1["age"])  # Output: 24

# Adding new attributes
d1.gender = "male"
print(d1.gender)  # Output: male

# Nested objict
d2 = objict(user=d1)
print(d2.user.name)  # Output: John
```

## Advanced Usage

### Hierarchical Keys

```python
d3 = objict()
d3.set("address.street", "123 Elm St")
print(d3.address.street)  # Output: 123 Elm St
```

### Serialization

#### JSON

```python
json_data = d1.to_json(as_string=True, pretty=True)
print(json_data)
```

#### XML

```python
xml_data = d1.to_xml()
print(xml_data)
```

#### ZIP Compression

```python
zip_data = d1.to_zip(as_string=True)
```

### File Operations

```python
d1.save("data.json")
d3 = objict.from_file("data.json")
print(d3)  # Reconstructed objict from file
```

### Delta Comparison

```python
d4 = objict(name="John", age=25)
changes = d1.changes(d4)
print(changes)  # Output: {'age': 25}
```

### Type Safety and Conversion

```python
dob = d1.get_typed("dob", typed=datetime.datetime)
```

### Handling Missing Keys and Defaults

```python
print(d1.unknown_key)  # Output: None

d1.setdefault("country", "USA")
print(d1.country)  # Output: USA
```

## Utility Methods

- **to_json()**: Convert `objict` to JSON.
- **to_xml()**: Convert `objict` to XML.
- **to_zip()**: Compress `objict`.
- **from_json(json_string)**: Create `objict` from JSON.
- **from_file(path)**: Load `objict` from a file.
- **copy(shallow=True)**: Create a copy of the `objict`.
- **extend(\*args, \*\*kwargs)**: Merge another dictionary into this `objict`.

## Conclusion

`objict` is a versatile tool for developers who want more flexible dictionary operations in Python. Whether you're dealing with nested data, performing file I/O, or converting data formats, `objict` simplifies these tasks with a clean and Pythonic interface.
