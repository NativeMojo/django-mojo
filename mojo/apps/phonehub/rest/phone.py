"""REST endpoints for phone number operations."""

import mojo.decorators as md
from mojo.apps.phonehub.models import PhoneNumber


@md.URL('number')
@md.URL('number/<int:pk>')
def on_phone(request, pk=None):
    """Standard CRUD for phone numbers."""
    return PhoneNumber.on_rest_request(request, pk)


@md.POST('number/normalize')
@md.requires_auth()
@md.requires_params(['phone_number'])
def on_phone_normalize(request):
    """
    Normalize a phone number to E.164 format.

    POST /api/phonehub/number/normalize
    {
        "phone_number": "415-555-1234",
        "country_code": "US"  // optional
    }

    Returns:
    {
        "phone_number": "+14155551234",
        "normalized": true
    }
    """
    phone_number = request.DATA.get('phone_number')
    country_code = request.DATA.get('country_code', 'US')

    normalized = PhoneNumber.normalize(phone_number)

    if normalized:
        return {
            'status': True,
            'data': {
                'phone_number': normalized,
            }
        }
    return {
        'status': False,
        'error': 'Invalid phone number'
    }

@md.POST('number/lookup')
@md.requires_auth()
@md.requires_params(['phone_number'])
def on_phone_lookup(request):
    """
    Lookup phone number information (carrier, line type, etc.).

    POST /api/phonehub/number/lookup
    {
        "phone_number": "+14155551234",
        "force_refresh": false,  // optional
        "group": 123  // optional
    }

    Returns PhoneNumber object with lookup data.
    """
    phone_number = request.DATA.get('phone_number')
    force_refresh = request.DATA.get('force_refresh', False)

    # Lookup phone
    phone = PhoneNumber.lookup(phone_number)
    if force_refresh:
        phone.refresh()

    if phone:
        return phone.on_rest_get(request)
    return {
        'status': False,
        'error': 'Phone lookup failed'
    }
