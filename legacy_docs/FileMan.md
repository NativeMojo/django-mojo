# Django-MOJO File Manager (mojo.fileman): Saving to a group's file manager

This guide shows how to save files to a group's FileManager entirely from Python (no REST required), with concise, copy-pasteable examples.

What you get:
- Automatic group-scoped storage (each group gets its own prefix/path)
- A single API to persist files regardless of backend (local FS, S3, etc.)
- Download URLs and automatic rendition creation for images/videos (if enabled)

Important
- A system-default FileManager should exist. The first time you save for a group, a group FileManager is auto-provisioned from the system default (you don't need to call `FileManager.get_for_group` yourself).
- All examples assume you already have a `group` instance and, when applicable, a `user` instance.


## Core concepts

- Group FileManager
  - Usually you don't need to call this; `File.create_from_file(..., user=user, group=group)` auto-resolves and auto-provisions. Use `FileManager.get_for_group(group)` only when you need to manage or inspect the manager directly.
  - The group's storage prefix is derived automatically from the system default (e.g., appending the group's UUID to the base path/bucket prefix).

- Saving files
  - Prefer `File.create_from_file(file_obj, name, user=user, group=group)`.
  - For Django `UploadedFile` objects (from `request.FILES`), this works out of the box.
  - For in-memory files (e.g., `BytesIO`), set `name`, `size`, and `content_type` attributes on the object before calling.

- Download URLs
  - `file.url` returns a URL. If the group’s FileManager is public, you get a stable public URL. If private, you get a signed URL with an expiry.

- Cleanup
  - `file.delete()` removes the DB record and also deletes the underlying file and any generated renditions from storage.


## Example 1: Save a file from a Django view (multipart/form-data)

```/dev/null/examples.py#L1-80
from mojo.apps.fileman.models import File, FileManager

def upload_view(request):
    # Assumes "file" was uploaded via <input type="file" name="file" />
    uploaded = request.FILES["file"]

    # Resolve your group (example patterns)
    group = getattr(request, "group", None)  # MOJO sets this when group param is used
    # Or: group = request.user.groups.first()  (if your app models group membership differently)

    # No need to fetch a FileManager; create_from_file will resolve and auto-provision

    # Persist to the group's file manager
    rec = File.create_from_file(uploaded, uploaded.name, user=request.user, group=group)

    # Use it
    print("Stored path:", rec.storage_file_path)
    print("Category:", rec.category)  # e.g., image, document, video
    print("Download URL:", rec.url)   # public or signed URL depending on FileManager.is_public

    # Return or render as needed
    return {"id": rec.id, "filename": rec.filename, "url": rec.url}
```


## Example 2: Save an in-memory file (BytesIO)

When you don’t have a Django `UploadedFile`, you can use `BytesIO`. Set `name`, `size`, and `content_type` so the pipeline can determine the filename, MIME type, and size.

```/dev/null/examples.py#L82-170
from io import BytesIO
from mojo.apps.fileman.models import File

def save_in_memory_bytes(group, user):
    data = b"hello from memory"
    f = BytesIO(data)

    # Required attributes for create_from_file + on_rest_save_file:
    f.name = "greeting.txt"
    f.size = len(data)
    f.content_type = "text/plain"

    rec = File.create_from_file(f, f.name, user=user, group=group)

    print("Saved:", rec.filename, rec.file_size, rec.content_type)
    print("URL:", rec.url)
    return rec
```


## Example 3: Mark a file public/private and get URLs

Public FileManager → stable public URLs. Private FileManager → signed, expiring URLs.

```/dev/null/examples.py#L172-250
from mojo.apps.fileman.models import FileManager

def toggle_manager_visibility(group, make_public=False):
    fm = FileManager.get_for_group(group)

    fm.is_public = bool(make_public)
    # Saving triggers backend helpers that can adjust bucket/object ACLs where possible
    fm.save()

    return fm.is_public

# Later, file.url reflects the visibility:
# - Public manager: stable public URL
# - Private manager: signed URL (expires)
```


## Example 4: List, download URL, and delete

