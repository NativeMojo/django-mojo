# Assistant File and Image Analysis Tools

**Type**: request
**Status**: open
**Date**: 2026-04-05
**Priority**: medium

## Description

Add file query, metadata, and image analysis tools to the assistant. The headline feature is `analyze_image` — a tool that reads an image file from storage and sends it to Claude's vision capability for description and analysis. This lets admins ask the assistant to interpret screenshots, error captures, or security evidence attached to tickets and incidents.

## Context

Files are already attached to tickets (`TicketNote.media` FK) and incidents (`IncidentHistory.media` FK) as `fileman.File` records. Admins frequently attach screenshots of errors, suspicious activity, or UI issues. Today the assistant can see that an attachment exists (via the `"media": "basic"` graph) but cannot look at the image content. With these tools, an admin could say "analyze the screenshot from ticket note #3" and get a detailed description of what the image shows — error messages, stack traces, UI state, IP addresses in logs, etc.

The fileman app supports multiple storage backends (filesystem, S3, Azure, GCS) and all backends implement an `open()` method that returns file bytes. This means the analyze tool can work regardless of where the file is stored.

## Design

### Tool 1: `query_files`

List and search files by type, name, user, date range.

- **Parameters**: `search` (filename/content_type text search), `category` (image/video/document/etc.), `content_type` (exact mime type), `user_id`, `status` (default: completed), `size` (max results, default 20)
- **Permission**: `view_admin` or `files`
- **Returns**: List of file summaries (id, filename, content_type, category, file_size, created, user basic info)
- **Uses**: `File.objects.filter(...)` with the File model's existing `SEARCH_TERMS` and `category` field
- **mutates**: False

### Tool 2: `get_file`

Get detailed metadata for a single file. Does NOT return file contents — returns metadata plus a URL the frontend can use to display/download.

- **Parameters**: `file_id` (required)
- **Permission**: `view_admin` or `files`
- **Returns**: id, filename, content_type, category, file_size, upload_status, created, modified, is_public, url (generated download URL), user (basic), group (basic), file_manager (basic), renditions, metadata dict
- **Uses**: `File.objects.get(pk=file_id)` then `file.rest_serialize(graph="detailed")`
- **mutates**: False

### Tool 3: `analyze_image`

Analyze an image file using Claude's multimodal vision capability. This is the key tool.

- **Parameters**: `file_id` (required), `prompt` (optional — custom analysis instruction, defaults to general description)
- **Permission**: `view_admin` (stricter — this reads actual file bytes and makes an LLM call)
- **mutates**: False (read-only analysis)

**Flow**:
1. Load `File` record by id, verify `upload_status == "completed"`
2. Verify the file is an image: check `category == "image"` or `content_type.startswith("image/")`
3. Validate content type is a supported vision format: `image/jpeg`, `image/png`, `image/gif`, `image/webp`
4. Read file bytes via `file.file_manager.backend.open(file.storage_file_path)` then `.read()`
5. Enforce a size cap (e.g., 10 MB) to avoid sending huge images to the API
6. Base64-encode the bytes
7. Make a separate `llm.call()` with a message containing an image content block:
   ```
   [
       {"type": "image", "source": {"type": "base64", "media_type": content_type, "data": b64_data}},
       {"type": "text", "text": prompt or "Describe this image in detail. If it contains error messages, code, UI elements, or log output, transcribe and explain them. Note any IP addresses, hostnames, timestamps, or security-relevant details."}
   ]
   ```
8. Return: `{"file_id": ..., "filename": ..., "content_type": ..., "analysis": <LLM response text>}`

**Error cases**:
- File not found → clean error
- File not completed → "File upload is not completed"
- Not an image → "File is not an image (category: document, content_type: application/pdf)"
- Unsupported image format → "Image format not supported for analysis. Supported: JPEG, PNG, GIF, WebP"
- File too large → "Image exceeds 10 MB size limit for analysis"
- Backend read failure → "Could not read file from storage"

### Future consideration: Image context in conversations

