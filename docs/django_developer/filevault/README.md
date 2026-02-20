# FileVault — Django Developer Reference

FileVault provides encrypted file storage and structured data storage with optional password protection and signed, IP-bound download tokens.

## Models

### VaultFile

AES-256-GCM encrypted files stored in S3 (or configured backend).

```python
from mojo.apps.filevault.models import VaultFile
from mojo.apps.filevault.services import vault as vault_service

# Upload and encrypt a file
vault_file = vault_service.upload_file(
    file_obj=uploaded_file,
    name="confidential.pdf",
    group=request.group,
    user=request.user,
    password="optional-user-password",   # additional password layer
    description="Q4 Financial Report",
    metadata={"department": "finance"}
)

# Generate a signed, IP-bound download token
client_ip = get_remote_ip(request)
token = vault_service.generate_download_token(vault_file, client_ip, ttl=300)
download_url = f"/api/filevault/file/download/{token}"

# Validate token and download (in a handler)
vault_file = vault_service.validate_download_token(token, client_ip)
chunks = vault_service.download_file_streaming(vault_file, password="optional-password")
```

### VaultData

Encrypted JSON data stored directly in the database (no S3 needed).

```python
from mojo.apps.filevault.models import VaultData

# Store encrypted structured data
vault_data = VaultData.objects.create(
    name="API Credentials",
    group=group,
    user=user,
    description="Production API keys",
)
vault_data.set_secret("api_key", "sk-prod-abc123")
vault_data.set_secret("api_secret", "xyz789")
vault_data.save()

# Retrieve
api_key = vault_data.get_secret("api_key")
```

## Key Fields — VaultFile

| Field | Description |
|---|---|
| `name` | User-provided file name |
| `description` | Optional description |
| `content_type` | MIME type |
| `size` | File size in bytes |
| `hashed_password` | Bcrypt hash of optional password |
| `is_encrypted` | Encryption state flag |
| `chunk_count` | Number of encrypted chunks |
| `metadata` | JSONField for custom data |
| `group` | FK → Group (required) |
| `user` | FK → User (uploader) |
| `unlocked_by` | FK → User (last unlock) |

## RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["file_vault", "manage_files"]
    SAVE_PERMS = ["file_vault", "manage_files"]
    DELETE_PERMS = ["file_vault", "manage_files"]
    CAN_DELETE = True
    GRAPHS = {
        "list": {"fields": ["id", "name", "description", "content_type", "size", "created"]},
        "default": {"fields": ["id", "name", "description", "content_type", "size",
                               "is_encrypted", "metadata", "created", "modified"]},
    }
```

## Security Model

- Files require the `file_vault` permission to access
- Group is required for all uploads — no personal vault files
- Download tokens are signed with HMAC and bound to the requester's IP
- Tokens expire after TTL (default 300 seconds)
- Password protection is a second layer on top of user authentication
- Password verification (`/password` endpoint) does not decrypt the file

## Settings

| Setting | Description |
|---|---|
| `FILEVAULT_DEFAULT_TTL` | Default download token TTL in seconds (default 300) |
| `FILEVAULT_S3_BUCKET` | S3 bucket for encrypted file storage |
