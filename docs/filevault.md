# FileVault

Encrypted file storage and encrypted JSON data for Django. FileVault stores encrypted blobs via FileManager and keeps wrapped encryption keys in the database. All encrypt/decrypt happens in the Django process.

## Overview

FileVault provides two primitives:

- VaultFile: Encrypted files stored via FileManager (typically S3).
- VaultData: Encrypted JSON stored directly in the database.

Both use AES-256-GCM for confidentiality and integrity. Keys are derived with PBKDF2-HMAC-SHA256. Optional passwords are supported and are required to decrypt password-protected files.

## Architecture

```
Client
  |
  v
Django (auth, encryption, token signing)
  | \
  |  \-> Database (wrapped ekeys, metadata, password hashes)
  |
  \-> FileManager backend (usually S3) stores ciphertext only
```

## Storage and Encryption Notes

- Uploads are currently in-memory: the full file is read, encrypted into the vault format, and saved as one blob.
- Downloads can stream: the encrypted blob is opened and decrypted chunk by chunk.
- The encrypted blob has a fixed-size header and chunked layout to support streaming.

## Models

### VaultFile

Fields (key ones):
- `uuid`: storage key (not a crypto key)
- `name`, `content_type`, `description`, `size`
- `chunk_count`, `is_encrypted`
- `ekey`: wrapped encryption key (never exposed in REST graphs)
- `hashed_password`: PBKDF2 hash (never exposed)
- `metadata`: JSONField for arbitrary metadata
- `user`, `group`, `unlocked_by`

REST meta:
- Graphs exclude `ekey`, `hashed_password`, `uuid`, `chunk_count`.
- `NO_SAVE_FIELDS`: `id`, `pk`, `ekey`, `uuid`, `chunk_count`, `hashed_password`, `is_encrypted`.

### VaultData

Fields (key ones):
- `name`, `description`
- `ekey`, `edata`, `hashed_password` (never exposed)
- `metadata`: JSONField
- `user`, `group`

REST meta:
- Graphs exclude `ekey`, `edata`, `hashed_password`.
- `NO_SAVE_FIELDS`: `id`, `pk`, `ekey`, `edata`, `hashed_password`.

## Service API (Python)

Use the service layer directly from any Django app:

```python
from mojo.apps.filevault.services import vault as vault_service

vault_file = vault_service.upload_file(
    file_obj=uploaded_file,
    name=uploaded_file.name,
    group=request.group,
    user=request.user,
    password=request.DATA.get("password"),
    description=request.DATA.get("description"),
    metadata={"source": "invoices"},
)

data_bytes = vault_service.download_file(vault_file, password="secret")
stream = vault_service.download_file_streaming(vault_file, password="secret")

token = vault_service.generate_download_token(vault_file, client_ip)
vault_file = vault_service.validate_download_token(token, client_ip)

vault_data = vault_service.store_data(
    group=request.group,
    user=request.user,
    name="settings",
    data={"foo": "bar"},
    password=None,
)

payload = vault_service.retrieve_data(vault_data, password=None)
```

## REST API

Routes live in `mojo/apps/filevault/rest/`.

### File Endpoints

| Method | Path                    | Auth     | Description                                   |
| ------ | ----------------------- | -------- | --------------------------------------------- |
| GET    | `file`                  | Required | List file metadata (group-scoped)             |
| POST   | `file`                  | Required | Create/update metadata only                   |
| GET    | `file/<pk>`             | Required | Get file metadata                             |
| PUT    | `file/<pk>`             | Required | Update file metadata                          |
| DELETE | `file/<pk>`             | Required | Delete file (removes blob and DB record)      |
| POST   | `file/upload`           | Required | Upload + encrypt file                         |
| POST   | `file/<pk>/unlock`      | Required | Generate signed download token                |
| POST   | `file/<pk>/password`    | Required | Verify password without downloading           |
| GET    | `file/download/<token>` | None     | Download via signed token                     |

### Data Endpoints

| Method | Path                    | Auth     | Description                          |
| ------ | ----------------------- | -------- | ------------------------------------ |
| GET    | `data`                  | Required | List data metadata                   |
| POST   | `data`                  | Required | Create/update metadata only          |
| GET    | `data/<pk>`             | Required | Get data metadata                    |
| PUT    | `data/<pk>`             | Required | Update data metadata                 |
| DELETE | `data/<pk>`             | Required | Delete data                          |
| POST   | `data/store`            | Required | Encrypt + store JSON payload         |
| POST   | `data/<pk>/retrieve`    | Required | Decrypt + return JSON payload        |

### Custom Endpoint Payloads

- `POST file/upload`: multipart with `file` plus optional `name`, `description`, `password`, `metadata` (JSON string or object). Returns VaultFile metadata.
- `POST file/<pk>/unlock`: optional `ttl` (seconds). Returns `token`, `download_url`, `ttl`.
- `POST file/<pk>/password`: `password` required. Returns `valid`.
- `GET file/download/<token>`: optional `password` for protected files. Streams file bytes.
- `POST data/store`: `name` and `data` required; optional `password`, `description`, `metadata`. Returns VaultData metadata.
- `POST data/<pk>/retrieve`: optional `password`. Returns decrypted `data`.

Note: custom endpoints return plain dicts. If you need the canonical REST envelope (`status`/`data`), wrap with `MojoModel.return_rest_response(...)` or `JsonResponse(...)`.

## MojoModel File Integration

`MojoModel.on_rest_save_file` calls `related_model.create_from_file(file, field_name)` when a relation field points at a model with `create_from_file`.

FileVault supports this via `VaultFile.create_from_file(...)`.

Behavior:
- If `request` is not provided, `create_from_file` pulls the active request from `mojo.models.rest.ACTIVE_REQUEST`.
- `user`, `group`, and optional `password`, `description`, `metadata` are read from the request when available.
- The uploaded file name is taken from `file.name`.

This enables patterns like:

```python
class Invoice(models.Model, MojoModel):
    attachment = models.ForeignKey("filevault.VaultFile", null=True, on_delete=models.SET_NULL)

# POST multipart with field "attachment"
# MojoModel will call VaultFile.create_from_file(...) and assign the relation.
```

If you need custom routing (for example, different password logic), override your model's `on_rest_save_file` and call `vault_service.upload_file(...)` directly.

## Access Tokens

Download tokens are signed, IP-bound, and stateless:

- Payload: `fid`, `ip`, `exp`, `iat`
- Token: `base64url(payload) + "." + base64url(HMAC(payload))`
- Validation checks signature, IP match, and expiration.

## Configuration

Encryption parameters are constants in `mojo/helpers/crypto/vault.py`:
- `VAULT_CHUNK_SIZE`
- `VAULT_TOKEN_TTL`
- `VAULT_KDF_ITERATIONS`

The root of trust is `SECRET_KEY`.

## Dependencies

- `pycryptodome`: AES-256-GCM, PBKDF2, secure random
- stdlib: `hashlib`, `hmac`, `json`
