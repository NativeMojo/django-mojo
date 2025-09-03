# TestIt Testing Framework

TestIt is Django-MOJO's built-in lightweight test runner designed for comprehensive testing of Django applications. It provides simple decorators, REST API testing capabilities, and flexible test organization with shared state management.

## Quick Start Guide

### 1. Basic Test Structure

Create your test files in `tests/[module_name]/[test_name].py`. Tests run in the order they appear in the file.

```python
from testit import helpers as th

# Setup functions (run before tests)
@th.unit_setup()
def setup_basic_data(opts):
    """Setup outside Django environment"""
    opts.test_data = {"created_at": "2024-01-01"}

@th.django_unit_setup() 
def setup_django_models(opts):
    """Setup inside Django environment with database access"""
    from myapp.models import MyModel
    opts.test_model = MyModel.objects.create(name="Test Model")

# Test functions
@th.unit_test()
def test_basic_logic(opts):
    """Test outside Django environment"""
    assert opts.test_data["created_at"] == "2024-01-01"

@th.django_unit_test()
def test_django_functionality(opts):
    """Test inside Django environment with database access"""
    assert opts.test_model.name == "Test Model"
```

### 2. REST API Testing with opts.client

The `opts.client` provides authenticated REST API testing capabilities:

```python
@th.django_unit_test()
def test_api_endpoints(opts):
    # Unauthenticated request
    response = opts.client.get("/api/public-endpoint")
    assert response.status_code == 200
    
    # Authenticate
    opts.client.login("username", "password")
    
    # Authenticated requests
    response = opts.client.get("/api/protected-endpoint")
    assert response.status_code == 200
    
    # POST with data
    response = opts.client.post("/api/create", {"name": "Test Item"})
    assert response.status_code == 201
    
    # Logout
    opts.client.logout()
```

### 3. Running Tests

```bash
# Run all tests
/bin/testit

# Run specific module
/bin/testit -m account

# Run specific test file
/bin/testit -m account.user

# Run with verbose output
/bin/testit -v

# Stop on first failure
/bin/testit -s
```

## Core Concepts

### Test Execution Order

- **Setup functions run first** (in order they appear in file)
- **Test functions run second** (in order they appear in file)
- **State persists** in `opts` object between setup and tests
- **Tests within a file share the same `opts` instance**

### Naming Conventions

- **Setup functions**: Must be prefixed with `setup_` 
- **Test functions**: Must be prefixed with `test_`
- **Files**: Place in `tests/[module]/[test_file].py`

### The opts Object

The `opts` object is your shared state container:

```python
@th.unit_setup()
def setup_shared_data(opts):
    opts.users = []
    opts.api_key = "test-key-123"
    opts.counter = 0

@th.unit_test() 
def test_increment_counter(opts):
    opts.counter += 1
    assert opts.counter == 1

@th.unit_test()
def test_counter_persists(opts):
    # Counter value from previous test persists
    assert opts.counter == 1
    opts.counter += 5
    assert opts.counter == 6
```

## Decorators Reference

### Setup Decorators

#### @unit_setup()
Runs setup code **outside** Django environment. Use for:
- Basic data preparation
- External service mocking
- Environment variable setup

```python
@th.unit_setup()
def setup_external_data(opts):
    opts.external_api_url = "https://api.example.com"
    opts.mock_responses = {"user_id": 123}
```

#### @django_unit_setup() 
Runs setup code **inside** Django environment. Use for:
- Database model creation
- Django settings configuration
- User authentication setup

```python
@th.django_unit_setup()
def setup_test_users(opts):
    from django.contrib.auth.models import User
    opts.admin_user = User.objects.create_superuser(
        username='admin', 
        email='admin@test.com', 
        password='testpass123'
    )
    opts.regular_user = User.objects.create_user(
        username='user',
        email='user@test.com', 
        password='testpass123'
    )
```

### Test Decorators

