# File Upload — REST API Reference

## Upload Methods

Three methods are supported depending on file size and storage backend.

---

## Method 1: Multipart Form Upload

Best for small/medium files via a single request.

**POST** `/api/fileman/file` (multipart/form-data)

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -F "file=@/path/to/document.pdf" \
  https://api.example.com/api/fileman/file
```

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 123,
    "filename": "document.pdf",
    "content_type": "application/pdf",
    "file_size": 102400,
    "upload_status": "completed",
    "url": "https://storage.example.com/files/document_a1b2c3d4.pdf",
    "category": "document"
  }
}
```

---

## Method 2: Initiated Upload (S3 presigned or direct token)

Best for large files or when you need explicit upload tracking. Initiate first to get an `upload_url`, then upload, then confirm.

The `upload_url` returned depends on the storage backend:
- **S3/cloud backends** — returns a presigned PUT URL. Client uploads directly to cloud storage; the file never passes through the API server.
- **Local/other backends** — returns `/api/fileman/upload/<token>`. Client POSTs the file to that URL.

### Step 1: Initiate Upload

**POST** `/api/fileman/upload/initiate`

```json
{
  "filename": "large-video.mp4",
  "content_type": "video/mp4",
  "file_size": 524288000
}
```

Optional params: `file_manager` (id), `group` (id), `metadata` (object).

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 124,
    "filename": "large-video.mp4",
    "content_type": "video/mp4",
    "file_size": 524288000,
    "upload_url": "https://s3.amazonaws.com/bucket/file_xyz?X-Amz-Signature=..."
  }
}
```

### Step 2a: Upload to Presigned URL (S3/cloud backends)

```bash
curl -X PUT \
  -H "Content-Type: video/mp4" \
  --data-binary @large-video.mp4 \
  "https://s3.amazonaws.com/bucket/file_xyz?X-Amz-Signature=..."
```

### Step 2b: Upload to Direct Token URL (local/other backends)

When `upload_url` starts with `/api/fileman/upload/`, POST the file directly:

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -F "file=@large-video.mp4" \
  "https://api.example.com/api/fileman/upload/<token>"
```

### Step 3: Confirm Upload

**POST** `/api/fileman/file/124`

```json
{
  "action": "mark_as_completed"
}
```

---

## Method 3: Base64 Inline

Include a file as base64 data within a JSON POST to a related resource. The file is automatically decoded and stored.

```json
{
  "name": "Alice Smith",
  "avatar": "data:image/jpeg;base64,/9j/4AAQSkZJRgAB..."
}
```

The `avatar` field must be a `ForeignKey` to `fileman.File` on the model.

---

## Upload Status Values

| Status | Meaning |
|---|---|
| `pending` | File record created, upload not started |
| `uploading` | Upload in progress |
| `completed` | File stored successfully |
| `failed` | Upload failed |
| `expired` | Upload token expired |

## Selecting a FileManager

If multiple storage backends exist (e.g., separate bucket for avatars vs. documents), pass the FileManager id in the initiate body:

```json
{
  "filename": "avatar.jpg",
  "content_type": "image/jpeg",
  "file_size": 20480,
  "file_manager": 3
}
```

If omitted, the default FileManager for the user/group is resolved automatically.

## Group-Scoped Uploads

To associate a file with a group, pass `group` in the initiate body:

```json
{
  "filename": "report.pdf",
  "content_type": "application/pdf",
  "file_size": 51200,
  "group": 7
}
```
