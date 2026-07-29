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
- `vector.py` — SVG rasterized to PNG, then handed to the image path. See [SVG rasterization](#svg-rasterization) below.
- `video.py` — ffmpeg-based thumbnails and transcodes. Warns on missing ffmpeg; per-role exceptions are isolated.
- `audio.py` — ffmpeg-based waveform/transcode.
- `document.py` — PDF page previews via poppler/ImageMagick.

Dispatch: `renderer.get_renderer_for_file(file)` returns the first renderer that claims the file. Most renderers claim on `supported_categories` containing `file.category`; `VectorRenderer` overrides `supports_file` to match on **content type** instead and is registered first, so SVG is routed before `ImageRenderer` can pick it up on `category == "image"`.

## SVG rasterization

SVG uploads produce PNG renditions with the same roles and sizes as any raster image. The original SVG is never modified — only the renditions are PNG.

### Install

Rasterization is an **optional extra**:

```bash
pip install django-mojo[svg]
```

Without it, SVG files upload normally and simply get no renditions (a warning naming the extra is logged). **A plain `pip install django-mojo` upgrade does not enable this** — downstream deployments that want SVG thumbnails must change their install line.

The extra pulls `resvg-py`, which ships self-contained wheels (manylinux/musllinux x86_64 + aarch64, macOS, Windows; cp310–cp315) with zero runtime dependencies. **No system libraries are required.**

### Why resvg

Both `resvg-py` and `cairosvg` were probed against hostile inputs before choosing. Two findings drove the design:

- **Neither engine is safe in-process.** resvg renders SVG filters correctly, and that is exactly what makes it bombable: `feMorphology radius=800` is a 326-byte document that runs for over 40 seconds. cairosvg is immune to that only because it does not implement filters at all — it silently drops them, so any SVG with a drop shadow or blur renders *wrong* — and it has its own bomb (a 300k-segment path ran over 45 seconds) plus a `RecursionError` on deep nesting. A hard wall-clock timeout is mandatory either way, and a Python-level timeout cannot interrupt a native call, so the work has to run in a subprocess.
- Given a subprocess is required regardless, resvg wins on correctness and deployment: it renders filters properly and needs no system libraries, where cairosvg requires `libcairo2` on every downstream install.

`librsvg` and ImageMagick delegate chains are deliberately **not** used — that combination is behind the long ImageTragick CVE tail.

Trade-off worth knowing: `resvg-py` is a single-maintainer wrapper around the upstream `resvg` Rust crate. That is the reason it is an optional extra rather than a core dependency, and the reason it runs behind a process boundary.

### Security posture

| Property | How |
|---|---|
| No script execution | resvg has no JavaScript engine at all — not a flag |
| No network access | resvg has no network stack; external `<image href>`, remote CSS and web fonts are simply not fetched |
| No XXE | External XML entities are not resolved; the refusal message does not echo the referenced path |
| No local file reads | `resources_dir` is never passed, so `file://` references load nothing |
| No credentials in reach | The child gets a minimal environment — no DB or cloud credentials — and imports no Django, no ORM |

The child is launched **by absolute path**, never `python -m mojo.apps.fileman.renderer.svg_raster`: `-m` imports the parent packages first and hits a module-scope settings read in `mojo/helpers/request.py`. Every `mojo.*` import in `svg_raster.py` therefore sits inside a function body.

### Caps

Five independent caps, each covering a bomb the others miss:

| Setting | Default | Stops |
|---|---|---|
| `FILEMAN_SVG_MAX_BYTES` | `2097152` (2 MB) | oversized documents and large `data:` payloads |
| `FILEMAN_SVG_MAX_EMBEDDED_PIXELS` | `40000000` (40 Mpx) | a `data:`-URI raster bomb — a 1.6 MB SVG embedding a 20000×20000 PNG passes the byte cap *and* the timeout, then decodes to multiple GB |
| `FILEMAN_SVG_RASTER_BOX` | `1024` | an absurd `viewBox`/`width` — 100000×100000 renders for ~2 minutes unbounded |
| `FILEMAN_SVG_TIMEOUT` | `15` | filter bombs (`feMorphology`, `feTurbulence`) — tiny inputs on a tiny canvas |
| `FILEMAN_SVG_MEMORY_MB` | `512` | backstop only — `RLIMIT_AS` in the child |

Two of these deserve care if you are changing this code:

- **The raster box is passed as BOTH width and height.** Passing `width` alone preserves aspect ratio without bounding the other axis: a 40×4000 SVG at `width=150` renders 150×**15000**.
- **`FILEMAN_SVG_MEMORY_MB` is a Linux-only backstop, not the memory control.** macOS cannot set `RLIMIT_AS` at all (`setrlimit` raises `ValueError: current limit exceeds maximum limit`), so on a Mac dev box that cap does not exist and cannot be tested. `FILEMAN_SVG_MAX_EMBEDDED_PIXELS` is the portable control against memory bombs — do not remove it on the assumption that `RLIMIT_AS` has it covered.

The rasterized PNG and any refusal are memoized per renderer instance, so a bomb costs one timeout per file rather than one per requested role.

### Behavior

- Rendition roles, sizes and crop modes are inherited from `ImageRenderer` — SVG gets exactly what a raster image gets.
- Output is PNG with alpha preserved (correct for logos), regardless of the role's usual format.
- Roles larger than `FILEMAN_SVG_RASTER_BOX` are upscaled from the raster. No default role is.
- `.svgz` (gzip-compressed SVG) is **not supported** — it is refused on its magic bytes as a decompression-bomb vector.
- Content is sniffed, not trusted. A payload that is not an SVG document falls back to the raster path, so a real PNG uploaded as `logo.svg` (browsers set the content type from the extension) still gets a thumbnail. Every other refusal is terminal.
- Any refusal degrades exactly like a file with no renderer: no rendition row, no exception out of the job, a logged warning.
- SVG text needs fonts on the worker. A slim container with no system fonts renders text with glyphs missing — cosmetic, not a failure.

### Backfilling existing SVGs

SVGs uploaded before this shipped have no renditions. Rebuild per file with the existing action:

```json
POST /api/fileman/file/123
{"regenerate_renditions": true}
```

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
| `resvg-py` missing on worker | SVG renditions are skipped with a warning naming `django-mojo[svg]`. The job still completes; other files are unaffected. |
| Malicious or malformed SVG | Refused by one of the five caps. No rendition row, no exception out of the job — identical to a file with no renderer. |
| File deleted before job runs | Handler catches `DoesNotExist`, returns `"completed:skipped=file-missing"`. |
| Client reads file before renditions ready | `renditions` map is empty `{}`. Client should poll or re-fetch. |
| Same file completed twice quickly | Idempotency key collapses to one job; even if executed, renderer skips existing roles. |
| Storage backend unavailable during rendition | Renderer logs error for the failed role; other roles proceed. Rerun via `regenerate_renditions`. |

## Developer utilities

- `File.publish_renditions()` — enqueue the default-renditions job for this file.
- `File.publish_regenerate_renditions(roles=None)` — enqueue regenerate with optional role filter.
- `renderer.create_all_renditions(file)` — synchronous creation (use only in tests / scripts).
