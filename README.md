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

class Group(models.Model, MojoModel):
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

Documentation is organized by audience:

### [Django Developer Reference](docs/django_developer/README.md)
For developers building applications with django-mojo. Covers models, REST framework, helpers, and all built-in apps. Written for both human developers and AI agents (LLM-optimized).

| Section | Description |
|---|---|
| [Core](docs/django_developer/core/README.md) | MojoModel, REST framework, decorators, middleware, serialization |
| [Helpers](docs/django_developer/helpers/README.md) | logit, dates, settings, crypto, request, redis, and other utilities |
| [Account](docs/django_developer/account/README.md) | User, Group, JWT authentication, permissions |
| [Logging](docs/django_developer/logging/README.md) | Database logging, security incidents |
| [Files](docs/django_developer/files/README.md) | File upload, storage backends, renditions |
| [Email](docs/django_developer/email/README.md) | AWS SES email, templates, inbound handling |
| [Jobs](docs/django_developer/jobs/README.md) | Async task queue |
| [Metrics](docs/django_developer/metrics/README.md) | Redis-backed time-series metrics |
| [Realtime](docs/django_developer/realtime/README.md) | WebSocket pub/sub |
| [PhoneHub](docs/django_developer/phonehub/README.md) | Phone number management and SMS |
| [FileVault](docs/django_developer/filevault/README.md) | Encrypted file vault |
| [DocIt](docs/django_developer/docit/README.md) | Documentation/wiki system |

### [REST API Reference](docs/web_developer/README.md)
For web developers integrating with django-mojo APIs. Covers endpoints, authentication, filtering, and all app APIs.

| Section | Description |
|---|---|
| [Core](docs/web_developer/core/README.md) | Authentication, request format, filtering, pagination, graphs |
| [Account](docs/web_developer/account/README.md) | Login, users, groups, API keys |
| [Logging](docs/web_developer/logging/README.md) | Log queries, incident management |
| [Files](docs/web_developer/files/README.md) | File uploads, downloads |
| [Email](docs/web_developer/email/README.md) | Templates, mailboxes |
| [Jobs](docs/web_developer/jobs/README.md) | Job status |
| [Metrics](docs/web_developer/metrics/README.md) | Time-series metrics |
| [Realtime](docs/web_developer/realtime/README.md) | WebSocket protocol |
| [PhoneHub](docs/web_developer/phonehub/README.md) | Phone lookup and normalization |
| [FileVault](docs/web_developer/filevault/README.md) | Encrypted file vault |
| [DocIt](docs/web_developer/docit/README.md) | Documentation pages |

---

## Contributing

We welcome pull requests and issues! Contributions should follow our [Developer Guide](docs/developer_guide.md) and maintain the framework’s philosophy: **keep it simple, explicit, and secure**.

---

## License

Licensed under the Apache License v2.0.
See the [LICENSE](LICENSE) file for details.

---
