# Fileman Framework Alignment Refactor

**Type**: request
**Status**: planned
**Date**: 2026-04-23
**Priority**: high

## Description

Refactor `mojo/apps/fileman/` to align with django-mojo framework conventions. The app currently mixes (1) abandoned Celery plumbing, (2) custom REST endpoints that duplicate RestMeta CRUD, (3) inline blocking rendition work that bypasses the native `mojo.apps.jobs` system, and (4) stale utility code referencing model fields that no longer exist.

The tangible symptom is that **video renditions never appear** — the Celery path is dead (never wired, broken import, no celery dep) and the fallback synchronous path blocks the upload request on ffmpeg transcoding, so it times out or fails silently.

This refactor treats fileman as a first-class django-mojo app: RestMeta CRUD + `POST_SAVE_ACTIONS` for per-instance operations, `jobs.publish` + `asyncjobs.py` for background work, and a thin utils layer.

## Context

- User reported video renditions are not being rendered.
- Investigation surfaced much deeper framework-drift in fileman.
- Celery is **only** used in fileman across the entire repo (12 other apps use `mojo.apps.jobs`). Celery is not even a declared dependency.
- `fileman/cronjobs.py` + `fileman/asyncjobs.py` already follow the native pattern — the rest of fileman has never been migrated.

## Acceptance Criteria

- `fileman/tasks.py` (Celery) deleted.
- `fileman/signals.py` (Celery-dispatching signal handlers) deleted.
- No `from celery` imports remain anywhere in `mojo/apps/fileman/`.
- All rendition work runs via `jobs.publish(func="mojo.apps.fileman.asyncjobs.<handler>", ...)`.
- `File.mark_as_completed()` does not block on ffmpeg — it enqueues a job and returns.
- Video, image, audio, and document renditions all appear for newly completed uploads (verified end-to-end against the dev backend).
- Custom `/upload/initiate` endpoint replaced by RestMeta `POST /fileman/file` create + appropriate `on_rest_pre_save` / `on_rest_get` graph ("upload").
- Per-instance operations (mark_as_completed, mark_as_failed, regenerate_renditions) exposed via `POST_SAVE_ACTIONS` with discrete `on_action_<name>` methods — replace the single ambiguous `on_action_action` dispatch.
- Token-based `/upload/<token>` (PUT/POST body upload) and `/download/<token>` endpoints kept as non-RestMeta endpoints, since they use `@md.custom_security("requires upload token")` — but moved into `rest/upload.py` cleanly and documented.
- Dead functions in `utils/upload.py` removed (`initiate_upload`, `finalize_upload`, `get_file_manager`, `validate_file_request` if unused) — they reference fields (`uploaded_by`, `original_filename`, `file_path`, `upload_expires_at`, `is_upload_expired`, `generate_unique_filename`) that no longer exist on `File`.
- `@md.uses_model_security(Model)` present on every RestMeta URL endpoint in `rest/fileman.py`.
- `VIEW_PERMS`/`SAVE_PERMS` on `File` and `FileRendition` include the `files` domain category permission (already present — verify).
- `FileRendition.RestMeta` tightened: `CAN_CREATE = False`, `CAN_DELETE = False` from direct REST (renditions are created by the renderer system; delete via File cascade or regenerate action). Read-only via REST.
- Docs updated in both `docs/django_developer/fileman/` and `docs/web_developer/fileman/`.
- `CHANGELOG.md` entry added describing the breaking REST changes.

## Investigation

### What exists

- **Dead Celery layer**
  - `fileman/tasks.py` — 4 `@shared_task` funcs (`process_file_renditions`, `cleanup_renditions`, `regenerate_renditions`, `process_bulk_renditions`). Imports `process_new_file` from `renderer`, which does not exist.
  - `fileman/signals.py` — post_save/post_delete handlers calling `.delay()`. Not loaded: `fileman/apps.py:14` has `# from . import signals` commented out.
  - Celery is not in `pyproject.toml`. No other app in the codebase uses it.

- **Working native-job layer (template for the rest)**
  - `fileman/cronjobs.py` uses `@schedule(...)` + `jobs.publish(func="mojo.apps.fileman.asyncjobs.cleanup_expired_files", ...)`.
  - `fileman/asyncjobs.py` defines `cleanup_expired_files(job)` handler.

