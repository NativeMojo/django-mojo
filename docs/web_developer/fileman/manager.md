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

## AWS credential fields

An S3 manager can carry credentials in the request body. All of these are
settings, not model columns — send them as ordinary fields.

| Field | Write | Read back as |
|---|---|---|
| `aws_key` | `manage_files` / `files` | `aws_key` |
| `aws_secret` | `manage_files` / `files` | `aws_secret_masked` (last 4 chars) |
| `aws_region` | `manage_files` / `files` | `aws_region` |
| `assume_role_arn` | `manage_files` / `files` on a store with its own AWS key; otherwise **superuser only** | `assume_role_arn` |
| `external_id` | `manage_files` / `files` on a store with its own AWS key; otherwise **superuser only** | `has_external_id` (boolean) |
| `role_session_name` | `manage_files` / `files` on a store with its own AWS key; otherwise **superuser only** | *(not serialized)* |
| `assume_role_duration` | `manage_files` / `files` on a store with its own AWS key; otherwise **superuser only** | *(not serialized)* |

- **A role on platform credentials is superuser only.** A role is assumed with
  the store's AWS key. A store *runs on platform credentials* when it has no
  `aws_key` or no `aws_secret` of its own (a parent's does not count), when its
  `aws_key` is the platform's `AWS_KEY` or a system-scoped manager's key, or
  when it is itself system-scoped. A store created without a key is given the
  platform's, so it is in this group until it is given its own.
  - A save that would leave such a store with `assume_role_arn` set returns
    **403** for a non-superuser, and stores nothing from the request, when the
    request changes any of the four role fields, `aws_key` or `aws_secret`.
    That covers adding or changing the role, and removing the store's own key
    or replacing it with the platform's while a role is set.
  - The rule looks at the store as the request leaves it, so it is the same
    whether the values are sent as flat fields or inside `secrets` or
    `settings`, on update and on create. Send the store's own key and the role
    in one request to set both.
  - On a store with its own key, `manage_files` / `files` is enough to store
    the role.
  - **Storing a value is not the same as using it.** Storage calls read the
    key and the role from the *root* manager only (the top of the `parent`
    chain). On a manager with no parent, its own key assumes its own role and
    reaches only what that key can. On a child manager the values are saved
    and have no effect: configure the root.
  - A refused save is refused whole. Nothing in the request is stored,
    including a nested record such as `parent: {...}`.
  - Other fields stay editable on a store that already has a role on platform
    credentials, as long as the request leaves the role fields and the key as
    they are. Removing the role is always allowed.
- **`mojo_secrets`, `secret` and `setting` are not accepted** in a request body.
  They are skipped.
- **`external_id` is write-only.** It is never returned in any form — not even
  masked — because it is short and its whole purpose is to be unguessable by
  someone who already knows the role ARN. The `default` and `list` graphs expose
  only `has_external_id: true|false`. Send the value again to change it; there
  is no way to read the current one back.
- **Omitting `aws_key` and `aws_secret` is valid.** The server then uses its own
  ambient AWS credentials. Sending only one of the pair is rejected when the
  connection is next tested, with an explicit "AWS key configured without a
  secret (or vice versa)".

Use the `test_connection` action to verify a configuration after saving.

> **System-scoped managers are superuser-only.** A manager created with no `user` **and** no group (no `group` in the body and no active group on the request) is *system-scoped* and can become the system default. Creating one via REST returns **403** unless the caller is a superuser. Supply a `group` — or operate within a group context — to create a group-scoped manager as a regular user.

## Selecting a FileManager for uploads

Clients do not need to manage FileManagers directly. To select a specific
manager during an upload, pass `file_manager: <id>` in the initiate body. The
server authorizes that exact manager before creating a File: user, group, and
dual scopes must match; inactive/effectively-inactive scopes fail closed; and a
system manager requires global `manage_files`/`files`. API keys and restricted
group tokens cannot initiate. Explicit `group` and `use` selectors must agree
with the selected manager even for a global file administrator.

The safe `upload_policy` graph exposes only policy fields (`id`, `name`, `use`,
active flag, maximum size, allowed extensions/MIME types, and direct-upload
support). It never exposes backend locations, credentials, or settings. See
[upload.md](upload.md) for the complete lifecycle and retry contract.
