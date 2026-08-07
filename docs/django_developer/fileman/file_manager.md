# FileManager Model — Django Developer Reference

## Overview

`FileManager` is the storage backend configuration. Each `File` belongs to a `FileManager` that determines where and how files are stored (local disk, AWS S3, etc.).

## Key Concepts

- One or more `FileManager` instances exist per deployment
- A `FileManager` can be scoped to a `User`, a `Group`, or neither (system-wide default)
- Backends are pluggable: local, S3, and others

## Ownership and Scoping

`FileManager` supports three scopes: user-owned, group-scoped, and system-wide. Because of this, `FileManager.RestMeta` sets `CREATED_BY_OWNER_FIELD = None` — the framework's create-time auto-stamping of the `user` field is **disabled**.

Behavior on REST create (`POST /api/fileman/manager`):

| Request body | `user` on created record |
|---|---|
| `user` omitted | `None` — group- or system-scoped manager |
| `user: null` | `None` |
| `user: <id>` | That user's id — user-scoped manager |

`group` auto-fill from `request.group` is **not** affected and works normally.

### System-scoped creation is superuser-only

A manager created with **no `user` and no `group`** is system-scoped — it is eligible to become the system default that `get_for_user` / `get_for_group` derive every other manager from. `FileManager.on_rest_pre_save` rejects this via REST unless the requester is a superuser, raising `PermissionDeniedException` (HTTP 403). Direct ORM creation (`FileManager.objects.create(...)`, bootstrap code, the internal `get_for_*` provisioning helpers) does not go through `on_rest_pre_save` and bypasses this guard.

## Getting the Right FileManager

```python
from mojo.apps.fileman.models import FileManager

# From the current request (resolves group, then system default)
fm = FileManager.get_from_request(request)

# For a specific user/group
fm = FileManager.get_for_user_group(user, group)
```

Upload initiation must use `FileManager.resolve_for_upload(request)`, not a
bare primary-key lookup. The resolver applies the upload authorization truth
table before any `File` or upload capability is created:

- uploads require an ordinary authenticated `User` session; API keys,
  override-user API keys, and group-scoped tokens are rejected;
- inactive managers and managers in an effectively inactive group are rejected;
- a user-scoped manager requires the exact calling user;
- a group-scoped manager requires the exact active request group and an active
  direct or inherited membership;
- a manager with both scopes requires both constraints; and
- a system-scoped manager requires global `manage_files` or `files` authority.

Global file administrators may use any active manager, but explicit `group`
and `use` selectors must still agree with that manager. Client-supplied `user`
selectors are never accepted. Authorization failures happen before storage
targets are generated.

`upload_policy` is the safe graph for upload configuration. It contains only
`id`, `name`, `use`, `is_active`, `max_file_size`, `allowed_extensions`,
`allowed_mime_types`, and `supports_direct_upload`; backend locations,
credentials, and encrypted settings are deliberately absent.

## FileManager Settings

Each `FileManager` stores backend-specific settings through the encrypted
`MojoSecrets.mojo_secrets` text field. Use the model helpers rather than reading
or writing that encrypted blob directly:

```python
# Access a setting
expiry = fm.get_setting("urls_expire_in", 3600)  # URL expiry in seconds
is_pub = fm.is_public                             # Whether files are publicly accessible
root = fm.root_path                               # Storage root path/bucket
```

### Shortlink settings

Three optional keys control shortlink behavior for files and renditions in this manager:

| Key | Default | Description |
|---|---|---|
| `use_shortlinks` | `None` (inherit global) | Force-on (`True`) or force-off (`False`) shortlink wrapping for this manager. `None` defers to the global `FILEMAN_USE_SHORTLINKS` setting (default `True`). |
| `shortlink_track_clicks` | `False` | When `True`, tier-1 shortlinks (auto-generated display URLs) log each click as a `ShortLinkClick` record. |
| `shortlink_expire_days` | `0` (never) | Lifetime in days for tier-1 shortlinks. `0` means the shortlink never expires. |

These settings apply only to tier-1 (auto-generated) links. Tier-2 share links set their own `expire_days` and `track_clicks` per call and do not consult these settings.

```python
fm.set_setting("use_shortlinks", False)          # disable short URLs for this manager
fm.set_setting("shortlink_track_clicks", True)   # log clicks on display URLs
fm.set_setting("shortlink_expire_days", 90)      # expire tier-1 links after 90 days
fm.save()
```

See [shortlinks.md](shortlinks.md) for the full shortlink pipeline and opt-out behavior.

## Storage Backends

