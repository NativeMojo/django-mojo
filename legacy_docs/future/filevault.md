# FileVault — Encrypted File Storage System

## Overview

FileVault is an encrypted file storage service that stores files in AWS S3 as ciphertext. Encryption keys live in the application database, which is never directly exposed — it sits behind a Django REST API. This separation means an S3 bucket compromise yields only useless encrypted blobs, and a database compromise yields only encrypted keys that require the server's secret to unwrap.

FileVault provides two storage primitives:

- **VaultFile** — Encrypted file storage in S3 with optional password protection and secure, time-limited download links.
- **VaultData** — Encrypted structured JSON data stored in the database.

---

## Architecture

```
Client
  |
  v
nginx (TLS termination, streaming proxy)
  |
  v
Django ASGI (authentication, authorization, encryption)
  |
  ├── Database (PostgreSQL)
  │     └── Encrypted ekeys, file metadata, password hashes
  |
  └── AWS S3
        └── Ciphertext only (vault/files/{uuid})
```

All encryption and decryption happens in the Django process. S3 is a dumb blob store that never sees plaintext. The database holds metadata and wrapped encryption keys but never holds file content.

---

## Encryption

### Algorithm: AES-256-GCM

All file encryption uses AES-256-GCM (Galois/Counter Mode). GCM provides:

- **Confidentiality** — Data cannot be read without the key.
- **Authenticity** — Any modification to the ciphertext is detected on decryption. There is no need for a separate integrity hash.
- **Performance** — GCM is hardware-accelerated on modern CPUs via AES-NI.

### Chunk-Based Format (Streaming Download)

Files are encrypted into a fixed-size chunked format (default 5MB). Each chunk is encrypted independently with its own nonce and authentication tag. This enables:

- **Per-chunk integrity** — A tampered chunk is detected immediately without processing the entire file.
- **Streaming download** — The implementation supports streaming decryption (`download_file_streaming`) without loading the full file into memory.
- **Random-access friendly** — The header + fixed chunk sizes make byte offsets predictable.

**Important**: the current upload path reads the entire file into memory and writes one encrypted blob via the FileManager backend. It is chunked on disk, but not streamed on upload. Large uploads therefore require memory roughly proportional to file size.

### Key Derivation: PBKDF2-HMAC-SHA256

Raw encryption keys are never used directly as AES keys. A key derivation function transforms them into well-distributed cryptographic keys:

```
aes_key = PBKDF2-HMAC-SHA256(
    passphrase,       # ekey, or password + ekey if password-protected
    salt,             # 32 random bytes, unique per file, stored in file header
    iterations=600000,
    dklen=32          # 256-bit AES key
)
```

PBKDF2 with 600,000 iterations ensures that brute-force attacks against password-protected files are computationally expensive. For non-password files, the KDF still provides proper key derivation from the high-entropy `ekey`. PBKDF2-HMAC-SHA256 is in Python's standard library (`hashlib.pbkdf2_hmac`) — no additional dependencies.

The KDF runs once per file operation. The derived AES key is then used for all chunks in the file.

---

## Key Management

### Per-File Encryption Key (ekey)

Every file gets a unique encryption key generated at upload time:

```
ekey = 32 characters from [a-zA-Z0-9] via Crypto.Random (~190 bits of entropy)
```

This key is the secret that protects the file. It is never exposed through the REST API and cannot be set or read via any endpoint.

### Wrapped Key Storage

The `ekey` is encrypted before being stored in the database using a passphrase derived from the server's `SECRET_KEY` and the file UUID:

```
Wrapping:
  wrap_salt = 32 random bytes
  wrap_passphrase = SECRET_KEY + file_uuid
  wrap_key = PBKDF2-HMAC-SHA256(wrap_passphrase, wrap_salt, iterations=600000, dklen=32)
  wrap_nonce = 12 random bytes
  encrypted_ekey, auth_tag = AES-256-GCM(wrap_key, wrap_nonce, ekey)

Stored in DB column (base64-encoded):
  [wrap_salt (32)][wrap_nonce (12)][encrypted_ekey (variable)][auth_tag (16)]
```

This means:

