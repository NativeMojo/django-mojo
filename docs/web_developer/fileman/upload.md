# File Upload — REST API Reference

## Recommended Approach: Initiated Upload

**Always use the Initiated Upload flow unless you have a specific reason not to.**

When a client uploads a file directly via multipart POST, the file must travel through the API server — which holds open a long-lived HTTP connection for the entire transfer duration. For large files or high concurrency, this exhausts server resources quickly and adds unnecessary latency.

The Initiated Upload flow avoids proxying through the API where the backend
supports provider-direct upload:

- On **S3/cloud backends**, the client uploads directly to cloud storage via a presigned URL. The file never passes through the API server.
- On **local backends**, a short-lived bearer URL is issued and the API streams
  it to storage with a strict byte bound.

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

Optional selectors are `file_manager` (id), `group` (id), and `use` (string).
Do not send `user` or `metadata`. The filename is reduced to a basename, size
must be a nonnegative integer (booleans are invalid), and the manager's size,
extension, and MIME policies are checked before a capability is created.

`idempotency_key` is optional. It must be 1–128 ASCII letters, digits, `.`,
`_`, `:`, or `-`. Use the same key only for the same filename, normalized MIME
type, size, manager, group, and use. A changed fingerprint returns 409.

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 124,
    "filename": "large-video.mp4",
    "content_type": "video/mp4",
    "file_size": 524288000,
    "category": "video",
    "upload_status": "uploading",
    "is_active": true,
    "user_id": 19,
    "group_id": 7,
    "file_manager_id": 3,
    "upload_url": "https://storage.example/upload-capability",
    "method": "PUT",
    "fields": {},
    "headers": {"Content-Type": "video/mp4"}
  }
}
```

Treat the target as an opaque bearer capability and do not log it. Follow the
returned `method`, `fields`, and `headers`; do not infer these from URL shape.
The target depends on the storage backend:
- **S3/cloud backends** — a presigned PUT URL. Upload directly to cloud storage; the file never passes through the API server.
- **Local backends** — `/api/fileman/upload/<token>`. Multipart POST and raw
  PUT are supported. Raw PUT requires an explicit `Content-Length`, including
  `0` for an empty file.

The standard django-mojo S3 system bucket accepts these presigned requests from
any browser origin. That wildcard CORS rule is not public upload access: the
opaque presigned URL is still the short-lived upload credential, and an
unsigned request still has no S3 permission.

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
  -F "file=@large-video.mp4" \
  "https://api.example.com/api/fileman/upload/<token>"
```

The URL itself is the upload credential; no login bearer is required by that
endpoint. The body MIME type must match initiation. The server validates the
actual byte count and detected MIME type while storing, removes partial output
on failure, and leaves a successful transfer in `uploading` until Step 3.

### Step 3: Confirm Upload

**POST** `/api/fileman/file/124`

```json
{
  "action": "mark_as_completed"
}
```

The file's `upload_status` becomes `completed` immediately. Repeating this
request is safe and does not publish rendition work twice. Renditions
(thumbnails, previews, etc.) are generated **asynchronously** in the background
— re-fetch after a short delay if you need them.

The response is a capability-free lifecycle object. It omits upload tokens and
targets, storage paths, metadata, download URLs, and renditions.

**Who may call this:** the user who **initiated** the upload (files are stamped to the calling user on `upload/initiate`), or any user holding `manage_files` / `files` (or a superuser). A member who did not initiate the upload and lacks those permissions gets `403 group_member_permission_denied`. This means the same member who initiated an upload can always finalize it — no elevated permission required.

### Step 4: Associate with a Model (optional)

If you want to associate the uploaded file with a model instance, set the relevant field to the returned file id after upload completes.

**POST** `/api/user/1`

```json
{
  "avatar": 124
}
```

You may only attach a file you can **see**: one you uploaded (own), or any file if you hold `manage_files` / `files`. Attaching a file id you don't have view access to is silently ignored — the record saves (HTTP 200) but the field stays unchanged. So the normal flow — initiate → upload → complete → attach — works end-to-end for the uploading user with no elevated permissions.

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

Explicit managers are not an authorization bypass. Ordinary user sessions may
use only an exact user-scoped manager, an exact active group-scoped manager for
which they have active direct/inherited membership, or a dual-scope manager
where both constraints match. System-scoped managers require global
`manage_files`/`files`. API keys and group-scoped tokens cannot initiate
uploads. Inactive managers and effectively inactive groups fail closed.

## Retry behavior

With an idempotency key, retrying the same initiation while `uploading`
returns the same file id and a refreshed target. Once the file is `completed`,
`failed`, or `expired`, retry returns its lifecycle state without any writable
target. Without a key, every initiation creates a new file.

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