### Local Backend

Files stored on local filesystem. No presigned URLs — uses the framework's upload endpoint.

### S3 Backend

Files stored in AWS S3. Supports:
- Presigned upload URLs (direct browser-to-S3 uploads)
- Presigned download URLs with configurable TTL
- Public bucket support

`python manage.py aws-check --section s3 --check` audits the system-default S3
manager, bucket access/region, Public Access Block and CORS without persisting
audit state. `--apply --bucket-name <name>` can create a missing private bucket
and system manager. `--probe-s3` is separately confirmed and uses only a unique
sentinel key that is deleted in `finally`; it never lists or deletes user
objects. Existing buckets and policies are preserved.

Bucket and prefix come from `backend_url` (`s3://my-bucket/some/prefix`).
Credentials come from the manager's own settings, falling back to these
`settings.py` keys when a manager is created through REST:

```python
AWS_KEY = "..."       # optional — leave unset to use the instance profile
AWS_SECRET = "..."
AWS_REGION = "us-east-1"
```

#### Credential resolution

All S3 calls a manager makes — uploads, downloads, CORS, the public-access
audit — run through **one** session built by the backend. See
[../aws/credentials.md](../aws/credentials.md) for the underlying factories.

| Setting | Meaning |
|---|---|
| `aws_key` | Static access key id. Optional. |
| `aws_secret` | Static secret. Required if `aws_key` is set, and vice versa. |
| `aws_region` | Region. Defaults to `us-east-1`. |
| `assume_role_arn` | Cross-account role to assume. When set, the settings above become only the *source* identity used to call `sts:AssumeRole`. **Superuser-writable only.** |
| `external_id` | `sts:ExternalId` for the trust policy. Omitted from the STS call when unset. **Superuser-writable only.** Write-only — reads back as `has_external_id`. |
| `role_session_name` | STS session name. Defaults to `django-mojo-fileman-<manager pk>`. **Superuser-writable only.** |
| `assume_role_duration` | Role session seconds. Defaults to 43200 (12 hours). **Superuser-writable only.** |

**Leaving `aws_key`/`aws_secret` unset is a supported configuration**, not a
misconfiguration: the session falls through to botocore's default chain, which
ends at the EC2 instance profile or ECS task role. Setting exactly one of the
pair is an error and fails fast with a readable message rather than silently
acting as a different identity.

**Credential settings are read from the primary parent manager.** The backend
resolves them through `file_manager.primary_settings`, which walks up the
`parent` chain to the root. Setting `aws_key` or `assume_role_arn` on a child
manager has no effect on that child's storage calls — configure the root.

**Presigned URL lifetimes are clamped under an assumed role.** A URL signed with
STS temporary credentials stops working the moment those credentials expire,
regardless of its own `ExpiresIn`. botocore refreshes the role 10–15 minutes
ahead of expiry, so the backend caps `ExpiresIn` at whatever the live credential
has left. With the default 12-hour role duration this is never the binding
constraint for a 1-hour URL; with a short `assume_role_duration` it will be, and
the returned URL simply expires sooner than requested. The lifetime actually
requested for downloads is `urls_expire_in` (default 3600).

**Why the role settings are superuser-only:** REST save dispatches a `set_<key>`
method for any key in the payload, and `SAVE_PERMS` for FileManager is the
group-level `files`/`manage_files` permission. Without the gate, anyone who can
administer files could point the platform's own credentials at a role they
control — a confused deputy. Writing any of the four role settings through REST
therefore requires `is_superuser`; direct ORM/bootstrap code is unaffected.

Changing `assume_role_arn` or `external_id` also changes the manager's
public-access config fingerprint, so cached audit evidence collected under the
old identity is invalidated.

#### S3 credential boundary

FileManager credentials are written with `set_aws_key()` and
`set_aws_secret()` and read by trusted backend code through the raw `aws_key`
and `aws_secret` properties. Those raw properties are internal-only and must
never be added to a REST graph.

The `default`, `list`, and `basic` REST graphs expose only
`aws_key_masked` and `aws_secret_masked`. A configured value longer than four
characters keeps only its last four characters visible; a non-empty value of
four characters or fewer is fully masked. An absent value is returned as an
empty string. File responses that nest a manager use the same safe `basic`
graph, and an unknown FileManager graph falls back to the safe `default`
graph.

REST create and update requests still accept `aws_key` and `aws_secret` as
write-only inputs through the model's custom setters. The masked properties are
response-only: generic REST save ignores them. Clients should omit unchanged
credentials and must never echo a displayed mask as a replacement value.

