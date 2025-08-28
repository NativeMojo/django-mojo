# Developer Guide: User Device & Geolocation Tracking

This document provides a guide for developers on using the User Device and Geolocation tracking feature within the MOJO framework.

---

## 1. Feature Overview

The device tracking system provides a simple and powerful way to log the devices and IP addresses used by your application's users. It is designed to be performant, scalable, and extensible.

**Core Functionality:**

-   **Device Tracking**: Identifies unique user devices via a device ID (`duid`) or a fallback hash of the user agent string.
-   **Location Logging**: Logs every IP address used by a tracked device.
-   **Asynchronous Geolocation**: Enriches IP addresses with geolocation data (country, city, etc.) using a background task to avoid impacting request-response times.
-   **Intelligent Caching**: Geolocation results are cached in the database to prevent redundant and costly API calls to external providers.

---

## 2. How It Works

The entire system is orchestrated by a single method: `UserDevice.track(request)`.

When called, this method performs the following actions:

1.  **Identifies the Device**: It looks for a `duid` in the request or generates a unique signature based on the user agent. It then finds or creates a `UserDevice` record linked to the authenticated user.
2.  **Logs the Location**: It creates or updates a `UserDeviceLocation` record, linking the device to the request's IP address.
3.  **Triggers Geolocation**: If the IP address has not been geolocated before (or if its cached data has expired), it triggers a background task to fetch the data. 
    - For **public IPs**, the result is stored in the `GeoLocatedIP` model.
    - For **private and reserved IPs** (e.g., `192.168.x.x`, `10.x.x.x`), a special record is created with metadata like "Private Network" to identify them clearly in reporting.

---

## 3. Integration

To enable tracking, you must call `UserDevice.track(request)` at a point in your code where the `request` object is available and `request.user` has been populated by the authentication middleware.

The ideal place is within your authentication flow, after a user has been successfully authenticated.

### Example: Integrating with JWT Authentication

If you are using JWTs, you can call this method right after a user successfully logs in or refreshes a token.

Here is an example of how you might modify the `on_user_login` endpoint in `mojo/apps/account/rest/user.py`:

```python
# mojo/apps/account/rest/user.py

from mojo.apps.account.models import User, UserDevice # <-- Import UserDevice

# ...

@md.POST("login")
@md.POST("auth/login")
@md.requires_params("username", "password")
def on_user_login(request):
    username = request.DATA.username
    password = request.DATA.password
    user = User.objects.filter(username=username.lower().strip()).last()
    if user is None:
        return JsonResponse(dict(status=False, error="Invalid username or password", code=403))
    if not user.check_password(password):
        user.report_incident(f"{user.username} enter an invalid password", "invalid_password")
        return JsonResponse(dict(status=False, error="Invalid username or password", code=401))
    
    user.last_login = dates.utcnow()
    user.touch()

    # --- Add Device Tracking Here ---
    UserDevice.track(request)
    # --------------------------------

    token_package = JWToken(user.get_auth_key()).create(uid=user.id)
    token_package['user'] = user.to_dict("basic")
    return JsonResponse(dict(status=True, data=token_package))

```

---

## 4. Configuration

The geolocation service is configured via your Django `settings.py` file.

-   `GEOLOCATION_PROVIDER` (Optional):
    -   Specifies which third-party geolocation service to use.
    -   **Default**: `'ipinfo'`

-   `GEOLOCATION_API_KEY_[PROVIDER_NAME]` (Optional):
    -   Your API key for the chosen provider. The key must match the provider name.
    -   **Example**: `GEOLOCATION_API_KEY_IPINFO = 'your_ipinfo_api_key_here'`
    -   **Default**: `None`

-   `GEOLOCATION_CACHE_DURATION_DAYS` (Optional):
    -   The number of days to cache a geolocation result before it is considered expired.
    -   **Default**: `30`

### Example `settings.py` Configuration

```python
# settings.py

# ...

# Geolocation Settings
GEOLOCATION_PROVIDER = 'ipinfo'
GEOLOCATION_API_KEY_IPINFO = 'your_ipinfo_api_key_here'
GEOLOCATION_CACHE_DURATION_DAYS = 60

# ...
```

---

## 5. Model Schema

-   **`UserDevice`**: Stores a record of each unique device belonging to a user.
    -   `user`: Foreign key to the User.
    -   `duid`: The unique device identifier.
    -   `last_ip`: The last IP address seen from this device.
    -   `device_info`: JSON blob containing parsed user agent data.

-   **`UserDeviceLocation`**: A log entry linking a `UserDevice` to an IP address.
    -   `user`: Foreign key to the User (for direct permission checks).
    -   `user_device`: Foreign key to the `UserDevice`.
    -   `ip_address`: The IP address used.
    -   `geolocation`: Foreign key to the `GeoLocatedIP` cache record.

-   **`GeoLocatedIP`**: The cache for geolocation data.
    -   `ip_address`: The IP address that was looked up.
    -   `country_code`, `region`, `city`, etc.: Standardized, indexed location fields.
    -   `data`: The raw JSON response from the external provider.
    -   `expires_at`: When the cache entry becomes stale.

## 5. Model Schema

-   **`UserDevice`**: Stores a record of each unique device belonging to a user.
    -   `user`: Foreign key to the User.
    -   `duid`: The unique device identifier.
    -   `last_ip`: The last IP address seen from this device.
    -   `device_info`: JSON blob containing parsed user agent data.

-   **`UserDeviceLocation`**: A log entry linking a `UserDevice` to an IP address.
    -   `user_device`: Foreign key to the `UserDevice`.
    -   `ip_address`: The IP address used.
    -   `geolocation`: Foreign key to the `GeoLocatedIP` cache record.

-   **`GeoLocatedIP`**: The cache for geolocation data.
    -   `ip_address`: The IP address that was looked up.
    -   `country_code`, `region`, `city`, etc.: Standardized, indexed location fields.
    -   `data`: The raw JSON response from the external provider.
    -   `expires_at`: When the cache entry becomes stale.
