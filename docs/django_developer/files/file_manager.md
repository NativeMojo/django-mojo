# FileManager Model — Django Developer Reference

## Overview

`FileManager` is the storage backend configuration. Each `File` belongs to a `FileManager` that determines where and how files are stored (local disk, AWS S3, etc.).

## Key Concepts

- One or more `FileManager` instances exist per deployment
- A `FileManager` can be scoped to a `Group` (organization-specific storage) or be a system-wide default
- Backends are pluggable: local, S3, and others

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

If no `FileManager` exists for a group, the system-wide default is used automatically. Set `is_system_default=True` on one FileManager to designate it.
