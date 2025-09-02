# DocIt Phase 1: Implementation Plan

## Overview

Phase 1 focuses on implementing the core DocIt models and basic REST API following Django-MOJO conventions. This establishes the foundation for our documentation system with proper security, permissions, and CRUD operations.

## Goals for Phase 1

1. Implement core Django models with proper Django-MOJO inheritance
2. Create basic REST API endpoints with standard CRUD operations
3. Establish proper file structure and organization
4. Implement security and permissions system
5. Create initial graphs for API responses
6. Basic model relationships and data integrity

## File Structure

```
mojo/apps/docit/
├── __init__.py
├── models/
│   ├── __init__.py
│   ├── book.py          # Book model
│   ├── page.py          # Page model
│   ├── page_revision.py # PageRevision model
│   └── asset.py         # Asset model
├── rest/
│   ├── __init__.py
│   ├── book.py          # Book REST endpoints
│   ├── page.py          # Page REST endpoints
│   ├── page_revision.py # PageRevision REST endpoints
│   └── asset.py         # Asset REST endpoints
└── services/
    ├── __init__.py
    └── docit.py         # DocIt business logic
```

## Model Implementation Details

### 1. Book Model (`models/book.py`)

**Inheritance**: `models.Model, MojoModel`

**Core Fields**:
- `title` - CharField(max_length=200)
- `slug` - SlugField(unique=True, max_length=200)
- `description` - TextField(blank=True)
- `order_priority` - IntegerField(default=0, db_index=True)
- `permissions` - CharField(max_length=500, blank=True)
- `config` - JSONField(default=dict)
- `is_active` - BooleanField(default=True)
- `created` - DateTimeField(auto_now_add=True, editable=False, db_index=True)
- `modified` - DateTimeField(auto_now=True, db_index=True)

**Foreign Keys**:
- `group` - ForeignKey to `account.Group` (CASCADE)
- `user` - ForeignKey to `account.User` (PROTECT) - for owner permissions
- `created_by` - ForeignKey to `account.User` (PROTECT)
- `modified_by` - ForeignKey to `account.User` (PROTECT)

**RestMeta Configuration**:
```python
class RestMeta:
    VIEW_PERMS = ['public']
    SAVE_PERMS = ['manage_docit', 'owner']
    DELETE_PERMS = ['manage_docit', 'owner']

    GRAPHS = {
        'default': ['id', 'title', 'slug', 'description', 'is_active', 'created', 'modified'],
        'detail': ['id', 'title', 'slug', 'description', 'order_priority', 'config',
                   'is_active', 'created', 'modified', 'created_by', 'modified_by'],
        'list': ['id', 'title', 'slug', 'description', 'is_active']
    }
```

**Fine-Grained Access Control**: The Book model's `permissions` field and `config` JSON field provide granular control over who can view specific books beyond the basic "public" access.

### 2. Page Model (`models/page.py`)

**Inheritance**: `models.Model, MojoModel`

**Core Fields**:
- `title` - CharField(max_length=200)
- `slug` - SlugField(max_length=200, db_index=True)
- `content` - TextField()
- `order_priority` - IntegerField(default=0, db_index=True)
- `metadata` - JSONField(default=dict)
- `is_published` - BooleanField(default=True, db_index=True)
- `created` - DateTimeField(auto_now_add=True, editable=False, db_index=True)
- `modified` - DateTimeField(auto_now=True, db_index=True)

**Foreign Keys**:
- `book` - ForeignKey to `Book` (CASCADE)
- `parent` - ForeignKey to self (CASCADE, null=True, blank=True)
- `user` - ForeignKey to `account.User` (PROTECT) - inherited from book permissions
- `created_by` - ForeignKey to `account.User` (PROTECT)
- `modified_by` - ForeignKey to `account.User` (PROTECT)