- **Renderer system** — `fileman/renderer/` has functional Image/Video/Audio/Document renderers. `VideoRenderer` exists and is wired into `RENDERERS`. Currently only invoked via the synchronous `File.create_renditions()` path.

- **Synchronous blocking rendition**
  - `File.mark_as_completed()` (file.py:333) calls `self.create_renditions()` inline.
  - `create_renditions()` calls `renderer.create_all_renditions(self)` which runs ffmpeg/Pillow synchronously inside the upload-complete request. For video, this reliably times out.

- **REST surface**
  - `rest/fileman.py` — RestMeta CRUD for `FileManager` and `File`. Missing `@md.uses_model_security(Model)` on both endpoints (will 404 under current framework).
  - `rest/upload.py` — custom `/upload/initiate`, `/upload/<token>`, `/download/<token>`. `upload/initiate` duplicates what `POST /fileman/file` + `on_rest_pre_save` + "upload" graph already do.
  - `rest/qrcode.py` — separate feature, out of scope.

- **Model action dispatch**
  - `File.POST_SAVE_ACTIONS = ["action"]` with a single `on_action_action(self, action)` that switches on string values. Works, but the documented idiom is discrete `POST_SAVE_ACTIONS = ["mark_completed", "mark_failed", "regenerate_renditions", ...]` each with its own `on_action_<name>`.

- **Stale utils**
  - `utils/upload.py` — `initiate_upload()` and `finalize_upload()` reference fields not on the current model (`uploaded_by`, `original_filename`, `file_path`, `upload_expires_at`, `is_upload_expired`, `generate_unique_filename`). These functions are not imported anywhere in the live code path. `direct_upload` and `get_download_url` **are** live and must be preserved.

### What changes

**Delete**
- `mojo/apps/fileman/tasks.py`
- `mojo/apps/fileman/signals.py`
- Dead functions in `mojo/apps/fileman/utils/upload.py`: `get_file_manager`, `validate_file_request`, `initiate_upload`, `finalize_upload` (keep `direct_upload`, `get_download_url`).

**Modify**
- `mojo/apps/fileman/asyncjobs.py` — add:
  - `process_file_renditions(job)` — reads `file_id` from job payload, runs `renderer.create_all_renditions(file)`.
  - `cleanup_renditions(job)` — reads `file_id`, deletes renditions + storage.
  - `regenerate_renditions(job)` — reads `file_id`, optional `roles`.
- `mojo/apps/fileman/models/file.py`
  - `mark_as_completed()` — replace inline `self.create_renditions()` with `jobs.publish(func="mojo.apps.fileman.asyncjobs.process_file_renditions", channel="renditions", payload={"file_id": self.id})`.
  - Remove `create_renditions()` helper (or keep as a sync test-only utility — decide during design).
  - `on_rest_pre_delete` already calls backend delete synchronously; consider moving rendition-folder cleanup to `cleanup_renditions` asyncjob for consistency.
  - Replace `POST_SAVE_ACTIONS = ["action"]` + `on_action_action` with discrete actions:
    - `on_action_mark_completed(self, value)`
    - `on_action_mark_failed(self, value)`
    - `on_action_mark_uploading(self, value)`
    - `on_action_regenerate_renditions(self, value)` — value is optional list of roles
- `mojo/apps/fileman/rest/fileman.py`
  - Add `@md.uses_model_security(FileManager)` and `@md.uses_model_security(File)` on the two handlers.
- `mojo/apps/fileman/rest/upload.py`
  - Remove `/upload/initiate` endpoint. Clients create files via `POST /fileman/file` with the "upload" graph and read `upload_url` from the response.
  - Keep `/upload/<token>` (PUT/POST body) and `/download/<token>` as-is — they use token-based custom security and are not CRUD-shaped.
- `mojo/apps/fileman/models/rendition.py`
  - `RestMeta`: set `CAN_CREATE = False`, `CAN_DELETE = False`. Renditions are derived, not user-created.
- `mojo/apps/fileman/apps.py`
  - Leave signals-import commented out / remove comment entirely. No Django signals needed.

