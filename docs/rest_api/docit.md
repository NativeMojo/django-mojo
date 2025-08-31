# DocIt REST API Documentation

## Overview

DocIt is a powerful documentation management system built on Django-MOJO that provides hierarchical documentation organization, version control, and asset management. This API allows you to programmatically create, manage, and organize documentation collections.

## Core Concepts

- **Book**: Top-level documentation collection that contains pages and assets
- **Page**: Individual documentation pages that can be organized hierarchically
- **PageRevision**: Version history snapshots of page content
- **Asset**: Files (images, documents, etc.) associated with books

## Authentication & Permissions

All DocIt endpoints require authentication. The system uses the following permission levels:

- **VIEW_PERMS**: `['public']` - Anyone can view published content
- **SAVE_PERMS**: `['manage_docit', 'owner']` - Create/update requires manage_docit permission or ownership
- **DELETE_PERMS**: `['manage_docit', 'owner']` - Delete requires manage_docit permission or ownership

## Base URL

All DocIt endpoints are prefixed with `/api/docit/`

---

## Books API

Books are the top-level containers for documentation collections.

### List Books

```http
GET /api/docit/book
```

**Query Parameters:**
- `graph` (optional): Response format (`default`, `detail`, `list`)
- `limit` (optional): Number of results per page
- `offset` (optional): Pagination offset

**Response:**
```json
[
  {
    "id": 1,
    "title": "API Documentation",
    "slug": "api-documentation",
    "description": "Complete API reference and guides",
    "is_active": true,
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T10:30:00Z"
  }
]
```

### Get Book Details

```http
GET /api/docit/book/{id}
```

**Response (default graph):**
```json
{
  "id": 1,
  "title": "API Documentation",
  "slug": "api-documentation",
  "description": "Complete API reference and guides",
  "is_active": true,
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-15T10:30:00Z"
}
```

**Response (detail graph):**
```http
GET /api/docit/book/{id}?graph=detail
```

```json
{
  "id": 1,
  "title": "API Documentation",
  "slug": "api-documentation",
  "description": "Complete API reference and guides",
  "order_priority": 100,
  "config": {},
  "is_active": true,
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-15T10:30:00Z",
  "created_by": {
    "id": 1,
    "username": "admin"
  },
  "modified_by": {
    "id": 1,
    "username": "admin"
  }
}
```

### Create Book

```http
POST /api/docit/book
```

**Request Body:**
```json
{
  "title": "User Guide",
  "description": "Comprehensive user documentation",
  "group": 1,
  "order_priority": 50
}
```

**Response:**
```json
{
  "id": 2,
  "title": "User Guide",
  "slug": "user-guide",
  "description": "Comprehensive user documentation",
  "order_priority": 50,
  "is_active": true,
  "created": "2024-01-15T11:00:00Z",
  "modified": "2024-01-15T11:00:00Z"
}
```

### Update Book

```http
PUT /api/docit/book/{id}
```

**Request Body:**
```json
{
  "title": "Updated User Guide",
  "description": "Updated comprehensive user documentation",
  "order_priority": 75
}
```

### Delete Book

```http
DELETE /api/docit/book/{id}
```

**Response:** `204 No Content`

---

## Pages API

Pages represent individual documentation pages within books and support hierarchical organization.

### List Pages

```http
GET /api/docit/page
```

**Query Parameters:**
- `book` (optional): Filter by book ID
- `parent` (optional): Filter by parent page ID
- `is_published` (optional): Filter by publication status
- `graph` (optional): Response format

**Response:**
```json
[
  {
    "id": 1,
    "title": "Getting Started",
    "slug": "getting-started",
    "content": "# Getting Started\n\nWelcome to our documentation...",
    "is_published": true,
    "created": "2024-01-15T10:45:00Z",
    "modified": "2024-01-15T10:45:00Z"
  }
]
```

### Get Page Details

```http
GET /api/docit/page/{id}
```

**Response (detail graph):**
```http
GET /api/docit/page/{id}?graph=detail
```

```json
{
  "id": 1,
  "title": "Getting Started",
  "slug": "getting-started",
  "content": "# Getting Started\n\nWelcome to our documentation...",
  "order_priority": 100,
  "metadata": {
    "tags": ["introduction", "tutorial"],
    "author": "Documentation Team"
  },
  "is_published": true,
  "created": "2024-01-15T10:45:00Z",
  "modified": "2024-01-15T10:45:00Z",
  "book": {
    "id": 1,
    "title": "API Documentation"
  },
  "parent": null
}
```

### Create Page

```http
POST /api/docit/page
```