#### @unit_test()
Runs tests **outside** Django environment. Use for:
- Pure Python logic testing
- Utility function testing
- Algorithm validation

```python
@th.unit_test()
def test_utility_functions(opts):
    from myapp.utils import calculate_score
    result = calculate_score(85, 92)
    assert result == 88.5
```

#### @django_unit_test()
Runs tests **inside** Django environment. Use for:
- Model testing
- View testing  
- Database operations
- REST API testing

```python
@th.django_unit_test()
def test_model_creation(opts):
    from myapp.models import Article
    article = Article.objects.create(
        title="Test Article",
        content="Test content"
    )
    assert article.title == "Test Article"
    assert Article.objects.count() == 1
```

## REST Client (opts.client)

The REST client is automatically available in `opts.client` for API testing.

### Authentication

```python
# Login (stores JWT token)
success = opts.client.login("username", "password")
assert success == True

# Check authentication status
assert opts.client.is_authenticated == True

# Logout (clears token)
opts.client.logout()
assert opts.client.is_authenticated == False
```

### HTTP Methods

```python
# GET request
response = opts.client.get("/api/users")
assert response.status_code == 200
assert len(response.response.data) > 0

# GET with parameters
response = opts.client.get("/api/users", params={"active": True})

# POST request
response = opts.client.post("/api/users", json={
    "username": "newuser",
    "email": "newuser@test.com"
})
assert response.status_code == 201

# PUT request  
response = opts.client.put("/api/users/1", json={
    "email": "updated@test.com"
})

# DELETE request
response = opts.client.delete("/api/users/1")
assert response.status_code == 204
```

### Response Handling

```python
response = opts.client.get("/api/users/1")

# Status code
assert response.status_code == 200

# Response data (automatically parsed JSON)
user_data = response.response.data
assert user_data.username == "testuser"

# Error handling
if response.status_code >= 400:
    error_message = response.error_reason
    print(f"API Error: {error_message}")
```

## Command Line Interface

### Basic Usage

```bash
# Run all tests
/bin/testit

# Run all tests with verbose output
/bin/testit -v

# Run tests and stop on first failure
/bin/testit -s
```

### Module and Test Selection

```bash
# Run specific module
/bin/testit -m account

# Run specific test file  
/bin/testit -m account.user_tests

# Run specific test method (use with -t)
/bin/testit -m account -t test_user_creation
```

### Utility Options

```bash
# List available tests
/bin/testit -l

# Run setup only
/bin/testit --setup

# Specify custom host for API tests
/bin/testit --host http://localhost:9000

# Show detailed errors
/bin/testit -e
```

### Advanced Options

```bash
# Run only Mojo framework tests
/bin/testit --onlymojo

# Skip Mojo framework tests
/bin/testit --nomojo

# Run quick tests only (functions prefixed with quick_)
/bin/testit -q
```

## Test Organization

### Directory Structure

```
tests/
├── account/
│   ├── __init__.py
│   ├── setup.py          # Module-wide setup
│   ├── user_tests.py     # User model tests
│   ├── auth_tests.py     # Authentication tests
│   └── api_tests.py      # API endpoint tests
├── notifications/
│   ├── __init__.py
│   ├── push_tests.py
│   └── email_tests.py
└── utils/
    ├── __init__.py
    └── helper_tests.py
```

### Module Setup Files

Create `setup.py` in test modules for module-wide initialization:

```python
# tests/account/setup.py
def run_setup(opts):
    """Module-wide setup run before any tests in this module"""
    print("Setting up account module tests...")
    # Module-wide setup code here
```

## Best Practices

### 1. Test Organization
- **Group related tests** in the same file
- **Use descriptive test names** that explain what's being tested
- **Order tests logically** - basic functionality first, complex scenarios last

### 2. State Management
- **Use opts for shared state** between setup and tests
- **Don't rely on test execution order** between different files
- **Clean up resources** in setup if tests might conflict

