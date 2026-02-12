# FileVault — Design Document

## Overview

FileVault is an encrypted file storage app for django-mojo. It stores files as ciphertext in S3 (via fileman) and encrypted structured data in the database. An S3 compromise yields useless blobs; a database compromise yields wrapped keys that require `SECRET_KEY` to unwrap.

Two storage primitives:

- **VaultFile** — Encrypted file storage in S3 with optional password protection and signed download tokens.
- **VaultData** — Encrypted structured JSON stored in the database.

---

## Architecture

```
Client
  |
  v
nginx (TLS termination)
  |
  v
Django (auth, permissions, encryption/decryption)
  |
  ├── Database (PostgreSQL)
  │     └── Wrapped ekeys, metadata, password hashes
  |
  └── FileManager (use="filevault")
        └── S3 backend → ciphertext blobs only
```

All encryption/decryption happens in the Django process. S3 never sees plaintext. The database holds metadata and wrapped keys but never file content.

### Integration with FileMan

FileVault delegates all S3 operations to fileman's existing `FileManager` and `S3StorageBackend`:

- **Storage routing**: `FileManager.get_for_group(group, use="filevault")` auto-provisions a filevault-scoped manager inheriting credentials from the system default.
- **Upload/download**: Uses `backend.save()`, `backend.open()`, `backend.delete()` from fileman's S3 backend.
- **No duplicate S3 code**: FileVault never creates boto3 clients directly.
- **Path layout**: Automatic via fileman — `s3://{bucket}/{group_uuid}/filevault/{uuid}`.
- **FileVault always uses `is_public=False`** on its FileManager since all files are encrypted and require token-based access.

---

## Encryption

### Algorithm: AES-256-GCM

All file encryption uses AES-256-GCM via pycryptodome (already a dependency). GCM provides:

- **Confidentiality** — Data unreadable without the key.
- **Authenticity** — Tampering detected on decryption. No separate integrity hash needed.
- **Performance** — Hardware-accelerated on modern CPUs via AES-NI.

### Chunk-Based Streaming

Files are split into fixed-size chunks (default 5MB) for encryption. Each chunk is encrypted independently with its own nonce and authentication tag. This enables:

- **Bounded memory** — One plaintext chunk + one ciphertext chunk in memory at a time (~10MB regardless of file size).
- **Streaming upload** — Chunks encrypted and uploaded as they arrive.
- **Streaming download** — Chunks fetched from S3, decrypted, streamed to client.
- **Per-chunk integrity** — Tampered chunk detected immediately.
- **No file size limit** — Chunked approach has no practical ceiling.

### Key Derivation: PBKDF2-HMAC-SHA256

Raw encryption keys are never used directly as AES keys. PBKDF2 transforms them:

```
aes_key = PBKDF2-HMAC-SHA256(
    passphrase,       # ekey, or password + ekey if password-protected
    salt,             # 32 random bytes, unique per file, stored in file header
    iterations=600000,
    dklen=32          # 256-bit AES key
)
```

600,000 iterations makes brute-force expensive for password-protected files. For non-password files, KDF still provides proper key derivation from the high-entropy `ekey`.

Uses `Crypto.Protocol.KDF.PBKDF2` from pycryptodome (already available — see `mojo/helpers/crypto/aes.py`).

The KDF runs once per file operation. The derived AES key is used for all chunks.

---

## Additions to `mojo/helpers/crypto`

The existing crypto module handles single-shot AES-256-GCM (see `aes.py`) but has no streaming/chunked support. FileVault needs:

### New file: `mojo/helpers/crypto/vault.py`

