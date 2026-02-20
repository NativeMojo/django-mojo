# Geolocated IP API

The Geolocated IP (GeoIP) API provides access to the system's cache of IP address geolocation data. This is a system-level endpoint primarily used for administrative and debugging purposes.

**Permissions:** Access to this endpoint requires the `manage_users` permission.

---

## 1. GeoLocatedIP Objects

The `GeoLocatedIP` object acts as a cache to store geolocation results, reducing redundant and costly calls to external API providers. The system automatically creates and updates these records in the background as new IP addresses are encountered.

### List GeoIP Records

- **Endpoint:** `GET /api/system/geoip`
- **Description:** Retrieves a paginated list of all cached `GeoLocatedIP` records.

**Query Parameters:**

- `ip_address=<ip>`: Search for a specific IP address.
- `country_code=<country_code>`: Filter by a two-letter country code (e.g., `US`, `CA`).
- `region=<region>`: Filter by region or state name.

**Example Response:**

```json
{
    "results": [
        {
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
    ],
    "count": 1,
    "pages": 1
}
```

### Get a Specific GeoIP Record

- **Endpoint:** `GET /api/system/geoip/<id>`
- **Description:** Retrieves a single `GeoLocatedIP` record by its database ID.