### 3. API Testing
- **Test both authenticated and unauthenticated endpoints**
- **Verify response status codes and data structure**
- **Test error conditions** (404, 403, 400, etc.)

### 4. Django Testing
- **Use django_unit_test for database operations**
- **Create test data in django_unit_setup**
- **Test model validation and business logic**

## Example Test Files

### Complete REST API Test Example

```python
from testit import helpers as th

@th.django_unit_setup()
def setup_test_data(opts):
    from django.contrib.auth.models import User
    from myapp.models import Article
    
    # Create test users
    opts.admin = User.objects.create_superuser(
        username='admin', 
        email='admin@test.com', 
        password='admin123'
    )
    opts.user = User.objects.create_user(
        username='testuser',
        email='user@test.com', 
        password='user123'
    )
    
    # Create test article
    opts.article = Article.objects.create(
        title="Test Article",
        content="Test content",
        author=opts.admin
    )

@th.django_unit_test()
def test_public_article_list(opts):
    """Test public article list endpoint"""
    response = opts.client.get("/api/articles")
    assert response.status_code == 200
    assert len(response.response.data) >= 1

@th.django_unit_test() 
def test_authenticated_article_creation(opts):
    """Test article creation requires authentication"""
    # First try without authentication
    response = opts.client.post("/api/articles", json={
        "title": "New Article",
        "content": "New content"
    })
    assert response.status_code == 401
    
    # Login and try again
    opts.client.login(opts.admin.username, 'admin123')
    response = opts.client.post("/api/articles", json={
        "title": "New Article", 
        "content": "New content"
    })
    assert response.status_code == 201
    assert response.response.data.title == "New Article"

@th.django_unit_test()
def test_article_permissions(opts):
    """Test article edit permissions"""
    opts.client.login(opts.user.username, 'user123')
    
    # Regular user shouldn't be able to edit admin's article
    response = opts.client.put(f"/api/articles/{opts.article.id}", json={
        "title": "Modified Title"
    })
    assert response.status_code == 403
    
    # Admin should be able to edit
    opts.client.login(opts.admin.username, 'admin123') 
    response = opts.client.put(f"/api/articles/{opts.article.id}", json={
        "title": "Modified Title"
    })
    assert response.status_code == 200
```

### Model Testing Example

```python
from testit import helpers as th

@th.django_unit_setup()
def setup_user_model_tests(opts):
    from django.contrib.auth.models import User
    opts.user_data = {
        "username": "testuser",
        "email": "test@example.com",
        "first_name": "Test",
        "last_name": "User"
    }

@th.django_unit_test()
def test_user_creation(opts):
    """Test user model creation and validation"""
    from django.contrib.auth.models import User
    
    user = User.objects.create(**opts.user_data)
    assert user.username == opts.user_data["username"]
    assert user.email == opts.user_data["email"]
    assert user.get_full_name() == "Test User"

@th.django_unit_test()
def test_user_validation(opts):
    """Test user model validation"""
    from django.contrib.auth.models import User
    from django.core.exceptions import ValidationError
    
    # Test duplicate username
    User.objects.create(**opts.user_data)
    
    try:
        User.objects.create(**opts.user_data)  # Should fail
        assert False, "Expected ValidationError for duplicate username"
    except:
        pass  # Expected to fail
```

## Troubleshooting

### Common Issues

1. **Tests not found**: Ensure functions are prefixed with `setup_` or `test_`
2. **Django import errors**: Use `@django_unit_test` for Django-dependent code
3. **Authentication failures**: Check username/password and endpoint URLs
4. **State not persisting**: Ensure you're storing data in `opts` object

### Debugging

```bash
# Run with verbose output to see detailed logs
/bin/testit -v

# Run single test for debugging
/bin/testit -m mymodule.mytest

# Show full error traces
/bin/testit -e
```

The TestIt framework provides everything you need for comprehensive testing of Django-MOJO applications, from simple unit tests to complex API integration testing.