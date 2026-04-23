# Fileman — Django Developer Reference

File management with multiple storage backends (file system, S3, Azure, GCS), direct/presigned uploads, and asynchronous rendition generation.

## Contents

- [file.md](file.md) — File model, upload flow, storage backends
- [file_manager.md](file_manager.md) — FileManager configuration and backends
- [renditions.md](renditions.md) — Async rendition pipeline: handlers, channel, idempotency, adding roles.

## Models

| Model | Purpose |
|---|---|
| `FileManager` | Backend configuration — bucket, root path, size/MIME restrictions, per-group defaults |
| `File` | Uploaded file — lifecycle status, storage path, metadata, rendition accessors |
| `FileRendition` | Derived artifact (thumbnail, preview, transcoded video, etc.) linked to a `File` |

Located in `mojo/apps/fileman/models/`. Each has `RestMeta` wiring the standard MojoModel REST surface.

## Upload lifecycle

```
 PENDING → UPLOADING → COMPLETED → (async) renditions populated
                     ↘ FAILED / EXPIRED
```

Transitions are model methods:

- `mark_as_uploading(commit=False)`
- `mark_as_completed(file_size=None, checksum=None, commit=False)` — enqueues async rendition job
- `mark_as_failed(error_message=None, commit=False)`
- `mark_as_expired()`

`mark_as_completed()` verifies the file exists on the backend. If present, status becomes `completed` and a rendition job is enqueued via `transaction.on_commit`. Otherwise the file is marked `failed`.

## REST endpoints

| Method | Path | Description |
|---|---|---|
| GET / POST | `/api/fileman/manager[/<pk>]` | File manager CRUD |
| GET / POST / DELETE | `/api/fileman/file[/<pk>]` | File CRUD |
| POST | `/api/fileman/upload/initiate` | Initiate an upload (returns File + `upload_url`) |
| POST / PUT | `/api/fileman/upload/<token>` | Direct upload body (non-S3 backends) |
| GET | `/api/fileman/download/<token>` | Token-gated download URL |

`FileRendition` has no REST endpoint — renditions are derived and managed by the renderer + the `regenerate_renditions` action on `File`.

## POST_SAVE_ACTIONS on File

`File.RestMeta.POST_SAVE_ACTIONS = ["action"]`. Dispatched by `on_action_action`:

| `action` value | Effect |
|---|---|
| `"mark_as_completed"` | Run `mark_as_completed(commit=True)` — flips to completed and enqueues rendition job |
| `"mark_as_failed"` | Run `mark_as_failed(commit=True)` |
| `"mark_as_uploading"` | Run `mark_as_uploading(commit=True)` |
| `"regenerate_renditions"` | Publish a regenerate job; optional `roles: [...]` in the same body to scope it |

## Renditions

See [renditions.md](renditions.md).

The short version:

- The renderer system lives in `mojo/apps/fileman/renderer/` (image, video, audio, document).
- `File.mark_as_completed()` enqueues `mojo.apps.fileman.asyncjobs.process_file_renditions` on the `"renditions"` channel with `idempotency_key="renditions:<file_id>"`.
- The asyncjob calls `renderer.create_all_renditions(file)`, which creates every role defined in the matching renderer's `default_renditions`.
- Regeneration goes through `mojo.apps.fileman.asyncjobs.regenerate_renditions`.

## Storage deletion

`File.on_rest_pre_delete()` removes both the primary storage object and the rendition folder synchronously on DELETE — keep this in mind for models that cascade.

## Scheduled cleanup

`fileman/cronjobs.py` schedules `cleanup_expired_files` daily, which deletes files whose `metadata.expires_at` has passed.

## Adding a new storage backend

See `mojo/apps/fileman/backends/` — each backend inherits `BaseStorageBackend` and is registered in `backends/__init__.py`.

## Permissions

- `view_fileman`, `manage_files`, `files` — domain category permission model
- `manage_files` appears in both `VIEW_PERMS` and `SAVE_PERMS` on `File`
- `FileRendition` follows the same permission set (read-only)
