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

### Chunk-Based Streaming

Files are split into fixed-size chunks (default 5MB) for encryption. Each chunk is encrypted independently with its own nonce and authentication tag. This enables:

- **Bounded memory usage** — At most one plaintext chunk and one ciphertext chunk are in memory at any time (~10MB total regardless of file size).
- **Streaming upload** — Chunks are encrypted and uploaded via S3 multipart upload as they arrive through ASGI/nginx.
- **Streaming download** — Chunks are downloaded from S3 via range requests, decrypted, and streamed to the client.
- **Per-chunk integrity** — A tampered chunk is detected immediately without processing the entire file.
- **No file size limit** — The chunk-based approach has no practical ceiling.

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

The `ekey` is encrypted before being stored in the database using the server's `SECRET_KEY`:

```
Wrapping:
  wrap_salt = 32 random bytes
  wrap_key = PBKDF2-HMAC-SHA256(SECRET_KEY, wrap_salt, iterations=600000, dklen=32)
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

## S3 Storage

### Layout

```
s3://{VAULT_S3_BUCKET}/vault/files/{uuid}
```

Each file is a single S3 object. The UUID is generated at upload time and has no relation to the encryption key.

### Upload: S3 Multipart

Files are uploaded using S3 multipart upload:

1. Initiate multipart upload.
2. For each chunk: encrypt and upload as a part.
3. Complete multipart upload.

The 5MB chunk size matches S3's minimum multipart part size. This is handled by `boto3`.

### Download: Range Requests

On download, the file header is fetched first (bytes 0-63) to read the KDF salt, chunk size, and chunk count. Each subsequent chunk is fetched via an S3 range request, decrypted, and streamed to the client.

Alternatively, the entire object can be streamed sequentially and chunked in the application layer, which avoids the per-request overhead of range requests. The choice depends on whether random-access to individual chunks is needed (it generally isn't for sequential downloads).

### Deletion

Deleting a VaultFile deletes the single S3 object and the database record. No orphaned chunks or manifests.

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
6. An audit log entry is written (who unlocked which file, when, from what IP).

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

## Upload Flow

```
1. Client POSTs file to the upload endpoint (authenticated, streamed via ASGI/nginx).

2. Create VaultFile record:
   a. Generate UUID for S3 path.
   b. Generate ekey (32 random alphanumeric characters).
   c. Generate KDF salt (32 random bytes).
   d. If password provided:
      - Hash password with PBKDF2 and store hash.
      - Combine password + ekey as passphrase.
   e. Derive AES key via PBKDF2(passphrase, salt, 600k iterations).
   f. Encrypt ekey with SECRET_KEY and store wrapped blob.

3. Initiate S3 multipart upload.

4. Write file header (64 bytes) as first part of the data stream.

5. Stream file body in chunks:
   a. Read up to 5MB from the request body.
   b. Derive nonce from chunk index.
   c. Encrypt chunk with AES-256-GCM.
   d. Upload as S3 multipart part.
   e. Increment chunk count.
   f. Repeat until request body is exhausted.

6. Complete S3 multipart upload.