**Request Body:**
```json
{
  "book": 1,
  "title": "Authentication",
  "content": "# Authentication\n\nAll API requests require authentication...",
  "parent": null,
  "order_priority": 90,
  "metadata": {
    "tags": ["auth", "security"]
  }
}
```

**Response:**
```json
{
  "id": 2,
  "title": "Authentication",
  "slug": "authentication",
  "content": "# Authentication\n\nAll API requests require authentication...",
  "order_priority": 90,
  "is_published": true,
  "book": 1,
  "parent": null,
  "created": "2024-01-15T11:15:00Z",
  "modified": "2024-01-15T11:15:00Z"
}
```

### Create Child Page

```http
POST /api/docit/page
```

**Request Body:**
```json
{
  "book": 1,
  "parent": 2,
  "title": "API Keys",
  "content": "# API Keys\n\nTo authenticate with API keys...",
  "order_priority": 80
}
```

### Update Page

```http
PUT /api/docit/page/{id}
```

**Request Body:**
```json
{
  "title": "Updated Authentication",
  "content": "# Updated Authentication\n\nRevised authentication documentation...",
  "is_published": true
}
```

### Delete Page

```http
DELETE /api/docit/page/{id}
```

**Note:** Deleting a page will also delete all its child pages.

---

## Page Revisions API

Page revisions provide version control and change tracking for pages.

### List Page Revisions

```http
GET /api/docit/page/revision
```

**Query Parameters:**
- `page` (optional): Filter by page ID
- `version` (optional): Filter by version number

**Response:**
```json
[
  {
    "id": 1,
    "version": 2,
    "change_summary": "Updated authentication examples",
    "created": "2024-01-15T12:00:00Z"
  },
  {
    "id": 2,
    "version": 1,
    "change_summary": "Initial page creation",
    "created": "2024-01-15T11:15:00Z"
  }
]
```

### Get Revision Details

```http
GET /api/docit/page/revision/{id}
```

**Response (detail graph):**
```json
{
  "id": 1,
  "content": "# Authentication\n\nAll API requests require authentication...",
  "version": 2,
  "change_summary": "Updated authentication examples",
  "created": "2024-01-15T12:00:00Z",
  "page": {
    "id": 2,
    "title": "Authentication"
  },
  "created_by": {
    "id": 1,
    "username": "admin"
  }
}
```

### Create Page Revision

Revisions are typically created automatically when pages are updated, but you can also create them manually:

```http
POST /api/docit/page/revision
```

**Request Body:**
```json
{
  "page": 2,
  "content": "# Authentication\n\nManual revision content...",
  "change_summary": "Manual revision for testing"
}
```

---

## Assets API

Assets manage files (images, documents, etc.) associated with documentation books.

### List Book Assets

```http
GET /api/docit/book/asset
```

**Query Parameters:**
- `book` (optional): Filter by book ID
- `file__category` (optional): Filter by file type (`image`, `document`, etc.)

**Response:**
```json
[
  {
    "id": 1,
    "alt_text": "API workflow diagram",
    "order_priority": 100
  }
]
```

### Get Asset Details

```http
GET /api/docit/book/asset/{id}
```

**Response (detail graph):**
```json
{
  "id": 1,
  "alt_text": "API workflow diagram",
  "description": "Detailed diagram showing the API request flow",
  "order_priority": 100,
  "file": {
    "id": 15,
    "name": "api-workflow.png",
    "url": "/media/files/api-workflow.png"
  },
  "book": {
    "id": 1,
    "title": "API Documentation"
  },
  "created": "2024-01-15T11:30:00Z",
  "created_by": {
    "id": 1,
    "username": "admin"
  }
}
```

### Create Asset

```http
POST /api/docit/book/asset
```

**Request Body:**
```json
{
  "book": 1,
  "file": 15,
  "alt_text": "Authentication flow diagram",
  "description": "Visual representation of the authentication process",
  "order_priority": 90
}
```

### Update Asset

```http
PUT /api/docit/book/asset/{id}
```

**Request Body:**
```json
{
  "alt_text": "Updated authentication flow diagram",
  "description": "Revised visual representation of the authentication process",
  "order_priority": 95
}
```

### Delete Asset

```http
DELETE /api/docit/book/asset/{id}
```

---

## Response Graphs

All endpoints support different response formats using the `graph` parameter:

### Book Graphs
- `default`: Basic book information
- `detail`: Full book details including relationships
- `list`: Minimal information for lists

### Page Graphs
- `default`: Basic page information with content
- `detail`: Full page details including metadata and relationships
- `list`: Minimal information for lists
- `content_only`: Just ID, title, and content
- `tree`: Hierarchical structure with children

### PageRevision Graphs
- `default`: Basic revision information
- `detail`: Full revision with content and relationships
- `list`: Minimal revision list
- `content_only`: Just version and content

