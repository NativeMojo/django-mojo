# Django-MOJO

**The full-stack Django framework for teams that want to ship, not assemble.**

---

Most Django projects end up wiring together the same set of packages: a REST layer, an auth system, a job queue, a WebSocket server, a file store, a secrets manager, a metrics backend. Each one has its own conventions, its own config, its own failure modes — and none of them know about each other.

Django-MOJO ships all of that pre-integrated, with consistent patterns throughout. Define your models, configure permissions, and you have a production-grade API. The rest is already there when you need it.

---

## What You Get

| You used to wire up... | Django-MOJO ships... |
|---|---|
| DRF + serializers + viewsets | Model-first REST with `RestMeta` — permissions, graphs, filtering, pagination built in |
| SimpleJWT + django-allauth + MFA library | Complete auth stack: JWT, OAuth, TOTP, passkeys, API keys, device management, session control |
| Channels + custom routing | WebSocket pub/sub realtime — same auth as HTTP, topic-based, zero extra config |
| Celery + Redis | Async job queue with scheduling, retries, heartbeat, and a web API |
| django-storages + custom S3 wrappers | File upload, S3 backend, renditions, and an encrypted file vault |
| python-decouple + HashiCorp Vault | DB-backed encrypted settings — secrets stay out of your repo, update without restart |
| structlog + Sentry + custom alerting | Request logging, security incident tracking, and alerting baked into the request cycle |
| InfluxDB client or custom Redis counters | Redis-backed time-series metrics with group-scoped rollups |
| Twilio SDK + phone number logic | PhoneHub — SMS sending, phone normalization, test-number shortcuts |

---

## Install

```bash
pip install django-mojo
```

---

## Quick Start

**1. Define your model:**

```python
from django.db import models
from mojo.models import MojoModel

class Article(models.Model, MojoModel):
    title = models.CharField(max_length=200)
    body = models.TextField()
    author = models.ForeignKey("account.User", on_delete=models.CASCADE)
    is_published = models.BooleanField(default=False)

    class RestMeta:
        VIEW_PERMS = ["view_articles"]
        SAVE_PERMS = ["manage_articles"]
        LIST_DEFAULT_FILTERS = {"is_published": True}
        GRAPHS = {
            "default": {"fields": ["id", "title", "created"]},
            "detail":  {"fields": ["id", "title", "body", "author", "created"]},
        }
```

**2. Expose it as a REST endpoint:**

```python
from mojo import decorators as md
from .models.article import Article

@md.URL('article')
@md.uses_model_security(Article)
def on_article(request, pk=None):
    return Article.on_rest_request(request, pk)
```

That's it. You now have list, retrieve, create, update, and delete — with object-level permissions, graph-controlled output, filtering, pagination, and auth — all from two files.

---

## Authentication Out of the Box

Django-MOJO ships a complete auth stack. Nothing to install or wire up separately.

- **JWT** — access + refresh tokens, configurable TTLs, device-aware
- **OAuth** — Google, Apple, and extensible to any provider
- **TOTP** — authenticator apps, backup codes, recovery flow
- **Passkeys** — WebAuthn discoverable credentials
- **API Keys** — per-user, revocable, scoped
- **Sessions** — list and revoke active sessions
- **Security events** — login history, suspicious activity, lockout guard

```python
# Login — returns JWT access + refresh tokens
POST /api/account/login
{"username": "user@example.com", "password": "..."}

# All auth flows use the same bearer token middleware
Authorization: Bearer <token>
```

---

## Secure Settings

Secrets belong in the database, not in your repo. Django-MOJO's settings helper reads from a lookup chain: Redis cache → DB (group-scoped → global) → Django file settings.

```python
from mojo.helpers.settings import settings

# Static config — read from file, safe at import time
HEADER = settings.get_static("DUID_HEADER", "x-mojo-duid")

# Dynamic secrets — read from DB/Redis at call time
def send_sms(to, body):
    sid   = settings.get("TWILIO_ACCOUNT_SID", kind="str")
    token = settings.get("TWILIO_AUTH_TOKEN",  kind="str")
    ...
```

DB-backed settings activate automatically once Django is ready — no manual flag, no restart required to pick up changes.

---

## Async Jobs

```python
from mojo.apps.jobs import queue_job

# Queue a background task
queue_job("send_welcome_email", user_id=user.pk)

# Define the handler
from mojo.apps.jobs import job_handler

@job_handler("send_welcome_email")
def handle_welcome_email(job):
    user = User.objects.get(pk=job.data["user_id"])
    ...
```

Built-in: priority channels, retries with backoff, idempotency keys, scheduled jobs, worker heartbeat, and a REST API for job status.

---

