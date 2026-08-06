# FileManager API — REST API Reference

FileManagers are storage backend configurations. Admins create and manage them; end-user file uploads resolve a manager automatically.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/fileman/manager` | List file managers |
| POST | `/api/fileman/manager` | Create a file manager |
| GET | `/api/fileman/manager/<id>` | Get a file manager |
| POST/PUT | `/api/fileman/manager/<id>` | Update a file manager |
| DELETE | `/api/fileman/manager/<id>` | Delete a file manager |

## Permissions

- `view_fileman` or `manage_files`

## Creating a FileManager

**POST** `/api/fileman/manager`

```json
{
  "name": "documents",
  "backend_type": "s3",
  "backend_url": "s3://my-bucket/docs/",
  "aws_region": "us-east-1",
  "aws_key": "<access-key-id>",
  "aws_secret": "<secret-access-key>",
  "is_default": false,
  "group": 7
}
```

### S3 credential inputs and responses

| Field | Direction | Description |
|---|---|---|
| `aws_region` | Input and output | S3 region used by the manager. |
| `aws_key` | Write-only input | Access key id accepted on create or update. Never returned. |
| `aws_secret` | Write-only input | Secret access key accepted on create or update. Never returned. |
| `aws_key_masked` | Response only | Masked access-key hint. Only the last four characters of a long value remain visible. |
| `aws_secret_masked` | Response only | Masked secret hint. Only the last four characters of a long value remain visible. |

FileManager list and detail responses, including managers nested in File
responses, omit `aws_key` and `aws_secret`. They return only the masked fields:

```json
{
  "aws_region": "us-east-1",
  "aws_key_masked": "****************1234",
  "aws_secret_masked": "************************5678"
}
```

Non-empty values of four characters or fewer are fully masked, and absent
credentials appear as empty strings. The masked fields are display hints, not
update values. Generic REST save ignores `aws_key_masked` and
`aws_secret_masked`, but clients should not submit them. To keep credentials
unchanged, omit `aws_key` and `aws_secret` from the update. Never echo a mask
back as a replacement credential.

### User field behavior

The `user` field is **not** auto-stamped on create. Omitting `user` (or sending `user: null`) creates a group-scoped or system-scoped manager — this is the normal case for shared storage. Pass an explicit `user` id only to create a user-owned manager.

| `user` in body | Owner on created record |
|---|---|
| Omitted | `null` — group/system scoped |
| `null` | `null` — group/system scoped |
| `<user id>` | That user — user-scoped manager |

`group` is auto-filled from the caller's active group (`request.group`) when not specified in the body.

> **System-scoped managers are superuser-only.** A manager created with no `user` **and** no group (no `group` in the body and no active group on the request) is *system-scoped* and can become the system default. Creating one via REST returns **403** unless the caller is a superuser. Supply a `group` — or operate within a group context — to create a group-scoped manager as a regular user.

## Selecting a FileManager for uploads

Clients do not need to manage FileManagers directly. To select a specific manager during an upload, pass `file_manager: <id>` in the initiate body. See [upload.md](upload.md).
