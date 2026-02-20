# Authentication — REST API Reference

## Login

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
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600,
    "user": {
      "id": 42,
      "username": "alice@example.com",
      "display_name": "Alice"
    }
  }
}
```

Store both tokens. Use `access_token` in subsequent requests.

## Authenticating Requests

Include the access token in every authenticated request:

```
Authorization: Bearer <access_token>
```

## Refreshing a Token

**POST** `/api/refresh_token`

```json
{
  "refresh_token": "eyJhbGci..."
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "access_token": "eyJhbGci...",
    "refresh_token": "eyJhbGci...",
    "expires_in": 21600
  }
}
```

Refresh before the access token expires. The refresh token itself has a longer TTL (typically 7 days).

## Get Current User

**GET** `/api/user/me`

Returns the profile of the authenticated user.

```json
{
  "status": true,
  "data": {
    "id": 42,
    "username": "alice@example.com",
    "email": "alice@example.com",
    "display_name": "Alice",
    "permissions": {"manage_reports": true},
    "is_active": true
  }
}
```

## Password Reset — Code Method

**Step 1: Request reset code**

**POST** `/api/auth/forgot`

```json
{
  "email": "alice@example.com",
  "method": "code"
}
```

A 6-digit code is sent to the email. Response always returns success (to prevent email enumeration).

**Step 2: Submit code and new password**

**POST** `/api/auth/password/reset/code`

```json
{
  "email": "alice@example.com",
  "code": "483921",
  "new_password": "newpassword123"
}
```

Returns a JWT on success (automatically logs the user in).

## Password Reset — Link Method

**Step 1: Request reset link**

**POST** `/api/auth/forgot`

```json
{
  "email": "alice@example.com",
  "method": "link"
}
```

A reset link with a signed token is emailed.

**Step 2: Submit token and new password**

**POST** `/api/auth/password/reset/token`

```json
{
  "token": "<token-from-email>",
  "new_password": "newpassword123"
}
```

Returns a JWT on success.

## Error Responses

**Invalid credentials:**

```json
{
  "status": false,
  "code": 401,
  "error": "Permission denied"
}
```

**Unauthenticated request to protected endpoint:**

```json
{
  "status": false,
  "code": 403,
  "is_authenticated": false
}
```
