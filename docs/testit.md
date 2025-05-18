# TestIt

TestIt is a lightweight test runner and utility package designed for managing and running unit tests for Django applications. It provides a simple and flexible way to write, organize, and execute tests across different modules, using customizable logging and REST client utilities for API testing.

## Features

- **REST Client**: Simplifies interaction with REST APIs.
- **Customizable Logging**: Integrated logging to track requests, responses, and test outcomes.
- **Test Decorators**: Easily create and manage tests with decorators.
- **Test Runner**: Command-line utility to execute tests module by module or test by test.


## Usage

### 1. REST Client

`RestClient` allows you to interact with a REST API by making HTTP requests.

#### Initialization

```python
from testit.client import RestClient

client = RestClient("http://api.example.com", logger=my_logger)
```

#### Authentication

```python
client.login("username", "password")
```

#### Making Requests

```python
# GET request
response = client.get("/api/resource")

# POST request
response = client.post("/api/resource", json=data)

# PUT request
response = client.put("/api/resource/1", json=update_data)

# DELETE request
response = client.delete("/api/resource/1")
```

### 2. Test Decorators

Annotate your test functions with `@unit_test` to integrate with the test runner.

```python
from testit.helpers import unit_test

@unit_test("Sample Test for Addition")
def test_addition():
    assert 1 + 1 == 2
```

### 3. Running Tests

The test runner allows for flexible command-line execution of tests.

#### Command-Line Interface

Use the `runner.py` to manage and execute your tests.

```bash
python runner.py [-h] [-v] [-f] [-u USER] [-m MODULE] [--method METHOD] [-t TEST] [-q] [-x EXTRA] [-l] [-s] [-e] [--host HOST] [--setup]
```

#### Options

- `-m, --module`: Run only this app/module.
- `-t, --test`: Specify a specific test method to run.
- `-l, --list`: List available tests instead of running them.
- `-s, --stop`: Stop on errors.
- `--setup`: Run setup before executing tests.
- `-v, --verbose`: Enable verbose logging.

#### Examples

- **Run All Tests**

  ```bash
  python runner.py
  ```

- **Run Tests for a Specific Module**

  ```bash
  python runner.py -m module_name
  ```

- **Run a Specific Test Method**

  ```bash
  python runner.py -m module_name -t test_method_name
  ```

- **List All Available Tests**

  ```bash
  python runner.py -l
  ```

## Configuration

### Host Configuration

The `get_host` function reads the `dev_server.conf` to determine the host and port. Ensure your configuration file exists and is correctly formatted.

### Logging

Configure a logger to capture request/response logs. Customize the logging setup in your `runner.py`.

```python
opts.logger = logit.get_logger("testit", "testit.log")
```

## Conclusion

TestIt offers a comprehensive solution for managing and executing tests within Django applications. With its flexible setup and intuitive API, it supports both simple and complex test environments. Leverage the power of decorators, dynamic test discovery, and a robust REST client to enhance your testing workflows.
