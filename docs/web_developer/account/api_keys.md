# API Keys — REST API Reference

## Overview

API keys are long-lived JWT tokens restricted to specific IP addresses. Use them for server-to-server integrations, automation scripts, and CI/CD pipelines.

## Generate an API Key (Own Account)

**POST** `/api/auth/generate_api_key`

Requires authentication.

```json
{
  "allowed_ips": ["192.168.1.1", "10.0.0.0/24"],
  "expire_days": 90
}
```

| Field | Required | Description |
|---|---|---|
| `allowed_ips` | Yes | List of allowed IP addresses/CIDR ranges (must have at least one) |
| `expire_days` | No | Expiry in days (default 360, max 360) |

**Response:**

```json
{
  "status": true,
  "data": {
    "token": "eyJhbGci...",
    "jti": "abc123",
    "expires_at": "2025-01-15T00:00:00Z"
  }
}
```

## Generate API Key for Another User (Admin)

**POST** `/api/auth/manage/generate_api_key`

Requires `manage_users` permission.

```json
{
  "uid": 42,
  "allowed_ips": ["10.0.0.1"],
  "expire_days": 30
}
```

## Using an API Key

Include the token in the `Authorization` header exactly like a regular JWT:

```
Authorization: Bearer <api_key_token>
```

Requests from IPs not in the `allowed_ips` list will be rejected.

## Security Notes

- Store API keys securely — treat them like passwords
- Use the most restrictive IP allowlist possible
- Set short expiry periods for temporary integrations
- Each generated key is logged in the audit trail