```python
# Constants
VAULT_MAGIC = b"VF02"
VAULT_HEADER_SIZE = 64
VAULT_CHUNK_SIZE = 5 * 1024 * 1024  # 5MB
VAULT_KDF_ITERATIONS = 600_000
VAULT_SALT_LENGTH = 32
VAULT_NONCE_LENGTH = 12
VAULT_TAG_LENGTH = 16

# Functions
def generate_ekey()
    # Returns 32-char random alphanumeric string (~190 bits entropy)
    # Uses crypto.random_string(32, allow_special=False)

def derive_aes_key(passphrase, salt)
    # PBKDF2-HMAC-SHA256, 600k iterations, 32-byte output

def derive_chunk_nonce(aes_key, chunk_index)
    # HMAC-SHA256(aes_key, chunk_index_as_4_byte_big_endian)[:12]

def encrypt_chunk(aes_key, chunk_index, plaintext)
    # Returns: nonce + ciphertext + tag

def decrypt_chunk(aes_key, chunk_index, chunk_data)
    # Verifies nonce derivation, decrypts, returns plaintext
    # Raises on auth failure

def build_header(chunk_size, kdf_salt, total_chunks)
    # Returns 64-byte header: magic(4) + chunk_size(4) + salt(32) + total_chunks(4) + reserved(20)

def parse_header(header_bytes)
    # Returns dict with chunk_size, kdf_salt, total_chunks
    # Raises on invalid magic

def wrap_ekey(ekey, secret_key, file_uuid)
    # Derives per-file wrapping passphrase: secret_key + file_uuid
    # Encrypts ekey with AES-256-GCM + PBKDF2
    # Returns base64 string: [wrap_salt(32)][wrap_nonce(12)][ciphertext][tag(16)]

def unwrap_ekey(wrapped_b64, secret_key, file_uuid)
    # Derives per-file wrapping passphrase: secret_key + file_uuid
    # Decrypts wrapped ekey, returns plaintext ekey string

def hash_password(password)
    # PBKDF2-HMAC-SHA256 with random 32-byte salt, 600k iterations
    # Returns base64 string: [salt(32)][hash(32)]

def verify_password(password, stored_hash)
    # Constant-time comparison via hmac.compare_digest

def generate_access_token(file_id, client_ip, ttl, secret_key)
    # HMAC-SHA256 signed, base64url payload: {fid, ip, exp, iat}
    # Returns: base64url(payload).base64url(signature)

def validate_access_token(token, client_ip, secret_key)
    # Verifies signature, expiry, IP binding
    # Returns file_id on success, None on failure
```

These are pure utility functions — no Django models, no I/O. They use only pycryptodome and stdlib (`hashlib`, `hmac`, `struct`, `json`, `base64`).

**Why a separate file instead of extending `aes.py`?** The vault functions have different constants (600k iterations vs 100k, 32-byte salt vs 16-byte) and vault-specific formats (chunked headers, key wrapping, access tokens). Keeping them separate avoids polluting the general-purpose crypto API.

---

## Key Management

### Per-File Encryption Key (ekey)

Every file gets a unique key generated at upload time:

```
ekey = 32 alphanumeric chars via crypto.random_string (~190 bits entropy)
```

Never exposed through the REST API. Cannot be set or read via any endpoint.

### Wrapped Key Storage

The `ekey` is encrypted before database storage using a **per-file wrapping passphrase** derived from `SECRET_KEY` + the file's `uuid`:

```
Wrapping:
  wrap_passphrase = SECRET_KEY + file.uuid    # per-file, no single key unlocks all
  wrap_salt = 32 random bytes
  wrap_key = PBKDF2-HMAC-SHA256(wrap_passphrase, wrap_salt, 600k iterations, 32 bytes)
  wrap_nonce = 12 random bytes
  encrypted_ekey, tag = AES-256-GCM(wrap_key, wrap_nonce, ekey)

Stored (base64):
  [wrap_salt(32)][wrap_nonce(12)][encrypted_ekey][tag(16)]
```

Compromise scenarios:
- **Database only** — Wrapped keys useless without `SECRET_KEY`.
- **S3 only** — Ciphertext useless without `ekey`.
- **Database + S3, no SECRET_KEY** — Still protected.
- **SECRET_KEY only (no database)** — Useless. Each file's wrapping key requires its `uuid` from the database.
- **SECRET_KEY + database** — Can unwrap ekeys for non-password files. Password-protected files still require the password.

### Optional Password Protection

When a password is set:
1. Password hashed with PBKDF2 for verification.
2. Passphrase for encryption = `password + ekey`.
3. Decryption requires `ekey` (from DB, unwrapped with `SECRET_KEY`) **and** password.

---

## Password Handling

### Hashing for Verification

