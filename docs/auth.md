# MOJO (DJANGO-MOJO): Documentation

MOJO is a Django-based authentication library that extends Django's built-in authentication capabilities. It provides models, middleware, and views for handling user, group, and permission management, along with JWT token-based authentication.

## Table of Contents

- [Models](#models)
- [Admin Interface](#admin-interface)
- [Middleware](#middleware)
- [REST API](#rest-api)
- [JWT Authentication](#jwt-authentication)
- [Usage Examples](#usage-examples)
- [Configuration](#configuration)
- [Contributing](#contributing)
- [License](#license)

## Installation


1. Add `mojo.account` to your Django project's `INSTALLED_APPS` in `settings.py`:
   ```python
   INSTALLED_APPS = [
       ...
       'mojo.base',
       'mojo.account',
       'mojo.files'
       ...
   ]
   ```

2. Apply the migrations to set up your database:
   ```bash
   python manage.py migrate mojo.account
   ```

3. Add the necessary middleware in your `settings.py`:
   ```python
   MIDDLEWARE = [
       ...
       'mojo.account.middleware.JWTAuthenticationMiddleware',
       'mojo.account.middleware.LoggerMiddleware',
       ...
   ]
   ```

## Models

### User

- Custom model extending `AbstractBaseUser`
- Fields include `username`, `email`, `phone_number`, `permissions`, `metadata`, etc.

### Group

- Manages user groupings, hierarchical structure with parent groups.
- Fields include `name`, `uuid`, `kind`, `metadata`.

### GroupMember

- Manages the relationship between users and groups.
- Fields include `user`, `group`, `permissions`, `metadata`.

## Admin Interface

Custom Django admin views for managing users, groups, and group members are available through the admin panel.

- **UserAdmin**
  - View/Edit user details, permissions, and important dates.
  - Searchable fields include `username`, `email`.

- **GroupAdmin**
  - View/Edit group details, kind, and relationships.
  - Searchable by `name`, `uuid`, `kind`.

- **GroupMemberAdmin**
  - View/Edit membership details and permissions.
  - Filterable by `group` and active status.

## Middleware

### JWTAuthenticationMiddleware

- Authenticates requests using a Bearer JWT Authentication scheme.
- Recognizes users based on JWT tokens in the Authorization header.

### LoggerMiddleware

- Logs each incoming request and outgoing response that interacts with the `/api` endpoints.

## REST API

MOJO provides several restful endpoints for managing users and groups:

- **Users**
  - GET, POST `/api/user/`: List and create users.
  - GET, PUT, DELETE `/api/user/<int:pk>/`: Retrieve, update or delete users by ID.

- **Groups**
  - GET, POST `/api/group/`: List and create groups.
  - GET, PUT, DELETE `/api/group/<int:pk>/`: Retrieve, update or delete groups by ID.

- **Group Members**
  - GET, POST `/api/group/member/`: List and add group members.
  - GET, PUT, DELETE `/api/group/member/<int:pk>/`: Manage members.

### Example User Login

- Endpoint: `/api/login`
- Method: `POST`
- Parameters: `username`, `password`
- Response: JWT access token

## JWT Authentication

Authorization is handled using JWT (JSON Web Tokens). Each token can contain payload data about user identification and permission levels.

### Generating a Token

Access and refresh tokens are generated upon login and are managed via the `JWToken` utility class.

### Validate JWT

Tokens are validated using the `JWTAuthenticationMiddleware`, ensuring the token is active and not expired.

## Usage Examples

### Example: Creating a User

```python
from mojo.account.models import User

user = User.objects.create_user(email="test@example.com", password="password123")
user.save()
```

### Example: JWT Token Usage

```python
from mojo.account.utils.jwtoken import JWToken

user = User.objects.get(email="test@example.com")
token_manager = JWToken(key=user.auth_key)
jwt = token_manager.create(uid=user.id)
```

### Example: Using Admin Interface

Navigate to `/admin/` in your browser, and you will find the `Users`, `Groups`, and `Group Members` sections for management.

## Configuration

Certain features of MOJO can be customized in your Django settings, such as `USER_PERMS_PROTECTION`, `MEMBER_PERMS_PROTECTION`, and default token expiry times.

## Contributing

We welcome contributions! Please create an issue or submit a pull request on the GitHub repository.

## License

This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.
