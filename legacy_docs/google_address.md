# Get singleton instance
from mojo.helprs.location.google import get_google_api

google = get_google_api()

# Address validation
```json
{
    "address1": "123 Main St",
    "address2": "Apt 4B",
    "city": "Anytown",
    "state": "CA",
    "zip": "12345"
}
```

```python
result = google.validate_address({
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "zip": "94043"
})
```

```python
result = google.validate_address({
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "zip": "94043"
})
```

# Autocomplete
suggestions = google.get_address_suggestions("1600 Amph")

# Geocoding (address → coordinates)
coords = google.geocode_address("1600 Amphitheatre Parkway, Mountain View, CA")
print(f"Lat/Long: {coords['latitude']}, {coords['longitude']}")

# Reverse geocoding (coordinates → address)
address = google.reverse_geocode(37.4224764, -122.0842499)
print(f"Address: {address['formatted_address']}")

# Timezone lookup
tz = google.get_timezone(37.4224764, -122.0842499)
print(f"Timezone: {tz['timezone_id']} ({tz['timezone_name']})")




from mojo.helpers.location import usps


data = {
    "address1": "123 Main St",
    "address2": "Apt 4B",
    "city": "Anytown",
    "state": "CA",
    "zip": "12345"
}

resp = usps.validate_address(data)
print(resp)
{
    "valid": True,
    "source": "usps_v3",
    "standardized_address": {
        "line1": "123 MAIN ST APT 100",
        "line2": None,
        "city": "ANYTOWN",
        "state": "CA",
        "zip": "90210",
        "zip4": "1234",
        "full_zip": "90210-1234"
    },
    "metadata": {
        "residential": True,
        "business": False,
        "deliverable": True,
        "vacant": False,
        "carrier_route": "C001",
        "delivery_point": "01",
        "dpv_confirmation": "Y",
        "cmra": False  # Commercial Mail Receiving Agency (PO Box equivalent)
    },
    "corrections": {
        "address_corrected": False,
        "street_corrected": False,
        "city_state_corrected": False,
        "zip_corrected": False,
        "zip4_corrected": True
    },
    "original_address": {...}
}


or we have google apis
from mojo.helpers.location.google import get_google_api
google = get_google_api()
result = google.validate_address({
    "address1": "1600 Amphitheatre Parkway",
    "city": "Mountain View",
    "state": "CA",
    "zip": "94043"
})
print(result)
{'valid': True, 'source': 'google', 'standardized_address': {'line1': '1600 Amphitheatre Pkwy', 'line2': None, 'city': 'Mountain View', 'state': 'CA', 'zip': '94043-1351', 'zip4': None, 'full_zip': '94043-1351'}, 'metadata': {'residential': False, 'business': False, 'deliverable': True, 'vacant': False, 'latitude': 37.4216724, 'longitude': -122.0856444, 'place_id': 'ChIJPzxqWQK6j4AR3OFRJ6LMaKo', 'plus_code': '849VCWC7+MP'}, 'corrections': {'address_corrected': False, 'has_unconfirmed_components': False, 'validation_granularity': 'PREMISE'}, 'original_address': {'address1': '1600 Amphitheatre Parkway', 'city': 'Mountain View', 'state': 'CA', 'zip': '94043'}}