**Verify / minor**
- `File.RestMeta.VIEW_PERMS` and `SAVE_PERMS` already include `"files"` domain cat — OK.
- `FileRendition.RestMeta.VIEW_PERMS` already include `"files"` — OK.
- Ensure `renditions` extra field in graphs still works after async path (there will be a window where a completed File has no renditions yet — document this for web_developer).

### Constraints

- **Breaking REST changes**: `/upload/initiate` goes away, `on_action_action` goes away. Needs CHANGELOG entry and web_developer docs update.
- **Ordering**: rendition job must only enqueue after the File row commits, otherwise the async worker races the transaction. Use `transaction.on_commit(lambda: jobs.publish(...))` inside `mark_as_completed`.
- **Idempotency**: `process_file_renditions` should skip roles that already exist unless regenerate is requested.
- **No celery dep added**. Period.
- **Backend consistency**: some code uses `file_manager.backend`, other paths use `get_backend(file_manager)`. Standardize on `file_manager.backend` (already the model-level idiom).

### Related files

- `mojo/apps/fileman/apps.py`
- `mojo/apps/fileman/tasks.py` (delete)
- `mojo/apps/fileman/signals.py` (delete)
- `mojo/apps/fileman/asyncjobs.py`
- `mojo/apps/fileman/cronjobs.py`
- `mojo/apps/fileman/models/file.py`
- `mojo/apps/fileman/models/rendition.py`
- `mojo/apps/fileman/rest/fileman.py`
- `mojo/apps/fileman/rest/upload.py`
- `mojo/apps/fileman/utils/upload.py`
- `mojo/apps/fileman/renderer/__init__.py`
- `mojo/apps/fileman/renderer/video.py` (verify ffmpeg path)
- `docs/django_developer/fileman/*`
- `docs/web_developer/fileman/*`
- `CHANGELOG.md`

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | `fileman/manager` | List file managers | `view_fileman` / `manage_files` / `files` |
| GET/POST | `fileman/manager/<int:pk>` | Read/update file manager | `view_fileman` / `manage_files` / `files` |
| GET | `fileman/file` | List files | `view_fileman` / `manage_files` / `files` |
| POST | `fileman/file` | **Create file (replaces `/upload/initiate`)** — request `graph=upload` to get `upload_url` in response | `manage_files` / `files` |
| GET/POST/DELETE | `fileman/file/<int:pk>` | CRUD on file; POST supports action fields (see below) | `manage_files` / `files` |
| PUT/POST | `fileman/upload/<token>` | Direct body upload (no auth, token-gated) | custom_security |
| GET | `fileman/download/<token>` | Token-gated download URL | custom_security |

### POST_SAVE_ACTIONS on File

| Action field | Effect |
|---|---|
| `mark_completed: true` | Runs `mark_as_completed`, enqueues rendition job |
| `mark_failed: "<msg>"` | Runs `mark_as_failed` with optional error message |
| `mark_uploading: true` | Runs `mark_as_uploading` |
| `regenerate_renditions: true \| ["video_mp4", ...]` | Enqueues `regenerate_renditions` asyncjob |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| (existing) `urls_expire_in` | 3600 | Presigned download URL TTL — no change |
| (new) `rendition_channel` | `"renditions"` | `jobs.publish` channel used for rendition work — allows ops to dedicate a worker |

## Tests Required

- `tests/test_fileman/renditions_async.py`
  - Completing a file upload enqueues a rendition job (assert row created in `jobs`, not synchronous renderer call).
  - Running the asyncjob creates expected renditions for an image fixture.
  - Running the asyncjob creates expected renditions for a small video fixture (gated on ffmpeg availability — skip if absent).
  - Regenerate action deletes then re-creates specified roles only.
- `tests/test_fileman/rest_crud.py`
  - `POST /fileman/file` with `graph=upload` returns `upload_url`.
  - `POST /fileman/file/<id>` with `mark_completed: true` transitions state and enqueues job.
  - `DELETE /fileman/file/<id>` enqueues rendition cleanup.
  - `FileRendition` REST is read-only (POST/DELETE rejected).
- `tests/test_fileman/migration_smoke.py`
  - No module under `mojo/apps/fileman/` imports `celery`.
  - `fileman/tasks.py` and `fileman/signals.py` do not exist.

## Out of Scope