```
password_salt = 32 random bytes
hashed = PBKDF2-HMAC-SHA256(password, password_salt, 600k iterations, 32 bytes)

Stored (base64): [salt(32)][hash(32)]
```

Verification uses `hmac.compare_digest` for constant-time comparison.

### Role in Encryption

The password is part of the encryption key, not just an access gate. A password-protected file cannot be decrypted without the password, even with full database and server access.

---

## Encrypted File Format

Each encrypted file is a single S3 object:

```
[Header — 64 bytes]
  Magic       (4 bytes):  "VF02"
  Chunk size  (4 bytes):  uint32 big-endian (default: 5,242,880)
  KDF salt    (32 bytes): Random, for PBKDF2 key derivation
  Total chunks(4 bytes):  uint32 big-endian
  Reserved    (20 bytes): Zeroed

[Chunk 0]
  Nonce       (12 bytes): Derived from chunk index
  Ciphertext  (variable): AES-GCM encrypted data
  Auth tag    (16 bytes): GCM authentication tag

[Chunk 1] ...
[Chunk N — final chunk, may be smaller]
```

### Nonce Derivation

```
nonce = HMAC-SHA256(aes_key, chunk_index_as_4_byte_big_endian)[:12]
```

Guarantees uniqueness per chunk and prevents chunk reordering.

### Byte Offset Calculation

```
chunk_data_size = chunk_size + 12 (nonce) + 16 (tag)
chunk_offset = 64 (header) + (chunk_index * chunk_data_size)
```

Enables S3 range requests for individual chunks.

---

## Access Tokens: Signed, IP-Bound, Stateless

### Token Structure

```
payload = {
    "fid": <file PK>,
    "ip":  <client IP>,
    "exp": <expiration UTC timestamp>,
    "iat": <issued-at UTC timestamp>
}

signature = HMAC-SHA256(SECRET_KEY, base64url(payload))
token = base64url(payload) + "." + base64url(signature)
```

### Generation (Unlock)

1. Verify user permission.
2. Read client IP from request.
3. Construct payload with file ID, IP, expiration (default 300s).
4. Sign with HMAC-SHA256 using `SECRET_KEY`.
5. Return token + download URL.

### Validation (Download)

1. Split at `.`, verify HMAC signature.
2. Check `exp > now`.
3. Check `ip == requesting IP`.
4. Look up file, verify password if required.
5. Proceed with decrypt + stream.

### Security Properties

| Threat | Mitigation |
|--------|-----------|
| Token leaked (logs, referrer) | Bound to original IP |
| Token replay from same IP | Short expiry (5 min default) |
| Token forgery | Requires SECRET_KEY for valid HMAC |
| Token tampering | HMAC verification fails |

---

## Data Model

### VaultFile

```python
class VaultFile(models.Model, MojoModel):
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", null=True, on_delete=models.SET_NULL)
    group = models.ForeignKey("account.Group", on_delete=models.CASCADE)

    uuid = models.CharField(max_length=64, unique=True, db_index=True)  # S3 object key component
    name = models.CharField(max_length=200)                              # Original filename
    content_type = models.CharField(max_length=128)                      # MIME type
    description = models.TextField(blank=True, null=True, default=None)
    size = models.BigIntegerField(default=0)                             # Original file size
    chunk_count = models.IntegerField(default=0)
    is_encrypted = models.IntegerField(default=2)                        # 0=plaintext, 2=AES-256-GCM
    ekey = models.TextField()                                            # Wrapped encryption key
    hashed_password = models.TextField(blank=True, null=True, default=None)
    unlocked_by = models.ForeignKey("account.User", null=True, on_delete=models.SET_NULL,
                                     related_name="vault_unlocked_files")
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        CAN_SAVE = True
        CAN_CREATE = True
        CAN_DELETE = True
        VIEW_PERMS = ["view_vault", "manage_vault", "owner"]
        SAVE_PERMS = ["manage_vault", "owner"]
        DELETE_PERMS = ["manage_vault", "owner"]
        SEARCH_FIELDS = ["name", "content_type", "description"]
        NO_SAVE_FIELDS = ["id", "pk", "ekey", "uuid", "chunk_count",
                          "hashed_password", "is_encrypted"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified", "name", "content_type",
                    "description", "size", "is_encrypted", "metadata"
                ],
                "extra": ["requires_password"],
                "graphs": {
                    "user": "basic",
                    "unlocked_by": "basic",
                    "group": "basic"
                }
            },
            "basic": {
                "fields": [
                    "id", "name", "content_type", "size", "is_encrypted"
                ],
                "extra": ["requires_password"]
            },
            "list": {
                "fields": [
                    "id", "created", "name", "content_type", "size",
                    "is_encrypted"
                ],
                "extra": ["requires_password"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            }
        }

    @property
    def requires_password(self):
        return self.hashed_password is not None
```

