# File Upload — REST API Reference

## Recommended Approach: Initiated Upload

**Always use the Initiated Upload flow unless you have a specific reason not to.**

When a client uploads a file directly via multipart POST, the file must travel through the API server — which holds open a long-lived HTTP connection for the entire transfer duration. For large files or high concurrency, this exhausts server resources quickly and adds unnecessary latency.

The Initiated Upload flow avoids this entirely:

- On **S3/cloud backends**, the client uploads directly to cloud storage via a presigned URL. The file never passes through the API server.
- On **local/other backends**, a short-lived token URL is issued and the upload goes to a dedicated endpoint, keeping the main API unblocked.

---

## Method 1 (Primary): Initiated Upload

Use this for all uploads. It works for any file size, keeps the API server free, and gives you explicit upload tracking.

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

The `upload_url` returned depends on the storage backend:
- **S3/cloud backends** — a presigned PUT URL. Upload directly to cloud storage; the file never passes through the API server.
- **Local/other backends** — `/api/fileman/upload/<token>`. POST the file to that URL.

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

### Step 4: Associate with a Model (optional)

If you want to associate the uploaded file with a model instance, set the relevant field to the returned file id after upload completes.

**POST** `/api/user/1`

```json
{
  "avatar": 124
}
```

---

## Method 2 (Fallback): Multipart Form Upload

**Use this only for small, one-off files where the initiated flow would be disproportionate overhead** — for example, a simple avatar upload in a low-traffic context.

The file travels through the API server on every request. At scale or with large files this will block server workers and degrade performance for all other clients.

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

## Method 3 (Fallback): Base64 Inline

**Use this only when embedding a small file inline with a resource creation request is genuinely simpler** — for example, uploading a tiny thumbnail alongside a form POST where a separate upload round-trip would be awkward.

Do not use this for anything large or frequently uploaded. Base64 encoding inflates file size by ~33% and the full payload passes through the API server.

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

---

## Selecting a FileManager

If multiple storage backends exist (e.g., a separate bucket for avatars vs. documents), pass the FileManager id in the initiate body:

```json
{
  "filename": "avatar.jpg",
  "content_type": "image/jpeg",
  "file_size": 20480,
  "file_manager": 3
}
```

If omitted, the default FileManager for the user/group is resolved automatically.

---

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
