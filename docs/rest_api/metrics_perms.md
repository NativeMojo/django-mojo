# Metrics Permissions REST API

This guide covers the REST API endpoints for managing metrics permissions in Django-MOJO applications. These endpoints allow administrators to control access to metrics data on a per-account basis.

## Overview

The metrics permissions system supports:
- **View Permissions**: Control who can read metrics data for specific accounts
- **Write Permissions**: Control who can record metrics data for specific accounts  
- **Account Management**: List and manage all accounts with configured permissions
- **Flexible Permission Types**: Support for single permissions, multiple permissions, or public access

## Authentication & Authorization

All metrics permission endpoints require:
- **Authentication**: User must be logged in
- **Authorization**: User must have `manage_metrics` permission OR be a superuser

## Account Types

Metrics permissions are organized by account:
- **`public`**: Publicly accessible metrics (default)
- **`global`**: System-wide metrics requiring special permissions
- **`group_<id>`**: Group-specific metrics (e.g., `group_123`)
- **Custom accounts**: Organization-specific or custom account identifiers

---

## Get Permissions

Retrieve current view and write permissions for a specific account.

**GET** `/api/metrics/permissions`

### Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `account` | string | ✅ | Account identifier to get permissions for |

### Response
```json
{
  "account": "group_123",
  "view_permissions": ["view_metrics", "view_analytics"],
  "write_permissions": ["write_metrics"],
  "status": true
}
```

---

## Set View Permissions

Set or update view permissions for an account.

**POST** `/api/metrics/permissions/view`  
**PUT** `/api/metrics/permissions/view`

### Request Body
```json
{
  "account": "group_123",
  "permissions": "view_metrics"
}
```

### Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `account` | string | ✅ | Account identifier to set permissions for |
| `permissions` | string \| array \| null | ✅ | Permission(s) to set, or null to remove |

### Permission Values
- **String**: Single permission (e.g., `"view_metrics"`)
- **Array**: Multiple permissions (e.g., `["view_metrics", "view_analytics"]`)
- **`"public"`**: Allow public access without authentication
- **`null`**: Remove all permissions (deny access)

### Response
```json
{
  "account": "group_123",
  "view_permissions": "view_metrics",
  "action": "set",
  "status": true
}
```

---

## Set Write Permissions

Set or update write permissions for an account.

**POST** `/api/metrics/permissions/write`  
**PUT** `/api/metrics/permissions/write`

### Request Body
```json
{
  "account": "group_123", 
  "permissions": ["write_metrics", "admin_metrics"]
}
```

### Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `account` | string | ✅ | Account identifier to set permissions for |
| `permissions` | string \| array \| null | ✅ | Permission(s) to set, or null to remove |

### Response
```json
{
  "account": "group_123",
  "write_permissions": ["write_metrics", "admin_metrics"],
  "action": "set", 
  "status": true
}
```

---

## Delete All Permissions

Remove all view and write permissions for an account.

**DELETE** `/api/metrics/permissions`

### Request Body
```json
{
  "account": "group_123"
}
```

### Parameters
| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `account` | string | ✅ | Account identifier to remove permissions from |

### Response
```json
{
  "account": "group_123",
  "action": "deleted",
  "status": true
}
```

---

## List Accounts with Permissions

Get all accounts that have permissions configured.

**GET** `/api/metrics/permissions/accounts`

### Response
```json
{
  "accounts": [
    {
      "account": "group_123",
      "view_permissions": ["view_metrics"],
      "write_permissions": ["write_metrics"]
    },
    {
      "account": "group_456", 
      "view_permissions": "public",
      "write_permissions": ["admin_metrics"]
    }
  ],
  "count": 2,
  "status": true
}
```

---

## Error Handling

### Common Error Responses

**401 Unauthorized**
```json
{
  "error": "Authentication required",
  "status": false
}
```

**403 Forbidden**  
```json
{
  "error": "Insufficient permissions to manage metrics permissions",
  "status": false
}
```

**400 Bad Request**
```json
{
  "error": "Missing required parameter: account",
  "status": false
}
```

---

## Usage Examples

### Basic Permission Setup

1. **Allow public read access:**
```json
POST /api/metrics/permissions/view
{
  "account": "group_123",
  "permissions": "public"
}
```

2. **Set specific permissions:**
```json
POST /api/metrics/permissions/view
{
  "account": "group_123", 
  "permissions": ["view_metrics", "view_analytics"]
}
```

3. **Grant write access:**
```json  
POST /api/metrics/permissions/write
{
  "account": "group_123",
  "permissions": "write_metrics"
}
```

### Advanced Permission Management

1. **Update existing permissions:**
```json
PUT /api/metrics/permissions/view
{
  "account": "group_123",
  "permissions": ["view_metrics", "view_reports", "view_analytics"]
}
```

2. **Remove specific permission type:**
```json
POST /api/metrics/permissions/view  
{
  "account": "group_123",
  "permissions": null
}
```

3. **Remove all permissions:**
```json
DELETE /api/metrics/permissions
{
  "account": "group_123"
}
```

---

## Best Practices

### Permission Design
- Use specific permission names (e.g., `view_group_metrics` instead of generic `view`)
- Group related permissions logically
- Start with restrictive permissions and expand as needed

### Account Management
- Use consistent naming conventions for accounts (e.g., `group_<id>`, `org_<name>`)
- Document which accounts are used for what purposes
- Regular audit of configured permissions

### Security Considerations
- Only grant `manage_metrics` permission to trusted administrators
- Use group-specific accounts for multi-tenant applications
- Regular review of accounts with `public` permissions
- Monitor usage of global metrics access

For more information on using metrics data, see the [Metrics API Guide](metrics.md).