- Changes to `rest/qrcode.py`.
- Changes to storage backends (`backends/`).
- Replacing token-based `/upload/<token>` and `/download/<token>` with RestMeta — they legitimately need custom security and are not shaped like CRUD.
- Adding new rendition roles or changing ffmpeg parameters.
- Webhook-based upload completion from S3 (could be a follow-up request).
- Reworking `FileManager` model — only its REST decorator is touched.

## Plan

**Status**: planned
**Planned**: 2026-04-23

### Objective
Move rendition work to `mojo.apps.jobs` via `asyncjobs`, delete the dead Celery layer and stale utils, and tighten RestMeta — while preserving `/upload/initiate`, the token upload/download endpoints, and the existing `{"action": "..."}` dispatch so the current UI keeps working.

### Steps

1. Delete `mojo/apps/fileman/tasks.py` — Celery, dead (broken import, never wired).
2. Delete `mojo/apps/fileman/signals.py` — Celery dispatch, never wired.
3. `mojo/apps/fileman/apps.py` — remove commented `from . import signals` line; `ready()` stays no-op.
4. `mojo/apps/fileman/asyncjobs.py` — add:
   - `process_file_renditions(job)` — reads `file_id` from `job.payload`; no-op if file missing or not completed; calls `renderer.create_all_renditions(file)`.
   - `regenerate_renditions(job)` — reads `file_id` and optional `roles`; deletes matching renditions then recreates.
5. `mojo/apps/fileman/models/file.py`
   - `mark_as_completed()` — replace inline `self.create_renditions()` with `transaction.on_commit(lambda: jobs.publish("mojo.apps.fileman.asyncjobs.process_file_renditions", {"file_id": self.id}, channel="renditions", idempotency_key=f"renditions:{self.id}", max_exec_seconds=1800))`.
   - Remove now-unused `create_renditions()` helper.
   - Keep `POST_SAVE_ACTIONS = ["action"]` and `on_action_action`; extend dispatch to accept `"regenerate_renditions"` (publishes the regenerate job; reads optional `roles` from `self.active_request.DATA`).
6. `mojo/apps/fileman/rest/fileman.py` — add `@md.uses_model_security(FileManager)` and `@md.uses_model_security(File)` on the two endpoints.
7. `mojo/apps/fileman/rest/upload.py` — unchanged. `/upload/initiate`, `/upload/<token>`, `/download/<token>` preserved.
8. `mojo/apps/fileman/models/rendition.py` — `RestMeta`: `CAN_CREATE = False`, `CAN_DELETE = False`.
9. `mojo/apps/fileman/utils/upload.py` — delete dead funcs referencing nonexistent model fields: `get_file_manager`, `validate_file_request`, `initiate_upload`, `finalize_upload`. Keep `direct_upload` and `get_download_url`.
10. `mojo/apps/fileman/README.md` — refresh rendition pipeline section (async); leave upload docs alone.
11. Rename `docs/web_developer/files/` → `docs/web_developer/fileman/`. Update link in `docs/web_developer/README.md:28` (`[files/]` → `[fileman/]`).
12. `docs/web_developer/fileman/files.md` — Renditions section: note async creation, `renditions` map may be `{}` briefly after completion, document `"regenerate_renditions"` action value.
13. `docs/django_developer/fileman/` (new) — `README.md` + `renditions.md` covering async pipeline, channel, idempotency, how to add a rendition role.
14. `CHANGELOG.md` — entry: renditions moved to async `mojo.apps.jobs`; `regenerate_renditions` action added; Celery code removed (internal).
15. `tests/test_fileman/4_test_renditions_async.py` (new) — assert `mark_as_completed` enqueues a Job row; handler creates renditions for a tiny PNG; video rendition gated on `shutil.which("ffmpeg")`.
16. `tests/test_fileman/5_test_no_celery.py` (new) — assert `tasks.py`/`signals.py` absent; walk `mojo/apps/fileman/` and assert no `from celery` / `import celery`.
17. After changes: `bin/create_testproject` then `bin/run_tests --agent`.

### Design Decisions

