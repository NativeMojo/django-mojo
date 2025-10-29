# Django-MOJO

**The Lightweight, Secure, and Extensible REST and Auth Framework for Django**

---

Django-MOJO helps you rapidly build secure APIs and manage authentication in Django projects—without the usual boilerplate or complexity. It brings clarity and maintainability to your codebase, all while providing robust object-level security and powerful helper utilities for real-world development.

---

## Why Django-MOJO?

- **Minimal Setup, Maximum Power:** Expose Django models as RESTful APIs with a few lines of code. Add new features without touching central registries.
- **Object-Level Permissions:** Move beyond Django’s traditional model-level permissions for fine-grained access control.
- **Secure by Default:** Every endpoint and operation is permission-aware, designed to keep your data and users safe.
- **Simple Patterns & Helpers:** Use concise decorators, built-in utilities (cron, crypto, logging, tasks), and keep codebases easy for teams to extend.
- **Effortless Testing:** Built-in lightweight test framework for REST APIs—no external dependencies needed.

---

## Core Workflow

1. **Create Django Models:**  
   Inherit from `MojoModel` and define a `RestMeta` inner class to configure permissions, default filters, and output graphs.

2. **Register REST Endpoints:**  
   Use decorators (`@md.URL`, `@md.GET`, etc.) to expose catch-all or method-specific API endpoints in your app’s rest/ directory.

3. **Embrace Helpers:**  
   Leverage and extend the extensive helpers in `mojo/helpers/` for logging, cron, request parsing, redis, and more.

4. **Test with Confidence:**  
   Write and run tests using MOJO’s integrated “testit” system—both for APIs and backend logic.

---

## Quick Example

**models/group.py**
```python
from django.db import models
from mojo.models import MojoModel

class Group(MojoModel):
    name = models.CharField(max_length=200)
    class RestMeta:
        VIEW_PERMS = ["view_groups"]
        SAVE_PERMS = ["manage_groups"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "default": {"fields": ["id", "name", "created"]},
        }
```

**rest/group.py**
```python
from mojo import decorators as md
from .models.group import Group

@md.URL('group')
@md.URL('group/<int:pk>')
def on_group(request, pk=None):
    return Group.on_rest_request(request, pk)
```

---

## Project Structure

- **mojo/account/** – Authentication models, JWT middleware, and permissions
- **mojo/base/** – Core model/REST abstractions
- **mojo/helpers/** – Logging, crypto, cron, and other utilities
- **mojo/tasks/** – Redis-backed task scheduling and runner
- **mojo/testit/** – Lightweight test suite and REST client
- **docs/** – Area-focused detailed documentation

---

## Documentation

Want details or examples? Dive into the docs:

- [Getting Started & Installing](docs/llm_context.md)
- [REST API & Graph Serialization](docs/rest.md)
- [Authentication & JWT](docs/auth.md)
- [Helpers & Utilities](docs/helpers.md)
- [Tasks & Cron Schedules](docs/tasks.md)  
- [Testing Framework](docs/testit/index.md)
- [Developing & Contributing](docs/developer_guide.md)
- [Decorators Reference](docs/decorators.md)
- [Metrics](docs/metrics.md)
- [Cron Scheduler](docs/cron.md)

---

## Contributing

We welcome pull requests and issues! Contributions should follow our [Developer Guide](docs/developer_guide.md) and maintain the framework’s philosophy: **keep it simple, explicit, and secure**.

---

## License

Licensed under the Apache License v2.0.
See the [LICENSE](LICENSE) file for details.

---
