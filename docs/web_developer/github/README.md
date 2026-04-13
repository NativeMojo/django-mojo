# GitHub App — REST API Reference

## Overview

The github app exposes GitHub App installation records via a standard REST CRUD endpoint. Each record tracks one GitHub App installation linked to a group (or globally).

For GitHub OAuth login (sign in with GitHub), see [OAuth / Social Login](../account/oauth.md).

---

## Endpoints

| Method | Path | Permission | Description |
|---|---|---|---|
| GET | `/api/github/github_install` | `view_github` | List installations |
| POST | `/api/github/github_install` | `manage_github` | Create an installation record |
| GET | `/api/github/github_install/<id>` | `view_github` | Get installation detail |
| POST/PUT | `/api/github/github_install/<id>` | `manage_github` | Update an installation record |
| DELETE | `/api/github/github_install/<id>` | `manage_github` | Delete an installation record |

Any of `github`, `view_github`, or `manage_github` satisfies the view permission. Only `github` or `manage_github` satisfies the save/delete permission.

---

## List Installations

**GET** `/api/github/github_install`

**Permission:** `view_github`

**Response:**

```json
{
  "status": true,
  "count": 2,
  "data": [
    {
      "id": 1,
      "installation_id": 12345678,
      "account_name": "myorg",
      "token_expires_at": "2026-04-13T10:00:00Z",
      "created": "2026-04-01T08:00:00Z"
    },
    {
      "id": 2,
      "installation_id": 87654321,
      "account_name": "anotherorg",
      "token_expires_at": "2026-04-13T11:30:00Z",
      "created": "2026-04-10T14:00:00Z"
    }
  ]
}
```

Add `?graph=default` for full details including `permissions`, `metadata`, and `group`.

---

## Get Installation Detail

**GET** `/api/github/github_install/<id>`

**Permission:** `view_github`

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 1,
    "installation_id": 12345678,
    "account_name": "myorg",
    "token_expires_at": "2026-04-13T10:00:00Z",
    "permissions": {
      "contents": "read",
      "metadata": "read"
    },
    "metadata": {
      "repo_full_name": "myorg/myrepo"
    },
    "group": {
      "id": 5,
      "name": "My Group"
    },
    "created": "2026-04-01T08:00:00Z",
    "modified": "2026-04-13T09:45:00Z"
  }
}
```

---

## Create Installation

**POST** `/api/github/github_install`

**Permission:** `manage_github`

```json
{
  "installation_id": 12345678,
  "account_name": "myorg",
  "group": 5,
  "metadata": {
    "repo_full_name": "myorg/myrepo"
  }
}
```

`group` is optional. Omit it for a global (project-wide) installation.

**Response:** Full default graph of the created record.

---

## Update Installation

**POST** `/api/github/github_install/<id>`

**Permission:** `manage_github`

```json
{
  "metadata": {
    "repo_full_name": "myorg/other-repo"
  }
}
```

Only the fields provided are updated.

---

## Delete Installation

**DELETE** `/api/github/github_install/<id>`

**Permission:** `manage_github`

**Response:**

```json
{
  "status": true
}
```

---

## Response Fields

### List Graph

| Field | Description |
|---|---|
| `id` | Record ID |
| `installation_id` | GitHub's numeric installation ID |
| `account_name` | GitHub org or user login |
| `token_expires_at` | When the cached installation token expires (ISO 8601) |
| `created` | Record creation timestamp |

### Default Graph

All list fields plus:

| Field | Description |
|---|---|
| `permissions` | Object — permissions granted by the GitHub installation |
| `metadata` | Object — app-specific data (e.g. linked repo names) |
| `group` | Linked group object (`id`, `name`) or `null` for global installations |
| `modified` | Last-modified timestamp |

The installation access token is never included in any response.

---

## Permissions Summary

| Permission | Access |
|---|---|
| `github` | View and manage installations |
| `view_github` | View installations (read-only) |
| `manage_github` | Full create / update / delete access |

---

## See Also

- [OAuth / Social Login](../account/oauth.md) — Sign in with GitHub (user login)
