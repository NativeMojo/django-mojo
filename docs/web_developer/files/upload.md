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

## Method 2: Presigned URL Upload (S3/Cloud)

Best for large files. Client uploads directly to cloud storage — the file never passes through the API server.

### Step 1: Create File Record

**POST** `/api/fileman/file`

```json
{
  "filename": "large-video.mp4",
  "content_type": "video/mp4",
  "file_size": 524288000
}
```

**Response (upload graph):**

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

### Step 2: Upload to Presigned URL

```bash
curl -X PUT \
  -H "Content-Type: video/mp4" \
  --data-binary @large-video.mp4 \
  "https://s3.amazonaws.com/bucket/file_xyz?X-Amz-Signature=..."
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

If multiple storage backends exist (e.g., separate bucket for avatars vs. documents), specify which to use:

```
POST /api/fileman/file?use=avatars
```

## Group-Scoped Uploads

To associate a file with a group:

```
POST /api/fileman/file?group=7
```
