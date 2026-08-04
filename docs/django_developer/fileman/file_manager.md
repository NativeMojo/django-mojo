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

`python manage.py aws-check --section s3 --check` audits the system-default S3
manager, bucket access/region, Public Access Block and CORS without persisting
audit state. `--apply --bucket-name <name>` can create a missing private bucket
and system manager. `--probe-s3` is separately confirmed and uses only a unique
sentinel key that is deleted in `finally`; it never lists or deletes user
objects. Existing buckets and policies are preserved.

Configure via FileManager settings or in `settings.py`:

```python
AWS_ACCESS_KEY_ID = "..."
AWS_SECRET_ACCESS_KEY = "..."
AWS_STORAGE_BUCKET_NAME = "my-bucket"
AWS_S3_REGION_NAME = "us-east-1"
```

#### Public-access reconciliation

For a user-scoped S3 manager, `is_public` is a stored classification that must
agree with the storage service. FileManager performs a policy-level check when
`get_for_user()` provisions or retrieves a personal manager. The first real
file or rendition URL then supersedes policy-only evidence once with a stronger
object-level check. An existing manager can also enter directly through that
object-level path when one of its files is resolved after upgrade.

The internal `public_access_audit` field stores versioned evidence with one of
three statuses:

- `public` — anonymous access was conclusively established; `is_public` is repaired to `True` and unsigned URLs are allowed.
- `private` — anonymous access was conclusively denied; `is_public` is repaired to `False`.
- `unknown` — AWS could not establish either result. The stored `is_public` value is preserved, but download behavior fails closed to a presigned URL.

The audit uses an anonymous HEAD probe after authenticated S3 access confirms a
real object exists. An anonymous 403 is enough to disprove manager-wide public
access. A successful probe proves only that one object is readable, so the
manager is classified public only when policy evidence also shows an
unconditional anonymous `s3:GetObject` allow covering the entire prefix, no
matching deny can override it, and effective bucket/account Public Access Block
settings do not restrict the existing policy. Before any object exists, that
same conservative policy evidence is used by itself. The audit never adds or
broadens bucket policy statements.

Audit metadata carries a one-way fingerprint of the backend URL/type and
effective connection settings. Changing those FileManager inputs invalidates
the evidence. Normal reads do not use an hourly TTL and do not poll AWS after
the current evidence is established.

Bucket/account policy or Public Access Block changes made through AWS Console,
Terraform, or another path outside FileManager are not observable from the
model. Force a bulk policy refresh after such a change:

```bash
python manage.py reconcile_fileman_public_access
python manage.py reconcile_fileman_public_access --dry-run
```

The command audits active user-scoped S3 managers independently, reports
`public`/`private`/`unknown` totals, and continues if one manager fails. Dry-run
performs the read-only AWS inspection without changing `is_public` or audit
metadata.

### Backend interface — `download(file_path, local_path)`

All backends used by the renderer pipeline (image, vector, video, audio, document) must implement:

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

If no `FileManager` exists for a group or user, the system-wide default is used
automatically. Set `is_default=True` on one FileManager to designate it. A new
user manager inherits the system manager's public/private value and reconciles
its derived S3 prefix before it is returned.