7. Update VaultFile record with final size and chunk count.
```

**Memory usage**: ~10MB peak (one plaintext chunk + one ciphertext chunk).

---

## Download Flow

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
11. Fetch file header from S3 (first 64 bytes).
12. Stream chunks:
    a. Fetch encrypted chunk from S3.
    b. Decrypt with AES-256-GCM (validates auth tag).
    c. Yield plaintext chunk to StreamingHttpResponse.
13. Response streams through ASGI/nginx to client.
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
| member          | FK(Member)      | User who uploaded the file                         |
| unlocked_by     | FK(Member)      | User who last generated a download token           |
| group           | FK(Group)       | Owning organization (CASCADE delete)               |

**Fields hidden from REST API** (`NO_SHOW_FIELDS`): `ekey`, `hashed_password`

**Fields not settable via REST API** (`NO_SAVE_FIELDS`): `id`, `pk`, `ekey`, `uuid`, `chunk_count`

### VaultFileMetaData

Key/value metadata storage for VaultFile. Extends `MetaDataBase`.

| Field    | Type          | Description                   |
| -------- | ------------- | ----------------------------- |
| parent   | FK(VaultFile) | Related file (CASCADE delete) |
| category | CharField     | Metadata category             |
| key      | CharField     | Metadata key                  |
| value    | TextField     | Metadata value                |

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
| member          | FK(Member)    | Owning member                         |
| group           | FK(Group)     | Owning organization (CASCADE delete)  |

VaultData uses the same encryption (AES-256-GCM) and key wrapping as VaultFile, but without chunking — the data is small enough for single-operation GCM.

### VaultDataMetaData

Key/value metadata storage for VaultData. Same structure as VaultFileMetaData.

---

## REST API Endpoints

### File Endpoints

| Method | Path                    | Auth     | Description                                   |
| ------ | ----------------------- | -------- | --------------------------------------------- |
| GET    | `file`                  | Required | List files (scoped to user's group)           |
| POST   | `file`                  | Required | Upload a new file                             |
| GET    | `file/<pk>`             | Required | Get file metadata                             |
| PUT    | `file/<pk>`             | Required | Update file metadata                          |
| DELETE | `file/<pk>`             | Required | Delete file (removes S3 object and DB record) |
| POST   | `file/<pk>/unlock`      | Required | Generate a signed download token              |
| POST   | `file/<pk>/password`    | Required | Verify a password without downloading         |
| GET    | `file/download/<token>` | None     | Download file using signed token              |

### Data Endpoints

| Method | Path        | Auth     | Description           |
| ------ | ----------- | -------- | --------------------- |
| GET    | `data`      | Required | List data records     |
| POST   | `data`      | Required | Create encrypted data |
| GET    | `data/<pk>` | Required | Get data metadata     |
| PUT    | `data/<pk>` | Required | Update data           |
| DELETE | `data/<pk>` | Required | Delete data           |

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
    "member": { "id": 1, "username": "jdoe" },
    "unlocked_by": null
}
```

The `ekey`, `hashed_password`, `uuid`, and `chunk_count` fields are never included in API responses.

---

## Security Summary

### Encryption Layers

```
Layer 1: ekey encrypted with SECRET_KEY (database at rest)
Layer 2: File encrypted with ekey + optional password (S3 at rest)
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
| File plaintext    | Server memory during streaming    | One chunk at a time (~5MB), never stored on disk      |

---

## Dependencies

| Package            | Purpose                                       | Notes                                        |
| ------------------ | --------------------------------------------- | -------------------------------------------- |
| `hashlib` (stdlib) | PBKDF2-HMAC-SHA256                            | No install needed                            |
| `hmac` (stdlib)    | Token signing, constant-time comparison       | No install needed                            |
| `json` (stdlib)    | Token payload encoding                        | No install needed                            |
| `cryptography`     | AES-256-GCM encrypt/decrypt                   | Widely used, actively maintained, has wheels |
| `boto3`            | S3 multipart upload, range requests, deletion | Already in use                               |
| `pycryptodome`     | Secure random generation (`Crypto.Random`)    | Already in use                               |

---

## Configuration

| Setting                | Description                                      | Example                      |
| ---------------------- | ------------------------------------------------ | ---------------------------- |
| `SECRET_KEY`           | Root of trust for key wrapping and token signing | Set via environment variable |
| `VAULT_S3_BUCKET`      | S3 bucket for encrypted file storage             | `"camlock"`                  |
| `VAULT_CHUNK_SIZE`     | Chunk size in bytes (default 5MB)                | `5242880`                    |
| `VAULT_TOKEN_TTL`      | Default download token lifetime in seconds       | `300`                        |
| `VAULT_KDF_ITERATIONS` | PBKDF2 iteration count                           | `600000`                     |