- Preserve `/upload/initiate` — purpose-built for S3 presigned flow; UI depends on it.
- Preserve `{"action": "..."}` dispatch — non-breaking extension for `regenerate_renditions`.
- `transaction.on_commit` before `jobs.publish` — prevents worker reading pre-commit state.
- `idempotency_key=f"renditions:{file_id}"` — collapses duplicate publishes.
- Channel `"renditions"` — isolates heavy ffmpeg from default workers.
- Keep sync storage-delete on DELETE — small backend call, no regression.
- `FileRendition` read-only via REST — derived data.
- Rename `web_developer/files/` → `web_developer/fileman/` — folder name matches URL prefix and avoids clash with `files` permission category.

### Edge Cases

- ffmpeg missing — `VideoRenderer._check_ffmpeg` logs; handler isolates per-role failures so one bad renderer does not abort others.
- File deleted before job runs — handler catches `File.DoesNotExist` and returns cleanly.
- Renditions not yet ready on client read — documented; client polls or re-fetches when `upload_status == "completed"` and `renditions` is empty.
- Rapid re-mark_completed — idempotency key dedupes; existing renditions short-circuit in `renderer.get_rendition`.
- Long-running video — `max_exec_seconds=1800` advisory visibility.

### Testing

- `tests/test_fileman/4_test_renditions_async.py`
  - `mark_as_completed(commit=True)` on a completed file creates a `Job` row with func `mojo.apps.fileman.asyncjobs.process_file_renditions` and the expected payload.
  - Running the handler directly on a tiny PNG creates the default image renditions.
  - POSTing `{"action": "regenerate_renditions"}` with `{"roles": ["thumbnail"]}` enqueues the regenerate job; handler replaces only the named role.
  - Video rendition test skipped when `shutil.which("ffmpeg") is None`.
- `tests/test_fileman/5_test_no_celery.py`
  - `mojo/apps/fileman/tasks.py` and `mojo/apps/fileman/signals.py` do not exist.
  - No `.py` file under `mojo/apps/fileman/` matches `^(from|import)\s+celery`.
- Existing `tests/test_fileman/2_test_fileman.py` passes unchanged (preserved contracts).

### Docs

- `docs/web_developer/README.md` — link update only.
- `docs/web_developer/fileman/` (renamed from `files/`) — renditions async note + `regenerate_renditions` action.
- `docs/django_developer/fileman/README.md` (new) — overview, model/action map, rendition pipeline.
- `docs/django_developer/fileman/renditions.md` (new) — async flow, channel, idempotency, adding a role.
- `mojo/apps/fileman/README.md` — rendition section refresh.
- `CHANGELOG.md` — entry.

## Resolution

**Status**: resolved
**Date**: 2026-04-23
**Commits**: 53f63eb (refactor), 9cde9a2 (security + docs follow-up)

### What Was Built

Renditions in fileman are no longer generated inline during the upload-complete request. `File.mark_as_completed()` now enqueues a job on `mojo.apps.jobs` (`process_file_renditions` on the `renditions` channel) via `transaction.on_commit`, with an idempotency key so duplicate publishes collapse to one job. The dead Celery layer (`tasks.py`, `signals.py` — never wired, broken import, no declared dep) was removed. A non-breaking `regenerate_renditions` POST_SAVE_ACTION was added with sanitized, length-capped `roles` input. The UI-critical `/upload/initiate`, `/upload/<token>`, and `/download/<token>` endpoints were preserved as-is, and the existing `{"action": "..."}` dispatch was extended rather than replaced.

Pre-existing renderer bugs that prevented renditions from ever working on local backends (and on audio/video/document renditions even on S3) were fixed as part of the change: `FileSystemStorageBackend.download(file_path, local_path)` was added, and audio/video/document renderers were corrected to pass a path instead of a file handle to `backend.download`.

REST was tightened: `@md.uses_model_security(Model)` on both RestMeta endpoints, and `FileRendition` is now read-only (`CAN_CREATE=False`, `CAN_DELETE=False`). `utils/upload.py` lost four dead functions that referenced model fields that no longer exist.

Docs folders renamed from `docs/{django,web}_developer/files/` to `.../fileman/` to match the URL prefix and stop colliding with the `files` permission category.

### Files Changed

