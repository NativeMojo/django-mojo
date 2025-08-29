# Device Tracking API

The Device Tracking API provides endpoints for querying user device and location history. This data is collected automatically by the MOJO framework whenever a user authenticates.

**Permissions:** Access to all device tracking endpoints requires the `manage_users` permission.

---

## 1. User Devices

The `UserDevice` object represents a unique device a user has logged in with.

### List User Devices

- **Endpoint:** `GET /api/user/device`
- **Description:** Retrieves a list of all devices for all users.

**Query Parameters:**

- `user=<user_id>`: Filter devices by a specific user's ID.
- `duid=<duid>`: Filter by a specific Device Unique ID.
- `graph=locations`: Include the location history for each device in the response.

**Example Response (`graph=default`):**

```json
{
    "data": [
        {
            "duid": "ua-hash-a1b2c3d4...",
            "device_info": {
                "browser": {"family": "Chrome", "version": "108.0.0"},
                "os": {"family": "Mac OS X", "version": "10.15.7"},
                "device": {"family": "Mac", "brand": "Apple", "model": "Mac"}
            },
            "last_ip": "8.8.8.8",
            "first_seen": "2025-08-28T10:00:00Z",
            "last_seen": "2025-08-28T12:30:00Z"
        }
    ],
    "size": 1,
    "count": 1
}
```

### Get a Specific Device

- **Endpoint:** `GET /api/user/device/<id>`
- **Description:** Retrieves a single `UserDevice` by its database ID.

---

## 2. User Device Locations

The `UserDeviceLocation` object is a log entry linking a specific device to an IP address it was used from.

### List Device Locations

- **Endpoint:** `GET /api/user/device/location`
- **Description:** Retrieves a list of all device location history across all users.

**Query Parameters:**

- `user=<user_id>`: Filter locations by a specific user's ID.
- `user_device=<device_id>`: Filter locations for a specific device.
- `ip_address=<ip>`: Filter by a specific IP address.

**Example Response:**

Each location object includes a nested `geolocation` object with the resolved geographic data for the IP address.

```json
{
    "data": [
        {
            "ip_address": "8.8.8.8",
            "first_seen": "2025-08-28T10:00:00Z",
            "last_seen": "2025-08-28T12:30:00Z",
            "geolocation": {
                "ip_address": "8.8.8.8",
                "country_code": "US",
                "country_name": "United States",
                "region": "California",
                "city": "Mountain View",
                "postal_code": "94043",
                "latitude": 37.422,
                "longitude": -122.084,
                "timezone": "America/Los_Angeles",
                "provider": "ipinfo",
                "is_expired": false
            }
        }
    ],
    "size": 1,
    "count": 1
}
```

### Get a Specific Device Location

- **Endpoint:** `GET /api/user/device/location/<id>`
- **Description:** Retrieves a single `UserDeviceLocation` by its database ID.
