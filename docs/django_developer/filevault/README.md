# FileVault — Django Developer Reference

FileVault provides AES-256-GCM encrypted file storage (backed by S3) and encrypted structured data storage (in-database), with optional password protection and signed, IP-bound download tokens.

---

## Quick Start

### 1. Upload an encrypted file

```python
from mojo.apps.filevault.services import vault as vault_service

vault_file = vault_service.upload_file(
    file_obj=uploaded_file,       # any file-like object with .read()
    name="confidential.pdf",
    group=request.group,          # required — every vault file belongs to a group
    user=request.user,            # optional — tracks who uploaded
    password="secret",            # optional — adds a second encryption layer
    description="Q4 report",
    metadata={"department": "finance"},
)
```

### 2. Generate a shareable download link

```python
from mojo.helpers.request import get_remote_ip

client_ip = get_remote_ip(request)
token = vault_service.generate_download_token(vault_file, client_ip, ttl=300)

download_url = f"/api/filevault/file/download/{token}"
```

### 3. Download / stream the file

```python
# Validate the token (returns VaultFile or None)
vault_file = vault_service.validate_download_token(token, client_ip)

# Stream decrypted chunks (for large files)
chunks = vault_service.download_file_streaming(vault_file, password="secret")

# Or get the full decrypted bytes at once
file_bytes = vault_service.download_file(vault_file, password="secret")
```

---

## Using VaultFile in Your Own Models

The most common use case is attaching an encrypted file to one of your domain models — a contract, an invoice, a medical record, etc. There are two approaches.

### Approach A: ForeignKey with automatic upload (recommended)

Add a `ForeignKey` to `VaultFile` on your model. When a file is uploaded through the REST API for that field, Mojo's `MojoModel` machinery will automatically detect that the related model has a `create_from_file` classmethod and call it to encrypt + store the file for you.

```python
from django.db import models
from mojo.models import MojoModel
from mojo.apps.filevault.models import VaultFile


class Contract(models.Model, MojoModel):
    """A contract with an encrypted document attached."""

    class RestMeta:
        CAN_SAVE = True
        CAN_CREATE = True
        GRAPHS = {
            "default": {
                "fields": ["id", "title", "status", "created"],
                "graphs": {
                    "document": "basic",   # include vault file info in responses
                }
            }
        }

    title = models.CharField(max_length=200)
    status = models.CharField(max_length=32, default="draft")
    created = models.DateTimeField(auto_now_add=True)

    # This FK is all you need — file uploads to the "document" field
    # will be automatically encrypted and stored as a VaultFile.
    document = models.ForeignKey(
        VaultFile,
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL,
        related_name="contracts",
    )
```

When a client sends a multipart form POST with a `document` file field, the flow is:

1. `MojoModel.on_rest_save_file("document", file)` is called
2. It sees that the `document` field's related model (`VaultFile`) has `create_from_file`
3. `VaultFile.create_from_file(file, name, request=...)` encrypts and uploads the file
4. The resulting `VaultFile` instance is assigned to `contract.document`

The client can also pass `password`, `description`, and `metadata` in the same request body, and they'll be forwarded to the vault.

### Approach B: Manual upload in your own code

If you need more control (e.g., uploading from a background task, setting specific metadata), call the service layer directly:

```python
from mojo.apps.filevault.services import vault as vault_service


def create_contract_with_document(file_obj, title, group, user):
    vault_file = vault_service.upload_file(
        file_obj=file_obj,
        name=file_obj.name,
        group=group,
        user=user,
        metadata={"type": "contract", "title": title},
    )

    contract = Contract.objects.create(
        title=title,
        document=vault_file,
    )
    return contract
```

### Querying models with vault files

Since it's a standard Django FK, all the usual queries work:

```python
# All contracts that have an encrypted document attached
Contract.objects.filter(document__isnull=False)

# All contracts whose document was uploaded by a specific user
Contract.objects.filter(document__user=some_user)

# All contracts with password-protected documents
Contract.objects.filter(document__hashed_password__isnull=False)

# Get the vault file from a contract
contract = Contract.objects.select_related("document").get(pk=1)
vault_file = contract.document
```

