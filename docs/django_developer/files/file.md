# File Model — Django Developer Reference

## Overview

`File` tracks uploaded files with metadata and delegates storage to a `FileManager` backend (local, S3, etc.). Each file goes through an upload lifecycle: `pending` → `uploading` → `completed` (or `failed`/`expired`).

## Inheritance

```python
class File(models.Model, MojoModel):
```

## Key Fields

| Field | Type | Description |
|---|---|---|
| `filename` | CharField | User-provided filename |
| `storage_filename` | CharField | Generated unique filename in storage |
| `storage_file_path` | TextField | Full path in storage backend |
| `content_type` | CharField | MIME type |
| `category` | CharField | Auto-detected: `image`, `document`, `video`, etc. |
| `file_size` | BigIntegerField | Size in bytes |
| `checksum` | CharField | MD5/SHA256 checksum |
| `upload_status` | CharField | `pending`, `uploading`, `completed`, `failed`, `expired` |
| `upload_token` | CharField | Unique token for tracking direct uploads |
| `is_public` | BooleanField | Publicly accessible without auth |
| `is_active` | BooleanField | Active/accessible flag |
| `metadata` | JSONField | Arbitrary file metadata |
| `group` | FK → Group | Owning group |
| `user` | FK → User | Uploader |
| `file_manager` | FK → FileManager | Storage backend config |
| `download_url` | TextField | Cached persistent download URL |

## Upload Flow

### Option 1: Direct Upload (Multipart)

POST a multipart form with the file. The framework handles storage automatically.

```python
# REST handler
@md.POST('upload')
def on_upload(request):
    file = request.FILES.get("file")
    instance = File.create_from_file(file, file.name, request=request)
    return instance.on_rest_get(request)
```

### Option 2: Presigned URL (S3/Cloud)

1. Create a File record to get a presigned upload URL
2. Client uploads directly to storage
3. Client confirms completion

```python
file = File(filename="report.pdf", file_size=102400)
file.file_manager = FileManager.get_from_request(request)
file.save()
url = file.request_upload_url()
# Return url to client for direct upload
```

### Option 3: Base64 Inline (in JSON payload)

When a related model has a `ForeignKey` to `File`, you can pass base64-encoded data inline:

```json
{
  "name": "My Product",
  "avatar": "data:image/png;base64,iVBORw0KGgo..."
}
```

The framework calls `File.on_rest_related_save()` automatically to decode and store the file.

## Creating Files Programmatically

```python
from mojo.apps.fileman.models import File

# From a Django UploadedFile
file_instance = File.create_from_file(
    file=uploaded_file,
    name=uploaded_file.name,
    request=request,
    user=request.user,
    group=request.group
)

# From BytesIO (e.g., generated file)
import io
buf = io.BytesIO(b"PDF content here...")
buf.name = "report.pdf"
buf.size = len(buf.getvalue())
buf.content_type = "application/pdf"
file_instance = File.create_from_file(buf, "report.pdf", user=user, group=group)
```

## Download URLs

```python
url = file_instance.generate_download_url()
```

- Public files return a permanent URL
- Private files return a time-limited presigned URL (configurable TTL via `urls_expire_in` setting on FileManager)
- The URL is cached in `download_url` for public files

## Upload Status Management

```python
file.mark_as_uploading(commit=True)
file.mark_as_completed(file_size=1024, checksum="abc123", commit=True)
file.mark_as_failed(error_message="Storage error", commit=True)
```

`mark_as_completed` also triggers rendition creation automatically.

## Renditions

Renditions are alternate versions of a file (thumbnail, resized image, PDF preview):

```python
thumbnail_url = file_instance.thumbnail  # URL of thumbnail rendition
renditions = file_instance.renditions    # objict of all renditions by role
rendition = file_instance.get_rendition_by_role("thumbnail")
```

## File on Related Models

Use `ForeignKey` to `fileman.File` on any model to link files:

```python
class Product(models.Model, MojoModel):
    image = models.ForeignKey("fileman.File", null=True, on_delete=models.SET_NULL)
```

The framework will automatically handle inline base64 upload when `image` is passed as a data URL in a REST POST.

## RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["view_fileman", "manage_files"]
    CAN_DELETE = True
    GRAPHS = {
        "basic": {"fields": ["id", "filename", "content_type", "category"], "extra": ["url", "thumbnail"]},
        "default": {"extra": ["url", "renditions"]},
        "upload": {"fields": ["id", "filename", "content_type", "file_size", "upload_url"]},
        "list": {"extra": ["url", "renditions"], "graphs": {"group": "basic", "user": "basic"}},
    }
```

## Metadata

```python
file_instance.get_metadata("width")            # returns None if not set
file_instance.set_metadata("width", 1920)
file_instance.save()
```

## Access Control

```python
can_access = file_instance.can_be_accessed_by(user=request.user, group=request.group)
```