### Asset Graphs
- `default`: Basic asset information
- `detail`: Full asset details with file and book information
- `list`: Minimal asset list
- `file_info`: Asset with file details

---

## Common Use Cases

### Creating a Complete Documentation Book

1. **Create the book:**
```http
POST /api/docit/book
{
  "title": "Product Guide",
  "description": "Complete product documentation",
  "group": 1
}
```

2. **Create a homepage:**
```http
POST /api/docit/page
{
  "book": 3,
  "title": "Welcome",
  "content": "# Welcome\n\nWelcome to our product guide...",
  "order_priority": 1000
}
```

3. **Create child pages:**
```http
POST /api/docit/page
{
  "book": 3,
  "parent": 10,
  "title": "Installation",
  "content": "# Installation\n\nTo install the product...",
  "order_priority": 900
}
```

### Building a Page Hierarchy

```http
# Root page
POST /api/docit/page
{
  "book": 1,
  "title": "User Guide",
  "content": "# User Guide\n\nMain user documentation"
}

# Child page
POST /api/docit/page
{
  "book": 1,
  "parent": 5,
  "title": "Getting Started",
  "content": "# Getting Started\n\nFirst steps..."
}

# Grandchild page
POST /api/docit/page
{
  "book": 1,
  "parent": 6,
  "title": "Installation",
  "content": "# Installation\n\nDetailed installation steps..."
}
```

### Managing Page Versions

```http
# Update a page (automatically creates revision)
PUT /api/docit/page/5
{
  "content": "# Updated User Guide\n\nRevised content..."
}

# List revisions for the page
GET /api/docit/page/revision?page=5

# Get specific revision content
GET /api/docit/page/revision/3?graph=content_only
```

### Organizing Assets

```http
# Upload and associate an image
POST /api/docit/book/asset
{
  "book": 1,
  "file": 20,
  "alt_text": "Product screenshot",
  "description": "Main dashboard screenshot",
  "order_priority": 100
}

# List all images in a book
GET /api/docit/book/asset?book=1&file__category=image
```

---

## Error Handling

### Common HTTP Status Codes

- `200 OK`: Successful request
- `201 Created`: Resource successfully created
- `204 No Content`: Successful deletion
- `400 Bad Request`: Invalid request data
- `401 Unauthorized`: Authentication required
- `403 Forbidden`: Insufficient permissions
- `404 Not Found`: Resource not found
- `409 Conflict`: Constraint violation (e.g., duplicate slug)

### Error Response Format

```json
{
  "error": "Validation failed",
  "details": {
    "slug": ["This slug already exists"],
    "title": ["This field is required"]
  }
}
```

### Validation Rules

- **Book slugs** must be unique across all books
- **Page slugs** must be unique within each book
- **Page hierarchy** cannot create circular references
- **Parent pages** must belong to the same book as child pages

---

## Performance Tips

1. **Use appropriate graphs**: Use `list` graph for listings, `detail` only when needed
2. **Filter results**: Use query parameters to limit results to what you need
3. **Pagination**: Use `limit` and `offset` for large datasets
4. **Batch operations**: Consider creating multiple resources in sequence rather than individual requests

---

## SDK Examples

### Python Example

```python
import requests

# Authentication
headers = {
    'Authorization': 'Bearer your-token-here',
    'Content-Type': 'application/json'
}

# Create a book
book_data = {
    'title': 'My Documentation',
    'description': 'Comprehensive docs',
    'group': 1
}
response = requests.post('https://yourapp.com/api/docit/book', 
                        json=book_data, headers=headers)
book = response.json()

# Create a page
page_data = {
    'book': book['id'],
    'title': 'Introduction',
    'content': '# Introduction\n\nWelcome to our docs...'
}
response = requests.post('https://yourapp.com/api/docit/page',
                        json=page_data, headers=headers)
```

### JavaScript Example

```javascript
const API_BASE = 'https://yourapp.com/api/docit';
const token = 'your-token-here';

async function createBook(title, description, groupId) {
  const response = await fetch(`${API_BASE}/book`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      title,
      description,
      group: groupId
    })
  });
  return response.json();
}

async function createPage(bookId, title, content, parentId = null) {
  const response = await fetch(`${API_BASE}/page`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json'
    },
    body: JSON.stringify({
      book: bookId,
      title,
      content,
      parent: parentId
    })
  });
  return response.json();
}
```

---

## Support

For additional help with the DocIt API:
- Check the main API documentation for authentication details
- Review the Django-MOJO framework documentation for general patterns
- Use the `graph=detail` parameter to see all available fields
- Test endpoints with tools like Postman or curl