---

## Sharing Download Access

FileVault uses **signed, IP-bound, time-limited tokens** for download access. This means you can share a download URL that:

- Expires after a configurable TTL (default: 300 seconds)
- Only works from the IP address it was generated for
- Does not require the recipient to be authenticated (the download endpoint is public)

### Generating a download link for a related model

Here's a complete example — a REST endpoint on your `Contract` model that returns a download URL for its attached document:

```python
import mojo.decorators as md
import mojo.errors as me
from mojo.helpers.request import get_remote_ip
from mojo.apps.filevault.services import vault as vault_service


@md.POST("contract/<int:pk>/download")
@md.requires_auth()
def on_contract_download(request, pk=None):
    contract = Contract.objects.filter(pk=pk).first()
    if not contract or not contract.document:
        raise me.ValueException("Document not found", code=404)

    client_ip = get_remote_ip(request)
    token = vault_service.generate_download_token(
        contract.document,
        client_ip,
        ttl=600,  # 10-minute link
    )

    return {
        "download_url": f"/api/filevault/file/download/{token}",
        "ttl": 600,
        "requires_password": contract.document.requires_password,
    }
```

The client then hits the returned `download_url` to get the decrypted file. If the file is password-protected, the client must include the `password` parameter in that download request.

### Using the built-in unlock endpoint

You don't have to write a custom endpoint — FileVault ships with one:

```
POST /api/filevault/file/<id>/unlock
```

This generates a token for the requesting user's IP and returns:

```json
{
    "token": "abc123...",
    "download_url": "/api/filevault/file/download/abc123...",
    "ttl": 300
}
```

### Verifying a password without downloading

If a file is password-protected, you can check the password first:

```
POST /api/filevault/file/<id>/password
Body: {"password": "user-entered-password"}
```

Returns `{"valid": true}` or `{"valid": false}`. This does **not** decrypt or download the file.

### Download flow summary

```
┌──────────┐     POST /unlock      ┌──────────┐
│  Client   │ ───────────────────>  │ FileVault│
│           │ <───────────────────  │  Server  │
│           │   {download_url, ttl} │          │
│           │                       │          │
│           │  GET download_url     │          │
│           │ ───────────────────>  │          │
│           │ <───────────────────  │          │
│           │   decrypted file      │          │
└──────────┘   (streaming)         └──────────┘
```

For password-protected files, add a password verification step before unlock, and pass `password` in the download request.

---

## REST API Endpoints

### VaultFile

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/filevault/file` | List vault files (respects group + permissions) |
| GET | `/api/filevault/file/<id>` | Get vault file metadata |
| POST | `/api/filevault/file` | Create vault file record (metadata only) |
| PUT | `/api/filevault/file/<id>` | Update vault file metadata |
| DELETE | `/api/filevault/file/<id>` | Delete vault file (also removes S3 object) |
| POST | `/api/filevault/file/upload` | Upload and encrypt a file |
| POST | `/api/filevault/file/<id>/unlock` | Generate a signed download token |
| POST | `/api/filevault/file/<id>/password` | Verify password without downloading |
| GET | `/api/filevault/file/download/<token>` | Download decrypted file (public, token-secured) |

### VaultData

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/filevault/data` | List vault data records |
| GET | `/api/filevault/data/<id>` | Get vault data metadata |
| POST | `/api/filevault/data/store` | Encrypt and store JSON data |
| POST | `/api/filevault/data/<id>/retrieve` | Decrypt and return stored data |

---

## VaultData — Encrypted Key-Value Storage

`VaultData` stores encrypted JSON in the database (no S3 needed). Useful for API keys, credentials, secrets, and other structured data.

### Storing data via the service layer

```python
from mojo.apps.filevault.services import vault as vault_service

vault_data = vault_service.store_data(
    group=group,
    user=user,
    name="API Credentials",
    data={"api_key": "sk-prod-abc123", "api_secret": "xyz789"},
    password="optional-password",
    description="Production API keys",
)
```

### Retrieving data

