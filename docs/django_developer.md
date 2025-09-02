# Django-MOJO: AI Developer's Guide

This document provides a comprehensive guide for Large Language Models (LLMs) and AI Agents to effectively build Django applications using the `django-mojo` framework. It distills the core philosophy, architecture, and conventions necessary to produce idiomatic and secure code.

## Project Philosophy

- **Explicit Simplicity:** Code should be direct and readable. Avoid "magic," excessive abstraction, or complex import schemes.
- **Separation of Concerns:** Each model, REST handler, and helper should reside in its own file.
- **Security by Default:** All endpoints enforce strong, object-level permissions using a "fail closed" model.

---

## Core Concepts & Base Models

The framework's foundation is built upon a set of powerful base models that provide out-of-the-box RESTful capabilities, secret management, and more.

### `MojoModel`: A Deep Dive

`MojoModel` is the cornerstone of the framework's REST integration. It's a mixin that adds a comprehensive suite of methods to your Django models, instantly making them API-ready. It does not add any database fields itself, but rather provides the logic for handling RESTful interactions.

**Inheritance:**

- **Standard Models:** `class MyModel(models.Model, MojoModel): ...`
- **Models with Secrets:** `class MySecretModel(MojoSecrets, MojoModel): ...` (Note: `MojoSecrets` includes `models.Model`)

#### Key Methods and Features

**1. The `on_rest_request` Entrypoint**

This class method is the single entry point for all standard CRUD operations. By routing a URL to it, you get a complete REST API for your model without writing any further view logic. It automatically handles:
- **Listing & Pagination:** `GET /api/resource/`
- **Creation:** `POST /api/resource/`
- **Retrieval:** `GET /api/resource/<pk>/`
- **Updates:** `POST` or `PUT /api/resource/<pk>/`
- **Deletion:** `DELETE /api/resource/<pk>/`

**2. CRUD Lifecycle Hooks for Customization**

While `on_rest_request` handles everything automatically, you can inject your own custom logic at key points in the lifecycle by implementing these methods on your model:

- `on_rest_pre_save(self, changed_fields, created)`: Called before a model instance is saved. `changed_fields` is a dictionary of the fields that were modified in the request, and `created` is a boolean. This is the ideal place for validation, data transformation, or triggering side effects before the data hits the database.
- `on_rest_saved(self, changed_fields, created)`: Called immediately after a model instance is saved.
- `on_rest_created(self)`: Called only when a new model instance is created.
- `on_rest_pre_delete(self)`: Called before a model instance is deleted.

**3. Automated Save Logic**

- `on_rest_save(self, request, data_dict)`: This is the core method that processes incoming data from `request.DATA` and applies it to the model instance. It intelligently handles regular fields, foreign keys (by ID), and even nested JSON data for related models.
- `on_rest_save_field(self, key, value, request)`: Handles saving a single field. It will automatically look for and call a custom setter method on your model (e.g., `set_my_field(self, value)`) if it exists, giving you fine-grained control over how specific fields are handled.
- `on_rest_update_jsonfield(self, field_name, field_value)`: If a field is a `JSONField`, this method will intelligently merge the incoming JSON data with the existing data instead of replacing it.

**4. Permission Handling**

- `rest_check_permission(cls, request, permission_keys, instance=None)`: This class method is the engine for the permission system. It's called automatically by the framework and uses the `*_PERMS` lists from your `RestMeta` to authorize requests.

**5. Serialization and Response**

- `to_dict(self, graph="default")`: Serializes a model instance into a dictionary based on the requested `graph` from `RestMeta`.
- `on_rest_get(self, request, graph="default")`: Handles the final serialization and wrapping of a model instance into a `JsonResponse`.

**6. Utility Methods**

- `active_request` & `active_user`: Properties to easily access the current request and authenticated user from within any model method.
- `atomic_save(self)`: A convenience method to save the model instance within a database transaction.
- `log(self, ...)` & `report_incident(self, ...)`: Integrated methods for logging and security incident reporting.

### `MojoSecrets`

For models that need to store encrypted data, inherit from `MojoSecrets`.

**Key Features:**

- **Encrypted JSON Field:** All secrets are stored in a single `mojo_secrets` text field, encrypted at rest.
- **Simple API:**
  - `set_secret(key, value)`: Set a secret.
  - `get_secret(key, default=None)`: Retrieve a secret.
  - `secrets`: A property that returns an `objict` of all decrypted secrets.
- **Automatic Encryption/Decryption:** The framework handles all cryptographic operations automatically upon saving and loading the model. The encryption key is derived from the model's primary key and class name.

### `RestMeta` Inner Class