- `mojo/apps/fileman/tasks.py` — **deleted** (dead Celery)
- `mojo/apps/fileman/signals.py` — **deleted** (dead Celery)
- `mojo/apps/fileman/apps.py` — removed commented signals import
- `mojo/apps/fileman/asyncjobs.py` — added `process_file_renditions` and `regenerate_renditions` handlers
- `mojo/apps/fileman/models/file.py` — async publish via `transaction.on_commit` + idempotency key; added `publish_renditions`, `publish_regenerate_renditions` (with sanitized roles + MAX_REGENERATE_ROLES=20 cap); extended `on_action_action` to dispatch `regenerate_renditions`; rewrote `on_rest_pre_delete` to walk `file_renditions` rows (layout-agnostic cleanup)
- `mojo/apps/fileman/models/rendition.py` — `CAN_CREATE=False`, `CAN_DELETE=False`
- `mojo/apps/fileman/rest/fileman.py` — `@md.uses_model_security` on both endpoints
- `mojo/apps/fileman/utils/upload.py` — deleted 4 dead functions, kept `direct_upload` + `get_download_url`
- `mojo/apps/fileman/backends/filesystem.py` — added `download(file_path, local_path)`
- `mojo/apps/fileman/renderer/{audio,video,document}.py` — pass `temp_path` to `backend.download` (was a stale file-handle pattern)
- `mojo/apps/fileman/README.md` — added a "Renditions (async)" section
- `bin/create_testproject` + `testproject/config/settings/local/__init__.py` — registered `renditions` in `JOBS_CHANNELS`

### Tests

- `tests/test_fileman/4_test_renditions_async.py` — job enqueue, idempotency, image-rendition handler creation, role-scoped regenerate, skips on missing/incomplete files, video-renderer import gate.
- `tests/test_fileman/5_test_no_celery.py` — `tasks.py`/`signals.py` absent; walks `mojo/apps/fileman/` to reject any `from celery` / `import celery`.
- `tests/test_fileman/2_test_fileman.py` — unchanged; contracts preserved.
- Run: `bin/run_tests --agent -t test_fileman` — 59/59 pass locally.
- Full suite post-build: 1,777/1,833 pass (56 opt-in/conditional skips), zero cross-app regressions.

### Docs Updated

- `docs/django_developer/README.md`, `docs/web_developer/README.md`, top-level `README.md` — link updates from `files/` to `fileman/`.
- `docs/django_developer/fileman/README.md` (new) — overview, model/action map, rendition pipeline entry point.
- `docs/django_developer/fileman/renditions.md` (new) — full async pipeline: handlers, channel, idempotency, edge cases, adding a new role.
- `docs/django_developer/fileman/file.md` — replaced stale sync-rendition wording with accurate async description; documented `publish_renditions` / `publish_regenerate_renditions`.
- `docs/django_developer/fileman/file_manager.md` — documented the `download(file_path, local_path)` backend contract.
- `docs/web_developer/fileman/files.md` — updated Renditions section with async-timing warning and `regenerate_renditions` action.
- `docs/web_developer/fileman/upload.md` — note that `upload_status = completed` does not mean renditions are ready.
- `mojo/apps/fileman/README.md` — added "Renditions (async)" section referencing the new docs.
- `CHANGELOG.md` — entry under v1.1.0.

### Security Review

Three findings from the initial review:

1. **LOW — unbounded/untyped `roles` list** → fixed in 9cde9a2: `publish_regenerate_renditions` coerces entries to stripped strings, drops non-strings, caps at `MAX_REGENERATE_ROLES` (20).
2. **NONE — filesystem backend `download()` path traversal** → `_get_full_path` already normalizes and rejects escapes; `local_path` is framework-internal, never user-data.
3. **MEDIUM — `on_rest_pre_delete` targeted the wrong path** → fixed in 9cde9a2: rewritten to iterate `file_renditions` rows and delete each `storage_path`, independent of renderer layout. Original file deletion also wrapped in try/except so one failure does not block cleanup of the rest.

Remaining review items (auth, FileRendition read-only, new RestMeta decorator) returned **NONE** — the changes close an existing gap rather than open one.

### Follow-up

- None. All acceptance criteria met; renderer layout inconsistency (image subfolder vs flat video/audio/document) was worked around at the cleanup side. Normalizing that layout could be a future cleanup but is out of scope here.
