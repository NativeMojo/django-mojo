# REST API Reference

Web developer documentation for integrating with django-mojo REST APIs.

Companion track for framework developers: [docs/django_developer/README.md](../django_developer/README.md).

## Getting Started

| Flow | Guide |
|---|---|
| User registration & onboarding | [account/ § Common Flows](account/README.md#common-flows) — register → verify email → first login |
| Securing the login page | [account/ § Securing the Login Flow](account/README.md#securing-the-login-flow) — bouncer gate, MFA, passkeys |
| Frontend integration | [frontend_starter.md](frontend_starter.md) — JWT storage, reload checks, refresh flow |
| Deploy a web app | [edge/deploy_your_webapp.md](edge/deploy_your_webapp.md) — from a web address you own to a live, HTTPS-secured site, then day-2 management |

## Core Concepts

| Section | Description |
|---|---|
| [core/](core/README.md) | Authentication, request format, filtering, pagination, graphs |
| [frontend_starter.md](frontend_starter.md) | Frontend integration starter (JWT storage, reload checks, refresh flow) |

## API Reference by App

| Section | Description |
|---|---|
| [account/](account/README.md) | Login, users, groups, API keys, push notifications |
| [logging/](logging/README.md) | Log queries, incident management |
| [fileman/](fileman/README.md) | File uploads, downloads, management |
| [email/](email/README.md) | Email templates, mailboxes, sent messages |
| [jobs/](jobs/README.md) | Job status and management |
| [metrics/](metrics/README.md) | Time-series metrics |
| [chat/](chat/README.md) | Real-time chat rooms, messages, WebSocket protocol |
| [realtime/](realtime/README.md) | WebSocket pub/sub protocol |
| [security/](security/README.md) | Security operations — incidents, events, firewall, bouncer, logs |
| [phonehub/](phonehub/README.md) | Phone and device management |
| [shortlink/](shortlink/README.md) | Short URL creation, redirect behavior, OG previews |
| [aws/cloudwatch](aws/cloudwatch.md) | Live AWS infrastructure metrics (EC2, RDS, ElastiCache) |
| [aws/guardduty](aws/guardduty.md) | Public AWS SNS endpoint that receives signed GuardDuty findings |
| [aws/s3](aws/s3.md) | Global S3 bucket inventory, private creation, access posture, and confirmed empty operations |
| [aws/maintenance](aws/maintenance.md) | Pending managed-service upgrades, applying one behind typed confirmation, and the django-mojo framework update |
| [aws/capacity](aws/capacity.md) | Adding/removing an app node, an RDS reader, or a cache replica, resizing to a curated size, and batch plan/apply — five endpoints, the operation phase ladders, and every error code |
| [aws/infrastructure_mode](aws/infrastructure_mode.md) | Which endpoints answer 403 `infrastructure_external`, the body shape, and how a client learns the installation's mode |
| [aws/admin_email](aws/admin_email.md) | Admin email management — SES domain/mailbox summary, the always-200 test send and its error vocabulary, and setting the default mailbox |
| [dnsman/](dnsman/README.md) | Domain search/purchase, DNS records, WHOIS/privacy, TLS certificates |
| [edge/](edge/README.md) | nginx vhosts and declared upstreams — how a domain gets served |
| [filevault/](filevault/README.md) | Encrypted file vault |
| [assistant/](assistant/README.md) | LLM admin assistant — natural language queries, conversations |
| [assistant/approvals](assistant/approvals.md) | Resolving a mutating assistant action — the `approval` block, both transports, the one failure contract |
| [assistant/webapp_tools](assistant/webapp_tools.md) | The assistant's `webapp` domain from a client's side — result shapes, what each approval card binds, the fresh-auth split, and the handoffs |
| [github/](github/README.md) | GitHub App installations — list, create, update, delete |
| [docit/](docit/README.md) | Documentation system |

## Quick Reference

### Authentication

```
Authorization: Bearer <jwt-token>
```

### Standard Response Envelope

```json
{"status": true, "data": {...}}
{"status": true, "count": 42, "start": 0, "size": 10, "data": [...]}
{"status": false, "code": 403, "error": "Permission denied", "is_authenticated": true}
```

### Common Query Parameters

| Param | Description |
|---|---|
| `graph` | Response shape (e.g., `?graph=basic`) |
| `start` / `size` | Pagination (e.g., `?start=0&size=20`) |
| `sort` | Sort field, prefix `-` for descending |
| `search` | Full-text search |
| `group` | Group context for scoped resources |
| `dr_start` / `dr_end` | Date range filter — accepts ISO datetime or partial date (`YYYY`, `YYYY-MM`, `YYYY-MM-DD`); see [Filtering](core/filtering.md#date-range-filter) |
| `timezone` | IANA timezone name (e.g. `America/Los_Angeles`) — anchors partial-date expansion and CSV localization |
| `_mode` | Aggregation mode (`count`, `top`, `distinct`, `summary`, `histogram`) — see [Aggregation](core/aggregation.md) |
| `_stats` | With `_mode=count`: JSON object of named filter bundles → one count each (batched stat strips) — see [Aggregation](core/aggregation.md#mode_count) |
| `download_format` | Export format (e.g., `?download_format=csv`) |