```/dev/null/examples.py#L252-360
from mojo.apps.fileman.models import File

def list_and_cleanup(group):
    # List completed files for the group
    files = File.objects.filter(group=group, upload_status=File.COMPLETED, is_active=True).order_by("-created")

    for f in files:
        print(f"id={f.id} name={f.filename} type={f.content_type} url={f.url}")

    # Delete one (removes renditions and storage objects too)
    if files:
        victim = files[0]
        victim.delete()
        print("Deleted:", victim.id)
```


## Example 5: Save base64 content as a file

If you receive base64 content (e.g., from a service), decode it, wrap it in a `BytesIO`, and set `name`, `size`, `content_type`.

```/dev/null/examples.py#L362-470
import base64
from io import BytesIO
from mojo.apps.fileman.models import File

def save_base64_to_group(group, user, b64_data, filename="upload.bin", content_type="application/octet-stream"):
    # Strip a Data URL prefix if present: "data:<mime>;base64,<payload>"
    if b64_data.startswith("data:") and "," in b64_data:
        _, b64_data = b64_data.split(",", 1)

    raw = base64.b64decode(b64_data)

    f = BytesIO(raw)
    f.name = filename
    f.size = len(raw)
    f.content_type = content_type

    rec = File.create_from_file(f, f.name, user=user, group=group)
    return rec
```


## How the group selection works

- `File.create_from_file(..., group=group)` delegates to `FileManager.get_for_user_group(user, group)`.
- If `group` is provided, the group’s default FileManager is used (ignores user).
- If the group has no manager yet, one is created by inheriting from the system-default FileManager. The group’s storage prefix (e.g., S3 key prefix or local path) is derived automatically from the parent manager’s `backend_url` plus a group-specific component.

This gives you group-level isolation in storage while keeping configuration DRY.


## Tips and gotchas

- Always supply `content_type` when saving in-memory files. If you don’t, MOJO will guess from the filename, but explicit is better for correct category/renditions.
- `file.url` is lazy and cached; it respects `FileManager.is_public`.
- Deletion cleans up renditions and the original file path for you.
- For programmatic backend actions (e.g., copying/moving within storage), go through `fm.backend`, but for typical use just persist via `File.create_from_file(...)`.
- If your app uses request-bound flows:
  - Keep using the same APIs; MOJO will pick `request.group` when available and automatically route to that group’s FileManager.


## Minimal end-to-end snippet

```/dev/null/examples.py#L472-560
from mojo.apps.fileman.models import File, FileManager

def save_to_group(group, user, uploaded_file):
    # No need to fetch a FileManager; create_from_file will resolve and auto-provision

    # Save the upload to this group's storage
    rec = File.create_from_file(uploaded_file, uploaded_file.name, user=user, group=group)

    return {
        "id": rec.id,
        "filename": rec.filename,
        "size": rec.file_size,
        "category": rec.category,
        "url": rec.url,
        "storage_path": rec.storage_file_path,
    }
```

## Multiple managers per group with "use"

Groups can have multiple file managers. The default manager is used when you call `File.create_from_file(..., user=user, group=group)` without specifying a manager. To route files to a specific purpose (e.g., "invoices"), use the `use` key:

- Programmatic (auto-provision if missing):
```/dev/null/examples.py#L562-660
from mojo.apps.fileman.models import File, FileManager

def save_invoice_to_group(group, user, uploaded_file):
    # Get (or auto-provision) the group's "invoices" manager under the group's namespace
    invoices_manager = FileManager.get_for_group(group, use="invoices")

    # Save directly to that manager
    rec = File.create_from_file(
        uploaded_file,
        uploaded_file.name,
        user=user,
        group=group,
        file_manager=invoices_manager,
    )
    return rec
```

- REST: target a specific manager by "use" (also accepts aliases like `fileman_use` or `file_manager_use`)
```/dev/null/request.json#L1-40
{
  "use": "invoices",
  "files": [
    {
      "filename": "inv-1001.pdf",
      "content_type": "application/pdf",
      "size": 12345
    }
  ]
}
```

Notes:
- When a `use` is provided and the manager does not exist, it is created automatically by inheriting credentials/settings from the system default, with a group-specific path and an additional "`use`" subpath.
- Continue using the default manager for general uploads; specify `use` only when you want a distinct storage area (e.g., invoices, reports, media).
