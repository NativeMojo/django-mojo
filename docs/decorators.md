# MOJO Decorators

MOJO provides a set of utility decorators to simplify common tasks in Django applications. These decorators can be used to enhance your Django views with route registration, request validation, error handling, and function scheduling with cron syntax. Below is a guide on how to use each of these decorators with clear examples.

## Table of Contents
- [Installation](#installation)
- [HTTP Route Decorators](#http-route-decorators)
  - [Usage](#usage)
  - [Examples](#examples)
- [Validation Decorators](#validation-decorators)
  - [Usage](#usage-1)
  - [Examples](#examples-1)
- [Error Handling Decorator](#error-handling-decorator)
- [Authentication Decorators](#authentication-decorators)
  - [Usage](#usage-2)
  - [Examples](#examples-2)
- [Cron Scheduling Decorator](#cron-scheduling-decorator)
  - [Usage](#usage-3)
  - [Examples](#examples-3)

## HTTP Route Decorators

MOJO provides decorators to register your view functions with specific HTTP methods (GET, POST, PUT, DELETE) easily.

### Usage

- `@URL(pattern)`: Registers a view for any HTTP method.
- `@GET(pattern)`: Registers a view for the GET method.
- `@POST(pattern)`: Registers a view for the POST method.
- `@PUT(pattern)`: Registers a view for the PUT method.
- `@DELETE(pattern)`: Registers a view for the DELETE method.

Each takes:

| Parameter | Type   | Description                         |
|-----------|--------|-------------------------------------|
| `pattern` | `str`  | route pattern (e.g. `"foo/"`)       |
| `docs`    | `dict` | Optional OpenAPI docs metadata      |

### Examples

```python
from mojo.decorators.http import GET, POST

# Register a GET endpoint
@GET('hello/')
def hello_view(request):
    return JsonResponse({"message": "Hello, World!"})

# Register a POST endpoint with docs (openapi, swagger, etc)
@POST("submit/", docs={
    "summary": "Submit form data",
    "description": "This endpoint handles form submissions",
    "parameters": [
        {
            "name": "graph",
            "in": "query",
            "schema": {"type": "string", "default": "default"}
        }
    ],
    "responses": {
        "200": {
            "description": "Successful form submission"
        }
    }
})
def submit_form(request):
    data = request.DATA  # Contains parsed request data
    # Process data...
    return JsonResponse({"message": "Data submitted successfully!"})
```

## Validation Decorators

The `requires_params` decorator ensures that the necessary parameters are present in the request.

### Usage

- `@requires_params(*required_params)`: Checks for the presence of `required_params` in the request data.

### Examples

```python
from mojo.decorators.validate import requires_params

@requires_params('username', 'password')
@POST('login/')
def login_view(request):
    username = request.DATA['username']
    password = request.DATA['password']
    # Perform login logic...
    return JsonResponse({"message": "Logged in successfully!"})
```

## Error Handling Decorator

MOJO automatically wraps your views with an error handling mechanism that logs exceptions and returns appropriate HTTP responses. No setup is needed beyond registering your routes with MOJO decorators.

## Authentication Decorators

Introduce security by ensuring that users are authenticated and have the necessary permissions before accessing certain views.

### Usage

- `@requires_auth`: Ensures that the user is authenticated.
- `@requires_perms(*required_perms)`: Verifies that the user has the specified permissions.

### Examples

```python
from mojo.decorators.auth import requires_auth, requires_perms

@requires_auth
@GET('profile/')
def profile_view(request):
    # Logic to fetch and return user profile
    return JsonResponse({"message": "User profile retrieved successfully."})

@requires_perms('admin', 'edit')
@POST('edit/')
def edit_view(request):
    # Logic to handle edit operations
    return JsonResponse({"message": "Edit successful."})
```

## Automatic Docs for Model Routes

When you use the `URL()` decorator with `method="ALL"` on a standard model endpoint, the framework:

- Assumes the last part of the path is the model name
- Infers Django model via `apps.get_model(app_name, model_name)`
- Supports:
  - `GET /model` for list with filters
  - `GET /model/{pk}` for retrieve
  - `POST /model` to create
  - `POST /model/{pk}` to update
  - `DELETE /model/{pk}` to delete
- Uses `RestMeta.GRAPHS` for serialization

### List endpoint query params:

| Name      | Description                              |
|-----------|------------------------------------------|
| `graph`   | Serialization graph (default: `default`) |
| `size`    | Max number of items per page             |
| `start`   | Starting index                           |
| `sort`    | Field to sort by                         |
| `dr_start`, `dr_end`, `dr_field` | Date range filters |
| ...       | Any model field is allowed as a filter   |


## Cron Scheduling Decorator

Schedule functions to run at specific intervals using cron syntax.

### Usage

Attach the `@schedule` decorator to a function to schedule it based on a cron pattern.

```python
from mojo.decorators.cron import schedule

# Schedule this task to run every hour
@schedule(minutes='0')
def hourly_task():
    print("This task runs hourly.")
```

### Examples

```python
@schedule(minutes='0', hours='*/3')
def every_three_hours_task():
    print("This task runs every three hours.")
```

For detailed cron syntax, each parameter such as `minutes`, `hours`, `days`, `months`, and `weekdays` supports cron expressions and defaults to '*', meaning it will trigger for each respective time unit.

With these decorators, you can securely and efficiently enhance your Django application with routing, validation, error handling, and scheduling features provided by MOJO.
