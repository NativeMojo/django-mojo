# Authentication — REST API Reference

## Overview

django-mojo uses JWT (JSON Web Token) Bearer authentication. Include a token in the `Authorization` header on every authenticated request.

## Request Header

```
Authorization: Bearer <your-jwt-token>
```

## Obtaining a Token

**POST** `/api/account/login`

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
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "user": {
      "id": 42,
      "username": "alice@example.com",
      "display_name": "Alice"
    }
  }
}
```

Use the `token` value in all subsequent requests.

## Authenticated Request Example

```bash
curl -H "Authorization: Bearer eyJhbGci..." \
     https://api.example.com/api/myapp/book
```

## Token Expiry

Tokens expire. When a request returns `401`, obtain a new token by logging in again.

## Group Context

Some resources are scoped to a group (organization/tenant). Pass the group ID as a query parameter or in the POST body:

```
GET /api/myapp/resource?group=7
```

When `group` is provided, permission checks are evaluated against the user's membership and permissions within that group.

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