When the `assistant-context-conversations` feature builds context messages from ticket notes and incident history entries, it should mention attached media. For example: "Note #3 by admin@example.com (2 hours ago): 'See attached screenshot of the error' [has image attachment: error_capture.png, id=456]". This lets the admin know they can ask "analyze the image from note #3" and the assistant can extract the file_id. This belongs in the context conversations request, not here — noted for cross-reference only.

## Acceptance Criteria

- `query_files` returns paginated file list filtered by category, content_type, user, search text
- `get_file` returns full metadata including generated download URL
- `analyze_image` reads image bytes from storage, sends to Claude vision, returns analysis text
- `analyze_image` validates: file exists, is completed, is a supported image format, is under size cap
- `analyze_image` works with both filesystem and S3 backends (both implement `open()`)
- All tools are permission-gated (`view_admin` or `files` for query/get, `view_admin` for analyze)
- Clean error messages for all failure cases
- Tool definitions registered in `tools/__init__.py`

## Investigation

**File model** (`mojo/apps/fileman/models/file.py`):
- `File.category` — auto-set from content_type via `utils.get_file_category()`, values: image, video, audio, pdf, csv, spreadsheet, document, archive, text, other
- `File.content_type` — full MIME type string (e.g., `image/png`)
- `File.upload_status` — pending/uploading/completed/failed/expired
- `File.storage_file_path` — full path in storage backend
- `File.file_size` — bytes
- `File.file_manager` FK → `FileManager` → `.backend` property → storage backend instance
- `File.url` property → calls `generate_download_url()` (pre-signed for private, direct for public)
- `File.RestMeta.GRAPHS` — "basic" (id, filename, content_type, category, url, thumbnail), "detailed" (adds renditions, user, group, file_manager)

**Storage backends** (`mojo/apps/fileman/backends/`):
- `base.py` — `StorageBackend` ABC with `open(file_path, mode='rb')` method
- `s3.py` — `open()` returns `obj.get()['Body']` (streaming body, supports `.read()`)
- `filesystem.py` — `open()` returns standard Python file object
- Both return file-like objects that support `.read()` for getting raw bytes

**Image format support** (Claude vision API):
- Supported: JPEG (`image/jpeg`), PNG (`image/png`), GIF (`image/gif`), WebP (`image/webp`)
- Max recommended size: ~20 MB per image, but we should cap lower (10 MB) for performance
- Base64 encoding increases size by ~33%, so 10 MB file becomes ~13 MB in the API payload

**Ticket/incident media FKs**:
- `TicketNote.media` → `fileman.File` (null, SET_NULL) — `mojo/apps/incident/models/ticket.py:81`
- `IncidentHistory.media` → `fileman.File` (null, CASCADE) — `mojo/apps/incident/models/history.py:40`
- Both include `"media": "basic"` in their RestMeta graphs

**Existing assistant tools** (`mojo/apps/assistant/services/tools/`):
- No file-related tools exist yet
- Pattern: each tool module exports a `TOOLS` list, registered in `__init__.py`
- Existing modules: discovery, docs, groups, jobs, logs, metrics, models, security, users, web

**LLM call pattern**:
- The assistant already uses `llm.call()` — check `mojo/apps/assistant/services/agent.py` for the existing pattern
- Image content blocks use the Messages API format with `type: "image"` and base64 source

**New file**: `mojo/apps/assistant/services/tools/files.py`

## Tests Required

- `query_files` returns files filtered by category="image"
- `query_files` returns files filtered by content_type
- `query_files` respects search text on filename
- `get_file` returns metadata with download URL for valid file
- `get_file` returns error for nonexistent file
- `analyze_image` rejects non-image file (e.g., PDF) with clear error
- `analyze_image` rejects unsupported image format (e.g., image/tiff)
- `analyze_image` rejects file with upload_status != completed
- `analyze_image` rejects file exceeding size cap
- `analyze_image` calls LLM with image content block and returns analysis text
- `analyze_image` works with custom prompt parameter
- Permission checks: user without `files` perm denied on query/get
- Permission checks: user without `view_admin` perm denied on analyze

## Out of Scope

- Uploading files through the assistant
- Deleting files through the assistant
- File renditions or thumbnail generation
- Non-image file content analysis (PDFs, spreadsheets, text files — future enhancement)
- Modifying file metadata through the assistant
- FileManager configuration through the assistant