**Notes:**
- Uses `user`/`group` per MOJO convention (not `member` from the proposal).
- `ekey` and `hashed_password` excluded from all graphs — never in API responses.
- `NO_SAVE_FIELDS` prevents REST clients from setting crypto fields.
- Regular model (`models.Model, MojoModel`) — no MojoSecrets needed since we handle key wrapping ourselves via `crypto.vault.wrap_ekey()`.

### VaultData

Encrypted JSON stored in the database (not S3). For small structured secrets.

```python
class VaultData(models.Model, MojoModel):
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", null=True, on_delete=models.SET_NULL)
    group = models.ForeignKey("account.Group", on_delete=models.CASCADE)

    name = models.CharField(max_length=64)
    description = models.TextField(blank=True, null=True, default=None)
    ekey = models.TextField()                          # Wrapped encryption key
    edata = models.TextField()                         # AES-256-GCM encrypted JSON (base64)
    hashed_password = models.TextField(blank=True, null=True, default=None)
    metadata = models.JSONField(default=dict, blank=True)

    class RestMeta:
        CAN_SAVE = True
        CAN_CREATE = True
        CAN_DELETE = True
        VIEW_PERMS = ["view_vault", "manage_vault", "owner"]
        SAVE_PERMS = ["manage_vault", "owner"]
        DELETE_PERMS = ["manage_vault", "owner"]
        NO_SAVE_FIELDS = ["id", "pk", "ekey", "edata", "hashed_password"]

        GRAPHS = {
            "default": {
                "fields": [
                    "id", "created", "modified", "name", "description", "metadata"
                ],
                "extra": ["requires_password"],
                "graphs": {
                    "user": "basic",
                    "group": "basic"
                }
            },
            "basic": {
                "fields": ["id", "name", "description"],
                "extra": ["requires_password"]
            }
        }

    @property
    def requires_password(self):
        return self.hashed_password is not None
```

Uses same encryption/key-wrapping as VaultFile but without chunking — data is small enough for single-operation GCM.

---

## REST API Endpoints

### File Endpoints

```python
# Standard CRUD → /api/filevault/file, /api/filevault/file/<pk>
@md.URL('file')
@md.URL('file/<int:pk>')
def on_vault_file(request, pk=None):
    return VaultFile.on_rest_request(request, pk)

# Upload (custom — handles encryption + streaming)
@md.POST('file/upload')
def on_vault_file_upload(request):
    # Accepts multipart file upload
    # Encrypts in chunks, stores via FileManager(use="filevault")
    # Returns VaultFile metadata

# Unlock (generate download token)
@md.POST('file/<int:pk>/unlock')
def on_vault_file_unlock(request, pk=None):
    # Verify permissions
    # Generate signed, IP-bound access token
    # Return token + download URL

# Password verify (without downloading)
@md.POST('file/<int:pk>/password')
def on_vault_file_password(request, pk=None):
    # Verify password is correct
    # Returns success/failure, no file content

# Download (token-based, no auth required)
@md.GET('file/download/<str:token>')
@md.public_endpoint("Token-secured vault file download")
def on_vault_file_download(request, token=None):
    # Validate token (signature, expiry, IP)
    # If password-protected, verify password from request
    # Decrypt + stream file
```

### Data Endpoints

