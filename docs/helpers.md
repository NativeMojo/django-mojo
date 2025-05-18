# Django MOJO Helper Modules

This document provides an overview of the helper modules included in the `django-mojo` package. Each module offers specific utilities to enhance and simplify Django development.

## Table of Contents

1. [Cron Helper](#cron-helper)
2. [Crypto Helper](#crypto-helper)
3. [Daemon Helper](#daemon-helper)
4. [Dates Helper](#dates-helper)
5. [Faker Helper](#faker-helper)
6. [Logit Helper](#logit-helper)
7. [Modules Helper](#modules-helper)
8. [Paths Helper](#paths-helper)
9. [Redis Helper](#redis-helper)
10. [Request Helper](#request-helper)
11. [Settings Helper](#settings-helper)

## Cron Helper

The `cron.py` module provides functionality to execute scheduled functions based on cron-like time specifications.

- **`run_now()`**: Executes functions that match the current time.

- **`find_scheduled_functions()`**: Returns a list of functions scheduled to run at the current time.

- **`match_time(current_time, cron_spec)`**: Checks if the current timestamp matches the cron specification.

```python
from mojo.helpers.cron import run_now

# To execute scheduled functions
run_now()
```

## Crypto Helper

The `crypto.py` module provides methods for encryption, decryption, and hashing.

- **`encrypt(data, key)`**: Encrypts data using AES encryption.

- **`decrypt(enc_data_b64, key)`**: Decrypts data encrypted by `encrypt()`.

- **`hash_to_hex(input_string)`**: Generates a SHA-256 hex digest of a string.

- **`hash_digits(digits, secret_key)`**: Hashes a string of digits using a derived salt.

```python
from mojo.helpers.crypto import encrypt, decrypt

# Encrypt data
encrypted_data = encrypt('my sensitive data', 'secret_key')

# Decrypt data
decrypted_data = decrypt(encrypted_data, 'secret_key')
```

## Daemon Helper

The `daemon.py` module provides a base class for creating Linux daemon processes.

- **`Daemon` class**: Manages the lifecycle of a daemon process, including starting, stopping, and signal handling.

```python
from mojo.helpers.daemon import Daemon

class MyDaemon(Daemon):
    def run(self):
        while self.running:
            # Your daemon code here
            pass

# Starting the daemon
daemon = MyDaemon("my_daemon_name")
daemon.start()
```

## Dates Helper

The `dates.py` module facilitates operations with timezones and datetime parsing.

- **`parse_datetime(value, timezone)`**: Parses a date string and converts it to the specified timezone.

- **`get_local_day(timezone, dt_utc)`**: Returns the start and end of the local day in UTC for a given timezone.

```python
from mojo.helpers.dates import parse_datetime

# Parse a date with timezone
localized_date = parse_datetime('2023-10-01 10:00:00', 'Europe/Berlin')
```

## Faker Helper

The `faker.py` module is currently empty but can be used to include functionalities related to data generation using libraries like `Faker`.

## Logit Helper

The `logit.py` module provides configurable logging utility with support for colorized console output and file logging.

- **`get_logger(name, filename, debug)`**: Retrieves or creates a logger with the specified configuration.

- **`pretty_print(msg)`**: Formats and prints a message nicely in the console.

```python
from mojo.helpers.logit import get_logger

# Create a logger
logger = get_logger("AppLogger", "app.log", debug=True)
logger.info("Application started successfully!")
```

## Modules Helper

The `modules.py` module assists in dynamically loading and managing modules.

- **`module_exists(module_name)`**: Checks if a module exists.

- **`load_module(module_name, package)`**: Imports a module by name.

```python
from mojo.helpers.modules import load_module

# Load a module
my_module = load_module('my_app.module')
```

## Paths Helper

The `paths.py` module defines and configures important file paths in a Django project.

- **`configure_paths(base_dir)`**: Configures paths based on the provided base directory.

```python
from mojo.helpers.paths import configure_paths

# Configure paths
configure_paths('/path/to/django/project')
```

## Redis Helper

The `redis.py` module provides a simple way to manage Redis connections using a connection pool.

- **`get_connection()`**: Returns a Redis connection from the pool.

```python
from mojo.helpers.redis import get_connection

# Retrieve a connection
redis_conn = get_connection()
```

## Request Helper

The `request.py` module provides utilities for parsing Django HTTP requests.

- **`parse_request_data(request)`**: Converts a Django request into a dictionary, handling JSON, form data, and file uploads.

```python
from mojo.helpers.request import parse_request_data

# Parse request data
data = parse_request_data(request)
```

## Settings Helper

The `settings.py` module offers a flexible way to deal with Django settings, including default values and app-specific settings.

- **`SettingsHelper` class**: Accesses settings with support for default values and app-specific loading.

```python
from mojo.helpers.settings import settings

# Access settings
debug_mode = settings.DEBUG
```

This document outlines the functionality provided by each helper module. Adjust and utilize these modules based on the specific needs of your Django project.
