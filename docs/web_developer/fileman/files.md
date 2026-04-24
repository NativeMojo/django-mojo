# File API — REST API Reference

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/fileman/file` | List files |
| POST | `/api/fileman/file` | Upload / create file |
| GET | `/api/fileman/file/<id>` | Get file |
| POST/PUT | `/api/fileman/file/<id>` | Update file metadata / dispatch action |
| DELETE | `/api/fileman/file/<id>` | Delete file |
| GET | `/api/fileman/rendition` | List renditions |
| GET | `/api/fileman/rendition/<id>` | Get rendition |
| POST | `/api/fileman/rendition/<id>` | Dispatch action (share) |

## Permissions

- `view_fileman` or `manage_files`

## URLs are short URLs by default

The `url` and `thumbnail` fields on File, and the `url` field on each rendition, return a short URL (e.g. `https://app.example.com/s/Xk9mR2p`) that redirects to the underlying backend URL. This is intentional:

- **S3 presigns stay fresh.** Clicking the short URL regenerates the presigned URL server-side — no expired-link pain on long-lived UIs.
- **One stable URL per resource.** You can embed, log, or share the short URL and it does not rotate across renders.

For the normal case — `<img src={file.thumbnail}>`, `<a href={file.url}>Download</a>` — no client-side change is needed. Browsers follow the redirect transparently.

If a deployment has shortlinks disabled (global `FILEMAN_USE_SHORTLINKS=False` or per-FileManager override), `url` will be the direct backend URL with no wrapping. Treat both forms as opaque.

## Get File

**GET** `/api/fileman/file/123`

```json
{
  "status": true,
  "data": {
    "id": 123,
    "filename": "document.pdf",
    "content_type": "application/pdf",
    "category": "document",
    "file_size": 102400,
    "upload_status": "completed",
    "is_public": false,
    "is_active": true,
    "created": "2024-01-15T10:00:00Z",
    "url": "https://storage.example.com/...",
    "renditions": {}
  }
}
```

## Available Graphs

| Graph | Description |
|---|---|
| `basic` | id, filename, content_type, category, url, thumbnail |
| `default` | All fields + url, renditions |
| `list` | default + group, file_manager, user |
| `upload` | id, filename, content_type, file_size, upload_url |
| `detailed` | All + nested group/user/manager |

```
GET /api/fileman/file/123?graph=detailed
GET /api/fileman/file?graph=basic&size=20
```

## List Files

```
GET /api/fileman/file?upload_status=completed&sort=-created
GET /api/fileman/file?group=7&content_type=image/jpeg
GET /api/fileman/file?search=report
```

## Update File Metadata

**POST** `/api/fileman/file/123`

```json
{
  "is_public": true,
  "metadata": {"tags": ["report", "2024"]}
}
```

## Delete a File

**DELETE** `/api/fileman/file/123`

Deletes the database record and the underlying file from storage (including all renditions).

## Renditions

Renditions (thumbnails, previews, resized images, transcoded video/audio) are created **asynchronously** after a file is marked completed. The server enqueues a background job on the `renditions` channel; the file's `upload_status` flips to `completed` immediately while rendition work runs in the background.

This means: **immediately after completion the `renditions` map may be empty `{}`**. Poll the file or re-fetch after a short delay until the map is populated.

Access via the `renditions` field or the `thumbnail` shortcut:

```json
{
  "thumbnail": "https://storage.example.com/thumbnails/img_abc.jpg",
  "renditions": {
    "thumbnail": {"url": "...", "width": 150, "height": 150},
    "preview": {"url": "...", "width": 800}
  }
}
```

### Regenerating renditions

To rebuild renditions (e.g., after changing FileManager settings), POST to the file with the `regenerate_renditions` action. **The field name is the action name** — do not wrap it in `{"action": "..."}`.

**POST** `/api/fileman/file/123`

Regenerate all default renditions:

```json
{ "regenerate_renditions": true }
```

Regenerate only specific roles:

```json
{ "regenerate_renditions": ["thumbnail", "preview"] }
```

The call returns immediately; the actual work runs on the background worker. Only the named roles (or all, if the value is `true`) are replaced.

### Sharing a file (per-share audit trail)

Every call to the `share` action mints a **new** shortlink attributed to the current user, enabling "whose link got used" audit.

**POST** `/api/fileman/file/123`

Simplest form — never-expire, no click tracking:

```json
{ "share": true }
```

With options:

```json
{
  "share": {
    "expire_days": 30,
    "track_clicks": true,
    "note": "for the Q3 review call"
  }
}
```

Response:

```json
{
  "url": "https://app.example.com/s/Xk9mR2p",
  "shortlink_code": "Xk9mR2p",
  "expires_at": "2026-05-23T18:00:00+00:00",
  "track_clicks": true,
  "code": 200,
  "server": "host1"
}
```

Fields:
- `url` — the short URL to distribute.
- `shortlink_code` — the 7-character code (useful for revocation).
- `expires_at` — ISO 8601 timestamp, or `null` if the link never expires.
- `track_clicks` — echoes whether per-click audit is enabled for this share.

`expire_days` is clamped to 3650. `note` is truncated to 512 characters.

Renditions support the same action at `POST /api/fileman/rendition/<id>` with `{"share": ...}`.

#### Listing existing shares for a file

```
GET /api/shortlink/shortlink?source=fileman-share&file=123
```

Each row carries `user` (the sharer), `hit_count`, `expires_at`, `track_clicks`, and `metadata.note`. Per-click detail lives in the `ShortLinkClick` table (when `track_clicks=True`).

#### Revoking a share

Set `is_active=false` on the shortlink row:

```
POST /api/shortlink/shortlink/<id>
{ "is_active": false }
```

Revoked short URLs return a 404/gone at the redirect endpoint. The share is audit-preserved — the row is not deleted.

## Upload Token Direct Endpoint

For non-S3 backends, the framework provides a token-based upload endpoint:

**POST** `/api/fileman/upload/<upload_token>`

Used when the File record returns `/api/fileman/upload/<token>` as the `upload_url`. Upload the file binary to this URL.