```python
# Standard CRUD → /api/filevault/data, /api/filevault/data/<pk>
@md.URL('data')
@md.URL('data/<int:pk>')
def on_vault_data(request, pk=None):
    return VaultData.on_rest_request(request, pk)

# Store encrypted data
@md.POST('data/store')
def on_vault_data_store(request):
    # Accepts JSON payload
    # Encrypts with AES-256-GCM (single-shot, not chunked)
    # Returns VaultData metadata

# Retrieve decrypted data
@md.POST('data/<int:pk>/retrieve')
def on_vault_data_retrieve(request, pk=None):
    # Verify permissions
    # If password-protected, verify password
    # Decrypt and return JSON payload
```

### URL Summary

All paths are auto-prefixed with `/api/filevault/` by the framework.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `file` | Required | List files (group-scoped) |
| POST | `file` | Required | Create file metadata |
| GET | `file/<pk>` | Required | Get file metadata |
| PUT | `file/<pk>` | Required | Update file metadata |
| DELETE | `file/<pk>` | Required | Delete file + S3 object |
| POST | `file/upload` | Required | Upload + encrypt file |
| POST | `file/<pk>/unlock` | Required | Generate download token |
| POST | `file/<pk>/password` | Required | Verify password |
| GET | `file/download/<token>` | None | Download via signed token |
| GET | `data` | Required | List data records |
| POST | `data` | Required | Create data metadata |
| GET | `data/<pk>` | Required | Get data metadata |
| PUT | `data/<pk>` | Required | Update data |
| DELETE | `data/<pk>` | Required | Delete data |
| POST | `data/store` | Required | Store encrypted JSON |
| POST | `data/<pk>/retrieve` | Required | Retrieve decrypted JSON |

---

## Upload Flow

```
1. Client POSTs file to /api/filevault/file/upload (authenticated, multipart).

2. Resolve FileManager:
   fm = FileManager.get_for_group(request.group, use="filevault")

3. Create VaultFile record:
   a. Generate UUID for S3 path.
   b. Generate ekey (32 random alphanumeric chars).
   c. Generate KDF salt (32 random bytes).
   d. If password provided:
      - Hash password with PBKDF2, store hash.
      - Passphrase = password + ekey.
   e. Else: passphrase = ekey.
   f. Derive AES key via PBKDF2(passphrase, salt, 600k iterations).
   g. Wrap ekey with SECRET_KEY, store wrapped blob.

4. Build 64-byte file header.

5. Encrypt file body in chunks:
   a. Read up to 5MB from uploaded file.
   b. Derive nonce from chunk index.
   c. Encrypt chunk with AES-256-GCM.
   d. Append to buffer.
   e. Repeat until EOF.

6. Save complete encrypted blob via fm.backend.save().

7. Update VaultFile with final size and chunk count.
```

**Memory**: ~10MB peak (one plaintext + one ciphertext chunk).

---

## Download Flow

```
1. Authenticated user POSTs to /api/filevault/file/<pk>/unlock.
2. Django verifies permission (VIEW_PERMS).
3. Generates signed token (file ID, client IP, expiration).
4. Returns download URL with token.

5. Client GETs /api/filevault/file/download/<token>.
6. Validate token (signature, expiry, IP).
7. If password-protected, verify password from request params.
8. Unwrap ekey from database using SECRET_KEY.
9. Derive AES key via PBKDF2.
10. Open file from S3 via fm.backend.open().
11. Read and parse 64-byte header.
12. Stream chunks:
    a. Read encrypted chunk.
    b. Decrypt with AES-256-GCM (verify auth tag).
    c. Yield plaintext to StreamingHttpResponse.
13. Response streams through to client.
```

**Error handling**: If any chunk fails GCM auth, stream aborts immediately. Client detects via `Content-Length` mismatch.

---

## Service Layer

Business logic lives in `mojo/apps/filevault/services/vault.py`:

```python
# File operations
def upload_file(request, file_obj, password=None)
    # Orchestrates: FM resolution, ekey gen, encryption, S3 save, DB record
    # Returns VaultFile instance

def download_file(vault_file, password=None)
    # Returns generator that yields decrypted chunks
    # For use with StreamingHttpResponse

def generate_download_token(vault_file, request)
    # Creates signed IP-bound token
    # Returns token string + download URL

def validate_download_token(token, request)
    # Returns VaultFile on success, None on failure

# Data operations
def store_data(group, user, name, data, password=None)
    # Encrypts JSON, stores in DB
    # Returns VaultData instance

def retrieve_data(vault_data, password=None)
    # Decrypts and returns dict

# Key operations (delegate to crypto.vault)
def wrap_and_store_ekey(vault_obj, ekey)
def unwrap_ekey(vault_obj)
def verify_file_password(vault_file, password)
```