**RestMeta Configuration**:
```python
class RestMeta:
    VIEW_PERMS = ['public']
    SAVE_PERMS = ['manage_docit', 'owner']
    DELETE_PERMS = ['manage_docit', 'owner']

    GRAPHS = {
        'default': ['id', 'title', 'slug', 'content', 'is_published', 'created', 'modified'],
        'detail': ['id', 'title', 'slug', 'content', 'order_priority', 'metadata',
                   'is_published', 'created', 'modified', 'book', 'parent'],
        'list': ['id', 'title', 'slug', 'is_published', 'order_priority'],
        'content_only': ['id', 'title', 'content'],
        'tree': ['id', 'title', 'slug', 'order_priority', 'parent', 'children']
    }
```

### 3. PageRevision Model (`models/page_revision.py`)

**Inheritance**: `models.Model, MojoModel`

**Core Fields**:
- `content` - TextField()
- `version` - IntegerField(db_index=True)
- `change_summary` - CharField(max_length=200, blank=True)
- `created` - DateTimeField(auto_now_add=True, editable=False, db_index=True)
- `modified` - DateTimeField(auto_now=True, db_index=True)

**Foreign Keys**:
- `page` - ForeignKey to `Page` (CASCADE)
- `user` - ForeignKey to `account.User` (PROTECT) - inherited permissions
- `created_by` - ForeignKey to `account.User` (PROTECT)

**RestMeta Configuration**:
```python
class RestMeta:
    VIEW_PERMS = ['public']
    SAVE_PERMS = ['manage_docit', 'owner']
    DELETE_PERMS = ['manage_docit', 'owner']

    GRAPHS = {
        'default': ['id', 'version', 'change_summary', 'created'],
        'detail': ['id', 'content', 'version', 'change_summary', 'created', 'page'],
        'list': ['id', 'version', 'change_summary', 'created']
    }
```

### 4. Asset Model (`models/asset.py`)

**Inheritance**: `models.Model, MojoModel`

**Core Fields**:
- `order_priority` - IntegerField(default=0, db_index=True)
- `alt_text` - CharField(max_length=200, blank=True)
- `description` - TextField(blank=True)
- `created` - DateTimeField(auto_now_add=True, editable=False, db_index=True)
- `modified` - DateTimeField(auto_now=True, db_index=True)

**Foreign Keys**:
- `book` - ForeignKey to `Book` (CASCADE)
- `file` - ForeignKey to `fileman.File` (SET_NULL, null=True, blank=True)
- `user` - ForeignKey to `account.User` (PROTECT) - inherited permissions
- `created_by` - ForeignKey to `account.User` (PROTECT)

**RestMeta Configuration**:
```python
class RestMeta:
    VIEW_PERMS = ['public']
    SAVE_PERMS = ['manage_docit', 'owner']
    DELETE_PERMS = ['manage_docit', 'owner']

    GRAPHS = {
        'default': ['id', 'alt_text', 'description', 'order_priority', 'created'],
        'detail': ['id', 'alt_text', 'description', 'order_priority', 'file', 'book', 'created'],
        'list': ['id', 'alt_text', 'order_priority']
    }
```

## REST API Endpoints

### Standard CRUD Endpoints

Following Django-MOJO conventions, implement simple CRUD patterns:

**Book REST Handler (`rest/book.py`)**:
```python
@md.URL('book')
@md.URL('book/<int:pk>')
def on_book(request, pk=None):
    return Book.on_rest_request(request, pk)
```

**Page REST Handler (`rest/page.py`)**:
```python
@md.URL('page')
@md.URL('page/<int:pk>')
def on_page(request, pk=None):
    return Page.on_rest_request(request, pk)
```

**PageRevision REST Handler (`rest/page_revision.py`)**:
```python
@md.URL('page/revision')
@md.URL('page/revision/<int:pk>')
def on_page_revision(request, pk=None):
    return PageRevision.on_rest_request(request, pk)
```

**Asset REST Handler (`rest/asset.py`)**:
```python
@md.URL('book/asset')
@md.URL('book/asset/<int:pk>')
def on_book_asset(request, pk=None):
    return Asset.on_rest_request(request, pk)
```

### API Functionality

**Supported Operations**:
- `GET /api/docit/book` - List books with filtering by group
- `GET /api/docit/book/<id>` - Get single book
- `POST /api/docit/book` - Create new book
- `PUT /api/docit/book/<id>` - Update book
- `DELETE /api/docit/book/<id>` - Delete book

