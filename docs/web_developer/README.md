# REST API Reference

Web developer documentation for integrating with django-mojo REST APIs.

Companion track for framework developers: [docs/django_developer/README.md](../django_developer/README.md).

## Core Concepts

| Section | Description |
|---|---|
| [core/](core/README.md) | Authentication, request format, filtering, pagination, graphs |

## API Reference by App

| Section | Description |
|---|---|
| [account/](account/README.md) | Login, users, groups, API keys |
| [logging/](logging/README.md) | Log queries, incident management |
| [files/](files/README.md) | File uploads, downloads, management |
| [email/](email/README.md) | Email templates, mailboxes, sent messages |
| [jobs/](jobs/README.md) | Job status and management |
| [metrics/](metrics/README.md) | Time-series metrics |
| [realtime/](realtime/README.md) | WebSocket protocol |
| [phonehub/](phonehub/README.md) | Phone and device management |
| [shortlink/](shortlink/README.md) | Short URL creation, redirect behavior, OG previews |
| [filevault/](filevault/README.md) | Encrypted file vault |
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
| `dr_start` / `dr_end` | Date range filter |
| `download_format` | Export format (e.g., `?download_format=csv`) |