The `RestMeta` inner class within a `MojoModel` is used to configure its REST API behavior.

```python
class MyModel(models.Model, MojoModel):
    # ... fields ...

    class RestMeta:
        LOG_CHANGES = True
        VIEW_PERMS = ["view_my_model", "owner"]
        SAVE_PERMS = ["manage_my_model", "owner"]
        SEARCH_FIELDS = ["name", "description"]
        GRAPHS = {
            "basic": {"fields": ["id", "name"]},
            "default": {"fields": ["id", "name", "description", "created"]}
        }
```

**Common `RestMeta` Attributes:**

- `VIEW_PERMS`, `SAVE_PERMS`, `DELETE_PERMS`: A list of permission keys required for the respective operations. The `"owner"` key grants access if the instance's `user` field matches the request user.
- `SEARCH_FIELDS`: A list of fields to be searched when a `search` query parameter is provided.
- `GRAPHS`: A dictionary defining serialization formats. See the "Serialization with GRAPHS" section below.
- `NO_SHOW_FIELDS`: A list of fields to always exclude from API responses (e.g., `password`).
- `LOG_CHANGES`: If `True`, model changes made via the REST API will be automatically logged.

---

## Key Models

### `account.User`

The `User` model is a full custom user model built on Django's `AbstractBaseUser`.

**Key Fields:**

- `username`, `email`, `display_name`
- `permissions`: A `JSONField` for storing granular, key-based permissions.
- `org`: A foreign key to the `Group` model, for multi-tenancy.
- `avatar`: A foreign key to the `fileman.File` model.

**Permissions Management:**

- `has_permission(perm_key)`: Checks if a user has a specific permission.
- `add_permission(perm_key)` / `remove_permission(perm_key)`: Modify user permissions.

### `account.Group`

The `Group` model is used for multi-tenancy, organization, and group-based permissions.

**Key Fields:**

- `name`, `kind` (e.g., "group", "organization")
- `parent`: A self-referential foreign key for creating hierarchies.
- `metadata`: A `JSONField` for storing arbitrary group-related data.

**Key Methods:**

- `get_children()`: Returns a queryset of all descendant groups.
- `get_parents()`: Returns a queryset of all ancestor groups.
- `top_most_parent`: A property that returns the root ancestor.

### `fileman.File`

The `File` model manages file uploads, storage, and metadata.

**Key Fields:**

- `filename`, `storage_filename`, `storage_file_path`
- `content_type`, `file_size`, `checksum`
- `upload_status`: Tracks the state of the upload (e.g., `pending`, `completed`, `failed`).
- `file_manager`: A foreign key to a `FileManager` instance, which defines the storage backend (e.g., local, S3) and other policies.

**Key Methods:**

- `create_from_file(file, name, ...)`: A class method to create a `File` instance from an uploaded file object.
- `request_upload_url()`: Generates a pre-signed URL for direct-to-storage uploads if the backend supports it.
- `generate_download_url()`: Generates a URL to access the file, respecting public/private settings.
- `on_rest_related_save(...)`: A powerful class method that allows creating a file from a base64-encoded string within a JSON payload when saving a related model.

---

## REST API

### The Convention: Keep It Simple

The primary way to expose a model via the REST API is to route a URL to its `on_rest_request` class method. This convention is designed to be simple, consistent, and cover 99% of CRUD use cases without any boilerplate.

**The canonical way to create a CRUD endpoint for a model is as follows:**

```python
# in myapp/rest/my_model.py
import mojo.decorators as md
from ..models import MyModel

@md.URL('my_model')
@md.URL('my_model/<int:pk>')
def on_my_model(request, pk=None):
    """
    This single function provides a full CRUD API for the MyModel object.
    """
    return MyModel.on_rest_request(request, pk)
```

By adhering to this pattern, you leverage the full power of the `MojoModel` base class, which automatically handles:
- **List (GET /my_model):** Returns a paginated list of objects.
- **Create (POST /my_model):** Creates a new object from request data.
- **Retrieve (GET /my_model/<pk>):** Returns a single object.
- **Update (PUT/POST /my_model/<pk>):** Updates an existing object.
- **Delete (DELETE /my_model/<pk>):** Deletes an object.

There is no need to write separate views or functions for each HTTP method. The framework handles permission checking, data parsing, serialization, and response generation based on your model's `RestMeta` configuration. Only deviate from this pattern for highly specialized, non-CRUD actions.

### Permissions