Similar patterns for all models.

**Graph Usage**:
- Default graph for standard responses
- `?graph=detail` for complete information
- `?graph=list` for condensed listings
- `?graph=content_only` for Page content retrieval
- `?graph=tree` for Page hierarchy

## Security & Permissions

### Permission Strategy

**Simplified Approach**: All models use the same basic permission pattern:
- `VIEW_PERMS = ['public']` - Public read access by default
- `SAVE_PERMS = ['manage_docit', 'owner']` - Only managers and owners can edit
- `DELETE_PERMS = ['manage_docit', 'owner']` - Only managers and owners can delete

**Fine-Grained Control**: The Book model provides additional access controls:
- `permissions` field - Comma-separated permission strings for specific access
- `config` JSON field - Custom access rules and visibility settings
- `is_active` flag - Quick enable/disable for entire book

**Permission Strings**:
- `manage_docit` - Full DocIt management permissions
- Custom permissions defined in Book `permissions` field for specific access control

This keeps the RestMeta simple while allowing flexible access control at the Book level.

## Data Integrity & Business Logic

### Model Methods

**Book Model**:
- Auto-generate slug from title
- Cascade delete to pages and assets
- Track creation/modification users

**Page Model**:
- Auto-generate slug from title (unique within book)
- `full_path` property for hierarchical URLs
- `get_children()` and `get_descendants()` methods
- Prevent circular parent relationships

**PageRevision Model**:
- Auto-increment version numbers
- Store content snapshots
- Immutable once created

**Asset Model**:
- `is_image` property for file type checking
- Integration with fileman File model

### Services Layer (`services/docit.py`)

Business logic for:
- Page hierarchy management
- Revision creation and management
- Asset organization
- Content processing preparation (Phase 2)

## Database Considerations

### Indexes
- All timestamp fields (`created`, `modified`) have db_index=True
- `order_priority` fields have db_index=True for sorting
- `is_published`, `is_active` for filtering
- `slug` fields for URL lookups

### Constraints
- Unique slugs within appropriate scope
- Foreign key constraints with proper CASCADE/PROTECT
- Non-null constraints on required fields

## Validation & Error Handling

### Model Validation
- Title length limits
- Slug uniqueness validation
- Parent-child relationship validation (no circles)
- Permission string format validation

### API Validation
- Required field validation through Django-MOJO
- Permission enforcement through RestMeta
- Data sanitization through `request.DATA`

## Migration Strategy

1. Create initial models with all fields and relationships
2. Add indexes for performance
3. Create initial permissions in Django admin
4. Test data creation and API access

## Testing Approach (Preparation for Phase 3)

### Model Tests
- Model creation with required fields
- Relationship integrity
- Permission inheritance
- Business logic methods

### REST API Tests
- CRUD operations for all models
- Permission enforcement
- Graph response validation
- Error condition handling

### Integration Tests
- Book → Page → Asset relationships
- User/Group permission scenarios
- Revision tracking functionality

## Success Criteria

Phase 1 is complete when:

1. ✅ All four models implemented with proper Django-MOJO inheritance
2. ✅ REST endpoints functional for basic CRUD operations
3. ✅ Permission system enforces group/user/owner access patterns
4. ✅ Graph responses work for all defined graph types
5. ✅ Database migrations run cleanly
6. ✅ Basic model relationships and data integrity enforced
7. ✅ File structure follows Django-MOJO conventions
8. ✅ All models properly log operations using logit
9. ✅ Services layer established for business logic
10. ✅ Ready for Phase 2 content processing features

## Next Steps (Phase 2 Preview)

Phase 2 will add:
- Markdown processing to HTML/AST
- Markdown Plugin Support

Phase 3 will add:
- Content search and indexing
- Advanced page operations (move, copy, etc.)
- Asset management features
- Template and plugin system
- Export capabilities

This Phase 1 foundation ensures we have solid models, security, and API patterns before adding complex content processing features.
