# Fileman Renditions — Async Pipeline

Renditions are derived artifacts (thumbnails, previews, transcoded video/audio, document pages) produced by the renderer system and stored alongside the original file.

## Pipeline

```
  mark_as_completed()
        │
        │  transaction.on_commit(...)
        ▼
  jobs.publish(
      "mojo.apps.fileman.asyncjobs.process_file_renditions",
      {"file_id": N},
      channel="renditions",
      idempotency_key=f"renditions:{N}",
      max_exec_seconds=1800,
  )
        │
        ▼  (worker on `renditions` channel)
  renderer.create_all_renditions(file)
        │
        ▼
  FileRendition rows + storage objects written
```

### Why on_commit

`transaction.on_commit` ensures the rendition worker never reads pre-commit state — the file row is guaranteed visible before the job fires. It runs immediately in autocommit mode (standalone scripts, tests), so the wrapper works everywhere.

### Why idempotency

Rapid re-posts of `{"action": "mark_as_completed"}` (double-click, client retry) collapse to a single job via `idempotency_key="renditions:<file_id>"`. The renderer itself also short-circuits roles that already exist, so repeat execution is safe.

### Why a dedicated channel

ffmpeg/Pillow work can be long and memory-heavy. Running it on the `renditions` channel lets ops point a specialized worker pool at it (e.g., `--channels renditions --max-workers 2`) without slowing the default channel.

If the `renditions` channel is not listed in `JOBS_CHANNELS`, the publish falls back to `default` with a warning — work still happens, it just shares a pool.

## Handlers

Both live in `mojo/apps/fileman/asyncjobs.py`.

### `process_file_renditions(job)`

Payload: `{"file_id": <int>}`

- No-op if file does not exist (already deleted).
- No-op if file is not in `completed` status.
- Calls `renderer.create_all_renditions(file)` which iterates the matching renderer's `default_renditions` and skips roles that already exist.

### `regenerate_renditions(job)`

Payload: `{"file_id": <int>, "roles": ["thumbnail", ...]?}`

- If `roles` is present, deletes only the matching `FileRendition` rows then recreates those specific roles.
- If `roles` is omitted, calls `cleanup_renditions()` (wipes all) then `create_all_renditions()` (recreates defaults).

Triggered via the `regenerate_renditions` POST_SAVE_ACTION on `File`:

```json
POST /api/fileman/file/123
{"regenerate_renditions": ["thumbnail"]}
```

Regenerate all default roles:

```json
POST /api/fileman/file/123
{"regenerate_renditions": true}
```

## Renderers

`mojo/apps/fileman/renderer/`:

- `image.py` — Pillow-based thumbnails and resizes.
- `video.py` — ffmpeg-based thumbnails and transcodes. Warns on missing ffmpeg; per-role exceptions are isolated.
- `audio.py` — ffmpeg-based waveform/transcode.
- `document.py` — PDF page previews via poppler/ImageMagick.

Dispatch: `renderer.get_renderer_for_file(file)` returns the first renderer whose `supported_categories` includes `file.category`.

## Adding a new rendition role

1. Add the role constant to `RenditionRole` in `renderer/base.py`.
2. Add an entry to the matching renderer's `default_renditions` mapping with its options (dimensions, bitrate, format, etc.).
3. Extend the renderer's `create_rendition` dispatch if the role needs custom handling.
4. No model migration is needed — `FileRendition.role` is a free-form string field.

Existing files can be backfilled via the `regenerate_renditions` action (per-file) or a one-off management script that iterates `File.objects.filter(upload_status="completed")` and calls `file.publish_regenerate_renditions(roles=[NEW_ROLE])`.

## Edge cases

| Scenario | Behavior |
|---|---|
| ffmpeg missing on worker | `VideoRenderer._check_ffmpeg` logs a warning. Video rendition attempts raise and are caught per-role; other renderers continue. |
| File deleted before job runs | Handler catches `DoesNotExist`, returns `"completed:skipped=file-missing"`. |
| Client reads file before renditions ready | `renditions` map is empty `{}`. Client should poll or re-fetch. |
| Same file completed twice quickly | Idempotency key collapses to one job; even if executed, renderer skips existing roles. |
| Storage backend unavailable during rendition | Renderer logs error for the failed role; other roles proceed. Rerun via `regenerate_renditions`. |

## Developer utilities

- `File.publish_renditions()` — enqueue the default-renditions job for this file.
- `File.publish_regenerate_renditions(roles=None)` — enqueue regenerate with optional role filter.
- `renderer.create_all_renditions(file)` — synchronous creation (use only in tests / scripts).
