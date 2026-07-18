# FileVault — REST API Reference

## Overview

FileVault stores files with AES-256-GCM encryption. Files are group-scoped and accessed via signed, IP-bound download tokens.

## Permissions

- `view_vault` or `manage_vault` — list/view/update/delete vault files within your group
- **Owner or group member, per file** — `unlock` and `password` additionally check that you own the specific file or belong to its group holding `view_vault`/`manage_vault`. Holding the permission alone does not let you reach another group's file by guessing its id — a mismatched id returns `403`.

## Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/filevault/file` | List vault files |
| GET | `/api/filevault/file/<id>` | Get vault file |
| DELETE | `/api/filevault/file/<id>` | Delete vault file |
| POST | `/api/filevault/file/upload` | Upload and encrypt file |
| POST | `/api/filevault/file/<id>/unlock` | Get signed download token |
| POST | `/api/filevault/file/<id>/password` | Verify password |
| GET | `/api/filevault/file/download/<token>` | Download file (token-secured) |

## Upload a File

**POST** `/api/filevault/file/upload` (multipart/form-data)

Requires `?group=<id>` or group in request context.

```bash
curl -X POST \
  -H "Authorization: Bearer <token>" \
  -F "file=@/path/to/document.pdf" \
  -F "name=Q4 Report" \
  -F "description=Confidential financial report" \
  -F "password=optional-password" \
  "https://api.example.com/api/filevault/file/upload?group=7"
```

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 42,
    "name": "Q4 Report",
    "description": "Confidential financial report",
    "content_type": "application/pdf",
    "size": 204800,
    "is_encrypted": true,
    "created": "2024-01-15T10:00:00Z"
  }
}
```

## Get a Download Token

Requires **view access** to the file — you must be its owner or a member of its group holding `view_vault`/`manage_vault`. A file id you can't view returns `403` (no token is minted).

**POST** `/api/filevault/file/42/unlock`

```json
{
  "ttl": 300
}
```

If the file is password-protected, `password` is required in the same request — a token is not minted without it (wrong or missing password returns `403`):

```json
{
  "ttl": 300,
  "password": "the-file-password"
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "token": "eyJhbGci...",
    "download_url": "/api/filevault/file/download/eyJhbGci...",
    "ttl": 300
  }
}
```

The token is bound to the IP address of whoever called `unlock`, and expires after `ttl` seconds (default 300, **hard-capped at 3600** — a larger requested `ttl` is silently clamped and the response `ttl` reflects the clamped value). Because it's bound to the *generating* caller's IP, the resulting `download_url` is a same-network/same-session convenience, not a link you can hand to someone on a different network — their request will be rejected.

## Download a File

**GET** `/api/filevault/file/download/<token>`

No authentication header required — the token is the credential.

If the file is password-protected, include the password:

```
GET /api/filevault/file/download/<token>?password=mypassword
```

Response is a binary file download with `Content-Disposition: attachment`.

## Verify a Password

Requires the same **view access** as `unlock` (owner or group member holding `view_vault`/`manage_vault`) — you cannot probe an arbitrary file's password by id.

**POST** `/api/filevault/file/42/password`

Check if a password is correct without downloading.

```json
{"password": "mypassword"}
```

**Response:**

```json
{"status": true, "data": {"valid": true}}
```

## List Vault Files

**GET** `/api/filevault/file?group=7`

```json
{
  "status": true,
  "count": 3,
  "data": [
    {
      "id": 42,
      "name": "Q4 Report",
      "description": "Confidential financial report",
      "content_type": "application/pdf",
      "size": 204800,
      "created": "2024-01-15T10:00:00Z"
    }
  ]
}
```