---

## File Organization

```
mojo/apps/filevault/
├── __init__.py
├── apps.py
├── models/
│   ├── __init__.py          # exports: VaultFile, VaultData
│   ├── file.py              # VaultFile model
│   └── data.py              # VaultData model
├── rest/
│   ├── __init__.py          # imports all endpoint modules
│   ├── file.py              # CRUD + upload + unlock + download endpoints
│   └── data.py              # CRUD + store + retrieve endpoints
├── services/
│   ├── __init__.py
│   └── vault.py             # encryption/upload/download orchestration
└── migrations/

mojo/helpers/crypto/
├── vault.py                 # NEW: chunked encryption, key wrapping, access tokens
└── (existing files unchanged)
```

---

## Security Summary

### Encryption Layers

```
Layer 1: ekey encrypted with SECRET_KEY (database at rest)
Layer 2: File encrypted with ekey + optional password (S3 at rest)
Layer 3: Signed, IP-bound tokens (download access)
Layer 4: TLS via nginx (transport)
Layer 5: MOJO permissions (user/group/owner via RestMeta)
```

### Compromise Scenarios

| Compromised | Can read files? |
|-------------|----------------|
| S3 only | No — ciphertext only |
| Database only | No — wrapped keys need SECRET_KEY |
| SECRET_KEY only | No — needs per-file uuid from database |
| Database + S3 | No — need SECRET_KEY to unwrap |
| SECRET_KEY + database | Non-password files yes; password files no |
| SECRET_KEY + database + S3 | Non-password yes; password files need password |
| Download token leaked | No — bound to original IP + short expiry |

### What Lives Where

| Secret | Location | Exposure |
|--------|----------|----------|
| SECRET_KEY | Server env/settings | Never in DB or API |
| ekey (wrapped) | Database | Encrypted, never in API |
| ekey (unwrapped) | Server memory | Only during encrypt/decrypt |
| User password | Nowhere (hashed) | PBKDF2 hash in DB only |
| AES key (derived) | Server memory | Never stored |
| File plaintext | Server memory | One chunk at a time |

---

## Dependencies

| Package | Purpose | Status |
|---------|---------|--------|
| `pycryptodome` | AES-256-GCM, PBKDF2, secure random | Already installed |
| `hashlib` (stdlib) | HMAC, SHA-256 | Built-in |
| `hmac` (stdlib) | Token signing, constant-time compare | Built-in |
| `struct` (stdlib) | Header packing/unpacking | Built-in |
| `boto3` | S3 operations (via fileman backend) | Already installed |

No new dependencies required.

---

## Configuration

| Setting | Description | Default |
|---------|-------------|---------|
| `SECRET_KEY` | Root of trust for key wrapping + token signing | (Django setting) |
| `VAULT_CHUNK_SIZE` | Chunk size in bytes | `5242880` (5MB) |
| `VAULT_TOKEN_TTL` | Download token lifetime in seconds | `300` |
| `VAULT_KDF_ITERATIONS` | PBKDF2 iteration count | `600000` |

All accessed via `settings.get("VAULT_*", default)`.

S3 bucket/credentials are inherited from fileman's system-default FileManager — no vault-specific S3 config needed.

---

## Implementation Order

1. **`mojo/helpers/crypto/vault.py`** — Pure crypto functions (ekey gen, wrap/unwrap, chunk encrypt/decrypt, header pack/parse, password hash/verify, token sign/validate). Fully testable in isolation.
2. **Models** — VaultFile and VaultData. Straightforward Django models with RestMeta. Both use `metadata` JSONField for arbitrary key/value storage.
3. **Service layer** — `services/vault.py` orchestrating crypto + fileman + models.
4. **REST endpoints** — Thin handlers delegating to service layer.
5. **Tests** — Each layer independently.
