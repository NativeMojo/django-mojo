# MOJO REST API Authentication

Most MOJO REST API endpoints are secured: you must log in and authenticate using JWT tokens for all requests.

---

## Logging In & Getting Your Token

Authenticate by sending your username and password to the login endpoint:

**Request:**
```
POST /api/login
Content-Type: application/json

{
  "username": "youruser",
  "password": "yourpass"
}
```

**Response:**
```
{
  "status": true,
  "data": {
    "access_token": "eyJ0eXAiOiJKV1QiLCJh...",
    "refresh_token": "eyJhbGciOi...",
    "user": { "id": 1, "username": "youruser", ... }
  }
}
```
- Keep your access token safe!
- Store the refresh token to get new tokens without re-logging in.

---

## Authenticating Subsequent Requests

For any authenticated API call, include the access token in the HTTP `Authorization` header:

```
Authorization: Bearer <access_token>
```

**Example:**
```
GET /api/project/123
Authorization: Bearer eyJ0eXAiOiJKV1QiLCJh...
```

---

## Token Expiration & Refresh

Tokens expire—when your access token is rejected, use the refresh endpoint:

**Request:**
```
POST /api/refresh_token
Content-Type: application/json

{
  "refresh_token": "<refresh_token>"
}
```

**Response:**
```
{
  "status": true,
  "data": {
    "access_token": "<new_access_token>",
    "refresh_token": "<new_refresh_token>"
  }
}
```

Use the new access token going forward.

---

## Common Authentication Issues

- **Invalid token**: You'll get a 401 or 403. Re-authenticate or refresh your token.
- **Token expired**: Use your `refresh_token` on `/api/refresh_token`.
- **No Authorization header**: Most endpoints will return 403.
- **Multiple failed logins**: Be careful, repeated failures may trigger incident reporting for your user.

---

## Quick Tips

- Always use HTTPS in production—tokens are sensitive.
- Logout by forgetting/deleting your tokens client-side; tokens may also expire or be revoked by admins.
- Contact your backend team if you need extended lifetimes, more claims, or custom login flows.

---

Happy authenticating!