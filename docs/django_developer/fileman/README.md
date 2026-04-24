# Fileman — Django Developer Reference

File management with multiple storage backends (file system, S3, Azure, GCS), direct/presigned uploads, and asynchronous rendition generation.

## Contents

- [file.md](file.md) — File model, upload flow, storage backends
- [file_manager.md](file_manager.md) — FileManager configuration and backends
- [renditions.md](renditions.md) — Async rendition pipeline: handlers, channel, idempotency, adding roles.
- [shortlinks.md](shortlinks.md) — Short URLs for File/Rendition, tier-1 auto + tier-2 share, opt-out toggles.

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
| GET | `/api/fileman/rendition[/<pk>]` | Read-only rendition list / detail (supports the `share` action) |
| POST | `/api/fileman/upload/initiate` | Initiate an upload (returns File + `upload_url`) |
| POST / PUT | `/api/fileman/upload/<token>` | Direct upload body (non-S3 backends) |
| GET | `/api/fileman/download/<token>` | Token-gated download URL |

`FileRendition` supports read and the `share` action via REST. Create/delete are blocked — renditions are derived and managed through the parent `File`.

## POST_SAVE_ACTIONS on File

`File.RestMeta.POST_SAVE_ACTIONS = ["action", "regenerate_renditions", "share"]`.

| Body | Handler | Effect |
|---|---|---|
| `{"action": "mark_as_completed"}` | `on_action_action` (legacy) | Flips to completed, enqueues rendition job |
| `{"action": "mark_as_failed"}` | `on_action_action` (legacy) | Marks failed |
| `{"action": "mark_as_uploading"}` | `on_action_action` (legacy) | Marks uploading |
| `{"regenerate_renditions": true}` | `on_action_regenerate_renditions` | Enqueue regenerate of all default roles |
| `{"regenerate_renditions": ["thumbnail", …]}` | `on_action_regenerate_renditions` | Enqueue regenerate of specific roles only |
| `{"share": true \| {...}}` | `on_action_share` | Mint a tier-2 share shortlink; see `shortlinks.md` |

**Note**: the `action` key only supports the three legacy `mark_as_*` verbs. Do not extend it with new verbs — add a discrete POST_SAVE_ACTIONS key with its own `on_action_<name>` handler instead (see `docs/django_developer/core/mojo_model.md`).

## POST_SAVE_ACTIONS on FileRendition

`FileRendition.RestMeta.POST_SAVE_ACTIONS = ["share"]`. Only the share action is exposed; renditions have no lifecycle transitions.

## Short URLs

By default, `File.generate_download_url()` and `FileRendition.generate_download_url()` return a short URL (`/s/<code>`) backed by `mojo.apps.shortlink`. See [shortlinks.md](shortlinks.md) for the full pipeline, opt-out toggles, and the tier-1/tier-2 distinction.

## Storage deletion

`File.on_rest_pre_delete()` iterates `file_renditions`, deletes each rendition's storage object, deletes the original storage object, then removes auto-generated shortlink rows (`source__in=["fileman", "fileman-share"]`). Human-created shortlinks pointing at the file (other `source` values) are preserved — their `file` FK goes NULL via `SET_NULL`.

## Renditions

See [renditions.md](renditions.md).

The short version:

- The renderer system lives in `mojo/apps/fileman/renderer/` (image, video, audio, document).
- `File.mark_as_completed()` enqueues `mojo.apps.fileman.asyncjobs.process_file_renditions` on the `"renditions"` channel with `idempotency_key="renditions:<file_id>"`.
- The asyncjob calls `renderer.create_all_renditions(file)`, which creates every role defined in the matching renderer's `default_renditions`.
- Regeneration goes through `mojo.apps.fileman.asyncjobs.regenerate_renditions`.

## Scheduled cleanup

`fileman/cronjobs.py` schedules `cleanup_expired_files` daily, which deletes files whose `metadata.expires_at` has passed.

## Adding a new storage backend

See `mojo/apps/fileman/backends/` — each backend inherits `BaseStorageBackend` and is registered in `backends/__init__.py`.

## Permissions

- `view_fileman`, `manage_files`, `files` — domain category permission model
- `manage_files` appears in both `VIEW_PERMS` and `SAVE_PERMS` on `File`
- `FileRendition` follows the same permission set (read-only)
