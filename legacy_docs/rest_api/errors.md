# Errors & Error Handling in the MOJO REST API

Every robust API returns clear error messages to help clients debug and recover from issues quickly. This guide explains the structure, meaning, and best practices for handling errors from MOJO-powered REST APIs.

---

## Error Response Structure

Most errors are returned as JSON objects with at least the following fields:

```json
{
  "status": false,
  "error": "Permission denied: Project",
  "code": 403,
  "is_authenticated": true
}
```

- **status**: Always `false` for errors.
- **error**: Human-readable description of what went wrong.
- **code**: HTTP status code (see table below).
- **is_authenticated**: Was the user/session authenticated in this request?

Other fields (like `details`, `fields`, or contextual data) may be included for validation or business errors.

---

## Common HTTP Status Codes

| Code | Meaning                 | Typical Scenarios                                       |
|------|-------------------------|---------------------------------------------------------|
| 400  | Bad Request             | Bad input data, validation failed, missing params       |
| 401  | Unauthorized            | No/invalid authentication (login required)              |
| 403  | Forbidden               | Authenticated but not allowed (permissions missing)     |
| 404  | Not Found               | Resource does not exist or inaccessible                 |
| 409  | Conflict                | Resource already exists, or data integrity conflict     |
| 422  | Unprocessable Entity    | Well-formed but invalid data (e.g. validation errors)   |
| 429  | Too Many Requests       | Rate limiting, abuse prevention                         |
| 500  | Internal Server Error   | Unhandled error or server malfunction                   |

*Note: Most errors returned by MOJO will use 400/401/403/404 for client-side issues you can fix.*

---

## Example Error Scenarios

### 1. Not Authenticated (401)
```json
{
  "status": false,
  "error": "Authentication required",
  "code": 401,
  "is_authenticated": false
}
```

### 2. Permission Denied (403)
```json
{
  "status": false,
  "error": "Permission denied: User",
  "code": 403,
  "is_authenticated": true
}
```

### 3. Validation Error (400/422)
```json
{
  "status": false,
  "error": "Validation failed",
  "code": 400,
  "fields": {
    "email": "Enter a valid email address."
  }
}
```

### 4. Resource Not Found (404)
```json
{
  "status": false,
  "error": "Project not found",
  "code": 404
}
```

---

## Best Practices for Clients

- **Check the `status` field**: If `false`, don't trust or use the data—it's an error.
- **Read the `error` and `code`**: Use these for debugging and user feedback.
- **Handle `is_authenticated`**: If false, prompt for login/refresh token.
- **On validation errors**: Display field-specific errors to the user.
- **Log server or unknown errors**: For `500` or unhandled codes, consider escalating.

---

## Troubleshooting Checklist

- **401/403?** Check your token, permissions, or login state.
- **400/422?** Review all required fields, input types, and validation data.
- **404?** Double-check the object's ID, tenant/group visibility, or permissions.
- **429?** Slow down—you're sending too many requests.
- **500?** Wait and retry—if persists, contact API support.

---

## Error Returns for REST API Bulk/Batch Actions

Some endpoints may return lists or mappings indicating which items failed and which succeeded. Always look for item-specific `status`/`error` information in such cases.

---

## More Help

If you keep seeing unexplained errors:
- Review the full response body.
- Check authentication and group/tenant context.
- Review [permissions guide](../permissions.md) for more detail.
- If the problem persists, reach out to backend support with the error response.

---

**Good error handling leads to great client apps!**