This response-contract change does not rotate, rewrite, or migrate stored
credentials and does not change backend authentication. No data migration is
required.

#### Public-access reconciliation

For a user-scoped S3 manager, `is_public` is a stored classification that must
agree with the storage service. FileManager performs a policy-level check when
`get_for_user()` provisions or retrieves a personal manager. The first real
file or rendition URL then supersedes policy-only evidence once with a stronger
object-level check. An existing manager can also enter directly through that
object-level path when one of its files is resolved after upgrade.

The internal `public_access_audit` field stores versioned evidence with one of
three statuses:

- `public` — anonymous access was conclusively established; `is_public` is repaired to `True` and unsigned URLs are allowed.
- `private` — anonymous access was conclusively denied; `is_public` is repaired to `False`.
- `unknown` — AWS could not establish either result. The stored `is_public` value is preserved, but download behavior fails closed to a presigned URL.

The audit uses an anonymous HEAD probe after authenticated S3 access confirms a
real object exists. An anonymous 403 is enough to disprove manager-wide public
access. A successful probe proves only that one object is readable, so the
manager is classified public only when policy evidence also shows an
unconditional anonymous `s3:GetObject` allow covering the entire prefix, no
matching deny can override it, and effective bucket/account Public Access Block
settings do not restrict the existing policy. Before any object exists, that
same conservative policy evidence is used by itself. The audit never adds or
broadens bucket policy statements.

Audit metadata carries a one-way fingerprint of the backend URL/type and
effective connection settings. Changing those FileManager inputs invalidates
the evidence. Normal reads do not use an hourly TTL and do not poll AWS after
the current evidence is established.

Bucket/account policy or Public Access Block changes made through AWS Console,
Terraform, or another path outside FileManager are not observable from the
model. Force a bulk policy refresh after such a change:

```bash
python manage.py reconcile_fileman_public_access
python manage.py reconcile_fileman_public_access --dry-run
```

The command audits active user-scoped S3 managers independently, reports
`public`/`private`/`unknown` totals, and continues if one manager fails. Dry-run
performs the read-only AWS inspection without changing `is_public` or audit
metadata.

### Backend interface — `download(file_path, local_path)`

All backends used by the renderer pipeline (image, vector, video, audio, document) must implement:

```python
def download(self, file_path: str, local_path: str) -> None:
    """Copy the stored file at file_path to a local filesystem path."""
```

The renderers download the original file to a temp path before processing. The local backend (`FileSystemStorageBackend`) and the S3 backend both implement this. If you write a custom backend, implement `download()` or rendition generation will fail for that backend's files.

See `mojo/apps/fileman/backends/` — each backend inherits `BaseStorageBackend`.

## Initiated Upload Flow

```python
# The REST endpoint resolves and authorizes the manager, validates policy,
# creates the File, and returns a normalized upload target.
POST /api/fileman/upload/initiate

# The client transfers to the returned target, then confirms exactly once
# (retries are safe):
POST /api/fileman/file/<id>  {"action": "mark_as_completed"}
```

Local and cloud transfers share this lifecycle. A successful transfer leaves
the `File` in `uploading`; only `mark_as_completed` validates the stored object,
sets `completed`, revokes the local upload token, and publishes rendition work.
Completion is lock-protected and side-effect-idempotent.

Initiation validates a normalized basename, a nonnegative integer size,
extension policy, and exact/top-level-wildcard MIME policy. Local uploads are
also bounded while streaming and checked against their actual byte count and
detected content type. Validation failure removes any partial storage object
and moves the row to `failed`.

An optional idempotency key may contain 1–128 ASCII letters, digits, `.`, `_`,
`:`, or `-`. The raw key is never stored: an internal `UploadInitiation` stores
only a digest, fingerprint, actor, and `File`. Same actor/key/fingerprint
replays the same file. Only `uploading` replays receive a refreshed target;
`completed`, `failed`, and `expired` replays return lifecycle state without a
writable capability. Omitting the key always creates a distinct file.

## Multiple FileManagers per Group

A group can have multiple named FileManagers (e.g., `"avatars"`, `"documents"`). Specify by name:

```python
fm = FileManager.get_for_user_group(user, group, use="avatars")
```

REST clients pass `?use=avatars` to select a specific manager.

## Auto-Provisioning

If no `FileManager` exists for a group or user, the system-wide default is used
automatically. Set `is_default=True` on one FileManager to designate it. A new
user manager inherits the system manager's public/private value and reconciles
its derived S3 prefix before it is returned.