## Realtime WebSockets

```python
# Client subscribes to a topic
ws.send({"action": "subscribe", "topic": "user:123"})

# Server pushes to a topic from anywhere
from mojo.apps.realtime import publish
publish("user:123", {"type": "notification", "message": "You have a new message"})
```

Same JWT authentication as HTTP. No extra config.

---

## Project Layout

```
mojo/
├── apps/
│   ├── account/     # Users, groups, auth (JWT, OAuth, TOTP, passkeys, API keys)
│   ├── jobs/        # Async job queue and scheduler
│   ├── realtime/    # WebSocket pub/sub (Django Channels)
│   ├── fileman/     # File upload, S3 backend, renditions
│   ├── filevault/   # Encrypted file vault
│   ├── logit/       # Request + database logging
│   ├── incident/    # Security incident tracking and alerting
│   ├── metrics/     # Redis-backed time-series metrics
│   ├── phonehub/    # SMS and phone number management
│   ├── shortlink/   # URL shortening
│   ├── docit/       # Wiki and documentation pages
│   └── aws/         # SES email, SNS, S3 helpers
├── helpers/
│   ├── settings/    # DB-backed settings with encryption
│   ├── crypto/      # Signing, hashing, token generation
│   ├── logit/       # Structured logging
│   ├── geoip/       # IP geolocation and threat detection
│   ├── content_guard/ # Deterministic content moderation
│   └── ...          # dates, redis, request, response, paths, and more
├── middleware/      # CORS, auth, request logging
├── models/          # MojoModel, RestMeta base classes
├── serializers/     # Serialization engine and cache
└── rest/            # OpenAPI, decorators, routing
```

---

## Documentation

Docs are organized by audience and optimized for both human developers and AI coding assistants.

### [Django Developer Reference](docs/django_developer/README.md)
For developers building applications with Django-MOJO.

| Section | Description |
|---|---|
| [Core](docs/django_developer/core/README.md) | MojoModel, REST framework, decorators, middleware, serialization |
| [Helpers](docs/django_developer/helpers/README.md) | logit, dates, settings, crypto, request, redis, and other utilities |
| [Account](docs/django_developer/account/README.md) | User, Group, JWT auth, OAuth, TOTP, passkeys, API keys, sessions |
| [Logging](docs/django_developer/logging/README.md) | Database logging, security incidents |
| [Files](docs/django_developer/files/README.md) | File upload, storage backends, renditions |
| [Email](docs/django_developer/email/README.md) | AWS SES email, templates, inbound handling |
| [Jobs](docs/django_developer/jobs/README.md) | Async task queue |
| [Metrics](docs/django_developer/metrics/README.md) | Redis-backed time-series metrics |
| [Realtime](docs/django_developer/realtime/README.md) | WebSocket pub/sub |
| [PhoneHub](docs/django_developer/phonehub/README.md) | Phone number management and SMS |
| [FileVault](docs/django_developer/filevault/README.md) | Encrypted file vault |
| [DocIt](docs/django_developer/docit/README.md) | Documentation and wiki system |

### [REST API Reference](docs/web_developer/README.md)
For frontend and mobile developers integrating with Django-MOJO APIs.

| Section | Description |
|---|---|
| [Core](docs/web_developer/core/README.md) | Authentication, request format, filtering, pagination, graphs |
| [Account](docs/web_developer/account/README.md) | Login, users, groups, API keys |
| [Logging](docs/web_developer/logging/README.md) | Log queries, incident management |
| [Files](docs/web_developer/files/README.md) | File uploads and downloads |
| [Email](docs/web_developer/email/README.md) | Templates, mailboxes |
| [Jobs](docs/web_developer/jobs/README.md) | Job status |
| [Metrics](docs/web_developer/metrics/README.md) | Time-series metrics |
| [Realtime](docs/web_developer/realtime/README.md) | WebSocket protocol |
| [PhoneHub](docs/web_developer/phonehub/README.md) | Phone lookup and normalization |
| [FileVault](docs/web_developer/filevault/README.md) | Encrypted file vault |
| [DocIt](docs/web_developer/docit/README.md) | Documentation pages |

---

## Migrating from `django-nativemojo`

If you currently have `django-nativemojo` installed, switch with no code changes — your imports are unchanged.

```bash
pip uninstall django-nativemojo
pip install django-mojo
```

The old package name remains available as a compatibility shim for existing deployments.

---

## Contributing

Pull requests and issues are welcome. Contributions should follow the [Developer Guide](docs/developer_guide.md) and keep to the framework's philosophy: **explicit over magic, secure by default, conventions over configuration**.

---

## License

Licensed under the MIT License. See [LICENSE](LICENSE) for details.
