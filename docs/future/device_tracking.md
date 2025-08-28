# User Device & Geolocation Tracking - Design Document

This document outlines the plan for implementing a comprehensive, performant, and scalable system for tracking user devices and their locations.

## 1. Core Components

The feature will be composed of four main parts:
1.  **New Models**: Three new Django models in the `account` app to store device, location, and cached geolocation data.
2.  **Geolocation Service**: A new, extensible helper in `mojo/helpers` to handle IP address geolocation, incorporating caching and support for multiple providers.
3.  **Core Tracking Logic**: The primary `UserDevice.track(request)` method that serves as the single entry point for the system.
4.  **Documentation**: A new developer guide explaining the feature, its configuration, and how to integrate it.

---

## 2. New Models (`mojo/apps/account/models/`)

Three new models will be created to form the foundation of the tracking system.

### `GeoLocatedIP`

-   **Purpose**: Acts as a cache to store geolocation results with a standardized, indexed schema for fast querying, while preserving the original provider data.
-   **Schema**:
    -   `ip_address`: `CharField` (or `GenericIPAddressField`), indexed.
    -   `country_code`: `CharField(max_length=2, db_index=True, null=True)`
    -   `country_name`: `CharField(max_length=100, null=True)`
    -   `region`: `CharField(max_length=100, db_index=True, null=True)` (State, province, etc.)
    -   `city`: `CharField(max_length=100, null=True)`
    -   `postal_code`: `CharField(max_length=20, null=True)`
    -   `latitude`: `FloatField(null=True)`
    -   `longitude`: `FloatField(null=True)`
    -   `timezone`: `CharField(max_length=50, null=True)`
    -   `provider`: `CharField` to note the data source (e.g., 'ipinfo').
    -   `data`: `JSONField` to store the complete, raw response from the provider.
    -   `expires_at`: `DateTimeField` for cache invalidation.
    -   `created`, `modified`: Standard timestamps.

### `UserDevice`

-   **Purpose**: Represents a unique device used by a user.
-   **Schema**:
    -   `user`: `ForeignKey` to the `User` model.
    -   `duid`: A unique device identifier (indexed).
    -   `user_agent_hash`: A hash of the user agent string, used as a fallback `duid`.
    -   `device_info`: A `JSONField` to store parsed user agent data (OS, browser, device type).
    -   `last_ip`: The last known IP address for this device.
    -   `first_seen`, `last_seen`: `DateTimeField`s for tracking device usage.

### `UserDeviceLocation`

-   **Purpose**: A log linking a `UserDevice` to every IP address it uses.
-   **Schema**:
    -   `user_device`: `ForeignKey` to the `UserDevice` model.
    -   `ip_address`: The IP address used.
    -   `geolocation`: `ForeignKey` to `GeoLocatedIP` (nullable, to be populated by a background task).
    -   `first_seen`, `last_seen`: `DateTimeField`s to track when this IP was used with this device.

---

## 3. Geolocation Service (`mojo/helpers/location.py`)

-   A new file `mojo/helpers/location.py` will encapsulate all geolocation logic.
-   A primary function, `geolocate(ip_address)`, will:
    1.  Check the `GeoLocatedIP` model for a fresh, non-expired cache entry.
    2.  On a cache miss, use Django settings (e.g., `GEOLOCATION_PROVIDER`, `GEOLOCATION_API_KEY`) to call the configured third-party service.
    3.  Normalize the response and populate the standardized fields in a new `GeoLocatedIP` record.
    4.  Return the result.

---

## 4. Core Tracking Logic (`UserDevice.track()`)

-   A class method `track(request)` on the `UserDevice` model will be the single entry point.
-   **Synchronous Operations**:
    1.  Extract `user`, `ip`, `user_agent`, and `duid` from the `request`.
    2.  If `duid` is `None`, generate a fallback by hashing the user agent string.
    3.  `get_or_create` the `UserDevice` record.
    4.  `get_or_create` the `UserDeviceLocation` record.
-   **Asynchronous Operation**:
    -   To avoid blocking the request, the `track()` method will trigger a background task to execute `geolocate(ip_address)` if the IP is not already cached. This task will link the resulting `GeoLocatedIP` record to the `UserDeviceLocation`.

---

## 5. Integration & Documentation

-   The developer will be responsible for integrating the `UserDevice.track(request)` call into the application's authentication flow (e.g., after JWT validation).
-   A new developer document, `docs/device_tracking.md`, will be created to explain the feature, model schemas, configuration settings, and provide clear integration examples.