Permissions are checked automatically based on the `*_PERMS` attributes in `RestMeta`. The permission flow is:
1.  **Unauthenticated:** Denied unless `"all"` is in perms.
2.  **Instance Owner:** If `"owner"` is in perms and the model instance has a `user` field that matches `request.user`, access is granted.
3.  **Group:** If the request includes a `group` parameter and the model has a `group` field, group-level permissions are checked.
4.  **System/User:** Otherwise, the user's own permissions are checked.

### Serialization with `GRAPHS`

The `GRAPHS` dictionary in `RestMeta` defines different serialization "shapes" for a model. The client can request a specific graph using the `graph` query parameter (e.g., `?graph=basic`).

```python
"GRAPHS": {
    "basic": {
        "fields": ["id", "display_name", "username"],
        "graphs": {
            "avatar": "basic"  # Nest the 'basic' graph of the related 'avatar' File object
        }
    },
    "default": { ... },
    "full": {
        # An empty graph definition includes all fields
    }
}
```

### Handling Request Data with `request.DATA`

The framework automatically parses all incoming data (GET params, POST form data, JSON body) into a single `objict` (an object-like dictionary) available at `request.DATA`.

- **Always use `request.DATA`** to access incoming data.
- It supports dot notation for nested objects (e.g., `user.profile.name`).
- It handles array notation in keys (e.g., `tags[]`).

---

## Helpers

The `mojo/helpers/` directory contains a rich set of utilities.

### `logit`

A simple and powerful logging helper.

- **Usage:** `from mojo.helpers import logit`
- **Functions:** `logit.info()`, `logit.warn()`, `logit.error()`, `logit.debug()`
- **Automatic Routing:** Messages are automatically routed to different log files (`mojo.log`, `error.log`, `debug.log`).

### `request_parser` & `request`

- `from mojo.helpers.request_parser import parse_request_data`
- `parse_request_data(request)` is the function that populates `request.DATA`.
- `from mojo.helpers.request import get_remote_ip, get_user_agent` provides useful request-related utilities.

### `dates`

Timezone-aware datetime utilities.

- **Usage:** `from mojo.helpers import dates`
- **Functions:**
  - `utcnow()`: Returns a timezone-aware `datetime` object for the current time in UTC.
  - `parse_datetime(value)`: Parses various string formats into aware `datetime` objects.
  - `has_time_elsapsed(when, seconds=...)`: Checks if a certain amount of time has passed since a given `datetime`.

### `crypto`

A suite of cryptographic helpers.

- **`aes`:** `encrypt(data, password)` and `decrypt(data, password)` for symmetric encryption.
- **`hash`:** `hash(value, salt)` for SHA-256 hashing.
- **`sign`:** `generate_signature(data, secret_key)` and `verify_signature(data, signature, secret_key)` for HMAC-SHA256 signing.
- **`privpub.hybrid`:** `PrivatePublicEncryption` class for asymmetric (public/private key) encryption.

---

## Development Workflow Example

1.  **Create the Model:**
    - In `myapp/models/book.py`, define the `Book` model, inheriting from `models.Model, MojoModel`.
    - Add standard `created` and `modified` fields.
    - Add a `user` foreign key to `account.User` to enable owner-based permissions.
    - Define the `RestMeta` class with `VIEW_PERMS`, `SAVE_PERMS`, and `GRAPHS`.

2.  **Create the REST Endpoint:**
    - In `myapp/rest/book.py`, create a function `on_book(request, pk=None)`.
    - Decorate it with `@md.URL('book')` and `@md.URL('book/<int:pk>')`.
    - The function body should simply be `return Book.on_rest_request(request, pk)`.

3.  **Interact with the API:**
    - **POST /api/myapp/book:** Create a new book.
    - **GET /api/myapp/book:** List all books.
    - **GET /api/myapp/book/1:** Retrieve the book with ID 1.
    - **GET /api/myapp/book/1?graph=basic:** Retrieve the book with ID 1 using the "basic" serialization graph.

---

## Best Practices & Checklist

- **Model Inheritance:** Use `models.Model, MojoModel` for standard models and `MojoSecrets, MojoModel` for models with encrypted fields.
- **Data Access:** Always use `request.DATA`.
- **Permissions:** Define `VIEW_PERMS` and `SAVE_PERMS` in `RestMeta`. Use the `"owner"` permission for user-owned objects.
- **File Naming:** Keep filenames short and descriptive (e.g., `book.py`, not `book_model.py`).
- **Business Logic:** Place complex business logic in `app/services/` rather than in models or REST handlers.
- **Logging:** Use `logit` for all logging.
- **Secrets:** Use `MojoSecrets` for all sensitive data. Never create individual encrypted model fields.
- **Migrations:** This is a framework, not a project. **Never create migration files.** The end-user of the framework is responsible for running `makemigrations` and `migrate`.