```python
decrypted = vault_service.retrieve_data(vault_data, password="optional-password")
# decrypted == {"api_key": "sk-prod-abc123", "api_secret": "xyz789"}
```

### Using VaultData as a FK

Just like `VaultFile`, you can reference `VaultData` from your own models:

```python
from mojo.apps.filevault.models import VaultData

class Integration(models.Model, MojoModel):
    name = models.CharField(max_length=200)
    credentials = models.ForeignKey(
        VaultData,
        null=True, blank=True, default=None,
        on_delete=models.SET_NULL,
    )
```

---

## Key Fields

### VaultFile

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | CharField | Unique identifier, used as the S3 object key |
| `name` | CharField | User-provided file name |
| `description` | TextField | Optional description |
| `content_type` | CharField | MIME type |
| `size` | BigIntegerField | Original (unencrypted) file size in bytes |
| `is_encrypted` | IntegerField | `0` = plaintext, `2` = AES-256-GCM |
| `chunk_count` | IntegerField | Number of encrypted chunks |
| `hashed_password` | TextField | Bcrypt hash of optional password (null if none) |
| `ekey` | TextField | Wrapped encryption key (not directly usable) |
| `metadata` | JSONField | Arbitrary metadata dict |
| `group` | FK → Group | Required — scopes the file to a group |
| `user` | FK → User | Who uploaded the file |
| `unlocked_by` | FK → User | Who last generated a download token |

### VaultData

| Field | Type | Description |
|-------|------|-------------|
| `name` | CharField | Name / label for the data |
| `description` | TextField | Optional description |
| `ekey` | TextField | Wrapped encryption key |
| `edata` | TextField | Base64-encoded encrypted payload |
| `hashed_password` | TextField | Bcrypt hash of optional password |
| `metadata` | JSONField | Arbitrary metadata (includes internal `_uuid`) |
| `group` | FK → Group | Required |
| `user` | FK → User | Who stored the data |

---

## Security Model

| Layer | Detail |
|-------|--------|
| **Encryption** | AES-256-GCM with per-file keys, chunked for large files |
| **Key wrapping** | File encryption keys are wrapped using the Django `SECRET_KEY` + file UUID |
| **Password protection** | Optional second layer — password is mixed into the KDF passphrase |
| **Password storage** | Bcrypt hash stored separately; password verification does not decrypt the file |
| **Download tokens** | HMAC-signed, bound to the requester's IP address, time-limited |
| **Token endpoint** | `/file/download/<token>` is public — authentication is the token itself |
| **Permissions** | All other endpoints require `view_vault` or `manage_vault` permissions |
| **Group scoping** | Every vault file/data record belongs to a group — no personal vault items |
| **S3 storage** | FileManager backend is forced to `is_public=False` for filevault usage |
| **Deletion** | Deleting a `VaultFile` record also deletes the S3 object |

---

## Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `FILEVAULT_DEFAULT_TTL` | `300` | Default download token TTL in seconds |
| `FILEVAULT_S3_BUCKET` | — | S3 bucket for encrypted file storage |

---

## Common Patterns

### Checking if a file requires a password

```python
if vault_file.requires_password:
    # prompt the user for a password before downloading
    ...
```

### Deleting a vault file when its parent is deleted

Use `on_delete=models.CASCADE` if you want the `VaultFile` record deleted when the parent is deleted (this also removes the S3 object automatically via `on_rest_pre_delete`):

```python
document = models.ForeignKey(VaultFile, on_delete=models.CASCADE)
```

Or use `models.SET_NULL` if you want to keep the vault file around independently of the parent model.

### Replacing a file on an existing record

```python
# Delete the old vault file (also cleans up S3)
if contract.document:
    contract.document.delete()

# Upload a new one
contract.document = vault_service.upload_file(
    file_obj=new_file,
    name=new_file.name,
    group=contract.document.group,
    user=request.user,
)
contract.save()
```

### Downloading a file in a background task

```python
from mojo.apps.filevault.services import vault as vault_service

file_bytes = vault_service.download_file(vault_file, password=None)
# file_bytes is the decrypted content — write it wherever you need
```
