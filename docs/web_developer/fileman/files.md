# File API — REST API Reference

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/fileman/file` | List files |
| POST | `/api/fileman/file` | Upload / create file |
| GET | `/api/fileman/file/<id>` | Get file |
| POST/PUT | `/api/fileman/file/<id>` | Update file metadata |
| DELETE | `/api/fileman/file/<id>` | Delete file |

## Permissions

- `view_fileman` or `manage_files`

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

To rebuild renditions (e.g., after changing FileManager settings), POST to the file with the `regenerate_renditions` action.

**POST** `/api/fileman/file/123`

Regenerate all default renditions:

```json
{ "action": "regenerate_renditions" }
```

Regenerate only specific roles:

```json
{
  "action": "regenerate_renditions",
  "roles": ["thumbnail", "preview"]
}
```

The call returns immediately; the actual work runs on the background worker. Only the named roles (or all, if `roles` is omitted) are replaced.

## Upload Token Direct Endpoint

For non-S3 backends, the framework provides a token-based upload endpoint:

**POST** `/api/fileman/upload/<upload_token>`

Used when the File record returns `/api/fileman/upload/<token>` as the `upload_url`. Upload the file binary to this URL.
