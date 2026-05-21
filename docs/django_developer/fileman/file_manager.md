# FileManager Model — Django Developer Reference

## Overview

`FileManager` is the storage backend configuration. Each `File` belongs to a `FileManager` that determines where and how files are stored (local disk, AWS S3, etc.).

## Key Concepts

- One or more `FileManager` instances exist per deployment
- A `FileManager` can be scoped to a `User`, a `Group`, or neither (system-wide default)
- Backends are pluggable: local, S3, and others

## Ownership and Scoping

`FileManager` supports three scopes: user-owned, group-scoped, and system-wide. Because of this, `FileManager.RestMeta` sets `CREATED_BY_OWNER_FIELD = None` — the framework's create-time auto-stamping of the `user` field is **disabled**.

Behavior on REST create (`POST /api/fileman/manager`):

| Request body | `user` on created record |
|---|---|
| `user` omitted | `None` — group- or system-scoped manager |
| `user: null` | `None` |
| `user: <id>` | That user's id — user-scoped manager |

`group` auto-fill from `request.group` is **not** affected and works normally.

### System-scoped creation is superuser-only

A manager created with **no `user` and no `group`** is system-scoped — it is eligible to become the system default that `get_for_user` / `get_for_group` derive every other manager from. `FileManager.on_rest_pre_save` rejects this via REST unless the requester is a superuser, raising `PermissionDeniedException` (HTTP 403). Direct ORM creation (`FileManager.objects.create(...)`, bootstrap code, the internal `get_for_*` provisioning helpers) does not go through `on_rest_pre_save` and bypasses this guard.

## Getting the Right FileManager

```python
from mojo.apps.fileman.models import FileManager

# From the current request (resolves group, then system default)
fm = FileManager.get_from_request(request)

# For a specific user/group
fm = FileManager.get_for_user_group(user, group)
```

## FileManager Settings

Each `FileManager` has a `settings` JSONField for backend-specific configuration:

```python
# Access a setting
expiry = fm.get_setting("urls_expire_in", 3600)  # URL expiry in seconds
is_pub = fm.is_public                             # Whether files are publicly accessible
root = fm.root_path                               # Storage root path/bucket
```

### Shortlink settings

Three optional keys control shortlink behavior for files and renditions in this manager:

| Key | Default | Description |
|---|---|---|
| `use_shortlinks` | `None` (inherit global) | Force-on (`True`) or force-off (`False`) shortlink wrapping for this manager. `None` defers to the global `FILEMAN_USE_SHORTLINKS` setting (default `True`). |
| `shortlink_track_clicks` | `False` | When `True`, tier-1 shortlinks (auto-generated display URLs) log each click as a `ShortLinkClick` record. |
| `shortlink_expire_days` | `0` (never) | Lifetime in days for tier-1 shortlinks. `0` means the shortlink never expires. |

These settings apply only to tier-1 (auto-generated) links. Tier-2 share links set their own `expire_days` and `track_clicks` per call and do not consult these settings.

```python
fm.set_setting("use_shortlinks", False)          # disable short URLs for this manager
fm.set_setting("shortlink_track_clicks", True)   # log clicks on display URLs
fm.set_setting("shortlink_expire_days", 90)      # expire tier-1 links after 90 days
fm.save()
```

See [shortlinks.md](shortlinks.md) for the full shortlink pipeline and opt-out behavior.

## Storage Backends

### Local Backend

Files stored on local filesystem. No presigned URLs — uses the framework's upload endpoint.

### S3 Backend

Files stored in AWS S3. Supports:
- Presigned upload URLs (direct browser-to-S3 uploads)
- Presigned download URLs with configurable TTL
- Public bucket support

Configure via FileManager settings or in `settings.py`:

```python
AWS_ACCESS_KEY_ID = "..."
AWS_SECRET_ACCESS_KEY = "..."
AWS_STORAGE_BUCKET_NAME = "my-bucket"
AWS_S3_REGION_NAME = "us-east-1"
```

### Backend interface — `download(file_path, local_path)`

All backends used by the renderer pipeline (image, video, audio, document) must implement:

```python
def download(self, file_path: str, local_path: str) -> None:
    """Copy the stored file at file_path to a local filesystem path."""
```

The renderers download the original file to a temp path before processing. The local backend (`FileSystemStorageBackend`) and the S3 backend both implement this. If you write a custom backend, implement `download()` or rendition generation will fail for that backend's files.

See `mojo/apps/fileman/backends/` — each backend inherits `BaseStorageBackend`.

## Direct Upload Flow (S3)

```python
# 1. Create File record
file = File(filename="report.pdf", file_size=102400, content_type="application/pdf")
file.file_manager = FileManager.get_from_request(request)
file.on_rest_pre_save({}, True)
file.save()

# 2. Get presigned upload URL
upload_url = file.request_upload_url()
# Return upload_url to the client

# 3. Client uploads directly to S3
# 4. Client confirms: POST /api/fileman/file/<id> with action=mark_as_completed
```

## Multiple FileManagers per Group

A group can have multiple named FileManagers (e.g., `"avatars"`, `"documents"`). Specify by name:

```python
fm = FileManager.get_for_user_group(user, group, use="avatars")
```

REST clients pass `?use=avatars` to select a specific manager.

## Auto-Provisioning

If no `FileManager` exists for a group, the system-wide default is used automatically. Set `is_default=True` on one FileManager to designate it.
