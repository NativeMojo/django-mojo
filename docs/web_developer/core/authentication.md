# Authentication — REST API Reference

## Overview

django-mojo uses JWT (JSON Web Token) Bearer authentication. Include a token in the `Authorization` header on every authenticated request.

## Request Header

```
Authorization: Bearer <your-jwt-token>
```

## Obtaining a Token

**POST** `/api/login`

```json
{
  "username": "alice@example.com",
  "password": "mysecretpassword"
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "user": {
      "id": 42,
      "username": "alice@example.com",
      "display_name": "Alice"
    }
  }
}
```

Store both tokens. Use `access_token` in the `Authorization` header on all subsequent requests; `refresh_token` is only sent to the refresh endpoint below.

## Authenticated Request Example

```bash
curl -H "Authorization: Bearer eyJhbGci..." \
     https://api.example.com/api/myapp/book
```

## Token Expiry

The access token expires — default 6 hours, configured server-side by `JWT_TOKEN_EXPIRY`. The lifetime is **not** returned in the login response; decode the JWT's `exp` claim if you need it client-side.

When a request returns `401`, exchange the refresh token for a fresh pair rather than sending the user back through login:

**POST** `/api/refresh_token`

```json
{
  "refresh_token": "eyJhbGci..."
}
```

The response carries a new `access_token` and `refresh_token`. The refresh token has its own longer TTL (default 7 days, `JWT_REFRESH_TOKEN_EXPIRY`); once that expires, the user must log in again.

See [Account Authentication](../account/authentication.md) for the full token lifecycle, storage guidance, and MFA flows.

## Group Context

Some resources are scoped to a group (organization/tenant). Pass the group ID as a query parameter or in the POST body:

```
GET /api/myapp/resource?group=7
```

When `group` is provided, permission checks are evaluated against the user's membership and permissions within that group. Only **active** groups resolve — a deactivated group's id is treated exactly like an unknown one: the request behaves as if no group was passed (typically a `403` on member-scoped resources).

## Public Endpoints

Some endpoints require no authentication (e.g., registration, health checks). These are documented per-app. Unauthenticated requests to protected endpoints return:

```json
{
  "status": false,
  "code": 401,
  "error": "Authentication required",
  "is_authenticated": false
}
```

## Permission Errors

If authenticated but lacking the required permission:

```json
{
  "status": false,
  "code": 403,
  "error": "GET permission denied: Book",
  "is_authenticated": true
}
```
