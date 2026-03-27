# Django Developer Reference

LLM-optimized reference documentation for building Django applications with the django-mojo framework.

Companion track for REST integrators: [docs/web_developer/README.md](../web_developer/README.md).

## Framework Core

| Section | Description |
|---|---|
| [core/](core/README.md) | MojoModel, REST framework, decorators, middleware, serialization |
| [helpers/](helpers/README.md) | logit, dates, settings, settings reference keys, crypto, request, response, redis, and other utilities |

## Built-in Apps

| Section | Description |
|---|---|
| [account/](account/README.md) | User, Group, JWT authentication, permissions, push notifications |
| [logging/](logging/README.md) | Database logging (logit app), security incidents, fleet-wide IP blocking |
| [files/](files/README.md) | File upload, storage backends, renditions |
| [email/](email/README.md) | AWS SES email, templates, mailboxes, inbound handling |
| [aws/cloudwatch.md](aws/cloudwatch.md) | CloudWatch monitoring — EC2, RDS, and ElastiCache metrics via boto3 |
| [jobs/](jobs/README.md) | Async job queue — publishing, scheduling, retries, webhooks, broadcasting, monitoring |
| [metrics/](metrics/README.md) | Redis-backed time-series metrics |
| [chat/](chat/README.md) | Real-time chat rooms, messages, reactions, moderation |
| [realtime/](realtime/README.md) | WebSocket pub/sub via Django Channels |
| [phonehub/](phonehub/README.md) | Phone and device management |
| [shortlink/](shortlink/README.md) | URL shortener with OG previews, file links, and click tracking |
| [filevault/](filevault/README.md) | Encrypted file vault |
| [docit/](docit/README.md) | Documentation system |
| [testit/](testit/Overview.md) | Testing framework and patterns for django-mojo projects |

## Quick Start Pattern

Every feature follows the same pattern:

```python
# 1. Model (app/models/my_model.py)
from django.db import models
from mojo.models import MojoModel

class MyModel(models.Model, MojoModel):
    user = models.ForeignKey("account.User", null=True, on_delete=models.SET_NULL)
    group = models.ForeignKey("account.Group", null=True, on_delete=models.SET_NULL)
    name = models.CharField(max_length=255)
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class RestMeta:
        VIEW_PERMS = ["view_mymodel", "owner"]
        SAVE_PERMS = ["manage_mymodel", "owner"]
        GRAPHS = {
            "list": {"fields": ["id", "name", "created"]},
            "default": {"fields": ["id", "name", "created", "modified"]},
        }

# 2. REST endpoint (app/rest/my_model.py)
import mojo.decorators as md
from ..models.my_model import MyModel

@md.URL('mymodel')
@md.URL('mymodel/<int:pk>')
def on_my_model(request, pk=None):
    return MyModel.on_rest_request(request, pk)
```

## Key Conventions

- Always use `request.DATA` (never `request.POST.get()` or `request.GET.get()`) — it is an [`objict`](helpers/objict.md) with attribute access and dot-notation nested keys
- View functions return **plain dicts** — never construct `JsonResponse` manually (see [Return Values](core/decorators.md#return-values))
- List endpoints must NOT end with trailing slashes
- **URL auto-prefix is the Django app directory name** — `@md.URL('session')` in app `wallet` → `/api/wallet/session`. Never use a parent package name.
- **Dynamic URL segments go at the END only** — `book/<int:pk>` is correct; `book/<int:pk>/chapters` is never correct. Use query params or POST data for nested lookups.
- **Use `POST_SAVE_ACTIONS` for model operations** — prefer `on_action_<name>` over dedicated REST endpoints when acting on a specific instance
- Model inheritance: `models.Model, MojoModel` (or `MojoSecrets, MojoModel`)
- Always include `created` and `modified` fields
- Add `user` and `group` FKs when model needs access control
- Never create migration files (user's responsibility)
- Never use Python type hints