- **Database compromise** — Attacker gets encrypted key blobs. Without the `SECRET_KEY` (which lives on the server, not in the database), they cannot unwrap them.
- **S3 compromise** — Attacker gets ciphertext. Without the `ekey` (which they can't get without both the database and the `SECRET_KEY`), decryption is infeasible.
- **Both database and S3 but not SECRET_KEY** — Still protected.

The `SECRET_KEY` is the root of trust. If it is lost, all wrapped keys are unrecoverable. It must be backed up securely and never stored in the database or the repository.

### Optional Password Protection

Files can require a password for download. When a password is set:

1. The password is hashed for verification (see Password Handling below).
2. For encryption, the password is combined with the `ekey` to form the KDF passphrase: `passphrase = password + ekey`.
3. This means decrypting a password-protected file requires the `ekey` (from the database, unwrapped with `SECRET_KEY`) **and** the user's password. Three secrets must align.

---

## Password Handling

### Hashing for Verification

Passwords are hashed using PBKDF2-HMAC-SHA256 with a per-file random salt:

```
password_salt = 32 random bytes
hashed_password = PBKDF2-HMAC-SHA256(password, password_salt, iterations=600000, dklen=32)

Stored in DB column (base64-encoded):
  [password_salt (32)][hashed_password (32)]
```

Verification uses constant-time comparison (`hmac.compare_digest`) to prevent timing attacks.

### Role in Encryption

The password is not just an access gate — it is part of the encryption key. A file encrypted with a password cannot be decrypted without it, even by someone with full database and server access. This provides a true zero-knowledge property for password-protected files: the server stores everything needed to verify the password and decrypt the file, but only when the user supplies the password.

---

## Encrypted File Format

Each encrypted file is a single S3 object with the following structure:

```
[Header — 64 bytes]
  Magic       (4 bytes):  "VF02"
  Chunk size  (4 bytes):  uint32 big-endian (default: 5,242,880)
  KDF salt    (32 bytes): Random, used for PBKDF2 key derivation
  Total chunks(4 bytes):  uint32 big-endian
  Reserved    (20 bytes): Zeroed, for future use

[Chunk 0]
  Nonce       (12 bytes): Deterministic, derived from chunk index
  Ciphertext  (variable): AES-GCM encrypted data
  Auth tag    (16 bytes): GCM authentication tag

[Chunk 1]
  ...

[Chunk N — final chunk, may be smaller]
```

### Nonce Derivation

Each chunk's 12-byte nonce is derived deterministically:

```
nonce = HMAC-SHA256(aes_key, chunk_index_as_4_byte_big_endian)[:12]
```

This guarantees uniqueness per chunk (required for GCM security) and prevents chunk reordering — decrypting chunk N with chunk M's expected nonce fails authentication.

### Byte Offset Calculation

For a known chunk size, any chunk can be located by byte offset without reading the entire file:

```
chunk_data_size = chunk_size + 12 (nonce) + 16 (auth tag)
chunk_offset = 64 (header) + (chunk_index * chunk_data_size)
```

The final chunk's ciphertext may be smaller than `chunk_size`, but its offset is still calculated the same way. The actual size is determined by the remaining bytes in the S3 object.

This enables S3 range requests to fetch individual chunks without downloading the full file.

---

## FileManager Storage

FileVault stores encrypted blobs using the FileManager subsystem:

- Storage is resolved via `FileManager.get_for_group(group, use="filevault")`.
- Objects are written to `{fm.root_path}/{uuid}` using `fm.backend.save(...)`.
- The backend is usually S3, but can be any FileManager-compatible storage.
- Deletion removes the single object and the database record.

---

## Access Tokens: Signed, IP-Bound, Stateless

### Purpose

When a file needs to be shared via a download link (e.g., for unauthenticated access), a signed access token is generated. The token is self-contained — the server does not store any state for it.

### Token Structure

```
payload = {
    "fid": <file primary key>,
    "ip":  <client IP address>,
    "exp": <expiration UTC timestamp>,
    "iat": <issued-at UTC timestamp>
}

signature = HMAC-SHA256(SECRET_KEY, base64url(payload))

token = base64url(payload) + "." + base64url(signature)
```

### Generating a Token (Unlock)

An authenticated user requests a download link:

1. Django verifies the user has permission to access the file.
2. Django reads the client's IP address from the request (via `X-Forwarded-For` or `X-Real-IP` from nginx).
3. Django constructs the token payload with the file ID, client IP, and a short expiration (default 300 seconds).
4. Django signs the payload with HMAC-SHA256 using `SECRET_KEY`.
5. The token and download URL are returned.
6. Optionally record an audit log entry (who unlocked which file, when, from what IP).

### Validating a Token (Download)

The download endpoint receives the token in the URL path:

1. Split the token at the `.` separator.
2. Verify the HMAC-SHA256 signature against `SECRET_KEY`. Reject if invalid.
3. Decode the payload.
4. Check `exp` > current time. Reject if expired.
5. Check `ip` matches the requesting client's IP. Reject if mismatched.
6. Look up the file by `fid`.
7. If the file has a password, verify it from the request. Reject if wrong or missing.
8. Proceed with decryption and streaming download.

### Security Properties

| Threat                                                       | Mitigation                                                 |
| ------------------------------------------------------------ | ---------------------------------------------------------- |
| Token leaked via server logs, browser history, referrer headers | Bound to the original client's IP — useless from elsewhere |
| Token replay from same IP                                    | Short expiration (default 5 minutes)                       |
| Token forgery                                                | Requires `SECRET_KEY` to produce valid HMAC                |
| Token tampering (modify IP or expiration)                    | HMAC signature verification fails                          |
| Brute-force token guessing                                   | HMAC-SHA256 output is 256 bits — infeasible                |

### IP Binding Considerations

The token binds to whatever IP address Django sees in the request. For this to work correctly:

- nginx must forward the real client IP via `X-Forwarded-For` or `X-Real-IP`.
- The same header must be read consistently during both unlock (token generation) and download (token validation).
- Users behind proxies or VPNs that rotate exit IPs could see failures if their IP changes between unlock and download. The short default expiration (300 seconds) minimizes this risk.

---

## Upload Flow (Current Implementation)

```
1. Client POSTs file to the upload endpoint (authenticated, multipart).
2. Server reads the full file into memory.
3. Create VaultFile record:
   a. Generate UUID for storage path.
   b. Generate ekey (32 random alphanumeric characters).
   c. Generate KDF salt (32 random bytes).
   d. If password provided:
      - Hash password with PBKDF2 and store hash.
      - Combine password + ekey as passphrase.
   e. Derive AES key via PBKDF2(passphrase, salt, 600k iterations).
   f. Encrypt ekey with SECRET_KEY and store wrapped blob.
4. Encrypt the file in-memory into the vault chunked format.
5. Save the encrypted blob via `fm.backend.save(...)`.
```

**Memory usage**: roughly 2x file size for uploads (plaintext + encrypted blob).

---

## Download Flow (Streaming)

### Authenticated Access (Unlock + Download)

```
1. Authenticated user POSTs to unlock endpoint with file ID and desired TTL.
2. Django verifies permission.
3. Django generates signed token (file ID, client IP, expiration).
4. Returns download URL containing the token.
5. Audit log entry written.

6. Client (or anyone at that IP within the TTL) GETs the download URL.
7. Django validates token (signature, expiration, IP).
8. If password-protected, verify password from request.
9. Unwrap ekey from database using SECRET_KEY.
10. Derive AES key via PBKDF2.
11. Open the encrypted blob via `fm.backend.open(...)`.
12. Read and parse the 64-byte header.
13. Stream chunks:
    a. Read encrypted chunk from storage.
    b. Decrypt with AES-256-GCM (validates auth tag).
    c. Yield plaintext chunk to StreamingHttpResponse.
14. Response streams through ASGI/nginx to client.
```

**Memory usage**: ~10MB peak.

**Error handling**: If any chunk fails GCM authentication, the stream aborts immediately. The client receives a truncated response, detectable by comparing received bytes to the `Content-Length` header.

---

## Data Model

### VaultFile

| Field           | Type            | Description                                        |
| --------------- | --------------- | -------------------------------------------------- |
| id              | AutoField       | Primary key                                        |
| created         | DateTimeField   | Auto-set on creation                               |
| modified        | DateTimeField   | Auto-set on save                                   |
| uuid            | CharField(64)   | S3 object key (not a crypto key), indexed          |
| name            | CharField(200)  | Original filename                                  |
| content_type    | CharField(128)  | MIME type (auto-detected from filename)            |
| description     | TextField       | User-provided description, nullable                |
| size            | BigIntegerField | Original file size in bytes                        |
| chunk_count     | IntegerField    | Number of encrypted chunks                         |
| is_encrypted    | IntegerField    | 0=plaintext, 2=AES-256-GCM chunked                 |
| ekey            | TextField       | Wrapped encryption key (encrypted with SECRET_KEY) |
| hashed_password | TextField       | PBKDF2 password hash + salt, nullable              |
| metadata        | JSONField       | Arbitrary metadata                                 |
| user            | FK(User)        | User who uploaded the file                         |
| unlocked_by     | FK(User)        | User who last generated a download token           |
| group           | FK(Group)       | Owning organization (CASCADE delete)               |

**Fields excluded from REST graphs**: `ekey`, `hashed_password`, `uuid`, `chunk_count`

**Fields not settable via REST API** (`NO_SAVE_FIELDS`): `id`, `pk`, `ekey`, `uuid`, `chunk_count`, `hashed_password`, `is_encrypted`

### VaultData

Encrypted JSON data stored in the database (not S3).

| Field           | Type          | Description                           |
| --------------- | ------------- | ------------------------------------- |
| id              | AutoField     | Primary key                           |
| created         | DateTimeField | Auto-set on creation                  |
| modified        | DateTimeField | Auto-set on save                      |
| name            | CharField(64) | Data name                             |
| description     | TextField     | Description, nullable                 |
| ekey            | TextField     | Wrapped encryption key                |
| edata           | TextField     | AES-256-GCM encrypted JSON (base64)   |
| hashed_password | TextField     | PBKDF2 password hash + salt, nullable |
| metadata        | JSONField     | Arbitrary metadata                    |
| user            | FK(User)      | Owning user                           |
| group           | FK(Group)     | Owning organization (CASCADE delete)  |

**Fields excluded from REST graphs**: `ekey`, `edata`, `hashed_password`

**Fields not settable via REST API** (`NO_SAVE_FIELDS`): `id`, `pk`, `ekey`, `edata`, `hashed_password`

VaultData uses the same encryption (AES-256-GCM) and key wrapping as VaultFile, but without chunking — the data is small enough for single-operation GCM.

Both models include a `metadata` JSONField for arbitrary key/value storage.

---

## Using FileVault in Django Apps

### Service API (Python)

Import `mojo.apps.filevault.services.vault` as `vault_service` and call it directly:

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

download_bytes = vault_service.download_file(vault_file, password="secret")

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

data = vault_service.retrieve_data(vault_data, password=None)
```

### REST + MojoModel Integration

- `VaultFile` and `VaultData` are `MojoModel` subclasses; standard CRUD routes are exposed via `on_rest_request`.
- Uploading encrypted files must use `POST /api/filevault/file/upload`. The standard `POST /api/filevault/file` only creates metadata and does not upload or encrypt file contents.
- Encrypted JSON data must use `POST /api/filevault/data/store`, and decrypted payloads are returned by `POST /api/filevault/data/<pk>/retrieve`.
- Custom endpoints return dicts directly. If you want the canonical REST envelope (`status`/`data`), wrap with `MojoModel.return_rest_response(...)` or `JsonResponse(...)`.

### Using `on_rest_save_file` / `create_from_file`

`MojoModel.on_rest_save_file` only handles relation fields when the related model implements `create_from_file(file, name)`. It does **not** pass the request, so `create_from_file` must look up context itself (for example via `mojo.models.rest.ACTIVE_REQUEST.get()`).

Uploaded files are parsed into `request.DATA["files"]` by the request parser. The file field name must match the model field name (for example, an `attachment` FK expects a multipart field named `attachment`).

VaultFile implements `create_from_file(...)`, so MojoModel file handling will create encrypted VaultFile records automatically when a relation field points at VaultFile.

If you need custom routing (for example, custom password logic), override your model’s `on_rest_save_file` and call `vault_service.upload_file(...)` directly using `self.active_request` for user/group context.

Example override in your model (no code changes in FileVault required):

```python
from mojo.apps.filevault.services import vault as vault_service

def on_rest_save_file(self, name, file):
    if name != "attachment":
        return
    req = self.active_request
    self.attachment = vault_service.upload_file(
        file_obj=file,
        name=file.name,
        group=req.group,
        user=req.user,
        password=req.DATA.get("password"),
    )
```

---

## REST API Endpoints

### File Endpoints

| Method | Path                    | Auth     | Description                                   |
| ------ | ----------------------- | -------- | --------------------------------------------- |
| GET    | `file`                  | Required | List files (scoped to user's group)           |
| POST   | `file`                  | Required | Create/update metadata only                   |
| GET    | `file/<pk>`             | Required | Get file metadata                             |
| PUT    | `file/<pk>`             | Required | Update file metadata                          |
| DELETE | `file/<pk>`             | Required | Delete file (removes S3 object and DB record) |
| POST   | `file/upload`           | Required | Upload + encrypt file                         |
| POST   | `file/<pk>/unlock`      | Required | Generate a signed download token              |
| POST   | `file/<pk>/password`    | Required | Verify a password without downloading         |
| GET    | `file/download/<token>` | None     | Download file using signed token              |

### Data Endpoints

| Method | Path        | Auth     | Description                          |
| ------ | ----------- | -------- | ------------------------------------ |
| GET    | `data`      | Required | List data records (metadata only)    |
| POST   | `data`      | Required | Create/update metadata only          |
| GET    | `data/<pk>` | Required | Get data metadata                    |
| PUT    | `data/<pk>` | Required | Update data metadata                 |
| DELETE | `data/<pk>` | Required | Delete data                          |
| POST   | `data/store` | Required | Encrypt + store JSON payload        |
| POST   | `data/<pk>/retrieve` | Required | Decrypt + return JSON payload |

### Custom Endpoint Payloads

- `POST file/upload`: multipart with `file` plus optional `name`, `description`, `password`, `metadata` (JSON string or object). Returns VaultFile metadata.
- `POST file/<pk>/unlock`: optional `ttl` (seconds). Returns `token`, `download_url`, `ttl`.
- `POST file/<pk>/password`: `password` required. Returns `valid`.
- `GET file/download/<token>`: optional `password` if the file is protected. Streams file bytes.
- `POST data/store`: `name` and `data` required; optional `password`, `description`, `metadata`. Returns VaultData metadata.
- `POST data/<pk>/retrieve`: optional `password`. Returns decrypted `data`.

### Response Graphs

**VaultFile default graph:**

```json
{
    "id": 123,
    "created": "2026-02-11T10:00:00Z",
    "name": "report.pdf",
    "content_type": "application/pdf",
    "description": "Monthly report",
    "size": 5242880,
    "is_encrypted": 2,
    "requires_password": false,
    "metadata": { ... },
    "user": { "id": 1, "username": "jdoe" },
    "unlocked_by": null
}
```

The `ekey`, `hashed_password`, `uuid`, and `chunk_count` fields are never included in API responses.

---

## Security Summary

### Encryption Layers

```
Layer 1: ekey encrypted with SECRET_KEY (database at rest)
Layer 2: File encrypted with ekey + optional password (storage backend at rest)
Layer 3: Signed, IP-bound tokens (download access)
Layer 4: TLS via nginx (transport)
```

### Compromise Scenarios

| What's compromised         | What the attacker gets              | Can they read files?                                         |
| -------------------------- | ----------------------------------- | ------------------------------------------------------------ |
| S3 bucket only             | Ciphertext blobs                    | No                                                           |
| Database only              | Wrapped (encrypted) ekeys, metadata | No                                                           |
| Database + S3              | Ciphertext + wrapped ekeys          | No (need SECRET_KEY to unwrap)                               |
| SECRET_KEY + database      | Unwrapped ekeys                     | Yes, except password-protected files                         |
| SECRET_KEY + database + S3 | Everything except passwords         | Yes for non-password files; need password for protected files |
| Download token leaked      | Token bound to original IP          | No (unless attacker shares the IP)                           |

### What Lives Where

| Secret            | Location                          | Exposure                                              |
| ----------------- | --------------------------------- | ----------------------------------------------------- |
| SECRET_KEY        | Server settings (env var or file) | Never in database, never in API responses             |
| ekey (wrapped)    | Database                          | Encrypted, never in API responses                     |
| ekey (unwrapped)  | Server memory during request      | Exists only for duration of encrypt/decrypt operation |
| User password     | Nowhere (hashed only)             | PBKDF2 hash in database, never stored plaintext       |
| AES key (derived) | Server memory during request      | Derived from ekey+password+salt, never stored         |
| File plaintext    | Server memory during operations   | Upload loads full file; download streams by chunk     |

---

## Dependencies

| Package            | Purpose                                       | Notes                                        |
| ------------------ | --------------------------------------------- | -------------------------------------------- |
| `hashlib` (stdlib) | PBKDF2-HMAC-SHA256                            | No install needed                            |
| `hmac` (stdlib)    | Token signing, constant-time comparison       | No install needed                            |
| `json` (stdlib)    | Token payload encoding                        | No install needed                            |
| `boto3`            | S3 backend operations (via fileman)           | Already in use                               |
| `pycryptodome`     | AES-256-GCM, PBKDF2, secure random generation | Already in use                               |

---

## Configuration

| Setting                | Description                                      | Example                      |
| ---------------------- | ------------------------------------------------ | ---------------------------- |
| `SECRET_KEY`           | Root of trust for key wrapping and token signing | Set via environment variable |

Encryption parameters (chunk size, token TTL, KDF iterations) are currently defined as constants in `mojo/helpers/crypto/vault.py` (`VAULT_CHUNK_SIZE`, `VAULT_TOKEN_TTL`, `VAULT_KDF_ITERATIONS`).
